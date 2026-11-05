"""Consumers' source clauses must survive catalog extraction and stay scoped."""
from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.catalog_coverage import apply_coverage, build_source_units, source_family_anchor


SOURCE = {"kind": "source_manifest", "id": "material-coverage-fixture", "revision": 1,
          "content_hash": "sha256:" + "a" * 64, "schema_version": "PRELIVE-2.3"}
STANDARD = "LR10-A"
LARGE = "LR10-B"
RETURN = "签收后 7 天内，未使用且包装完整的商品可申请退货。"
DAMAGE = "到货破损或少件可联系客服补发或退款。"
EXTRA = "松开纸卷两端卡扣后可取下纸卷。"
TABLE = [
    ("table:1:row:1", "款式 | SKU | 包装内容 | 日常售价 | 直播售价 | 可售库存"),
    ("table:1:row:2", "标准装 | LR10-A | 1 个手柄＋2 卷粘纸 | 29.90 元 | 24.90 元 | 180 件"),
    ("table:1:row:3", "加量装 | LR10-B | 1 个手柄＋5 卷粘纸 | 39.90 元 | 34.90 元 | 90 件"),
]


def prepared(*parts):
    locations = []
    for index, (locator, value) in enumerate(parts, 1):
        location = {"location": f"L{index}", "locator": locator, "text": value, "source_ref": SOURCE}
        if locator.startswith("table:"):
            _, table, _, row = locator.split(":")
            location.update(table_id=int(table), row=int(row), cells=[
                {"locator": f"{locator}:cell:{column}", "text": cell, "column": column,
                 "grid_column": column, "column_span": 1, "vertical_merge": None}
                for column, cell in enumerate(value.split(" | "), 1)
            ])
        locations.append(location)
    return {"locations": locations, "source_units": build_source_units(locations),
            "product_schema": {"properties": {field: {} for field in (
                "name", "sku_id", "currency", "price", "specifications", "return_policy",
                "shipping_origin", "shipping_time", "usage_instructions", "knowledge_documents")}}}


def test_exact_topic_heading_is_excluded_for_new_and_legacy_source_snapshots():
    source = prepared(("paragraph:1", "适用场景"), ("paragraph:2", "适合出差途中临时补扣。"))
    source["locations"][0].update(section_label=True, kind="paragraph")
    title, fact = source["source_units"]
    assert "excluded_reason" not in title  # An already frozen snapshot from the earlier reader.
    refreshed = build_source_units(source["locations"])
    assert refreshed[0]["excluded_reason"] == "metadata"
    assert "excluded_reason" not in refreshed[1]

    (saved,), _, audit = apply_coverage(source, [product()], [mark(title, "excluded", reason="metadata"),
        mark(fact, "knowledge", STANDARD, "knowledge_documents")], [])

    assert audit["dispositions"][0]["reason"] == "metadata"
    assert saved["knowledge_documents"][0]["content"] == "适合出差途中临时补扣"
    assert "excluded_reason" not in title


@pytest.mark.parametrize(("text", "section_label"), [
    ("适用场景", False), ("适用场景：办公室和出差", True), ("适用线径为 3 至 5 毫米。", True),
])
def test_topic_classification_does_not_discard_body_text_or_values(text, section_label):
    source = prepared(("paragraph:1", text))
    source["locations"][0].update(section_label=section_label, kind="paragraph")
    (unit,) = build_source_units(source["locations"])
    assert not unit.get("excluded_reason")


def product(sku=STANDARD):
    name, price, count = ("标准装", 29.90, 2) if sku == STANDARD else ("加量装", 39.90, 5)
    return {"name": "净爪便携粘毛滚筒 " + name, "sku_id": sku, "currency": "CNY", "price": price,
            "specifications": [{"name": "包装内容", "value": f"1 个手柄＋{count} 卷粘纸"}]}


def mark(unit, disposition, sku=None, field=None, *, reason=None):
    value = {"unit": unit["id"], "disposition": disposition,
             "targets": [{"sku_id": sku, "field": field}] if sku is not None else []}
    if reason is not None:
        value["reason"] = reason
    return value


def evidence(unit, sku, field, identity="e1"):
    return {"id": identity, "sku_id": sku, "fields": [field], "source_ref": unit["source_ref"],
            "locator": unit["locator"], "quote": unit["text"]}


