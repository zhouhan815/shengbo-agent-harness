"""Catalog Markdown keeps exact SKU rows and separates the campaign lane."""
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.materials import MaterialStore
from shengbo_prelive.store import RuntimeStore
from test_document_import import DocumentModel, _draft, _run
from test_runtime_business import env


SOURCE = """# 海湾收纳袋商品资料

品牌 海湾。商品类目 收纳袋。币种 CNY，人民币。

| 款式 | SKU | 日常售价 | 展开尺寸 | 净重 |
| --- | --- | ---: | --- | ---: |
| 轻便款 | HW-B06 | 39.90 元 | 40 × 30 厘米 | 160 克 |
| 加宽款 | HW-B09 | 49.90 元 | 48 × 38 厘米 | 220 克 |

## 双十一直播活动

### 活动价

本场活动价，HW-B06 每件 35.90 元，HW-B09 每件 45.90 元。
"""


def test_markdown_span_reader_checks_quotes_against_exact_original(tmp_path):
    runtime = RuntimeStore(tmp_path / "runtime.sqlite")
    store = MaterialStore(tmp_path, runtime)
    original = SOURCE.replace("\n", "\r\n").encode()
    ref = store.accept("c1", "商品.md", "text/markdown", original)["material_ref"]

    read = store.read(ref, "c1")
    rows = [row for row in read["manifest"]["locators"] if row["kind"] == "table_row"]
    standard, large = rows[1:]
    assert store.blob_bytes(ref, "c1") == original
    assert store.read(ref, "c1", "text:1")["text"] == original.decode()
    assert store.read(ref, "c1", standard["id"])["text"] == standard["text"]
    assert store.validate_locators(ref, "c1", [standard["id"], large["id"]], quote_checks=[
        (standard["id"], "39.90 元"), (standard["id"], "49.90 元"), (large["id"], "45.90 元")
    ])["quote_mismatch_indexes"] == [1, 2]
    with pytest.raises(ContractError):
        store.validate_locators(ref, "c1", ["md:0:1"])
    with pytest.raises(ContractError):
        store.validate_locators(ref, "foreign", [standard["id"]])


@pytest.mark.asyncio
async def test_markdown_table_first_response_saves_daily_price_and_routes_campaign_separately(env):
    engine, service = env
    await engine.start()

    def response(inputs):
        material = inputs["source_material"]
        units = inputs["source_units"]
        products, coverage = [], {}
        for sku, variant, price, size, weight in [("HW-B06", "轻便款", 39.9, "40 × 30 厘米", "160 克"),
                                                   ("HW-B09", "加宽款", 49.9, "48 × 38 厘米", "220 克")]:
            scoped = [unit for unit in units if unit.get("sku_id") == sku]
            assert len(scoped) == 5
            types = {"SKU": "sku_id", "款式": "name", "日常售价": "price"}
            for unit in scoped:
                coverage[unit["id"]] = sku + "|" + types.get(unit["column"], "specifications")
            row = next(item for item in material if item["location"] == scoped[0]["location"])
            currency = next(item for item in material if "币种 CNY，人民币。" in item["text"])
            products.append({"fields": {"name": "海湾收纳袋 " + variant, "sku_id": sku, "price": price,
                "currency": "CNY", "specifications": [{"name": "展开尺寸", "value": size}, {"name": "净重", "value": weight}]},
                "evidence": [{"location": row["location"], "quote": row["text"], "fields": ["name", "sku_id", "price", "specifications"]},
                             {"location": currency["location"], "quote": "币种 CNY，人民币。", "fields": ["currency"]}]})
        for unit in units:
            if unit["text"] in {"币种 CNY", "人民币"}:
                coverage[unit["id"]] = "*|currency"
            if unit["text"] in {"品牌 海湾", "商品类目 收纳袋"}:
                coverage[unit["id"]] = "*|specifications"
            assert not any(amount in unit["text"] for amount in ("35.90", "45.90"))
        activity = next(item for item in material if "每件 35.90 元" in item["text"])
        return {"products": products, "coverage": coverage, "questions": [], "session": {"prices": [
            {"location": activity["location"], "quote": "HW-B06 每件 35.90 元", "sku": "HW-B06", "amount": 35.9},
            {"location": activity["location"], "quote": "HW-B09 每件 45.90 元", "sku": "HW-B09", "amount": 45.9},
        ]}}

    engine.model = DocumentModel(response)
    saved = await _run(engine, SOURCE, filename="商品.md", answers={"catalog_intake_approval": "save_all"})

    assert engine.model.calls == 1
    products = [(await service.query("read_product", {"product_id": product_id}))["body"]
                for product_id in saved["product_ids"]]
    assert {row["sku_id"]: row["price"] for row in products} == {"HW-B06": 39.9, "HW-B09": 49.9}
    draft = await _draft(service, saved)
    assert {key: row["price"] for key, row in draft["commerce"]["product_overrides"].items()} == {
        product["id"]: 35.9 if product["sku_id"] == "HW-B06" else 45.9 for product in products}
