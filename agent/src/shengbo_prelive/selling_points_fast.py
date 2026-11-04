"""Checkpointed report writeback with one exact product at a time."""
from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from shengbo_contracts.common import ContractError, canonical_hash, same_artifact_ref

from .models import IntentAssessment, RunScope
from .selling_point_evidence import FACT_FIELDS, build_evidence_catalog
from .selling_point_extract import SellingPointsOutputError
from .selling_points import (
    _product, _require_empty_selling_points, _research, automatic_writeback_requested,
    execute, explicit_generation_refs,
)

_MAX_STAGE_CORRECTIONS = 3

PUBLIC_FAILURES = frozenset({
    "SELLING_POINTS_EVIDENCE_INVALID", "SELLING_POINTS_MODEL_INTERRUPTED", "SELLING_POINTS_OUTPUT_INVALID",
    "SELLING_POINTS_PRODUCT_STALE", "SELLING_POINTS_PRODUCT_UNAVAILABLE", "SELLING_POINTS_PROPOSAL_CLOSED",
    "SELLING_POINTS_RECEIPT_INVALID",
    "SELLING_POINTS_RESEARCH_INVALID", "SELLING_POINTS_RESEARCH_LOOKUP_LIMIT", "SELLING_POINTS_RESEARCH_NO_FACTS",
    "SELLING_POINTS_RESEARCH_OUTPUT_INVALID", "SELLING_POINTS_STAGE_TIMEOUT", "SELLING_POINTS_UNSUPPORTED",
    "SELLING_POINTS_RECOVERY_EXHAUSTED", "SELLING_POINTS_WRITEBACK_INCOMPLETE", "SELECTION_STALE",
})


def explicit_intent(run):
    if not automatic_writeback_requested(run):
        raise ContractError("SELLING_POINTS_REQUEST_INVALID", "缺少生成并保存卖点的精确授权。", status=422)
    quote = run["input"]["text"][:4000]
    return IntentAssessment(objective="catalog_update", summary="为已选商品补齐空白核心卖点",
        target_product_ids=[ref["id"] for ref in explicit_generation_refs(run)],
        requested_product_paths=["/core_selling_points"], product_field_scope_quote=quote,
        required_analyses=["audience_scenes"], proposed_effects=["accept_analysis_report", "save_product"],
        authorization_quote=quote)


class SellingPointsState(TypedDict, total=False):
    index: int
    step: str
    product_ref: dict
    product: dict
    research_ref: dict | None
    research: dict | None
    draft_items: list[dict]
    reviewed_items: list[dict]
    review_decisions: list[dict]


def _call_scope(scope, ref, stage, branch_id=None):
    identity = "selling_" + canonical_hash([scope.run_id, ref, stage])[7:39]
    return RunScope(scope.run_id, identity, scope.phase, "audience_scenes" if branch_id else "main", branch_id)


def _stage_corrections(run):
    stored = run.get("selling_points_stage_corrections")
    if stored is not None:
        return stored
    legacy = run.get("selling_points_recovery_used")
    return {legacy["invocation_id"]: legacy} if legacy else {}


def selling_points_correction_count(run):
    return len(_stage_corrections(run))


def _assert_unsent(engine, scope):
    with engine.store.tx() as db:
        sent = db.execute("""SELECT 1 FROM attempts WHERE run_id=?
            AND json_extract(body,'$.invocation_id')=? AND json_extract(body,'$.sent_at') IS NOT NULL LIMIT 1""",
            (scope.run_id, scope.invocation_id)).fetchone()
    if sent:
        raise ContractError("SELLING_POINTS_MODEL_INTERRUPTED",
            "上一阶段已发送模型请求但没有保存结果，请核对后重新发起。", status=409)


async def _find_research(engine, scope, run, ref):
    cursor = 0
    for _ in range(5):
        page = await engine.business_query(scope, "find_audience_research", {"ref": ref, "cursor": cursor, "limit": 100})
        for item in page.get("items", []):
            refs = item.get("product_refs") or []
            report = item.get("report") or {}
            if (item.get("compatibility") != "exact" or not item.get("context_match", True)
                    or not refs or any(not same_artifact_ref(value, ref) for value in refs)
                    or set(report.get("product_ids", [])) - {ref["id"]}):
                continue
            try:
                verified, = await _research(engine, scope, run, [item["research_ref"]], ref)
            except ContractError as error:
                if error.code not in {"SELLING_POINTS_RESEARCH_INVALID", "ANALYSIS_READ_DENIED"}:
                    raise
                continue
            return verified, report
        next_cursor = page.get("next_cursor")
        if type(next_cursor) is not int or next_cursor <= cursor:
            return None, None
        cursor = next_cursor
    raise ContractError("SELLING_POINTS_RESEARCH_LOOKUP_LIMIT", "历史研究较多，尚未核对完当前商品的适用研究。", status=422)


