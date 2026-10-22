"""Read-only current blockers, distinct from the immutable provider ledger.

A lost writing response stays unknown for accounting/retry purposes.  It need
not poison a later, independently committed replacement of the same writing
target.  Audio/preview unknowns and incomplete provenance remain conservative.
"""
from __future__ import annotations

from datetime import datetime

from .storage import BusinessError


UNKNOWN = frozenset({"timeout_unknown", "outcome_unknown"})


def _identity(ref):
    return tuple(ref.get(key) for key in ("kind", "id", "revision", "content_hash")) if isinstance(ref, dict) else None


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _target(spec):
    product = _identity(spec.get("product_ref"))
    variant, number = spec.get("variant_id"), spec.get("version_number")
    if not product or not all(product) or not isinstance(variant, str) or not variant or not isinstance(number, int):
        return None
    return product, variant, number


def _changed_modules(spec):
    return {row.get("key") for row in spec.get("modules", []) if row.get("action") in {"generate", "patch", "disable"}}


def _replacement(service, connection, prep, jobs, source, item):
    """Prove a later selected formal result; names/numbers alone never suffice."""
    if source.get("kind") != "scripts" or source.get("status") in {"queued", "running"}:
        return None
    spec = item.get("input", {})
    target = _target(spec)
    attempts = [row for row in item.get("provider_attempts", []) if row.get("state") in UNKNOWN]
    if not target or not attempts:
        return None
    moments, affected = [], set()
    for attempt in attempts:
        moment = _time(attempt.get("sent_at") or item.get("sent_at"))
        purpose = attempt.get("purpose", "")
        if moment is None or not purpose.startswith("copywriting."):
            return None
        moments.append(moment)
        if purpose == "copywriting.whole_script":
            affected.update(row.get("key") for row in spec.get("modules", [])
                if row.get("action") in {"generate", "patch"})
        else:
            affected.add(purpose.removeprefix("copywriting."))
    if not affected or None in affected:
        return None
    last_unknown = max(moments)
    for selected_ref in prep.get("script_refs", []):
        ref, visited = selected_ref, set()
        # A manual edit retains provenance but gets a new ref. Follow its exact
        # parent to the committed result instead of trusting copied provenance.
        while ref and _identity(ref) not in visited:
            visited.add(_identity(ref))
            if len(visited) > 128:
                break
            try:
                record = service._artifact(connection, ref, "script_revision", prep["id"])
            except BusinessError:
                break
            body = record["body"]
            if _target({"product_ref": body.get("product_ref"), "variant_id": body.get("variant_id"),
                        "version_number": body.get("version_number")}) != target:
                break
            provenance = body.get("generation_provenance", {})
            replacement_job = jobs.get(provenance.get("job_id"))
            if (replacement_job and replacement_job.get("id") != source.get("id")
                    and replacement_job.get("kind") == "scripts"
                    and replacement_job.get("status") not in {"queued", "running"}
                    and replacement_job.get("preparation_id") == prep["id"]):
                created = _time(replacement_job.get("created_at"))
                for result in replacement_job.get("items", []):
                    completed = _time(result.get("completed_at"))
                    result_spec = result.get("input", {})
                    if (created and completed and created > last_unknown and completed > last_unknown
                            and result.get("item_id") == provenance.get("item_id")
                            and result.get("status") == "succeeded" and result.get("call_state") == "result_received"
                            and not any(row.get("state") in UNKNOWN for row in result.get("provider_attempts", []))
                            and _target(result_spec) == target
                            and affected <= _changed_modules(result_spec)
                            and _identity((result.get("output") or {}).get("script_ref")) == _identity(record["ref"])):
                        return {"selected_script_ref": selected_ref, "replacement_script_ref": record["ref"],
                            "replacement_job_id": replacement_job["id"], "replacement_item_id": result["item_id"],
                            "replaced_module_keys": sorted(affected)}
            ref = body.get("parent_ref")
    return None


def observe_job_outcomes(service, connection, prep):
    """Keep all unresolved results visible; only proven replaced writing is historical."""
    records = [row for row in service.store.all(connection, "prelive_job")
        if row["body"].get("preparation_id") == prep["id"]]
    jobs = {row["ref"]["id"]: {**row["body"], "id": row["ref"]["id"]} for row in records}
    current, historical, unrelated, historical_audio = [], [], [], []
    selected_products = {_identity(ref) for ref in prep.get("selected_product_refs", [])}
    target_set = set()
    for ref in prep.get("script_refs", []):
        body = service._artifact(connection, ref, "script_revision", prep["id"])["body"]
        target_set.add(_target({"product_ref": body.get("product_ref"), "variant_id": body.get("variant_id"),
            "version_number": body.get("version_number")}))
    if prep.get("plan_ref"):
        plan = service._artifact(connection, prep["plan_ref"], "script_plan", prep["id"])["body"]
        target_set.update(_target(item) for item in plan.get("items", []))
    required_ids = None
    if prep.get("requirements_ref"):
        requirements = service._artifact(connection, prep["requirements_ref"], "requirements", prep["id"])["body"]
        required_ids = {row["requirement_id"] for row in requirements.get("items", [])}
    for job in jobs.values():
        for item in job.get("items", []):
            if not (item.get("call_state") in UNKNOWN or any(
                    row.get("state") in UNKNOWN for row in item.get("provider_attempts", []))):
                continue
            evidence = {"job_id": job["id"], "item_id": item.get("item_id"), "kind": job.get("kind"),
                "unknown_attempt_ids": [row.get("attempt_id") for row in item.get("provider_attempts", [])
                    if row.get("state") in UNKNOWN]}
            spec = item.get("input", {})
            target = _target(spec)
            outside_current = (job.get("kind") == "scripts" and target is not None
                and (target[0] not in selected_products or target not in target_set))
            # Requirement IDs include exact text/voice/style. An old approval
            # alone does not make its still-needed audio a different request.
            requirement_id = spec.get("requirement_id")
            if (job.get("kind") in {"audio", "preview"} and required_ids is not None
                    and requirement_id and requirement_id not in required_ids
                    and job.get("requirements_ref") != prep.get("requirements_ref")):
                outside_current = True
            if outside_current and job.get("status") not in {"queued", "running"}:
                unrelated.append({**evidence, "reason": "not_in_current_preparation_dependencies",
                    "provider_outcome": "still_unknown", "retry_authorized": False})
                continue
            from .audio_reissue import replacement_evidence
            audio_replacement = replacement_evidence(service, connection, prep, job, item)
            if audio_replacement:
                historical_audio.append({**evidence, **audio_replacement,
                    "reason": "human_authorized_reissue_committed_current_audio",
                    "provider_outcome": "still_unknown", "retry_authorized": False})
                continue
            replacement = _replacement(service, connection, prep, jobs, job, item)
            if replacement:
                historical.append({**evidence, **replacement, "reason": "selected_later_committed_script_replaces_target",
                    "provider_outcome": "still_unknown", "retry_authorized": False})
            else:
                current.append(evidence)
    return {"unknown_items": current, "historical_unknown_script_items": historical,
        "historical_unknown_audio_items": historical_audio,
        "unrelated_historical_unknown_items": unrelated,
        "pending_job_ids": [job["id"] for job in jobs.values() if job.get("status") in {"queued", "running"}]}
