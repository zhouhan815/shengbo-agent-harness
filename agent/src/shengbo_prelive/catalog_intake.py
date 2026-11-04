"""Bounded catalog workflow on the existing runtime, checkpointer and grants."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_contracts.workflow_recovery import WorkflowRecoveryReceipt, WorkflowStepProgress
from .catalog_intake_price import CATALOG_INTAKE_PRICE_KIND, read_catalog_price_receipt
from .catalog_intake_currency import CATALOG_INTAKE_CURRENCY_KIND, read_catalog_currency_receipt
from .models import IntentAssessment, RunScope
from .document_import import (apply_document_import, existing_product, import_entry, is_catalog_request,
                              previous_import)

BASIC_CREATE_REQUEST = (
    "请根据本次上传资料核对商品身份，并新增资料能够支持的新商品。"
    "若可能与已有商品相同、资料涉及多个难以区分的商品，或关键事实不明确，请先向我确认。"
    "禁止修改已有商品。本次只整理并保存基础商品资料（基础信息、规格、商品知识库和常见问答），"
    "暂不开展顾客研究，不生成直播话术或语音。"
)
_EVIDENCE_FIELD_LABELS = {"name": "商品名称", "sku_id": "款式编号", "currency": "币种",
    "price": "日常售价", "core_selling_points": "核心卖点", "selling_points": "核心卖点",
    "specifications": "规格参数", "knowledge": "商品知识", "knowledge_documents": "知识文档",
    "faq": "常见问答", "material": "材质", "shipping_origin": "发货地",
    "shipping_time": "发货时效", "return_policy": "退换货政策"}


def explicit_intent(run):
    request = run["input"]
    if import_entry(run) == "room":
        return IntentAssessment(objective="catalog_update", summary="导入当前直播间的本场资料及商品",
            catalog_delivery="basic_facts_only", catalog_delivery_quote=request["text"],
            authorization_quote=request["text"])
    if ((request.get("view_context") or {}).get("product_material_action") != "create"
            or not (request.get("text") == BASIC_CREATE_REQUEST or import_entry(run) == "catalog"
                    and is_catalog_request(request.get("text", ""), BASIC_CREATE_REQUEST))
            or run.get("scope", {}).get("kind") != "catalog"
            or not 1 <= len(request.get("material_refs", [])) <= 4):
        return None
    return IntentAssessment(objective="catalog_update", summary="核对并新增基础商品资料",
        required_analyses=["product_facts"], catalog_delivery="basic_facts_only",
        catalog_delivery_quote=BASIC_CREATE_REQUEST, authorization_quote=BASIC_CREATE_REQUEST)


def owns_basic_creation(run, proposal, *, scope, target_ids, allowed):
    return ((scope.kind == "catalog" or scope.kind == "preparation" and import_entry(run) == "room") and proposal.objective == "catalog_update"
        and proposal.catalog_delivery == "basic_facts_only"
        and not target_ids and "save_product" in allowed
        and not proposal.requested_product_paths
        and (run["input"].get("view_context") or {}).get("product_material_action") != "update"
        and bool(run["input"].get("material_refs")))


class CatalogState(TypedDict, total=False):
    branch_id: str
    step: str
    decision: str
    price_decision_ids: list[str]
    currency_decision_ids: list[str]
    price_deferred: bool
    recovery_failure: dict
    recovery_draft: dict | None
    recovery_draft_kind: str | None
    session_failure: dict
    session_draft: dict


def _source_fingerprint(prepared):
    """Use immutable document structure and image bytes, not OCR float coordinates."""
    return canonical_hash({**prepared, "locations": [
        {key: value for key, value in row.items() if key not in {"ocr_regions", "text"}}
        if row["kind"] == "image" else row for row in prepared.get("locations", [])]})


def _same_source(prepared, fingerprint):
    return fingerprint in {_source_fingerprint(prepared), canonical_hash(prepared)}


def _active_runtime_resume(run):
    incident = run.get("recovery_pending") or {}
    if incident.get("state") == "resuming" and incident.get("stage") == "execute":
        return incident
    return None


def _resume_hint(incident):
    answer = incident.get("answer") if incident else None
    return answer.get("text") if isinstance(answer, dict) else None


def _archived_catalog_submissions(engine, worker, attempts, fingerprint, *, allow_legacy):
    """Use only a completed full submission owned by this run and branch."""
    from .catalog_extract import TOOL_NAME
    from .fixed_step_recovery import _archived_calls

    for item in reversed(attempts):
        recorded = item.get("source_fingerprint")
        if recorded != fingerprint and (recorded is not None or not allow_legacy):
            continue
        ref = item.get("response_payload_ref")
        if (not ref or item.get("provider_response_received") is not True
                or item.get("response_status") != "completed" or item.get("state") != "succeeded"):
            continue
        record = engine.trace.read_payload(worker.run_id, ref)
        if record.get("attempt_id") != item.get("provider_attempt_id") or not isinstance(record.get("output"), (dict, list)):
            continue
        output = record["output"]
        args = output.get("json") if isinstance(output, dict) else None
        if not isinstance(args, dict):
            calls = _archived_calls(engine, worker, item)
            if len(calls) == 1 and calls[0].get("name") == TOOL_NAME:
                args = calls[0].get("args")
        if isinstance(args, dict) and {"products", "questions"} <= args.keys():
            yield item, args


def build_catalog_graph(engine):
    def progress(scope, step, title, *, status="RUNNING", **details):
        event = WorkflowStepProgress(workflow="catalog_intake", step_id=step, title=title, status=status, **details)
        engine._emit(scope.run_id, "workflow.progress", event.model_dump(mode="json", exclude_none=True), scope)

    def record_validation(worker, error, *, strategy, status="failed", model_requests=0, step="extract",
                          public=True):
        details = error.details
        units = []
        for item in details.get("issues", []):
            if not isinstance(item, dict):
                continue
            if item.get("unit"):
                units.append(item["unit"])
            elif item.get("sku_id"):
                units.extend(f"{item['sku_id']}/{field}" for field in item.get("fields", []))
                units.extend(item.get("rejected_locations", []))
        units += [details.get("location")]
        if step == "session_validation":
            units += details.get("repair_fields", [])
        receipt = WorkflowRecoveryReceipt(workflow="catalog_intake", step_id=step,
            strategy=strategy, status=status, selected_by="program", model_requests=model_requests,
            issue_codes=[error.code], affected_units=list(dict.fromkeys(
                unit for unit in units if isinstance(unit, str) and 0 < len(unit) <= 160))[:64])
        branch = engine.store.get("branch", worker.branch_task_id)
        stored = receipt.model_dump(mode="json", exclude_none=True)
        engine.store.update("branch", worker.branch_task_id,
            catalog_validation_receipts=[*branch.get("catalog_validation_receipts", [])[-7:], stored])
        if public:
            engine._emit(worker.run_id, "workflow.validation", {"title": "本场配置核对结果" if step == "session_validation"
                         else "商品资料核对结果", "recovery": stored,
                         **({"issues": details.get("issues", [])} if step == "session_validation" else {})}, worker)

    def accept_extraction(worker, report):
        if any("currency" not in row for row in report["fields"]["products"]):
            engine.store.update("branch", worker.branch_task_id, catalog_intake_currency_draft=report)
            return "currency"
        if any("price" not in row for row in report["fields"]["products"]):
            engine.store.update("branch", worker.branch_task_id, catalog_intake_price_draft=report)
            return "price"
        engine.submit_report(worker, report, strict_evidence=True)
        return "prepare"

    def session_repair_state(worker, error, prepared):
        failure = {"workflow": "catalog_intake", "step": "session_validation", "branch_id": worker.branch_task_id,
                   "code": error.code, "message": error.message, "status": error.status,
                   "details": error.details, "known_no_commit": True,
                   "evidence_hash": _source_fingerprint(prepared), "disposition": "field_repair"}
        record_validation(worker, error, strategy="repair_session", step="session_validation")
        return {"branch_id": worker.branch_task_id, "step": "session_repair", "session_failure": failure,
                "session_draft": error.draft}

    async def extract(state: CatalogState, runtime: Runtime[RunScope]):
        from .catalog_extract import (CatalogResponseRejection, SessionCorrection, extract_catalog_report,
                                      validate_catalog_submission)
        scope = runtime.context
        run = await engine.assert_scope(scope)
        progress(scope, "extract", "正在读取商品资料")
        if import_entry(run) and previous_import(engine, run):
            return {"step": "document"}
        if not run.get("task_assessment"):
            engine.save_assessment(scope, {"summary": "整理资料、确认候选、逐款入库",
                "capabilities": ["product_facts"], "steps": ["读取附件", "整理事实", "确认并保存"]})
        sources = run["input"]["material_refs"]
        task = {"role": "product_facts", "instruction": "从同一份资料提取各款商品与原文证据", "source_refs": sources}
        batch = await engine.dispatch(scope, [task], "基础入库的事实提取", "catalog-intake-facts", inline_catalog=True)
        branch_id = batch["branch_task_ids"][0]
        branch = engine.store.get("branch", branch_id)
        if branch["status"] in {"PREPARED", "ACCEPTED"}:
            return {"branch_id": branch_id, "step": "prepare"}
        if branch.get("catalog_intake_price_draft"):
            return {"branch_id": branch_id, "step": "price"}
        if branch.get("catalog_intake_currency_draft"):
            return {"branch_id": branch_id, "step": "currency"}
        incident = _active_runtime_resume(run)
        invocation_id = scope.invocation_id
        if incident:
            invocation_id = "invoke_catalog_resume_" + canonical_hash([scope.run_id, branch_id, incident["id"]])[7:]
        worker = RunScope(scope.run_id, invocation_id, "execute", "product_facts", branch_id)
        started = time.perf_counter()
        recovery = None
        recovered = False
        handed_off = False
        prepared = None
        try:
            async with asyncio.timeout(110):
                prepared = await prepared_source(worker, sources)
                if prepared is None:
                    raise ContractError("CATALOG_SOURCE_LAYOUT_UNSUPPORTED",
                        "当前资料未能读出完整的商品内容。请检查原件是否可打开，或分批上传后继续整理。", status=422)
                if any(item["kind"] == "image" and not item["text"].strip() for item in prepared["locations"]):
                    progress(scope, "extract", "照片没有文字，正在结合正文和原图继续整理")
                from .runtime_recovery import step_model_attempts, step_request_pending
                attempts = step_model_attempts(engine, worker)
                # Only adopt an archive tied to this unchanged source snapshot.
                old_source = branch.get("catalog_prepared_source")
                archived, arguments = (None, None)
                if old_source and _source_fingerprint(old_source) == _source_fingerprint(prepared):
                    archived, arguments = next(_archived_catalog_submissions(engine, worker, attempts,
                        _source_fingerprint(prepared), allow_legacy=not branch.get("catalog_source_reprepared")),
                        (None, None))
                adopted = archived["id"] if archived else None
                if step_request_pending(attempts, worker, incident, adopted=adopted):
                    raise ContractError("CATALOG_EXTRACTION_INTERRUPTED",
                        "当前资料核对仍有未能确认的请求结果。", status=409)
                if archived:
                    engine.store.update("branch", branch_id, catalog_adopted_attempt_id=adopted)
                    progress(scope, "extract", "已找回这份原件的模型回复，正在复核已有草稿", status="RECOVERING")
                    engine._emit(scope.run_id, "catalog_intake.recovery", {
                        "title": "已沿用原任务已收到的提取结果", "status": "RECOVERING",
                        "method": "reuse_archived_response", "attempt_id": adopted}, worker)
                feedback = None
                for attempt in range(2):
                    remaining = 110 - (time.perf_counter() - started)
                    if remaining <= 5:
                        raise ContractError("CATALOG_EXTRACTION_TIMEOUT", "商品资料对账超时，请缩小资料范围后重试。", status=408)
                    alternate_layout = attempt == 1
                    if alternate_layout:
                        recovery = {"method": "structured_source_layout",
                                    "trigger_codes": [feedback["code"]],
                                    "title": "按原表格与段落结构重新核对资料"}
                        engine._emit(scope.run_id, "catalog_intake.recovery",
                            {**recovery, "state": "started", "status": "RECOVERING"}, worker)
                    try:
                        if attempt == 0 and archived:
                            report = await validate_catalog_submission(engine, worker, run, prepared, arguments)
                        else:
                            report = await extract_catalog_report(engine, worker, prepared, feedback=feedback,
                                                                  alternate_layout=alternate_layout,
                                                                  user_hint=_resume_hint(incident),
                                                                  timeout_seconds=min(104, remaining - 2))
                    except SessionCorrection as error:
                        return session_repair_state(worker, error, prepared)
                    except CatalogResponseRejection as error:
                        rejection = error
                    else:
                        if report.get("document_session_only"):
                            engine.store.update("branch", branch_id, status="CANCELLED",
                                                summary="资料只包含本场配置，无商品需要入库。")
                            return {"branch_id": branch_id, "step": "document"}
                        if report.get("conflicts") or not report.get("fields", {}).get("products"):
                            rejection = CatalogResponseRejection("CATALOG_RECOVERY_UNRESOLVED",
                                "资料中有商品信息待确认。", status=422,
                                details={"unresolved": report.get("conflicts", [])})
                        else:
                            rejection = None
                    if rejection is None:
                        try:
                            step = accept_extraction(worker, report)
                        except ContractError as error:
                            current = engine.store.get("branch", branch_id)
                            if (error.status != 422 or error.code.endswith("_DENIED")
                                    or current.get("report") or current.get("catalog_intake_price_draft")
                                    or current.get("catalog_intake_currency_draft")):
                                raise
                            rejection = error
                        else:
                            recovered = alternate_layout
                            return {"branch_id": branch_id, "step": step}

                    severe = rejection.details.get("major_extraction_failure") is True or rejection.code in {
                        "model_tool_call_invalid", "model_empty_completed_response", "model_json_output_invalid",
                    }
                    record_validation(worker, rejection, strategy="structured_source_layout" if alternate_layout
                                      else "extract", public=severe)
                    if attempt or not severe:
                        handed_off = True
                        raise rejection
                    feedback = {"code": rejection.code, "details": rejection.details}
        except (TimeoutError, ContractError) as error:
            if isinstance(error, ContractError) and error.code not in {"CATALOG_EXTRACTION_TIMEOUT", "MODEL_TIMEOUT"}:
                raise
            failure = {"workflow": "catalog_intake", "step": "extract", "branch_id": branch_id,
                       "code": "CATALOG_EXTRACTION_TIMEOUT", "message": "读取或提取商品资料超时。",
                       "status": 408, "details": {}, "known_no_commit": True,
                       "evidence_hash": _source_fingerprint(prepared) if prepared else None}
            handed_off = True
            return {"branch_id": branch_id, "step": "needs_recovery", "recovery_failure": failure}
        finally:
            if recovery:
                outcome = "succeeded" if recovered else "handed_off" if handed_off else "failed"
                engine.trace.try_emit(scope.run_id, "catalog_intake.recovery", {**recovery, "state": outcome,
                    "status": "SUCCEEDED" if recovered else "RECOVERING" if handed_off else "FAILED",
                    "title": "已重新核对商品资料" if recovered else
                             "按原文结构核对后仍有疑点，继续处理当前步骤" if handed_off else "重新核对后仍未确认全部商品资料"},
                    invocation_id=worker.invocation_id, branch_task_id=worker.branch_task_id)
            engine._emit(scope.run_id, "catalog_intake.extraction", {"title": "整理商品资料",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3)}, scope)

    async def prepared_source(worker, sources):
        from .catalog_extract import prepare_catalog_extraction

        branch = engine.store.get("branch", worker.branch_task_id)
        snapshot = branch.get("catalog_prepared_source")
        if snapshot and snapshot.get("sources") == sources:
            schema = await engine.business_query(worker, "get_business_schema", {})
            if schema["schema_hash"] == snapshot["schema_hash"]:
                return snapshot
        prepared = await prepare_catalog_extraction(engine, worker, sources)
        if prepared:
            engine.store.update("branch", worker.branch_task_id, catalog_prepared_source=prepared,
                                catalog_source_reprepared=bool(snapshot))
        return prepared

    async def recover(state: CatalogState, runtime: Runtime[RunScope]):
        from .catalog_extract import (CoverageCorrection, EvidenceCorrection, SessionCorrection,
                                      extract_catalog_report, reread_catalog_evidence)

        scope = runtime.context
        run = await engine.assert_scope(scope)
        branch_id = state["branch_id"]
        branch = engine.store.get("branch", branch_id)
        if branch["status"] in {"PREPARED", "ACCEPTED"}:
            return {"step": "prepare", "recovery_draft": None}
        if branch.get("catalog_intake_price_draft"):
            return {"step": "price", "recovery_draft": None}
        if branch.get("catalog_intake_currency_draft"):
            return {"step": "currency", "recovery_draft": None}

        failure = state["recovery_failure"]
        progress(scope, "extract", "正在换一种方法核对商品资料", status="RECOVERING")
        original = ContractError(failure["code"], failure["message"], status=failure["status"], details=failure["details"])
        draft = state.get("recovery_draft")
        evidence_only = state.get("recovery_draft_kind") == "evidence" and draft is not None
        strategies = {"reread_source": "用原文定位重新读取被校验指出的材料，再重新核对候选。"}
        if evidence_only:
            strategies = {"repair_evidence": "重读涉及的原文，仅核对未通过的来源引用。"}
        elif draft is not None:
            strategies = {"repair_candidate": "保留已核对的候选，仅重读和修正未通过的原文去向。"}

        incident = _active_runtime_resume(run)
        invocation_key = [scope.run_id, branch_id, failure["step"]]
        if incident:
            invocation_key.append(incident["id"])
        invocation_id = "invoke_catalog_recovery_" + canonical_hash(invocation_key)[7:]
        worker = RunScope(scope.run_id, invocation_id, "execute", "product_facts", branch_id)
        from .runtime_recovery import step_model_attempts, step_request_pending
        attempts = step_model_attempts(engine, worker)
        if incident:
            prepared = await prepared_source(worker, run["input"]["material_refs"])
            if prepared and failure["evidence_hash"] and _same_source(prepared, failure["evidence_hash"]):
                adopted = await adopt_archived_extraction(worker, run, prepared, branch, attempts, incident)
                if adopted:
                    return adopted
        if step_request_pending(attempts, worker, incident, adopted=branch.get("catalog_adopted_attempt_id")):
            raise ContractError("CATALOG_EXTRACTION_INTERRUPTED", "当前资料核对仍有未能确认的请求结果。", status=409)

        # A timed-out read or extraction has one known safe next action.
        if failure["code"] == "CATALOG_EXTRACTION_TIMEOUT":
            engine.store.update("run", scope.run_id, catalog_timeout_retry=True)
            strategy = "reread_source"
        elif incident:
            # The durable runtime decision already authorized this exact failed step.
            strategy = next(iter(strategies))
        else:
            strategy = await engine.recover_fixed_step(worker, failure, strategies=strategies, automatic_if_single=True)
        if strategy not in strategies:
            raise original
        recovery_event = {"workflow": failure["workflow"], "step": failure["step"], "strategy": strategy}
        try:
            async with asyncio.timeout(75):
                prepared = await prepared_source(worker, run["input"]["material_refs"])
                if prepared is None or (failure["evidence_hash"] and not _same_source(prepared, failure["evidence_hash"])):
                    raise ContractError("CATALOG_RECOVERY_SOURCE_CHANGED", "资料或商品字段规则已变化，请重新提交资料。", status=409)
                options = {"feedback": {"code": original.code, "details": original.details}, "alternate_layout": True}
                if strategy == "repair_candidate":
                    options = {"feedback": CoverageCorrection(original, draft),
                               "reread_evidence": await reread_catalog_evidence(engine, worker, prepared, original.details)}
                elif strategy == "repair_evidence":
                    options = {"feedback": EvidenceCorrection(original, draft),
                               "reread_evidence": await reread_catalog_evidence(engine, worker, prepared, original.details)}
                elif strategy == "reread_source" and failure["code"] != "CATALOG_EXTRACTION_TIMEOUT":
                    options["reread_evidence"] = await reread_catalog_evidence(engine, worker, prepared, original.details)
                if incident:
                    options["user_hint"] = _resume_hint(incident)
                try:
                    report = await extract_catalog_report(engine, worker, prepared, timeout_seconds=70, **options)
                except SessionCorrection as error:
                    return session_repair_state(worker, error, prepared)
                if report.get("document_session_only"):
                    engine.store.update("branch", branch_id, status="CANCELLED",
                                        summary="资料只包含本场配置，无商品需要入库。")
                    return {"step": "document", "recovery_draft": None}
                if report.get("conflicts") or not report.get("fields", {}).get("products"):
                    raise ContractError("CATALOG_RECOVERY_UNRESOLVED",
                        "重新核对后仍无法确认可保存的商品。请核对资料中的商品型号、日常售价和款式归属。", status=422)
                step = accept_extraction(worker, report)
        except TimeoutError:
            engine.trace.try_emit(scope.run_id, "fixed_step_recovery.failed", {**recovery_event,
                "status": "FAILED", "error_code": "CATALOG_EXTRACTION_TIMEOUT", "title": "重新核对商品资料超时"},
                invocation_id=worker.invocation_id, branch_task_id=branch_id)
            raise ContractError("CATALOG_EXTRACTION_TIMEOUT", "资料重新核对超时，请缩小资料范围后重试。", status=408) from None
        except Exception as error:
            engine.trace.try_emit(scope.run_id, "fixed_step_recovery.failed", {**recovery_event,
                "status": "FAILED", "error_code": getattr(error, "code", type(error).__name__),
                "title": "补救后仍有商品资料未通过核对"}, invocation_id=worker.invocation_id, branch_task_id=branch_id)
            if evidence_only and isinstance(error, CoverageCorrection):
                record_validation(worker, error, strategy="repair_coverage")
                return {"step": "needs_recovery", "recovery_failure": {**failure, "step": "coverage_validation",
                        "code": error.code, "message": error.message, "status": error.status,
                        "details": error.details, "disposition": "field_repair"},
                        "recovery_draft": error.draft, "recovery_draft_kind": "coverage"}
            if evidence_only and isinstance(error, EvidenceCorrection):
                record_validation(worker, error, strategy="repair_evidence", status="needs_input")
                return {"step": "evidence_wait", "recovery_failure": {**failure, "details": error.details},
                        "recovery_draft": error.draft}
            raise
        engine.trace.try_emit(scope.run_id, "fixed_step_recovery.completed", {**recovery_event,
            "status": "SUCCEEDED", "title": "商品资料重新核对通过，继续入库确认"},
            invocation_id=worker.invocation_id, branch_task_id=branch_id)
        return {"step": step, "recovery_draft": None}

    async def session_repair(state: CatalogState, runtime: Runtime[RunScope]):
        from .catalog_extract import (keep_verified_session_fields, request_catalog_session_patch,
                                      verify_catalog_session_patch)
        from .fixed_step_recovery import _archived_calls, _sent_attempt
        from .providers.chatgpt import ModelProviderError

        scope = runtime.context
        run = await engine.assert_scope(scope)
        branch_id, failure, draft = state["branch_id"], state["session_failure"], state["session_draft"]
        branch = engine.store.get("branch", branch_id)
        if branch["status"] in {"PREPARED", "ACCEPTED"}:
            return {"step": "prepare"}
        if branch.get("catalog_intake_price_draft"):
            return {"step": "price"}
        if branch.get("catalog_intake_currency_draft"):
            return {"step": "currency"}
        incident = _active_runtime_resume(run)
        identity = canonical_hash([scope.run_id, branch_id, failure["evidence_hash"],
                                   (incident or {}).get("id")])[7:]
        worker = RunScope(scope.run_id, "invoke_session_repair_" + identity, "execute", "product_facts", branch_id)
        prepared = await prepared_source(worker, run["input"]["material_refs"])
        if not prepared or not _same_source(prepared, failure["evidence_hash"]):
            raise ContractError("CATALOG_RECOVERY_SOURCE_CHANGED", "原资料或本场核对规则已变化。", status=409)
        original = ContractError(failure["code"], failure["message"], status=failure["status"], details=failure["details"])
        progress(scope, "session_validation", "已保留商品结果，正在单独核对本场配置", status="RECOVERING")
        attempt = _sent_attempt(engine, worker)
        try:
            if attempt and attempt.get("state") == "succeeded" and attempt.get("response_payload_ref"):
                archived = engine.trace.read_payload(scope.run_id, attempt["response_payload_ref"])
                output = archived.get("output")
                raw = output.get("json") if isinstance(output, dict) else None
                if raw is None:
                    calls = _archived_calls(engine, worker, attempt)
                    raw = calls[0].get("args") if len(calls) == 1 and calls[0].get("name") == "repair_catalog_session" else None
            elif attempt:
                raw = None
            else:
                await engine.recover_fixed_step(worker, failure,
                    strategies={"repair_session": "仅核对本场未通过的字段，沿用已经通过的商品资料。"},
                    automatic_if_single=True)
                async with asyncio.timeout(55):
                    raw = await request_catalog_session_patch(engine, worker, prepared, failure, draft["session"])
            session = verify_catalog_session_patch(raw, draft["session"], failure, prepared)
            if session is None and draft["report"].get("document_session_only"):
                raise original
        except (ContractError, TimeoutError, ModelProviderError) as error:
            if (isinstance(error, ModelProviderError) and (error.outcome_unknown or error.code not in {
                    "model_tool_call_invalid", "model_empty_completed_response", "model_json_output_invalid"})
                    or isinstance(error, ContractError) and (error.status in {401, 403} or error.code.endswith("_DENIED"))):
                raise
            issue = error if isinstance(error, ContractError) and error.code == original.code else original
            record_validation(worker, issue, strategy="repair_session", status="needs_input", step="session_validation")
            session = keep_verified_session_fields(draft["session"], original, prepared)
            progress(scope, "session_validation", "已保留本场已核实内容，未核对的字段标为待补充", status="SUCCEEDED")
        else:
            progress(scope, "session_validation", "本场配置已单独核对通过", status="SUCCEEDED")
        engine.store.update("branch", branch_id, document_session=session,
                            document_session_source_hash=failure["evidence_hash"])
        report = draft["report"]
        if report.get("document_session_only"):
            engine.store.update("branch", branch_id, status="CANCELLED", summary="资料只包含本场配置。")
            return {"step": "document"}
        if report.get("conflicts") or not report.get("fields", {}).get("products"):
            raise ContractError("CATALOG_RECOVERY_UNRESOLVED", "仍有商品信息需要核对。", status=422,
                                details={"unresolved": report.get("conflicts", [])})
        return {"step": accept_extraction(worker, report)}

    async def adopt_archived_extraction(worker, run, prepared, branch, attempts, incident=None):
        from .catalog_extract import CatalogResponseRejection, SessionCorrection, validate_catalog_submission
        from .runtime_recovery import step_request_pending

        for item, args in _archived_catalog_submissions(engine, worker, attempts, _source_fingerprint(prepared),
                allow_legacy=not branch.get("catalog_source_reprepared")):
            if step_request_pending(attempts, worker, incident, adopted=item["id"]):
                continue
            try:
                report = await validate_catalog_submission(engine, worker, run, prepared, args)
            except SessionCorrection as error:
                return session_repair_state(worker, error, prepared)
            except CatalogResponseRejection as error:
                record_validation(worker, error, strategy="extract")
                continue
            if report.get("document_session_only"):
                engine.store.update("branch", worker.branch_task_id, status="CANCELLED", summary="资料只包含本场配置。")
                return {"step": "document", "recovery_draft": None}
            if report.get("conflicts") or not report.get("fields", {}).get("products"):
                continue
            step = accept_extraction(worker, report)
            engine.store.update("branch", worker.branch_task_id, catalog_adopted_attempt_id=item["id"])
            engine._emit(worker.run_id, "catalog_intake.recovery", {
                "title": "已沿用原任务已收到的提取结果", "status": "RECOVERING",
                "method": "reuse_archived_response", "attempt_id": item["id"]}, worker)
            return {"step": step, "recovery_draft": None}
        return None

    async def evidence_wait(state: CatalogState, runtime: Runtime[RunScope]):
        from .catalog_extract import EvidenceCorrection, extract_catalog_report

        scope = runtime.context
        run = await engine.assert_scope(scope)
        branch_id = state["branch_id"]
        branch = engine.store.get("branch", branch_id)
        if branch["status"] in {"PREPARED", "ACCEPTED"}:
            return {"step": "prepare"}
        if branch.get("catalog_intake_currency_draft"):
            return {"step": "currency"}
        if branch.get("catalog_intake_price_draft"):
            return {"step": "price"}
        failure = state["recovery_failure"]
        draft = state["recovery_draft"]
        draft_hash = canonical_hash(draft)
        progress(scope, "extract", "已保留提取结果，等待补充原文线索", status="WAITING")
        affected = []
        for issue in failure["details"]["issues"]:
            product = draft["products"][issue["candidate_index"]]["fields"]
            fields = "、".join(dict.fromkeys(_EVIDENCE_FIELD_LABELS.get(key, "其他商品资料")
                                           for key in issue["fields"]))
            affected.append(f"「{product['name'][:80]}」的{fields}")
        pending = "；".join(affected[:8])
        result = await engine.request_decision(scope, {"kind": "catalog_evidence_clarification",
            "prompt": f"还需要确认{pending}的出处。请指出原文件里的页码、表格或段落，系统会只核对这些内容。",
            "options": [{"value": "skip", "label": "暂不入库"}]}, f"catalog-evidence-{draft_hash}")
        answer = result.get("answer")
        if answer == "skip":
            return {"step": "cancelled", "decision": "skip"}
        hint = answer.get("text") if isinstance(answer, dict) else None
        if not isinstance(hint, str) or not hint.strip():
            raise ContractError("CATALOG_EVIDENCE_HINT_INVALID", "缺少可核对的原文线索。", status=422)
        invocation_id = "invoke_catalog_evidence_" + canonical_hash([scope.run_id, draft_hash])[7:39]
        worker = RunScope(scope.run_id, invocation_id, "execute", "product_facts", branch_id)
        with engine.store.tx() as db:
            sent = db.execute("""SELECT 1 FROM attempts WHERE run_id=?
                AND json_extract(body,'$.invocation_id')=? AND json_extract(body,'$.sent_at') IS NOT NULL LIMIT 1""",
                (scope.run_id, invocation_id)).fetchone()
        if sent:
            raise ContractError("CATALOG_EXTRACTION_INTERRUPTED", "补充线索的核对已发出，请查看原有执行记录。", status=409)
        prepared = await prepared_source(worker, run["input"]["material_refs"])
        if prepared is None or not _same_source(prepared, failure["evidence_hash"]):
            raise ContractError("CATALOG_RECOVERY_SOURCE_CHANGED", "原始资料或字段规则已变化，请重新核对。", status=409)
        from .runtime_recovery import step_model_attempts
        adopted = await adopt_archived_extraction(worker, run, prepared, branch, step_model_attempts(engine, worker),
                                                   _active_runtime_resume(run))
        if adopted:
            return adopted
        engine.store.update("run", scope.run_id, catalog_evidence_hint_retry=True)
        progress(scope, "extract", "正在根据补充线索核对原文", status="RECOVERING")
        original = ContractError(failure["code"], failure["message"], status=failure["status"], details=failure["details"])
        try:
            report = await extract_catalog_report(engine, worker, prepared, feedback=EvidenceCorrection(original, draft),
                                                  user_hint=hint[:1200], timeout_seconds=70)
        except EvidenceCorrection as error:
            record_validation(worker, error, strategy="verify_user_source_hint")
            raise ContractError("CATALOG_EVIDENCE_UNCONFIRMED",
                                "补充线索未能核实待处理信息，草稿已保留。请补充可核对的原始资料。", status=422) from error
        return {"step": accept_extraction(worker, report), "recovery_draft": None}

    async def currency(state: CatalogState, runtime: Runtime[RunScope]):
        scope = runtime.context
        run = await engine.assert_scope(scope)
        branch_id = state["branch_id"]
        branch = engine.store.get("branch", branch_id)
        if branch["status"] in {"PREPARED", "ACCEPTED"}:
            return {"step": "prepare"}
        if branch.get("catalog_intake_price_draft"):
            return {"step": "price"}
        draft = branch["catalog_intake_currency_draft"]
        draft_hash = canonical_hash(draft)
        missing = [row["sku_id"] for row in draft["fields"]["products"] if "currency" not in row]
        ids = state.get("currency_decision_ids", [])
        receipts = [read_catalog_currency_receipt(engine, run, identity) for identity in ids]
        if ([receipt["sku_id"] for receipt in receipts] != missing[:len(receipts)]
                or any(receipt["draft_hash"] != draft_hash for receipt in receipts)):
            raise ContractError("CATALOG_CURRENCY_BINDING_INVALID", "币种答复与本次待补规格不一致。", status=409)
        index = len(receipts)
        if index < len(missing):
            progress(scope, "currency", "已保留商品资料，等待确认日常售价币种", status="WAITING",
                completed_count=index, total_count=len(missing), recovery=WorkflowRecoveryReceipt(
                    workflow="catalog_intake", step_id="currency", strategy="confirm_currency",
                    status="needs_input", selected_by="program", model_requests=0, disposition="missing_input",
                    input_hash=draft_hash, issue_codes=["CATALOG_CURRENCY_MISSING"], affected_units=[missing[index]]
                    if len(missing[index]) <= 160 else []))
            context = {"branch_id": branch_id, "draft_hash": draft_hash, "sku_id": missing[index]}
            result = await engine.request_decision(scope, {"kind": CATALOG_INTAKE_CURRENCY_KIND,
                "prompt": "请确认日常售价币种。", "context": context},
                f"catalog-intake-currency-{branch_id}-{index}")
            return {"step": "currency", "currency_decision_ids": [*ids, result["decision_id"]]}
        report = deepcopy(draft)
        supplied = {receipt["sku_id"]: receipt["currency"] for receipt in receipts}
        for row in report["fields"]["products"]:
            if row["sku_id"] in supplied:
                row["currency"] = supplied[row["sku_id"]]
        report.setdefault("insights", []).extend({"kind": "catalog_human_currency", **receipt} for receipt in receipts)
        worker = RunScope(scope.run_id, scope.invocation_id, "execute", "product_facts", branch_id)
        return {"step": accept_extraction(worker, report)}

    async def price(state: CatalogState, runtime: Runtime[RunScope]):
        scope = runtime.context
        progress(scope, "price", "等待补充商品的日常售价", status="WAITING")
        run = await engine.assert_scope(scope)
        branch_id = state["branch_id"]
        branch = engine.store.get("branch", branch_id)
        if branch["status"] in {"PREPARED", "ACCEPTED"}:
            return {"step": "prepare"}
        draft = branch["catalog_intake_price_draft"]
        draft_hash = canonical_hash(draft)
        missing = [row["sku_id"] for row in draft["fields"]["products"] if "price" not in row]
        receipts = [read_catalog_price_receipt(engine, run, identity)
                    for identity in state.get("price_decision_ids", [])]
        if (any(receipt["draft_hash"] != draft_hash or receipt["price"] is None for receipt in receipts)
                or [receipt["sku_id"] for receipt in receipts] != missing[:len(receipts)]):
            raise ContractError("CATALOG_PRICE_BINDING_INVALID", "本次商品的价格答复与待补规格不一致。", status=409)
        index = len(receipts)
        if index < len(missing):
            deferred = state.get("price_deferred", False)
            result = await engine.request_decision(scope, {"kind": CATALOG_INTAKE_PRICE_KIND,
                "prompt": "请填写日常售价。", "context": {"branch_id": branch_id,
                    "draft_hash": draft_hash, "sku_id": missing[index], "deferred": deferred}},
                f"catalog-intake-price-{branch_id}-{index}-{deferred}")
            identity = result["decision_id"]
            receipt = read_catalog_price_receipt(engine, run, identity)
            ids = state.get("price_decision_ids", [])
            if receipt["price"] is None:
                return {"step": "price", "price_deferred": True}
            return {"step": "price", "price_deferred": False, "price_decision_ids": [*ids, identity]}

        report = deepcopy(draft)
        prices = {receipt["sku_id"]: receipt["price"] for receipt in receipts}
        for row in report["fields"]["products"]:
            if row["sku_id"] in prices:
                row["price"] = prices[row["sku_id"]]
        report.setdefault("insights", []).extend({"kind": "catalog_human_daily_price", **receipt} for receipt in receipts)
        worker = RunScope(scope.run_id, scope.invocation_id, "execute", "product_facts", branch_id)
        engine.submit_report(worker, report, strict_evidence=True)
        return {"step": "prepare"}

    async def prepare(state: CatalogState, runtime: Runtime[RunScope]):
        from .catalog_intake_approval import make_catalog_intake_plan
        scope = runtime.context
        run = await engine.assert_scope(scope)
        progress(scope, "prepare", "正在核对商品款式和入库资料")
        if run.get("catalog_intake_plan"):
            return {"step": "confirm"}
        branch = engine.store.get("branch", state["branch_id"])
        candidates = branch["report"]["fields"]["products"]
        schema = await engine.business_query(scope, "get_business_schema", {})
        reused = {}
        for fields in candidates:
            result = await engine.business_query(scope, "validate_product_assembly",
                {"schema_hash": schema["schema_hash"], "fields": fields})
            if not result.get("valid"):
                raise ContractError("CATALOG_CANDIDATE_INVALID", "商品候选未通过商品字段校验。", status=422)
            if import_entry(run):
                ref = await existing_product(engine, scope, fields)
                if ref:
                    reused[fields["sku_id"]] = ref
                    continue
            for query in (fields["sku_id"], fields["name"]):
                existing = await engine.business_query(scope, "search_products", {"q": query, "limit": 50})
                if existing.get("items"):
                    raise ContractError("CATALOG_RECOVERY_DUPLICATE_UNRESOLVED",
                        f"「{fields['name']}」可能已在商品库中。请核对商品编号，若是已有商品，请从该商品选择更新资料。", status=409)
        await engine.accept_reports(scope, [branch["id"]], "catalog-intake-accept")
        plan = make_catalog_intake_plan(run, branch_task_id=branch["id"],
            report_hash=branch["report_hash"], candidates=candidates)
        engine.store.update("run", scope.run_id, catalog_intake_plan=plan, document_reused_products=reused)
        return {"step": "confirm"}

    async def confirm(state: CatalogState, runtime: Runtime[RunScope]):
        from .catalog_intake_approval import resolve_catalog_intake_decision
        scope = runtime.context
        progress(scope, "confirm", "等待确认本次要入库的商品", status="WAITING")
        run = await engine.assert_scope(scope)
        round_number = run.get("catalog_intake_confirmation_round", 1)
        await engine.request_decision(scope, {"kind": "catalog_intake_approval", "prompt": "请确认要入库的款式。"},
                                      f"catalog-intake-confirm-{round_number}")
        current = engine.store.get("run", scope.run_id)
        decision = resolve_catalog_intake_decision(engine, current)
        if decision["state"] == "supplement":
            engine.store.update("run", scope.run_id, catalog_intake_confirmation_round=round_number + 1,
                                catalog_intake_supplement_note=decision["text"])
            return {"decision": "supplement"}
        return {"decision": decision["state"]}

    async def commit(state: CatalogState, runtime: Runtime[RunScope]):
        from .catalog_intake_approval import resolve_catalog_intake_decision
        from .catalog_intake_report import build_catalog_intake_result
        from .product_materials import save_accepted_product
        scope = runtime.context
        run = await engine.assert_scope(scope)
        decision = resolve_catalog_intake_decision(engine, run)
        if decision["state"] != "approved":
            raise ContractError("CATALOG_INTAKE_NOT_APPROVED", "请先确认要保存的商品款式。", status=403)
        branch = engine.store.get("branch", state["branch_id"])
        chosen = decision["selected_sku_ids"]
        expected_images = {row["fields"]["sku_id"]: row["fields"].get("image")
                           for row in run["catalog_intake_plan"]["candidates"]}
        saved_products = []
        for sku_id in chosen:
            progress(scope, "commit", "正在保存并逐款核对商品", completed_count=len(saved_products),
                total_count=len(chosen))
            reused = (run.get("document_reused_products") or {}).get(sku_id)
            if reused:
                item = await engine.business_query(scope, "read_product", {"ref": reused})
                if item.get("ref") != reused or item["body"].get("sku_id", "").casefold() != sku_id.casefold():
                    raise ContractError("DOCUMENT_IMPORT_PRODUCT_IDENTITY", "已有商品无法回读核对。", status=409)
                saved_products.append(item)
                continue
            try:
                receipt = await save_accepted_product(engine, scope, {"branch_task_id": branch["id"],
                    "accepted_report_ref": branch["accepted_ref"], "candidate_sku_id": sku_id},
                    tool_call_id=f"catalog-intake-save-{run['catalog_intake_plan']['plan_hash']}-{sku_id}")
            except ContractError as error:
                if not import_entry(run) or error.code not in {"SKU_CONFLICT", "PRODUCT_IDENTITY_CONFIRMATION_REQUIRED"}:
                    raise
                fields = next(row["fields"] for row in run["catalog_intake_plan"]["candidates"] if row["sku_id"] == sku_id)
                concurrent = await existing_product(engine, scope, fields)
                if not concurrent:
                    raise
                saved_products.append(await engine.business_query(scope, "read_product", {"ref": concurrent}))
                continue
            saved = await engine.business_query(scope, "read_product", {"ref": receipt["data"]["product_ref"]})
            body = saved.get("body", {})
            if (saved.get("ref") != receipt["data"]["product_ref"]
                    or body.get("sku_id") != sku_id
                    or expected_images.get(sku_id) and body.get("image") != expected_images[sku_id]):
                raise ContractError("CATALOG_INTAKE_READBACK_FAILED", "商品保存回执无法核对，请查看已保存款式。", status=409)
            saved_products.append(saved)
        if import_entry(run):
            engine.store.update("run", scope.run_id, catalog_intake_result=build_catalog_intake_result(saved_products))
            await apply_document_import(engine, scope, products=saved_products, session=branch.get("document_session"),
                unresolved_session=bool((branch.get("catalog_validation_diagnostics") or {}).get("session")))
        check = await engine.finish(scope, "COMPLETED", f"已核对 {len(chosen)} 款商品"
                                    + ("并保存本场配置草稿。" if import_entry(run) else "。"))
        if not check.get("accepted"):
            raise ContractError("CATALOG_INTAKE_INCOMPLETE", "部分商品的保存结果仍待核对。", status=409)
        engine.store.update("run", scope.run_id, catalog_intake_result=build_catalog_intake_result(saved_products))
        progress(scope, "commit", f"已保存并核对 {len(saved_products)} 款商品", status="SUCCEEDED",
            completed_count=len(saved_products), total_count=len(chosen))
        return {"step": "complete"}

    async def document(state: CatalogState, runtime: Runtime[RunScope]):
        scope = runtime.context
        branch = engine.store.get("branch", state["branch_id"]) if state.get("branch_id") else {}
        run = await engine.assert_scope(scope)
        prior = previous_import(engine, run) if not branch else None
        products, session = [], branch.get("document_session")
        if prior and prior.get("reused_catalog"):
            session = prior["session"]
            products = [await engine.business_query(scope, "read_product", {"product_id": identity})
                        for identity in prior["product_ids"]]
            progress(scope, "extract", "已沿用同一份资料中核验过的商品与活动", status="SUCCEEDED")
        result = await apply_document_import(engine, scope, products=products, session=session,
            unresolved_session=bool((branch.get("catalog_validation_diagnostics") or {}).get("session")))
        check = await engine.finish(scope, "COMPLETED",
            f"已核对直播间「{result['room_name']}」的商品与本场配置草稿。")
        if not check.get("accepted"):
            raise ContractError("DOCUMENT_IMPORT_INCOMPLETE", "导入结果尚未通过回读核对。", status=409)
        return {"step": "complete"}

    graph = StateGraph(CatalogState, context_schema=RunScope)
    graph.add_node("extract", extract)
    graph.add_node("recover", recover)
    graph.add_node("session_repair", session_repair)
    graph.add_node("evidence_wait", evidence_wait)
    graph.add_node("currency", currency)
    graph.add_node("price", price)
    graph.add_node("prepare", prepare)
    graph.add_node("confirm", confirm)
    graph.add_node("commit", commit)
    graph.add_node("document", document)
    graph.add_edge(START, "extract")
    next_steps = {"currency": "currency", "price": "price", "prepare": "prepare", "document": "document"}
    graph.add_conditional_edges("extract", lambda s: s["step"], {**next_steps, "needs_recovery": "recover", "session_repair": "session_repair"})
    graph.add_conditional_edges("recover", lambda s: s["step"], {**next_steps, "evidence_wait": "evidence_wait", "session_repair": "session_repair",
                                                                            "needs_recovery": "recover"})
    graph.add_conditional_edges("session_repair", lambda s: s["step"], next_steps)
    graph.add_conditional_edges("evidence_wait", lambda s: s["step"], {**next_steps, "cancelled": END,
                                                                          "session_repair": "session_repair"})
    graph.add_conditional_edges("currency", lambda s: s["step"], next_steps)
    graph.add_conditional_edges("price", lambda s: s["step"], {"price": "price", "prepare": "prepare"})
    graph.add_edge("prepare", "confirm")
    graph.add_conditional_edges("confirm", lambda s: s["decision"],
        {"approved": "commit", "supplement": "confirm", "skip": END, "pending": END})
    graph.add_edge("commit", END)
    graph.add_edge("document", END)
    return graph.compile(checkpointer=engine.saver, name="catalog-intake")