def _show_stage(engine, scope, ref, product, stage, action):
    title = f"正在为「{product['name']}」{action}"
    engine.store.update("run", scope.run_id, summary=title)
    engine._emit(scope.run_id, "selling_points.stage", {"title": title, "stage": stage,
        "product_ref": ref, "status": "RUNNING"}, scope)


async def _recover_stage(engine, scope, ref, stage, invoke, error, branch_id):
    failure = {"workflow": "selling_points", "step": stage, "branch_id": branch_id,
               "code": error.code, "message": error.message, "details": error.details,
               "known_no_commit": True, "verified_refs": [ref]}
    current = await engine.assert_scope(scope)
    binding = {"failure": failure, "ref": ref}
    if current.get("selling_points_handoff") != binding:
        engine.store.put("run", {**current, "selling_points_handoff": binding}, expected_revision=current["revision"])
    strategies = {
        "reread_product": "重新核对对应版本商品的原始资料，再完成本阶段",
        "isolate_verified_facts": "只使用商品可核实的事实，缩小本阶段的生成或复核范围",
    }
    strategy = await engine.recover_fixed_step(scope, failure, strategies=strategies)
    next_scope = _call_scope(scope, ref, stage + ":main_recovery", branch_id)
    _assert_unsent(engine, next_scope)
    options = {}
    if strategy == "reread_product":
        current = await engine.assert_scope(next_scope)
        _, body = await _product(engine, next_scope, current, ref)
        options["evidence_catalog"] = build_evidence_catalog(ref, body)
    elif stage == "research":
        options["web_results"] = {"status": "unavailable_for_recovery", "results": []}
    elif stage == "draft":
        options["research"] = {}
    guidance = ("根据重新核对的商品原始资料完成本阶段。" if strategy == "reread_product"
                else "缩小输出范围。只保留能用精确商品事实核实的结论；复核仍须逐条给出保留、修改或移除的判断。")
    feedback = {"code": error.code, "message": error.message, "details": error.details,
                "recovery_instruction": guidance}
    try:
        result = await invoke(engine, next_scope, recovery=feedback, **options)
    except Exception as retry_error:
        engine.trace.try_emit(scope.run_id, "fixed_step_recovery.failed",
            {"title": "卖点补救后仍未通过", "status": "FAILED", "workflow": "selling_points",
             "step": stage, "error_code": getattr(retry_error, "code", type(retry_error).__name__)},
            invocation_id=next_scope.invocation_id, branch_task_id=branch_id)
        raise
    engine.trace.try_emit(scope.run_id, "fixed_step_recovery.succeeded",
        {"title": "卖点步骤已通过补救复核", "status": "SUCCEEDED", "workflow": "selling_points", "step": stage},
        invocation_id=next_scope.invocation_id, branch_task_id=branch_id)
    return result


