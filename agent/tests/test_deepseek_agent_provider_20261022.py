"""DeepSeek Agent provider adapter: offline wire/hook contracts, no network."""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from shengbo_prelive.providers import build_agent_model
from shengbo_prelive.providers.deepseek import (NON_LEADING_SYSTEM_PREFIX, SEEN_IMAGE_PLACEHOLDER, DeepSeekAgentModel,
                                                agent_model_settings, build_chat_messages, read_api_key)

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 16).decode()


@tool
def read_context(ref: str) -> str:
    """Read a context."""
    return ref


def test_defaults_follow_user_decision(monkeypatch):
    for name in ("SHENGBO_AGENT_MODEL_PROVIDER", "SHENGBO_AGENT_MODEL", "SHENGBO_AGENT_VISION_MODEL",
                 "SHENGBO_SUMMARY_MODEL", "SHENGBO_AGENT_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    assert agent_model_settings() == {"provider": "deepseek", "model": "deepseek-v4-pro", "vision_model": "deepseek-flash",
                                      "summary_model": "deepseek-flash", "effort": "high"}
    main = build_agent_model(model="gpt-6.1-sol")
    summary = build_agent_model(purpose="summary")
    assert isinstance(main, DeepSeekAgentModel) and main.model_name == "deepseek-v4-pro" and main.thinking
    assert summary.model_name == "deepseek-flash" and summary.thinking is False
    monkeypatch.setenv("SHENGBO_AGENT_MODEL_PROVIDER", "chatgpt_plan")
    assert build_agent_model(purpose="summary") is None


def test_messages_route_unseen_images_to_vision_and_keep_reasoning():
    messages = [SystemMessage("规则"), HumanMessage("开始"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "read_source", "args": {"ref": "x"}}],
                  additional_kwargs={"reasoning_content": "先读图"}),
        ToolMessage(content=[{"type": "text", "text": "page"}, {"type": "image_url", "image_url": {"url": PNG}}], tool_call_id="c1"),
        SystemMessage("<shengbo_trusted_context>{}</shengbo_trusted_context>")]
    chat, unseen = build_chat_messages(messages)
    assert unseen is True
    assert [item["role"] for item in chat] == ["system", "user", "assistant", "tool", "user", "user"]
    assert chat[2]["reasoning_content"] == "先读图" and chat[2]["tool_calls"][0]["function"]["name"] == "read_source"
    assert chat[3] == {"role": "tool", "tool_call_id": "c1", "content": "page"}
    assert chat[4]["content"][-1]["type"] == "image_url"
    assert chat[5]["content"].startswith(NON_LEADING_SYSTEM_PREFIX)
    model = DeepSeekAgentModel(model_name="deepseek-v4-pro", vision_model_name="deepseek-flash")
    assert model._payload(messages)["model"] == "deepseek-flash"
    # Once the model has answered after the image, the pro model continues.
    later = [*messages[:4], AIMessage(content="看完了"), HumanMessage("继续")]
    chat, unseen = build_chat_messages(later)
    assert unseen is False and SEEN_IMAGE_PLACEHOLDER in chat[4]["content"]
    assert model._payload(later)["model"] == "deepseek-v4-pro"


def test_payload_thinking_options_and_tool_schema():
    model = DeepSeekAgentModel(model_name="deepseek-v4-pro", reasoning_effort="high")
    bound = model.bind_tools([read_context])
    payload = model._payload([HumanMessage("x")], **bound.kwargs)
    assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == "high"
    assert payload["tools"][0]["type"] == "function" and payload["tools"][0]["function"]["name"] == "read_context"
    summary = DeepSeekAgentModel(model_name="deepseek-flash", thinking=False, purpose="summary")
    assert summary._payload([HumanMessage("x")])["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in summary._payload([HumanMessage("x")])


def _transport(body, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
            assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_round_trip_hooks_usage_details_and_tool_calls():
    events, budget, requests, responses, seen = [], [], [], [], []
    body = {"id": "resp-1", "choices": [{"finish_reason": "tool_calls", "message": {
        "content": "", "reasoning_content": "想一想",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_context", "arguments": "{\"ref\": \"a\"}"}}]}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
                  "prompt_cache_hit_tokens": 640, "prompt_cache_miss_tokens": 360,
                  "completion_tokens_details": {"reasoning_tokens": 30}}}
    model = DeepSeekAgentModel(model_name="deepseek-v4-pro", observer=events.append, before_send=budget.append,
                               on_request=requests.append, on_response=responses.append,
                               async_transport=_transport(body, seen=seen), key_reader=lambda: "test-key")
    result = await model.bind_tools([read_context]).ainvoke([SystemMessage("规则"), HumanMessage("读")])
    assert result.tool_calls[0]["args"] == {"ref": "a"} and result.additional_kwargs["reasoning_content"] == "想一想"
    assert result.usage_metadata == {"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050}
    assert [event["type"] for event in events] == ["model.sent", "model.response_observed", "model.completed"]
    assert "input" in budget[0] and budget[0]["model"] == "deepseek-v4-pro"  # provider-neutral preview for the fence
    assert requests[0]["payload"]["provider"] == "deepseek" and "test-key" not in json.dumps(requests)
    assert responses[0]["usage_details"] == {"cached_input_tokens": 640, "reasoning_output_tokens": 30}
    assert seen[0]["model"] == "deepseek-v4-pro" and seen[0]["thinking"] == {"type": "enabled"}


def _sse_transport(chunks, seen=None):
    text = "".join(": keep-alive\n\n" if chunk is None else "data: " + (chunk if isinstance(chunk, str) else json.dumps(chunk)) + "\n\n"
                   for chunk in chunks)

    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=text.encode())
    return httpx.MockTransport(handler)


SSE_CHUNKS = [
    {"id": "resp-s", "choices": [{"index": 0, "delta": {"reasoning_content": "想"}}]},
    None,
    {"id": "resp-s", "choices": [{"index": 0, "delta": {"reasoning_content": "一想"}}]},
    {"id": "resp-s", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                                          "function": {"name": "read_context", "arguments": "{\"ref\""}}]}}]},
    {"id": "resp-s", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": \"a\"}"}}]}}]},
    {"id": "resp-s", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    {"id": "resp-s", "choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
                                              "prompt_cache_hit_tokens": 640, "completion_tokens_details": {"reasoning_tokens": 30}}},
    "[DONE]",
]


