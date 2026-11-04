"""Bounded readers for an already archived job, never a retry or approval.

The caller must supply the snapshot already created under the normal history
ACL. This helper performs no I/O and does not create another snapshot. An
accepted provider output is a read-only sentence view; it is not a formal
Module body and does not replace the journal's exact ModuleRef.
"""
from __future__ import annotations

from copy import deepcopy
import re

from pydantic import ValidationError

from shengbo_contracts.business import MODULE_KEYS, PublicProviderResponseView
from shengbo_contracts.common import ArtifactRef, RecordRef, SCHEMA_VERSION, canonical_hash
from shengbo_contracts.workflow_recovery import WorkflowRecoveryReceipt
from .observability import sanitize


_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}")
_UNKNOWN = {"timeout_unknown", "outcome_unknown"}
_IN_FLIGHT = {"reserved", "pending", "sent", "running"}
_KNOWN_RESULTS = {"result_received", "response_received"}


def _ref(value, kind):
    try:
        model = (ArtifactRef if isinstance(value, dict) and "schema_version" in value else RecordRef).model_validate(value, strict=True)
        if (model.kind != kind or not model.id or len(model.id) > 256
                or not _HASH.fullmatch(model.content_hash)
                or isinstance(model, ArtifactRef) and model.schema_version != SCHEMA_VERSION):
            return None
    except (ValidationError, ValueError, TypeError):
        return None
    return deepcopy(value)


def _identity(ref):
    return tuple(ref[key] for key in ("kind", "id", "revision", "content_hash"))


def _identifier(value):
    return value if isinstance(value, str) and 0 < len(value) <= 256 and sanitize(value) == value else None


def _code(error):
    value = error.get("code") if isinstance(error, dict) else None
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def _module_key(attempt):
    purpose = attempt.get("purpose")
    if not isinstance(purpose, str) or not purpose.startswith("copywriting."):
        return None
    key = purpose[len("copywriting."):]
    return key if key in MODULE_KEYS else None


def _attempt_module_keys(attempt):
    """One paid whole attempt may concern several modules; never clone it."""
    key = _module_key(attempt)
    if key is not None:
        return (key,)
    source = attempt.get("input")
    if (attempt.get("purpose") != "copywriting.whole_script" or not isinstance(source, dict)
            or source.get("generation_mode") != "whole_script"):
        return ()
    keys = source.get("module_keys")
    if (not isinstance(keys, list) or not 1 <= len(keys) <= 6
            or any(not isinstance(key, str) or key not in MODULE_KEYS or key == "transition" for key in keys)
            or len(set(keys)) != len(keys)):
        return ()
    return tuple(keys)


def _whole_attempt(attempt):
    return attempt.get("purpose") == "copywriting.whole_script" and bool(_attempt_module_keys(attempt))


def _reader(ref, path):
    return {"tool": "read_run_history", "ref": deepcopy(ref), "path": path}


def _accepted_sentences(attempt, key, base_path):
    """Return only a proven existing path, never copy or parse raw LLM JSON."""
    output = attempt.get("output")
    if not isinstance(output, dict) or not _HASH.fullmatch(str(attempt.get("output_hash", ""))):
        return None
    if canonical_hash(output) != attempt["output_hash"]:
        return None
    if _whole_attempt(attempt):
        modules = output.get("modules")
        if (output.get("protocol") != "whole-script-v1" or not isinstance(modules, dict)
                or set(modules) != set(_attempt_module_keys(attempt)) or not isinstance(modules.get(key), dict)
                or modules[key].get("module_key") != key):
            return None
        payload = modules[key]
        path = base_path + "/output/modules/" + key
    else:
        if "module_key" in output and output["module_key"] != key:
            return None
        payload = output.get("sentences")
        path = base_path + "/output/sentences"
    if isinstance(payload, dict):
        if payload.get("module_key") != key:
            return None
        if "sentence_details" in payload:
            payload, path = payload["sentence_details"], path + "/sentence_details"
        elif "sentences" in payload:
            payload, path = payload["sentences"], path + "/sentences"
        else:
            return None
    if not isinstance(payload, list) or not payload:
        return None
    if all(isinstance(row, dict) and isinstance(row.get("spoken_text"), str) and row["spoken_text"].strip()
           and isinstance(row.get("display_text", row.get("text")), str)
           and row.get("display_text", row.get("text")).strip() for row in payload):
        shape = "accepted_sentence_details"
    elif all(isinstance(row, str) and row.strip() for row in payload):
        shape = "accepted_spoken_lines"
    else:
        return None
    return {"path": path, "sentence_count": len(payload), "sentence_view_hash": canonical_hash(payload), "format": shape}


