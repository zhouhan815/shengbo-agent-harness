"""Exact receipts for successful business reads, never semantic acceptance.

This module adds no reader or permission. Original query responses are returned
unchanged; safe immutable snapshots use RuntimeEngine's existing history ACL.
"""
from __future__ import annotations

from copy import deepcopy
import re

from pydantic import ValidationError

from shengbo_contracts.business import MODULE_KEYS
from shengbo_contracts.common import ArtifactRef, RecordRef, SCHEMA_VERSION, ContractError, canonical_hash
from .observability import sanitize


BUSINESS_READ_KIND = "business_query_result"
_QUERIES = {"read_script": "script_revision", "read_preparation": "preparation"}
_PREPARATION_REFS = {"playlist_ref": "playlist", "compiled_plan_ref": "compiled_plan",
    "requirements_ref": "requirements", "approval_ref": "approval",
    "audio_manifest_ref": "audio_manifest", "candidate_publication_ref": "publication"}


def canonical_read_args(query, args):
    """Keep the additive empty preparation view identical to its old wire.

    A nonempty view remains a distinct query and receipt. Do not remove other
    defaults: read_script already included its empty sections in historical
    requests, and malformed views must retain their conservative metadata.
    """
    if query == "read_preparation" and args.get("sections") == []:
        result = deepcopy(args)
        result.pop("sections")
        return result
    return args


def _exact_ref(value, kind):
    """Validate an observed reference without adding fields or certifying it."""
    try:
        model = (ArtifactRef if isinstance(value, dict) and "schema_version" in value else RecordRef).model_validate(value, strict=True)
        if (model.kind != kind or not model.id or len(model.id) > 256
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", model.content_hash)
                or isinstance(model, ArtifactRef) and model.schema_version != SCHEMA_VERSION):
            return None
    except (ValidationError, TypeError, ValueError):
        return None
    return deepcopy(value)


def _script_observation(body):
    identity, missing, invalid = {}, [], []
    for key, bound in (("product_id", 256), ("variant_id", 256)):
        value = body.get(key)
        if key not in body:
            missing.append(key)
        elif not isinstance(value, str) or not value or len(value) > bound:
            invalid.append(key)
        else:
            identity[key] = value
    for key in ("version_number", "script_revision"):
        value = body.get(key)
        if key not in body:
            missing.append(key)
        elif type(value) is not int or value < 1:
            invalid.append(key)
        else:
            identity[key] = value
    result = {"identity": identity, "missing_fields": missing, "invalid_fields": invalid}
    refs = body.get("module_refs")
    if "module_refs" not in body:
        missing.append("module_refs")
    elif not isinstance(refs, dict) or len(refs) > len(MODULE_KEYS) or any(key not in MODULE_KEYS for key in refs):
        invalid.append("module_refs")
    else:
        selected = {key: _exact_ref(value, "module") for key, value in refs.items() if value is not None}
        if any(value is None for value in selected.values()):
            invalid.append("module_refs")
        else:
            result["module_refs"] = selected
    return result


def _preparation_observation(body):
    fields, missing, invalid = {}, [], []
    for key, kind in _PREPARATION_REFS.items():
        if key not in body:
            missing.append(key)
        elif body[key] is None:
            fields[key] = None  # An observed null is different from no field.
        else:
            value = _exact_ref(body[key], kind)
            (fields.update({key: value}) if value is not None else invalid.append(key))
    if "generation_epoch" not in body:
        missing.append("generation_epoch")
    elif type(body["generation_epoch"]) is not int or body["generation_epoch"] < 0:
        invalid.append("generation_epoch")
    else:
        fields["generation_epoch"] = body["generation_epoch"]
    if "script_refs" not in body:
        missing.append("script_refs")
    elif not isinstance(body["script_refs"], list) or len(body["script_refs"]) > 1000:
        invalid.append("script_refs")
    else:
        refs = [_exact_ref(value, "script_revision") for value in body["script_refs"]]
        if any(ref is None for ref in refs):
            invalid.append("script_refs")
        else:
            fields["script_refs"] = refs
    return {"fields": fields, "missing_fields": missing, "invalid_fields": invalid}


