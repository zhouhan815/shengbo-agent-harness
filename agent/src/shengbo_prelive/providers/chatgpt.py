"""Stateless SIWC Responses adapter for LangChain / Deep Agents.

Installed langchain-openai 1.7.2 supports Responses but its default function-tool
payload is flat and its generic request controls do not enforce this preview
route. This deliberately small BaseChatModel adapter owns the restricted wire
contract; the application still owns all local tool execution and Agent control.
"""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import hashlib
import json
import re
import time
from typing import Any, Callable, Iterator, AsyncIterator
from uuid import uuid4

import httpx
from pydantic import Field, PrivateAttr
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

from .credentials import CredentialManager, CredentialError, RESOURCE


TRANSPORT_PHASES = frozenset({"credential", "client_setup", "catalog_get", "request_observation",
    "responses_connect", "responses_stream", "response_parse", "client_cleanup"})
_TRANSPORT_EXCEPTION_TYPES = (
    httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
    httpx.ProxyError, httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.CloseError,
    httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.UnsupportedProtocol,
    httpx.TooManyRedirects, httpx.DecodingError, httpx.InvalidURL, httpx.HTTPStatusError,
    httpx.TimeoutException, httpx.RequestError, CredentialError, asyncio.CancelledError,
    KeyboardInterrupt, SystemExit, RuntimeError, OSError,
)
TRANSPORT_EXCEPTION_NAMES = frozenset({kind.__name__ for kind in _TRANSPORT_EXCEPTION_TYPES}
                                     | {"ModelProviderError", "OtherException"})
MAX_ERROR_METADATA_BYTES = 16 * 1024
_CONNECT_RETRY_DELAY_SECONDS = 0.2
# A pattern/length check alone could still admit a token-like arbitrary string.
# Only fixed public protocol labels may leave the adapter; messages never do.
PUBLIC_ERROR_CODES = frozenset({
    "invalid_request_error", "invalid_request", "invalid_api_key", "incorrect_api_key",
    "authentication_error", "permission_denied", "access_denied", "model_not_found",
    "context_length_exceeded", "rate_limit_exceeded", "insufficient_quota", "quota_exceeded",
    "unsupported_value", "invalid_value", "missing_required_parameter", "unsupported_parameter",
    "invalid_parameter", "invalid_prompt", "content_policy_violation", "policy_violation",
    "server_error", "internal_error", "internal_server_error", "service_unavailable", "overloaded",
})
PUBLIC_ERROR_TYPES = frozenset({
    "invalid_request_error", "authentication_error", "permission_error", "not_found_error",
    "rate_limit_error", "insufficient_quota", "server_error", "internal_server_error",
    "api_error", "service_unavailable_error", "validation_error", "invalid_payload",
})
_ERROR_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Timeouts/conflicts/unknown 4xx codes do not establish that inference never ran.
_KNOWN_REJECTION_STATUSES = frozenset({401, 403, 404, 405, 406, 410, 411, 413, 414, 415, 422, 428, 429, 431, 451})
_REQUEST_REJECTION_CODES = PUBLIC_ERROR_CODES - {
    "server_error", "internal_error", "internal_server_error", "service_unavailable", "overloaded"}
_REQUEST_REJECTION_TYPES = PUBLIC_ERROR_TYPES - {
    "server_error", "internal_server_error", "api_error", "service_unavailable_error"}


def safe_transport_diagnostic(phase=None, exception_type=None, responses_post_started=None, *,
                              http_status=None, provider_error_code=None, provider_error_type=None):
    """Fixed labels only: never exception text, headers, identity or addresses."""
    result = {}
    if isinstance(phase, str) and phase in TRANSPORT_PHASES:
        result["phase"] = phase
    if isinstance(exception_type, str) and exception_type in TRANSPORT_EXCEPTION_NAMES:
        result["exception_type"] = exception_type
    if isinstance(responses_post_started, bool):
        result["responses_post_started"] = responses_post_started
    if type(http_status) is int and 100 <= http_status <= 599:
        result["http_status"] = http_status
    for key, value, allowed in (("provider_error_code", provider_error_code, PUBLIC_ERROR_CODES),
                                ("provider_error_type", provider_error_type, PUBLIC_ERROR_TYPES)):
        if isinstance(value, str) and _ERROR_LABEL.fullmatch(value) and value in allowed:
            result[key] = value
    return result


