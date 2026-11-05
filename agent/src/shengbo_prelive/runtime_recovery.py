"""Durable recovery of the original run, with a bounded main-Agent handoff."""
from __future__ import annotations

import json
import re
from time import time

from shengbo_contracts.common import ArtifactRef, ContractError, canonical_hash, now_iso
from .models import RunScope
from .observability import sanitize
from .store import TERMINAL

KIND = "runtime_recovery"
_FENCED = TERMINAL | {"FINALIZING", "CANCELLING"}
_MAX_PLANS = 2
_TRANSIENT = {"VOICE_UNAVAILABLE", "OBSERVABILITY_UNAVAILABLE", "TimeoutError", "MODEL_TIMEOUT",
              "model_transport_failed", "model_provider_rate_limited", "model_provider_server_error"}
_SOURCE = {"CATALOG_SOURCE_LAYOUT_UNSUPPORTED", "CATALOG_RECOVERY_SOURCE_CHANGED",
           "CATALOG_EVIDENCE_UNCONFIRMED", "MATERIAL_SOURCE_CHANGED", "MATERIAL_SECTION_NOT_FOUND",
           "MATERIAL_STORAGE_INVALID", "MATERIAL_BLOB_INTEGRITY_FAILED"}
_RESTORE_ORIGINAL = {"MATERIAL_STORAGE_INVALID", "MATERIAL_BLOB_INTEGRITY_FAILED", "FileNotFoundError"}
_TEXT = {
    "CATALOG_RECOVERY_SOURCE_CHANGED": "重新核对时，系统未能核实保存的资料读取记录与当前读取规则一致。已保留原附件。",
    "MATERIAL_STORAGE_INVALID": "暂时读不到本次上传的原件，可以重新上传同一份文件继续核对。",
    "MATERIAL_BLOB_INTEGRITY_FAILED": "本机保存的原件未通过完整性检查，可以重新上传同一份文件。",
    "FileNotFoundError": "当前步骤找不到需要读取的本地文件。",
    "CATALOG_FACTS_INVALID": "提取结果里没有可用的商品信息，已保留原件。",
    "CATALOG_COVERAGE_INCOMPLETE": "部分原文没有对应到已核实的商品字段。",
    "CATALOG_RECOVERY_UNRESOLVED": "仍有商品信息需要核对。",
    "CATALOG_EXTRACTION_INTERRUPTED": "上一笔资料提取未取得可继续使用的结果。已保留原件和当前进度。",
    "CATALOG_EXTRACTION_TIMEOUT": "读取或提取商品资料超时，已保留原件和当前进度。",
    "CATALOG_SOURCE_LAYOUT_UNSUPPORTED": "系统未能从这份资料读取可核对的商品结构。",
    "model_provider_balance_insufficient": "模型服务余额不足。充值后可以继续这次任务。",
    "BUDGET_EXHAUSTED": "本轮自动核对已达到请求上限，当前任务已保留。",
    "PLAN_CAPABILITY_DENIED": "执行计划里的操作不属于本次授权范围，需要调整计划。",
    "GOAL_SCOPE_CONFLICT": "任务需要的操作与本次授权不一致，需要核对范围。",
    "INVALID_EFFECT_ENUM": "系统无法识别执行计划里的一项操作，需要重新核对任务步骤。",
    "model_transport_failed": "模型服务连接中断，当前步骤尚未得到可验收的回复。",
    "FINISH_MISSING": "这一轮执行没有交回可核对的任务结果。",
    "INTENT_MISSING": "这一轮未能确定可以执行的任务范围。",
    "FINISH_BLOCKED": "主 Agent 表示当前步骤无法继续，需要补充信息或选择另一种处理方式。",
    "REMOTE_OUTCOME_UNKNOWN": "业务服务未返回确认回执，正在核对原操作有没有实际保存。",
    "REVISION_CONFLICT": "保存前发现目标内容已更新，需要先核对当前版本。",
    "CONTEXT_LIMIT": "现有资料超出本轮可处理范围，需要补充具体位置或缩小当前核对范围。",
    "VOICE_UNAVAILABLE": "声波业务服务暂时不可用。",
    "OBSERVABILITY_UNAVAILABLE": "本地执行记录暂时无法可靠保存。",
}
_FIELD_NAMES = {"sku_id": "商品规格编号", "name": "商品名称", "price": "日常售价",
                "currency": "币种", "faq": "常见问答", "return_policy": "售后政策",
                "knowledge_documents": "知识文档", "core_selling_points": "核心卖点"}


