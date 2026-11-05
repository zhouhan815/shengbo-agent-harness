from copy import deepcopy

import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.catalog_extract import _result_tool
from shengbo_prelive.catalog_facts import facts_only_catalog_report
from test_catalog_coverage import SOURCE, prepared


def fixture():
    source = prepared(("paragraph:1", "海湾收纳盒 商品资料"),
        ("paragraph:2", "商品币种为人民币（CNY）。"),
        ("table:1:row:1", "SKU | 款式 | 日常售价 | 活动价 | 盒体尺寸"),
        ("table:1:row:2", "HW-C01 | 便携款 | 39.90 元 | 35.90 元 | 20 × 10 厘米"),
        ("table:1:row:3", "HW-C02 | 加宽款 | 49.90 元 | 45.90 元 | 30 × 20 厘米"))
    source["sources"] = [SOURCE]
    return source


def _row(sku="HW-C01", name="海湾收纳盒 便携款", price=39.9, currency="CNY"):
    return {"sku_id": sku, "name": name, "price": price, "currency": currency, "knowledge_documents": [
        {"id": "product-care", "title": "清洁方式", "content": "用干布擦拭。"}]}


def test_contract_and_legacy_reply_do_not_depend_on_evidence_or_coverage():
    source = fixture()
    raw = _row()
    contract = _result_tool(source["product_schema"], source_unit_count=4, image_locations=["L8"], facts_only=True)
    properties = contract["function"]["parameters"]["properties"]
    assert set(properties) == {"products", "questions"}
    assert {"name", "sku_id"} == set(properties["products"]["items"]["required"])
    draft = {"products": [{"fields": raw, "evidence": [{"location": "L999", "fields": ["never_submitted"]}]}],
             "coverage": {"does_not_exist": "not_a_valid_route"}, "questions": []}
    original = deepcopy(draft)

    report, issues = facts_only_catalog_report(draft, source)

    assert report["fields"]["products"] == [raw] and not report["evidence"]
    assert draft == original
    assert issues == [{"sku_id": "HW-C02", "reason": "source_sku_omitted"}]


def test_known_source_corrects_wrong_regular_price_currency_and_exact_scoped_spec():
    source = fixture()
    row = {**_row(price=35.9, currency="USD"), "specifications": [{"name": "盒体尺寸", "value": "30 × 20 厘米"}]}
    report, issues = facts_only_catalog_report({"products": [row], "questions": []}, source)
    saved, = report["fields"]["products"]
    assert (saved["price"], saved["currency"]) == (39.9, "CNY")
    assert saved["specifications"] == [{"name": "盒体尺寸", "value": "20 × 10 厘米"}]
    assert {item.get("field") for item in issues} >= {"price", "currency", "specifications"}


def test_wrong_sku_cannot_enter_catalog_but_one_safe_sibling_can_continue():
    source = fixture()
    report, diagnostics = facts_only_catalog_report({"products": [_row(sku="MADE-UP"), _row()], "questions": []}, source)
    assert [row["sku_id"] for row in report["fields"]["products"]] == ["HW-C01"]
    assert {item["reason"] for item in diagnostics} == {"sku_not_in_source", "source_sku_omitted"}
    with pytest.raises(ContractError) as error:
        facts_only_catalog_report({"products": [_row(sku="MADE-UP")], "questions": []}, source, allow_partial=True)
    assert error.value.details["major_extraction_failure"] is True
    assert error.value.details["meaningful_units"] == error.value.details["unretained_units"] == 2


def test_blank_or_conflicting_currency_and_price_go_to_the_existing_specific_input_flow():
    source = fixture()
    units = source["source_units"]
    units[:] = [unit for unit in units if "人民币" not in unit["text"]]
    daily = next(unit for unit in units if unit.get("sku_id") == "HW-C01" and unit.get("column") == "日常售价")
    units.append({**daily, "id": "U90", "text": "55 元"})
    draft = _row()
    draft.pop("currency")
    report, _ = facts_only_catalog_report({"products": [draft], "questions": []}, source)
    saved, = report["fields"]["products"]
    assert "price" not in saved and "currency" not in saved


def test_unusual_prose_and_bad_optional_question_shape_do_not_create_a_fact_gate():
    source = prepared(("paragraph:1", "随身盒的店内编号如图片及描述，正常出售时是四十多元整。"))
    source["sources"] = [SOURCE]
    raw = _row(sku="hw-c01", price=43)
    report, diagnostics = facts_only_catalog_report({"products": [raw], "questions": "optional internal comment"}, source)
    assert report["fields"]["products"] == [raw]
    assert report["conflicts"] == []
    assert diagnostics == [{"reason": "model_questions_omitted", "count": 1}]


def test_clear_source_sku_is_canonicalized_case_insensitively():
    source = fixture()
    report, _ = facts_only_catalog_report({"products": [_row(sku="hw-c01")]}, source)
    assert report["fields"]["products"][0]["sku_id"] == "HW-C01"


def test_most_structured_facts_missing_is_severe_but_safe_rows_can_be_retained_after_recovery():
    source = fixture()
    template = next(unit for unit in source["source_units"] if unit.get("sku_id") == "HW-C01")
    for number in range(12):
        source["source_units"].append({**template, "id": f"U{100 + number}", "column": f"明确参数{number}",
                                       "text": f"原文精确参数值{number}"})
    args = {"products": [_row()], "questions": []}
    with pytest.raises(ContractError) as error:
        facts_only_catalog_report(args, source)
    assert error.value.details["major_extraction_failure"] is True
    assert error.value.details["meaningful_units"] == error.value.details["unretained_units"] == 14
    report, diagnostics = facts_only_catalog_report(args, source, allow_partial=True)
    assert [row["sku_id"] for row in report["fields"]["products"]] == ["HW-C01"]
    assert any(item["reason"] == "partial_facts_retained" for item in diagnostics)