def _public_error_metadata(content):
    """Parse only bounded JSON and return allowlisted code/type, never body text."""
    if not isinstance(content, bytes) or len(content) > MAX_ERROR_METADATA_BYTES:
        return {}
    try:
        body = json.loads(content)
    except (ValueError, UnicodeError, RecursionError):
        return {}
    if not isinstance(body, dict):
        return {}
    error = body.get("error", body)
    if not isinstance(error, dict):
        return {}
    return safe_transport_diagnostic(provider_error_code=error.get("code"), provider_error_type=error.get("type"))


def _http_error(status, metadata):
    rejected = status in _KNOWN_REJECTION_STATUSES or (status == 400 and (
        metadata.get("provider_error_code") in _REQUEST_REJECTION_CODES
        or metadata.get("provider_error_type") in _REQUEST_REJECTION_TYPES))
    return ModelProviderError("model_http_error", http_status=status, outcome_unknown=not rejected, **metadata)


def _sync_http_error(response):
    content = bytearray()
    try:
        for chunk in response.iter_bytes(chunk_size=4096):
            if len(content) + len(chunk) > MAX_ERROR_METADATA_BYTES:
                return _http_error(response.status_code, {})
            content.extend(chunk)
    except httpx.HTTPError:
        return _http_error(response.status_code, {})
    return _http_error(response.status_code, _public_error_metadata(bytes(content)))


async def _async_http_error(response):
    content = bytearray()
    try:
        async for chunk in response.aiter_bytes(chunk_size=4096):
            if len(content) + len(chunk) > MAX_ERROR_METADATA_BYTES:
                return _http_error(response.status_code, {})
            content.extend(chunk)
    except httpx.HTTPError:
        return _http_error(response.status_code, {})
    return _http_error(response.status_code, _public_error_metadata(bytes(content)))


def _transport_exception_name(error):
    if isinstance(error, ModelProviderError):
        return "ModelProviderError"
    # Match to fixed library/base labels. An arbitrary subclass name may itself
    # contain sensitive text, so type(error).__name__ is deliberately not used.
    return next((kind.__name__ for kind in _TRANSPORT_EXCEPTION_TYPES if isinstance(error, kind)), "OtherException")


def _connection_retry_allowed(error, phase, observed, retries):
    """Only a connection not yet established may be attempted once again.

    Do not broaden this to generic RequestError or replay an entered response,
    body read/write, HTTP rejection, observer failure or unknown model output.
    The existing Client retains its configured transport and environment proxy.
    """
    return (retries == 0 and phase == "responses_connect" and not observed
            and isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)))


class ModelProviderError(RuntimeError):
    def __init__(self, code: str, *, http_status: int | None = None, outcome_unknown: bool = False,
                 phase=None, exception_type=None, responses_post_started=None,
                 provider_error_code=None, provider_error_type=None):
        self.code, self.outcome_unknown = code, outcome_unknown
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        for key, value in safe_transport_diagnostic(phase, exception_type, responses_post_started,
                http_status=self.http_status, provider_error_code=provider_error_code, provider_error_type=provider_error_type).items():
            setattr(self, key, value)
        super().__init__(code)


def _events(lines: Iterator[str]):
    parts = []
    for line in lines:
        if not line:
            if parts:
                raw = "\n".join(parts)
                parts = []
                if raw != "[DONE]":
                    try:
                        yield json.loads(raw)
                    except ValueError:
                        raise ModelProviderError("model_sse_invalid", outcome_unknown=True) from None
        elif line.startswith("data:"):
            parts.append(line[5:].lstrip())
    if parts and "\n".join(parts) != "[DONE]":
        try:
            yield json.loads("\n".join(parts))
        except ValueError:
            raise ModelProviderError("model_sse_invalid", outcome_unknown=True) from None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in content)
    return json.dumps(content, ensure_ascii=False)


def _content(content: Any, role: str):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ModelProviderError("model_message_content_unsupported")
    converted = []
    for part in content:
        if isinstance(part, str):
            converted.append({"type": "input_text" if role != "assistant" else "output_text", "text": part})
        elif part.get("type") in ("text", "input_text", "output_text"):
            converted.append({"type": "input_text" if role != "assistant" else "output_text", "text": part.get("text", "")})
        elif role != "assistant" and part.get("type") == "image_url":
            value = part.get("image_url")
            converted.append({"type": "input_image", "image_url": _image_data_url(value.get("url") if isinstance(value, dict) else value)})
        elif role != "assistant" and part.get("type") == "input_image":
            converted.append({"type": "input_image", "image_url": _image_data_url(part.get("image_url"))})
        elif part.get("type") in ("reasoning", "non_standard"):
            # Not plaintext internal reasoning. Encrypted replay is handled below.
            continue
        else:
            raise ModelProviderError("model_message_content_unsupported")
    return converted


