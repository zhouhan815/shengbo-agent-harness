from copy import deepcopy

import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.catalog_coverage import apply_coverage
from shengbo_prelive.catalog_extract import _catalog_report, _result_tool
from shengbo_prelive.catalog_intake import _source_fingerprint
from shengbo_prelive.document_import import SessionImport, checked_session
from test_catalog_coverage import SOURCE, STANDARD, evidence, mark, prepared, product
from test_catalog_currency_evidence import _draft


def test_document_import_output_schema_does_not_mutate_recovery_fingerprint():
    source = {"locations": [], "product_schema": {"properties": {}, "$defs": {"Document": {"type": "object"}}}}
    original = deepcopy(source)
    parameters = _result_tool(source["product_schema"])["function"]["parameters"]
    parameters["$defs"].update(SessionImport.model_json_schema()["$defs"])

    assert "Coupon" in parameters["$defs"]
    assert source == original
    assert _source_fingerprint(source) == _source_fingerprint(original)


@pytest.mark.parametrize("conflicting_sku", [False, True])
def test_exact_sku_table_repairs_an_incomplete_quote_but_rejects_other_sku(conflicting_sku):
    draft, source = _draft("69.90 元人民币", "CNY")
    candidate = draft["products"][0]
    link = next(item for item in candidate["evidence"] if item["fields"] == ["sku_id"])
    if conflicting_sku:
        link["quote"] = "HY-IC20"
        with pytest.raises(ContractError) as error:
            _catalog_report(draft, source)
        assert error.value.details["fields"] == ["sku_id"]
        return

    link.update(location="L1", quote="保冷袋便携款", fields=["sku_id", "name"])
    original = deepcopy(draft)
    report = _catalog_report(draft, source)

    proofs = [item for item in report["evidence"] if item["sku_id"] == "HY-IC12" and "sku_id" in item["fields"]]
    assert [(item["locator"], item["quote"]) for item in proofs] == [("table:1:row:2", "HY-IC12")]
    assert report["fields"]["products"][0]["name"] == "保冷袋便携款"
    assert draft == original


def test_session_configuration_is_not_permanent_catalog_knowledge_or_public_promotion():
    source = prepared(("paragraph:1", "直播活动"), ("paragraph:2", "商品卡付款后享活动价。"),
                      ("paragraph:3", "本场知识文档与口播安排"),
                      ("paragraph:4", "先展示桌面清洁，再用普通话口播。"),
                      ("paragraph:5", "配送与售后"), ("paragraph:6", "从江苏苏州发货。"))
    for block in source["locations"]:
        block["kind"] = "paragraph"
    local = [unit for unit in source["source_units"] if unit["locator"] == "paragraph:4"]
    assert local and all(unit["excluded_reason"] == "session_configuration" for unit in local)
    assert next(unit for unit in source["source_units"] if unit["locator"] == "paragraph:6").get("excluded_reason") is None
    with pytest.raises(ContractError) as rejected:
        apply_coverage(source, [{**product(), "knowledge_documents": [
            {"id": "demo", "title": "演示安排", "content": local[0]["text"]}]}], [],
            [evidence(local[0], STANDARD, "knowledge_documents")])
    assert rejected.value.details["issues"][0]["reason"] == "session_configuration_in_catalog_evidence"
    session = checked_session({"rules": [{"location": "L2", "quote": "商品卡付款后享活动价。"},
        {"location": "L4", "quote": "先展示桌面清洁，再用普通话口播。"}]}, source)
    assert [rule["location"] for rule in session["rules"]] == ["L2"]


def test_verified_exact_faq_can_correct_a_knowledge_coverage_target_locally():
    source = prepared(("paragraph:1", "尘杯装满了怎么办？关机后拆下尘杯。"))
    unit, = source["source_units"]
    source["product_schema"]["properties"]["faq"] = {}
    candidate = {**product(), "faq": "问：尘杯装满了怎么办？\n答：关机后拆下尘杯。"}
    coverage = [mark(unit, "knowledge", STANDARD, "knowledge_documents")]
    proof = [evidence(unit, STANDARD, "faq")]
    (saved,), _, audit = apply_coverage(source, [candidate], coverage, proof)

    assert "knowledge_documents" not in saved
    assert audit["dispositions"][0]["targets"] == [{"sku_id": STANDARD, "field": "faq"}]
    with pytest.raises(ContractError):
        apply_coverage(source, [{**candidate, "faq": "问：尘杯装满了怎么办？\n答：继续开机。"}], coverage, proof)


