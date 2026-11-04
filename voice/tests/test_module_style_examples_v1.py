"""Actual request selection/cache boundaries; no live provider or style scoring."""
from copy import deepcopy
import json
import re
from unittest.mock import patch

import pytest

from app.prelive import live_script_writing as writing
from app.prelive import module_generation as local
from app.prelive import whole_script_generation as whole


def product():
    return {"id": "example-test", "name": "测试练习套装", "price": 23.58,
            "currency": "CNY", "selling_points": "球拍、回弹网球和底座配成套。"}


def test_whole_gets_one_example_per_selected_module_under_budget_without_new_facts():
    selected = ["opening_long", "product_intro", "selling_points", "cta"]
    context = {"claim_constraints": ["材质不得推导耐用"], "user_business_decisions": ["既有完整决定"]}
    original = deepcopy((product(), context))
    # Disabled campaign must not inject a promotion example into no-offer work.
    actions = [{"key": key, "action": "generate"} for key in selected] + [{"key": "campaign", "action": "disable"}]
    body, _ = whole.build_request(product(), context, actions, model="offline")
    task = json.loads(body["messages"][1]["content"])
    assert list(task["module_style_examples"]) == selected
    assert all(len(items) == 1 for items in task["module_style_examples"].values())
    samples = json.dumps(task["module_style_examples"], ensure_ascii=False)
    # V3 keeps a 236-character complete scene expansion plus literal source
    # voice. The existing 1600-character cap remains; not all 18 scripts.
    assert len(re.findall(r"[\u4e00-\u9fff]", samples)) <= 1600
    assert "旅行装" in samples
    assert "旅行装" not in json.dumps(task["facts"], ensure_ascii=False)
    assert task["facts"] == local.fact_catalog(product())
    assert task["effective_product_snapshot"] == product()
    assert task["context_bundle"] == context
    assert (product(), context) == original
    assert task["style_example_contract"]["role"] == "module_speaking_patterns_only_not_current_product_facts"
    assert task["length_goal"]["spoken_characters_range"] == [1920, 2400]
    assert task["text_contract"] == whole.TEXT_CONTRACT
    assert "exact_excerpt" not in samples and "source_array_index" not in samples


def test_literal_source_voice_and_complete_demonstration_reach_actual_wire():
    actions = [{"key": key, "action": "generate"}
               for key in ["opening_long", "product_intro", "selling_points", "cta"]]
    body, _ = whole.build_request(product(), {}, actions, model="offline")
    task = json.loads(body["messages"][1]["content"])
    examples = task["module_style_examples"]
    # Previously these were merely a third-person abstract of the source. The
    # request must now contain the literal upbeat wording in the allowlisted
    # field (not only a non-transmitted source.exact_excerpt audit record).
    assert "哈喽哈喽，家人们好呀！" in examples["opening_long"][0]["source_style_example"]
    assert "放在包包里超级方便" in examples["selling_points"][0]["source_style_example"]
    assert "家人们，你们说是不是超级棒呀！" in examples["selling_points"][0]["source_style_example"]
    assert "快来快来，让我们一起打造最靓丽的狗狗吧！" in examples["cta"][0]["source_style_example"]
    assert "示范事实仅为" in examples["selling_points"][0]["speaking_pattern"]
    assert "周五收拾行李" in examples["selling_points"][0]["speaking_pattern"]
    assert "这份方便我太喜欢啦" in examples["selling_points"][0]["speaking_pattern"]
    assert "具体时刻、不同顾虑或动作画面" in examples["selling_points"][0]["speaking_pattern"]
    assert all(set(item) == {"id", "learn", "speaking_pattern", "address_use", "source_style_example"}
               for values in examples.values() for item in values)
    # Style examples never expand the product fact catalogue or become product
    # identity, and the direct one-response JSON contract stays identical.
    assert task["facts"] == local.fact_catalog(product())
    assert "漱口水" not in json.dumps(task["facts"], ensure_ascii=False)
    assert task["text_contract"] == whole.TEXT_CONTRACT



def test_script_examples_exclude_faq_sources_and_expand_one_real_scene():
    # FAQ response examples have their own catalogue use. A script style bank
    # must not teach a made-up viewer question/host answer interlude.
    bank = writing._MODULE_EXAMPLE_BANK
    assert all(item["source"]["location"] == "oral" for item in bank["examples"])
    assert all(item["source"]["file"] == "口播数据.json" for item in bank["examples"])
    assert "answer-concern-then-select" not in {item["id"] for item in bank["examples"]}
    selling = next(item for item in bank["examples"] if item["id"] == "feature-scene-benefit")
    demo = selling["speaking_pattern"].split("完整局部口播：", 1)[1].split(" 这是一个理由", 1)[0]
    assert 180 <= len(re.findall(r"[\u4e00-\u9fff]", demo)) <= 260
    assert "周五收拾行李" in demo and "到了住处" in demo
    assert "我真要好好夸一夸" in demo and "这份方便我太喜欢啦" in demo
    assert not any(term in demo for term in ("有人问", "主播答", "大家问", "能吗"))
    cta = next(item for item in bank["examples"] if item["id"] == "value-to-purchase-action")
    # Explicit teaching of non-adjacent re-listing, not runtime text deletion.
    assert "不把刚讲完的颜色、价格和组成换词复读" in cta["speaking_pattern"]
    assert cta["speaking_pattern"].count("{当前准确价格}") == 1
    assert cta["speaking_pattern"].count("{真实选项只在这里集中说明一次}") == 1


