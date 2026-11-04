"""The explicit report submission can fill only the exact blank product revision."""
from types import SimpleNamespace

import pytest

from shengbo_contracts.business import AnalysisReport
from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import IntentAssessment, MessageRequest, RunScope
from shengbo_prelive.workflows import run_workflow
from test_runtime_business import env, human_command
from test_selling_points_workflow import candidates, create_tennis, execute, fields


async def prepare_report_run(engine, service, refs):
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    selections = [{"target": {"object_ref": ref, "field_path": "/core_selling_points"}} for ref in refs]
    text = "为选中的商品生成并保存核心卖点。只补空白核心卖点。"
    sent = await engine.submit_message(conversation["id"], MessageRequest(text=text, intent_mode="new_task",
        client_request_id="report-writeback-test", selection=selections[0] if len(refs) == 1 else {"items": selections},
        observed_versions=refs, view_context={"view": "products", "selling_points_action": "generate_and_save"}))
    rid = sent["run"]["id"]
    proposal = IntentAssessment(objective="catalog_update", summary="填入已选商品的空白核心卖点",
        target_product_ids=[ref["id"] for ref in refs], requested_product_paths=["/core_selling_points"],
        product_field_scope_quote="只补空白核心卖点", authorization_quote=text, required_analyses=["audience_scenes"])
    engine.save_assessment(RunScope(rid, "test-intent", "intent"), proposal)
    await engine._freeze_goal(engine.store.get("run", rid))
    return SimpleNamespace(engine=engine, service=service, run=engine.store.get("run", rid),
        scope=RunScope(rid, "report-writeback", "execute"), ref=refs[0])


async def ready_payload(case, ref):
    report = AnalysisReport(summary="已验收的顾客需求与使用场景", product_ids=[ref["id"]], dependencies=[ref])
    accepted = await human_command(case.service, "accept_analysis_report", {"role": "audience_scenes",
        "report": report.model_dump(mode="json"), "report_hash": canonical_hash(report), "product_refs": [ref]})
    research_ref = accepted["data"]["analysis_ref"]
    await case.engine.business_query(case.scope, "find_audience_research", {"ref": ref})
    product = await case.engine.business_query(case.scope, "read_product", {"ref": ref})
    inspected = await execute(case, "inspect", {"product_ref": ref})
    evidence = product["selling_point_evidence"]
    assert evidence == inspected["selling_point_evidence"]
    evidence_id, = [key for key, item in evidence.items() if item["product_field"] == "/specifications/0/value"]
    candidate = {**candidates()[0], "evidence": [{"evidence_id": evidence_id}]}
    return {"product_ref": ref, "items": [candidate], "research_refs": [research_ref]}