def test_preserves_return_conditions_and_anchors_uncategorized_facts_in_knowledge():
    source = prepared(("paragraph:11", RETURN + DAMAGE), ("paragraph:12", EXTRA))
    units = source["source_units"]
    return_unit = next(unit for unit in units if "包装完整" in unit["text"])
    damage_unit = next(unit for unit in units if "联系客服" in unit["text"])
    extra_unit = next(unit for unit in units if "卡扣" in unit["text"])
    assert len({unit["id"] for unit in (return_unit, damage_unit, extra_unit)}) == 3
    assert "7 天内，未使用且包装完整" in return_unit["text"]
    assert "破损或少件" in damage_unit["text"] and "补发或退款" in damage_unit["text"]
    row = {**product(), "return_policy": RETURN}
    initial = [evidence(return_unit, STANDARD, "return_policy")]
    coverage = [mark(return_unit, "retained", STANDARD, "return_policy"),
                mark(damage_unit, "knowledge", STANDARD, "knowledge_documents"),
                mark(extra_unit, "knowledge", STANDARD, "knowledge_documents")]

    saved, proof, summary = apply_coverage(source, [row], coverage, initial)

    assert saved[0]["return_policy"] == RETURN
    knowledge = "\n".join(document["content"] for document in saved[0]["knowledge_documents"])
    assert "破损或少件可联系客服补发或退款" in knowledge
    assert "松开纸卷两端卡扣后可取下纸卷" in knowledge
    assert all(any(item["locator"] == unit["locator"] and item["source_ref"] == SOURCE
                   and item["quote"] == unit["text"] and item["sku_id"] == STANDARD
                   and "knowledge_documents" in item["fields"] for item in proof)
               for unit in (damage_unit, extra_unit))
    assert summary["retained_units"] == 1 and summary["knowledge_units"] == 2


@pytest.mark.parametrize("raw", ["问：可以机洗吗？答：仅可用湿布擦拭。",
                                 "可以机洗吗？仅可用湿布擦拭。"])
def test_faq_accepts_both_labelled_and_unlabelled_source_without_changing_quotes(raw):
    source = prepared(("paragraph:1", raw))
    source["product_schema"]["properties"]["faq"] = {}
    unit, = source["source_units"]
    row = {**product(), "faq": "问：可以机洗吗？\n答：仅可用湿布擦拭。"}
    saved, proof, summary = apply_coverage(source, [row], [mark(unit, "retained", STANDARD, "faq")],
                                          [evidence(unit, STANDARD, "faq")])
    assert saved[0]["faq"] == row["faq"] and not saved[0].get("knowledge_documents")
    assert proof[0]["quote"] == raw
    assert summary["retained_units"] == 1


def test_timed_promotion_section_stops_at_shipping_and_cannot_supply_catalog_evidence():
    source = prepared(("paragraph:1", "双十一直播活动"), ("paragraph:2", "活动价与限购。每件 59.90 元。"),
                      ("paragraph:3", "满 129 元减 15 元券，不含运费。"),
                      ("paragraph:4", "配送与售后"), ("paragraph:5", "订单满 99 元包邮。"))
    source["locations"][0]["section_label"] = True
    source["source_units"] = build_source_units(source["locations"])
    promo_amount = next(unit for unit in source["source_units"] if unit["text"] == "每件 59.90 元")
    coupon = next(unit for unit in source["source_units"] if "减 15 元券" in unit["text"])
    shipping = next(unit for unit in source["source_units"] if "满 99 元包邮" in unit["text"])
    assert promo_amount["excluded_reason"] == coupon["excluded_reason"] == "session_promotion"
    assert not shipping.get("excluded_reason")
    saved, proof, _ = apply_coverage(source, [product()], [mark(shipping, "knowledge", STANDARD, "knowledge_documents")], [])
    assert [row["quote"] for row in proof] == [shipping["text"]]
    assert saved[0]["knowledge_documents"][0]["content"] == shipping["text"]
    with pytest.raises(ContractError) as error:
        apply_coverage(source, [{**product(), "knowledge": promo_amount["text"]}],
                       [mark(shipping, "knowledge", STANDARD, "knowledge_documents")],
                       [evidence(promo_amount, STANDARD, "knowledge")])
    assert error.value.details["issues"][0]["reason"] == "temporary_promotion_in_catalog_evidence"


@pytest.mark.parametrize("clause", ["签收后 7 天内， 未使用且包装完整才可申请退货", "订单满 99 元，才享免运费"])
def test_conditional_source_unit_keeps_original_punctuation_and_internal_spaces(clause):
    source = prepared(("paragraph:1", clause + "。"))
    units = source["source_units"]
    assert [unit["text"] for unit in units] == [clause]


def test_currency_scope_header_is_not_a_minimum_condition():
    source = prepared(("paragraph:1", "以上金额均为人民币。"))
    unit, = source["source_units"]
    saved, _, summary = apply_coverage(source, [product(), product(LARGE)],
        [mark(unit, "retained", "*", "currency")], [])
    assert [row["currency"] for row in saved] == ["CNY", "CNY"]
    assert summary["retained_units"] == 1


def test_conditional_currency_statement_cannot_be_used_as_a_global_currency():
    source = prepared(("paragraph:1", "金额超过100元才用人民币结算。"))
    unit, = source["source_units"]
    with pytest.raises(ContractError, match="CATALOG_COVERAGE_INCOMPLETE"):
        apply_coverage(source, [product()], [mark(unit, "retained", "*", "currency")], [])


