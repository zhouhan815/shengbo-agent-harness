"""Lossless-index choices for the main Agent's current branch working set.

This changes model-visible annotation status, never reports or business state.
An explicit main review classifies an annotation; it does not prove a business
blocker resolved, turn unknown facts into known ones, approve text, or grant an
action. Every original report and review remains readable by its exact ref.
"""
from copy import deepcopy

from pydantic import ValidationError

from shengbo_contracts.common import ArtifactRef, RecordRef, canonical_hash
from .models import BindingConflictReview
from .observability import sanitize


_DUPLICATE_FIELDS = ("financial_values", "conflicts", "missing_fields", "unresolved_fields", "unverified_hypothesis_count")

_HISTORY_STATE_COUNTS = ("unknown_item_count", "unknown_provider_attempt_count", "in_flight_provider_attempt_count")


def _history_snapshot_ref(value):
    """Only an already-created exact history reader can replace old detail."""
    try:
        ref = ArtifactRef.model_validate(value, strict=True)
    except (ValidationError, TypeError, ValueError):
        return False
    digest = ref.content_hash.removeprefix("sha256:")
    return (ref.kind == "runtime_context" and ref.content_hash.startswith("sha256:") and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest)
            and ref.id == "context_" + digest)


def _exact_business_ref(value, kind=None):
    try:
        model = (ArtifactRef if isinstance(value, dict) and "schema_version" in value else RecordRef).model_validate(value, strict=True)
    except (ValidationError, TypeError, ValueError):
        return False
    digest = model.content_hash.removeprefix("sha256:")
    return (kind in (None, model.kind) and model.content_hash.startswith("sha256:") and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest))


def _historical_preparation_index(observation, metrics):
    """Index only known old body copies behind their existing observation."""
    preparation, full_ref = observation.get("preparation"), observation.get("full_ref")
    if (not isinstance(preparation, dict) or preparation.get("projection_kind") == "historical_preparation_index"
            or not _history_snapshot_ref(full_ref) or full_ref.get("revision") != 1):
        return preparation
    # A foreign/legacy projection must keep its existing fields. No unknown
    # field, future fence, single approval ref or current business state is
    # selected by a suffix/pattern or replaced by a generic summary.
    reserved = {"projection_kind", "history_index", "currentness_verified", "full_ref", "reader_path", "body_details_projection", "note"}
    body, reference = preparation.get("body"), preparation.get("ref")
    if (reserved.intersection(preparation) or not isinstance(body, dict)
            or not _exact_business_ref(reference, "preparation")
            or body.get("id") != reference["id"] or not isinstance(body.get("status"), str)
            or type(body.get("generation_epoch")) is not int or body["generation_epoch"] < 0
            or canonical_hash(body) != reference["content_hash"]):
        return preparation
    fields, counts = [], {}
    for name in ("analysis_refs", "script_refs", "selected_product_refs", "source_refs", "recap_refs"):
        value = body.get(name)
        if isinstance(value, list) and value and all(_exact_business_ref(ref) for ref in value):
            fields.append(name)
            counts[name] = len(value)
    # The historical preparation's goal is free text. A future structured goal
    # (possibly with a new action fence) stays entirely visible by default.
    if isinstance(body.get("goal"), str) and body["goal"]:
        fields.append("goal")
    if not fields:
        return preparation
    result = deepcopy(preparation)
    for name in fields:
        result["body"].pop(name)
    result.update(projection_kind="historical_preparation_index", history_index=True,
        currentness_verified=False, full_ref=full_ref, reader_path="/observation/preparation",
        body_details_projection={"fields": sorted(fields), "reference_counts": counts,
            "path": "/observation/preparation/body"},
        note="历史本场快照。省略字段按full_ref与body_details_projection.path回读；不代表当前状态、批准或授权。")
    from shengbo_contracts.common import canonical_json
    if len(canonical_json(result)) >= len(canonical_json(preparation)):
        return preparation
    metrics["indexed_historical_preparations"] += 1
    metrics["indexed_historical_preparation_fields"] += len(fields)
    return result


