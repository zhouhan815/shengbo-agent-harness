"""Exact, receipt-backed catalog choices through isolated Agent/Voice services."""
from copy import deepcopy

from langgraph.errors import GraphInterrupt
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.catalog_intake_approval import make_catalog_intake_plan, resolve_catalog_intake_decision
from shengbo_prelive.models import AnswerRequest, RunScope
from shengbo_prelive.product_materials import save_accepted_product
from test_optional_workflows import authorize
from test_runtime_business import Product, env, new_run, stop_at_interrupt


async def prepare_case(engine):
    run = await new_run(engine, "按附件新增两个规格商品。只整理和保存基础资料，不做研究。")
    source = engine.materials.accept(run["conversation_id"], "catalog-approval.txt", "text/plain",
        "测试收纳袋，标准装一份29元，加量装一份39元。".encode())["material_ref"]
    run = await authorize(engine, run, ["import_blob", "accept_analysis_report", "save_product"])
    run = engine.store.update("run", run["id"], catalog_intake_enabled=True,
        input={**run["input"], "material_refs": [source]},
        goal={**run["goal"], "objective": "catalog_update", "catalog_delivery": "basic_facts_only",
              "required_analyses": ["product_facts"]})
    candidates = [Product(name=f"测试收纳袋 {size}", price=price, sku_id=f"TEST-BAG-{index}", currency="CNY").model_dump()
                  for index, (size, price) in enumerate([("标准装", 29), ("加量装", 39)], 1)]
    branch = "catalog-approval-worker"
    engine.store.put("branch", {"id": branch, "run_id": run["id"], "role": "product_facts", "status": "RUNNING",
        "task": {"role": "product_facts", "instruction": "整理附件中的两个真实规格", "product_ids": [],
                 "source_refs": [source], "input_refs": []}})
    engine.submit_report(RunScope(run["id"], "catalog-worker", "execute", "product_facts", branch),
        {"summary": "两种规格", "fields": {"products": candidates}, "source_refs": [source]})
    prepared = engine.store.get("branch", branch)
    plan = make_catalog_intake_plan(run, branch_task_id=branch, report_hash=prepared["report_hash"], candidates=candidates)
    engine.store.update("run", run["id"], catalog_intake_plan=plan)
    scope = RunScope(run["id"], "catalog-main", "execute")
    accepted = (await engine.accept_reports(scope, [branch], "catalog-accept"))["accepted_refs"][0]
    return run["id"], scope, candidates, branch, accepted, source


async def ask(engine, scope):
    with pytest.raises(GraphInterrupt):
        await engine.request_decision(scope, {"kind": "catalog_intake_approval", "prompt": "确认新商品"}, "catalog-confirm")
    return engine.get_run(scope.run_id)["questions"][0]


async def answer(engine, question, value, request_id):
    return await engine.answer(question["id"], AnswerRequest(
        question_revision=question["revision"], answer=value, client_request_id=request_id))