async def _run_model_stage(engine, scope, ref, product, stage, action, invoke, branch_id=None):
    original = _call_scope(scope, ref, stage, branch_id)
    run = await engine.assert_scope(original)
    incident = run.get("recovery_pending") or {}
    binding = {"incident_id": incident.get("id"), "product_ref": ref, "stage": stage}
    previous = run.get("selling_points_runtime_resume") or {}
    resumed_after_connect = (incident.get("selected_by") == "program"
                             and incident.get("error_code") == "model_transport_failed"
                             and not incident.get("outcome_unknown"))
    if (incident.get("state") == "resuming"
            and (incident.get("selected_by") == "human" or resumed_after_connect)
            and (previous.get("incident_id") != incident["id"] or previous == binding)):
        from .runtime_recovery import step_model_attempts, step_request_pending

        known_scopes = {_call_scope(scope, ref, name, branch_id).invocation_id
                        for name in (stage, stage + ":recovery", stage + ":main_recovery")}
        prior_attempts = [item for item in step_model_attempts(engine, original)
                          if item.get("invocation_id") in known_scopes]
        resumed = _call_scope(scope, ref, stage + ":runtime_resume:" + incident["id"], branch_id)
        if step_request_pending(prior_attempts, resumed, incident):
            raise ContractError("SELLING_POINTS_MODEL_INTERRUPTED", "上次卖点请求的结果仍待核对。", status=409)
        engine.store.update("run", scope.run_id, selling_points_runtime_resume=binding)
        _assert_unsent(engine, resumed)
        _show_stage(engine, resumed, ref, product, stage, action)
        answer = incident.get("answer")
        note = answer.get("text", "")[:600] if isinstance(answer, dict) else ""
        feedback = {"code": incident["error_code"], "message": incident["message"],
                    "recovery_instruction": "只处理当前商品和当前步骤，保留已核验的原商品事实。"}
        if note:
            feedback["user_source_hint"] = note
        return await invoke(engine, resumed, recovery=feedback)
    handoff = run.get("selling_points_handoff") or {}
    if handoff.get("ref") == ref and handoff["failure"]["step"] == stage:
        failure = handoff["failure"]
        return await _recover_stage(engine, scope, ref, stage, invoke,
            SellingPointsOutputError(failure["code"], failure["message"], status=422, details=failure["details"]), branch_id)
    reservation = _stage_corrections(run).get(original.invocation_id)
    if reservation is None:
        _assert_unsent(engine, original)
        try:
            return await invoke(engine, original)
        except ContractError as error:
            stage_timeout = error.code == "SELLING_POINTS_STAGE_TIMEOUT" and error.status == 504
            if not stage_timeout and not isinstance(error, SellingPointsOutputError):
                raise
            run = await engine.assert_scope(original)
            if run.get("pending_unknown_operation"):
                raise
            corrections = _stage_corrections(run)
            if len(corrections) >= _MAX_STAGE_CORRECTIONS:
                raise ContractError("SELLING_POINTS_RECOVERY_EXHAUSTED",
                    "卖点步骤的自动补救机会已用完，请查看未完成的商品。", status=409) from error
            reservation = {"invocation_id": original.invocation_id,
                           "feedback": {"code": error.code, "message": error.message, "details": error.details}}
            engine.store.put("run", {**run, "selling_points_recovery_used": reservation,
                                    "selling_points_stage_corrections": {**corrections, original.invocation_id: reservation}},
                             expected_revision=run["revision"])

    correction = _call_scope(scope, ref, stage + ":recovery", branch_id)
    _assert_unsent(engine, correction)
    _show_stage(engine, correction, ref, product, stage, action)
    event = {"stage": stage, "product_ref": ref}
    correction_title = ("模型回复超时，重新执行当前卖点步骤"
                        if reservation["feedback"]["code"] == "SELLING_POINTS_STAGE_TIMEOUT"
                        else "修正该阶段的结构或证据后重新核对")
    start = engine._emit(scope.run_id, "selling_points.recovery", {**event,
        "title": correction_title, "status": "RECOVERING",
        "error_code": reservation["feedback"]["code"]}, correction)
    try:
        result = await invoke(engine, correction, recovery=reservation["feedback"])
    except Exception as error:
        engine.trace.try_emit(scope.run_id, "selling_points.recovery", {**event,
            "title": "该阶段重新核对后仍有问题", "status": "FAILED", "error_code": getattr(error, "code", type(error).__name__)},
            correction.invocation_id, branch_id, start["event_id"])
        if isinstance(error, SellingPointsOutputError):
            return await _recover_stage(engine, scope, ref, stage, invoke, error, branch_id)
        raise
    engine.trace.try_emit(scope.run_id, "selling_points.recovery", {**event,
        "title": "该阶段已通过重新核对", "status": "SUCCEEDED"}, correction.invocation_id, branch_id, start["event_id"])
    return result


async def _public_research(engine, scope, product):
    category = next((str(item.get("value") or "").strip()[:100] for item in product.get("specifications", [])
                     if item.get("name") in {"类目", "商品类目", "品类", "商品品类"} and item.get("value")
                     and "specifications" not in product.get("hidden_optional_fields", [])), "")
    if not category:
        return {"status": "error", "error": {"code": "research_category_unavailable"}}
    query = f"{category} 使用场景 选购指南 顾客顾虑"
    identity = "research_" + canonical_hash([scope.run_id, query])[7:39]
    previous = engine.store.get("selling_points_public_research", identity, required=False)
    if previous:
        return previous["result"]
    try:
        engine.assert_network_budget(scope)
    except ContractError as error:
        if error.code != "RESEARCH_BUDGET":
            raise
        result = {"status": "error", "error": {"code": error.code}}
    else:
        try:
            async with asyncio.timeout(18):
                result = await asyncio.to_thread(engine.research.search, query, 3)
        except TimeoutError:
            result = {"status": "error", "error": {"code": "public_search_timeout"}}
    engine.store.put("selling_points_public_research", {"id": identity, "run_id": scope.run_id, "result": result})
    engine._emit(scope.run_id, "selling_points.public_research", {"title": "查询公开选购资料", "status": result["status"],
        "result_count": len(result.get("results", [])), "error_code": (result.get("error") or {}).get("code")}, scope)
    return result


