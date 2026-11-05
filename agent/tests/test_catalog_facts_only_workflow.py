"""The fixed import workflow routes around missing citations and recovers real extraction failures."""
import json

import httpx
import pytest

from shengbo_prelive.providers.deepseek import DeepSeekAgentModel
from test_catalog_response_recovery import submit_and_wait
from test_fixed_step_recovery import PLANNER_TOOL, _stream
from test_runtime_business import env


SOURCE = """# 整理架商品资料

| 款式 | SKU | 日常售价 |
| --- | --- | --- |
| 收纳整理架短款 | FIX-FACT-A | 39.9 元 |
| 收纳整理架长款 | FIX-FACT-B | 59.9 元 |

商品币种为人民币（CNY）。
问：怎么清洁？答：湿布擦拭即可。
"""


@pytest.mark.parametrize("severe", [False, True])
async def test_catalog_only_replans_when_whole_extract_fails(env, severe):
    engine, _ = env
    sends = []

    def respond(request):
        wire = json.loads(request.content)
        if any(tool["function"]["name"] == PLANNER_TOOL for tool in wire.get("tools", [])):
            sends.append("main_recovery")
            return _stream(len(sends), {"strategy": "structured_source_layout"}, tool=PLANNER_TOOL)
        inputs = json.loads(wire["messages"][-1]["content"])
        sends.append("layout" if "source_layout" in inputs else "extract")
        values = [
            {"name": "收纳整理架短款", "sku_id": "FIX-FACT-A", "price": 39.9, "currency": "CNY"},
            {"name": "收纳整理架长款", "sku_id": "FIX-FACT-B", "price": 59.9, "currency": "CNY"},
        ]
        if severe and len(sends) == 1:
            values = []
        return _stream(len(sends), {"products": values, "questions": []})

    await engine.start()
    engine.model = DeepSeekAgentModel(async_transport=httpx.MockTransport(respond), key_reader=lambda: "fixture-token",
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request,
        on_response=engine._model_response)
    identity, run = await submit_and_wait(engine, SOURCE, filename="facts-only.md", mime="text/markdown")
    assert sends == (["extract", "main_recovery", "layout"] if severe else ["extract"]), run.get("error")
    question, = run["questions"]
    assert question["kind"] == "catalog_intake_approval", run.get("error")
    assert [item["sku_id"] for item in question["preview"]["items"]] == ["FIX-FACT-A", "FIX-FACT-B"]
    events = engine.events(identity, 0, 200)["events"]
    assert not any(event["type"] == "workflow.validation" for event in events)
