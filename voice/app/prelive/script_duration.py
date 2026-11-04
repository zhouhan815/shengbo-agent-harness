"""One shared, observable text-length estimate; actual audio stays authoritative.

The 240 characters/minute basis is an application planning heuristic, not a
provider promise. It counts spoken letters/numbers without punctuation/space.
No output is truncated, rejected, or automatically regenerated for this goal.
"""
from __future__ import annotations

from collections.abc import Mapping


DURATION_POLICY_REVISION = "script-duration-8-10min-v4-scenario-allocation"
TARGET_MINUTES = (8, 10)
CHARACTERS_PER_MINUTE = 240
RATE_RANGE = (220, 260)
TARGET_SPOKEN_CHARACTERS = tuple(value * CHARACTERS_PER_MINUTE for value in TARGET_MINUTES)


def spoken_character_count(text):
    """Measure normalized spoken text; unlike len, do not count pauses twice."""
    return sum(character.isalnum() for character in text) if isinstance(text, str) else 0


def length_goal(module_keys, requested_range=None):
    keys = list(module_keys)
    weights = {"opening_long": 7, "opening_short": 2, "product_intro": 1,
               "selling_points": 63, "campaign": 12, "cta": 22}
    # Unused opening/campaign time is shared by the selected speaking modules.
    total_weight = sum(weights.get(key, 0) for key in keys)
    limits = requested_range or TARGET_SPOKEN_CHARACTERS
    middle = sum(limits) // 2
    module_targets = {key: round(middle * weights.get(key, 0) / total_weight)
                      for key in keys} if total_weight else {}
    return {"policy_revision": DURATION_POLICY_REVISION,
            "target_minutes": ([round(count / CHARACTERS_PER_MINUTE, 2) for count in limits]
                               if requested_range else list(TARGET_MINUTES)),
            "spoken_characters_range": list(limits),
            "nominal_spoken_characters": middle,
            "counting_basis": "spoken_text_letters_and_numbers_excluding_punctuation_and_space",
            "assumed_characters_per_minute": CHARACTERS_PER_MINUTE,
            "module_spoken_character_targets": module_targets,
            "measurement_kind": "text_estimate_not_measured_audio",
            "requirements": ["按本次指定字数写完一版短口播，四段合计控制在范围内。" if requested_range else
                "整版一次生成，完整正文目标约八至十分钟，长短开场只改变引入长度，不缩短主体。",
                "商品介绍只写一句营销亮品，不另写参数段；更多组成与用途在相关卖点中讲清。",
                "参考模块预算统筹全文，用各条已确认卖点的具体场景、使用动作、顾客利益和递进邀选充分展开；不写FAQ或模拟现场问答，不靠紧邻重复清单凑字数。",
                "信息不足以有意义展开时保留真实可用内容，不能编造性能、优惠、见证、库存或重复句填满时长。"],
            "enforcement": "advisory_only_no_truncation_no_automatic_retry"}


def script_duration_metrics(modules):
    """Return stable aggregate/per-module evidence using public spoken text only."""
    rows = []
    items = modules.items() if isinstance(modules, Mapping) else []
    for key, module in items:
        lines = module.get("sentences", []) if isinstance(module, dict) else []
        texts = [line if isinstance(line, str) else line.get("spoken_text", "")
                 if isinstance(line, dict) else "" for line in lines]
        count = sum(spoken_character_count(text) for text in texts)
        rows.append({"module_key": key, "spoken_characters": count,
                     "estimated_seconds": round(count * 60 / CHARACTERS_PER_MINUTE, 3)})
    count = sum(row["spoken_characters"] for row in rows)
    low, high = TARGET_SPOKEN_CHARACTERS
    status = "below_target" if count < low else "above_target" if count > high else "within_target"
    return {"policy_revision": DURATION_POLICY_REVISION, "spoken_characters": count,
            "target_minutes": list(TARGET_MINUTES), "target_spoken_characters": [low, high],
            "assumed_characters_per_minute": CHARACTERS_PER_MINUTE,
            "estimated_seconds": round(count * 60 / CHARACTERS_PER_MINUTE, 3),
            "estimated_seconds_range": [round(count * 60 / RATE_RANGE[1], 3),
                                        round(count * 60 / RATE_RANGE[0], 3)],
            "measurement_kind": "text_estimate_not_measured_audio", "duration_goal_status": status,
            "warnings": [] if status == "within_target" else [{"code": "SCRIPT_DURATION_" + status.upper(),
                "severity": "advisory", "automatic_regeneration": False}], "modules": rows}
