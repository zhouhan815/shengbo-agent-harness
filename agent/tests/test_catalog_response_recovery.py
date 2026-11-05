"""Catalog response correction and bounded alternate reads in the real intake graph."""
import asyncio
import json
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest

from shengbo_prelive.api import create_app
from shengbo_prelive.catalog_extract import TOOL_NAME
from shengbo_prelive.catalog_intake import BASIC_CREATE_REQUEST
from shengbo_prelive.models import AnswerRequest, MessageRequest
from shengbo_prelive.providers.deepseek import DeepSeekAgentModel
from shengbo_prelive.store import TERMINAL
from test_runtime_business import env, human_command

FIELDS = {"name": "示例收纳架", "sku_id": "RECOVER-SKU-1", "price": 29.9, "currency": "CNY"}
RAW_TO_FIELD = {"示例收纳架": "name", "RECOVER-SKU-1": "sku_id", "29.9": "price", "人民币": "currency"}
SOURCE_TEXT = "示例收纳架。\nRECOVER-SKU-1。\n29.9。\n人民币。"
EXTRA = "收纳架只可放置在干燥桌面"


def report_json(inputs):
    units = [unit for unit in inputs["source_units"] if unit["text"] in RAW_TO_FIELD]
    return {
        "products": [{"fields": FIELDS, "evidence": [
            {"location": unit["location"], "fields": [RAW_TO_FIELD[unit["text"]]]} for unit in units]}],
        "questions": [],
        "coverage": {unit["id"]: "*|" + RAW_TO_FIELD[unit["text"]] for unit in units},
    }


def facts_json(**fields):
    return {"products": [{**FIELDS, **fields}], "questions": []}


def json_stream(index, value, *, tool=None):
    if tool:
        delta = {"tool_calls": [{"index": 0, "id": f"fixture-tool-{index}", "type": "function",
                  "function": {"name": tool, "arguments": json.dumps(value, ensure_ascii=False)}}]}
    else:
        delta = {"content": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}
    events = [{"id": f"fixture-{index}", "choices": [{"delta": delta}]},
              {"choices": [{"delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]},
              {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 40, "total_tokens": 70}}]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
        text="".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n")


async def submit_and_wait(engine, content, *, filename="source.txt", mime="text/plain", create_room=False):
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    data = content.encode() if isinstance(content, str) else content
    source = engine.accept_material(conversation["id"], filename, mime, data)["material_ref"]
    view = {"view": "products", "product_material_action": "create"}
    if create_room:
        view["catalog_create_room"] = True
    result = await engine.submit_message(conversation["id"], MessageRequest(
        text=BASIC_CREATE_REQUEST, client_request_id="catalog-response-recovery", intent_mode="new_task",
        material_refs=[source], view_context=view))
    identity = result["run"]["id"]
    async with asyncio.timeout(8):
        while True:
            current = engine.get_run(identity)
            if current["questions"] or current["status"] in TERMINAL:
                return identity, current
            await asyncio.sleep(.025)


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_catalog_resumes_initial_extract_after_provider_error_with_human_confirmation(env, unknown):
    engine, service = env
    requests = []

    def respond(request):
        inputs = json.loads(json.loads(request.content)["messages"][-1]["content"])
        requests.append(inputs)
        if len(requests) == 1:
            if unknown:
                raise httpx.ReadError("fixture connection interrupted", request=request)
            return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
        return json_stream(len(requests), facts_json())

    await engine.start()
    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(respond), key_reader=lambda: "fixture",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    run_id, waiting = await submit_and_wait(engine, SOURCE_TEXT)
    question, = waiting["questions"]
    expected = "model_transport_failed" if unknown else "model_provider_balance_insufficient"
    assert waiting["error"]["code"] == expected
    assert question["kind"] == "runtime_recovery" and len(requests) == 1
    if unknown:
        assert question["options"][0]["label"] == "确认重新发起模型请求"
        assert "额外模型费用" in question["prompt"]
    await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
        answer="retry", client_request_id="continue-after-recharge"))
    async with asyncio.timeout(8):
        while not ((waiting := engine.get_run(run_id))["questions"] or waiting["status"] in TERMINAL):
            await asyncio.sleep(.025)
    approval, = waiting["questions"]
    assert approval["kind"] == "catalog_intake_approval", waiting.get("error")
    assert len(requests) == 2 and requests[1]["source_units"] == requests[0]["source_units"]
    assert not (await service.query("search_products", {}))["items"]