def business_observation(query, public_result):
    """Small, bounded metadata from fields actually returned by the reader."""
    body = public_result.get("body") if isinstance(public_result, dict) else None
    if not isinstance(body, dict):
        return {}
    if query == "read_script":
        return {"script_observation": _script_observation(body)}
    if query == "read_preparation":
        return {"preparation_observation": _preparation_observation(body)}
    return {}


def project_business_workset(records, *, max_scripts=8):
    """Project observed refs for optional actions, never live business readiness.

    Caller supplies the current run/branch's read records. A latest observed
    preparation is the selector; older script revisions remain exactly readable
    but are not expanded into this working set. No HTTP, acceptance or approval.
    """
    if type(max_scripts) is not int or not 1 <= max_scripts <= 8:
        raise ValueError("max_scripts must be between 1 and 8")
    result = {"observed_only": True, "currentness_verified": False,
        "acceptance_implied": False, "approval_implied": False,
        "status": "preparation_not_observed", "scripts": []}
    ordered = [record for _, record in sorted(enumerate(records),
        key=lambda item: (item[1].get("last_returned_at") if isinstance(item[1].get("last_returned_at"), str) else "", item[0]))
        if record.get("kind") == BUSINESS_READ_KIND]

    def reader(row, path="/result"):
        ref = _exact_ref(row.get("ref"), "runtime_context")
        return {"ref": ref, "path": path} if ref is not None and row.get("reader_path") == "/result" else None

    def identity(ref):
        return tuple(ref[key] for key in ("kind", "id", "revision", "content_hash"))

    preps = [row for row in ordered if row.get("query") == "read_preparation"
        and _exact_ref(row.get("artifact_ref"), "preparation") and reader(row)]
    if not preps:
        return result
    prep = preps[-1]
    observed = prep.get("preparation_observation")
    result.update(status="preparation_observation_unavailable", preparation={
        "artifact_ref": deepcopy(prep["artifact_ref"]), "reader": reader(prep)})
    if not isinstance(observed, dict) or not isinstance(observed.get("fields"), dict):
        return result  # Legacy receipts still have the unchanged exact reader.
    view = _preparation_observation(observed["fields"])
    view["invalid_fields"] = sorted(set(view["invalid_fields"]) | {
        key for key in observed.get("invalid_fields", []) if key in {*_PREPARATION_REFS, "script_refs", "generation_epoch"}})
    result["preparation"].update(deepcopy(view))
    refs = view["fields"].get("script_refs")
    if refs is None:
        return result
    result.update(status="observed_snapshot", selected_script_count=len(refs),
        expanded_script_count=min(len(refs), max_scripts), omitted_script_count=max(0, len(refs)-max_scripts),
        available_module_index_count=0)
    if len(refs) > max_scripts:
        result["preparation"]["fields"].pop("script_refs")
        result["preparation"]["script_refs_window"] = {"total": len(refs), "items": deepcopy(refs[:max_scripts]),
            "complete": False, "reader": reader(prep, "/result/body/script_refs")}
    for selected in refs[:max_scripts]:
        matching = [row for row in ordered if row.get("query") == "read_script"
            and all(row.get(key) == prep.get(key) for key in ("run_id", "branch_task_id", "scope_hash"))
            and _exact_ref(row.get("artifact_ref"), "script_revision")
            and identity(row["artifact_ref"]) == identity(selected) and reader(row)]
        item = {"artifact_ref": deepcopy(selected), "module_index_status": "script_not_read",
            "business_reader": {"tool": "read_script", "args": {"ref": deepcopy(selected), "sections": ["spoken_text"]}},
            "source_reader": reader(prep, "/result/body/script_refs")}
        if matching:
            item.update(module_index_status="module_index_not_captured", reader=reader(matching[-1]))
            indexes = []
            for row in matching:
                value = row.get("script_observation")
                if not isinstance(value, dict) or not isinstance(value.get("identity"), dict):
                    continue
                body = deepcopy(value["identity"])
                if "module_refs" in value:
                    body["module_refs"] = value["module_refs"]
                validated = _script_observation(body)
                if "module_refs" in validated and not validated["invalid_fields"]:
                    indexes.append((row, validated))
            hashes = {canonical_hash(value) for _, value in indexes}
            if len(hashes) > 1:
                item["module_index_status"] = "ambiguous_observations"
            elif indexes:
                row, value = indexes[-1]
                item.update(module_index_status="observed_exact_refs", reader=reader(row),
                    module_refs_reader=reader(row, "/result/body/module_refs"), **deepcopy(value))
                result["available_module_index_count"] += 1
        result["scripts"].append(item)
    return result


