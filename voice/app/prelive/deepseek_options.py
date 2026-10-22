"""DeepSeek request options for formal copywriting (whole and module scripts).

2026-10-22 user decision: formal script writing uses DEEPSEEK_MODEL
(deepseek-v4-pro) with thinking enabled at DEEPSEEK_REASONING_EFFORT (high).
Live welcome/comment replies keep their own fast model and thinking-disabled
requests (reply_lab, DEEPSEEK_REPLY_MODEL). Unset variables keep the previous
behaviour (thinking disabled, 120 s), so nothing changes silently.
"""
from __future__ import annotations

import os

VALID_EFFORTS = {"low", "high", "max"}


def script_thinking_enabled() -> bool:
    return os.environ.get("DEEPSEEK_SCRIPT_THINKING", "disabled").strip().lower() == "enabled"


def script_thinking_options() -> dict:
    if not script_thinking_enabled():
        return {"thinking": {"type": "disabled"}}
    effort = os.environ.get("DEEPSEEK_REASONING_EFFORT", "high").strip().lower()
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort if effort in VALID_EFFORTS else "high"}


def script_max_tokens(base: int) -> int:
    # Reasoning tokens share the output budget in thinking mode.
    return base + 16000 if script_thinking_enabled() else base


def script_timeout_seconds() -> float:
    return 300.0 if script_thinking_enabled() else 120.0