def _answered_preview_index(decision, metrics):
    """Only answered preview ref anchors move behind the original /preview."""
    if not isinstance(decision, dict) or decision.get("status") != "ANSWERED":
        return decision
    preview = decision.get("preview")
    if (not isinstance(preview, dict) or "exact_refs_index" in preview
            or not _history_snapshot_ref(preview.get("full_ref")) or preview["full_ref"].get("revision") != 1):
        return decision
    refs = preview.get("exact_refs")
    if not isinstance(refs, list) or not refs or not all(_exact_business_ref(ref) for ref in refs):
        return decision
    result = deepcopy(decision)
    result["preview"].pop("exact_refs")
    # exact_refs is a computed anchor list, not a field in the raw snapshot.
    # Read /preview, never the nonexistent /preview/exact_refs path. Finance,
    # actual approval/playlist bindings and verified receipts remain inline.
    result["preview"]["exact_refs_index"] = {"count": len(refs), "path": "/preview",
        "currentness_verified": False,
        "note": "精确引用锚点来自原始预览；按本preview.full_ref回读/preview，不表示当前批准。"}
    from shengbo_contracts.common import canonical_json
    if len(canonical_json(result)) >= len(canonical_json(decision)):
        return decision
    metrics["indexed_answered_previews"] += 1
    metrics["indexed_preview_exact_refs"] += len(refs)
    return result


def _historical_job_index(job, metrics):
    """Index *historical snapshot* ledgers, never resolve a pending outcome.

    project_context already owns immutable snapshots and their ACL. This view
    merely stops replaying the same old provider ledger in every M3 turn. It
    never changes the snapshot, report, ToolMessage, status or business ref.
    Historical sent/unknown states and their counts remain exact; the index
    does not promote an old snapshot to current status. Pending current-run
    operations and model attempts are not eligible for this transformation.
    """
    if not isinstance(job, dict) or job.get("projection_kind") == "historical_job_index":
        return job
    body, counts, full_ref = job.get("body"), job.get("items_projection"), job.get("full_ref")
    if (not isinstance(body, dict) or not isinstance(body.get("status"), str)
            or not isinstance(counts, dict) or not _history_snapshot_ref(full_ref)
            or any(type(counts.get(key)) is not int or counts[key] < 0 for key in _HISTORY_STATE_COUNTS)):
        return job
    if not isinstance(body.get("items", []), list) or not isinstance(job.get("attention_items", []), list):
        return job
    items = [*body.get("items", []), *job.get("attention_items", [])]
    if any(not isinstance(item, dict) for item in items):
        return job

    def parent_reader_ref(value):
        # The pre-existing full_ref/path reader contract already permits a
        # parent full_ref. Keep that exact ref once, rather than copying its
        # 200+ characters into every provider/item/count object in one job.
        if isinstance(value, dict):
            return {key: parent_reader_ref(item) for key, item in value.items()
                    if not (key in {"full_ref", "ref"} and item == full_ref)}
        if isinstance(value, list):
            return [parent_reader_ref(item) for item in value]
        return deepcopy(value)

    def index_item(item):
        path, item_ref = item.get("path"), item.get("full_ref")
        attempts = item.get("provider_attempts")
        projected = item.get("provider_attempts_projection")
        if (not isinstance(path, str) or not path.startswith("/job/body/items/")
                or not path.removeprefix("/job/body/items/").isdecimal()
                or item_ref != full_ref or not isinstance(attempts, list)
                or any(not isinstance(attempt, dict) for attempt in attempts)
                or not isinstance(projected, dict) or projected.get("full_ref") != full_ref
                or projected.get("path") != path + "/provider_attempts"
                or type(projected.get("unknown_count")) is not int or projected["unknown_count"] < 0):
            return None
        # _job_view preserves original item positions, including attention rows.
        # A reader points to the raw immutable provider array, not a re-ordered
        # view; IDs and state remain visible for a deliberate recovery decision.
        result = {key: parent_reader_ref(value) for key, value in item.items()
                  if key not in {"provider_attempts", "full_ref"}}
        # Remove only these known bulky detail fields. Future status, IDs,
        # hashes, reconciliation flags and action fences remain visible by
        # default; a new provider field must not silently widen this index.
        details = {"generation_response", "usage", "validation_error"}
        result["provider_attempts"] = [parent_reader_ref({
            key: value for key, value in attempt.items() if key not in details})
            for attempt in attempts]
        for row, attempt in zip(result["provider_attempts"], attempts):
            if "validation_error" in attempt:
                row["validation_error_present"] = bool(attempt["validation_error"])
        result["provider_details_omitted"] = True
        result["provider_detail_fields"] = sorted(details.intersection(
            key for attempt in attempts for key in attempt))
        result["provider_details_path"] = path + "/provider_attempts"
        metrics["indexed_provider_attempts"] += len(attempts)
        return result

    before_attempts = metrics["indexed_provider_attempts"]
    result = deepcopy(job)
    result["body"]["items"] = [index_item(item) for item in body.get("items", [])]
    result["attention_items"] = [index_item(item) for item in job.get("attention_items", [])]
    if any(item is None for item in [*result["body"]["items"], *result["attention_items"]]):
        metrics["indexed_provider_attempts"] = before_attempts
        return job
    if result == job:
        return job
    for key in ("items_projection", "attention_projection"):
        if key in result:
            result[key] = parent_reader_ref(result[key])
    result.update(projection_kind="historical_job_index", history_index=True,
        currentness_verified=False, reader_path="/job",
        note="这是历史快照，不代表任务当前状态。每个原始path配合本任务顶层full_ref用read_run_history读取；供应商详情按provider_details_path回读。未知和sent状态及计数原样保留，不表示失败、可重试、已批准或新增授权。")
    from shengbo_contracts.common import canonical_json
    if len(canonical_json(result)) >= len(canonical_json(job)):
        metrics["indexed_provider_attempts"] = before_attempts
        return job
    metrics["indexed_historical_jobs"] += 1
    return result


