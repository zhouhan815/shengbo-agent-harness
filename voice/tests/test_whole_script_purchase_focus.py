"""Focused whole-script wire/receipt tests, with no paid model calls."""
from copy import deepcopy
import json

import httpx
import pytest

from app.platform_rules import platform_policy
from app.prelive import module_generation as local, whole_script_generation as whole
from app.prelive.storage import BusinessError


FOCUS = {"audience": "没有固定球友的入门者", "decision": "试一次自己安排的挥拍", "scene": "周末在合适的户外练习"}
ACTIONS = [{"key": "selling_points", "action": "generate"}]


def whole_task(product, context=None):
    request, _ = whole.build_request(product, context or {}, ACTIONS, model="offline")
    return json.loads(request["messages"][1]["content"])


def test_new_whole_wire_uses_bounded_purchase_focus_and_scoped_sources():
    policy = platform_policy("douyin")["defaults"]
    product = {"name": "练习套装", "price": 23.58, "currency": "CNY", "sales_platform": "douyin",
               "specifications": [{"name": "组成", "value": "球拍和底座"}], **policy}
    source = {"records": [
        {"ref": {"kind": "product", "id": "selected"}, "body": product},
        {"ref": {"kind": "analysis", "id": "research"}, "body": {"role": "audience_scenes", "report": {
            "summary": "不进入写作的研究回顾", "fields": {
                "segments": [{"id": "g1", "who": "初学者", "need": "自己练习", "scene_ids": ["s1"]}],
                "scenes": [{"id": "s1", "situation": "周末户外", "task": "挥拍", "obstacle": "难约同伴", "status": "未知安装参数"}],
                "benefit_priorities": ["不进入写作的产品推断"]},
            "evidence": ["不进入写作的外部记录"], "claim_constraints": ["未知回弹路线"]}}},
    ]}
    original = deepcopy(source)
    task = whole_task(product, source)
    assert task["planning_contract"] == whole.PLANNING_CONTRACT
    assert "同一次请求" in task["writing_plan"]
    assert "同一次生成" in task["version_writing_contract"]["brief_declaration"]
    assert "明确要求多版对照" in task["version_writing_contract"]["brief_declaration"]
    focus = task["response_schema"]["$defs"]["PurchaseFocus"]["properties"]
    assert {name: field["maxLength"] for name, field in focus.items()} == {"audience": 80, "decision": 140, "scene": 100}
    serialized = json.dumps(task, ensure_ascii=False)
    assert all(value not in serialized for value in policy.values())
    assert all(value not in serialized for value in ("未知安装参数", "未知回弹路线", "不进入写作的"))
    report = task["context_bundle"]["records"][1]["body"]["report"]
    assert report["fields"]["scenes"] == [{"id": "s1", "situation": "周末户外", "task": "挥拍", "obstacle": "难约同伴"}]
    assert report["fields"]["segments"][0]["scene_ids"] == ["s1"]
    assert source == original
    part = local.build_request(product, {}, "selling_points", [], model="offline")
    assert all(value in part["messages"][1]["content"] for value in policy.values())
    assert "purchase_focus" not in part["messages"][1]["content"]


def test_short_expansion_wire_respects_explicit_length_and_keeps_one_exact_comparison():
    ref = {"kind": "script_revision", "id": "original"}
    text = "旧稿放在这里，仅用于确认表达不会连续重合。"
    sibling = "同批已完成的正文只在保存前查重。"
    product = {"name": "练习套装", "price": 99}
    context = {"original_script": {"ref": ref, "body": {"raw_text": text},
                "modules": {"selling_points": {"sentences": [{"text": text}]}}},
               "same_product_existing_scripts": [
                   {"ref": ref, "modules": {"selling_points": [text]}},
                   {"ref": {"kind": "script_revision", "id": "sibling"}, "modules": {"selling_points": [sibling]}},
               ],
               "expansion_expression": {"contract": "expansion-expression-v1", "expression_direction": "按场景讲"}}
    actions = [{**ACTIONS[0], "instruction": "每版约三百到四百五十个中文口播字。"}]
    wire, _ = whole.build_request(product, context, actions, model="offline")
    system, body = (row["content"] for row in wire["messages"])
    task = json.loads(body)
    assert task["length_goal"]["spoken_characters_range"] == [300, 450]
    assert "八至十分钟" not in system and "300–450" in system
    assert body.count(text) == 1
    assert sibling not in body
    assert context["original_script"]["body"]["raw_text"] == text
    ordinary, _ = whole.build_request(product, {}, actions, model="offline")
    assert json.loads(ordinary["messages"][1]["content"])["length_goal"]["spoken_characters_range"] == [1920, 2400]


