"""Versioned editorial directions and provider-supported speech parameters.

Directions are UI/documentation, never text to be spoken or a fictitious TTS prompt.
"""
from copy import deepcopy

STYLE_REVISION = "ecommerce-stage-20261007.1"
_PROFILES = {
    "welcome": ("热情招呼", "像迎接刚进直播间的朋友，明亮、有精神，句末自然收住，不喊叫。", "happy", 1.02, 0, 1.0),
    "pain": ("场景引入", "像聊一个生活小烦恼，带关切和提问感，留出理解空间，不刻意卖惨。", None, 0.98, 0, 1.0),
    "identity": ("商品亮相", "清楚说出商品名称和定位，平稳自信，不把长名称一口气抢读。", None, 0.98, 0, 1.0),
    "benefit": ("卖点讲解", "亲切、有画面感，强调具体特点和使用感受，句子之间有轻重变化。", None, 1.0, 0, 1.0),
    "scene": ("使用场景", "像给朋友展示怎么用，轻松自然，场景转换清楚，不全程亢奋。", None, 1.0, 0, 1.0),
    "specification": ("规格选购", "颜色、尺码和选择建议读清楚，比普通讲解稍慢，数字不要含混。", "calm", 0.94, 0, 1.0),
    "price": ("价格说明", "原价、活动价、币种和条件逐项说清，语速稍慢，让观众听得懂。", None, 0.94, 0, 1.0),
    "promotion": ("优惠强调", "有感染力地强调已知优惠，重点清楚、有节奏，兴奋但不喊叫，不制造未知紧迫感。", "happy", 1.02, 0, 1.0),
    "cta": ("购买引导", "热情明确地提示下一步选择和购买，重音集中，结尾落稳，不强迫观众。", "happy", 1.02, 0, 1.0),
    "neutral": ("自然讲述", "普通话自然清楚地讲述，保持正常音量，不朗读任何风格说明。", None, 1.0, 0, 1.0),
}
_ALIASES = {"hook": "pain", "introduction": "pain", "intro": "pain", "product": "identity",
            "selling_point": "benefit", "selling_points": "benefit", "scenario": "scene",
            "spec": "specification", "spec_notice": "specification", "closing": "cta",
            "call_to_action": "cta", "interaction": "welcome", "engagement": "welcome",
            # The prelive bridge persists module keys as stages. Keep the mapping
            # here so preview, production and runtime share the same profiles.
            "opening_long": "welcome", "opening_short": "welcome", "product_intro": "identity",
            "campaign": "promotion", "transition": "scene"}


def sentence_style(sentence, provider="minimax") -> dict:
    """Choose from the persisted stage, never infer emotion from untrusted prose."""
    stage = str(sentence.get("stage", "neutral")).lower()
    identity = _ALIASES.get(stage, stage)
    if identity not in _PROFILES:
        identity = "neutral"
    label, direction, emotion, speed, pitch, vol = _PROFILES[identity]
    local = provider == "system"
    return {"profile_id": identity, "stage": identity, "style_revision": STYLE_REVISION, "label": label,
            "direction": direction, "description": direction, "goal": label, "provider": provider,
            "emotion": None if local else emotion, "speed": 1.0 if local else speed,
            "pitch": 0 if local else pitch, "vol": vol,
            "emotion_supported": not local,
            "capability_note": "本机普通话保持默认读法，不支持分段情绪控制" if local else
                "方向说明用于展示；模型接收情绪和语速等参数，效果需试听确认"}


def profile_catalog(provider="minimax") -> list:
    return [deepcopy(sentence_style({"stage": stage}, provider)) for stage in _PROFILES]


def continuous_host_style():
    """One explicit host across openings, benefits and calls to action.

    No invented natural-language TTS prompt; only supported controls are sent.
    This is opt-in and versioned: existing approved assets keep their profiles.
    """
    return {"profile_id": "continuous_host", "style_revision": "continuous-host-20261023.1",
            "label": "连贯主播 · 明亮自然", "emotion": "happy", "speed": 1.0,
            "pitch": 0, "vol": 1.0, "subtitle_enable": True, "subtitle_type": "word"}


def interaction_style(item, provider):
    asset = item.get("prelive_audio") or {}
    if provider == "minimax" and asset.get("continuous_track") and asset.get("style", {}).get("profile_id") == "continuous_host":
        return {k: v for k, v in asset["style"].items() if k not in {"subtitle_enable", "subtitle_type"}}
    return sentence_style(item, provider)