@pytest.mark.parametrize(("text", "currency_sku", "extra_field", "proof_quote", "normalized"), [
    ("29.90 元人民币", STANDARD, None, None, True),
    ("29.90 元人民币", LARGE, None, None, False),
    ("29.90 元人民币", STANDARD, "name", None, False),
    ("29.90 元人民币", STANDARD, None, "人民币", False),
    ("29.90 元人民币或美元", STANDARD, None, None, False),
    ("29.90 元", STANDARD, None, None, False),
    ("39.90 元人民币", STANDARD, None, None, False),
])
def test_only_exact_verified_price_cells_can_normalize_redundant_currency_coverage(
        text, currency_sku, extra_field, proof_quote, normalized):
    from shengbo_prelive.catalog_extract import _normalize_price_coverage

    unit = {"id": "U1", "location": "L1", "source_ref": SOURCE, "locator": "table:1:row:2",
            "sku_id": STANDARD, "column": "日常售价", "text": text}
    entry = mark(unit, "retained", STANDARD, "price")
    entry["targets"].append({"sku_id": currency_sku, "field": "currency"})
    if extra_field:
        entry["targets"].append({"sku_id": STANDARD, "field": extra_field})
    original_targets = deepcopy(entry["targets"])
    proofs = [evidence(unit, STANDARD, "price"),
              {**evidence(unit, STANDARD, "currency", "e2"), "quote": proof_quote or text}]
    original_proofs = deepcopy(proofs)

    _normalize_price_coverage([entry], [unit], [product()], proofs)

    assert entry["targets"] == (original_targets[:1] if normalized else original_targets)
    assert proofs == original_proofs


def test_mislabeled_explicit_attribute_is_recovered_for_its_named_product_only():
    source = prepared(("paragraph:1", "商品名称为净爪便携粘毛滚筒 标准装，耐温等级为常温。"))
    name, attribute = source["source_units"]
    entries = [mark(name, "retained", "*", "name"), mark(attribute, "excluded", reason="metadata")]
    saved, proof, summary = apply_coverage(source, [product()], entries, [])
    assert {"name": "耐温等级", "value": "常温"} in saved[0]["specifications"]
    assert proof[-1]["quote"] == attribute["text"]
    assert summary["retained_units"] == 2
    with pytest.raises(ContractError, match="CATALOG_COVERAGE_INCOMPLETE"):
        apply_coverage(source, [product(), product(LARGE)], entries, [])


@pytest.mark.parametrize("reason", ["metadata", "out_of_scope"])
def test_standalone_consumer_attribute_survives_model_exclusion_for_single_sku(reason):
    clause = "握持结构为双指凹槽"
    source = prepared(("paragraph:1", clause + "。"))
    unit, = source["source_units"]
    assert not unit.get("excluded_reason")

    saved, proof, summary = apply_coverage(source, [product()], [mark(unit, "excluded", reason=reason)], [])

    row, = saved
    attribute = {"name": "握持结构", "value": "双指凹槽"}
    assert attribute in row["specifications"] or any(
        clause in doc["content"] for doc in row.get("knowledge_documents", []))
    assert summary["excluded_units"] == 0
    assert any(entry["sku_id"] == STANDARD and entry["locator"] == unit["locator"]
               and entry["source_ref"] == SOURCE and entry["quote"] == clause for entry in proof)


def test_shared_scope_is_not_inferred_for_unrelated_product_families():
    source = prepared(("paragraph:1", "握持结构为双指凹槽。"))
    unit, = source["source_units"]
    rows = [product(), {**product(LARGE), "name": "另一款独立商品"}]
    original = deepcopy(rows)

    with pytest.raises(ContractError, match="CATALOG_COVERAGE_INCOMPLETE"):
        apply_coverage(source, rows, [mark(unit, "excluded", reason="metadata")], [])

    assert rows == original


def test_equivalent_shared_specification_stays_in_the_structured_field():
    source = prepared(("paragraph:1", "所有款式均采用圆角设计。"))
    unit, = source["source_units"]
    rows = [product(), product(LARGE)]
    for row in rows:
        row["specifications"].append({"name": "边缘处理", "value": "圆角设计"})
    coverage = [mark(unit, "retained", "*", "specifications")]
    proofs = [evidence(unit, sku, "specifications", f"e{index}")
              for index, sku in enumerate((STANDARD, LARGE), 1)]

    saved, _, summary = apply_coverage(source, rows, coverage, proofs)

    assert saved == rows
    assert summary["retained_units"] == 1 and summary["knowledge_units"] == 0
    assert not any(row.get("knowledge_documents") for row in saved)


def test_elliptical_machine_wash_instruction_keeps_its_complete_source_condition():
    clause = "两个款式均可在水温不超过 30℃ 时机洗"
    source = prepared(("paragraph:1", clause + "。"))
    unit, = source["source_units"]
    rows = [{**product(sku), "usage_instructions": "水温不超过 30℃ 机洗"} for sku in (STANDARD, LARGE)]
    proofs = [evidence(unit, sku, "usage_instructions", f"e{index}")
              for index, sku in enumerate((STANDARD, LARGE), 1)]

    saved, _, summary = apply_coverage(source, rows, [mark(unit, "retained", "*", "usage_instructions")], proofs)

    for row in saved:
        assert "可在水温不超过30℃时机洗" in row["usage_instructions"].replace(" ", "")
        assert not row.get("knowledge_documents")
    assert summary["retained_units"] == 1