def project_context_working_set(projection, *, metrics=None):
    """Small M3 transport view, shared by native budget precheck and delivery.

    Only known inherited acceptance body copies, observed historical jobs and
    answered preview anchors with existing readers are eligible. Current Goal,
    source/report gaps, pending decisions, operations and model attempts stay
    verbatim. Historical unknown states/counts remain
    exact while provider detail stays in its immutable reader. No cross-turn
    cache or new reference protocol is introduced; every detail uses the
    existing same-scope read_run_history contract.
    """
    from shengbo_contracts.common import canonical_json
    stats = {"indexed_historical_jobs": 0, "indexed_provider_attempts": 0,
             "indexed_historical_preparations": 0, "indexed_historical_preparation_fields": 0,
             "indexed_answered_previews": 0, "indexed_preview_exact_refs": 0,
             "source_characters": len(canonical_json(projection))}
    result = deepcopy(projection)
    if isinstance(result, dict):
        inherited = result.get("inherited_context")
        acceptance = inherited.get("prior_acceptance") if isinstance(inherited, dict) else None
        observation = acceptance.get("observation") if isinstance(acceptance, dict) else None
        if isinstance(observation, dict) and isinstance(observation.get("jobs"), list):
            observation["jobs"] = [_historical_job_index(job, stats) for job in observation["jobs"]]
        if isinstance(observation, dict) and isinstance(observation.get("preparation"), dict):
            observation["preparation"] = _historical_preparation_index(observation, stats)
        if isinstance(result.get("decisions"), list):
            result["decisions"] = [_answered_preview_index(decision, stats) for decision in result["decisions"]]
        unknown = result.get("unknown_side_effects")
        related = unknown.get("related_jobs") if isinstance(unknown, dict) else None
        if isinstance(related, dict) and isinstance(related.get("items"), list):
            related["items"] = [_historical_job_index(job, stats) for job in related["items"]]
    stats["projected_characters"] = len(canonical_json(result))
    stats["removed_characters"] = stats["source_characters"] - stats["projected_characters"]
    if metrics is not None:
        metrics.update(stats)
    return result


