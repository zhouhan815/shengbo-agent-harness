"""Long-version goal is observable guidance, never another automatic paid pass.

Mocked responses validate transport/data contracts, not real marketing quality.
"""
from copy import deepcopy
import json

import httpx
import pytest

from app.prelive.script_duration import length_goal, script_duration_metrics, spoken_character_count
from app.prelive.whole_script_generation import build_request, generate_one_script


KEYS = ["opening_short", "product_intro", "selling_points", "cta"]


def actions():
    return [{"key": key, "action": "generate", "instruction": "按本版需求切入组织完整正文。"} for key in KEYS]


def product():
    return {"id": "duration-test", "name": "练习器材", "price": 23.58, "currency": "CNY",
            "selling_points": "可用于单人回弹练习。"}


def test_spoken_count_excludes_spacing_punctuation_and_timing_is_explicitly_estimated():
    assert spoken_character_count("练习，ABC 23。\n") == 7
    stats = script_duration_metrics({"selling_points": {"sentences": ["练" * 2160 + "。"]}})
    assert stats["spoken_characters"] == 2160
    assert stats["estimated_seconds"] == 540
    assert stats["estimated_seconds_range"] == [498.462, 589.091]
    assert stats["measurement_kind"] == "text_estimate_not_measured_audio"
    assert stats["duration_goal_status"] == "within_target"
    assert stats["warnings"] == []


def test_short_opening_keeps_full_version_goal_and_useful_body_budget():
    short = length_goal(KEYS)
    long = length_goal(["opening_long", *KEYS[1:]])
    assert short["target_minutes"] == long["target_minutes"] == [8, 10]
    assert short["spoken_characters_range"] == long["spoken_characters_range"] == [1920, 2400]
    assert short["module_spoken_character_targets"]["selling_points"] > 1200
    assert 450 <= short["module_spoken_character_targets"]["cta"] <= 650
    assert 450 <= long["module_spoken_character_targets"]["cta"] <= 650
    assert short["policy_revision"] == "script-duration-8-10min-v4-scenario-allocation"
    assert short["module_spoken_character_targets"]["opening_short"] < long["module_spoken_character_targets"]["opening_long"]
    assert "campaign" not in short["module_spoken_character_targets"]


def test_required_intro_slot_is_one_line_budget_without_shortening_full_version():
    for opening in ("opening_long", "opening_short"):
        plan = length_goal([opening, "product_intro", "selling_points", "cta"])
        assert 20 <= plan["module_spoken_character_targets"]["product_intro"] <= 60
        assert plan["module_spoken_character_targets"]["selling_points"] > 1300
        assert plan["spoken_characters_range"] == [1920, 2400]
        assert plan["enforcement"] == "advisory_only_no_truncation_no_automatic_retry"


def test_existing_versions_and_planned_angles_reach_one_complete_request_losslessly():
    old = {"ref": {"id": "saved-v1", "revision": 1}, "modules": {"selling_points": ["既有的消费者场景正文，不可只传编号。"]}}
    context = {"same_product_existing_scripts": [old], "script_variant_briefs": [
        {"variant_id": "v2", "module_instructions": {"selling_points": "新手自主练习切入。"}},
        {"variant_id": "v3", "module_instructions": {"selling_points": "课外延伸练习切入。"}}],
        "variant": {"variant_id": "v2", "version_number": 2}, "recap_refs": []}
    original = deepcopy(context)
    body, _ = build_request(product(), context, actions(), model="deepseek-flash")
    task = json.loads(body["messages"][1]["content"])
    assert task["context_bundle"] == original
    assert context == original
    assert task["length_goal"]["target_minutes"] == [8, 10]
    assert task["version_writing_contract"]["opening_alone_is_not_a_new_version"] is True
    assert task["requested_module_keys"] == KEYS
    assert body["max_tokens"] >= 16000
    assert "促单简短" not in body["messages"][0]["content"]
    assert "不播无活动状态" in body["messages"][0]["content"]


def test_whole_and_local_requests_separate_consumer_copy_from_missing_data_and_old_audit_voice():
    from app.prelive import module_generation
    context = {"same_product_existing_scripts": [{"modules": {"selling_points": [
        "资料里没有写耐热数据，所以我不下结论。"]}}], "unknowns": ["具体尺寸尚待提供"]}
    original = deepcopy(context)
    whole, _ = build_request(product(), context, actions(), model="offline-model")
    local = module_generation.build_request(product(), {"module_key": "selling_points", **context},
        "selling_points", ["我这里不替大家编步骤。"], model="offline-model")
    for body in (whole, local):
        system = body["messages"][0]["content"]
        assert module_generation.CONSUMER_VOICE_GUIDANCE in system
        assert "旧稿只用于找重复、对照版本差异" in system
        assert "示例只说明表达方法，不提供本商品事实" in system
        assert "好例：喜欢这款、也有相应需要的朋友，可以按自己的需要选择。" not in system
        assert "只有已确认且确实影响购买、使用或履约的限制" in system
    # Missing information and immutable old scripts stay available for reasoning;
    # style improvements do not silently rewrite or delete the source snapshot.
    assert json.loads(whole["messages"][1]["content"])["context_bundle"] == original
    assert context == original


@pytest.mark.asyncio
async def test_short_valid_output_is_kept_with_duration_warning_and_exactly_one_provider_attempt():
    requests, observations = [], []
    words = {"opening_short": "想试试网球练习吗？", "product_intro": "这是一套练习器材。",
             "selling_points": "可用于单人回弹练习。", "cta": "想开始练习的朋友可以了解这套器材。"}
    output = {"modules": [{"module_key": key, "sentences": [{"text": text,
        "kind": "product_claim" if key in {"product_intro", "selling_points"} else "engagement",
        "fact_ids": ["NAME" if key == "product_intro" else "SELLING"] if key in {"product_intro", "selling_points"} else []}]}
        for key, text in words.items()]}
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "duration-offline", "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(output, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}})
    result = await generate_one_script(product(), {}, actions(), api_key="offline-test-only",
        transport=httpx.MockTransport(handler), response_observer=observations.append)
    stats = result["validation_metrics"]["script_duration"]
    assert len(requests) == len(observations) == 1
    assert observations[0]["length_goal"]["target_minutes"] == [8, 10]
    assert stats["duration_goal_status"] == "below_target"
    assert stats["warnings"][0]["automatic_regeneration"] is False
    assert result["modules"]["selling_points"]["sentences"] == [words["selling_points"]]
    assert result["usage"]["total_tokens"] == 140


@pytest.mark.parametrize("count,status", [(1919, "below_target"), (1920, "within_target"),
    (2400, "within_target"), (2401, "above_target")])
def test_duration_range_boundaries_do_not_include_punctuation(count, status):
    stats = script_duration_metrics({"cta": {"sentences": ["练" * count + "，。！？"]}})
    assert stats["spoken_characters"] == count
    assert stats["duration_goal_status"] == status
