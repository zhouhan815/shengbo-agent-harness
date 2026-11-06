"""Known daily quotes do not turn a pure preview into an incomplete offer.

Offline boundary tests only: no real provider/TTS call or original store write.
The real RC14 R02 public rejected sentences are preserved verbatim below.
"""
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from app.copywriting_quality import content_issues, _daily_quote_detection_view
from app.prelive import module_generation as generation
from app.prelive.storage import BusinessError


def product(**changes):
    value = {"id": "rc15-offline", "name": "绯笺柔雾唇膏02玫瑰豆沙色", "currency": "CNY",
        "price": 129.0, "promo_price": 99.0, "selling_points": "柔雾妆效，旋转管身。",
        "specifications": [{"name": "净含量", "value": "单支3.2克"}],
        "promotion": "本场99元，先领满180减20券，每单限用一张，可以叠本场价，不与其他券或折扣叠加。",
        "_price_quote": {"daily_price": 129.0, "base_price": 99.0, "final_price": 99.0, "confirmed": True}}
    value.update(changes)
    return value


REAL_REJECTED = {"module_key": "product_intro", "sentences": [
    {"display_text": "这款是绯笺柔雾唇膏，色号02玫瑰豆沙色，属于普通彩妆唇膏。",
     "spoken_text": "这款是绯笺柔雾唇膏，色号零二玫瑰豆沙色，属于普通彩妆唇膏。",
     "kind": "product_claim", "fact_ids": ["NAME", "DETAILS"]},
    {"display_text": "它是旋转管身，单支3.2克，日常价129元；本场价格另有专场优惠，稍后单独说明。",
     "spoken_text": "它是旋转管身，单支三点二克，日常价一百二十九元；本场价格另有专场优惠，稍后单独说明。",
     "kind": "product_claim", "fact_ids": ["NAME", "DETAILS", "PRICE"]},
]}


def issues(text, value=None, *, coverage=False):
    return content_issues(product() if value is None else value,
                          [{"sentences": [{"text": text}]}], require_coverage=coverage)


def test_real_rejected_intro_now_passes_without_rewriting_or_reciting_coupon_conditions():
    raw = deepcopy(REAL_REJECTED)
    before = deepcopy(raw)
    assert generation.validate_output(raw, product(), "product_intro") == [s["spoken_text"] for s in raw["sentences"]]
    assert raw == before
    for field in ("display_text", "spoken_text"):
        assert content_issues(product(), [{"sentences": [{"text": s[field]} for s in raw["sentences"]]}],
                              require_coverage=False) == []


@pytest.mark.parametrize("text", [
    "日常价129元；优惠规则稍后单独说明。",
    "日常价一百二十九元；本场价格另有专场优惠，稍后单独说明。",
    "日常售价是129.00元。优惠规则后面集中介绍。",
    "原价为一百二十九元，优惠细则后面说明。",
    "日常价129元，优惠规则稍后介绍。",
])
def test_verified_explicit_daily_quote_and_clear_preview_need_no_full_offer(text):
    assert issues(text) == []


@pytest.mark.parametrize("text", [
    "日常价129元，本场99元；优惠规则后面说明。",
    "日常价一百二十九元，本场九十九元；优惠规则后面说明。",
    "日常价129元；满180减20，优惠细则稍后介绍。",
    "日常价129元；优惠后面再讲，现在买两支只要178元。",
    "日常价129元；优惠细则稍后讲，现在先领券下单。",
    "日常价129元，到手99元；优惠后面说明。",
    "日常价129元；领券就能便宜，优惠细则后面讲。",
    "日常价129元，买两支能省钱；优惠细则后面说。",
    "日常价129元，可以少花钱；优惠细则后面说。",
    "日常价129元，打八折；优惠后面再讲。",
    "日常价129元，券后99元；优惠细则后面讲。",
    "本场129元；优惠细则后面说明。",
    "日常价99元；优惠规则后面说明。",
    "商品价格129元；优惠规则后面说明。",
    "日常价129元；这张优惠券可以和所有折扣叠加。",
    "日常价129元；现在有优惠，喜欢就选。",
])
def test_concrete_benefits_and_unverified_or_non_daily_quotes_remain_strict(text):
    assert any("叠加" in value for value in issues(text))


@pytest.mark.parametrize("price", [None, True, "129", float("nan"), float("inf")])
def test_missing_or_untyped_daily_authority_does_not_exempt_a_quote(price):
    value = product(price=price)
    text = "日常价129元；优惠规则后面说明。"
    assert _daily_quote_detection_view(value, text) == text
    assert any("叠加" in issue for issue in issues(text, value))


