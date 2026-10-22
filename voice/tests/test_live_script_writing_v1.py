"""One fixed writing asset reaches actual whole/local requests and identities.

Transport responses are scripted. These tests establish input, fact and cache
boundaries, not the marketing quality of a real provider response.
"""
from copy import deepcopy
import json
from pathlib import Path
import runpy
from unittest.mock import patch

import httpx
import pytest

from app.prelive import live_script_writing as writing
from app.prelive import module_generation as local
from app.prelive import whole_script_generation as whole
from app.prelive.script_duration import length_goal


def product():
    return {"id": "writing-test", "name": "测试练习套装", "price": 23.58,
            "currency": "CNY", "selling_points": "套装含球拍、一个回弹网球和底座。"}


def actions():
    return [{"key": key, "action": "generate", "instruction": "面向新手，按有据特点明确引导选择。"}
            for key in ("opening_long", "product_intro", "selling_points", "cta")]


def module(key):
    words = {"opening_long": "想练习的朋友先来看看。", "product_intro": "这是测试练习套装。",
             "selling_points": "套装含球拍、一个回弹网球和底座。", "cta": "需要练习套装的朋友，选好就下单。"}
    factual = key in {"product_intro", "selling_points"}
    return {"module_key": key, "sentences": [{"display_text": words[key], "spoken_text": words[key],
        "kind": "product_claim" if factual else "engagement",
        "fact_ids": ["NAME" if key == "product_intro" else "SELLING"] if factual else []}]}


def test_asset_path_is_fixed_and_unicode_bom_decoding_is_explicit():
    expected = Path(writing.__file__).resolve().parents[2] / "prompts" / "live-commerce-writing.md"
    assert writing.WRITING_GUIDANCE_PATH == expected
    assert writing.LIVE_COMMERCE_WRITING_GUIDANCE == expected.read_text(encoding="utf-8-sig").strip()
    bank_text = writing.MODULE_EXAMPLES_PATH.read_text(encoding="utf-8-sig")
    def fixed_asset(path, *, encoding):
        assert encoding == "utf-8-sig"
        return "\n离线规则正文。\n" if path == expected else bank_text
    with patch.object(Path, "read_text", autospec=True, side_effect=fixed_asset) as read:
        loaded = runpy.run_path(writing.__file__)
    assert read.call_count == 2
    read.assert_any_call(expected, encoding="utf-8-sig")
    read.assert_any_call(writing.MODULE_EXAMPLES_PATH, encoding="utf-8-sig")
    assert loaded["LIVE_COMMERCE_WRITING_GUIDANCE"] == "离线规则正文。"


@pytest.mark.parametrize("failure", [FileNotFoundError("private-path"),
    PermissionError("private-path"), UnicodeError("private-data"), "", " \n\t"])
def test_missing_empty_or_unreadable_asset_has_no_silent_fallback(failure):
    config = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
    with patch.object(Path, "read_text", **config), pytest.raises(RuntimeError) as error:
        runpy.run_path(writing.__file__)
    assert "private" not in str(error.value)
    assert "直播写作规则资源" in str(error.value)


def test_shared_asset_is_loaded_once_and_both_requests_keep_context_and_facts():
    context = {"research": {"audience": "新手", "hypotheses": ["仅用于表达重点"]},
               "same_product_existing_scripts": [{"modules": {"selling_points": ["旧稿场景故事，不是当前风格。"]}}],
               "user_decisions": [{"children_usable": True}], "unknowns": ["未提供效果参数"]}
    prior = [{"spoken_text": "原模块正文。"}]
    original = deepcopy((context, prior, product()))
    # A request never reloads the file while a frozen job is executing.
    with patch.object(Path, "read_text", side_effect=AssertionError("dynamic asset reload")):
        full, _ = whole.build_request(product(), context, actions(), model="offline-model")
        part = local.build_request(product(), {**context, "module_key": "cta"}, "cta", prior, model="offline-model")
    for request in (full, part):
        system = request["messages"][0]["content"]
        assert system.count(writing.LIVE_COMMERCE_WRITING_GUIDANCE) == 1
        assert local.CONSUMER_VOICE_GUIDANCE in system
        task = json.loads(request["messages"][1]["content"])
        assert task["facts"] == local.fact_catalog(product())
        assert task["effective_product_snapshot"] == product()
        for key, value in context.items():
            assert task["context_bundle"][key] == value
        assert "好例：喜欢这款、也有相应需要的朋友，可以按自己的需要选择。" not in system
    assert (context, prior, product()) == original
    assert json.loads(part["messages"][1]["content"])["previous_module"] == prior


def test_writing_content_changes_both_prompt_identities_without_mutating_originals():
    before = (whole.prompt_revision(), {key: local.module_prompt_revision(key) for key in local.MODULE_KEYS})
    extra = "\n离线测试：新版统一口播规则。"
    with patch.object(whole, "SYSTEM", whole.SYSTEM + extra), patch.object(local, "SYSTEM", local.SYSTEM + extra):
        assert whole.prompt_revision() != before[0]
        assert all(local.module_prompt_revision(key) != revision for key, revision in before[1].items())
    assert whole.prompt_revision() == before[0]
    assert {key: local.module_prompt_revision(key) for key in local.MODULE_KEYS} == before[1]


def test_long_and_short_versions_keep_estimate_basis_and_higher_action_budget():
    for opening in ("opening_long", "opening_short"):
        goal = length_goal([opening, "product_intro", "selling_points", "cta"])
        assert goal["spoken_characters_range"] == [1920, 2400]
        assert goal["assumed_characters_per_minute"] == 240
        assert 450 <= goal["module_spoken_character_targets"]["cta"] <= 650
        assert goal["module_spoken_character_targets"]["selling_points"] > goal["module_spoken_character_targets"]["cta"]
        assert goal["enforcement"] == "advisory_only_no_truncation_no_automatic_retry"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["whole", "local"])
async def test_actual_scripted_transport_has_shared_system_once_and_one_attempt(mode):
    requests, observations = [], []
    selected = actions() if mode == "whole" else [{"key": "cta"}]
    def handler(request):
        requests.append(json.loads(request.content))
        value = {"modules": [module(item["key"]) for item in selected]} if mode == "whole" else module("cta")
        if mode == "whole":
            for item in value["modules"]:
                item["sentences"] = [{"text": row["spoken_text"], "kind": row["kind"], "fact_ids": row["fact_ids"]}
                                     for row in item["sentences"]]
        return httpx.Response(200, json={"id": "writing-offline", "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(value, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}})
    kwargs = {"api_key": "offline-only", "transport": httpx.MockTransport(handler),
              "response_observer": observations.append}
    if mode == "whole":
        result = await whole.generate_one_script(product(), {}, selected, **kwargs)
        assert set(result["modules"]) == {item["key"] for item in selected}
        expected = whole.prompt_revision()
    else:
        result = await local.generate_one_module(product(), {"module_key": "cta"}, "cta", [], **kwargs)
        assert result["sentences"] == [module("cta")["sentences"][0]["spoken_text"]]
        expected = local.module_prompt_revision("cta")
    assert len(requests) == len(observations) == 1
    assert requests[0]["messages"][0]["content"].count(writing.LIVE_COMMERCE_WRITING_GUIDANCE) == 1
    assert result["prompt_revision"] == observations[0]["prompt_revision"] == expected
    assert observations[0]["usage"] == {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}