def _step(run, stage):
    if stage == "job_observation":
        return "核对后台作业进度"
    if stage == "authorize" or run.get("phase") == "intent":
        return "核对任务范围与授权"
    if run.get("catalog_intake_enabled"):
        return "商品资料导入"
    if run.get("selling_points_fast_enabled"):
        return "生成并保存核心卖点"
    objective = (run.get("goal") or {}).get("objective")
    return {"session_config": "保存本场配置", "script_change": "处理直播话术",
            "materials_to_script": "准备直播话术", "audio_prepare": "准备语音",
            "prepare_session": "准备本场直播"}.get(objective, "核对当前任务步骤")


def _details(error):
    raw = getattr(error, "details", None)
    if not isinstance(raw, dict):
        return {}
    field_values, issue_values = raw.get("fields"), raw.get("issues")
    fields = [item for item in field_values[:8] if isinstance(item, str)] if isinstance(field_values, list) else []
    issue_keys = ("unit", "reason", "field", "sku_id", "location")
    issues = [{key: item[key] for key in issue_keys if key in item}
              for item in issue_values[:8] if isinstance(item, dict)] if isinstance(issue_values, list) else []
    values = {"fields": fields, "issues": issues}
    for key in ("location", "candidate_index", "commit_state"):
        value = raw.get(key)
        if isinstance(value, (str, int)):
            values[key] = value
    return sanitize(values)


def record(engine, run, error, *, stage="execute"):
    """Own the error before the scheduler sees it again. Never revoke the run."""
    identity = run["id"]
    code = str(getattr(error, "code", type(error).__name__))
    if not re.fullmatch(r"[\w.:-]{1,100}", code):
        code = "RUN_STEP_ERROR"
    details = _details(error)
    reason = _TEXT.get(code)
    if not reason:
        if isinstance(error, ContractError) and (code.startswith(("DOCUMENT_", "SELLING_POINTS_", "FIXED_STEP_", "MATERIAL_"))
                or code in {"REVISION_CONFLICT", "PREPARATION_PRODUCTS_NOT_SELECTED"}):
            reason = str(sanitize(error.message))[:320]
        else:
            reason = "当前步骤遇到执行错误，已保留原任务与可核对的成果。"
    with engine.store.tx() as db:
        row = db.execute("SELECT body FROM objects WHERE kind='run' AND id=?", (identity,)).fetchone()
        current = json.loads(row[0])
        if current["status"] in _FENCED or current.get("dispatch_frozen"):
            return
        previous = current.get("recovery_pending") or {}
        latest = current.get("runtime_recovery_last") or {}
        number = max(previous.get("round", 0), latest.get("round", 0)) + 1
        consecutive = previous.get("state") == "resuming"
        same_failure = (consecutive and previous.get("error_code") == code
                        and previous.get("stage") == stage and previous.get("details") == details)
        incident = {"id": canonical_hash([identity, number, stage, code])[7:39],
            "state": "planning", "stage": stage, "step": _step(current, stage), "error_code": code,
            "message": reason, "details": details, "round": number,
            "automatic_plans": previous.get("automatic_plans", 0) if consecutive else 0,
            "acknowledged_model_attempt_ids": previous.get("acknowledged_model_attempt_ids", []),
            "same_failure": same_failure, "outcome_unknown": bool(getattr(error, "outcome_unknown", False)),
            "created_at": now_iso()}
        if code == "FINISH_BLOCKED" and isinstance(error, ContractError):
            reason = incident["message"] = str(sanitize(error.message))[:320] or reason
        if stage == "reconcile" and current.get("pending_unknown_operation"):
            current["reconciliation_after"] = time() + 5
        engine.store._put(db, "run", {**current, "status": "RECOVERING", "recovery_pending": incident,
            "error": {"code": code, "message": reason}, "summary": reason, "finish": None})
    engine.trace.try_emit(identity, "runtime.recovery.planning", {"title": "执行遇到问题，任务已保留",
        "status": "RECOVERING", "step": incident["step"], "error_code": code, "summary": reason,
        "unresolved_checks": details.get("issues", [])})


