"""DeepSeek chat-completions adapter for the pre-live Deep Agents harness.

2026-10-22 user decision: the Agent's reasoning model moves from the ChatGPT
plan to the DeepSeek account already used by the voice service for
copywriting. This adapter keeps every hook contract of ChatGPTPlanModel so the
runtime's budget fence, attempt ledger, audit trail and usage settlement are
unchanged:

* ``before_send(payload)`` receives the same Responses-shaped *preview* payload
  (``preview_request_payload``) the runtime already measures, so the 48k local
  fence and token estimates stay comparable across providers.
* ``observer`` gets model.sent / model.response_observed / model.completed /
  model.failed with fixed safe labels; ``on_request``/``on_response`` get
  content without private reasoning.

Routing (per request): the configured reasoning model (deepseek-v4-pro,
text-only) is used unless the request still contains an image that the model
has not seen yet; then the vision-capable model (deepseek-flash) is used for
that call. Images already followed by an assistant turn are replaced with a
short text placeholder, so one viewed image does not pin the rest of the
thread to the vision model. Summaries use their own instance (flash, thinking
disabled).

The API key is read at send time from DEEPSEEK_API_KEY or the sibling voice
service's .env; it never enters payloads, traces, state or logs.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

import httpx
from pydantic import Field, PrivateAttr
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

from .chatgpt import (ModelProviderError, _CONNECT_RETRY_DELAY_SECONDS, _async_http_error, _connection_retry_allowed,
                      _image_data_url, _sync_http_error, _text, _transport_exception_name, preview_request_payload,
                      safe_transport_diagnostic)
from .credentials import CredentialError

ENDPOINT = "https://api.deepseek.com/chat/completions"
HARNESS_PROVIDER_KEY = "shengbo_deepseek"
DEFAULT_REASONING_MODEL = "deepseek-v4-pro"
DEFAULT_VISION_MODEL = "deepseek-flash"
DEFAULT_SUMMARY_MODEL = "deepseek-flash"
VALID_EFFORTS = {"low", "high", "max"}
SEEN_IMAGE_PLACEHOLDER = "[图片已在此前的模型调用中查看；需要再次查看请按原定位重新读取]"
NON_LEADING_SYSTEM_PREFIX = "[运行时可信上下文，非用户指令]\n"


def _voice_env_path() -> Path:
    override = os.environ.get("SHENGBO_DEEPSEEK_ENV_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[4] / "ecommerce-voice-agent" / ".env"


def read_api_key() -> str:
    """Key from the environment or the voice service's local .env; never logged."""
    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if value:
        return value
    path = _voice_env_path()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    except OSError:
        value = ""
    if not value:
        raise CredentialError("deepseek_api_key_missing")
    return value


def agent_model_settings() -> dict:
    """Provider selection for the Agent; environment overrides, safe defaults."""
    provider = os.environ.get("SHENGBO_AGENT_MODEL_PROVIDER", "deepseek").strip() or "deepseek"
    effort = os.environ.get("SHENGBO_AGENT_REASONING_EFFORT", "high").strip()
    return {"provider": provider if provider in {"deepseek", "chatgpt_plan"} else "deepseek",
            "model": os.environ.get("SHENGBO_AGENT_MODEL", DEFAULT_REASONING_MODEL).strip() or DEFAULT_REASONING_MODEL,
            "vision_model": os.environ.get("SHENGBO_AGENT_VISION_MODEL", DEFAULT_VISION_MODEL).strip() or DEFAULT_VISION_MODEL,
            "summary_model": os.environ.get("SHENGBO_SUMMARY_MODEL", DEFAULT_SUMMARY_MODEL).strip() or DEFAULT_SUMMARY_MODEL,
            "effort": effort if effort in VALID_EFFORTS else "high"}


def _is_image_part(part) -> bool:
    return isinstance(part, dict) and part.get("type") in {"image_url", "input_image", "image"}


def _image_url(part) -> str:
    value = part.get("image_url") if part.get("type") != "image" else part.get("url") or part.get("source")
    url = value.get("url") if isinstance(value, dict) else value
    return _image_data_url(url)