def _accepted_output(module_result, attempts, *, key, module_ref, job_id, item_id, item_path):
    module_ref = _ref(module_ref, "module")
    if module_ref is None:
        return None, "journal_module_reference_unverified"
    if (module_result.get("status") != "committed" or module_result.get("job_id") != job_id
            or module_result.get("item_id") != item_id or module_result.get("module_key") != key):
        return None, "journal_association_unverified"
    matches = []
    for position, attempt in attempts:
        whole = _whole_attempt(attempt)
        if whole:
            refs = attempt.get("committed_module_refs")
            keys = _attempt_module_keys(attempt)
            if (module_result.get("protocol") != "whole-script-v1" or not isinstance(refs, dict)
                    or set(refs) != set(keys) or any(_ref(ref, "module") is None for ref in refs.values())
                    or not _HASH.fullmatch(str(module_result.get("dependency_hash", "")))
                    or module_result["dependency_hash"] != attempt.get("input", {}).get("dependency_hash")):
                continue
            committed = _ref(refs.get(key), "module")
        else:
            committed = _ref(attempt.get("committed_module_ref"), "module")
        if (committed is None or _identity(committed) != _identity(module_ref)
                or attempt.get("state") != "result_received" or attempt.get("validation_state") != "accepted"
                or attempt.get("validation_error") or key not in _attempt_module_keys(attempt)):
            continue
        selected = _accepted_sentences(attempt, key, item_path + f"/provider_attempts/{position}")
        if selected is not None:
            matches.append((position, attempt, selected))
    if not matches:
        return None, "accepted_output_association_unverified"
    if len({value[2]["sentence_view_hash"] for value in matches}) != 1:
        return None, "ambiguous_accepted_outputs"
    position, attempt, selected = matches[0]
    return {**selected, "attempt_position": position}, "verified_accepted_output"


def _public_reader(attempt, key, path, snapshot_ref):
    if attempt.get("state") not in _KNOWN_RESULTS or attempt.get("validation_state") == "unknown":
        return None
    try:
        public = PublicProviderResponseView.model_validate(attempt.get("public_response"), strict=True)
    except (ValidationError, ValueError, TypeError):
        return None
    receipt = _ref(attempt.get("provider_response_ref"), "provider_response")
    if _whole_attempt(attempt):
        metadata = attempt.get("generation_response")
        expected = set(_attempt_module_keys(attempt))
        if (not isinstance(metadata, dict) or metadata.get("protocol") != "whole-script-v1"
                or key not in expected or public.module_key is not None or public.expected_module_key is not None
                or metadata.get("is_formal_script") is not False
                or metadata.get("reusable") is not False or metadata.get("approvable") is not False
                or not isinstance(metadata.get("expected_module_keys"), list)
                or not isinstance(metadata.get("response_module_keys"), list)
                or any(not isinstance(value, str) for value in metadata["expected_module_keys"] + metadata["response_module_keys"])
                or len(metadata["expected_module_keys"]) != len(expected)
                or len(metadata["response_module_keys"]) != len(expected)
                or set(metadata["expected_module_keys"]) != expected or set(metadata["response_module_keys"]) != expected):
            return None
        response_scope = "whole_script"
    else:
        if public.module_key != key or public.expected_module_key != key:
            return None
        response_scope = "module"
    if (receipt is None or _identity(public.receipt_ref.model_dump(mode="json")) != _identity(receipt)
            or public.validation_state != attempt.get("validation_state")
            or not isinstance(public.content, str) or not public.content.strip()):
        return None
    return {"reader": _reader(snapshot_ref, path + "/public_response/content"),
            "content_hash": canonical_hash(public.content), "is_formal_module": False,
            "reusable": False, "approvable": False, "response_scope": response_scope}


