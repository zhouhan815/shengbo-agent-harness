"""Optional, scoped reads of proven generation evidence; no business writes."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shengbo_contracts.business import MODULE_KEYS, ModuleKey
from shengbo_contracts.common import ContractError
from .recovery_workset import (
    _accepted_output, _attempt_module_keys, _code, _identifier, _identity, _module_key, _outcome,
    _public_reader, _reader, _ref, project_job_recovery_workset,
)


class GenerationOutputReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")
    job_id: str = Field(min_length=1, max_length=256)
    item_id: str = Field(min_length=1, max_length=256)
    module_key: ModuleKey
    view: Literal["accepted_module", "latest_failed_response"] = "accepted_module"
    attempt_id: str | None = Field(default=None, min_length=1, max_length=256)


def _target(item):
    spec = item.get("input") if isinstance(item.get("input"), dict) else {}
    target = {}
    for key, kind in (("product_ref", "product"), ("base_script_ref", "script_revision")):
        ref = _ref(spec.get(key), kind)
        if ref is not None:
            target[key] = ref
    variant = _identifier(spec.get("variant_id"))
    if variant:
        target["variant_id"] = variant
    version = spec.get("version_number")
    if type(version) is int and version > 0:
        target["version_number"] = version
    return target


def _unavailable(result, reason, *, status="UNAVAILABLE"):
    return {**result, "status": status, "reason_code": reason, "content": None,
        "content_complete": False, "content_kind": "metadata_only"}


async def read_generation_output(engine, scope, args):
    """Read one module's archived evidence through existing job/history fences.

    Accepted content is an archived sentence view plus a separately proven
    ModuleRef. It is never a formal Module body, approval or retry permission.
    """
    if (scope.role != "main" or scope.phase != "execute" or scope.branch_task_id):
        raise ContractError("GENERATION_OUTPUT_READ_DENIED", "当前角色或阶段不能读取生成结果", status=403)
    try:
        request = GenerationOutputReadInput.model_validate(args)
    except ValidationError:
        raise ContractError("GENERATION_OUTPUT_READ_INPUT_INVALID", "请提供精确job_id、item_id、module_key及合法view/attempt_id", status=422) from None
    run = await engine.assert_scope(scope)
    # business_query is the existing authoritative owner/scope read route and
    # already invokes _job_view(include_provider_outputs=True). Do not query or
    # project the same job again, nor look up jobs directly in a database.
    view = await engine.business_query(scope, "read_job", {"job_id": request.job_id})
    full_ref = _ref(view.get("full_ref"), "runtime_context") if isinstance(view, dict) else None
    view_job_ref = _ref(view.get("ref"), "prelive_job") if isinstance(view, dict) else None
    base = {"job_id": request.job_id, "item_id": request.item_id, "module_key": request.module_key,
        "requested_view": request.view, "observed_only": True, "currentness_verified": False,
        "approval_implied": False, "automatic_action_taken": False, "is_formal_module_body": False,
        "can_execute_or_approve": False, "committed_module_ref": None}
    if full_ref is None or view_job_ref is None or view_job_ref["id"] != request.job_id:
        return _unavailable(base, "GENERATION_JOB_VIEW_UNVERIFIED", status="UNVERIFIED")
    # This call performs the original account/conversation/branch/ref-hash ACL.
    # The complete value stays internal; only selected evidence is returned.
    history = engine.run_history(scope, {"ref": full_ref, "full": True})
    snapshot = {key: history.get(key) for key in ("conversation_id", "run_id", "document")}
    document = snapshot.get("document")
    job = document.get("job") if isinstance(document, dict) else None
    if not isinstance(job, dict):
        return _unavailable(base, "GENERATION_JOB_SNAPSHOT_UNVERIFIED", status="UNVERIFIED")
    projected = project_job_recovery_workset(job, snapshot_ref=full_ref, snapshot_body=snapshot,
        expected_run_id=run["id"], expected_conversation_id=run["conversation_id"],
        expected_preparation_id=(run.get("scope") or {}).get("preparation_id"),
        max_modules=56, only_attention_items=False)
    job_ref = _ref(job.get("ref"), "prelive_job")
    if (projected.get("status") != "verified_job_snapshot" or job_ref is None
            or _identity(job_ref) != _identity(view_job_ref) or job_ref["id"] != request.job_id):
        return _unavailable(base, projected.get("reason_code", "GENERATION_JOB_SNAPSHOT_UNVERIFIED"), status="UNVERIFIED")
    body = job["body"]
    base.update(job_ref=job_ref, snapshot_ref=full_ref, job_status=body.get("status"),
        job_outcome_unknown_count=projected["outcome_unknown_count"], job_in_flight_count=projected["in_flight_count"])
    # Workset validation covers the complete immutable snapshot. Select from
    # that verified snapshot, not its bounded default first-items projection.
    matches = [(index, item) for index, item in enumerate(body["items"])
        if isinstance(item, dict) and item.get("item_id") == request.item_id]
    if len(matches) != 1:
        return _unavailable(base, "GENERATION_ITEM_NOT_FOUND" if not matches else "GENERATION_ITEM_AMBIGUOUS")
    item_position, item = matches[0]
    attempts = [(index, value) for index, value in enumerate(item.get("provider_attempts") or [])
        if isinstance(value, dict) and request.module_key in _attempt_module_keys(value)]
    journal = item.get("module_results") if isinstance(item.get("module_results"), dict) else {}
    module_result = journal.get(request.module_key) if isinstance(journal.get(request.module_key), dict) else {}
    module_ref = _ref(module_result.get("module_ref"), "module")
    base.update(item_status=item.get("status"), target=_target(item),
        available_modules=sorted({key for key in journal if key in MODULE_KEYS}
            | {key for a in item.get("provider_attempts", []) if isinstance(a, dict) for key in _attempt_module_keys(a)}),
        attempt_count=len(attempts), outcome_unknown_count=sum(_outcome(a) == "outcome_unknown" for _, a in attempts),
        in_flight_count=sum(_outcome(a) == "in_flight" for _, a in attempts),
        observed_committed_module_ref=module_ref)
    if attempts:
        last = attempts[-1][1]
        rejection = (_code(last.get("validation_error"))
            if last.get("validation_state") == "rejected" and last.get("state") in {"result_received", "response_received"} else None)
        observed_state = last.get("state")
        allowed_states = {"reserved", "pending", "sent", "running", "result_received", "response_received",
            "timeout_unknown", "outcome_unknown", "known_failed", "failed", "request_failed", "provider_failed"}
        base.update(observed_latest_attempt_id=_identifier(last.get("attempt_id")),
            observed_latest_attempt_state=observed_state if observed_state in allowed_states else "unclassified",
            observed_error_code=rejection or _code(last.get("error")) or _code(item.get("error")))
    if base["outcome_unknown_count"]:
        return _unavailable(base, "GENERATION_OUTCOME_UNKNOWN", status="OUTCOME_UNKNOWN")
    if base["in_flight_count"]:
        return _unavailable(base, "GENERATION_NOT_READY", status="NOT_READY")
    explicit = [(index, a) for index, a in attempts if a.get("attempt_id") == request.attempt_id] if request.attempt_id else None
    if explicit is not None and len(explicit) != 1:
        return _unavailable(base, "GENERATION_ATTEMPT_NOT_FOUND" if not explicit else "GENERATION_ATTEMPT_AMBIGUOUS")
    item_path = f"/job/body/items/{item_position}"
    if request.view == "accepted_module":
        if module_ref is None:
            return _unavailable(base, "GENERATION_COMMITTED_MODULE_UNAVAILABLE")
        proved, reason = _accepted_output(module_result, attempts, key=request.module_key, module_ref=module_ref,
            job_id=request.job_id, item_id=request.item_id, item_path=item_path)
        if proved is None:
            return _unavailable(base, reason)
        if explicit is not None:
            proved, reason = _accepted_output(module_result, explicit, key=request.module_key, module_ref=module_ref,
                job_id=request.job_id, item_id=request.item_id, item_path=item_path)
            if proved is None:
                return _unavailable(base, reason)
        position = proved["attempt_position"]
        attempt = item["provider_attempts"][position]
        reader = _reader(full_ref, proved["path"])
        expected_hash = proved["sentence_view_hash"]
        content_kind = "archived_accepted_sentence_view"
        base["committed_module_ref"] = module_ref
        note = "归档输出逐句视图，committed_module_ref另行保留；不是正式Module正文或人工批准。显式reuse仍需合法完整基稿与原业务校验。"
    else:
        eligible = [(index, a) for index, a in (explicit if explicit is not None else attempts)
            if a.get("validation_state") == "rejected" and a.get("state") in {"result_received", "response_received"}]
        if not eligible:
            return _unavailable(base, "GENERATION_FAILED_RESPONSE_UNAVAILABLE")
        position, attempt = eligible[-1]
        proved = _public_reader(attempt, request.module_key, item_path + f"/provider_attempts/{position}", full_ref)
        if proved is None:
            return _unavailable(base, "GENERATION_PUBLIC_RESPONSE_UNVERIFIED")
        reader, expected_hash = proved["reader"], proved["content_hash"]
        if proved.get("response_scope") == "whole_script":
            content_kind = "archived_public_rejected_whole_response"
            base["response_scope"] = "whole_script"
            base["requested_module_keys"] = list(_attempt_module_keys(attempt))
            note = "整包公开失败原文，包含本次请求的全部模块；未从无效JSON猜测抽取单模块。不是正式稿、ModuleRef或批准，不自动重试或修改。"
        else:
            content_kind = "archived_public_rejected_response"
            note = "仅公开失败原文证据，不是正式ModuleRef、可复用模块或已批准话术；不自动重试或修改。"
    selected = engine.run_history(scope, {key: value for key, value in reader.items() if key != "tool"}
        | {"max_chars": 12000})
    selection = selected.get("selection") or {}
    if selection.get("content_hash") != expected_hash:
        raise ContractError("GENERATION_OUTPUT_HASH_MISMATCH", "生成证据选中内容哈希不一致", status=409)
    complete = (selection.get("next_cursor") is None and not selection.get("indexed_count")
        and selection.get("mode") != "index" and selection.get("cursor") == 0)
    return {**base, "status": "AVAILABLE", "content": selected["document"], "content_kind": content_kind,
        "content_complete": complete, "content_hash": expected_hash, "reader": reader,
        "selection": {key: selection[key] for key in ("mode", "units", "cursor", "next_cursor", "total", "paginated", "indexed_count") if key in selection},
        "attempt_id": _identifier(attempt.get("attempt_id")), "attempt_state": attempt.get("state"),
        "validation_state": attempt.get("validation_state"),
        "usage_state": attempt.get("usage_state") if attempt.get("usage_state") in {"known", "partial", "pending", "unknown"} else None,
        "provider_response_ref": _ref(attempt.get("provider_response_ref"), "provider_response"), "note": note}