def test_exact_unknown_optional_attribute_is_not_saved_as_a_specification():
    source = prepared(("paragraph:1", "颜色为暖白色。"), ("paragraph:2", "握持结构为待确认。"))
    known, unknown = source["source_units"]
    row = product()
    confirmed = {"name": "颜色", "value": "暖白色"}
    row["specifications"].extend([confirmed, {"name": "握持结构", "value": "待确认"}])
    coverage = [mark(known, "retained", STANDARD, "specifications")]
    if not unknown.get("excluded_reason"):
        coverage.append(mark(unknown, "retained", STANDARD, "specifications"))
    proofs = [evidence(known, STANDARD, "specifications", "e1"),
              evidence(unknown, STANDARD, "specifications", "e2")]

    saved, _, summary = apply_coverage(source, [row], coverage, proofs)

    result, = saved
    assert confirmed in result["specifications"]
    assert "待确认" not in json.dumps(result, ensure_ascii=False)
    assert summary["retained_units"] == 1


def test_explicit_negative_source_cannot_coexist_with_a_positive_structured_claim():
    source = prepared(("paragraph:1", "所有款式均不支持边充边用。"))
    unit, = source["source_units"]
    row = product()
    false_claim = {"name": "边充边用", "value": "支持"}
    row["specifications"].append(false_claim)
    coverage = [mark(unit, "retained", STANDARD, "specifications")]
    proofs = [evidence(unit, STANDARD, "specifications")]

    try:
        saved, _, _ = apply_coverage(source, [row], coverage, proofs)
    except ContractError as error:
        assert error.code == "CATALOG_COVERAGE_INCOMPLETE" and error.status == 422
    else:
        result, = saved
        assert false_claim not in result["specifications"]
        assert ({"name": "边充边用", "value": "不支持"} in result["specifications"]
                or any("不支持边充边用" in doc["content"] for doc in result.get("knowledge_documents", [])))


@pytest.mark.parametrize("wrong_field", ["return_policy", "shipping_time"])
def test_freight_terms_are_knowledge_when_the_clause_contains_no_time_or_return(wrong_field):
    clause = "中国大陆地区免运费，港澳台及海外地区除外"
    source = prepared(("paragraph:1", clause + "。"))
    row = {**product(), wrong_field: clause}
    coverage = [mark(unit, "retained", STANDARD, wrong_field) for unit in source["source_units"]]
    proofs = [evidence(unit, STANDARD, wrong_field, f"e{index}")
              for index, unit in enumerate(source["source_units"], 1)]

    saved, proof, summary = apply_coverage(source, [row], coverage, proofs)

    result, = saved
    assert not result.get("return_policy") and not result.get("shipping_time")
    assert any(clause in doc["content"] for doc in result["knowledge_documents"])
    assert summary["knowledge_units"] >= 1
    assert any("knowledge_documents" in entry["fields"] and entry["source_ref"] == SOURCE
               and entry["locator"] == "paragraph:1" for entry in proof)


@pytest.mark.parametrize(("field", "valid"), [
    ("return_policy", RETURN),
    ("shipping_time", "付款后 48 小时内发货。"),
])
def test_freight_is_removed_without_losing_other_valid_text_in_the_same_field(field, valid):
    freight = "中国大陆地区免运费，港澳台及海外地区除外。"
    combined = valid + freight
    source = prepared(("paragraph:1", combined))
    units = source["source_units"]
    row = {**product(), field: combined}
    coverage = [mark(unit, "retained", STANDARD, field) for unit in units]
    proofs = [evidence(unit, STANDARD, field, f"e{index}") for index, unit in enumerate(units, 1)]

    saved, _, _ = apply_coverage(source, [row], coverage, proofs)

    result, = saved
    assert result[field].rstrip("。") == valid.rstrip("。")
    assert any(freight.rstrip("。") in doc["content"] for doc in result["knowledge_documents"])


def test_freight_removal_preserves_after_sales_conditions_and_original_punctuation():
    freight = "单笔订单商品实付满 99 元包邮，运费不计入门槛；新疆、西藏、青海不参加包邮，具体运费以结算页为准。"
    returns = ("签收后 7 天内，商品未使用且包装、配件齐全，可申请七天无理由退货；"
               "若收到商品存在破损或拉链故障，请提供订单信息和现场照片联系售后。")
    source = prepared(("paragraph:1", freight), ("paragraph:2", returns))
    row = {**product(), "return_policy": freight + returns}
    units = [unit for unit in source["source_units"] if not unit.get("excluded_reason")]
    coverage = [mark(unit, "retained", STANDARD, "return_policy") for unit in units]
    proofs = [evidence(unit, STANDARD, "return_policy", f"e{index}") for index, unit in enumerate(units, 1)]

    (saved,), _, _ = apply_coverage(source, [row], coverage, proofs)

    assert saved["return_policy"] == returns
    knowledge = "\n".join(doc["content"] for doc in saved["knowledge_documents"])
    assert all(unit["text"] in knowledge for unit in units if unit["locator"] == "paragraph:1")