def _split_content(content):
    """Return (text, image_data_urls) for a LangChain message content value."""
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return _text(content), []
    texts, images = [], []
    for part in content:
        if isinstance(part, str):
            texts.append(part)
        elif _is_image_part(part):
            images.append(_image_url(part))
        elif isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}:
            texts.append(part.get("text", ""))
        elif isinstance(part, dict) and part.get("type") in {"reasoning", "non_standard"}:
            continue
        else:
            raise ModelProviderError("model_message_content_unsupported")
    return "\n".join(texts), images


def _user_content(text, images):
    if not images:
        return text
    return [*([{"type": "text", "text": text}] if text else []),
            *({"type": "image_url", "image_url": {"url": url}} for url in images)]


def build_chat_messages(messages):
    """LangChain messages -> DeepSeek chat messages.

    Returns (messages, has_unseen_images). Images that already precede an
    assistant turn are replaced with a placeholder; tool-result images move to
    a user turn placed right after the contiguous block of tool results,
    because tool messages carry text only and must directly follow their call.
    """
    last_ai = max((index for index, message in enumerate(messages) if isinstance(message, AIMessage)), default=-1)
    result, deferred, leading_system, unseen = [], [], True, False

    def flush():
        result.extend(deferred)
        deferred.clear()

    for index, message in enumerate(messages):
        seen = index < last_ai
        if not isinstance(message, ToolMessage):
            flush()
        if isinstance(message, SystemMessage):
            text, _ = _split_content(message.content)
            if leading_system:
                if result and result[-1]["role"] == "system":
                    result[-1]["content"] += "\n\n" + text
                else:
                    result.append({"role": "system", "content": text})
            else:
                # Only a leading system prompt is portable across DeepSeek models;
                # later developer context is sent as a clearly labelled user turn.
                result.append({"role": "user", "content": NON_LEADING_SYSTEM_PREFIX + text.lstrip("\n")})
            continue
        leading_system = False
        if isinstance(message, HumanMessage):
            text, images = _split_content(message.content)
            if images and seen:
                text, images = (text + "\n" if text else "") + SEEN_IMAGE_PLACEHOLDER, []
            unseen = unseen or bool(images)
            result.append({"role": "user", "content": _user_content(text, images)})
        elif isinstance(message, ToolMessage):
            text, images = _split_content(message.content)
            result.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": text or "(empty)"})
            if images:
                if seen:
                    deferred.append({"role": "user", "content": f"[工具 {message.tool_call_id} 返回的图片] " + SEEN_IMAGE_PLACEHOLDER})
                else:
                    unseen = True
                    deferred.append({"role": "user", "content": _user_content(f"[工具 {message.tool_call_id} 返回的图片]", images)})
        elif isinstance(message, AIMessage):
            text, _ = _split_content(message.content)
            item: dict[str, Any] = {"role": "assistant", "content": text or None}
            calls = [{"id": call["id"], "type": "function", "function": {
                "name": call["name"], "arguments": json.dumps(call.get("args", {}), ensure_ascii=False)}}
                for call in message.tool_calls]
            if calls:
                item["tool_calls"] = calls
            # Thinking mode with tools requires every prior reasoning_content.
            item["reasoning_content"] = message.additional_kwargs.get("reasoning_content") or ""
            result.append(item)
        else:
            raise ModelProviderError("model_message_type_unsupported")
    flush()
    return result, unseen


