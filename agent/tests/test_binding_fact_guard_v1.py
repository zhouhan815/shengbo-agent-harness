"""Explicit contradiction checks do not turn audience hypotheses into specs."""
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.models import RunScope
from shengbo_prelive.runtime import RuntimeEngine
from test_runtime_business import env
from test_source_alias_bridge import accepted_sources, Product


@pytest.mark.parametrize("fields,price,material,reject", [
    ({"price": 129}, 999, "EVA", True),
    ({"material": "EVA"}, 129, "牛皮", True),
    ({"product_fields": {"price": 129}}, 999, "EVA", True),
    ({"price": "129.00", "material": " EVA "}, 129, "EVA", False),
    ({"price": None, "material": ""}, 999, "牛皮", False),
    ({"price": {"value": 129, "knowledge": "unknown"}}, 999, "EVA", False),
    ({"context": {"customer_budget_hypothesis": 129}}, 999, "EVA", False),
])
@pytest.mark.asyncio
async def test_same_source_binding_rejects_only_explicit_known_product_conflicts(env, fields, price, material, reject):
    engine, service = env
    run, scope, sources, aliases, _ = await accepted_sources(engine, service)
    product_fields = Product(name="事实绑定回归商品", price=price, material=material).model_dump()
    saved = await engine.business_command(scope, "save_product", {"schema_hash": service.schema()["schema_hash"], "patches": [], "fields": product_fields, "source_manifest_refs": sources,
        "field_dispositions": [{"path": "/" + key, "action": "set", "knowledge": "known" if value not in (None, "", []) else "unknown", "source_refs": sources, "reason": "保留原始资料引用"} for key, value in product_fields.items()]}, "single-new-product")
    ref = saved["data"]["product_ref"]
    saved_body = (await service.query("read_product", {"ref": ref}))["body"]
    branch = "explicit-facts-audience"
    engine.store.put("branch", {"id": branch, "run_id": run["id"], "role": "audience_scenes", "status": "RUNNING", "task": {
        "role": "audience_scenes", "instruction": "根据原资料分析顾客与使用场景", "product_ids": [], "source_refs": sources, "input_refs": []}})
    engine.submit_report(RunScope(run["id"], "fake-worker", "execute", "audience_scenes", branch), {
        "summary": "基于商家资料的顾客场景研究；假设尚需验证。", "fields": fields,
        "hypotheses": ["顾客可能偏好129元左右的价位，此项只是研究假设"], "source_refs": sources})
    parameters = {"product_ref": ref, "validation_note": "主模型已选择该来源报告，程序仍须核对明确事实。"}
    if reject:
        with pytest.raises(ContractError) as caught:
            await engine.bind_source_analysis(scope, [branch], parameters, "bind-checked-product")
        assert caught.value.code == "BINDING_REQUIRES_REPLAN"
        assert caught.value.details["field_conflicts"]
        assert not (await service.query("find_audience_research", {"ref": ref}))["items"]
    else:
        result = await engine.bind_source_analysis(scope, [branch], parameters, "bind-checked-product")
        assert result["model_requests"] == 0
        assert (await service.query("find_audience_research", {"ref": ref}))["items"][0]["compatibility"] == "exact"
    assert (await service.query("read_product", {"ref": ref}))["body"] == saved_body
    assert engine.store.get("branch", branch)["report"]["product_ids"] == []


def test_fact_guard_compares_boolean_zero_and_partial_specs_without_adding_facts():
    guard = RuntimeEngine._binding_explicit_fact_conflicts
    assert guard({"price": 0}, {"price": 129})
    assert guard({"price": False}, {"price": 0})
    assert not guard({"price": None, "material": "EVA"}, {"price": 129, "material": ""})
    assert not guard({"specifications": [{"name": "颜色", "value": "红色"}]}, {"specifications": [{"name": "颜色", "value": "红色"}, {"name": "尺码", "value": "L"}]})
    assert guard({"specifications": [{"name": "尺码", "value": "M"}]}, {"specifications": [{"name": "尺码", "value": "L"}]})