def test_canonical_fulfillment_fields_are_used_even_if_model_routes_all_clauses_to_knowledge():
    origin = "发货地为浙江宁波"
    timing = "付款后 48 小时内发货"
    freight = "中国大陆地区免运费，港澳台及海外地区除外"
    source = prepared(("paragraph:1", f"{origin}，{timing}，{freight}。"))
    coverage = [mark(unit, "knowledge", STANDARD, "knowledge_documents") for unit in source["source_units"]]

    saved, proof, _ = apply_coverage(source, [product()], coverage, [])

    row, = saved
    assert row["shipping_origin"] == "浙江宁波"
    assert row["shipping_time"].rstrip("。") == timing
    knowledge = "\n".join(doc["content"] for doc in row["knowledge_documents"])
    assert freight in knowledge
    assert "浙江宁波" not in knowledge and timing not in knowledge
    destinations = {(entry["quote"], field) for entry in proof for field in entry["fields"]}
    assert destinations == {(origin, "shipping_origin"), (timing, "shipping_time"),
                            (freight, "knowledge_documents")}


def test_new_freight_evidence_id_does_not_collide_with_a_remaining_original_id():
    source = prepared(("paragraph:1", "中国大陆地区免运费。"), ("paragraph:2", RETURN))
    freight, returns = source["source_units"]
    row = {**product(), "shipping_time": freight["text"], "return_policy": RETURN}
    coverage = [mark(freight, "retained", STANDARD, "shipping_time"),
                mark(returns, "retained", STANDARD, "return_policy")]
    proofs = [evidence(freight, STANDARD, "shipping_time", "e1"),
              evidence(returns, STANDARD, "return_policy", "e2")]

    saved, proof, _ = apply_coverage(source, [row], coverage, proofs)

    assert not saved[0].get("shipping_time")
    assert len(proof) == len({entry["id"] for entry in proof}) == 2
    assert any(entry["quote"] == returns["text"] and entry["fields"] == ["return_policy"] for entry in proof)
    assert any(entry["quote"] == freight["text"] and entry["fields"] == ["knowledge_documents"] for entry in proof)


def test_arbitrary_source_table_attribute_is_recovered_in_its_own_sku():
    source = prepared(("table:1:row:1", "款式 | SKU | 日常售价 | 防脱结构"),
                      ("table:1:row:2", f"标准装 | {STANDARD} | 29.90 元 | 双侧卡扣"),
                      ("table:1:row:3", f"加量装 | {LARGE} | 39.90 元 | 弹簧插销"))
    rows = [product(), product(LARGE)]
    fields = {"款式": "name", "SKU": "sku_id", "日常售价": "price"}
    coverage, proofs = [], []
    for unit in source["source_units"]:
        if unit.get("excluded_reason"):
            continue
        column = unit["column"]
        if column == "防脱结构":
            coverage.append(mark(unit, "excluded", reason="metadata"))
        else:
            field = fields[column]
            coverage.append(mark(unit, "retained", unit["sku_id"], field))
            proofs.append(evidence(unit, unit["sku_id"], field, f"e{len(proofs) + 1}"))

    saved, proof, summary = apply_coverage(source, rows, coverage, proofs)

    expected = {STANDARD: "双侧卡扣", LARGE: "弹簧插销"}
    assert [(row["sku_id"], row["price"]) for row in saved] == [(STANDARD, 29.90), (LARGE, 39.90)]
    for row in saved:
        sku = row["sku_id"]
        assert [spec["value"] for spec in row["specifications"] if spec["name"] == "防脱结构"] == [expected[sku]]
    recovered_proof = {(entry["sku_id"], entry["quote"]) for entry in proof
                       if entry["quote"] in expected.values()}
    assert recovered_proof == set(expected.items())
    assert summary["excluded_units"] == 1


@pytest.mark.asyncio
async def test_legacy_optional_coverage_gap_does_not_require_a_second_call():
    from shengbo_prelive.catalog_extract import TOOL_NAME, extract_catalog_report

    row = product()
    row.pop("specifications")
    source = prepared(("paragraph:1", row["name"]), ("paragraph:2", row["sku_id"]),
                      ("paragraph:3", "29.9"), ("paragraph:4", "人民币"), ("paragraph:5", EXTRA))
    source.update(sources=[SOURCE], policy={})
    draft = {"products": [{"fields": row, "evidence": [
        {"location": f"L{i}", "fields": [field]} for i, field in
        enumerate(("name", "sku_id", "price", "currency"), 1)]}],
        "questions": [], "coverage": {"U1": "*|name", "U2": "*|sku_id",
                                      "U3": "*|price", "U4": "*|currency"}}
    calls = []
    diagnostics = {}

    class Model:
        def bind_tools(self, tools, **kwargs):
            assert tools[0]["function"]["name"] == TOOL_NAME
            return self

        async def ainvoke(self, messages):
            calls.append(json.loads(messages[-1].content))
            return SimpleNamespace(tool_calls=[{"name": TOOL_NAME, "args": draft}])

    class Store:
        def update(self, kind, identity, **values):
            assert (kind, identity) == ("branch", "branch")
            diagnostics.update(values)

    engine = SimpleNamespace(model=Model(), assert_scope=AsyncMock(return_value={}), store=Store())
    scope = SimpleNamespace(role="product_facts", phase="execute", branch_task_id="branch")
    result = await extract_catalog_report(engine, scope, source)
    assert [[u["id"] for u in sent["source_units"]] for sent in calls] == [["U1", "U2", "U3", "U4", "U5"]]
    saved, = result["fields"]["products"]
    assert saved == row
    assert not result["evidence"] and not diagnostics