def _open_question(engine, run, incident):
    identity = "decision_" + incident["id"]
    details = incident.get("details", {})
    fields = "、".join(dict.fromkeys(_FIELD_NAMES.get(key, "其他资料") for key in details.get("fields", [])))
    exact = ("需核对的内容为" + fields + "。" if fields else "")
    if incident["error_code"] in _SOURCE or fields:
        hint = "请指出原文所在页、表格或段落，或说明这份资料如何核对。原附件仍保留。"
        retry = "按原资料继续核对"
    elif incident["error_code"] == "model_provider_balance_insufficient":
        hint = "充值或确认模型服务恢复后，点击继续。"
        retry = "已充值，继续处理"
    else:
        hint = "可以让 Agent 从当前步骤重新核对，也可以补充你已确认的信息。"
        retry = "从当前步骤继续"
    can_restore = bool(run.get("input", {}).get("material_refs")) and incident["error_code"] in _RESTORE_ORIGINAL
    if can_restore:
        hint = "可以重新上传同一份原件。系统会核对它与本次锁定的文件完全一致。"
    if incident.get("outcome_unknown"):
        hint = "上一笔模型请求结果还无法核实。服务恢复后可以确认重新请求，可能产生一次额外模型费用。"
        retry = "确认重新发起模型请求"
    prompt = f"卡在「{incident['step']}」。{incident['message']}{exact}\n{hint}"
    with engine.store.tx() as db:
        row = db.execute("SELECT body FROM objects WHERE kind='run' AND id=?", (run["id"],)).fetchone()
        current = json.loads(row[0])
        if current["status"] in _FENCED or current.get("dispatch_frozen"):
            return
        if (current.get("recovery_pending") or {}).get("id") != incident["id"]:
            return
        previous = db.execute("SELECT body FROM objects WHERE kind='question' AND id=?", (identity,)).fetchone()
        if previous:
            return
        question = engine.store._put(db, "question", {"id": identity, "decision_id": identity, "run_id": run["id"],
            "kind": KIND, "status": "OPEN", "title": "任务需要你协助继续", "prompt": prompt,
             "context": {"incident_id": incident["id"], "step": incident["step"], "reason": incident["message"],
                        "can_restore_source": can_restore},
            "allow_text": True, "input_placeholder": "补充已知情况、原文位置或你希望如何继续",
            "options": [{"value": "retry", "label": retry}], "created_at": now_iso()})
        engine.store._put(db, "run", {**current, "status": "WAITING_USER", "summary": prompt,
            "recovery_pending": {**incident, "state": "waiting", "decision_id": identity}})
    engine.trace.try_emit(run["id"], "decision.opened", {"title": "等待你协助继续",
        "status": "WAITING_USER", "question": question})
    engine.trace.try_emit(run["id"], "runtime.recovery.waiting", {"title": "已保留任务，等待你回复",
        "status": "WAITING_USER", "step": incident["step"], "error_code": incident["error_code"],
        "summary": prompt})


def _original_match(engine, run, offered):
    try:
        uploaded = ArtifactRef.model_validate(offered)
    except ValueError:
        raise ContractError("RECOVERY_SOURCE_INVALID", "请从当前恢复窗口重新上传原文件。", status=422) from None
    conversation = run["conversation_id"]
    new, _ = engine.materials._record(uploaded, conversation)
    for source in run["input"]["material_refs"]:
        original = engine.store.get("material", source["id"], required=False)
        if (original and original["conversation_id"] == conversation and original["artifact_ref"] == source
                and original["manifest"]["original_sha256"] == new["manifest"]["original_sha256"]):
            return source, uploaded
    raise ContractError("RECOVERY_SOURCE_MISMATCH", "这份文件和本次锁定的原件不同，请重新选择同一份文件。", status=422)


def validate_answer(engine, run, question, answer):
    incident = run.get("recovery_pending") or {}
    if (incident.get("state") != "waiting" or incident.get("decision_id") != question["id"]
            or incident.get("id") != (question.get("context") or {}).get("incident_id")):
        raise ContractError("DECISION_STALE", "该恢复问题已变化，请刷新任务后继续。", status=409)
    if (isinstance(answer, dict) and set(answer) == {"text", "source_ref"}
            and isinstance(answer["text"], str) and 0 < len(answer["text"].strip()) <= 6000
            and question.get("context", {}).get("can_restore_source") is True):
        _original_match(engine, run, answer["source_ref"])
        return
    if answer == "retry" or (isinstance(answer, dict) and set(answer) == {"text"}
                              and isinstance(answer["text"], str) and 0 < len(answer["text"].strip()) <= 6000):
        return
    raise ContractError("ANSWER_INVALID", "请选择继续处理，或填写补充说明。", status=422)


