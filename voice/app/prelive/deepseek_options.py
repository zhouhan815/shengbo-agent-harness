"""DeepSeek request options for formal copywriting (whole and module scripts).

2026-10-22 user decision: formal script writing uses DEEPSEEK_MODEL
(deepseek-v4-pro) with thinking enabled at DEEPSEEK_REASONING_EFFORT (high).
Live welcome/comment replies keep their own fast model and thinking-disabled
requests (reply_lab, DEEPSEEK_REPLY_MODEL). Unset variables keep the previous
behaviour (thinking disabled, 120 s), so nothing changes silently.
"""
from __future__ import annotations

import json
import os
from time import perf_counter_ns

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


# 2026-10-22: a thinking request is silent for minutes when non-streamed, and
# this PC's network path cuts any TLS connection idle for ~60 s (both observed
# failures: 60.7 s and 62 s, timeout_unknown). Streaming keeps bytes flowing.
# The hashed request body is unchanged; only the sent copy asks for a stream.
STREAM_FIELDS = {"stream": True, "stream_options": {"include_usage": True}}


def streamed(body: dict) -> dict:
    return {**body, **STREAM_FIELDS}


async def read_provider_body(response, max_bytes, too_large, *, stream_marks: dict[str, int] | None = None):
    """Return (non-streaming body shape, counted bytes) from a JSON or SSE response.

    The size limit counts answer text, not SSE framing or reasoning, so a long
    thinking stream is not rejected for its wrapper bytes. A stream that ends
    without finish_reason yields no choices (an invalid, not a parsed, answer).
    Optional marks record monotonic timestamps in nanoseconds for SSE events.
    """
    if not response.headers.get("content-type", "").startswith("text/event-stream"):
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise too_large()
            chunks.append(chunk)
        try:
            result = json.loads(b"".join(chunks))
        except (TypeError, ValueError, UnicodeError):
            return {}, size
        return (result if isinstance(result, dict) else {}), size
    if stream_marks is not None:
        stream_marks["response_headers"] = perf_counter_ns()
    body, content, finish, size, tool_calls = {}, [], None, 0, False
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            return {}, size
        if not isinstance(chunk, dict):
            return {}, size
        if chunk.get("id"):
            body["id"] = chunk["id"]
        if chunk.get("usage"):
            body["usage"] = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if (stream_marks is not None and "first_reasoning_delta" not in stream_marks
                    and delta.get("reasoning_content")):
                stream_marks["first_reasoning_delta"] = perf_counter_ns()
            piece = delta.get("content") or ""
            if piece:
                if stream_marks is not None and "first_text_delta" not in stream_marks:
                    stream_marks["first_text_delta"] = perf_counter_ns()
                size += len(piece.encode("utf-8"))
                if size > max_bytes:
                    raise too_large()
                content.append(piece)
            tool_calls = tool_calls or bool(delta.get("tool_calls"))
            finish = choice.get("finish_reason") or finish
    message = {"content": "".join(content) if content else None}
    if tool_calls:
        message["tool_calls"] = [{}]
    body["choices"] = [{"message": message, "finish_reason": finish}] if finish else []
    return body, size