def _scoped_whole_rejection(attempt, spec, public):
    if not public or public["response_scope"] != "whole_script" or attempt.get("validation_state") != "rejected":
        return None
    if not isinstance(spec, dict) or spec.get("generation_mode") != "whole_script":
        return None
    expected = set(_attempt_module_keys(attempt))
    actions = spec.get("modules")
    if not isinstance(actions, list) or any(not isinstance(action, dict) for action in actions):
        return None
    generated = [action.get("key") for action in actions if action.get("action") == "generate"]
    if len(generated) != len(expected) or any(not isinstance(key, str) or key not in expected for key in generated):
        return None
    if set(generated) != expected or attempt.get("input_hash") != canonical_hash(attempt["input"]):
        return None
    error = attempt.get("validation_error")
    details = error.get("details") if isinstance(error, dict) else None
    if not isinstance(details, dict):
        return None
    try:
        recovery = WorkflowRecoveryReceipt.model_validate(details.get("recovery"), strict=True)
    except ValidationError:
        return None
    if (recovery.workflow != "whole_script" or recovery.step_id != "validate_provider_response"
            or recovery.strategy != "validate_existing_response" or recovery.disposition != "field_repair"
            or recovery.status != "failed" or recovery.selected_by != "program" or recovery.model_requests != 0):
        return None
    affected = set(recovery.affected_units)
    failures, siblings = details.get("module_validation_failures"), details.get("content_validated_module_keys")
    if (not affected or not affected <= expected or len(affected) != len(recovery.affected_units)
            or not isinstance(failures, list) or len(failures) != len(affected)
            or any(not isinstance(row, dict) or not isinstance(row.get("module_key"), str)
                   or not _code(row) or _code(row) not in recovery.issue_codes for row in failures)
            or {row["module_key"] for row in failures} != affected
            or not isinstance(siblings, list) or len(siblings) != len(expected - affected)
            or any(not isinstance(key, str) for key in siblings) or set(siblings) != expected - affected):
        return None
    return {row["module_key"]: row["code"] for row in failures}


def _outcome(attempt):
    state = attempt.get("state")
    if state in _UNKNOWN or attempt.get("validation_state") == "unknown":
        return "outcome_unknown"
    if state in _IN_FLIGHT:
        return "in_flight"
    if state in _KNOWN_RESULTS:
        return "known_result"
    if state in {"failed", "known_failed", "request_failed", "provider_failed"}:
        return "known_failure"
    return "unclassified"