@pytest.mark.asyncio
async def test_missing_currency_pauses_then_reuses_exact_draft_without_new_model_call(env):
    engine, service = env
    sends = []

    class Model:
        def bind_tools(self, tools, **kwargs):
            assert [item["function"]["name"] for item in tools] == [TOOL_NAME]
            return self

        async def ainvoke(self, messages):
            sends.append(1)
            result = facts_json()
            result["products"][0].pop("currency")
            return SimpleNamespace(tool_calls=[{"name": TOOL_NAME, "args": result}])

    await engine.start()
    engine.model = Model()
    rid, run = await submit_and_wait(engine, "示例收纳架。\nRECOVER-SKU-1。\n29.9。")
    assert run["status"] == "WAITING_USER", run.get("error")
    question, = run["questions"]
    assert question["kind"] == "catalog_intake_currency"
    assert not (await service.query("search_products", {}))["items"]
    await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
        answer={"text": "人民币"}, client_request_id="currency-supplied"))
    async with asyncio.timeout(8):
        while True:
            run = engine.get_run(rid)
            pending = [item for item in run["questions"] if item["status"] == "OPEN"]
            if pending or run["status"] in TERMINAL:
                break
            await asyncio.sleep(.025)
    approval, = pending
    assert approval["kind"] == "catalog_intake_approval"
    candidate, = approval["preview"]["items"]
    assert candidate["fields"]["currency"] == "CNY" and candidate["fields"]["price"] == 29.9
    await engine.answer(approval["id"], AnswerRequest(question_revision=approval["revision"],
        answer="save_all", client_request_id="currency-approval"))
    async with asyncio.timeout(8):
        while (run := engine.get_run(rid))["status"] not in TERMINAL:
            await asyncio.sleep(.025)
    assert run["status"] == "COMPLETED", run.get("error")
    assert sends == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_citation_or_coverage_gaps_do_not_trigger_a_second_request(env, legacy):
    engine, service = env
    sends = []

    class Model:
        def bind_tools(self, tools, **kwargs):
            assert [item["function"]["name"] for item in tools] == [TOOL_NAME]
            return self

        async def ainvoke(self, messages):
            sends.append(TOOL_NAME)
            result = facts_json()
            if legacy:
                result["products"] = [{"fields": FIELDS, "evidence": [{"location": "no-longer-required"}]}]
                result["coverage"] = {"old-invalid-identifier": "missing"}
            return SimpleNamespace(tool_calls=[{"name": TOOL_NAME, "args": result}])

    await engine.start()
    engine.model = Model()
    _, run = await submit_and_wait(engine, SOURCE_TEXT + "\n" + EXTRA + "。")
    question, = run["questions"]
    assert question["kind"] == "catalog_intake_approval"
    assert not (await service.query("search_products", {}))["items"]
    candidate, = question["preview"]["items"]
    assert all(candidate["fields"][key] == value for key, value in FIELDS.items())
    assert sends == [TOOL_NAME]


