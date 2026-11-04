"""The report entry fixes execution and commits reviewed points through Voice."""
import asyncio

import pytest

from shengbo_contracts.business import AnalysisReport
from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import AnswerRequest, MessageRequest
from shengbo_prelive.selling_point_extract import SellingPointsOutputError
from shengbo_prelive.store import TERMINAL
from test_runtime_business import Product, env, human_command


@pytest.mark.asyncio
@pytest.mark.parametrize(("warm", "count", "recover_stage", "blocked_stage", "blocked_code"), [
    (True, 1, None, None, None), (False, 1, None, None, None),
    (False, 2, None, None, None), (True, 1, None, "review", "SELLING_POINTS_UNSUPPORTED"),
    (False, 1, "research", None, None), (True, 1, "draft", None, None),
    (True, 1, "review", None, None), (True, 1, None, "draft", "SELLING_POINTS_STAGE_TIMEOUT"),
    (True, 1, None, "draft", "TIMEOUT_RECOVERY_SUCCEEDS"),
    (True, 1, None, "draft", "BUDGET_EXHAUSTED"),
    (False, 1, "research", "review", "SELLING_POINTS_OUTPUT_INVALID"),
    (True, 1, "review", "review", "SELLING_POINTS_OUTPUT_INVALID"),
    (True, 1, "review", "review", "RECOVERY_SUCCEEDS"),
    (True, 1, None, "review", "RESUME_AFTER_CONFIRM"),
])
async def test_report_writeback_has_only_required_stages(env, monkeypatch, warm, count, recover_stage,
                                                         blocked_stage, blocked_code):
    engine, service = env
    fields = Product(name="隔离测试折叠书架", price=59, sku_id="FAST-FIXTURE", specifications=[
        {"name": "类目", "value": "书架"}, {"name": "结构", "value": "可折叠"}]).model_dump()
    refs = [(await human_command(service, "save_product", {
        "fields": {**fields, "name": fields["name"] + str(index), "sku_id": fields["sku_id"] + str(index)},
        "schema_hash": service.schema()["schema_hash"], "patches": []}))["data"]["product_ref"] for index in range(count)]
    reports = {ref["id"]: AnalysisReport(summary="需要收纳并灵活调整使用空间", product_ids=[ref["id"]],
                                           dependencies=[ref]).model_dump(mode="json") for ref in refs}
    if warm:
        for ref in refs:
            report = reports[ref["id"]]
            await human_command(service, "accept_analysis_report", {"role": "audience_scenes", "report": report,
                "report_hash": canonical_hash(report), "product_refs": [ref]})
    observed = []
    failed_scopes = {}
    handoff = blocked_code in {"SELLING_POINTS_OUTPUT_INVALID", "RECOVERY_SUCCEEDS"}
    timed_out = blocked_code in {"SELLING_POINTS_STAGE_TIMEOUT", "TIMEOUT_RECOVERY_SUCCEEDS"}
    recoverable_stages = {recover_stage} - {None}
    if handoff or timed_out:
        recoverable_stages.add(blocked_stage)

    async def choose_recovery(scope, failure, *, strategies):
        observed.append("main_plan")
        assert failure["step"] == blocked_stage and failure["known_no_commit"] is True
        assert failure["verified_refs"] == refs
        assert "reread_product" in strategies
        return "reread_product"

    monkeypatch.setattr(engine, "recover_fixed_step", choose_recovery)

    def observe_model(stage, scope, recovery):
        observed.append(stage)
        if recovery and recovery.get("code") == "RESUME_AFTER_CONFIRM":
            assert stage == blocked_stage and "上一次核对" in recovery.get("user_source_hint", "")
            return
        if recovery and recovery.get("recovery_instruction"):
            assert handoff and stage == blocked_stage
            assert "重新核对" in recovery["recovery_instruction"]
            if blocked_code == "RECOVERY_SUCCEEDS":
                return
        elif recovery:
            assert stage in recoverable_stages
            assert scope.invocation_id != failed_scopes[stage].invocation_id
            assert scope.branch_task_id == failed_scopes[stage].branch_task_id
            expected_error = "SELLING_POINTS_STAGE_TIMEOUT" if timed_out and stage == blocked_stage else "SELLING_POINTS_OUTPUT_INVALID"
            assert recovery["code"] == expected_error
            if blocked_code == "TIMEOUT_RECOVERY_SUCCEEDS":
                assert not any(op["key"] == "save_product" and op["run_id"] == scope.run_id
                               for op in engine.store.list("operation"))
                return
        elif stage in recoverable_stages:
            failed_scopes[stage] = scope
            if timed_out and stage == blocked_stage:
                raise ContractError("SELLING_POINTS_STAGE_TIMEOUT", "该阶段未收到完整模型回复。", status=504)
            raise SellingPointsOutputError("SELLING_POINTS_OUTPUT_INVALID", "模型输出不符合结构。",
                                           status=422, details={"index": 0})
        if stage == blocked_stage:
            if handoff:
                raise SellingPointsOutputError("SELLING_POINTS_OUTPUT_INVALID", "结构仍不合法。", status=422)
            status = 504 if timed_out else 422
            raise ContractError(blocked_code, "现有资料不足以支持本次卖点。", status=status)

    def search(query, limit):
        observed.append("search")
        assert query.startswith("书架 ") and limit == 3
        return {"status": "error", "error": {"code": "public_search_unavailable"}}

    async def research(_engine, scope, *, product, product_ref, evidence_catalog, web_results, recovery=None):
        assert scope.role == "audience_scenes" and scope.branch_task_id
        assert product["sku_id"].startswith("FAST-FIXTURE")
        assert web_results["error"]["code"] == "public_search_unavailable"
        observe_model("research", scope, recovery)
        return reports[product_ref["id"]]

    async def draft(_engine, scope, *, product, evidence_catalog, research, recovery=None):
        assert scope.role == "main" and research["summary"] == "需要收纳并灵活调整使用空间"
        observe_model("draft", scope, recovery)
        evidence, = [key for key, value in evidence_catalog.items() if value["fact"] == "可折叠"]
        return [{"selling_point": "折叠收纳：需要时展开，不用时折起来。", "audience_need": "腾出桌面空间",
                 "use_scene": "家庭阅读后收纳", "limitations": "", "evidence": [{"evidence_id": evidence}]}]

    async def review(_engine, scope, *, product, evidence_catalog, research, items, recovery=None,
                     with_diagnostics=False):
        assert scope.role == "main" and items[0]["selling_point"].startswith("折叠收纳")
        assert with_diagnostics
        observe_model("review", scope, recovery)
        return {"items": [{**items[0], "selling_point": "用完可折叠收纳"}], "decisions": []}

    monkeypatch.setattr(engine.research, "search", search)
    monkeypatch.setattr("shengbo_prelive.selling_point_research_extract.extract_audience_research", research)
    monkeypatch.setattr("shengbo_prelive.selling_point_extract.extract_selling_points", draft)
    monkeypatch.setattr("shengbo_prelive.selling_point_extract.review_selling_points", review)
    await engine.start()
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    items = [{"target": {"object_ref": ref, "field_path": "/core_selling_points"}} for ref in refs]
    reply = await engine.submit_message(conversation["id"], MessageRequest(text="为选中商品生成并保存核心卖点",
        intent_mode="new_task", client_request_id="fast-writeback-test", observed_versions=refs,
        selection=items[0] if count == 1 else {"items": items},
        view_context={"view": "products", "selling_points_action": "generate_and_save"}))
    identity = reply["run"]["id"]
    async with asyncio.timeout(10):
        while (run := engine.get_run(identity))["status"] not in TERMINAL | {"WAITING_USER"}:
            await asyncio.sleep(.025)
    expected = ["draft", "review"] if warm else ["search"] + ["research", "draft", "review"] * count
    if blocked_stage and blocked_code != "TIMEOUT_RECOVERY_SUCCEEDS":
        expected = expected[:expected.index(blocked_stage) + 1]
    correction_order = [stage for stage in expected if stage in recoverable_stages]
    for stage in correction_order:
        expected.insert(expected.index(stage), stage)
    if handoff:
        expected.extend(["main_plan", blocked_stage])
    assert observed == expected
    corrections = engine.store.get("run", identity).get("selling_points_stage_corrections", {})
    assert set(corrections) == {scope.invocation_id for scope in failed_scopes.values()}
    recovery_events = [event for event in engine.events(identity)["events"] if event["type"] == "selling_points.recovery"]
    assert [event["data"]["stage"] for event in recovery_events] == [stage for stage in correction_order for _ in range(2)]
    for index, stage in enumerate(correction_order):
        started, ended = recovery_events[2 * index:2 * index + 2]
        expected_outcome = "FAILED" if blocked_stage == stage and blocked_code != "TIMEOUT_RECOVERY_SUCCEEDS" else "SUCCEEDED"
        assert [event["data"]["status"] for event in (started, ended)] == ["RECOVERING", expected_outcome]
        assert ended["parent_event_id"] == started["event_id"]
    if blocked_code == "RESUME_AFTER_CONFIRM":
        question, = run["questions"]
        assert run["status"] == "WAITING_USER" and not run.get("closure_ref")
        await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
            answer={"text": "沿用上一次核对的商品资料"}, client_request_id="resume-selling"))
        async with asyncio.timeout(10):
            while (run := engine.get_run(identity))["status"] not in TERMINAL:
                await asyncio.sleep(.025)
        assert observed == [*expected, "review"]
    saved_products = [await service.query("read_product", {"product_id": ref["id"]}) for ref in refs]
    ops = [row for row in engine.store.list("operation") if row["run_id"] == identity]
    if blocked_code and blocked_code not in {"RECOVERY_SUCCEEDS", "TIMEOUT_RECOVERY_SUCCEEDS", "RESUME_AFTER_CONFIRM"}:
        expected_code = "SELLING_POINTS_OUTPUT_INVALID" if handoff else blocked_code
        assert run["status"] == "WAITING_USER" and run["error"]["code"] == expected_code
        assert run["questions"][0]["kind"] == "runtime_recovery" and not run.get("closure_ref")
        assert saved_products[0]["ref"] == refs[0]
        assert [op["key"] for op in ops] == ([] if warm else ["accept_analysis_report"])
        return
    assert run["status"] == "COMPLETED", run.get("error")
    for saved, ref in zip(saved_products, refs):
        assert saved["body"]["core_selling_points"] == ["用完可折叠收纳"]
        assert saved["ref"]["revision"] == ref["revision"] + 1
    assert [op["key"] for op in ops] == (["save_product"] if warm else ["accept_analysis_report", "save_product"] * count)