def project_job_recovery_workset(job, *, snapshot_ref, snapshot_body, expected_run_id,
        expected_preparation_id, expected_conversation_id, max_modules=14, only_attention_items=True):
    """Index an existing job snapshot under caller-enforced scope/history ACL.

    All supplied job and snapshot data must come from the existing trusted
    reader. This is a data association check, not a new authorization boundary.
    It never decides readiness, retries, generation, publication or approval.
    """
    if type(max_modules) is not int or not 1 <= max_modules <= 56:
        raise ValueError("max_modules must be between 1 and 56")
    if type(only_attention_items) is not bool:
        raise ValueError("only_attention_items must be a boolean")
    result = {"status": "not_projected", "observed_only": True, "currentness_verified": False,
        "approval_implied": False, "acceptance_implied": False, "modules": []}
    snapshot = _ref(snapshot_ref, "runtime_context")
    if (snapshot is None or snapshot["revision"] != 1 or not isinstance(snapshot_body, dict)
            or canonical_hash(snapshot_body) != snapshot["content_hash"]
            or snapshot["id"] != "context_" + snapshot["content_hash"].split(":")[1]):
        return {**result, "reason_code": "SNAPSHOT_BASIS_UNVERIFIED"}
    if (snapshot_body.get("run_id") != expected_run_id
            or snapshot_body.get("conversation_id") != expected_conversation_id):
        return {**result, "reason_code": "SNAPSHOT_SCOPE_MISMATCH"}
    document = snapshot_body.get("document")
    if (not isinstance(job, dict) or not isinstance(document, dict) or document.get("kind") != "job"
            or document.get("job") != sanitize(job)):
        return {**result, "reason_code": "JOB_SNAPSHOT_MISMATCH"}
    body = document["job"].get("body")
    job_ref = _ref(document["job"].get("ref"), "prelive_job")
    if (not isinstance(body, dict) or job_ref is None or body.get("id") != job_ref["id"]
            or body.get("run_id") != expected_run_id or body.get("preparation_id") != expected_preparation_id):
        return {**result, "reason_code": "JOB_SCOPE_MISMATCH"}
    if body.get("kind") != "scripts":
        return {**result, "reason_code": "NOT_A_SCRIPT_JOB"}
    items = body.get("items")
    if not isinstance(items, list):
        return {**result, "reason_code": "JOB_ITEMS_UNVERIFIED"}

    rows, unindexed_attempt_count, unindexed_item_count = [], 0, 0
    all_unknown_attempt_count, all_in_flight_attempt_count = 0, 0
    unindexed_unknown_attempt_count, unindexed_in_flight_attempt_count = 0, 0
    attention_item_count, successful_item_count = 0, 0
    for item_position, item in enumerate(items):
        if not isinstance(item, dict):
            unindexed_item_count += 1
            continue
        attempts = item.get("provider_attempts")
        attempts = list(enumerate(attempts)) if isinstance(attempts, list) else []
        attempts = [(i, value) for i, value in attempts if isinstance(value, dict)]
        all_unknown_attempt_count += sum(_outcome(value) == "outcome_unknown" for _, value in attempts)
        all_in_flight_attempt_count += sum(_outcome(value) == "in_flight" for _, value in attempts)
        item_id = _identifier(item.get("item_id"))
        unindexed = [(i, value) for i, value in attempts if item_id is None or not _attempt_module_keys(value)]
        unindexed_attempt_count += len(unindexed)
        unindexed_unknown_attempt_count += sum(_outcome(value) == "outcome_unknown" for _, value in unindexed)
        unindexed_in_flight_attempt_count += sum(_outcome(value) == "in_flight" for _, value in unindexed)
        if item_id is None:
            unindexed_item_count += 1
            continue
        item_path = f"/job/body/items/{item_position}"
        known = {key: [(i, value) for i, value in attempts if key in _attempt_module_keys(value)] for key in MODULE_KEYS}
        journal = item.get("module_results")
        journal = journal if isinstance(journal, dict) else {}
        # A completed item with only historical rejected attempts is not a new
        # failure. Unknown/in-flight or uncommitted results still need attention
        # even if an item-level status incorrectly says succeeded.
        def incomplete_result(attempt):
            if _outcome(attempt) != "known_result":
                return False
            keys = _attempt_module_keys(attempt)
            if attempt.get("validation_state") == "pending" or not keys:
                return True
            if any(not isinstance(journal.get(key), dict) or journal[key].get("status") != "committed" for key in keys):
                return True
            if _whole_attempt(attempt) and attempt.get("validation_state") == "accepted":
                position = next(index for index, value in attempts if value is attempt)
                return any(_accepted_output(journal[key], [(position, attempt)], key=key,
                    module_ref=journal[key].get("module_ref"), job_id=body["id"], item_id=item_id,
                    item_path=item_path)[0] is None for key in keys)
            return False
        attention_item = (item.get("status") != "succeeded"
            or item.get("call_state") in _UNKNOWN | _IN_FLIGHT
            or any(_outcome(value) in {"outcome_unknown", "in_flight", "unclassified", "known_failure"}
                   for _, value in attempts)
            or any(incomplete_result(value) for _, value in attempts))
        attention_item_count += int(attention_item)
        successful_item_count += int(not attention_item)
        target, spec = {}, item.get("input") or {}
        if isinstance(spec, dict):
            for field, kind in (("product_ref", "product"), ("base_script_ref", "script_revision")):
                ref = _ref(spec.get(field), kind)
                if ref is not None:
                    target[field] = ref
            variant = _identifier(spec.get("variant_id"))
            if variant is not None:
                target["variant_id"] = variant
            if type(spec.get("version_number")) is int and spec["version_number"] > 0:
                target["version_number"] = spec["version_number"]
        for key in MODULE_KEYS:
            values = known[key]
            module_result = journal.get(key)
            module_result = module_result if isinstance(module_result, dict) else {}
            module_ref = _ref(module_result.get("module_ref"), "module")
            if not values and not module_result:
                continue
            row = {"item_id": item_id, "item_position": item_position, "target": deepcopy(target), "module_key": key,
                "attempt_count": len(values), "module_reader_is_formal_body": False, "_attention_item": attention_item}
            if module_ref is not None:
                row["module_ref"] = module_ref
                row["journal_status"] = module_result.get("status") if module_result.get("status") in {"committed", "pending", "failed"} else "unclassified"
                accepted, reader_status = _accepted_output(module_result, values, key=key, module_ref=module_ref,
                    job_id=body["id"], item_id=item_id, item_path=item_path)
                row["accepted_output_status"] = reader_status
                if accepted is not None:
                    row.update(accepted_output_reader=_reader(snapshot, accepted["path"]),
                        accepted_output_hash=accepted["sentence_view_hash"], sentence_count=accepted["sentence_count"],
                        accepted_output_format=accepted["format"])
            outcomes = [_outcome(value) for _, value in values]
            unknown = [(i, value) for i, value in values if _outcome(value) == "outcome_unknown"]
            sent = [(i, value) for i, value in values if _outcome(value) == "in_flight"]
            rejected = [(i, value) for i, value in values if _outcome(value) == "known_result" and value.get("validation_state") == "rejected"]
            unresolved = [(i, value) for i, value in values if _outcome(value) == "known_result"]
            failures = [(i, value) for i, value in values if _outcome(value) == "known_failure"]
            if unknown:
                status, outcome, selected = "outcome_unknown", "outcome_unknown", unknown[-1]
            elif sent:
                status, outcome, selected = "in_flight", "in_flight", sent[-1]
            elif module_ref is not None and module_result.get("status") == "committed":
                status, outcome, selected = "committed", "known_committed_ref", None
                row["validation_state"] = "accepted" if row.get("accepted_output_reader") else "unverified"
            elif rejected:
                status, outcome, selected = "validation_rejected", "known_result", rejected[-1]
            elif unresolved:
                status, outcome, selected = "uncommitted_result", "known_result", unresolved[-1]
            elif failures:
                status, outcome, selected = "known_failure", "known_failure", failures[-1]
            else:
                status, outcome, selected = "unclassified", "unclassified", values[-1] if values else None
            row.update(status=status, outcome=outcome, unknown_attempt_count=outcomes.count("outcome_unknown"),
                in_flight_attempt_count=outcomes.count("in_flight"))
            if selected is not None:
                position, attempt = selected
                validation = attempt.get("validation_state")
                row["validation_state"] = validation if validation in {"pending", "accepted", "rejected", "unknown"} else "unclassified"
                code = _code(attempt.get("validation_error")) or _code(attempt.get("error")) or _code(item.get("error"))
                public = _public_reader(attempt, key, item_path + f"/provider_attempts/{position}", snapshot)
                scoped_rejection = _scoped_whole_rejection(attempt, spec, public) if status == "validation_rejected" else None
                if scoped_rejection is not None and key not in scoped_rejection:
                    row.update(status="uncommitted_result", validation_state="unclassified")
                elif scoped_rejection is not None:
                    row["error_code"] = scoped_rejection[key]
                elif code:
                    row["error_code"] = code
                if public is not None and outcome == "known_result":
                    row["public_response_reader"] = public["reader"]
                    row["public_response_hash"] = public["content_hash"]
                    row["public_response_is_formal_module"] = False
                    row["public_response_scope"] = public["response_scope"]
            rows.append(row)
    priority = {"outcome_unknown": 0, "validation_rejected": 1, "known_failure": 1, "in_flight": 2,
        "uncommitted_result": 3, "unclassified": 4, "committed": 5}
    rows.sort(key=lambda row: (priority[row["status"]], row["item_position"], MODULE_KEYS.index(row["module_key"])))
    selected_rows = [row for row in rows if row["_attention_item"] or not only_attention_items]
    attention_module_count = sum(row["_attention_item"] for row in rows)
    returned = [{key: value for key, value in row.items() if key != "_attention_item"}
                for row in selected_rows[:max_modules]]
    result.update(status="verified_job_snapshot", job_ref=job_ref,
        modules=returned, total_module_count=len(rows), returned_module_count=len(returned),
        omitted_module_count=len(rows) - len(returned), attention_item_count=attention_item_count,
        successful_item_count=successful_item_count, attention_module_count=attention_module_count,
        only_attention_items=only_attention_items,
        filtered_successful_item_count=successful_item_count if only_attention_items else 0,
        omitted_successful_module_count=len(rows) - len(selected_rows),
        omitted_selected_module_count=max(0, len(selected_rows) - max_modules),
        unindexed_attempt_count=unindexed_attempt_count,
        unindexed_item_count=unindexed_item_count,
        outcome_unknown_count=all_unknown_attempt_count, in_flight_count=all_in_flight_attempt_count,
        unindexed_unknown_attempt_count=unindexed_unknown_attempt_count,
        unindexed_in_flight_attempt_count=unindexed_in_flight_attempt_count,
        full_job_reader=_reader(snapshot, "/job/body/items"),
        note="已提交ModuleRef是保留成果，以业务端复核为准；新计划显式reuse/patch仍须同目标完整base_script_ref，partial项尚无整稿时不得伪造基稿。主Agent可选择原job受控retry，或保持未改输入由后端核验cache补齐，不保证命中。accepted_output_reader只读已接受公开输出的逐句视图，非正式Module正文、非人工批准。未知不重发；没有自动重试或执行。")
    return result