@pytest.mark.asyncio
async def test_report_writeback_is_immediate_exact_and_recovers_after_business_commit(env, monkeypatch):
    engine, service = env
    first = await create_tennis(service)
    original = await service.query("read_product", {"ref": first})
    second = (await human_command(service, "save_product", {"fields": {**fields(original),
        "name": "隔离测试网球回弹训练器 加量款", "sku_id": "WRITEBACK-SECOND"},
        "schema_hash": service.schema()["schema_hash"], "patches": []}))["data"]["product_ref"]
    case = await prepare_report_run(engine, service, [first, second])
    payload = await ready_payload(case, first)
    sibling_payload = await ready_payload(case, second)
    with pytest.raises(ContractError, match="SELLING_POINTS_RESEARCH_INVALID"):
        await execute(case, "propose", {**payload, "research_refs": []}, "no-research")
    with pytest.raises(ContractError, match="SELLING_POINTS_EVIDENCE_INVALID") as error:
        await execute(case, "propose", {**payload, "items": sibling_payload["items"]}, "crossed-evidence")
    assert error.value.details["evidence_index"] == 0
    with pytest.raises(ContractError, match="SELLING_POINTS_CONFIRMATION_REQUIRED"):
        await engine.business_command(case.scope, "save_product", {"product_id": first["id"],
            "expected_revision": first["revision"], "schema_hash": service.schema()["schema_hash"],
            "fields": {**fields(original), "core_selling_points": ["私自绕过"], "selling_points": "私自绕过"},
            "patches": [{"path": "/core_selling_points", "op": "set", "value": ["私自绕过"]}]}, "raw-save")

    real_update = engine.store.update
    crashed = False

    def crash_after_commit(kind, identity, **updates):
        nonlocal crashed
        if kind == "selling_point_proposal" and updates.get("status") == "SAVED" and not crashed:
            crashed = True
            raise RuntimeError("simulate loss of local proposal result after commit")
        return real_update(kind, identity, **updates)

    monkeypatch.setattr(engine.store, "update", crash_after_commit)
    async def propose(args, call_id):
        result = await run_workflow(engine, case.scope, "wf_selling_points", {
            "action": "propose", "parameters": args}, tool_call_id=call_id)
        return result["result"]

    with pytest.raises(RuntimeError, match="simulate loss"):
        await propose(payload, "auto-first")
    written = await service.query("read_product", {"product_id": first["id"]})
    assert written["ref"]["revision"] == first["revision"] + 1
    saved = await propose(payload, "auto-first")
    assert saved["status"] == "SAVED" and saved["product_ref"] == written["ref"]
    partial = await engine.finish(case.scope, "COMPLETED", "已保存首款卖点")
    assert not partial["accepted"]
    with pytest.raises(ContractError, match="SELLING_POINTS_BINDING_MISMATCH"):
        await propose({**payload, "direction": "changed replay"}, "auto-first")
    assert (await service.query("read_product", {"product_id": second["id"]}))["ref"] == second
    other = await propose(sibling_payload, "auto-second")
    assert other["status"] == "SAVED" and other["product_ref"]["id"] == second["id"]
    complete = await engine.finish(case.scope, "COMPLETED", "两款卖点已保存")
    assert complete["accepted"], complete
    assert not engine.store.list("question")
    ops = [op for op in engine.store.list("operation") if op["run_id"] == case.scope.run_id and op["key"] == "save_product"]
    assert len(ops) == 2 and all(op["status"] == "committed" for op in ops)
    assert all([patch["path"] for patch in op["args"]["patches"]] == ["/core_selling_points"] for op in ops)
    artifacts = engine.get_run(case.scope.run_id)["artifacts"]
    assert {row["destination"]["product_id"] for row in artifacts
            if row["destination"].get("field_path") == "/core_selling_points"} == {first["id"], second["id"]}
    evidence = [row for row in engine.store.list("query_evidence") if row["run_id"] == case.scope.run_id]
    assert not any(row["key"] == "inspect_change_impact" for row in evidence)
    assert {row["args"]["ref"]["revision"] for row in evidence if row["key"] == "find_audience_research"} == {first["revision"], second["revision"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("use_legacy", [False, True])
async def test_report_writeback_refuses_existing_points(env, use_legacy):
    engine, service = env
    blank = await create_tennis(service)
    original = await service.query("read_product", {"ref": blank})
    point = "已有的人工卖点"
    path = "/selling_points" if use_legacy else "/core_selling_points"
    value = point if use_legacy else [point]
    filled = (await human_command(service, "save_product", {"product_id": blank["id"],
        "expected_revision": blank["revision"], "schema_hash": service.schema()["schema_hash"],
        "fields": {**fields(original), "selling_points": point, "core_selling_points": [] if use_legacy else [point]},
        "patches": [{"path": path, "op": "set", "value": value}]}))["data"]["product_ref"]
    case = await prepare_report_run(engine, service, [filled])
    with pytest.raises(ContractError, match="SELLING_POINTS_PROPOSAL_CLOSED"):
        await execute(case, "propose", {"product_ref": filled, "items": candidates()}, "auto-replace")
    assert (await service.query("read_product", {"product_id": filled["id"]}))["ref"] == filled
