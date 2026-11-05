"""Isolated contract and approval coverage for fixed workflow recovery."""
import asyncio
from io import BytesIO
import json

from docx import Document
import httpx
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.models import AnswerRequest, RunScope
from shengbo_prelive.providers.deepseek import DeepSeekAgentModel
from shengbo_prelive.store import TERMINAL
from test_catalog_response_recovery import json_stream as _stream, submit_and_wait
from test_runtime_business import env, new_run


MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PRODUCTS = [
    ("FIXTURE-JAR-A", "试验密封罐小号", "39.90 元", 39.9),
    ("FIXTURE-JAR-B", "试验密封罐大号", "59.90 元", 59.9),
]
PLANNER_TOOL = "choose_fixed_step_recovery"


def _attachment():
    doc = Document()
    doc.add_paragraph("以上金额均为人民币。")
    table_rows = [
        ("款式", PRODUCTS[0][1], PRODUCTS[1][1]),
        ("SKU", PRODUCTS[0][0], PRODUCTS[1][0]),
        ("日常售价", PRODUCTS[0][2], PRODUCTS[1][2]),
    ]
    table = doc.add_table(rows=len(table_rows), cols=3)
    for row, values in zip(table.rows, table_rows):
        for cell, text in zip(row.cells, values):
            cell.text = text
    output = BytesIO()
    doc.save(output)
    return output.getvalue()


def _draft(products=PRODUCTS):
    return {"products": [{"name": name, "sku_id": sku, "price": price, "currency": "CNY"}
                         for sku, name, _, price in products], "questions": []}


@pytest.mark.parametrize("alternate_has_result", [True, False])
async def test_catalog_replans_only_after_empty_extraction_and_keeps_approval_gate(env, alternate_has_result):
    engine, service = env
    sends = []

    def respond(request):
        wire = json.loads(request.content)
        tools = wire.get("tools", [])
        if tools:
            assert [item["function"]["name"] for item in tools] == [PLANNER_TOOL]
            strategies = tools[0]["function"]["parameters"]["properties"]["strategy"]["enum"]
            assert set(strategies) == {"reread_full_source", "structured_source_layout"}
            sends.append("main-recovery")
            return _stream(len(sends), {"strategy": "structured_source_layout"}, tool=PLANNER_TOOL)
        inputs = json.loads(wire["messages"][-1]["content"])
        sends.append("alternate" if "source_layout" in inputs else "extract")
        reply = _draft() if alternate_has_result and sends[-1] == "alternate" else _draft([])
        return _stream(len(sends), reply)

    await engine.start()
    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(respond), key_reader=lambda: "fixture-token",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    run_id, current = await submit_and_wait(engine, _attachment(), filename="fixed-recovery.docx", mime=MIME)
    assert sends == ["extract", "main-recovery", "alternate"], current.get("error")
    assert not (await service.query("search_products", {}))["items"]
    assert not any(row["key"] == "save_product" for row in engine.store.list("operation"))
    run = engine.store.get("run", run_id)
    assert run["catalog_intake_enabled"] is True
    assert run["fixed_step_recovery"]["selected_by"] == "model"
    with engine.store.tx() as db:
        attempt_roles = [json.loads(row[0])["role"] for row in db.execute(
            "SELECT body FROM attempts WHERE run_id=? ORDER BY rowid", (run_id,))]
    assert attempt_roles == ["product_facts", "main", "product_facts"]
    if not alternate_has_result:
        assert current["error"]["code"] == "CATALOG_FACTS_INVALID"
        assert not any(question["kind"] == "catalog_intake_approval" for question in current["questions"])
        return

    question, = current["questions"]
    assert question["kind"] == "catalog_intake_approval"
    assert question["catalog_intake_binding"]["plan_hash"] == run["catalog_intake_plan"]["plan_hash"]
    assert [(item["sku_id"], item["fields"]["price"]) for item in question["preview"]["items"]] == [
        (sku, price) for sku, _, _, price in PRODUCTS]
    await engine.answer(question["id"], AnswerRequest(
        question_revision=question["revision"], answer="save_1", client_request_id="only-first-recovered-sku"))
    async with asyncio.timeout(8):
        while (current := engine.get_run(run_id))["status"] not in TERMINAL:
            await asyncio.sleep(.025)
    assert current["status"] == "COMPLETED", current.get("error")
    saved, = current["catalog_intake_result"]["products"]
    product = await service.query("read_product", {"ref": saved["product_ref"]})
    assert (product["body"]["sku_id"], product["body"]["price"]) == (PRODUCTS[0][0], PRODUCTS[0][3])
    assert len(sends) == 3