@pytest.mark.parametrize("shortened", ["可申请退货", "签收后 7 天内可申请退货"])
def test_incomplete_return_policy_cannot_pass_as_an_unqualified_promise(shortened):
    source = prepared(("paragraph:1", RETURN))
    unit = source["source_units"][0]
    row = {**product(), "return_policy": shortened}
    coverage = [mark(unit, "retained", STANDARD, "return_policy")]
    try:
        saved, _, _ = apply_coverage(source, [row], coverage, [evidence(unit, STANDARD, "return_policy")])
    except ContractError as error:
        assert error.status == 422 and error.code == "CATALOG_COVERAGE_INCOMPLETE"
    else:
        assert "未使用且包装完整" in saved[0]["return_policy"]
    assert row["return_policy"] == shortened


def table_mappings(source):
    rows = [product(STANDARD), product(LARGE)]
    values = {"标准装": "name", "加量装": "name", STANDARD: "sku_id", LARGE: "sku_id",
              "1 个手柄＋2 卷粘纸": "specifications", "1 个手柄＋5 卷粘纸": "specifications",
              "29.90 元": "price", "39.90 元": "price"}
    coverage, proofs = [], []
    for index, unit in enumerate(source["source_units"], 1):
        if unit.get("excluded_reason"):
            continue
        field = next(value for text, value in values.items() if text in unit["text"])
        sku = unit["sku_id"]
        coverage.append(mark(unit, "retained", sku, field))
        proofs.append(evidence(unit, sku, field, f"e{index}"))
    return rows, coverage, proofs


def test_sku_table_keeps_retail_price_and_excludes_promo_and_stock():
    source = prepared(*TABLE)
    units = source["source_units"]
    for marker in ("24.90 元", "34.90 元", "180 件", "90 件"):
        assert any(marker in unit["text"] and unit.get("excluded_reason") for unit in units)
    rows, coverage, proofs = table_mappings(source)

    saved, proof, summary = apply_coverage(source, rows, coverage, proofs)

    assert [(item["sku_id"], item["price"]) for item in saved] == [(STANDARD, 29.90), (LARGE, 39.90)]
    assert summary["excluded_units"] >= 4
    product_content = json.dumps(saved, ensure_ascii=False)
    assert all(marker not in product_content for marker in ("24.90", "34.90", "180 件", "90 件"))
    assert all("直播售价" not in item["quote"] and "可售库存" not in item["quote"] for item in proof)


@pytest.mark.parametrize("intro, expected", [
    ([], ("净爪粘毛滚筒", "L1", "净爪粘毛滚筒商品资料")),
    ([("paragraph:2", "商品名称为净爪便携粘毛滚筒。")], ("净爪便携粘毛滚筒", "L2", "商品名称为净爪便携粘毛滚筒")),
    ([("paragraph:2", "商品名称为净爪粘毛滚筒。"), ("paragraph:3", "商品名称为折叠书架。")], None),
])
def test_family_anchor_requires_exact_source_name_and_one_sku_table(intro, expected):
    source = prepared(("paragraph:1", "净爪粘毛滚筒商品资料"), *intro, *TABLE)
    source["locations"][0]["section_label"] = True
    units = source["source_units"]
    variants = [unit for unit in units if unit.get("column") == "款式"]
    assert source_family_anchor(source["locations"], units, variants) == expected
    assert source_family_anchor(source["locations"], units, variants[:1]) is None


@pytest.mark.parametrize("problem", [None, "brand_in_name", "direct_opening", "no_shared_anchor", "wrong_variant", "wrong_scope", "conflicting_value"])
def test_titled_family_can_recover_misrouted_or_excluded_intro_attributes(problem):
    table = prepared(*TABLE)
    rows, coverage, proofs = table_mappings(table)
    if problem not in {"brand_in_name", "direct_opening"}:
        for row, name in zip(rows, ("标准装", "加量装")):
            row["name"] = name
    intro = "品牌为净爪，类目为织物清洁工具。"
    anchor = "清洁时可复用手柄。" if problem in {"no_shared_anchor", "direct_opening"} else "两款均可复用手柄。"
    source = prepared(("paragraph:1", "净爪便携粘毛滚筒 商品资料"),
                      ("paragraph:2", intro + anchor), *TABLE)
    indexed = {(unit["locator"], unit["text"]): unit for unit in source["source_units"]}
    for unit, mapped in zip((unit for unit in table["source_units"] if not unit.get("excluded_reason")), coverage):
        mapped["unit"] = indexed[unit["locator"], unit["text"]]["id"]
    brand, category, instruction = (unit for unit in source["source_units"] if unit["locator"] == "paragraph:2")
    coverage.extend([mark(brand, "retained", STANDARD if problem == "wrong_scope" else "*", "name"),
                     mark(category, "excluded", reason="metadata"),
                     mark(instruction, "knowledge", "*", "knowledge_documents")])
    if problem == "wrong_variant":
        rows[1]["name"] = "另一款独立商品"
    if problem == "conflicting_value":
        rows[0]["specifications"].append({"name": "品牌", "value": "其他品牌"})
    original = deepcopy(rows)
    if problem not in (None, "brand_in_name", "direct_opening"):
        with pytest.raises(ContractError, match="CATALOG_COVERAGE_INCOMPLETE"):
            apply_coverage(source, rows, coverage, proofs)
        assert rows == original
        return
    saved, evidence_records, summary = apply_coverage(source, rows, coverage, proofs)
    assert [(row["name"], row["sku_id"], row["price"]) for row in saved] == [
        (row["name"], row["sku_id"], row["price"]) for row in original]
    for row in saved:
        assert {"name": "品牌", "value": "净爪"} in row["specifications"]
        assert {"name": "类目", "value": "织物清洁工具"} in row["specifications"]
        assert {item["quote"] for item in evidence_records if item["sku_id"] == row["sku_id"]} >= {
            brand["text"], category["text"]}
    assert {entry["unit"] for entry in summary["dispositions"] if entry.get("recovery")} == {brand["id"], category["id"]}


