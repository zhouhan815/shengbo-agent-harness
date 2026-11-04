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
from shengbo_prelive.providers.chatgpt import ModelProviderError
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


async def submit_and_wait(engine, content, *, filename="source.txt", mime="text/plain"):
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    data = content.encode() if isinstance(content, str) else content
    source = engine.accept_material(conversation["id"], filename, mime, data)["material_ref"]
    result = await engine.submit_message(conversation["id"], MessageRequest(
        text=BASIC_CREATE_REQUEST, client_request_id="catalog-response-recovery", intent_mode="new_task",
        material_refs=[source], view_context={"view": "products", "product_material_action": "create"}))
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
        content = json.dumps(report_json(inputs), ensure_ascii=False)
        events = [{"id": "recharged", "choices": [{"delta": {"content": content}}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            text="".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n")

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
            result = report_json(json.loads(messages[-1].content))
            result["products"][0]["fields"] = {key: value for key, value in FIELDS.items() if key != "currency"}
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
@pytest.mark.parametrize("needs_hint", [False, True])
async def test_evidence_recovery_only_patches_failed_links_and_can_resume_after_hint(env, needs_hint):
    from shengbo_prelive.catalog_extract import EVIDENCE_REPAIR_TOOL

    engine, service = env
    sends = []

    class Model:
        def bind_tools(self, tools, **kwargs):
            self.tool, = [item["function"]["name"] for item in tools]
            assert self.tool in {TOOL_NAME, EVIDENCE_REPAIR_TOOL}
            return self

        async def ainvoke(self, messages):
            inputs = json.loads(messages[-1].content)
            sends.append(self.tool)
            if self.tool == TOOL_NAME:
                result = report_json(inputs)
                row = result["products"][0]
                row["evidence"] = [link for link in row["evidence"] if "name" not in link["fields"]]
            else:
                scope, = inputs["candidates_needing_evidence"].values()
                assert scope["fields"] == {"name": FIELDS["name"]}
                assert "source_material" not in inputs
                if needs_hint and "user_location_hint" not in inputs:
                    result = {"evidence": {}}
                else:
                    location = next(key for key, excerpts in scope["originals"].items()
                                    if any(FIELDS["name"] in excerpt for excerpt in excerpts))
                    result = {"evidence": {"0": [{"location": location, "quote": FIELDS["name"], "fields": ["name"]}]}}
            return SimpleNamespace(tool_calls=[{"name": self.tool, "args": result}])

    await engine.start()
    engine.model = Model()
    rid, run = await submit_and_wait(engine, SOURCE_TEXT)
    question, = run["questions"]
    if needs_hint:
        assert question["kind"] == "catalog_evidence_clarification"
        assert question["allow_text"] is True
        assert not (await service.query("search_products", {}))["items"]
        await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
            answer={"text": "请核对原文件第一行"}, client_request_id="evidence-location-hint"))
        async with asyncio.timeout(8):
            while True:
                run = engine.get_run(rid)
                pending = [item for item in run["questions"] if item["status"] == "OPEN"]
                if pending or run["status"] in TERMINAL:
                    break
                await asyncio.sleep(.025)
        question, = pending
    assert question["kind"] == "catalog_intake_approval"
    candidate, = question["preview"]["items"]
    assert all(candidate["fields"][key] == value for key, value in FIELDS.items())
    assert sends == [TOOL_NAME, *([EVIDENCE_REPAIR_TOOL] * (3 if needs_hint else 1))]


@pytest.mark.asyncio
async def test_catalog_completion_reports_actual_field_names_after_user_approval(env):
    engine, service = env
    await engine.start()
    specification = "最大承重为8千克"

    class Model:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            inputs = json.loads(messages[-1].content)
            draft = report_json(inputs)
            unit, = [unit for unit in inputs["source_units"] if unit["text"] == specification]
            candidate, = draft["products"]
            candidate["fields"] = {**candidate["fields"], "specifications": [{"name": "最大承重", "value": "8千克"}]}
            candidate["evidence"].append({"location": unit["location"], "fields": ["specifications"]})
            draft["coverage"][unit["id"]] = "*|specifications"
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
@pytest.mark.parametrize(("responses", "expected_calls", "expected_error"), [
    (("valid",), 1, None),
    (("malformed_call", "valid"), 2, None),
    (("missing_keys", "valid"), 2, None),
    (("malformed_call", "missing_keys", "valid"), 3, None),
    (("unknown_send",), 1, "model_tool_call_invalid"),
])
async def test_standard_catalog_bounds_response_correction_and_recovers_known_exhaustion(env, responses, expected_calls, expected_error):
    engine, service = env
    await engine.start()
    calls = []

    class Model:
        def bind_tools(self, tools, **kwargs):
            self.tool, = (item["function"]["name"] for item in tools)
            assert self.tool == TOOL_NAME
            return self

        async def ainvoke(self, messages):
            inputs = json.loads(messages[-1].content)
            outcome = responses[len(calls)]
            calls.append((inputs, messages[0].content))
            assert self.tool == TOOL_NAME
            if outcome in {"malformed_call", "unknown_send"}:
                raise ModelProviderError("model_tool_call_invalid", outcome_unknown=outcome == "unknown_send")
            args = {"products": []} if outcome == "missing_keys" else report_json(inputs)
            return SimpleNamespace(tool_calls=[{"name": TOOL_NAME, "args": args}])

    engine.model = Model()
    _, current = await submit_and_wait(engine, SOURCE_TEXT)
    assert len(calls) == expected_calls
    assert not (await service.query("search_products", {}))["items"]
    if expected_error:
        assert current["error"]["code"] == expected_error
        assert current["status"] == "WAITING_USER"
        question, = current["questions"]
        assert question["kind"] == "runtime_recovery" and question["allow_text"]
        assert expected_error not in question["prompt"] and TOOL_NAME not in question["prompt"]
    else:
        question, = current["questions"]
        assert question["kind"] == "catalog_intake_approval"
        candidate, = question["preview"]["items"]
        assert candidate["sku_id"] == FIELDS["sku_id"]
    if expected_calls >= 2:
        inputs, prompt = calls[1]
        feedback = inputs["previous_validation_feedback"]
        if responses[0] == "missing_keys":
            assert feedback["details"]["missing_keys"] == ["coverage", "questions"]
        else:
            assert feedback["code"] == "model_tool_call_invalid"
        assert "顶层字段必须且只能" in prompt
    if expected_calls == 3:
        assert calls[-1][0]["rechecked_source_evidence"]


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["normal", "blank", "mapping"])
async def test_deepseek_catalog_json_wire_preserves_ledger_and_coverage(env, recovery):
    engine, service = env
    await engine.start()
    sent = []

    def handle(request):
        wire = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer fixture-credential"
        assert wire["response_format"] == {"type": "json_object"}
        assert wire["thinking"] == {"type": "disabled"}
        assert "tools" not in wire and "tool_choice" not in wire
        assert "JSON" in wire["messages"][0]["content"]
        inputs = json.loads(wire["messages"][-1]["content"])
        sent.append(inputs)
        schema = inputs["output_schema"]
        if recovery == "blank" and len(sent) == 1:
            content = "   "
        elif schema["required"] == ["coverage"]:
            assert recovery == "mapping" and len(sent) == 2
            unit, = inputs["source_units"]
            assert unit["text"] == EXTRA
            content = json.dumps({"coverage": {unit["id"]: "+*"}}, ensure_ascii=False)
        else:
            assert set(schema["required"]) == {"products", "questions", "coverage"}
            content = json.dumps(report_json(inputs), ensure_ascii=False)
        mid = len(content) // 2
        chunks = [{"id": f"fixture-{len(sent)}", "choices": [{"delta": {"content": fragment}}]}
                  for fragment in (content[:mid], content[mid:])]
        chunks.extend([
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 40, "total_tokens": 70}},
        ])
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n")

    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(handle), key_reader=lambda: "fixture-credential",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    identity, current = await submit_and_wait(engine, SOURCE_TEXT + ("\n" + EXTRA + "。" if recovery == "mapping" else ""))
    question, = current["questions"]
    candidate, = question["preview"]["items"]
    assert candidate["sku_id"] == FIELDS["sku_id"]
    assert not (await service.query("search_products", {}))["items"]
    if recovery == "mapping":
        assert candidate["fields"]["knowledge_documents"][0]["content"] == EXTRA
    with engine.store.tx() as db:
        attempts = [json.loads(row[0]) for row in db.execute("SELECT body FROM attempts WHERE run_id=? ORDER BY rowid", (identity,))]
    expected = {"normal": ["succeeded"], "blank": ["known_failed", "succeeded"], "mapping": ["succeeded", "succeeded"]}
    assert [attempt["state"] for attempt in attempts] == expected[recovery]
    assert len(sent) == len(attempts) and all(attempt["usage"]["total_tokens"] == 70 for attempt in attempts)
    events = engine.events(identity, 0, 100)["events"]
    request = next(event for event in events if event["type"] == "model.request")
    safe = engine.trace.read_payload(identity, request["data"]["payload_ref"])
    assert safe["payload"]["provider_options"]["response_format"] == {"type": "json_object"}
    assert "fixture-credential" not in json.dumps(safe)
    response = next(event for event in events if event["type"] == "model.response")
    safe = engine.trace.read_payload(identity, response["data"]["payload_ref"])
    assert list(safe["output"]) == ["json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("successful_recovery", [True, False], ids=["writes-after-approval", "resumes-after-user-hint"])
async def test_catalog_changes_layout_after_two_known_sku_price_mismatches(env, successful_recovery):
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
        for cell, text in zip(row.cells, record):
            cell.text = text
    stream = BytesIO()
    document.save(stream)

    sent = []
    confirmed_resume = False

    def sse_response(sequence, delta, reason):
        chunks = [{"id": f"layout-{sequence}", "choices": [{"delta": delta}]},
                  {"choices": [{"delta": {}, "finish_reason": reason}]},
                  {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 60, "total_tokens": 100}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n")

    def handle(request):
        wire = json.loads(request.content)
        inputs = json.loads(wire["messages"][-1]["content"])
        assert not wire.get("tools")
        sent.append(inputs)
        by_text = {unit["text"]: unit for unit in inputs["source_units"]}
        products = []
        for sku, name, daily, price in (("CY-KS1", "精细款", "49.90 元", 49.9),
                                         ("CY-KS5", "大称量款", "69.90 元", 69.9)):
            if sku == "CY-KS1" and (len(sent) < 3 or (not successful_recovery and not confirmed_resume)):
                daily, price = "69.90 元", 69.9
            references = {"name": name, "sku_id": sku, "price": daily, "currency": "以上金额均为人民币"}
            products.append({"fields": {"name": name, "sku_id": sku, "price": price, "currency": "CNY"},
                             "evidence": [{"location": by_text[quote]["location"], "quote": quote, "fields": [field]}
                                          for field, quote in references.items()]})
        fields = {"款式": "name", "SKU": "sku_id", "日常售价": "price"}
        coverage = {unit["id"]: (f"{unit['sku_id']}|{fields[unit['column']]}" if "sku_id" in unit else "*|currency")
                    for unit in by_text.values()}
        content = json.dumps({"products": products, "questions": [], "coverage": coverage}, ensure_ascii=False)
        return sse_response(len(sent), {"content": content}, "stop")

    await engine.start()
    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(handle), key_reader=lambda: "fixture-credential",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    identity, current = await submit_and_wait(engine, stream.getvalue(), filename="source.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert len(sent) == (3 if successful_recovery else 4), current.get("error")
    assert "source_material" in sent[0] and "source_material" in sent[1]
    assert "source_material" not in sent[2]
    title, heading, grouped_table = sent[2]["source_layout"][0]["blocks"]
    assert title["lines"] == [f"{family} 商品资料"]
    assert heading["lines"] == ["以上金额均为人民币。"]
    assert [[cell["text"] for cell in row["cells"]] for row in grouped_table["rows"]] == [list(row) for row in entries]
    assert [cell["grid_column"] for cell in grouped_table["rows"][2]["cells"]] == [1, 2, 3]
    assert sent[2]["source_units"] == sent[0]["source_units"]
    assert engine.store.get("run", identity)["catalog_recovery_active"] is True
    assert not (await service.query("search_products", {}))["items"]
    events = engine.events(identity, 0, 100)["events"]
    limits = [event["data"]["model_call_limit"] for event in events if event["type"] == "model.window_checked"]
    assert limits == ([2, 2, 3] if successful_recovery else [2, 2, 3, 4])
    recovery_events = [event["data"] for event in events if event["type"] == "catalog_intake.recovery"]
    assert [data["state"] for data in recovery_events] == ["started", "succeeded" if successful_recovery else "handed_off"]
    assert recovery_events[0]["trigger_codes"] == ["CATALOG_FACTS_INVALID", "CATALOG_FACTS_INVALID"]
    if not successful_recovery:
        selected, = [event["data"] for event in events if event["type"] == "fixed_step_recovery.selected"]
        assert selected["selected_by"] == "program" and selected["strategy"] == "reread_source"
        assert sent[3]["previous_validation_feedback"]["details"]["fields"] == ["price"]
        assert sent[3]["rechecked_source_evidence"]
        assert any(event["type"] == "fixed_step_recovery.failed" for event in events)
        assert current["error"]["code"] == "CATALOG_FACTS_INVALID"
        assert current["status"] == "WAITING_USER"
        question, = current["questions"]
        assert question["kind"] == "runtime_recovery" and question["allow_text"]
        assert "CATALOG_FACTS_INVALID" not in question["prompt"] and TOOL_NAME not in question["prompt"]
        assert not any(operation["key"] == "save_product" for operation in engine.store.list("operation"))
        confirmed_resume = True
        hint = "精细款对应原表左侧，日常售价见第三行。"
        await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
            answer={"text": hint}, client_request_id="resume-with-catalog-location"))
        async with asyncio.timeout(8):
            while not ((current := engine.get_run(identity))["questions"] or current["status"] in TERMINAL):
                await asyncio.sleep(.025)
        assert len(sent) == 5, current.get("error")
        assert sent[-1]["user_location_hint"] == hint and sent[-1]["rechecked_source_evidence"]
        assert not (await service.query("search_products", {}))["items"]
    question, = current["questions"]
    assert question["kind"] == "catalog_intake_approval"
    assert [(item["sku_id"], item["fields"]["price"]) for item in question["preview"]["items"]] == [
        ("CY-KS1", 49.9), ("CY-KS5", 69.9)]
    assert [item["fields"]["name"] for item in question["preview"]["items"]] == [f"{family} 精细款", f"{family} 大称量款"]
    await engine.answer(question["id"], AnswerRequest(
        question_revision=question["revision"], answer="save_all", client_request_id="save-recovered-catalog"))
    async with asyncio.timeout(8):
        while (current := engine.get_run(identity))["status"] not in TERMINAL:
            await asyncio.sleep(.025)
    assert current["status"] == "COMPLETED", current.get("error")
    products = [await service.query("read_product", {"ref": item["product_ref"]})
                for item in current["catalog_intake_result"]["products"]]
    assert [(item["body"]["sku_id"], item["body"]["price"]) for item in products] == [("CY-KS1", 49.9), ("CY-KS5", 69.9)]
    assert [item["body"]["name"] for item in products] == [f"{family} 精细款", f"{family} 大称量款"]
