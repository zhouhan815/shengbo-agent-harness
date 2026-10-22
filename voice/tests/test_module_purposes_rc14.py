"""Purpose budgets are request guidance, never clipping or another paid pass.

Scripted HTTP proves the request/validation/accounting boundary only. It does
not establish that a real model will meet the suggested character budgets.
"""
from copy import deepcopy
import json

import httpx
import pytest

from app.prelive import module_generation as generation
from app.prelive.storage import BusinessError, digest


ACTIVE_PURPOSES = {
    "opening_long": "100–170",
    "opening_short": "30–60",
    "product_intro": "20–60",
    "selling_points": "1400–1700",
    "campaign": "180–280",
    "cta": "450–550",
}


def product(promotion="本场99元，先领满180减20券，每单一张，可与本场价叠加，不与其他券或折扣叠加。"):
    return {"id": "rc14-product", "name": "测试日用商品", "price": 129, "currency": "CNY",
            "promo_price": 99, "selling_points": "便携。", "promotion": promotion,
            "knowledge": "按需选择；图片尚待提供，不能据此说商品没有包装。",
            "specifications": [], "sale_status": "available"}


def module_output(key, lines, *, facts=None):
    return {"module_key": key, "sentences": [{"display_text": text, "spoken_text": text,
        "kind": "engagement" if facts is None else "product_claim", "fact_ids": facts or []} for text in lines]}


def provider_response(value):
    return {"id": "offline-rc14", "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(value, ensure_ascii=False), "reasoning_content": "private-not-an-audit-field"}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}}


@pytest.mark.parametrize("key,budget", ACTIVE_PURPOSES.items())
def test_actual_request_exposes_only_requested_purpose_without_losing_inputs(key, budget):
    item = product()
    context = {"module_key": key, "generation_instruction": "必要条件较多时完整保留，不为字数删除。",
               "records": [{"ref": {"kind": "analysis", "id": "offline-report"},
                            "body": {"summary": "顾客场景仅为假设"}}], "unknowns": ["没有真实试色图片"]}
    previous = [{"spoken_text": "旧模块公开正文。"}]
    original = deepcopy((item, context, previous))
    body = generation.build_request(item, context, key, previous, model="offline-model")
    task = json.loads(body["messages"][1]["content"])
    assert task["requested_module_key"] == key
    assert budget in task["module_spec"]
    assert "参考" in task["module_spec"]
    assert task["context_bundle"] == context
    assert task["previous_module"] == previous
    assert task["effective_product_snapshot"] == item
    assert task["facts"] == generation.fact_catalog(item)
    expected_schema = generation.ModuleOutput.model_json_schema()
    expected_schema["$defs"]["ModuleSentence"]["properties"]["fact_ids"]["items"] = {
        "type": "string", "enum": [fact["id"] for fact in generation.fact_catalog(item)]}
    assert task["response_schema"] == expected_schema
    assert (item, context, previous) == original
    assert body["messages"][0]["content"] == generation.SYSTEM
    assert "促单只做简短行动建议" not in body["messages"][0]["content"]
    assert not any(field in task for field in ("hard_character_limit", "truncate_after", "must_fill_budget"))


def test_claim_count_guidance_never_rewrites_authoritative_source_or_invents_typed_fields():
    item = product()
    task = json.loads(generation.build_request(item, {"module_key": "campaign"}, "campaign", [],
                                              model="offline-model")["messages"][1]["content"])
    assert task["effective_product_snapshot"]["promotion"] == item["promotion"]
    assert next(fact["text"] for fact in task["facts"] if fact["id"] == "PROMOTION") == item["promotion"]
    assert "先领券也不证明只能领一张" in task["module_spec"]
    assert "资料明确给出的合法领取限制仍应照实保留" in task["module_spec"]
    assert not any(field in task for field in ("claim_limit", "per_order_use_limit", "coupon_claim_policy"))


def test_neutral_cta_needs_neither_quotation_nor_minimum_characters():
    value = module_output("cta", ["合适再购买。"])
    assert generation.validate_output(value, product(), "cta") == ["合适再购买。"]


def test_complete_required_campaign_longer_than_reference_is_preserved_verbatim():
    applicable = "、".join(f"{index:03d}号商品" for index in range(45))
    line = (f"本场99元，先领满180减20券，每单限用一张，可与本场99元价叠加，不能与其他券或折扣叠加，"
            f"仅适用于{applicable}，其他商品不参加。")
    assert len(line) > 240
    value = module_output("campaign", [line], facts=["PROMOTION"])
    assert generation.validate_output(value, product(line), "campaign") == [line]
    assert value["sentences"][0]["spoken_text"] == line


def test_existing_offer_fact_gate_and_short_sentence_cap_are_unchanged():
    with pytest.raises(BusinessError) as error:
        generation.validate_output(module_output("campaign", ["本场99元，先领券满180减20。"], facts=["PROMOTION"]),
                                   product(), "campaign")
    assert error.value.code == "GENERATED_MODULE_NEEDS_REVIEW"
    assert any("叠加" in issue for issue in error.value.details["issues"])
    with pytest.raises(BusinessError) as error:
        generation.validate_output(module_output("opening_short", ["先看看。", "按需选择。", "合适再考虑。"]),
                                   product(), "opening_short")
    assert error.value.code == "SHORT_MODULE_TOO_LONG"


@pytest.mark.asyncio
async def test_scripted_rejection_keeps_original_public_usage_before_content_validation():
    raw = module_output("campaign", ["本场99元，先领券满180减20。"], facts=["PROMOTION"])
    observed, requests = [], []

    async def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=provider_response(raw))

    async def observer(receipt):
        observed.append(deepcopy(receipt))

    with pytest.raises(BusinessError) as error:
        await generation.generate_one_module(product(), {"module_key": "campaign"}, "campaign", [],
            api_key="offline-only-key", transport=httpx.MockTransport(transport), response_observer=observer)
    assert error.value.code == "GENERATED_MODULE_NEEDS_REVIEW"
    assert len(requests) == len(observed) == 1
    assert observed[0]["content"] == json.dumps(raw, ensure_ascii=False)
    assert observed[0]["usage"] == {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}
    assert observed[0]["usage_state"] == "known"
    assert "private-not-an-audit-field" not in json.dumps(observed)
    assert "offline-only-key" not in json.dumps(observed)
    assert observed[0]["prompt_revision"] == generation.module_prompt_revision("campaign")
    assert observed[0]["input_hash"] == digest(requests[0])


@pytest.mark.asyncio
async def test_long_necessary_campaign_is_one_scripted_request_with_no_clipping_or_second_pass():
    applicable = "、".join(f"{index:03d}号商品" for index in range(45))
    line = f"本场99元，先领满180减20券，每单限用一张，不能与其他券或折扣叠加，仅适用于{applicable}，其他商品不参与。"
    raw = module_output("campaign", [line], facts=["PROMOTION"])
    requests, observations = [], []

    async def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=provider_response(raw))

    result = await generation.generate_one_module(product(line), {"module_key": "campaign"}, "campaign", [],
        api_key="offline-only-key", transport=httpx.MockTransport(transport),
        response_observer=lambda receipt: observations.append(deepcopy(receipt)))
    assert len(requests) == len(observations) == 1
    assert requests[0]["max_tokens"] == 6000
    assert result["sentences"] == [line]
    assert result["sentence_details"][0]["spoken_text"] == line
    assert result["usage"] == observations[0]["usage"]