def _image_data_url(value):
    """Only the material service's bounded embedded images, never arbitrary URLs."""
    if not isinstance(value, str) or len(value) > 14_000_000:
        raise ModelProviderError("model_image_input_invalid")
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]*={0,2})", value)
    if not match:
        raise ModelProviderError("model_image_data_url_required")
    try:
        content = base64.b64decode(match[2], validate=True)
        kind = match[1]
        if not content or len(content) > 10 * 1024 * 1024:
            raise ValueError()
        valid = ((kind == "png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
                 or (kind == "jpeg" and content.startswith(b"\xff\xd8\xff"))
                 or (kind == "webp" and content.startswith(b"RIFF") and content[8:12] == b"WEBP"))
        if not valid:
            raise ValueError()
    except ValueError:
        raise ModelProviderError("model_image_input_invalid") from None
    return value


def payload_text_chars(payload):
    """Text/tool budget size, excluding separately bounded embedded image bytes."""
    view = deepcopy(payload)
    for item in view.get("input", []):
        for key in ("content", "output"):
            for block in item.get(key, []) if isinstance(item.get(key), list) else []:
                if block.get("type") == "input_image":
                    block["image_url"] = "[bounded embedded image]"
    return len(json.dumps(view, ensure_ascii=False))


def _public_usage(value):
    """Missing usage stays unknown; bool/negative values are not token counts."""
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in ("input_tokens", "output_tokens", "total_tokens")
            if type(value.get(key)) is int and value[key] >= 0}


def _public_usage_details(value):
    """Numeric cache/reasoning breakdown only; kept apart from the strict usage triple.

    ``usage`` stays exactly input/output/total for ledgers and usage_metadata.
    Missing details remain absent (unknown), never zero.
    """
    if not isinstance(value, dict):
        return {}
    result = {}
    for source, field, name in (("input_tokens_details", "cached_tokens", "cached_input_tokens"),
                                ("output_tokens_details", "reasoning_tokens", "reasoning_output_tokens")):
        details = value.get(source)
        if isinstance(details, dict) and type(details.get(field)) is int and details[field] >= 0:
            result[name] = details[field]
    return result


def _public_output(value):
    """Only published text and local call arguments, never reasoning blocks."""
    output = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            output.append({key: deepcopy(item[key]) for key in ("id", "type", "role", "status") if key in item} | {
                "content": [{key: deepcopy(part[key]) for key in ("type", "text", "refusal", "annotations") if key in part}
                            for part in item.get("content", []) if isinstance(part, dict)
                            and part.get("type") in {"output_text", "refusal"}]})
        elif item.get("type") == "function_call":
            public = {key: deepcopy(item[key]) for key in
                      ("id", "type", "name", "namespace", "call_id", "status") if key in item}
            arguments = item.get("arguments")
            try:
                parsed = json.loads(arguments) if isinstance(arguments, str) else None
                if not isinstance(parsed, dict):
                    raise ValueError()
            except (TypeError, ValueError):
                # Malformed provider output is diagnostic data, not legitimate
                # tool parameters. Never preserve its arbitrary exception-like
                # raw text, even when it does not match known secret patterns.
                raw = arguments.encode("utf-8", errors="replace") if isinstance(arguments, str) else b""
                public["arguments"] = None
                public["arguments_diagnostic"] = {"omitted": True, "reason": "invalid_json_object",
                    "characters": len(arguments) if isinstance(arguments, str) else None,
                    "sha256": hashlib.sha256(raw).hexdigest() if isinstance(arguments, str) else None}
            else:
                from ..observability import sanitize
                public["arguments"] = json.dumps(sanitize(parsed), ensure_ascii=False)
            output.append(public)
    return output


