"""Ensure a rejected session field does not cause accepted products to be extracted again."""
import asyncio
import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from docx import Document

from shengbo_prelive.catalog_extract import TOOL_NAME
from test_catalog_response_recovery import SOURCE_TEXT, report_json
from shengbo_prelive.catalog_intake import BASIC_CREATE_REQUEST
from shengbo_prelive.models import AnswerRequest, MessageRequest
from shengbo_prelive.store import TERMINAL
from test_document_import import _draft
from test_runtime_business import env


@pytest.mark.asyncio
@pytest.mark.parametrize("patch_valid", [True, False])
async def test_catalog_session_repairs_window_without_regenerating_products(env, patch_valid):
    engine, service = env
    await engine.start()
    heading = "直播活动"
    window = "2026 年 11 月 11 日 20:00 至 23:59，北京时间。"
    initial = {}

    class SessionModel:
        calls = []

        def bind_tools(self, tools, **kwargs):
            self.tool = tools[0]["function"]["name"]
            return self

        async def ainvoke(self, messages):
            inputs = json.loads(messages[-1].content)
            self.calls.append(self.tool)
            if self.tool == TOOL_NAME:
                source = next(row for row in inputs["source_material"] if window in row["text"])
                title = next(row for row in inputs["source_material"] if heading in row["text"])
                initial.update(location=source["location"], quote=window,
                               start="2026-11-11T10:00:00+08:00", end="2026-11-11T23:59:00+08:00")
                result = {**report_json(inputs), "session": {
                    "theme": {"location": title["location"], "quote": heading}, "window": initial}}
            else:
                assert self.tool == "repair_catalog_session"
                assert "existing_products" not in inputs and "products" not in inputs
                assert inputs["allowed_fields"] == ["window"]
                assert inputs["issues"][0]["path"] == ["window", "start"]
                result = {"session": {"window": {**initial, "start": "2026-11-11T20:00:00+08:00"}}}
                if not patch_valid:
                    result["session"]["theme"] = None
            return SimpleNamespace(tool_calls=[{"name": self.tool, "args": result}])

    engine.model = SessionModel()
    document = Document()
    for line in [*SOURCE_TEXT.splitlines(), heading, window]:
        document.add_paragraph(line)
    attachment = BytesIO()
    document.save(attachment)
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    material = engine.accept_material(conversation["id"], "source.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", attachment.getvalue())["material_ref"]
    receipt = await engine.submit_message(conversation["id"], MessageRequest(
        text=BASIC_CREATE_REQUEST, intent_mode="new_task", client_request_id=conversation["id"],
        material_refs=[material], view_context={"view": "products", "product_material_action": "create", "catalog_create_room": True}))
    run_id = receipt["run"]["id"]
    async with asyncio.timeout(8):
        while not ((current := engine.get_run(run_id))["questions"] or current["status"] in TERMINAL):
            await asyncio.sleep(.05)
        question, = current["questions"]
        assert question["kind"] == "catalog_intake_approval", current.get("error")
        await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
            answer="save_all", client_request_id=question["id"]))
        while (current := engine.get_run(run_id))["status"] not in TERMINAL:
            await asyncio.sleep(.05)
    assert current["status"] == "COMPLETED", current.get("error")
    result = current["document_import_result"]
    assert engine.model.calls == [TOOL_NAME, "repair_catalog_session"]
    products = (await service.query("search_products", {}))["items"]
    assert len(products) == 1 and products[0]["sku_id"] == "RECOVER-SKU-1"
    saved = await _draft(service, result)
    assert saved["theme"] == heading
    if patch_valid:
        assert saved["campaign_starts_at"] == "2026-11-11T20:00:00+08:00"
    else:
        assert not saved.get("campaign_starts_at")
        assert any("活动起止" in gap for gap in result["missing_fields"])