@pytest.mark.asyncio
async def test_native_and_helper_saves_require_exact_selected_approved_candidate(env, monkeypatch):
    engine, service = env
    monkeypatch.setattr("shengbo_prelive.runtime.interrupt", stop_at_interrupt)
    rid, scope, candidates, branch, accepted, source = await prepare_case(engine)
    choice = await ask(engine, scope)
    assert [option["value"] for option in choice["options"]] == ["save_all", "save_1", "save_2", "skip"]
    native = {"fields": candidates[0], "patches": [], "source_manifest_refs": [source],
              "schema_hash": service.schema()["schema_hash"]}
    with pytest.raises(ContractError, match="CATALOG_INTAKE_APPROVAL_REQUIRED"):
        await engine.business_command(scope, "save_product", native, "before-confirmation")
    with pytest.raises(ContractError, match="CATALOG_INTAKE_CHOICE_REQUIRED"):
        await answer(engine, choice, {"text": "好"}, "ambiguous-text")
    assert engine.store.get("question", choice["id"])["status"] == "OPEN"
    await answer(engine, choice, "save_1", "approve-first")
    resolved = resolve_catalog_intake_decision(engine, engine.store.get("run", rid))
    assert resolved["selected_sku_ids"] == [candidates[0]["sku_id"]]
    assert resolved["answer_event_ref"]
    with pytest.raises(ContractError, match="CATALOG_INTAKE_APPROVAL_REQUIRED"):
        await engine.business_command(scope, "save_product", {**native, "fields": {**candidates[0], "price": 999}}, "altered")
    with pytest.raises(ContractError, match="CATALOG_INTAKE_APPROVAL_REQUIRED"):
        await engine.business_command(scope, "save_product", {**native, "source_manifest_refs": []}, "missing-source")
    params = {"branch_task_id": branch, "accepted_report_ref": accepted}
    with pytest.raises(ContractError, match="FACTS_SAVE_FIELDS_INVALID"):
        await save_accepted_product(engine, scope, params, tool_call_id="missing-selection")
    with pytest.raises(ContractError, match="CATALOG_INTAKE_APPROVAL_REQUIRED"):
        await save_accepted_product(engine, scope, {**params, "candidate_sku_id": candidates[1]["sku_id"]}, tool_call_id="unselected")
    params["candidate_sku_id"] = candidates[0]["sku_id"]
    saved = await save_accepted_product(engine, scope, params, tool_call_id="save-selected")
    replay = await save_accepted_product(engine, scope, params, tool_call_id="save-selected")
    assert replay["data"]["product_ref"] == saved["data"]["product_ref"]
    with pytest.raises(ContractError, match="CATALOG_INTAKE_APPROVAL_REQUIRED"):
        await engine.business_command(scope, "save_product", native, "duplicate-write")
    result = resolve_catalog_intake_decision(engine, engine.store.get("run", rid))
    assert result["operations_by_sku"][candidates[0]["sku_id"]]["product_ref"] == saved["data"]["product_ref"]
    assert result["operations_by_sku"][candidates[0]["sku_id"]]["status"] == "committed"
    assert (await service.query("read_product", {"ref": saved["data"]["product_ref"]}))["body"]["price"] == 29


@pytest.mark.asyncio
async def test_revision_reasks_exact_original_choices_and_skip_never_authorizes(env, monkeypatch):
    engine, service = env
    monkeypatch.setattr("shengbo_prelive.runtime.interrupt", stop_at_interrupt)
    rid, scope, candidates, _, _, source = await prepare_case(engine)
    first = await ask(engine, scope)
    await answer(engine, first, {"choice": "revise", "text": "先补充包装数量"}, "supplement")
    supplied = resolve_catalog_intake_decision(engine, engine.store.get("run", rid))
    assert supplied["state"] == "supplement" and supplied["selected_sku_ids"] == []
    engine.store.update("run", rid, catalog_intake_confirmation_round=2, catalog_intake_supplement_note=supplied["text"])
    second = await ask(engine, scope)
    assert second["id"] != first["id"] and second["options"] == first["options"]
    assert "原方案尚未修改" in second["prompt"]
    await answer(engine, second, "skip", "skip-intake")
    assert resolve_catalog_intake_decision(engine, engine.store.get("run", rid))["state"] == "skip"
    with pytest.raises(ContractError, match="CATALOG_INTAKE_APPROVAL_REQUIRED"):
        await engine.business_command(scope, "save_product", {"fields": deepcopy(candidates[0]), "patches": [],
            "source_manifest_refs": [source], "schema_hash": service.schema()["schema_hash"]}, "after-skip")
    assert not any(op["key"] == "save_product" for op in engine.store.list("operation"))


@pytest.mark.asyncio
async def test_one_formal_report_covers_both_approved_skus_only_after_both_saves(env, monkeypatch):
    engine, _ = env
    monkeypatch.setattr("shengbo_prelive.runtime.interrupt", stop_at_interrupt)
    _, scope, candidates, branch, accepted, _ = await prepare_case(engine)
    await answer(engine, await ask(engine, scope), "save_all", "approve-all")

    for index, candidate in enumerate(candidates):
        sku_id = candidate["sku_id"]
        await save_accepted_product(engine, scope, {"branch_task_id": branch,
            "accepted_report_ref": accepted, "candidate_sku_id": sku_id}, tool_call_id=f"save-{sku_id}")
        result = await engine.finish(scope, "COMPLETED", "核对逐款保存回执")
        assert result["accepted"] is (index == len(candidates) - 1)