class _ResponseAccumulator:
    def __init__(self, on_observed=None):
        self.completed = None
        self.output_items = {}
        self.text_parts = {}
        self.argument_parts = {}
        self.event_shapes = {}
        self.on_observed = on_observed

    def accept(self, event):
        if not isinstance(event, dict):
            raise ModelProviderError("model_sse_invalid", outcome_unknown=True)
        kind = event.get("type", "")
        # Record public usage before protocol/content checks. A provider may
        # return usage with failed/incomplete output or before a stream drops.
        response = event.get("response")
        if self.on_observed and isinstance(response, dict) and (
                _public_usage(response.get("usage")) or kind in {
                    "response.completed", "response.failed", "response.incomplete"}):
            self.on_observed(response, kind)
        elif self.on_observed and _public_usage(event.get("usage")):
            self.on_observed({"usage": event["usage"]}, kind)
        shape = self.event_shapes.setdefault(kind, {"count": 0, "fields": set()})
        shape["count"] += 1
        shape["fields"].update(str(key) for key in event)
        if kind in ("response.failed", "response.incomplete", "error"):
            raise ModelProviderError("model_response_not_completed", outcome_unknown=kind == "error")
        if kind in {"response.output_item.added", "response.output_item.done"}:
            index = event.get("output_index", len(self.output_items))
            self.output_items[index] = event.get("item", {})
        if kind == "response.output_text.delta":
            key = (event.get("output_index", 0), event.get("content_index", 0))
            self.text_parts[key] = self.text_parts.get(key, "") + event.get("delta", "")
        if kind == "response.output_text.done":
            key = (event.get("output_index", 0), event.get("content_index", 0))
            self.text_parts[key] = event.get("text", self.text_parts.get(key, ""))
        if kind == "response.function_call_arguments.delta":
            index = event.get("output_index", 0)
            self.argument_parts[index] = self.argument_parts.get(index, "") + event.get("delta", "")
        if kind == "response.function_call_arguments.done":
            index = event.get("output_index", 0)
            self.argument_parts[index] = event.get("arguments", self.argument_parts.get(index, ""))
        if kind == "response.completed":
            response = event.get("response")
            if not isinstance(response, dict) or response.get("status") != "completed":
                raise ModelProviderError("model_response_not_completed")
            self.completed = response

    def finish(self):
        if self.completed is None:
            raise ModelProviderError("model_stream_interrupted", outcome_unknown=True)
        # Some streaming routes omit completed.output or send an empty list even
        # though authoritative item/text done events were emitted. Reconstruct
        # only after the real completed fence, never after a disconnected stream.
        if not self.completed.get("output"):
            for (index, content_index), text in sorted(self.text_parts.items()):
                item = self.output_items.setdefault(index, {"type": "message", "role": "assistant", "content": []})
                if item.get("type") == "message":
                    content = item.setdefault("content", [])
                    while len(content) <= content_index:
                        content.append({"type": "output_text", "text": ""})
                    if not content[content_index].get("text"):
                        content[content_index] = {"type": "output_text", "text": text}
            for index, arguments in self.argument_parts.items():
                item = self.output_items.get(index)
                if item and item.get("type") == "function_call" and not item.get("arguments"):
                    item["arguments"] = arguments
            if self.output_items:
                self.completed["output"] = [self.output_items[k] for k in sorted(self.output_items)]
        return self.completed

    def safe_shape(self):
        return {"event_shapes": {key: {"count": value["count"], "fields": sorted(value["fields"])} for key, value in self.event_shapes.items()},
                "completed_fields": sorted(self.completed) if self.completed else [],
                "completed_output_count": len((self.completed or {}).get("output", [])),
                "stream_item_types": [self.output_items[k].get("type") for k in sorted(self.output_items)],
                "text_segments": len(self.text_parts), "argument_segments": len(self.argument_parts)}


HARNESS_PROVIDER_KEY = "shengbo_chatgpt"