@pytest.mark.parametrize("products_in_columns", [True, False], ids=["sku-per-column", "sku-per-row"])
def test_docx_catalog_prices_follow_sku_and_field_axes(products_in_columns):
    from docx import Document

    from shengbo_prelive.catalog_extract import _catalog_report, _located_text
    from shengbo_prelive.material_parsers import parse_docx
    from shengbo_prelive.material_views import source_view

    entries = [
        ("款式", "精细款", "大称量款"),
        ("SKU", "CY-KS1", "CY-KS5"),
        ("最大称量", "1,000 克", "5,000 克"),
        ("日常售价", "49.90 元", "69.90 元"),
        ("直播活动价", "39.90 元", "59.90 元"),
    ]
    document = Document()
    family = "秤语厨房电子秤"
    document.add_heading(f"{family} 商品资料", 0)
    document.add_paragraph("以上金额均为人民币。")
    values = entries if products_in_columns else list(zip(*entries))
    table = document.add_table(rows=len(values), cols=len(values[0]))
    for row, record in zip(table.rows, values):
        for cell, value in zip(row.cells, record):
            cell.text = value
    stream = BytesIO()
    document.save(stream)
    manifest = parse_docx(stream.getvalue())
    blocks = _located_text(source_view({"manifest": manifest, "text": manifest["text"]}))
    locations = [{**block, "location": f"L{index}", "source_ref": SOURCE}
                 for index, block in enumerate(blocks, 1)]
    units = build_source_units(locations)
    by_text = {unit["text"]: unit for unit in units}
    expected = [
        ("CY-KS1", "精细款", "1,000 克", "49.90 元", "39.90 元", 49.90),
        ("CY-KS5", "大称量款", "5,000 克", "69.90 元", "59.90 元", 69.90),
    ]
    catalog = []
    for sku, name, capacity, daily, promo, price in expected:
        assert by_text[capacity]["sku_id"] == by_text[daily]["sku_id"] == sku
        assert by_text[capacity]["column"] == "最大称量"
        assert by_text[daily]["column"] == "日常售价"
        assert by_text[promo]["column"] == "直播活动价"
        assert by_text[promo]["excluded_reason"] == "out_of_scope"
        assert by_text[daily]["locator"].startswith("table:1:row:")
        field_texts = {"name": name, "sku_id": sku, "price": daily, "specifications": capacity,
                       "currency": "以上金额均为人民币"}
        catalog.append({"fields": {"name": name, "sku_id": sku, "price": price, "currency": "CNY",
                                    "specifications": [{"name": "最大称量", "value": capacity}]},
                        "evidence": [{"location": by_text[quote]["location"], "quote": quote, "fields": [field]}
                                     for field, quote in field_texts.items()]})
    targets = {"SKU": "sku_id", "最大称量": "specifications", "日常售价": "price"}
    coverage = {}
    for unit in units:
        if unit.get("excluded_reason"):
            continue
        if unit["text"] == "以上金额均为人民币":
            coverage[unit["id"]] = "*|currency"
        else:
            field = targets.get(unit["column"], "name")
            coverage[unit["id"]] = f"{unit['sku_id']}|{field}"
    source = {"locations": locations, "source_units": units, "sources": [SOURCE],
              "product_schema": prepared()["product_schema"]}

    report = _catalog_report({"products": catalog, "questions": [], "coverage": coverage}, source)

    assert [(row["sku_id"], row["price"]) for row in report["fields"]["products"]] == [
        ("CY-KS1", 49.90), ("CY-KS5", 69.90)]
    assert [row["name"] for row in report["fields"]["products"]] == [f"{family} 精细款", f"{family} 大称量款"]
    for sku, variant, *_ in expected:
        assert {item["quote"] for item in report["evidence"] if item["sku_id"] == sku and "name" in item["fields"]} >= {
            f"{family} 商品资料", variant}

    no_title = {**source, "locations": locations[1:], "source_units": [unit for unit in units if unit["location"] != "L1"]}
    with pytest.raises(ContractError) as bare_name:
        _catalog_report({"products": catalog, "questions": [], "coverage": coverage}, no_title)
    assert bare_name.value.details["fields"] == ["name"]
    full = deepcopy(catalog)
    for candidate in full:
        candidate["fields"]["name"] = f"{family} · {candidate['fields']['name']}"
    checked = _catalog_report({"products": full, "questions": [], "coverage": coverage}, source)
    assert [row["name"] for row in checked["fields"]["products"]] == [f"{family} · 精细款", f"{family} · 大称量款"]
    assert "39.90 元" not in json.dumps(report["fields"], ensure_ascii=False)
    assert "59.90 元" not in json.dumps(report["fields"], ensure_ascii=False)
    if products_in_columns:
        # Both amounts are in the same L row. Seeing the second amount in the
        # source excerpt is insufficient evidence for the first SKU.
        wrong = deepcopy(catalog)
        wrong[0]["fields"]["price"] = 69.90
        next(link for link in wrong[0]["evidence"] if link["fields"] == ["price"])["quote"] = "69.90 元"
        with pytest.raises(ContractError) as failure:
            _catalog_report({"products": wrong, "questions": [], "coverage": coverage}, source)
        assert failure.value.code == "CATALOG_FACTS_INVALID"
        assert failure.value.details["fields"] == ["price"]