def test_shared_energy_guidance_reaches_whole_and_local_system_once():
    body, _ = whole.build_request(product(), {}, [{"key": "selling_points", "action": "generate"}], model="offline")
    local_body = local.build_request(product(), {}, "selling_points", [], model="offline")
    for request in (body, local_body):
        system = request["messages"][0]["content"]
        assert system.count("live-commerce-writing-v6.2-grounded-marketing") == 1
        assert "场景一两句就回到商品" not in system
        assert "场景用于一句两句说清需求" not in system
        assert "脚本不写“有朋友问" in system
        assert "FAQ与脚本分开" in system
        assert "相邻几句不能再完整重报" in system
        assert "不冒充" in system


def test_local_only_gets_its_module_and_unavailable_promotion_gets_no_example():
    for key in local.MODULE_KEYS:
        task = json.loads(local.build_request(product(), {}, key, [], model="offline")["messages"][1]["content"])
        assert set(task["module_style_examples"]) <= {key}
        if key in {"transition", "campaign"}:
            assert task["module_style_examples"] == {}
    with_offer = {**product(), "promotion": "满100减10，每单限用一次，不与其他优惠叠加。"}
    samples = writing.module_style_examples(["campaign"], local.fact_catalog(with_offer))
    assert samples["campaign"][0]["id"] == "real-offer-conditions"


def test_fixed_bank_edit_changes_whole_and_only_affected_local_revision():
    before = {key: local.module_prompt_revision(key) for key in local.MODULE_KEYS}
    whole_before = whole.prompt_revision()
    revised = deepcopy(writing._MODULE_EXAMPLE_BANK)
    next(item for item in revised["examples"] if item["id"] == "feature-scene-benefit")["speaking_pattern"] += "离线修订。"
    with patch.object(writing, "_MODULE_EXAMPLE_BANK", revised):
        assert whole.prompt_revision() != whole_before
        assert local.module_prompt_revision("selling_points") != before["selling_points"]
        assert all(local.module_prompt_revision(key) == before[key] for key in local.MODULE_KEYS - {"selling_points"})
    assert whole.prompt_revision() == whole_before


def test_variant_learning_contract_is_versioned_without_changing_single_version_scope():
    before = whole.prompt_revision()
    local_before = local.module_prompt_revision("cta")
    revised = deepcopy(whole.VERSION_WRITING_CONTRACT)
    revised["default_minimum_changed_content_axes"] = 3
    context = {"variant": {"variant_id": "retained-v1", "version_number": 1},
               "same_product_existing_scripts": [{"variant_id": "retained-v2", "modules": {"cta": ["第二版原文。"]}}],
               "script_variant_briefs": [{"variant_id": "retained-v1", "module_instructions": {"cta": "本版新收口。"}}]}
    original = deepcopy(context)
    actions = [{"key": "cta", "action": "generate", "instruction": "仅本版收口。"}]
    with patch.object(whole, "VERSION_WRITING_CONTRACT", revised):
        assert whole.prompt_revision() != before
        assert local.module_prompt_revision("cta") == local_before
        task = json.loads(whole.build_request(product(), context, actions, model="offline")[0]["messages"][1]["content"])
        assert task["version_writing_contract"] == revised
        assert task["requested_module_keys"] == ["cta"]
        assert task["context_bundle"] == original
        assert task["facts"] == local.fact_catalog(product())
    assert context == original and whole.prompt_revision() == before


def test_selection_returns_copies_and_does_not_reload_or_accept_uploaded_bank():
    facts = local.fact_catalog(product())
    before = deepcopy(writing._MODULE_EXAMPLE_BANK)
    with patch.object(writing.MODULE_EXAMPLES_PATH.__class__, "read_text", side_effect=AssertionError("reload")):
        selected = writing.module_style_examples(["selling_points"], facts)
        selected["selling_points"][0]["speaking_pattern"] = "mutated"
        assert writing.module_style_examples(["selling_points"], facts)["selling_points"][0]["speaking_pattern"] != "mutated"
    assert writing._MODULE_EXAMPLE_BANK == before


@pytest.mark.parametrize("raw", ["[]", "not-json", '{"revision":"bad","examples":[]}'])
def test_invalid_fixed_bank_stops_loading_without_a_silent_empty_bank(raw):
    with patch.object(writing.MODULE_EXAMPLES_PATH.__class__, "read_text", return_value=raw):
        with pytest.raises(RuntimeError, match="直播模块表达样例资源无效"):
            writing._load_module_examples()