def answer_changes(run, question):
    incident = run["recovery_pending"]
    answer = question["answer"]
    return {"status": "RECOVERING", "finish": None, "resume_resolution": None,
        "summary": "已收到你的答复，正在继续原任务。",
        "recovery_pending": {**incident, "state": "approved_resume", "answer_event_ref": question["answer_event_ref"],
                             "answer": answer}}


def _resume(engine, run, incident, *, selected_by):
    status = "WAITING_JOBS" if incident["stage"] == "job_observation" else (
        "ASSESSING_INTENT" if run["phase"] == "intent" else "RUNNING")
    event = {"event": "从原任务失败步骤继续", "step": incident["step"],
             "reason": incident["message"], "details": incident.get("details", {}),
             "user_note": incident["answer"]["text"] if isinstance(incident.get("answer"), dict) else None,
             "note": "沿用原任务范围、原文件与已保存结果。先核对失败步骤，已有写入以业务回执为准。"}
    human_budget = run.get("runtime_recovery_human_budget", 0)
    if selected_by == "human":
        human_budget += 1
    acknowledged = set(incident.get("acknowledged_model_attempt_ids", []))
    if selected_by == "human" and incident.get("outcome_unknown") and incident.get("answer") == "retry":
        with engine.store.tx() as db:
            row = db.execute("""SELECT id FROM attempts WHERE run_id=?
                AND json_extract(body, '$.state')='outcome_unknown'
                AND json_extract(body, '$.sent_at')<=?
                ORDER BY json_extract(body, '$.sent_at') DESC, rowid DESC LIMIT 1""",
                (run["id"], incident["created_at"])).fetchone()
        if row:
            acknowledged.add(row[0])
    incident = {**incident, "acknowledged_model_attempt_ids": sorted(acknowledged)}
    engine.store.update("run", run["id"], status=status, error=None, summary="正在继续原任务",
        recovery_pending={**incident, "state": "resuming", "selected_by": selected_by}, append_event=event,
        runtime_recovery_human_budget=human_budget)
    engine.trace.try_emit(run["id"], "runtime.recovery.resuming", {"title": "正在继续原任务",
        "status": "RECOVERING", "step": incident["step"], "summary": "沿用原任务和已保存成果。",
        "selected_by": selected_by})


async def drive(engine, run):
    incident = run.get("recovery_pending") or {}
    state = incident.get("state")
    if state == "approved_resume":
        answer = incident.get("answer")
        if isinstance(answer, dict) and answer.get("source_ref"):
            original, uploaded = _original_match(engine, run, answer["source_ref"])
            engine.materials.restore_same_original(original, uploaded, run["conversation_id"])
            engine.trace.try_emit(run["id"], "runtime.recovery.source_restored",
                {"title": "已核对并恢复同一份原件", "status": "RECOVERING", "step": incident["step"]})
        _resume(engine, run, incident, selected_by="human")
        return True
    if state != "planning":
        return state == "waiting"
    code = incident["error_code"]
    if run.get("pending_unknown_operation"):
        return True
    if incident.get("same_failure") or (
            incident["automatic_plans"] >= _MAX_PLANS and not incident.get("planner_reserved")):
        _open_question(engine, run, incident)
        return True
    if ((incident.get("outcome_unknown") and code.startswith("model_"))
            or code in {"model_provider_balance_insufficient", "BUDGET_EXHAUSTED", "OBSERVABILITY_UNAVAILABLE"}):
        _open_question(engine, run, incident)
        return True

    if code == "model_transport_failed" and not incident.get("outcome_unknown"):
        if not incident.get("planner_reserved"):
            incident = {**incident, "automatic_plans": incident["automatic_plans"] + 1,
                        "planner_reserved": True}
            engine.store.update("run", run["id"], recovery_pending=incident,
                runtime_recovery_plan_budget=run.get("runtime_recovery_plan_budget", 0) + 1)
        _resume(engine, run, incident, selected_by="program")
        return True

    # Reuse the main Agent's single-turn, enum-only recovery decision. The
    # workflow owns retries and the model has no direct business-write tools.
    from .fixed_step_recovery import _choose
    scope = RunScope(run["id"], "runtime_recovery_" + incident["id"], run["phase"])
    strategies = {"ask_user": "明确指出当前需要人工提供的事实或需要重新连接的服务。"}
    if (not incident.get("outcome_unknown")
            and (code in _TRANSIENT or code in {"FINISH_BLOCKED", "FINISH_MISSING", "INTENT_MISSING"}
                 or incident.get("details", {}).get("commit_state") == "not_committed")):
        strategies["retry_checkpoint"] = "在原任务范围内重读或续跑失败步骤，沿用已提交回执。"
    if len(strategies) == 1:
        _open_question(engine, run, incident)
        return True
    if not incident.get("planner_reserved"):
        incident = {**incident, "automatic_plans": incident["automatic_plans"] + 1, "planner_reserved": True}
        engine.store.update("run", run["id"], recovery_pending=incident,
            runtime_recovery_plan_budget=run.get("runtime_recovery_plan_budget", 0) + 1)
    engine.trace.try_emit(run["id"], "runtime.recovery.planning", {"title": "主 Agent 正在判断下一步",
        "status": "RECOVERING", "step": incident["step"], "summary": incident["message"]})
    try:
        strategy = await _choose(engine, scope, run, {"workflow": run.get("workflow_id", "main"),
            "step": incident["step"], "code": code, "message": incident["message"], "details": incident["details"],
            "execution_state": {"phase": run["phase"], "confirmed_operation_count": len(run.get("receipts", [])),
                "original_user_request": run["input"]["text"][:1200]}}, strategies)
    except Exception as error:
        strategy = "retry_checkpoint" if "retry_checkpoint" in strategies and code in _TRANSIENT else "ask_user"
        engine.trace.try_emit(run["id"], "runtime.recovery.planner_unavailable", {"title": "暂未取得主 Agent 的恢复决定",
            "status": "RECOVERING", "error_code": getattr(error, "code", type(error).__name__),
            "summary": "已保留原任务，将按可用路径继续处理。"})
        selected_by = "program"
    else:
        selected_by = "model"
    current = engine.store.get("run", run["id"])
    if current["status"] in _FENCED or current.get("dispatch_frozen"):
        return True
    if strategy == "retry_checkpoint":
        _resume(engine, current, incident, selected_by=selected_by)
    else:
        _open_question(engine, current, incident)
    return True


