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
        if isinstance(args, dict) and ("products" in args or "session" in args):
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
        engine.submit_report(worker, report)
        return "prepare"

    def next_extraction_step(worker, report):
        if report.get("document_session_only"):
            engine.store.update("branch", worker.branch_task_id, status="CANCELLED",
                                summary="资料只包含本场配置，无商品需要入库。")
            return "document"
        if report.get("conflicts") or not report.get("fields", {}).get("products"):
            from .catalog_extract import CatalogResponseRejection
            raise CatalogResponseRejection("CATALOG_RECOVERY_UNRESOLVED",
                "未得到可保存的商品或本场配置。", status=422,
                details={"major_extraction_failure": True})
        return accept_extraction(worker, report)

    async def extract(state: CatalogState, runtime: Runtime[RunScope]):
        from .catalog_extract import CatalogResponseRejection, extract_catalog_report, validate_catalog_submission
        from .runtime_recovery import step_model_attempts, step_request_pending

        scope = runtime.context
        run = await engine.assert_scope(scope)
        progress(scope, "extract", "正在读取商品资料")
        if import_entry(run) and previous_import(engine, run):
            return {"step": "document"}
        if not run.get("task_assessment"):
            engine.save_assessment(scope, {"summary": "整理资料、确认候选、逐款入库",
                "capabilities": ["product_facts"], "steps": ["读取附件", "整理事实", "确认并保存"]})
        sources = run["input"]["material_refs"]
        # Keep the persisted dispatch identity stable across recovery.
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
        prepared = None
        try:
            async with asyncio.timeout(110):
                prepared = await prepared_source(worker, sources)
                if prepared is None:
                    raise CatalogResponseRejection("CATALOG_SOURCE_LAYOUT_UNSUPPORTED",
                        "当前读取方式没有读出可整理的商品资料。", status=422,
                        details={"major_extraction_failure": True})
                if any(item["kind"] == "image" and not item["text"].strip() for item in prepared["locations"]):
                    progress(scope, "extract", "照片没有文字，正在结合正文和原图继续整理")
                attempts = step_model_attempts(engine, worker)
                archived, arguments = next(_archived_catalog_submissions(engine, worker, attempts,
                    _source_fingerprint(prepared), allow_legacy=not branch.get("catalog_source_reprepared")),
                    (None, None))
                adopted = archived["id"] if archived else None
                if step_request_pending(attempts, worker, incident, adopted=adopted):
                    raise ContractError("CATALOG_EXTRACTION_INTERRUPTED",
                        "上一笔提取请求仍未确认结果，已保留本次资料。", status=409)
                if archived:
                    engine.store.update("branch", branch_id, catalog_adopted_attempt_id=adopted)
                    engine._emit(scope.run_id, "catalog_intake.recovery", {
                        "title": "已沿用原任务已收到的提取结果", "status": "RECOVERING",
                        "method": "reuse_archived_response", "attempt_id": adopted}, worker)
                    report = await validate_catalog_submission(engine, worker, run, prepared, arguments)
                else:
                    report = await extract_catalog_report(engine, worker, prepared, user_hint=_resume_hint(incident),
                                                         timeout_seconds=104)
                return {"branch_id": branch_id, "step": next_extraction_step(worker, report)}
        except (TimeoutError, ContractError) as error:
            code = getattr(error, "code", "CATALOG_EXTRACTION_TIMEOUT")
            details = getattr(error, "details", {})
            severe = details.get("major_extraction_failure") is True or code in {
                "CATALOG_EXTRACTION_TIMEOUT", "MODEL_TIMEOUT", "model_tool_call_invalid",
                "model_empty_completed_response", "model_json_output_invalid",
            }
            if not severe:
                raise
            if isinstance(error, CatalogResponseRejection):
                record_validation(worker, error, strategy="extract", public=False)
            return {"branch_id": branch_id, "step": "needs_recovery", "recovery_failure": {
                "workflow": "catalog_intake", "step": "extract", "branch_id": branch_id,
                "code": code, "message": getattr(error, "message", "读取或提取商品资料超时。"),
                "status": getattr(error, "status", 408), "details": details, "known_no_commit": True,
                "evidence_hash": _source_fingerprint(prepared) if prepared else None}}
        finally:
            engine._emit(scope.run_id, "catalog_intake.extraction", {"title": "整理商品资料",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3)}, scope)

    async def prepared_source(worker, sources, *, full_source=False):
        from .catalog_extract import prepare_catalog_extraction

        branch = engine.store.get("branch", worker.branch_task_id)
        snapshot = branch.get("catalog_prepared_source")
        if not full_source and snapshot and snapshot.get("sources") == sources:
            schema = await engine.business_query(worker, "get_business_schema", {})
            if schema["schema_hash"] == snapshot["schema_hash"]:
                return snapshot
        if full_source:
            prepared = await prepare_catalog_extraction(engine, worker, sources, full_source=True)
        else:
            prepared = await prepare_catalog_extraction(engine, worker, sources)
        if prepared:
            changed = bool(snapshot and not _same_source(prepared, _source_fingerprint(snapshot)))
            engine.store.update("branch", worker.branch_task_id, catalog_prepared_source=prepared,
                catalog_source_reprepared=changed or bool(branch.get("catalog_source_reprepared")))
        return prepared

    async def recover(state: CatalogState, runtime: Runtime[RunScope]):
        from .catalog_extract import CatalogResponseRejection, extract_catalog_report, validate_catalog_submission
        from .runtime_recovery import step_model_attempts, step_request_pending

        scope = runtime.context
        run = await engine.assert_scope(scope)
        branch_id = state["branch_id"]
        branch = engine.store.get("branch", branch_id)
        if branch["status"] in {"PREPARED", "ACCEPTED"}:
            return {"step": "prepare"}
        if branch.get("catalog_intake_price_draft"):
            return {"step": "price"}
        if branch.get("catalog_intake_currency_draft"):
            return {"step": "currency"}

        failure = state["recovery_failure"]
        sources = run["input"]["material_refs"]
        incident = _active_runtime_resume(run)
        invocation_id = "invoke_catalog_recovery_" + canonical_hash([scope.run_id, branch_id, failure["step"]])[7:]
        worker = RunScope(scope.run_id, invocation_id, "execute", "product_facts", branch_id)
        attempts = step_model_attempts(engine, worker)
        snapshot = branch.get("catalog_prepared_source")
        original_attempts = [item for item in attempts if item.get("invocation_id") != invocation_id]
        original_archive = next(_archived_catalog_submissions(engine, worker, original_attempts,
            _source_fingerprint(snapshot), allow_legacy=not branch.get("catalog_source_reprepared")),
            (None, None)) if snapshot else (None, None)

        if not (failure["details"].get("major_extraction_failure") is True or failure["code"] in {
                "CATALOG_EXTRACTION_TIMEOUT", "MODEL_TIMEOUT", "model_tool_call_invalid",
                "model_empty_completed_response", "model_json_output_invalid"}):
            # Older graph checkpoints may be waiting on a retired citation or coverage gate.
            return await reuse_legacy_result(state, runtime)

        strategies = {"reread_full_source": "重新打开完整原件，用完整正文和原始顺序提取，绕开首次的读取视图。"}
        if snapshot:
            strategies["structured_source_layout"] = "沿用同一原件，改用带行列和段落顺序的表格结构提取。"
        strategy = await engine.recover_fixed_step(worker, failure, strategies=strategies)
        progress(scope, "extract", "主 Agent 已选择另一种读取方式，正在重新提取", status="RECOVERING")
        try:
            async with asyncio.timeout(110):
                prepared = await prepared_source(worker, sources, full_source=strategy == "reread_full_source")
                if prepared is None:
                    raise CatalogResponseRejection("CATALOG_SOURCE_LAYOUT_UNSUPPORTED",
                        "换一种方式读取后，仍未能读出商品资料。", status=422)
                attempts = step_model_attempts(engine, worker)
                retried = [item for item in attempts if item.get("invocation_id") == invocation_id]
                archived, arguments = next(_archived_catalog_submissions(engine, worker, retried,
                    _source_fingerprint(prepared), allow_legacy=False), (None, None))
                if step_request_pending(attempts, worker, incident, adopted=archived["id"] if archived else None):
                    raise ContractError("CATALOG_EXTRACTION_INTERRUPTED",
                        "换一种方式读取的请求仍未确认结果，已保留本次资料。", status=409)
                if archived:
                    report = await validate_catalog_submission(engine, worker, run, prepared, arguments, allow_partial=True)
                else:
                    report = await extract_catalog_report(engine, worker, prepared,
                        alternate_layout=strategy == "structured_source_layout", allow_partial=True, timeout_seconds=104)
                step = next_extraction_step(worker, report)
        except (TimeoutError, ContractError) as error:
            if (original_archive[0] is None or not (isinstance(error, (TimeoutError, CatalogResponseRejection))
                    or error.code in {"CATALOG_EXTRACTION_TIMEOUT", "MODEL_TIMEOUT"})):
                raise
            # A failed alternate read must not erase usable facts in the original response.
            report = await validate_catalog_submission(engine, worker, run, snapshot, original_archive[1], allow_partial=True)
            step = next_extraction_step(worker, report)
        engine._emit(scope.run_id, "fixed_step_recovery.completed", {"workflow": "catalog_intake",
            "step": "extract", "strategy": strategy, "status": "SUCCEEDED", "title": "已完成替代读取，继续导入"}, worker)
        return {"step": step}

    async def reuse_legacy_result(state: CatalogState, runtime: Runtime[RunScope]):
        from .catalog_extract import validate_catalog_submission
        from .runtime_recovery import step_model_attempts

        scope = runtime.context
        run = await engine.assert_scope(scope)
        branch_id = state["branch_id"]
        branch = engine.store.get("branch", branch_id)
        if branch["status"] in {"PREPARED", "ACCEPTED"}:
            return {"step": "prepare"}
        if branch.get("catalog_intake_price_draft"):
            return {"step": "price"}
        if branch.get("catalog_intake_currency_draft"):
            return {"step": "currency"}
        worker = RunScope(scope.run_id, scope.invocation_id, "execute", "product_facts", branch_id)
        prepared = await prepared_source(worker, run["input"]["material_refs"])
        if prepared:
            archive = next(_archived_catalog_submissions(engine, worker, step_model_attempts(engine, worker),
                _source_fingerprint(prepared), allow_legacy=not branch.get("catalog_source_reprepared")), None)
            if archive:
                report = await validate_catalog_submission(engine, worker, run, prepared, archive[1], allow_partial=True)
                engine.store.update("branch", branch_id, catalog_adopted_attempt_id=archive[0]["id"])
                return {"step": next_extraction_step(worker, report)}
        raise ContractError("CATALOG_EXTRACTION_INTERRUPTED", "没有找到本次导入的可用回复，已保留原资料。", status=409)

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
        engine.submit_report(worker, report)
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
    graph.add_node("session_repair", reuse_legacy_result)
    graph.add_node("evidence_wait", reuse_legacy_result)
    graph.add_node("currency", currency)
    graph.add_node("price", price)
    graph.add_node("prepare", prepare)
    graph.add_node("confirm", confirm)
    graph.add_node("commit", commit)
    graph.add_node("document", document)
    graph.add_edge(START, "extract")
    next_steps = {"currency": "currency", "price": "price", "prepare": "prepare", "document": "document"}
    graph.add_conditional_edges("extract", lambda s: s["step"], {**next_steps, "needs_recovery": "recover"})
    graph.add_conditional_edges("recover", lambda s: s["step"], next_steps)
    graph.add_conditional_edges("session_repair", lambda s: s["step"], next_steps)
    graph.add_conditional_edges("evidence_wait", lambda s: s["step"], next_steps)
    graph.add_conditional_edges("currency", lambda s: s["step"], next_steps)
    graph.add_conditional_edges("price", lambda s: s["step"], {"price": "price", "prepare": "prepare"})
    graph.add_edge("prepare", "confirm")
    graph.add_conditional_edges("confirm", lambda s: s["decision"],
        {"approved": "commit", "supplement": "confirm", "skip": END, "pending": END})
    graph.add_edge("commit", END)
    graph.add_edge("document", END)
    return graph.compile(checkpointer=engine.saver, name="catalog-intake")

