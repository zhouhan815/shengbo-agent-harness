from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.catalog_extract import EVIDENCE_REPAIR_TOOL, EvidenceCorrection, _catalog_report, extract_catalog_report
from test_catalog_coverage import SOURCE, prepared


def _draft(amount_b, currency_b):
    source = prepared(
        ("table:1:row:1", "项目 | 保冷袋便携款 | 保冷袋家庭款"),
        ("table:1:row:2", "SKU | HY-IC12 | HY-IC20"),
        ("table:1:row:3", f"日常零售价 | 69.90 元人民币 | {amount_b}"),
    )
    source["sources"] = [SOURCE]
    columns = {"款式": "name", "SKU": "sku_id", "日常零售价": "price"}
    candidates = []
    coverage = {}
    for sku, name, currency in (("HY-IC12", "保冷袋便携款", "CNY"), ("HY-IC20", "保冷袋家庭款", currency_b)):
        links = []
        for unit in source["source_units"]:
            if unit.get("sku_id") != sku:
                continue
            field = columns[unit["column"]]
            coverage[unit["id"]] = f"{sku}|{field}"
            links.append({"location": unit["location"], "quote": unit["text"], "fields": [field]})
        candidates.append({"fields": {"name": name, "sku_id": sku, "price": 69.9, "currency": currency},
                           "evidence": links})
    return {"products": candidates, "questions": [], "coverage": coverage}, source


@pytest.mark.parametrize(("price_text", "currency"), [
    ("69.90 元人民币", "CNY"), ("69.90 新臺幣", "TWD"), ("USD69.90", "USD"), ("AU$69.90", "AUD"),
])
def test_exact_sku_price_attaches_currency_proof_without_changing_model_draft(price_text, currency):
    draft, source = _draft(price_text, currency)
    original = deepcopy(draft)
    report = _catalog_report(draft, source)
    assert draft == original
    assert [row["currency"] for row in report["fields"]["products"]] == ["CNY", currency]
    assert [(entry["sku_id"], entry["quote"], entry["locator"]) for entry in report["evidence"]
            if "currency" in entry["fields"]] == [
        ("HY-IC12", "69.90 元人民币", "table:1:row:3"), ("HY-IC20", price_text, "table:1:row:3")]


@pytest.mark.parametrize("price_text", ["69.90 元", "¥69.90", "$69.90"])
def test_no_currency_inference_from_ambiguous_symbol_or_sibling_sku(price_text):
    draft, source = _draft(price_text, "CNY")
    draft["products"][1]["evidence"].append({"location": "L3", "fields": ["currency"], "quote": price_text})
    with pytest.raises(ContractError) as caught:
        _catalog_report(draft, source)
    assert caught.value.details["candidate_index"] == 1
    assert caught.value.details["fields"] == ["currency"]
    assert "关联" in caught.value.details["correction"]


@pytest.mark.parametrize("price_text", ["69.90 美元", "69.90 美元（人民币）"])
def test_explicit_currency_conflict_is_rejected_even_with_a_model_link(price_text):
    draft, source = _draft(price_text, "CNY")
    draft["products"][1]["evidence"].append({"location": "L3", "fields": ["currency"], "quote": price_text})
    with pytest.raises(ContractError) as caught:
        _catalog_report(draft, source)
    assert caught.value.details["candidate_index"] == 1
    assert caught.value.details["fields"] == ["currency"]
    assert "不一致" in caught.value.details["correction"]


@pytest.mark.parametrize(("price_text", "expected"), [("69.90 元", None), ("69.90 美元", "USD")])
def test_currency_is_omitted_for_confirmation_or_filled_from_exact_source(price_text, expected):
    draft, source = _draft(price_text, "USD")
    draft["products"][1]["fields"].pop("currency")
    report = _catalog_report(draft, source)
    candidate = report["fields"]["products"][1]
    assert candidate.get("currency") == expected
    assert ("currency" in candidate) == (expected is not None)
    assert ("currency" in draft["products"][1]["fields"]) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_quote", ["69.90 美元", "69.90 元人民币"])
async def test_evidence_only_repair_keeps_candidate_and_rejects_other_sku_source(repair_quote):
    draft, source = _draft("69.90 美元", "USD")
    source["policy"] = {}
    candidate = draft["products"][1]
    candidate["evidence"].append({"location": "L3", "quote": "69.90 美元", "fields": ["currency"]})
    next(link for link in candidate["evidence"] if link["fields"] == ["price"])["quote"] = "模型误写的引句"
    with pytest.raises(EvidenceCorrection) as caught:
        _catalog_report(draft, source)
    repair = caught.value
    original = deepcopy(repair.draft)
    assert repair.details["issues"] == [
        {"candidate_index": 1, "sku_id": "HY-IC20", "fields": ["price"], "rejected_locations": ["L3"]}]

    class Model:
        def bind_tools(self, tools, **kwargs):
            assert tools[0]["function"]["name"] == EVIDENCE_REPAIR_TOOL
            return self

        async def ainvoke(self, messages):
            inputs = json.loads(messages[-1].content)
            scope = inputs["candidates_needing_evidence"]["1"]
            assert scope["fields"] == {"price": 69.9}
            assert scope["originals"]["L3"] == ["69.90 美元"]
            assert inputs["user_location_hint"] == "原表第三行。"
            return SimpleNamespace(tool_calls=[{"name": EVIDENCE_REPAIR_TOOL, "args": {
                "evidence": {"1": [{"location": "L3", "quote": repair_quote, "fields": ["price"]}]}}}])

    class Engine:
        model = Model()

        async def assert_scope(self, scope):
            return {}

    scope = SimpleNamespace(role="product_facts", phase="execute")
    if repair_quote == "69.90 美元":
        report = await extract_catalog_report(Engine(), scope, source, feedback=repair, user_hint="原表第三行。")
        assert report["fields"]["products"] == [item["fields"] for item in draft["products"]]
    else:
        with pytest.raises(ContractError) as invalid:
            await extract_catalog_report(Engine(), scope, source, feedback=repair, user_hint="原表第三行。")
        assert "本款明确的逐字原文" in invalid.value.details["correction"]
    assert repair.draft == original