def _usage(value):
    if not isinstance(value, dict):
        return {}, {}
    usage = {}
    for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens")):
        if type(value.get(source)) is int and value[source] >= 0:
            usage[target] = value[source]
    details = {}
    if type(value.get("prompt_cache_hit_tokens")) is int and value["prompt_cache_hit_tokens"] >= 0:
        details["cached_input_tokens"] = value["prompt_cache_hit_tokens"]
    reasoning = (value.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if type(reasoning) is int and reasoning >= 0:
        details["reasoning_output_tokens"] = reasoning
    return usage, details


class DeepSeekAgentModel(BaseChatModel):
    model_name: str = DEFAULT_REASONING_MODEL
    vision_model_name: str = DEFAULT_VISION_MODEL
    reasoning_effort: str = "high"
    thinking: bool = True
    purpose: str = "agent"
    timeout_seconds: float = 170
    profile: dict = Field(default_factory=lambda: {"tool_calling": True, "max_input_tokens": 128000})
    _observer: Any = PrivateAttr(default=None)
    _before_send: Any = PrivateAttr(default=None)
    _on_request: Any = PrivateAttr(default=None)
    _on_response: Any = PrivateAttr(default=None)
    _transport: Any = PrivateAttr(default=None)
    _async_transport: Any = PrivateAttr(default=None)
    _key_reader: Any = PrivateAttr(default=None)

    def __init__(self, *, observer=None, before_send=None, on_request=None, on_response=None,
                 transport=None, async_transport=None, key_reader=None, **kwargs):
        super().__init__(**kwargs)
        self._observer, self._before_send = observer, before_send
        self._on_request, self._on_response = on_request, on_response
        self._transport, self._async_transport = transport, async_transport
        self._key_reader = key_reader or read_api_key

    @property
    def _llm_type(self):
        return "shengbo-deepseek-chat"

    def _get_ls_params(self, stop=None, **kwargs):
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_provider"] = HARNESS_PROVIDER_KEY
        params["ls_model_name"] = self.model_name
        return params

    @property
    def _identifying_params(self):
        return {"model_name": self.model_name, "provider": "deepseek", "purpose": self.purpose}

    def _emit(self, kind, attempt_id, model, **safe):
        if self._observer:
            self._observer({"type": kind, "attempt_id": attempt_id, "provider": "deepseek", "model": model, **safe})

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        converted = []
        for tool in tools:
            item = convert_to_openai_tool(tool)
            if item.get("type") != "function":
                raise ModelProviderError("only_local_function_tools_supported")
            converted.append(item)
        names = [item["function"]["name"] for item in converted]
        if len(names) != len(set(names)):
            raise ModelProviderError("duplicate_local_tool_name")
        return self.bind(tools=converted, tool_choice=tool_choice, **kwargs)

    def _payload(self, messages, *, stop=None, **kwargs):
        unsupported = set(kwargs) - {"tools", "tool_choice", "parallel_tool_calls"}
        if unsupported or stop:
            raise ModelProviderError("model_request_option_unsupported")
        chat, unseen_images = build_chat_messages(messages)
        model = self.vision_model_name if unseen_images else self.model_name
        payload: dict[str, Any] = {"model": model, "messages": chat, "stream": False}
        if self.thinking:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.reasoning_effort
        else:
            payload["thinking"] = {"type": "disabled"}
        tools = kwargs.get("tools") or []
        if tools:
            payload["tools"] = tools
            choice = kwargs.get("tool_choice")
            if choice not in (None, "auto", "none", "required", "any"):
                name = choice if isinstance(choice, str) else choice.get("name", choice.get("function", {}).get("name"))
                if name not in {tool["function"]["name"] for tool in tools}:
                    raise ModelProviderError("model_tool_choice_invalid")
                payload["tool_choice"] = {"type": "function", "function": {"name": name}}
            elif choice:
                payload["tool_choice"] = "required" if choice == "any" else choice
        return payload

    def _prepare(self, messages, stop, kwargs):
        payload = self._payload(messages, stop=stop, **kwargs)
        preview = None
        if self._before_send or self._on_request:
            # The runtime fence and audit read the provider-neutral preview shape.
            tools = [tool["function"] for tool in kwargs.get("tools") or []]
            preview = preview_request_payload(messages, [{"type": "function", **tool} for tool in tools],
                                              model_name=payload["model"])
        if self._before_send:
            self._before_send(deepcopy(preview))
        return payload, preview, str(uuid4())

    def _trace_request(self, payload, preview, attempt_id):
        if self._on_request:
            safe = deepcopy(preview) if preview is not None else {}
            safe["input"] = [item for item in safe.get("input", []) if item.get("type") != "reasoning"]
            safe["provider"] = "deepseek"
            safe["provider_model"] = payload["model"]
            safe["provider_options"] = {key: payload[key] for key in ("thinking", "reasoning_effort") if key in payload}
            self._on_request({"attempt_id": attempt_id, "payload": safe})

    def _response(self, body, attempt_id, model, observed):
        usage, details = _usage(body.get("usage"))
        observed.update(response_received=True, response_status="completed")
        if usage:
            observed["usage"] = usage
        if details:
            observed["usage_details"] = details
        response_id = body.get("id")
        if isinstance(response_id, str) and 0 < len(response_id) <= 200 and response_id.replace("-", "").replace("_", "").isalnum():
            observed["provider_response_id"] = response_id
        self._emit("model.response_observed", attempt_id, model, **{k: v for k, v in observed.items() if k != "usage_details"},
                   source="response_parse")
        choices = body.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ModelProviderError("model_response_not_completed")
        choice = choices[0]
        message = choice.get("message") or {}
        finish = choice.get("finish_reason")
        if finish not in {"stop", "tool_calls"}:
            observed["response_status"] = "incomplete"
            raise ModelProviderError("model_response_not_completed")
        calls = []
        for call in message.get("tool_calls") or []:
            try:
                function = call["function"]
                args = json.loads(function.get("arguments") or "{}")
                if not isinstance(args, dict) or not function.get("name") or not call.get("id"):
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                raise ModelProviderError("model_tool_call_invalid") from None
            calls.append({"type": "tool_call", "name": function["name"], "args": args, "id": call["id"]})
        content = message.get("content") or ""
        if not content and not calls:
            raise ModelProviderError("model_empty_completed_response")
        reasoning = message.get("reasoning_content") or ""
        if self._on_response:
            self._on_response({"attempt_id": attempt_id, "status": "completed", "model": model,
                               "output": {"text": content, "tool_calls": calls},
                               "usage": observed.get("usage"), "usage_details": observed.get("usage_details"),
                               "provider_response_id": observed.get("provider_response_id"),
                               "response_received": True, "content_status": "pending_validation"})
        self._emit("model.completed", attempt_id, model, status="completed", usage=usage or None,
                   response_received=True, content_status="accepted",
                   provider_response_id=observed.get("provider_response_id"))
        return AIMessage(content=content, tool_calls=calls,
                         additional_kwargs={"reasoning_content": reasoning} if reasoning else {},
                         response_metadata={"provider": "deepseek", "model_name": model, "purpose": self.purpose,
                                            "response_completed": True, "attempt_id": attempt_id,
                                            "finish_reason": "tool_calls" if calls else "stop"},
                         usage_metadata=usage if set(usage) == {"input_tokens", "output_tokens", "total_tokens"} else None)

    def _failure(self, error, attempt_id, model, *, phase, post_started, observed):
        code = error.code if isinstance(error, (CredentialError, ModelProviderError)) else "model_transport_failed"
        diagnostic = safe_transport_diagnostic(phase, _transport_exception_name(error), post_started,
            http_status=getattr(error, "http_status", None), provider_error_code=getattr(error, "provider_error_code", None),
            provider_error_type=getattr(error, "provider_error_type", None))
        unknown = False if observed.get("response_received") else getattr(error, "outcome_unknown", post_started)
        if isinstance(error, ModelProviderError):
            error.outcome_unknown = unknown
        self._emit("model.failed", attempt_id, model, code=code, outcome_unknown=unknown,
                   **{key: value for key, value in observed.items() if key not in {"usage_details"}},
                   content_status="rejected" if observed.get("response_received") else "unavailable", **diagnostic)
        return diagnostic

    def _raise(self, error, diagnostic, observed):
        if isinstance(error, (ModelProviderError, CredentialError, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            for key, value in diagnostic.items():
                setattr(error, key, value)
            raise error
        raise ModelProviderError("model_transport_failed", outcome_unknown=not observed.get("response_received"),
                                 **diagnostic) from None

    def _invoke_wire(self, payload, preview, attempt_id):
        model, phase, post_started, observed = payload["model"], "credential", False, {}
        try:
            key = self._key_reader()
            phase = "client_setup"
            with httpx.Client(transport=self._transport, timeout=self.timeout_seconds, follow_redirects=False) as client:
                phase = "request_observation"
                self._trace_request(payload, preview, attempt_id)
                self._emit("model.sent", attempt_id, model, status="sent")
                for retries in range(2):
                    phase, post_started = "responses_connect", True
                    try:
                        response = client.post(ENDPOINT, headers={"Authorization": "Bearer " + key}, json=payload)
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                        if not _connection_retry_allowed(error, phase, observed, retries):
                            raise
                        time.sleep(_CONNECT_RETRY_DELAY_SECONDS)
                phase = "responses_stream"
                if not response.is_success:
                    raise _sync_http_error(response)
                phase = "response_parse"
                return self._response(response.json(), attempt_id, model, observed)
        except BaseException as error:
            diagnostic = self._failure(error, attempt_id, model, phase=phase, post_started=post_started, observed=observed)
            self._raise(error, diagnostic, observed)

    async def _ainvoke_wire(self, payload, preview, attempt_id):
        model, phase, post_started, observed = payload["model"], "credential", False, {}
        try:
            key = await asyncio.to_thread(self._key_reader)
            phase = "client_setup"
            async with httpx.AsyncClient(transport=self._async_transport or self._transport,
                                         timeout=self.timeout_seconds, follow_redirects=False) as client:
                phase = "request_observation"
                self._trace_request(payload, preview, attempt_id)
                self._emit("model.sent", attempt_id, model, status="sent")
                for retries in range(2):
                    phase, post_started = "responses_connect", True
                    try:
                        async with client.stream("POST", ENDPOINT, headers={"Authorization": "Bearer " + key}, json=payload) as response:
                            phase = "responses_stream"
                            if not response.is_success:
                                raise await _async_http_error(response)
                            raw = await response.aread()
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                        if not _connection_retry_allowed(error, phase, observed, retries):
                            raise
                        await asyncio.sleep(_CONNECT_RETRY_DELAY_SECONDS)
                phase = "response_parse"
                try:
                    body = json.loads(raw)
                except ValueError:
                    raise ModelProviderError("model_response_invalid", outcome_unknown=True) from None
                return self._response(body, attempt_id, model, observed)
        except BaseException as error:
            diagnostic = self._failure(error, attempt_id, model, phase=phase, post_started=post_started, observed=observed)
            self._raise(error, diagnostic, observed)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs):
        payload, preview, attempt = self._prepare(messages, stop, kwargs)
        return ChatResult(generations=[ChatGeneration(message=self._invoke_wire(payload, preview, attempt))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        payload, preview, attempt = self._prepare(messages, stop, kwargs)
        return ChatResult(generations=[ChatGeneration(message=await self._ainvoke_wire(payload, preview, attempt))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        message = self._generate(messages, stop, run_manager, **kwargs).generations[0].message
        chunk = AIMessageChunk(**message.model_dump(exclude={"type"}), chunk_position="last")
        yield ChatGenerationChunk(message=chunk)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        message = (await self._agenerate(messages, stop, run_manager, **kwargs)).generations[0].message
        chunk = AIMessageChunk(**message.model_dump(exclude={"type"}), chunk_position="last")
        yield ChatGenerationChunk(message=chunk)


def build_deepseek_model(*, purpose="agent", observer=None, before_send=None, on_request=None, on_response=None,
                         transport=None, async_transport=None, key_reader=None, settings=None, **_ignored) -> BaseChatModel:
    """No network or key read at build time."""
    settings = settings or agent_model_settings()
    summary = purpose == "summary"
    return DeepSeekAgentModel(model_name=settings["summary_model"] if summary else settings["model"],
                              vision_model_name=settings["vision_model"], reasoning_effort=settings["effort"],
                              thinking=not summary, purpose=purpose, observer=observer, before_send=before_send,
                              on_request=on_request, on_response=on_response, transport=transport,
                              async_transport=async_transport, key_reader=key_reader)
