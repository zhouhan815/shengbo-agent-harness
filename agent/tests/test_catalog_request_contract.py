import json
from types import SimpleNamespace

import httpx
import pytest

from shengbo_prelive.catalog_extract import extract_catalog_report
from shengbo_prelive.catalog_intake import BASIC_CREATE_REQUEST
from shengbo_prelive.document_import import SessionImport
from shengbo_prelive.providers.chatgpt import ModelProviderError
from shengbo_prelive.providers.deepseek import DeepSeekAgentModel
from test_catalog_coverage import prepared


@pytest.mark.asyncio
async def test_mixed_import_has_both_contracts_and_keeps_mixed_table_rows_in_catalog_scope():
    source = prepared(
        ("table:1:row:1", "款式 | SKU | 日常售价 | 直播售价 | 可售库存"),
        ("table:1:row:2", "轻便款 | CASE-A | 99 元 | 79 元 | 12 件"),
        ("paragraph:1", "以上金额均为人民币。"),
        ("paragraph:2", "本场配置"),
        ("paragraph:3", "现场演示开盖顺序。"),
    )
    for location in source["locations"]:
        location["kind"] = "table_row" if location.get("cells") else "paragraph"
    source["policy"] = {}
    sent = []

    def capture(request):
        sent.append(json.loads(request.content))
        return httpx.Response(402, json={"error": {"message": "fixture"}})

    class Engine:
        model = DeepSeekAgentModel(async_transport=httpx.MockTransport(capture), key_reader=lambda: "fixture")

        async def assert_scope(self, scope):
            return {"input": {"text": BASIC_CREATE_REQUEST,
                "view_context": {"view": "products", "product_material_action": "create", "catalog_create_room": True}}}

    with pytest.raises(ModelProviderError) as failed:
        await extract_catalog_report(Engine(), SimpleNamespace(role="product_facts", phase="execute"), source)
    assert failed.value.code == "model_provider_balance_insufficient"
    (wire,) = sent
    assert wire["response_format"] == {"type": "json_object"}
    system = wire["messages"][0]["content"]
    inputs = json.loads(wire["messages"][-1]["content"])
    assert inputs["session_source_locations"] == ["L4", "L5"]
    assert inputs["session_table_prices"] == [{"location": "L2", "sku": "CASE-A", "column": "直播售价",
        "cell_text": "79 元", "resolved_by_program": True}]
    assert any(unit["location"] == "L2" and unit["text"] == "99 元" for unit in inputs["source_units"])
    assert not any(unit["text"] in {"79 元", "12 件"} for unit in inputs["source_units"])
    assert "商品部分" in system and "本场部分" in system
    assert "U 编号仅用于 coverage" in system and "勿在 session.prices 重复输出" in system

    example = json.loads(system.rsplit("\n", 1)[-1])
    parsed = SessionImport.model_validate(example["session"])
    assert parsed.prices[0].sku == "BOX-A" and parsed.prices[0].amount == 79
    assert parsed.rules and parsed.scene == [] and example["products"][0]["fields"]["price"] == 99
    assert example["products"][0]["evidence"][-1]["fields"] == ["currency"]