def record_business_read(engine, scope, run, query, normalized_args, result):
    """Record an actually returned, structurally recognizable query result.

    Hashes describe the safe public query result, not a newly certified business
    artifact. Nonempty sections are conservatively a section view even if an
    older backend happens to return the whole record for that request.
    """
    if (query not in _QUERIES or not isinstance(normalized_args, dict)
            or not isinstance(result, dict) or not isinstance(result.get("body"), dict)):
        return None
    normalized_args = canonical_read_args(query, normalized_args)
    # Current business readers omit status and raise on HTTP errors. A legacy
    # HTTP-200 error envelope must nevertheless not become a successful receipt.
    # Do not alter that envelope, raise a new error, or infer an unknown write.
    if result.get("error"):
        return None
    if "status" in result and (not isinstance(result["status"], str)
            or result["status"] not in {"success", "ok", "completed"}):
        return None
    try:
        artifact = ArtifactRef.model_validate(result.get("ref")).model_dump(mode="json")
    except (ValidationError, TypeError):
        return None
    if artifact["kind"] != _QUERIES[query]:
        return None

    public_result = sanitize(result)
    reader_scope = {"account_id": scope.account_id, "role": scope.role, "phase": scope.phase,
        "branch_task_id": scope.branch_task_id, "work_scope": run["scope"]}
    raw_sections = normalized_args.get("sections", [])
    valid_sections = isinstance(raw_sections, list) and all(isinstance(section, str) for section in raw_sections)
    sections = raw_sections if valid_sections else []
    coverage = ("section_view" if sections else "full") if valid_sections else "unknown_view"
    if query == "read_script" and normalized_args.get("sentence_ids"):
        coverage = "section_view"  # A sentence selection never proves full-script coverage.
    metadata = {"query": query, "args_hash": canonical_hash(normalized_args),
        "scope_hash": canonical_hash(reader_scope), "artifact_ref": sanitize(result["ref"]),
        "reader_path": "/result", "coverage": coverage,
        "approval_implied": False,
        "return_summary": {"body_field_count": len(public_result["body"]),
            **({"module_count": len(public_result["modules"])}
                if isinstance(public_result.get("modules"), dict) else {})}}
    metadata.update(business_observation(query, public_result))
    if sections:
        # Query args remain exact in the snapshot; an index must not repeat an
        # unbounded or arbitrarily long section request into every model turn.
        metadata.update(sections_count=len(sections), sections_hash=canonical_hash(sections))
        if len(sections) <= 8 and all(len(section) <= 96 for section in sections):
            metadata["sections"] = sections
    elif not valid_sections:
        # The concrete QueryArgs already rejects this on the ordinary path.
        # Still fail conservatively if an older/custom caller bypasses that
        # normalizer: do not iterate strings, call len(int), or claim full.
        metadata["sections_hash"] = canonical_hash(sanitize(raw_sections))
        if isinstance(raw_sections, list):
            metadata["sections_count"] = len(raw_sections)

    try:
        snapshot = engine._snapshot_context(run, {"kind": BUSINESS_READ_KIND, "query": query,
            "normalized_args": normalized_args, "reader_scope": reader_scope,
            "branch": {"id": scope.branch_task_id}, "result": public_result})
        return engine._record_document_read(scope, BUSINESS_READ_KIND, ref=snapshot,
            owner_run_id=run["id"], role=scope.role, document_hash=canonical_hash(public_result),
            query_read=metadata)
    except Exception:
        # This is local bookkeeping after a read, never an ambiguous business
        # write or provider request. Do not set pending_unknown_operation or
        # expose an exception/SQLite path; an orphan snapshot remains harmless.
        raise ContractError("DOCUMENT_READ_RECORD_UNAVAILABLE", "业务查询已返回，但本机读取凭据暂未保存；没有发起业务写入",
            status=503, details={"query": query, "query_completed": True,
                "business_write_attempted": False, "side_effect_outcome_unknown": False}) from None