@pytest.mark.parametrize("exclusive_answer", [False, True])
def test_permanent_audience_and_unambiguous_original_faq_can_be_repaired_without_cross_sku_claims(exclusive_answer):
    permanent = "适合清理书桌和抽屉。"
    session_only = "面向本场活动的办公室用户。"
    answer = "仅大号款能拆下清理。" if exclusive_answer else "请先关机再拆下尘杯清理。"
    source = prepared(("paragraph:1", "清洁机 商品资料"), ("paragraph:2", permanent),
        ("table:1:row:1", "配置项 | 轻巧款 | 大号款"), ("table:1:row:2", "SKU | BR-100 | BR-200"),
        ("table:1:row:3", "日常售价 | 99 元人民币 | 129 元人民币"),
        ("paragraph:3", "尘杯装满了怎么办？" + answer), ("paragraph:4", "本场配置"), ("paragraph:5", session_only))
    source["product_schema"]["properties"].update({"faq": {}, "target_audience": {}})
    units = source["source_units"]
    audience = next(unit for unit in units if unit["locator"] == "paragraph:2")
    faq = next(unit for unit in units if unit["locator"] == "paragraph:3")
    session = next(unit for unit in units if unit["locator"] == "paragraph:5")
    rows, proof = [], []
    coverage = [mark(audience, "retained", "*", "target_audience"), mark(faq, "knowledge", "*", "knowledge_documents")]
    fields = {"款式": "name", "SKU": "sku_id", "日常售价": "price"}
    for sku, variant, price in (("BR-100", "轻巧款", 99), ("BR-200", "大号款", 129)):
        rows.append({"sku_id": sku, "name": "清洁机 " + variant, "price": price, "currency": "CNY",
                     "target_audience": session_only, "usage_instructions": answer})
        scoped = [unit for unit in units if unit.get("sku_id") == sku]
        coverage.extend(mark(unit, "retained", sku, fields[unit["column"]]) for unit in scoped)
        sku_unit = next(unit for unit in scoped if unit["column"] == "SKU")
        proof.extend([evidence(sku_unit, sku, "sku_id"), evidence(audience, sku, "target_audience"),
                      evidence(session, sku, "target_audience"), {**evidence(faq, sku, "usage_instructions"), "quote": answer}])
    original = deepcopy(rows)
    if exclusive_answer:
        with pytest.raises(ContractError) as rejected:
            apply_coverage(source, rows, coverage, proof)
        assert any(item["sku_id"] == "BR-100" and item["unit"] == faq["id"] for item in rejected.value.details["issues"])
    else:
        saved, proofs, audit = apply_coverage(source, rows, coverage, proof)
        assert [(row["price"], row["target_audience"], row["faq"]) for row in saved] == [
            (price, audience["text"], "问：尘杯装满了怎么办？\n答：" + answer) for price in (99, 129)]
        assert not any(item["locator"] == session["locator"] for item in proofs)
        assert len(audit["reclassified_audience"]) == 2
    assert rows == original


@pytest.mark.parametrize("exact_scene_proof", [True, False])
def test_shared_scene_is_rebuilt_from_a_single_mapped_literal_not_from_unrelated_quotes(exact_scene_proof):
    scene = "适合整理桌面上的充电线"
    material = "两款均为硅胶理线夹"
    source = prepared(("paragraph:1", "松岬理线夹 商品资料"),
        ("paragraph:2", f"品牌：松岬。类目：桌面收纳。计价币种：人民币。{material}，{scene}。"),
        ("table:1:row:1", "项目 | 六枚装 | 十二枚装"), ("table:1:row:2", "SKU | SJ-CL6 | SJ-CL12"),
        ("table:1:row:3", "包装数量 | 6 枚 | 12 枚"), ("table:1:row:4", "日常售价 | 19.90 元 | 29.90 元"))
    source["sources"] = [SOURCE]
    source["product_schema"]["properties"]["target_audience"] = {}
    units = source["source_units"]
    by_text = {unit["text"]: unit for unit in units}
    sku_fields = {"款式": "name", "SKU": "sku_id", "包装数量": "specifications", "日常售价": "price"}
    coverage = {unit["id"]: (f"{unit['sku_id']}|{sku_fields[unit['column']]}" if unit.get("sku_id") else "+*")
                for unit in units if not unit.get("excluded_reason")}
    for text, target in (("松岬理线夹 商品资料", "SKU|target_audience"), ("类目：桌面收纳", "SKU|target_audience"),
                         ("计价币种：人民币", "*|currency"), (scene, "*|target_audience")):
        coverage[by_text[text]["id"]] = target
    products = []
    for sku, variant, qty, price in (("SJ-CL6", "六枚装", "6 枚", 19.9), ("SJ-CL12", "十二枚装", "12 枚", 29.9)):
        links = [{"location": unit["location"], "quote": unit["text"], "fields": [sku_fields[unit["column"]]]}
                 for unit in units if unit.get("sku_id") == sku]
        links.extend([{"location": "L2", "quote": "计价币种：人民币", "fields": ["currency"]},
            {"location": "L2", "quote": f"{material}，{scene}" if exact_scene_proof else material, "fields": ["specifications"]}])
        products.append({"fields": {"sku_id": sku, "name": f"松岬理线夹 {variant}", "price": price, "currency": "CNY",
            "specifications": [{"name": "包装数量", "value": qty}], "target_audience": "需要整理桌面充电线的用户"},
            "evidence": links})
    draft = {"products": products, "questions": [], "coverage": coverage}
    if not exact_scene_proof:
        with pytest.raises(ContractError) as rejected:
            _catalog_report(draft, source)
        assert rejected.value.details["fields"] == ["target_audience"]
        return
    report = _catalog_report(draft, source)
    for row, quantity in zip(report["fields"]["products"], ("6 枚", "12 枚"), strict=True):
        specs = {item["name"]: item["value"] for item in row["specifications"]}
        assert specs == {"包装数量": quantity, "品牌": "松岬", "类目": "桌面收纳"}
        assert row["target_audience"] == scene and row["currency"] == "CNY"
    assert {x["quote"] for x in report["evidence"] if "target_audience" in x["fields"]} == {scene}