def build_selling_points_graph(engine):
    async def prepare(state: SellingPointsState, runtime: Runtime[RunScope]):
        scope = runtime.context
        run = await engine.assert_scope(scope)
        refs = explicit_generation_refs(run)
        if not automatic_writeback_requested(run) or not refs:
            raise ContractError("SELLING_POINTS_TARGET_DENIED", "当前任务未获得精确卖点保存授权。", status=403)
        index = state.get("index", 0)
        if index == len(refs):
            return {"step": "finish"}
        if not run.get("task_assessment"):
            engine.save_assessment(scope, {"summary": "逐款复用或补充顾客研究、生成核对并保存核心卖点",
                "capabilities": ["audience_scenes", "wf_selling_points"],
                "steps": ["查找顾客研究", "生成和复核卖点", "保存并核对回执"]})
        ref, body = await _product(engine, scope, run, refs[index])
        _require_empty_selling_points(body)
        ref = {key: ref[key] for key in ("kind", "id", "revision", "content_hash")}
        # The fixed stages need evidence and product identity, not stored images.
        fields = FACT_FIELDS | {"name", "sku_id", "hidden_optional_fields"}
        product = {key: body[key] for key in fields if key in body}
        if not build_evidence_catalog(ref, product):
            raise ContractError("SELLING_POINTS_RESEARCH_NO_FACTS", "商品缺少可支持卖点的事实。", status=422)
        research_ref, report = await _find_research(engine, scope, run, ref)
        engine._emit(scope.run_id, "selling_points.research_selected", {"title": "核对顾客研究", "product_ref": ref,
            "status": "reused" if research_ref else "missing", "research_ref": research_ref}, scope)
        return {"index": index, "product_ref": ref, "product": product, "research_ref": research_ref,
                "research": report, "step": "draft" if research_ref else "research"}

    async def research(state: SellingPointsState, runtime: Runtime[RunScope]):
        from .selling_point_research_extract import extract_audience_research

        scope, ref, product = runtime.context, state["product_ref"], state["product"]
        _show_stage(engine, scope, ref, product, "research", "补充顾客研究")
        task = {"role": "audience_scenes", "instruction": "分析当前款的顾客、使用场景、需求及购买顾虑。",
                "product_ids": [ref["id"]], "input_refs": [ref]}
        batch = await engine.dispatch(scope, [task], "为缺少有效研究的已选商品补充顾客研究",
                                      "selling-points-research:" + ref["id"], inline_selling_points=True)
        branch_id, = batch["branch_task_ids"]
        branch = engine.store.get("branch", branch_id)
        recovery = (await engine.assert_scope(scope)).get("recovery_pending") or {}
        if branch["status"] == "FAILED" and recovery.get("state") == "resuming" and recovery.get("selected_by") == "human":
            branch = engine.store.update("branch", branch_id, status="RUNNING", error=None)
        if branch["status"] not in {"PREPARED", "ACCEPTED"}:
            worker = _call_scope(scope, ref, "research", branch_id)
            try:
                web_results = await _public_research(engine, worker, product)
                report = await _run_model_stage(engine, scope, ref, product, "research", "重新核对顾客研究",
                    partial(extract_audience_research, product=product, product_ref=ref,
                            evidence_catalog=build_evidence_catalog(ref, product), web_results=web_results), branch_id)
                await engine.assert_scope(worker)
                engine.submit_report(worker, report)
            except asyncio.CancelledError:
                engine.store.update("branch", branch_id, status="CANCELLED")
                raise
            except Exception as error:
                engine.store.update("branch", branch_id, status="FAILED",
                                    error={"code": getattr(error, "code", type(error).__name__)})
                raise
            branch = engine.store.get("branch", branch_id)
        if branch["status"] != "ACCEPTED":
            run = await engine.assert_scope(scope)
            await _product(engine, scope, run, ref)
        accepted = await engine.accept_reports(scope, [branch_id], "selling-points-accept:" + ref["id"])
        research_ref, = accepted["accepted_refs"]
        return {"research_ref": research_ref, "research": branch["report"]}

    async def draft(state: SellingPointsState, runtime: Runtime[RunScope]):
        from .selling_point_extract import extract_selling_points

        ref, product = state["product_ref"], state["product"]
        scope = runtime.context
        _show_stage(engine, _call_scope(scope, ref, "draft"), ref, product, "draft", "生成核心卖点")
        items = await _run_model_stage(engine, scope, ref, product, "draft", "重新生成核心卖点",
            partial(extract_selling_points, product=product, evidence_catalog=build_evidence_catalog(ref, product),
                    research=state["research"]))
        return {"draft_items": items}

    async def review(state: SellingPointsState, runtime: Runtime[RunScope]):
        from .selling_point_extract import review_selling_points

        ref, product = state["product_ref"], state["product"]
        scope = runtime.context
        _show_stage(engine, _call_scope(scope, ref, "review"), ref, product, "review", "核对卖点依据和购买理由")
        reviewed = await _run_model_stage(engine, scope, ref, product, "review", "重新核对卖点",
            partial(review_selling_points, product=product, evidence_catalog=build_evidence_catalog(ref, product),
                    research=state["research"], items=state["draft_items"], with_diagnostics=True))
        return {"reviewed_items": reviewed["items"], "review_decisions": reviewed["decisions"]}

    async def commit(state: SellingPointsState, runtime: Runtime[RunScope]):
        scope, ref = runtime.context, state["product_ref"]
        _show_stage(engine, scope, ref, state["product"], "commit", "保存核心卖点")
        args = {"action": "propose", "parameters": {"product_ref": ref,
            "items": state["reviewed_items"], "research_refs": [state["research_ref"]]}}
        receipt = await execute(engine, scope, args, "selling-points-save:" + ref["id"])
        saved_ref = receipt.get("product_ref") or {}
        if receipt.get("status") != "SAVED" or saved_ref.get("id") != ref["id"]:
            raise ContractError("SELLING_POINTS_RECEIPT_INVALID", "核心卖点保存结果无法核对。", status=409)
        saved = await engine.business_query(scope, "read_product", {"ref": saved_ref})
        expected = [item["selling_point"].strip() for item in state["reviewed_items"]]
        if (not same_artifact_ref(saved.get("ref"), saved_ref)
                or saved.get("body", {}).get("core_selling_points") != expected
                or saved.get("body", {}).get("selling_points") != "\n".join(expected)):
            raise ContractError("SELLING_POINTS_RECEIPT_INVALID", "商品已提交，但保存后的核心卖点还无法核对。", status=409)
        body = saved["body"]
        checked = {"product_ref": saved_ref, "name": body["name"], "items": body["core_selling_points"],
                   "review": {"candidate_count": len(state["draft_items"]), "saved_count": len(expected),
                              "rejected_count": len(state["draft_items"]) - len(expected)}}
        if state.get("review_decisions"):
            checked["review"]["decisions"] = state["review_decisions"]
        run = await engine.assert_scope(scope)
        products = {item["product_ref"]["id"]: item
                    for item in (run.get("selling_points_result") or {}).get("products", [])}
        if products.get(ref["id"]) != checked:
            products[ref["id"]] = checked
            engine.store.put("run", {**run, "selling_points_result": {"products": list(products.values())}},
                             expected_revision=run["revision"])
            engine._emit(scope.run_id, "selling_points.readback", {"title": f"已从商品库核对 {len(expected)} 条核心卖点",
                "status": "SUCCEEDED", "product_ref": saved_ref, **checked["review"]}, scope)
        return {"index": state["index"] + 1, "draft_items": [], "reviewed_items": [], "review_decisions": []}

    async def finish(state: SellingPointsState, runtime: Runtime[RunScope]):
        result = await engine.finish(runtime.context, "COMPLETED", f"已保存并核对 {state['index']} 款商品的核心卖点。")
        if not result.get("accepted"):
            raise ContractError("SELLING_POINTS_WRITEBACK_INCOMPLETE", "有商品的核心卖点保存结果仍待核对。", status=409)
        return {"step": "complete"}

    graph = StateGraph(SellingPointsState, context_schema=RunScope)
    graph.add_node("prepare", prepare)
    graph.add_node("build_research", research)
    graph.add_node("draft", draft)
    graph.add_node("review", review)
    graph.add_node("commit", commit)
    graph.add_node("finish", finish)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", lambda state: state["step"],
                                {"research": "build_research", "draft": "draft", "finish": "finish"})
    graph.add_edge("build_research", "draft")
    graph.add_edge("draft", "review")
    graph.add_edge("review", "commit")
    graph.add_edge("commit", "prepare")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=engine.saver, name="selling-points-fast")