@pytest.mark.asyncio
async def test_streamed_response_is_folded_like_non_streaming_body():
    """Regression 2026-10-22: silent non-streaming calls were cut at 60.5 s on Windows; requests now stream."""
    responses, seen = [], []
    model = DeepSeekAgentModel(model_name="deepseek-v4-pro", on_response=responses.append,
                               async_transport=_sse_transport(SSE_CHUNKS, seen=seen), key_reader=lambda: "test-key")
    result = await model.bind_tools([read_context]).ainvoke([HumanMessage("读")])
    assert seen[0]["stream"] is True and seen[0]["stream_options"] == {"include_usage": True}
    assert result.tool_calls == [{"type": "tool_call", "name": "read_context", "args": {"ref": "a"}, "id": "call_1"}]
    assert result.additional_kwargs["reasoning_content"] == "想一想"
    assert result.usage_metadata == {"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050}
    assert responses[0]["usage_details"] == {"cached_input_tokens": 640, "reasoning_output_tokens": 30}
    assert responses[0]["provider_response_id"] == "resp-s"
    sync = DeepSeekAgentModel(transport=_sse_transport(SSE_CHUNKS), key_reader=lambda: "test-key")
    assert sync.bind_tools([read_context]).invoke([HumanMessage("读")]).tool_calls[0]["args"] == {"ref": "a"}


@pytest.mark.asyncio
async def test_stream_cut_before_finish_is_not_accepted():
    events = []
    model = DeepSeekAgentModel(observer=events.append, async_transport=_sse_transport(SSE_CHUNKS[:4]),
                               key_reader=lambda: "test-key")
    with pytest.raises(Exception):
        await model.bind_tools([read_context]).ainvoke([HumanMessage("读")])
    assert events[-1]["type"] == "model.failed" and events[-1]["code"] == "model_response_not_completed"


@pytest.mark.asyncio
async def test_http_rejection_is_known_failure_without_key_leak():
    events = []
    model = DeepSeekAgentModel(observer=events.append, async_transport=_transport(
        {"error": {"type": "invalid_request_error", "code": "invalid_request_error", "message": "secret test-key"}}, status=400),
        key_reader=lambda: "test-key")
    with pytest.raises(Exception) as error:
        await model.ainvoke([HumanMessage("x")])
    failed = events[-1]
    assert failed["type"] == "model.failed" and failed["http_status"] == 400
    assert "test-key" not in json.dumps(events) and "test-key" not in str(error.value)


def test_api_key_comes_from_voice_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_MODEL=x\nDEEPSEEK_API_KEY=abc123\n", encoding="utf-8")
    monkeypatch.setenv("SHENGBO_DEEPSEEK_ENV_FILE", str(env))
    assert read_api_key() == "abc123"
    env.write_text("DEEPSEEK_MODEL=x\n", encoding="utf-8")
    from shengbo_prelive.providers.credentials import CredentialError
    with pytest.raises(CredentialError, match="deepseek_api_key_missing"):
        read_api_key()


def test_harness_profile_applies_to_deepseek_model():
    from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model
    import shengbo_prelive.runtime  # registers profiles
    profile = _harness_profile_for_model(DeepSeekAgentModel(), None)
    assert profile.general_purpose_subagent.enabled is False


def test_voice_script_thinking_options(monkeypatch):
    from app.prelive.deepseek_options import script_max_tokens, script_thinking_options, script_timeout_seconds
    monkeypatch.delenv("DEEPSEEK_SCRIPT_THINKING", raising=False)
    assert script_thinking_options() == {"thinking": {"type": "disabled"}} and script_timeout_seconds() == 120.0
    monkeypatch.setenv("DEEPSEEK_SCRIPT_THINKING", "enabled")
    monkeypatch.setenv("DEEPSEEK_REASONING_EFFORT", "high")
    assert script_thinking_options() == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert script_max_tokens(24000) == 40000 and script_timeout_seconds() == 300.0
