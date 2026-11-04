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
from test_catalog_response_recovery import submit_and_wait
from test_runtime_business import env, new_run


MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PRODUCTS = [
    ("FIXTURE-JAR-A", "试验密封罐小号", "39.90 元", 39.9),
    ("FIXTURE-JAR-B", "试验密封罐大号", "59.90 元", 59.9),
]
PLANNER_TOOL = "choose_fixed_step_recovery"


def _attachment(extra_fact=None):
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
    if extra_fact:
        doc.add_paragraph(extra_fact + "。")
    output = BytesIO()
    doc.save(output)
    return output.getvalue()


def _draft(inputs, *, right_price):
    units = {item["text"]: item for item in inputs["source_units"]}
    products = []
    for i, (sku, name, amount, price) in enumerate(PRODUCTS):
        if not right_price and i == 0:
            amount, price = PRODUCTS[1][2:]
        values = {"name": name, "sku_id": sku, "price": price, "currency": "CNY"}
        evidence_values = {"name": name, "sku_id": sku, "price": amount, "currency": "以上金额均为人民币"}
        evidence = [{"location": units[quote]["location"], "fields": [field], "quote": quote}
                    for field, quote in evidence_values.items()]
        products.append({"fields": values, "evidence": evidence})
    labels = {"款式": "name", "SKU": "sku_id", "日常售价": "price"}
    coverage = {unit["id"]: f"{unit['sku_id']}|{labels[unit['column']]}" if "sku_id" in unit else "*|currency"
                for unit in units.values()}
    return {"products": products, "questions": [], "coverage": coverage}


def _stream(sequence, value, *, tool=None):
    if tool:
        delta = {"tool_calls": [{"index": 0, "id": f"fixture-tool-{sequence}", "type": "function",
                                 "function": {"name": tool, "arguments": json.dumps(value, ensure_ascii=False)}}]}
    else:
        delta = {"content": json.dumps(value, ensure_ascii=False)}
    chunks = [
        {"id": f"fixed-recovery-{sequence}", "choices": [{"delta": delta}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 40, "total_tokens": 70}},
    ]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
        text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n")


@pytest.mark.parametrize("successful_recovery", [True, False])
async def test_catalog_single_recovery_skips_planner_and_keeps_approval_gate(env, successful_recovery):
    engine, service = env
    sends = []
    extraction_inputs = []

    def respond(request):
        wire = json.loads(request.content)
        if any(item["function"]["name"] == PLANNER_TOOL for item in wire.get("tools", [])):
            sends.append("main-recovery")
            assert not successful_recovery
            return _stream(len(sends), {"strategy": "ask_user"}, tool=PLANNER_TOOL)
        sends.append("extract")
        assert len(sends) <= 4
        inputs = json.loads(wire["messages"][-1]["content"])
        extraction_inputs.append(inputs)
        return _stream(len(sends), _draft(inputs, right_price=successful_recovery and len(sends) == 4))

    await engine.start()
    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(respond), key_reader=lambda: "fixture-token",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    run_id, current = await submit_and_wait(engine, _attachment(), filename="fixed-recovery.docx", mime=MIME)
    assert sends == ["extract"] * 4, current.get("error")
    assert not (await service.query("search_products", {}))["items"]
    assert not any(row["key"] == "save_product" for row in engine.store.list("operation"))
    run = engine.store.get("run", run_id)
    assert run["catalog_intake_enabled"] is True
    assert run["fixed_step_recovery"]["selected_by"] == "program"
    events = engine.events(run_id, 0, 200)["events"]
    windows = [event["data"]["model_call_limit"] for event in events if event["type"] == "model.window_checked"]
    assert windows and max(windows) <= 5
    with engine.store.tx() as db:
        attempt_roles = [json.loads(row[0])["role"] for row in db.execute(
            "SELECT body FROM attempts WHERE run_id=? ORDER BY rowid", (run_id,))]
    assert attempt_roles == ["product_facts"] * 4
    if not successful_recovery:
        assert current["status"] == "WAITING_USER" and current["questions"][0]["kind"] == "runtime_recovery"
        assert not current.get("closure_ref")
        return
    checked = extraction_inputs[-1]["rechecked_source_evidence"]
    assert any(all(entry[2] in item["excerpt"] for entry in PRODUCTS) for item in checked)
    assert any(event["type"] == "material.read.completed" and event["data"].get("section")
               for event in events)
    async with asyncio.timeout(8):
        while (current := engine.get_run(run_id))["status"] not in TERMINAL | {"WAITING_USER"}:
            await asyncio.sleep(.025)
    assert current["status"] == "WAITING_USER", current.get("error")
    question, = current["questions"]
    assert question["kind"] == "catalog_intake_approval"
    assert question["catalog_intake_binding"]["plan_hash"] == run["catalog_intake_plan"]["plan_hash"]
    assert [(item["sku_id"], item["fields"]["price"]) for item in question["preview"]["items"]] == [
        (PRODUCTS[0][0], PRODUCTS[0][3]), (PRODUCTS[1][0], PRODUCTS[1][3])]
    await engine.answer(question["id"], AnswerRequest(
        question_revision=question["revision"], answer="save_1", client_request_id="only-first-recovered-sku"))
    async with asyncio.timeout(8):
        while (current := engine.get_run(run_id))["status"] not in TERMINAL:
            await asyncio.sleep(.025)
    assert current["status"] == "COMPLETED", current.get("error")
    saved, = current["catalog_intake_result"]["products"]
    product = await service.query("read_product", {"ref": saved["product_ref"]})
    assert product["body"]["sku_id"] == PRODUCTS[0][0]
    assert product["body"]["price"] == PRODUCTS[0][3]
    assert len(sends) == 4