def test_confirmed_merchant_terms_and_unstructured_research_are_preserved():
    platform_default = platform_policy("douyin")["defaults"]["shipping_time"]
    product = {"name": "练习套装", "price": 23.58, "currency": "CNY", "sales_platform": "douyin",
               "shipping_time": platform_default, "_shipping_overridden": True, "return_policy": "本商家明确约定的退换条件。"}
    report = {"summary": "这是该报告唯一的可用顾客结论。"}
    context = {"records": {"source": {"ref": {"kind": "analysis"}, "body": {"role": "audience_scenes", "report": report}}}}
    task = whole_task(product, context)
    assert task["effective_product_snapshot"] == product
    assert task["context_bundle"]["records"]["source"]["body"]["report"] == report


@pytest.mark.parametrize(('text', 'reject'), [
    ('给保温杯倒水时，这盏灯就在旁边。', False),
    ('从保温壶里倒出热水时，这盏灯就在旁边。', False),
    ('给保温杯倒水时，这盏灯摸着也不烫手。', True),
    ('你把保温袋里的餐具一样样拿出来，摆在桌面上。', False),
    ('从保温箱里取出饮料，再点亮这盏灯。', False),
    ('把保温袋里的餐具拿出来，这盏灯能为食物保温。', True),
    ('这盏灯带保温功能。', True),
    ('这款是保温杯，打开还能当营地灯。', True),
])
def test_scene_props_do_not_assert_thermal_performance_for_target_product(text, reject):
    from app.copywriting_quality import content_issues

    product = {'name': '营地灯', 'selling_points': '平底座可放在桌面。'}
    issues = content_issues(product, [{'sentences': [{'text': text}]}], require_coverage=False)
    assert any('保温' in issue or '防烫' in issue for issue in issues) is reject


@pytest.mark.parametrize(("product", "text", "expected_code"), [
    ({"selling_points": "鞋底柔软"}, "喜欢这种脚感的，穿上就不想脱。", None),
    ({"selling_points": "鞋子轻便"}, "喜欢这种上脚没什么重量感的，来看看这一款。", None),
    ({"selling_points": "鞋底柔软"}, "上脚没什么重量感。", "legacy_guard"),
    ({"selling_points": "用完可折叠收起"}, "折起来收着不占地方。", None),
    ({"selling_points": "用完可折叠收起"}, "折叠后，收起来也不占地方。", None),
    ({"selling_points": "用完可折叠收起"}, "折好往装备角落一放，不占地方。", None),
    ({"selling_points": "用完可折叠收起"}, "折好后收回家。这份不占地方、好归位的感觉我很喜欢。", None),
    ({"selling_points": "用完可折叠收起"}, "桌子折叠后，重新展开也不占地方。", "legacy_guard"),
    ({"selling_points": "用完可折叠收起"}, "折好往后备箱一放，收拾起来很方便。", "UNSUPPORTED_STORAGE_FIT"),
    ({"selling_points": "用完可折叠收起"}, "直接放车后备箱角落就行。", "UNSUPPORTED_STORAGE_FIT"),
    ({"selling_points": "用完可折叠收起"}, "小轿车的后座，都可以给它找到一个小空间。", "UNSUPPORTED_STORAGE_FIT"),
    ({"selling_points": "可放进后备箱"}, "不论SUV还是小轿车，折好都能放进后备箱。", "UNSUPPORTED_STORAGE_FIT"),
    ({"selling_points": "可放进后备箱"}, "折好可以放进后备箱。", None),
    ({"specifications": [{"name": "收纳", "value": "可放进后备箱"}]}, "折好可以放进后备箱。", None),
    ({"knowledge": "后备箱空间足够时，可放进后备箱。"}, "直接放后备箱就行。", "UNSUPPORTED_STORAGE_FIT"),
    ({"knowledge": "需先核对尺寸，未确认能放进小轿车后备箱。"}, "它可以放进小轿车后备箱。", "UNSUPPORTED_STORAGE_FIT"),
    ({"selling_points": "用完可折叠收起"}, "要放后备箱，先核对可用空间。", None),
    ({"selling_points": "用完可折叠收起"}, "如果你量过尺寸，确认放得下，折好可以装进后备箱。", None),
    ({"selling_points": "用完可折叠收起"}, "餐具可以放进后备箱，桌子也可以折起来。", None),
    ({"selling_points": "用完可折叠收起"}, "桌子展开也不占地方。", "legacy_guard"),
    ({"knowledge": "桌面可放保温袋。"}, "把保温袋放在桌上，桌子还能给食物保温。", "UNSUPPORTED_INSULATION"),
    ({"knowledge": "搭配的保温袋有保温功能。"}, "这张桌子有保温功能。", "UNSUPPORTED_INSULATION"),
    ({"selling_points": "桌面可以摆餐具。"}, "把保温袋放在桌上，碗筷集中放，取起来可顺手了。", None),
    ({"specifications": [{"name": "保温时长", "value": "6小时"}]}, "这款可以保温。", None),
    ({"specifications": [{"name": "保温时长", "value": "未知"}]}, "这款可以保温。", "UNSUPPORTED_INSULATION"),
    ({"specifications": [{"name": "保温时长", "value": "6小时"}]}, "这款拿在手里不烫手。", "UNSUPPORTED_TOUCH_SAFETY"),
    ({"shipping_time": "48小时内发货"}, "现在下单，下次露营就能用上。", "ORDER_TO_USE_PROMISE"),
    ({"shipping_time": "48小时内发货"}, "收到后，下次露营可以带上它。", None),
])
def test_marketing_benefits_and_props_are_distinct_from_verifiable_claims(product, text, expected_code):
    from app.prelive.storage import BusinessError

    product = {"name": "样品", "price": 129, **product}
    lines = ["来，接着看一个使用场景。", *(line + "。" for line in text.rstrip("。").split("。"))]
    raw = {"module_key": "selling_points", "sentences": [
        {"display_text": line, "spoken_text": line, "kind": "engagement", "fact_ids": []}
        for line in lines]}
    if expected_code is None:
        assert "".join(local.validate_output(raw, product, "selling_points")[1:]) == text
    else:
        with pytest.raises(BusinessError) as error:
            local.validate_output(raw, product, "selling_points")
        assert error.value.code == "GENERATED_MODULE_NEEDS_REVIEW"
        if expected_code != "legacy_guard":
            finding, = error.value.details["sentence_findings"]
            assert (finding["sentence_index"], finding["code"]) == (1, expected_code)