@pytest.mark.asyncio
async def test_mixed_document_saves_catalog_and_session_from_one_facts_only_request(env):
    engine, service = env
    theme = "桌面收纳"
    activity = "直播活动价：RECOVER-SKU-1 每件 24.90 元人民币。"
    sends = []

    def respond(request):
        wire = json.loads(request.content)
        assert not wire.get("tools")
        inputs = json.loads(wire["messages"][-1]["content"])
        schema = inputs["output_schema"]
        assert set(schema["required"]) == {"products", "questions", "session"}
        assert "evidence" not in schema["properties"]["products"]["items"]["properties"]
        assert "coverage" not in schema["properties"]
        session = {"theme": theme, "prices": [{"sku": FIELDS["sku_id"], "amount": 24.9}]}
        sends.append(inputs)
        return json_stream(len(sends), {**facts_json(), "session": session})

    await engine.start()
    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(respond), key_reader=lambda: "fixture",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    identity, current = await submit_and_wait(engine, f"{SOURCE_TEXT}\n本场主题：{theme}。\n{activity}", create_room=True)
    question, = current["questions"]
    assert question["kind"] == "catalog_intake_approval", current.get("error")
    assert not (await service.query("search_products", {}))["items"]
    await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
        answer="save_all", client_request_id="mixed-facts-save"))
    async with asyncio.timeout(8):
        while (current := engine.get_run(identity))["status"] not in TERMINAL:
            await asyncio.sleep(.025)
    assert current["status"] == "COMPLETED", current.get("error")
    saved, = current["catalog_intake_result"]["products"]
    product = (await service.query("read_product", {"ref": saved["product_ref"]}))["body"]
    result = current["document_import_result"]
    assert result["product_ids"] == [product["id"]]
    draft = (await service.query("read_context", {"ref": result["draft_ref"], "sections": ["draft"]}))["body"]["draft"]
    assert draft["theme"] == theme
    assert draft["commerce"]["product_overrides"][product["id"]]["price"] == 24.9
    assert product["price"] == 29.9 and product["promo_price"] is None
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_catalog_completion_reports_actual_field_names_after_user_approval(env):
    engine, service = env
    await engine.start()
    specification = "最大承重为8千克"

    class Model:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            draft = facts_json(specifications=[{"name": "最大承重", "value": "8千克"}])
            return SimpleNamespace(tool_calls=[{"name": TOOL_NAME, "args": draft}])

    engine.model = Model()
    identity, current = await submit_and_wait(engine, SOURCE_TEXT + "\n" + specification + "。")
    assert not current.get("catalog_intake_result")
    assert not (await service.query("search_products", {}))["items"]
    question, = current["questions"]
    await engine.answer(question["id"], AnswerRequest(
        question_revision=question["revision"], answer="save_all", client_request_id="test-catalog-save-report"))
    async with asyncio.timeout(8):
        while (current := engine.get_run(identity))["status"] not in TERMINAL:
            await asyncio.sleep(.025)
    assert current["status"] == "COMPLETED", current.get("error")
    report = current["catalog_intake_result"]
    saved, = report["products"]
    product = await service.query("read_product", {"ref": saved["product_ref"]})
    assert saved["product_id"] == product["ref"]["id"]
    assert saved["missing_required"] == ["核心卖点"]
    assert saved["configured_groups"] == [
        {"label": "入库必填项", "items": ["商品名称", "SKU", "币种", "日常售价"]},
        {"label": "规格参数", "items": ["最大承重"]},
    ]
    assert [item["id"] for item in saved["required_actions"]] == ["core_selling_points"]
    assert all(item["label"] != "核心卖点" for item in saved["recommendations"])
    assert "8千克" not in json.dumps(report, ensure_ascii=False)
    legacy = {key: value for key, value in saved.items() if key not in {"missing_required", "required_actions"}}
    legacy["recommendations"] = [*legacy["recommendations"], {"label": "核心卖点", "action": "generate_selling_points"}]
    engine.store.update("run", identity, catalog_intake_result={"products": [legacy]})
    assert engine.get_run(identity)["catalog_intake_result"] == report
    engine.store.update("run", identity, catalog_intake_result=None)
    assert engine.get_run(identity)["catalog_intake_result"] == report

    schema = service.schema()
    fields = {key: product["body"][key] for key in schema["schema"]["product"]["properties"]}
    point = "小空间也能清楚收纳"
    fields.update(core_selling_points=[point], selling_points=point)
    await human_command(service, "save_product", {
        "product_id": saved["product_id"], "expected_revision": saved["product_ref"]["revision"],
        "schema_hash": schema["schema_hash"], "fields": fields,
        "patches": [{"op": "set", "path": "/core_selling_points", "value": [point]}],
    })
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine, "fixture-report-token")),
                                 base_url="http://isolated") as client:
        response = await client.get(f"/api/assistant/v2/runs/{identity}",
                                    headers={"X-Shengbo-Service-Token": "fixture-report-token"})
    response.raise_for_status()
    updated, = response.json()["catalog_intake_result"]["products"]
    assert updated["missing_required"] == [] and updated["required_actions"] == []
    assert {"label": "讲解与问答", "items": ["核心卖点"]} in updated["configured_groups"]
    assert point not in json.dumps(response.json()["catalog_intake_result"], ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_result", ["normal", "blank", "malformed_json", "malformed_shape"])
async def test_deepseek_catalog_json_uses_one_request_except_for_unusable_extraction(env, first_result):
    engine, service = env
    await engine.start()
    sends = []
    planner_tool = "choose_fixed_step_recovery"

    def handle(request):
        wire = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer fixture-credential"
        tools = [item["function"]["name"] for item in wire.get("tools", [])]
        if tools:
            assert tools == [planner_tool]
            sends.append("plan")
            return json_stream(len(sends), {"strategy": "structured_source_layout"}, tool=planner_tool)

        assert wire["response_format"] == {"type": "json_object"}
        assert wire["thinking"] == {"type": "disabled"}
        inputs = json.loads(wire["messages"][-1]["content"])
        sends.append("alternate" if "source_layout" in inputs else "extract")
        schema = inputs["output_schema"]
        assert set(schema["required"]) == {"products", "questions"}
        assert "evidence" not in schema["properties"]["products"]["items"]["properties"]
        assert "coverage" not in schema["properties"]
        invalid = {"blank": "   ", "malformed_json": "not JSON", "malformed_shape": {"products": None}}
        reply = invalid.get(first_result, facts_json()) if len(sends) == 1 else facts_json()
        return json_stream(len(sends), reply)

    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(handle), key_reader=lambda: "fixture-credential",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    identity, current = await submit_and_wait(engine, SOURCE_TEXT + "\n" + EXTRA + "。")
    assert sends == (["extract"] if first_result == "normal" else ["extract", "plan", "alternate"]), current.get("error")
    question, = current["questions"]
    assert question["kind"] == "catalog_intake_approval"
    candidate, = question["preview"]["items"]
    assert candidate["sku_id"] == FIELDS["sku_id"]
    assert not (await service.query("search_products", {}))["items"]

    with engine.store.tx() as db:
        attempts = [json.loads(row[0]) for row in db.execute(
            "SELECT body FROM attempts WHERE run_id=? ORDER BY rowid", (identity,))]
    expected_roles = ["product_facts"] if first_result == "normal" else ["product_facts", "main", "product_facts"]
    assert [attempt["role"] for attempt in attempts] == expected_roles
    expected_first = "known_failed" if first_result in {"blank", "malformed_json"} else "succeeded"
    assert [attempt["state"] for attempt in attempts] == [expected_first] + ["succeeded"] * (len(sends) - 1)
    assert all(attempt["usage"]["total_tokens"] == 70 for attempt in attempts)

    events = engine.events(identity, 0, 100)["events"]
    request = next(event for event in events if event["type"] == "model.request")
    safe = engine.trace.read_payload(identity, request["data"]["payload_ref"])
    assert safe["payload"]["provider_options"]["response_format"] == {"type": "json_object"}
    assert "fixture-credential" not in json.dumps(safe)
    last_response = [event for event in events if event["type"] == "model.response"][-1]
    safe = engine.trace.read_payload(identity, last_response["data"]["payload_ref"])
    assert list(safe["output"]) == ["json"]


@pytest.mark.asyncio
async def test_catalog_corrects_unambiguous_sku_prices_in_one_request_then_requires_approval(env):
    from docx import Document

    engine, service = env
    document = Document()
    family = "秤语厨房电子秤"
    document.add_heading(f"{family} 商品资料", 0)
    document.add_paragraph("以上金额均为人民币。")
    entries = [("款式", "精细款", "大称量款"), ("SKU", "CY-KS1", "CY-KS5"),
               ("日常售价", "49.90 元", "69.90 元"), ("直播活动价", "39.90 元", "59.90 元")]
    table = document.add_table(rows=len(entries), cols=len(entries[0]))
    for row, record in zip(table.rows, entries):
        for cell, value in zip(row.cells, record):
            cell.text = value
    stream = BytesIO()
    document.save(stream)
    sent = []

    def handle(request):
        wire = json.loads(request.content)
        assert not wire.get("tools")
        sent.append(json.loads(wire["messages"][-1]["content"]))
        return json_stream(len(sent), {"products": [
            {"name": "精细款", "sku_id": "cy-ks1", "price": 69.9, "currency": "CNY"},
            {"name": "大称量款", "sku_id": "CY-KS5", "price": 59.9, "currency": "CNY"},
        ], "questions": []})

    await engine.start()
    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(handle), key_reader=lambda: "fixture-credential",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    identity, current = await submit_and_wait(engine, stream.getvalue(), filename="source.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert len(sent) == 1, current.get("error")
    assert not engine.store.get("run", identity).get("fixed_step_recovery")
    assert not (await service.query("search_products", {}))["items"]
    question, = current["questions"]
    assert question["kind"] == "catalog_intake_approval"
    expected = [("CY-KS1", 49.9, f"{family} 精细款"), ("CY-KS5", 69.9, f"{family} 大称量款")]
    assert [(item["sku_id"], item["fields"]["price"], item["fields"]["name"])
            for item in question["preview"]["items"]] == expected
    await engine.answer(question["id"], AnswerRequest(
        question_revision=question["revision"], answer="save_all", client_request_id="save-verified-catalog"))
    async with asyncio.timeout(8):
        while (current := engine.get_run(identity))["status"] not in TERMINAL:
            await asyncio.sleep(.025)
    assert current["status"] == "COMPLETED", current.get("error")
    products = [await service.query("read_product", {"ref": item["product_ref"]})
                for item in current["catalog_intake_result"]["products"]]
    assert [(item["body"]["sku_id"], item["body"]["price"], item["body"]["name"]) for item in products] == expected
    assert len(sent) == 1