def test_ambiguous_table_keeps_original_rows_without_guessing_sku():
    source = prepared(
        ("table:1:row:1", "项目 | 标准装 | 加量装"),
        ("table:1:row:2", "SKU | LR10-A | LR10-A"),
        ("table:1:row:3", "日常售价 | 29.90 元 | 39.90 元"),
    )

    assert [(unit["locator"], unit["text"]) for unit in source["source_units"]] == [
        (location["locator"], location["text"]) for location in source["locations"]]
    assert all("sku_id" not in unit for unit in source["source_units"])


def test_real_live_price_quote_cannot_be_used_as_the_retail_price():
    source = prepared(*TABLE)
    rows, coverage, proofs = table_mappings(source)
    promo = next(unit for unit in source["source_units"] if unit["text"] == "24.90 元")
    rows[0]["price"] = 24.90
    proofs = [proof for proof in proofs if not (proof["sku_id"] == STANDARD and "price" in proof["fields"])]
    proofs.append(evidence(promo, STANDARD, "price", "real-but-wrong-price-evidence"))

    with pytest.raises(ContractError) as error:
        apply_coverage(source, rows, coverage, proofs)

    assert error.value.status == 422


def test_sku_specific_clause_cannot_be_attributed_to_another_sku():
    source = prepared(*TABLE)
    rows, coverage, proofs = table_mappings(source)
    sku_unit = next(unit for unit in source["source_units"] if unit.get("sku_id") == STANDARD
                    and "1 个手柄＋2 卷粘纸" in unit["text"])
    wrong = [mark(sku_unit, "retained", LARGE, "specifications") if row["unit"] == sku_unit["id"] else row
             for row in coverage]
    proofs.append(evidence(sku_unit, LARGE, "specifications", "crossed-evidence"))

    with pytest.raises(ContractError) as error:
        apply_coverage(source, rows, wrong, proofs)

    assert error.value.status == 422


def test_explicitly_shared_consumer_fact_is_not_saved_to_only_one_sku():
    source = prepared(("paragraph:1", "两个款式均可更换纸卷并复用手柄。"))
    unit = source["source_units"][0]
    rows = [product(STANDARD), product(LARGE)]
    wrong = [mark(unit, "knowledge", STANDARD, "knowledge_documents")]

    with pytest.raises(ContractError) as error:
        apply_coverage(source, rows, wrong, [])

    assert error.value.status == 422

    correct = [mark(unit, "knowledge", "*", "knowledge_documents")]
    saved, proofs, summary = apply_coverage(source, rows, correct, [])
    assert all("更换纸卷并复用手柄" in row["knowledge_documents"][0]["content"] for row in saved)
    assert {proof["sku_id"] for proof in proofs} == {STANDARD, LARGE}
    assert summary["knowledge_units"] == 1


@pytest.mark.parametrize("invalid", ["missing", "unknown_unit", "bad_field", "bad_sku", "mixed_targets", "false_metadata"])
def test_rejects_unaccounted_consumer_fact_or_invalid_destination(invalid):
    source = prepared(("paragraph:1", RETURN))
    unit = source["source_units"][0]
    row = product()
    coverage = {
        "missing": [],
        "unknown_unit": [{**mark(unit, "knowledge", STANDARD, "knowledge_documents"), "unit": "U999"}],
        "bad_field": [mark(unit, "retained", STANDARD, "unrecognized_return_policy")],
        "bad_sku": [mark(unit, "knowledge", "WRONG-SKU", "knowledge_documents")],
        "mixed_targets": [{**mark(unit, "knowledge"), "targets": [
            {"sku_id": STANDARD, "field": "knowledge_documents"},
            {"sku_id": "WRONG-SKU", "field": "knowledge_documents"},
        ]}],
        "false_metadata": [mark(unit, "excluded", reason="metadata")],
    }[invalid]
    original = deepcopy(row)

    with pytest.raises(ContractError) as error:
        apply_coverage(source, [row], coverage, [])

    assert error.value.status == 422 and row == original
    if invalid == "missing":
        assert error.value.code == "CATALOG_COVERAGE_INCOMPLETE"