class ChatGPTPlanModel(BaseChatModel):
    model_name: str = "gpt-6.1-sol"
    timeout_seconds: float = 120
    max_input_chars: int = 600000
    verify_catalog: bool = True
    catalog_ttl_seconds: float = 600
    # These public fields are safe in tracing / dump. All capabilities are local.
    profile: dict = Field(default_factory=lambda: {"tool_calling": True, "max_input_tokens": 128000})
    _credentials: Any = PrivateAttr()
    _observer: Any = PrivateAttr(default=None)
    _before_send: Any = PrivateAttr(default=None)
    _on_request: Any = PrivateAttr(default=None)
    _on_response: Any = PrivateAttr(default=None)
    _transport: Any = PrivateAttr(default=None)
    _async_transport: Any = PrivateAttr(default=None)
    _catalog_at: float = PrivateAttr(default=0)

    def __init__(self, *, credentials=None, observer=None, before_send=None,
                 on_request=None, on_response=None, transport=None, async_transport=None, **kwargs):
        super().__init__(**kwargs)
        self._credentials = credentials if credentials is not None else CredentialManager()
        self._observer, self._before_send = observer, before_send
        self._on_request, self._on_response = on_request, on_response
        self._transport, self._async_transport = transport, async_transport

    @property
    def _llm_type(self):
        return "shengbo-chatgpt-plan-responses"

    def _get_ls_params(self, stop=None, **kwargs):
        # Stable provider key so deepagents resolves this app's HarnessProfile
        # (see runtime.HARNESS_PROVIDER_KEY); otherwise it is derived from the
        # class name. LangSmith tracing itself stays disabled by the service.
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_provider"] = HARNESS_PROVIDER_KEY
        params["ls_model_name"] = self.model_name
        return params

    @property
    def _identifying_params(self):
        return {"model_name": self.model_name, "provider": "chatgpt_plan"}

    def _emit(self, kind, attempt_id, **safe):
        if self._observer:
            self._observer({"type": kind, "attempt_id": attempt_id, "provider": "chatgpt_plan",
                            "model": self.model_name, **safe})

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        converted = []
        for tool in tools:
            item = convert_to_openai_tool(tool)
            if item.get("type") != "function":
                raise ModelProviderError("only_local_function_tools_supported")
            function = item.get("function", item)
            converted.append({"type": "function", **function})
        names = [x["name"] for x in converted]
        if len(names) != len(set(names)):
            raise ModelProviderError("duplicate_local_tool_name")
        return self.bind(tools=converted, tool_choice=tool_choice, **kwargs)

    def _payload(self, messages, *, stop=None, **kwargs):
        # Refuse unsupported wire controls rather than silently ignoring them.
        unsupported = set(kwargs) - {"tools", "tool_choice", "parallel_tool_calls"}
        if unsupported or stop:
            raise ModelProviderError("model_request_option_unsupported")
        inputs = []
        for message in messages:
            if isinstance(message, SystemMessage):
                inputs.append({"role": "developer", "content": _content(message.content, "developer")})
            elif isinstance(message, HumanMessage):
                inputs.append({"role": "user", "content": _content(message.content, "user")})
            elif isinstance(message, ToolMessage):
                # Responses accepts image parts as function output. Flattening
                # these to text silently discarded an embedded product image.
                visual = isinstance(message.content, list) and any(
                    isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}
                    for part in message.content)
                inputs.append({"type": "function_call_output", "call_id": message.tool_call_id,
                               "output": _content(message.content, "user") if visual else _text(message.content)})
            elif isinstance(message, AIMessage):
                replay = message.additional_kwargs.get("responses_replay_items")
                if replay:
                    inputs.extend(deepcopy(replay))
                else:
                    reasoning = message.additional_kwargs.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        # DeepSeek thinking text is replayed as input; count it in previews.
                        inputs.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": reasoning}]})
                    if message.content:
                        inputs.append({"role": "assistant", "content": _content(message.content, "assistant")})
                    for call in message.tool_calls:
                        inputs.append({"type": "function_call", "call_id": call["id"],
                                       "name": call["name"], "namespace": "functions",
                                       "arguments": json.dumps(call["args"], ensure_ascii=False)})
            elif getattr(message, "role", None) == "developer":
                inputs.append({"role": "developer", "content": _content(message.content, "developer")})
            else:
                raise ModelProviderError("model_message_type_unsupported")
        payload = {"model": self.model_name, "input": inputs, "store": False, "stream": True,
                   "include": ["reasoning.encrypted_content"]}
        tools = kwargs.get("tools", [])
        if tools:
            payload["tools"] = [{"type": "namespace", "name": "functions",
                                 "description": "Locally executed application tools. The application controls permission and execution.",
                                 "tools": tools}]
            choice = kwargs.get("tool_choice")
            if choice not in (None, "auto", "none", "required", "any"):
                # Restrict names by narrowing the tools; required is supported by
                # both namespace and additional_tools preview forms.
                name = choice if isinstance(choice, str) else choice.get("name", choice.get("function", {}).get("name"))
                tools = [t for t in tools if t["name"] == name]
                if not tools:
                    raise ModelProviderError("model_tool_choice_invalid")
                payload["tools"][0]["tools"] = tools
                choice = "required"
            if choice:
                payload["tool_choice"] = "required" if choice == "any" else choice
            if "parallel_tool_calls" in kwargs:
                payload["parallel_tool_calls"] = bool(kwargs["parallel_tool_calls"])
        if payload_text_chars(payload) > self.max_input_chars:
            raise ModelProviderError("model_payload_budget_exceeded")
        return payload

    def _check_catalog(self, response):
        if not response.is_success:
            raise ModelProviderError("model_catalog_unavailable", http_status=response.status_code,
                                     **_public_error_metadata(response.content[:MAX_ERROR_METADATA_BYTES + 1]))
        try:
            body = response.json()
            rows = body.get("models", body.get("data", []))
            available = any((r.get("slug") or r.get("id")) == self.model_name and r.get("visibility", "list") == "list" for r in rows)
        except Exception:
            raise ModelProviderError("model_catalog_invalid") from None
        if not available:
            raise ModelProviderError("requested_model_not_authorized")
        self._catalog_at = time.monotonic()

    def _prepare(self, messages, stop, kwargs):
        payload = self._payload(messages, stop=stop, **kwargs)
        if self._before_send:
            self._before_send(deepcopy(payload))
        return payload, str(uuid4())

    def _trace_request(self, payload, attempt_id):
        if self._on_request:
            safe = deepcopy(payload)
            # Opaque private reasoning is replayed to the model only. The trace
            # preserves all human-visible instructions/history/tool parameters.
            safe["input"] = [i for i in safe["input"] if i.get("type") != "reasoning"]
            self._on_request({"attempt_id": attempt_id, "payload": safe})

    def _observe_response(self, response, attempt_id, observed, source):
        usage = _public_usage(response.get("usage"))
        if usage:
            observed["usage"] = {**observed.get("usage", {}), **usage}
        details = _public_usage_details(response.get("usage"))
        if details:
            observed["usage_details"] = {**observed.get("usage_details", {}), **details}
        status = response.get("status")
        if status in {"completed", "failed", "incomplete", "in_progress", "queued", "cancelled"}:
            observed["response_status"] = status
        response_id = response.get("id")
        if isinstance(response_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", response_id):
            observed["provider_response_id"] = response_id
        observed["response_received"] = True
        self._emit("model.response_observed", attempt_id, **observed, source=source)

    def _archive_response(self, response, attempt_id, observed):
        # This runs in the actual response_parse phase, before local content
        # validation. Stream-time public usage has already reached the ledger.
        if self._on_response:
            self._on_response({"attempt_id": attempt_id, "status": observed.get("response_status", "unknown"),
                               "model": self.model_name, "output": _public_output(response.get("output")),
                               "usage": observed.get("usage"),
                               "usage_details": observed.get("usage_details"),
                               "provider_response_id": observed.get("provider_response_id"),
                               "response_received": True, "content_status": "pending_validation"})

    def _response(self, response, attempt_id, observed=None):
        observed = observed if observed is not None else {}
        if not observed.get("response_received"):
            self._observe_response(response, attempt_id, observed, "response_parse")
        self._archive_response(response, attempt_id, observed)
        output, calls, replay = [], [], []
        for item in response.get("output", []):
            kind = item.get("type")
            if kind == "message":
                replay.append(deepcopy(item))
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        output.append(part.get("text", ""))
                    elif part.get("type") == "refusal":
                        output.append(part.get("refusal", ""))
            elif kind == "function_call":
                try:
                    args = json.loads(item["arguments"])
                    if (not isinstance(args, dict) or not item.get("name") or not item.get("call_id")
                            or item.get("namespace", "functions") != "functions"):
                        raise ValueError()
                except (KeyError, ValueError, TypeError):
                    raise ModelProviderError("model_tool_call_invalid") from None
                calls.append({"type": "tool_call", "name": item["name"], "args": args, "id": item["call_id"]})
                replay.append(deepcopy(item))
            elif kind == "reasoning":
                if item.get("encrypted_content"):
                    replay.append({k: item[k] for k in ("id", "type", "encrypted_content") if k in item})
                    replay[-1]["summary"] = []
            else:
                raise ModelProviderError("model_hosted_output_unsupported")
        safe_usage = observed.get("usage", _public_usage(response.get("usage")))
        if not output and not calls:
            raise ModelProviderError("model_empty_completed_response")
        self._emit("model.completed", attempt_id, status="completed", usage=safe_usage or None,
                   response_received=True, content_status="accepted",
                   provider_response_id=observed.get("provider_response_id"))
        return AIMessage(content="".join(output), tool_calls=calls,
                         additional_kwargs={"responses_replay_items": replay},
                         response_metadata={"provider": "chatgpt_plan", "model_name": self.model_name,
                                            "response_completed": True, "attempt_id": attempt_id,
                                            "finish_reason": "tool_calls" if calls else "stop"},
                         usage_metadata=safe_usage if set(safe_usage) == {
                             "input_tokens", "output_tokens", "total_tokens"} else None)

    def _invoke_wire(self, payload, attempt_id):
        phase, post_started = "credential", False
        observed = {}
        try:
            token = self._credentials.get_access_token()
            phase = "client_setup"
            with httpx.Client(transport=self._transport, timeout=self.timeout_seconds, follow_redirects=False) as client:
                headers = {"Authorization": "Bearer " + token}
                if self.verify_catalog and time.monotonic() - self._catalog_at > self.catalog_ttl_seconds:
                    phase = "catalog_get"
                    self._check_catalog(client.get(RESOURCE + "/models", headers=headers))
                phase = "request_observation"
                self._trace_request(payload, attempt_id)
                self._emit("model.sent", attempt_id, status="sent")
                accumulator = _ResponseAccumulator(lambda value, source: self._observe_response(value, attempt_id, observed, source))
                for retries in range(2):
                    phase, post_started = "responses_connect", True
                    try:
                        with client.stream("POST", RESOURCE + "/responses", headers=headers, json=payload) as response:
                            phase = "responses_stream"
                            if not response.is_success:
                                raise _sync_http_error(response)
                            for event in _events(response.iter_lines()):
                                accumulator.accept(event)
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                        if not _connection_retry_allowed(error, phase, observed, retries):
                            raise
                        self._emit("model.connection_retry", attempt_id, phase="responses_connect",
                            retry_number=1, max_retries=1, delay_ms=200,
                            exception_type=_transport_exception_name(error))
                        time.sleep(_CONNECT_RETRY_DELAY_SECONDS)
                phase = "response_parse"
                completed = accumulator.finish()
                self._emit("model.protocol", attempt_id, shape=accumulator.safe_shape())
                phase = "client_cleanup"
            phase = "response_parse"
            return self._response(completed, attempt_id, observed)
        except BaseException as error:
            diagnostic = self._failure(error, attempt_id, phase=phase, responses_post_started=post_started, observed=observed)
            if isinstance(error, (ModelProviderError, CredentialError, KeyboardInterrupt, SystemExit)):
                for key, value in diagnostic.items():
                    setattr(error, key, value)
                raise
            raise ModelProviderError("model_transport_failed", outcome_unknown=self._outcome_unknown(error, observed), **diagnostic) from None

    async def _ainvoke_wire(self, payload, attempt_id):
        phase, post_started = "credential", False
        observed = {}
        try:
            token = await asyncio.to_thread(self._credentials.get_access_token)
            transport = self._async_transport or self._transport
            phase = "client_setup"
            async with httpx.AsyncClient(transport=transport, timeout=self.timeout_seconds, follow_redirects=False) as client:
                headers = {"Authorization": "Bearer " + token}
                if self.verify_catalog and time.monotonic() - self._catalog_at > self.catalog_ttl_seconds:
                    phase = "catalog_get"
                    self._check_catalog(await client.get(RESOURCE + "/models", headers=headers))
                phase = "request_observation"
                self._trace_request(payload, attempt_id)
                self._emit("model.sent", attempt_id, status="sent")
                accumulator, frame = _ResponseAccumulator(lambda value, source: self._observe_response(value, attempt_id, observed, source)), []
                for retries in range(2):
                    phase, post_started = "responses_connect", True
                    try:
                        async with client.stream("POST", RESOURCE + "/responses", headers=headers, json=payload) as response:
                            phase = "responses_stream"
                            if not response.is_success:
                                raise await _async_http_error(response)
                            async for line in response.aiter_lines():
                                if line:
                                    frame.append(line)
                                else:
                                    for event in _events(iter([*frame, ""])):
                                        accumulator.accept(event)
                                    frame = []
                            if frame:
                                for event in _events(iter(frame)):
                                    accumulator.accept(event)
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                        if not _connection_retry_allowed(error, phase, observed, retries):
                            raise
                        self._emit("model.connection_retry", attempt_id, phase="responses_connect",
                            retry_number=1, max_retries=1, delay_ms=200,
                            exception_type=_transport_exception_name(error))
                        await asyncio.sleep(_CONNECT_RETRY_DELAY_SECONDS)
                phase = "response_parse"
                completed = accumulator.finish()
                self._emit("model.protocol", attempt_id, shape=accumulator.safe_shape())
                phase = "client_cleanup"
            phase = "response_parse"
            return self._response(completed, attempt_id, observed)
        except BaseException as error:
            diagnostic = self._failure(error, attempt_id, phase=phase, responses_post_started=post_started, observed=observed)
            if isinstance(error, (ModelProviderError, CredentialError, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                for key, value in diagnostic.items():
                    setattr(error, key, value)
                raise
            raise ModelProviderError("model_transport_failed", outcome_unknown=self._outcome_unknown(error, observed), **diagnostic) from None

    @staticmethod
    def _outcome_unknown(error, observed):
        return False if (observed or {}).get("response_status") in {
            "completed", "failed", "incomplete"} else getattr(error, "outcome_unknown", True)

    def _failure(self, error, attempt_id, *, phase, responses_post_started, observed=None):
        code = error.code if isinstance(error, (CredentialError, ModelProviderError)) else "model_transport_failed"
        diagnostic = safe_transport_diagnostic(phase, _transport_exception_name(error), responses_post_started,
            http_status=getattr(error, "http_status", None), provider_error_code=getattr(error, "provider_error_code", None),
            provider_error_type=getattr(error, "provider_error_type", None))
        unknown = self._outcome_unknown(error, observed)
        if isinstance(error, ModelProviderError):
            error.outcome_unknown = unknown
        self._emit("model.failed", attempt_id, code=code, outcome_unknown=unknown,
                   **{key: value for key, value in (observed or {}).items() if key != "output_archived"},
                   content_status="rejected" if (observed or {}).get("response_status") == "completed" else "unavailable", **diagnostic)
        return diagnostic

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs):
        payload, attempt = self._prepare(messages, stop, kwargs)
        return ChatResult(generations=[ChatGeneration(message=self._invoke_wire(payload, attempt))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        payload, attempt = self._prepare(messages, stop, kwargs)
        return ChatResult(generations=[ChatGeneration(message=await self._ainvoke_wire(payload, attempt))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        # Completion-fenced streaming: the upstream is SSE, but partial text is
        # provisional. Emit one committed chunk only after response.completed.
        message = self._generate(messages, stop, run_manager, **kwargs).generations[0].message
        chunk = AIMessageChunk(**message.model_dump(exclude={"type"}), chunk_position="last")
        result = ChatGenerationChunk(message=chunk)
        if run_manager:
            run_manager.on_llm_new_token(chunk.text, chunk=result)
        yield result

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        message = (await self._agenerate(messages, stop, run_manager, **kwargs)).generations[0].message
        chunk = AIMessageChunk(**message.model_dump(exclude={"type"}), chunk_position="last")
        result = ChatGenerationChunk(message=chunk)
        if run_manager:
            await run_manager.on_llm_new_token(chunk.text, chunk=result)
        yield result


def preview_request_payload(messages, tools, *, model_name="gpt-6.1-sol", model_settings=None):
    """Build the real wire shape offline, without auth, observers or a send.

    Bypass only the character precheck for measuring an oversized candidate;
    the actual model keeps both its character limit and runtime token fence.
    """
    preview = ChatGPTPlanModel.model_construct(model_name=model_name, max_input_chars=2**63 - 1)
    bound = preview.bind_tools(tools)
    return preview._payload(messages, **{**bound.kwargs, **(model_settings or {})})


def build_chatgpt_model(model="gpt-6.1-sol", credentials=None, observer=None, *, before_send=None,
                        on_request=None, on_response=None, transport=None, async_transport=None,
                        timeout=120, max_input_chars=600000, verify_catalog=True, **kwargs) -> BaseChatModel:
    """Safe model factory. No network, credential read, refresh or login at build."""
    return ChatGPTPlanModel(model_name=model, credentials=credentials, observer=observer,
                            before_send=before_send, on_request=on_request, on_response=on_response,
                            transport=transport, async_transport=async_transport,
                            timeout_seconds=timeout, max_input_chars=max_input_chars,
                            verify_catalog=verify_catalog, **kwargs)
