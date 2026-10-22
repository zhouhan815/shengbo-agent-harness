"""Load the shared live-sales writing asset once for both actual request paths.

The fixed local asset is part of the deployed application, never supplied by a
material, tool argument or user path. Its text enters each SYSTEM and therefore
the existing prompt revision and exact-input cache identity. A restart loads a
new asset; an in-flight job cannot silently switch writing rules.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path


WRITING_GUIDANCE_PATH = Path(__file__).resolve().parents[2] / "prompts" / "live-commerce-writing.md"


def _load_guidance():
    try:
        content = WRITING_GUIDANCE_PATH.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        raise RuntimeError("直播写作规则资源不可读取，请检查应用部署后重新启动服务") from None
    if not content:
        raise RuntimeError("直播写作规则资源为空，请检查应用部署后重新启动服务")
    return content


LIVE_COMMERCE_WRITING_GUIDANCE = _load_guidance()

# Fixed deployment resource, separate from uploaded samples and product facts.
# Source quotes stay in the local bank for audit; actual requests only receive
# the selected, product-neutral speaking patterns below.
MODULE_EXAMPLES_PATH = WRITING_GUIDANCE_PATH.with_name("live-commerce-module-examples.json")


def _load_module_examples():
    try:
        bank = json.loads(MODULE_EXAMPLES_PATH.read_text(encoding="utf-8-sig"))
        if not (isinstance(bank, dict) and isinstance(bank["revision"], str) and bank["revision"]
                and isinstance(bank["examples"], list) and bank["examples"]):
            raise ValueError()
        identities = set()
        for item in bank["examples"]:
            if not (isinstance(item, dict) and isinstance(item["id"], str)
                    and item["id"] and item["id"] not in identities):
                raise ValueError()
            identities.add(item["id"])
            if not (isinstance(item["module_keys"], list) and item["module_keys"]
                    and set(item["module_keys"]) <= {"opening_long", "opening_short", "product_intro", "selling_points", "campaign", "cta"}):
                raise ValueError()
            for key in ("requires_all_fact_ids", "requires_any_fact_ids"):
                if not (isinstance(item[key], list) and all(isinstance(value, str) and value for value in item[key])):
                    raise ValueError()
            for key in ("learn", "speaking_pattern", "address_use", "source_style_example"):
                if not (isinstance(item[key], str) and item[key]):
                    raise ValueError()
            if not (isinstance(item["source"], dict) and item["source"]["exact_excerpt"]):
                raise ValueError()
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        raise RuntimeError("直播模块表达样例资源无效，请检查应用部署后重新启动服务") from None
    return bank


_MODULE_EXAMPLE_BANK = _load_module_examples()
STYLE_EXAMPLE_CONTRACT = {
    "role": "module_speaking_patterns_only_not_current_product_facts",
    "use": "只学习所选模块的推荐立场、讲解轮次与用词；按当前事实重新落地，不直接朗读占位符。样例不是完整七模块稿或篇幅模板。",
    "facts": "商品成分、功效、价格、发货、售后、优惠与入口只取本次facts；样例不扩大事实ID闭集。",
    "voice": "亲切招呼、解释利益、承接顾虑、坚定邀请按内容自然变化；称呼服从当前主播人设，情绪词不是TTS控制标签。",
}


def module_examples_revision(module_key=None):
    """The fixed bank affects generation/cache identity, scoped for local edits."""
    entries = [item for item in _MODULE_EXAMPLE_BANK["examples"]
               if module_key is None or module_key in item["module_keys"]]
    if not entries:
        return None
    value = {"revision": _MODULE_EXAMPLE_BANK["revision"], "examples": entries,
             "selection_policy": "module-and-current-fact-availability-first-one-v1",
             "contract": STYLE_EXAMPLE_CONTRACT}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def module_style_examples(module_keys, facts):
    """Select one small pattern per requested module, with no retrieval."""
    fact_ids = {item["id"] for item in facts}
    result = {}
    for module_key in module_keys:
        selected = []
        for item in _MODULE_EXAMPLE_BANK["examples"]:
            if module_key not in item["module_keys"]:
                continue
            if not set(item["requires_all_fact_ids"]) <= fact_ids:
                continue
            needs_any = set(item["requires_any_fact_ids"])
            if needs_any and not needs_any.intersection(fact_ids):
                continue
            selected.append({key: deepcopy(item[key]) for key in ("id", "learn", "speaking_pattern", "address_use", "source_style_example")})
            if len(selected) == 1:
                break
        if selected:
            result[module_key] = selected
    return result