async def test_catalog_model_timeout_retries_same_step_without_replanning(env, monkeypatch):
    from shengbo_prelive import catalog_extract

    engine, service = env
    calls = []
    actual_extract = catalog_extract.extract_catalog_report

    async def time_out_once(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ContractError("MODEL_TIMEOUT", "读取超时", status=504)
        return await actual_extract(*args, **kwargs)

    def respond(request):
        wire = json.loads(request.content)
        assert not any(item["function"]["name"] == PLANNER_TOOL for item in wire.get("tools", []))
        inputs = json.loads(wire["messages"][-1]["content"])
        return _stream(1, _draft(inputs, right_price=True))

    monkeypatch.setattr(catalog_extract, "extract_catalog_report", time_out_once)
    await engine.start()
    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(respond), key_reader=lambda: "fixture-token",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    run_id, current = await submit_and_wait(engine, _attachment(), filename="same-step-timeout.docx", mime=MIME)
    assert current["status"] == "WAITING_USER", current.get("error")
    assert len(calls) == 2 and calls[1]["alternate_layout"] is True
    assert not (await service.query("search_products", {}))["items"]
    run = engine.store.get("run", run_id)
    assert run["workflow_id"] == "catalog_intake" and run["catalog_timeout_retry"] is True
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


async def test_program_keeps_valid_candidates_and_repairs_only_the_failed_coverage_unit(env):
    engine, service = env
    extra_fact = "密封盖可用温水清洗"
    sends = []

    def respond(request):
        wire = json.loads(request.content)
        inputs = json.loads(wire["messages"][-1]["content"])
        assert not wire.get("tools")

        unit, = [item for item in inputs["source_units"] if item["text"] == extra_fact]
        repair = inputs["output_schema"]["required"] == ["coverage"]
        sends.append("coverage-only" if repair else "full-extraction")
        route = "+*" if "rechecked_source_evidence" in inputs else "OUT-OF-SCOPE|specifications"
        if repair:
            assert len(inputs["source_units"]) == 1
            assert inputs["output_schema"]["properties"]["coverage"]["required"] == [unit["id"]]
            reply = {"coverage": {unit["id"]: route}}
        else:
            reply = _draft(inputs, right_price=True)
            reply["coverage"][unit["id"]] = route
        return _stream(len(sends), reply)

    await engine.start()
    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(respond), key_reader=lambda: "fixture-token",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    _, current = await submit_and_wait(engine, _attachment(extra_fact), filename="fixed-coverage.docx", mime=MIME)
    assert sends == ["full-extraction", "coverage-only", "coverage-only"]
    question, = current["questions"]
    assert not (await service.query("search_products", {}))["items"]
    assert [item["fields"]["price"] for item in question["preview"]["items"]] == [entry[3] for entry in PRODUCTS]
    assert all(any(extra_fact in doc["content"] for doc in item["fields"]["knowledge_documents"])
               for item in question["preview"]["items"])