def test_conflicting_quote_or_foreign_currency_never_gets_daily_cny_exemption():
    text = "日常价129元；优惠规则后面说明。"
    for value in [product(_price_quote={"daily_price": 130}), product(currency="USD"), product(currency="TWD")]:
        assert _daily_quote_detection_view(value, text) == text
        assert any("叠加" in issue for issue in issues(text, value))


@pytest.mark.parametrize("price,spoken", [(129.5, "一百二十九点五"), (0.01, "零点零一"), (1001, "一千零一")])
def test_known_standard_spoken_daily_amounts_are_verified_not_arbitrarily_removed(price, spoken):
    value = product(price=price, _price_quote={"daily_price": price})
    assert issues(f"日常价{spoken}元；优惠细则后面介绍。", value) == []
    assert any("叠加" in issue for issue in issues(f"日常价{spoken}元，本场99元；优惠细则后面讲。", value))


def test_legacy_whole_script_and_true_offer_gates_still_need_nonstackable_condition():
    assert any("叠加" in issue for issue in issues("日常价129元；优惠细则后面说明。", coverage=True))
    complete = "本场99元，先领满180减20券，每单限用一张，不能与其他券或折扣叠加。"
    assert issues(complete) == []


def test_intro_boundary_is_guidance_only_and_other_cache_identities_remain_stable():
    assert hashlib.sha256(generation.SYSTEM.encode()).hexdigest() == "9da25be062a955b32f9597923d500667cb3e15c9f7c064a70c5bda136572367a"
    expected_unchanged = {"opening_long": "216323ada5c416af", "opening_short": "ab534e305c17c9a9",
        "selling_points": "71c0b1b8466f800b", "campaign": "2fc3127842895898",
        "cta": "b1fb08b56d57441f", "transition": "9da25be062a955b3"}
    for key, revision in expected_unchanged.items():
        assert generation.module_prompt_revision(key) == revision
    assert generation.module_prompt_revision("product_intro") != "22f94f7216766512"
    request = generation.build_request(product(), {"module_key": "product_intro"}, "product_intro", [], model="offline")
    task = json.loads(request["messages"][1]["content"])
    assert "不是礼盒套装、没有礼盒等商品否定事实" in task["module_spec"]
    assert "简介不做促销预告" in task["module_spec"]


@pytest.mark.asyncio
async def test_scripted_real_rejected_content_passes_once_with_usage_and_raw_public_content_preserved():
    raw = deepcopy(REAL_REJECTED)
    content = json.dumps(raw, ensure_ascii=False)
    observations, calls = [], []

    async def transport(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "rc15-offline", "choices": [{"finish_reason": "stop", "message": {
            "content": content, "reasoning_content": "private-never-log"}}],
            "usage": {"prompt_tokens": 61056, "completion_tokens": 173, "total_tokens": 61229}})

    value = await generation.generate_one_module(product(), {"module_key": "product_intro"}, "product_intro", [],
        api_key="offline-only-key", transport=httpx.MockTransport(transport),
        response_observer=lambda receipt: observations.append(deepcopy(receipt)))
    assert len(calls) == len(observations) == 1
    assert observations[0]["content"] == content
    assert observations[0]["usage"] == value["usage"] == {"prompt_tokens": 61056, "completion_tokens": 173, "total_tokens": 61229}
    assert value["sentences"] == [s["spoken_text"] for s in raw["sentences"]]
    assert value["sentence_details"] == raw["sentences"]
    assert "private-never-log" not in json.dumps(observations)
    assert "offline-only-key" not in json.dumps(observations)


@pytest.mark.asyncio
async def test_true_offer_rejection_still_records_received_usage_before_validation():
    value = {"module_key": "product_intro", "sentences": [{"display_text": "日常价129元，本场99元，优惠规则后面说明。",
        "spoken_text": "日常价一百二十九元，本场九十九元，优惠规则后面说明。", "kind": "product_claim", "fact_ids": ["PRICE", "PROMOTION"]}]}
    observations = []

    async def transport(_):
        return httpx.Response(200, json={"id": "rc15-offline-reject", "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(value, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}})

    with pytest.raises(BusinessError) as error:
        await generation.generate_one_module(product(), {"module_key": "product_intro"}, "product_intro", [],
            api_key="offline-only-key", transport=httpx.MockTransport(transport),
            response_observer=lambda receipt: observations.append(deepcopy(receipt)))
    assert error.value.code == "GENERATED_MODULE_NEEDS_REVIEW"
    assert len(observations) == 1
    assert observations[0]["usage_state"] == "known"
    assert observations[0]["usage"]["total_tokens"] == 100