async def test_catalog_model_timeout_replans_one_alternate_read_without_sending_the_original_again(env, monkeypatch):
    from shengbo_prelive import catalog_extract

    engine, service = env
    calls, sends = [], []
    actual_extract = catalog_extract.extract_catalog_report

    async def time_out_once(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ContractError("MODEL_TIMEOUT", "读取超时", status=504)
        return await actual_extract(*args, **kwargs)

    def respond(request):
        wire = json.loads(request.content)
        if wire.get("tools"):
            assert [item["function"]["name"] for item in wire["tools"]] == [PLANNER_TOOL]
            sends.append("plan")
            return _stream(len(sends), {"strategy": "structured_source_layout"}, tool=PLANNER_TOOL)
        sends.append("alternate")
        inputs = json.loads(wire["messages"][-1]["content"])
        assert "source_layout" in inputs
        return _stream(len(sends), _draft())

    monkeypatch.setattr(catalog_extract, "extract_catalog_report", time_out_once)
    await engine.start()
    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(respond), key_reader=lambda: "fixture-token",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    run_id, current = await submit_and_wait(engine, _attachment(), filename="same-step-timeout.docx", mime=MIME)
    assert current["status"] == "WAITING_USER", current.get("error")
    assert len(calls) == 2 and calls[1]["alternate_layout"] is True
    assert sends == ["plan", "alternate"]
    assert not (await service.query("search_products", {}))["items"]
    events = engine.events(run_id, 0, 200)["events"]
    assert any(event["type"] == "workflow.progress" and event["data"]["status"] == "RECOVERING" for event in events)
    assert not any(event["type"] == "catalog_intake.fallback" for event in events)


async def test_completed_main_recovery_plan_replays_from_receipt_without_another_send(env):
    engine, _ = env
    run = await new_run(engine, "读取已授权的资料并核对失败位置")
    engine.store.update("run", run["id"], phase="execute", status="RUNNING")
    scope = RunScope(run["id"], "fixed-recovery-test", "execute")
    failure = {"workflow": "fixture_workflow", "step": "read", "code": "KNOWN_INVALID",
               "message": "首次读取失败", "known_no_commit": True, "verified_refs": [{"id": "target-1"}]}
    strategies = {"other_reader": "换到另一个已授权的读取入口"}
    sends = []

    def respond(request):
        sends.append(request)
        assert len(sends) <= 3
        return _stream(len(sends), {"strategy": "other_reader"}, tool=PLANNER_TOOL)

    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(respond), key_reader=lambda: "fixture-token",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    assert await engine.recover_fixed_step(scope, failure, strategies=strategies) == "other_reader"
    selected = engine.store.get("run", run["id"])["fixed_step_recovery"]
    interrupted = {**{key: value for key, value in selected.items() if key != "strategy"}, "status": "planning"}
    engine.store.update("run", run["id"], fixed_step_recovery=interrupted,
                        fixed_step_recoveries={selected["key"]: interrupted})
    # Simulate process recovery after the response was archived, before the
    # selected recovery strategy reached the run record/checkpoint.
    assert await engine.recover_fixed_step(scope, failure, strategies=strategies) == "other_reader"
    assert len(sends) == 1
    with pytest.raises(ContractError, match="FIXED_STEP_RECOVERY_EXHAUSTED"):
        await engine.recover_fixed_step(scope, {**failure, "code": "SECOND_ERROR"}, strategies=strategies)
    assert await engine.recover_fixed_step(scope, {**failure, "verified_refs": [{"id": "target-2"}]},
                                           strategies=strategies) == "other_reader"
    assert await engine.recover_fixed_step(scope, {**failure, "step": "verify"},
                                           strategies=strategies) == "other_reader"
    with pytest.raises(ContractError, match="FIXED_STEP_RECOVERY_EXHAUSTED"):
        await engine.recover_fixed_step(scope, {**failure, "step": "finalize"}, strategies=strategies)
    assert await engine.recover_fixed_step(scope, failure, strategies=strategies) == "other_reader"
    engine.store.update("run", run["id"], pending_unknown_operation="fixture-unknown")
    with pytest.raises(ContractError, match="FIXED_STEP_RECOVERY_DENIED"):
        await engine.recover_fixed_step(scope, failure, strategies=strategies)
    assert len(sends) == 3


@pytest.mark.parametrize(("initial_count", "alternate_usable"), [(2, True), (1, True), (1, False)])
async def test_catalog_severe_known_fact_omissions_replan_without_losing_usable_rows(env, initial_count, alternate_usable):
    engine, service = env
    labels = ("标称容量", "产品净重", "耐热温度", "密封圈材质", "颜色")
    values = (("800 毫升", "140 克", "95℃", "硅胶", "透明白"), ("1500 毫升", "180 克", "105℃", "橡胶", "烟灰色"))
    doc = Document()
    doc.add_paragraph("以上金额均为人民币。")
    table = doc.add_table(rows=1, cols=3 + len(labels))
    for cell, value in zip(table.rows[0].cells, ("款式", "SKU", "日常售价", *labels)):
        cell.text = value
    for (sku, name, amount, _), specifications in zip(PRODUCTS, values):
        for cell, value in zip(table.add_row().cells, (name, sku, amount, *specifications)):
            cell.text = value
    buffer = BytesIO()
    doc.save(buffer)
    sends = []

    def respond(request):
        wire = json.loads(request.content)
        if wire.get("tools"):
            assert [item["function"]["name"] for item in wire["tools"]] == [PLANNER_TOOL]
            sends.append("plan")
            return _stream(len(sends), {"strategy": "structured_source_layout"}, tool=PLANNER_TOOL)
        inputs = json.loads(wire["messages"][-1]["content"])
        alternate = "source_layout" in inputs
        sends.append("alternate" if alternate else "extract")
        if alternate and not alternate_usable:
            return _stream(len(sends), _draft([]))
        result = _draft()
        for index, row in enumerate(result["products"]):
            specs = [{"name": label, "value": value} for label, value in zip(labels, values[index])]
            row["specifications"] = specs if alternate else specs[:initial_count] if index == 0 else []
        return _stream(len(sends), result)

    await engine.start()
    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(respond), key_reader=lambda: "fixture-token",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    _, current = await submit_and_wait(engine, buffer.getvalue(), filename="omitted-variants.docx", mime=MIME)
    assert sends == (["extract"] if initial_count == 2 else ["extract", "plan", "alternate"]), current.get("error")
    question, = current["questions"]
    assert question["kind"] == "catalog_intake_approval", current.get("error")
    assert not (await service.query("search_products", {}))["items"]
    assert [(item["sku_id"], item["fields"]["price"]) for item in question["preview"]["items"]] == [
        (sku, price) for sku, _, _, price in PRODUCTS]
    specifications = [item["fields"].get("specifications", []) for item in question["preview"]["items"]]
    if initial_count == 1 and alternate_usable:
        assert [[spec["value"] for spec in rows] for rows in specifications] == [list(items) for items in values]
    else:
        assert [spec["value"] for spec in specifications[0]] == list(values[0][:initial_count])
        assert not specifications[1]