def receipt_reconciled(engine, run):
    incident = run.get("recovery_pending") or {}
    if incident.get("error_code") == "REMOTE_OUTCOME_UNKNOWN" or incident.get("stage") == "reconcile":
        # The original receipt is now verified. Keep its inbox event for the
        # graph instead of planning or asking again.
        engine.store.update("run", run["id"], recovery_pending={**incident, "state": "resuming",
            "selected_by": "receipt"}, error=None)
        engine.trace.try_emit(run["id"], "runtime.recovery.resuming", {"title": "原业务回执已核实，继续当前步骤",
            "status": "RECOVERING", "step": incident["step"], "summary": "已沿用原操作回执。"})


def clear_progress(engine, run):
    incident = run.get("recovery_pending") or {}
    if incident.get("state") == "resuming":
        engine.store.update("run", run["id"], recovery_pending=None, error=None,
            runtime_recovery_last={"round": incident["round"], "step": incident["step"]})



def step_model_attempts(engine, scope):
    """Return the attempts owned by this fixed step, in send order."""
    with engine.store.tx() as db:
        rows = db.execute("""SELECT body FROM attempts WHERE run_id=?
            AND json_extract(body, '$.role')=? AND json_extract(body, '$.sent_at') IS NOT NULL
            ORDER BY json_extract(body, '$.sent_at'), rowid""", (scope.run_id, scope.role)).fetchall()
    attempts = [json.loads(row[0]) for row in rows]
    # Older inline catalog runs had exactly one product_facts branch and did
    # not persist the branch ID. Other branches never inherit that exception.
    legacy_catalog = scope.role == "product_facts" and engine.store.get("run", scope.run_id).get("catalog_intake_enabled")
    return [item for item in attempts if item.get("branch_task_id") == scope.branch_task_id
            or not item.get("branch_task_id") and legacy_catalog]


def step_request_pending(attempts, scope, incident, *, adopted=None):
    """Keep unknown history, but fence only current or unacknowledged sends."""
    after_adoption = adopted is None
    acknowledged = set((incident or {}).get("acknowledged_model_attempt_ids", []))
    for item in attempts:
        if item.get("state") == "sent":
            return True
        if item["id"] == adopted:
            after_adoption = True
            continue
        if after_adoption and item.get("state") == "outcome_unknown" and item["id"] not in acknowledged:
            return True
        if after_adoption and item.get("invocation_id") == scope.invocation_id:
            return True
    return False


def permits_new_model_after_unknown(incident):
    """An explicit human choice can permit one new read-only model attempt."""
    return bool(incident and incident.get("outcome_unknown") and incident.get("state") == "resuming"
                and incident.get("selected_by") == "human" and incident.get("answer") == "retry")