@pytest.mark.parametrize("focus", [FOCUS, None])
@pytest.mark.asyncio
async def test_one_provider_response_archives_optional_focus_without_speaking_it(focus):
    import test_whole_script_generation_v1 as base

    harness = base.WholeServiceTests()
    await harness.asyncSetUp()
    sent = []
    try:
        def respond(request):
            task = json.loads(json.loads(request.content)["messages"][1]["content"])
            sent.append(task)
            envelope = base.envelope(task["requested_module_keys"])
            content = json.loads(envelope["choices"][0]["message"]["content"])
            if focus is not None:
                content["purchase_focus"] = focus
            envelope["choices"][0]["message"]["content"] = json.dumps(content, ensure_ascii=False)
            return httpx.Response(200, json=envelope)

        harness.service.adapter.module_transport = httpx.MockTransport(respond)
        prep, job = await harness.setup_job()
        await harness.service.drain_jobs()
        current = await harness.read_job(job)
        assert current["status"] == "succeeded"
        assert len(sent) == 1
        attempt, = current["items"][0]["provider_attempts"]
        assert attempt["output"].get("purchase_focus") == focus
        with harness.service.store.transaction() as db:
            archive = harness.service.store.exact(db, attempt["provider_response_ref"])["body"]["response"]
            assert archive["planning_contract"] == whole.PLANNING_CONTRACT
            assert archive["authoring_fact_ids"] == sent[0]["allowed_fact_ids"]
            assert json.loads(archive["content"]).get("purchase_focus") == focus
        state = (await harness.service.query("read_preparation", {"preparation_id": prep["id"]}))["body"]
        saved = await harness.service.query("read_script", {"ref": state["script_refs"][0]})
        assert all(text not in json.dumps(saved, ensure_ascii=False) for text in FOCUS.values())
    finally:
        await harness.asyncTearDown()


@pytest.mark.parametrize("receipt_update, phase", [
    ({"planning_contract": "future"}, "planning_contract"),
    ({"authoring_fact_ids": ["PRICE"]}, "fact_references"),
])
def test_new_receipt_rejects_unknown_contract_and_unpresented_facts(receipt_update, phase):
    product = {"name": "练习套装", "price": 23.58, "currency": "CNY"}
    value = {"purchase_focus": FOCUS, "modules": [{"module_key": "selling_points", "sentences": [
        {"text": "看看练习套装。", "kind": "product_claim", "fact_ids": ["NAME"]}]}]}
    receipt = {"protocol": whole.PROTOCOL, "text_contract": whole.TEXT_CONTRACT, "planning_contract": whole.PLANNING_CONTRACT,
               "finish_reason": "stop", "content": json.dumps(value, ensure_ascii=False), **receipt_update}
    with pytest.raises(BusinessError) as caught:
        whole.finish_received_script(receipt, product, ACTIONS)
    assert caught.value.details["validation_stage"] == phase