def _verified_binding(item, items_by_id, records, run_id, conversation_id):
    """Fail closed to the original view on any incomplete/stale lineage."""
    record = records.get(item.get("branch_task_id"))
    if not record or record.get("run_id") != run_id or record.get("status") != "ACCEPTED":
        return None
    basis = record.get("binding_basis") or {}
    if not isinstance(basis, dict) or basis.get("method") != "same_source_single_new_product":
        return None
    original_id = basis.get("original_branch_id")
    original, original_item = records.get(original_id), items_by_id.get(original_id)
    if (not original or not original_item or original_id == item.get("branch_task_id")
            or original.get("run_id") != run_id or original.get("status") != "ACCEPTED"
            or item.get("status") != original_item.get("status") or item.get("status") != "ACCEPTED"
            or item.get("role") not in {"product_facts", "audience_scenes"}
            or item.get("role") != original_item.get("role") or item.get("role") != record.get("role")
            or original.get("role") != record.get("role")):
        return None
    source, bound = original.get("report"), record.get("report")
    if not isinstance(source, dict) or not isinstance(bound, dict):
        return None
    if (basis.get("original_report") != source or basis.get("original_report_hash") != canonical_hash(source)
            or original.get("report_hash") != canonical_hash(source) or original_item.get("report_hash") != original["report_hash"]
            or record.get("report_hash") != canonical_hash(bound) or item.get("report_hash") != record["report_hash"]
            or item.get("accepted_ref") != record.get("accepted_ref")
            or original_item.get("accepted_ref") != original.get("accepted_ref")
            or any(item.get(key) != original_item.get(key) for key in _DUPLICATE_FIELDS)):
        return None
    try:
        for projected, raw_record in ((item, record), (original_item, original)):
            report_ref = ArtifactRef.model_validate(projected["report_ref"])
            accepted_ref = ArtifactRef.model_validate(projected["accepted_ref"])
            if report_ref.kind != "runtime_context" or report_ref.revision != 1 or accepted_ref.kind != "analysis":
                return None
            if projected.get("report_path") != "/branch/report":
                return None
            expected_snapshot = sanitize({"conversation_id": conversation_id, "run_id": run_id,
                "document": {"kind": "branch", "branch": raw_record}})
            snapshot_hash = canonical_hash(expected_snapshot)
            if report_ref.content_hash != snapshot_hash or report_ref.id != "context_" + snapshot_hash.split(":", 1)[1]:
                return None
        product = RecordRef.model_validate(basis["product_ref"])
        if product.kind != "product":
            return None
    except (ValidationError, KeyError, TypeError):
        return None
    expected = deepcopy(source)
    expected["product_ids"] = [product.id]
    expected["dependencies"] = [*source.get("dependencies", []), basis["product_ref"]]
    review = basis.get("conflict_review")
    issues = source.get("conflicts", [])
    dispositions = []
    if issues:
        try:
            normalized = BindingConflictReview.model_validate(review).model_dump(mode="json")
        except (ValidationError, TypeError):
            return None
        if (normalized != review or review["branch_task_id"] != original_id
                or review["report_hash"] != original["report_hash"] or canonical_hash(review) != basis.get("review_hash")):
            return None
        dispositions = sorted(review["issue_dispositions"], key=lambda d: d["issue_index"])
        if [d["issue_index"] for d in dispositions] != list(range(len(issues))):
            return None
        if any(d["classification"] != "nonblocking_boundary_note" or d["issue_hash"] != canonical_hash(issues[d["issue_index"]])
               for d in dispositions):
            return None
        expected["evidence"] = [*source.get("evidence", []), {"kind": "main_annotation_review",
            "original_branch_id": original_id, "original_report_hash": original["report_hash"],
            "product_ref": basis["product_ref"], "conflict_review": review, "review_hash": basis["review_hash"],
            "semantic_decision_by": "main_agent", "fact_changes": False}]
    elif review is not None or basis.get("review_hash") is not None:
        return None
    if expected != bound:
        return None  # A material fact, unknown, note or evidence changed.
    return {"original_id": original_id, "review": review, "dispositions": dispositions,
            "original_report_hash": original["report_hash"], "review_hash": basis.get("review_hash")}


def project_branch_working_set(items, records, *, run_id, conversation_id):
    """Index only verified pure bindings; keep every other branch unchanged."""
    result = deepcopy(items)
    by_id = {item["branch_task_id"]: item for item in items}
    raw = {record["id"]: record for record in records}
    # Multiple distinct accepted bindings can be compared. An original only
    # aliases a single unambiguous successor, not whichever came last.
    verified = []
    for position, item in enumerate(items):
        try:
            proof = _verified_binding(item, by_id, raw, run_id, conversation_id)
        except (ValidationError, KeyError, TypeError, ValueError):
            proof = None  # A legacy/malformed record must keep its visible notes.
        if proof is not None:
            verified.append((position, proof))
    successors = {}
    for position, proof in verified:
        successors.setdefault(proof["original_id"], []).append(position)
    output_by_id = {item["branch_task_id"]: item for item in result}
    for position, proof in verified:
        item = result[position]
        if proof["dispositions"]:
            # Annotation review != formal business-blocker resolution. Keep
            # financial anchors/missing/unresolved fields verbatim; only the
            # exact reviewed annotation bodies move behind the existing reader.
            item.pop("conflicts", None)
            item["resolved_annotations"] = {
                "classification": "nonblocking_boundary_note", "semantic_decision_by": "main_agent",
                "original_report_hash": proof["original_report_hash"], "review_hash": proof["review_hash"],
                "issue_indices": [d["issue_index"] for d in proof["dispositions"]],
                "issue_hashes": [d["issue_hash"] for d in proof["dispositions"]],
                "original_reader": {"ref": item["report_ref"], "path": "/branch/binding_basis/original_report/conflicts"},
                "review_reader": {"ref": item["report_ref"], "path": "/branch/binding_basis/conflict_review"},
                "effect": "annotation_classification_only_not_fact_or_business_approval",
            }
        if len(successors[proof["original_id"]]) == 1:
            original = output_by_id[proof["original_id"]]
            for key in _DUPLICATE_FIELDS:
                original.pop(key, None)
            original.update(alias_of_branch_task_id=item["branch_task_id"], history_index=True)
    return result
