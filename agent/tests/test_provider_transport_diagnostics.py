"""Transport diagnostics stay safe with one bounded connection-only retry."""
import asyncio
from collections import Counter
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from shengbo_prelive.providers.chatgpt import ModelProviderError, build_chatgpt_model
from shengbo_prelive.providers.credentials import CredentialError
from test_provider_chatgpt import sse_response


FAKE_TOKEN = "fake-diagnostics-access-token-never-record"
FAKE_HEADER = "X-Fake-Secret-Header"
FAKE_URL = "https://private-fake.example.test/credentials?token=" + FAKE_TOKEN
EVIL_NAME = "EvilClass_" + FAKE_TOKEN
SENSITIVE_MESSAGE = "private failure " + FAKE_TOKEN + " Authorization Bearer " + FAKE_HEADER + " " + FAKE_URL
EvilException = type(EVIL_NAME, (Exception,), {})

PHASES = {"credential", "client_setup", "catalog_get", "request_observation", "responses_connect",
          "responses_stream", "response_parse", "client_cleanup"}
EXCEPTION_TYPES = {"ConnectError", "ConnectTimeout", "ReadTimeout", "CredentialError", "ModelProviderError",
                   "CancelledError", "RuntimeError", "OSError", "OtherException"}


class Credentials:
    def __init__(self, scenario):
        self.scenario, self.calls = scenario, 0

    def get_access_token(self):
        self.calls += 1
        if self.scenario == "credential_runtime":
            raise RuntimeError(SENSITIVE_MESSAGE)
        if self.scenario == "credential_known":
            raise CredentialError("chatgpt_reauthorization_required")
        if self.scenario == "credential_oserror":
            raise OSError(SENSITIVE_MESSAGE)
        if self.scenario == "credential_evil":
            raise EvilException(SENSITIVE_MESSAGE)
        return FAKE_TOKEN


class TimeoutStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, request):
        self.request = request

    def __iter__(self):
        yield b'data: {"type":"response.output_text.delta","delta":"provisional"}\n\n'
        raise httpx.ReadTimeout(SENSITIVE_MESSAGE, request=self.request)

    async def __aiter__(self):
        for chunk in self:
            yield chunk


class CleanupFailureTransport(httpx.MockTransport):
    def close(self):
        raise RuntimeError(SENSITIVE_MESSAGE)

    async def aclose(self):
        raise RuntimeError(SENSITIVE_MESSAGE)


def make_case(scenario):
    case = SimpleNamespace(scenario=scenario, requests=[], events=[], observed_requests=[], observed_responses=[])
    case.credentials = Credentials(scenario)

    def handler(request):
        case.requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + FAKE_TOKEN
        if request.method == "GET":
            if scenario == "catalog_connect_timeout":
                raise httpx.ConnectTimeout(SENSITIVE_MESSAGE, request=request)
            if scenario == "catalog_invalid":
                return httpx.Response(200, text=SENSITIVE_MESSAGE)
            if scenario == "catalog_missing_model":
                return httpx.Response(200, json={"models": [{"slug": "another-model"}]})
            return httpx.Response(200, json={"models": [{"slug": "gpt-6.1-sol", "visibility": "list"}]})
        assert request.method == "POST" and request.url.path.endswith("/responses")
        if scenario == "post_connect_error":
            raise httpx.ConnectError(SENSITIVE_MESSAGE, request=request)
        if scenario == "post_evil_error":
            raise EvilException(SENSITIVE_MESSAGE)
        if scenario == "stream_read_timeout":
            return httpx.Response(200, headers={"Content-Type": "text/event-stream", FAKE_HEADER: FAKE_TOKEN},
                                  stream=TimeoutStream(request))
        if scenario == "sse_invalid_json":
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  text="data: " + SENSITIVE_MESSAGE + "\n\n")
        if scenario == "response_invalid_tool":
            return sse_response([{"type": "function_call", "namespace": "functions", "name": "local_tool",
                "call_id": "invalid-arguments", "arguments": SENSITIVE_MESSAGE}])
        return sse_response()

    def observer(event):
        case.events.append(event)
        if scenario == "observer_runtime" and event["type"] == "model.sent":
            raise RuntimeError(SENSITIVE_MESSAGE)

    def on_request(event):
        case.observed_requests.append(event)
        if scenario == "request_hook_runtime":
            raise RuntimeError(SENSITIVE_MESSAGE)
        if scenario == "request_hook_evil":
            raise EvilException(SENSITIVE_MESSAGE)

    def on_response(event):
        case.observed_responses.append(event)
        if scenario == "response_hook_runtime":
            raise RuntimeError(SENSITIVE_MESSAGE)

    transport = CleanupFailureTransport(handler) if scenario == "client_cleanup" else httpx.MockTransport(handler)
    case.model = build_chatgpt_model(credentials=case.credentials, transport=transport, async_transport=transport,
        observer=observer, on_request=on_request, on_response=on_response, verify_catalog=True)
    return case


async def run(case, mode):
    if mode == "sync":
        return case.model.invoke("诊断测试。")
    return await case.model.ainvoke("诊断测试。")


def assert_safe_public_diagnostics(case, error):
    public = json.dumps({"error": str(error), "repr": repr(error), "fields": error.__dict__,
        "events": case.events, "requests": case.observed_requests, "responses": case.observed_responses,
        "model": case.model.model_dump()}, ensure_ascii=False, default=str)
    for forbidden in (FAKE_TOKEN, FAKE_HEADER, FAKE_URL, EVIL_NAME, "private failure", "Authorization", "Bearer "):
        assert forbidden not in public
    assert not any(event["type"] == "model.completed" for event in case.events)


# Unknown/outcome semantics intentionally follow the existing provider contract:
# raw exceptions before a terminal supplier response stay unknown even before
# POST. A received completed response is known even when later local archival or
# client cleanup fails; its already observed usage remains in the attempt ledger.
# POST-started is a separate observation, never a retry grant.
FAILURES = [
    ("credential_runtime", "credential", "RuntimeError", "model_transport_failed", True, 0, 0),
    ("credential_known", "credential", "CredentialError", "chatgpt_reauthorization_required", True, 0, 0),
    ("credential_oserror", "credential", "OSError", "model_transport_failed", True, 0, 0),
    ("credential_evil", "credential", "OtherException", "model_transport_failed", True, 0, 0),
    ("client_setup", "client_setup", "RuntimeError", "model_transport_failed", True, 0, 0),
    ("catalog_connect_timeout", "catalog_get", "ConnectTimeout", "model_transport_failed", True, 1, 0),
    ("catalog_invalid", "catalog_get", "ModelProviderError", "model_catalog_invalid", False, 1, 0),
    ("catalog_missing_model", "catalog_get", "ModelProviderError", "requested_model_not_authorized", False, 1, 0),
    ("request_hook_runtime", "request_observation", "RuntimeError", "model_transport_failed", True, 1, 0),
    ("request_hook_evil", "request_observation", "OtherException", "model_transport_failed", True, 1, 0),
    ("observer_runtime", "request_observation", "RuntimeError", "model_transport_failed", True, 1, 0),
    ("post_connect_error", "responses_connect", "ConnectError", "model_transport_failed", True, 1, 2),
    ("post_evil_error", "responses_connect", "OtherException", "model_transport_failed", True, 1, 1),
    ("stream_read_timeout", "responses_stream", "ReadTimeout", "model_transport_failed", True, 1, 1),
    ("sse_invalid_json", "responses_stream", "ModelProviderError", "model_sse_invalid", True, 1, 1),
    ("response_invalid_tool", "response_parse", "ModelProviderError", "model_tool_call_invalid", False, 1, 1),
    ("response_hook_runtime", "response_parse", "RuntimeError", "model_transport_failed", False, 1, 1),
    ("client_cleanup", "client_cleanup", "RuntimeError", "model_transport_failed", False, 1, 1),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(("scenario", "phase", "exception_type", "code", "unknown", "gets", "posts"), FAILURES)
async def test_transport_failure_diagnostics_are_safe_precise_and_retry_only_connect_once(mode, scenario, phase, exception_type, code, unknown, gets, posts):
    case = make_case(scenario)
    target = "shengbo_prelive.providers.chatgpt.httpx." + ("Client" if mode == "sync" else "AsyncClient")
    if scenario == "client_setup":
        with patch(target, side_effect=RuntimeError(SENSITIVE_MESSAGE)):
            with pytest.raises((ModelProviderError, CredentialError)) as caught:
                await run(case, mode)
    else:
        with pytest.raises((ModelProviderError, CredentialError)) as caught:
            await run(case, mode)
    error = caught.value
    if scenario == "credential_known":
        assert isinstance(error, CredentialError), "known credential codes keep their established exception class"
    else:
        assert isinstance(error, ModelProviderError)
    assert error.code == code and str(error) == code
    assert error.phase == phase and error.phase in PHASES
    assert error.exception_type == exception_type and error.exception_type in EXCEPTION_TYPES
    assert error.responses_post_started is bool(posts)
    if isinstance(error, ModelProviderError):
        assert error.outcome_unknown is unknown
    failures = [event for event in case.events if event["type"] == "model.failed"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["phase"] == error.phase
    assert failure["exception_type"] == error.exception_type
    assert failure["responses_post_started"] == error.responses_post_started
    assert failure["code"] == code and failure["outcome_unknown"] is unknown
    assert Counter(request.method for request in case.requests) == Counter({"GET": gets, "POST": posts})
    assert len([event for event in case.events if event["type"] == "model.connection_retry"]) == (1 if scenario == "post_connect_error" else 0)
    assert case.credentials.calls == 1
    assert_safe_public_diagnostics(case, error)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_budget_before_send_retains_original_error_and_prevents_credentials_and_requests(mode):
    case = make_case("success")

    def gate(payload):
        raise RuntimeError("budget_closed")

    case.model._before_send = gate
    with pytest.raises(RuntimeError, match="budget_closed") as caught:
        await run(case, mode)
    assert not isinstance(caught.value, ModelProviderError)
    assert case.credentials.calls == 0 and case.requests == [] and case.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_success_uses_one_catalog_get_and_one_responses_post_without_diagnostic_failure(mode):
    case = make_case("success")
    result = await run(case, mode)
    assert result.content == "已完成" and result.response_metadata["response_completed"] is True
    assert Counter(request.method for request in case.requests) == {"GET": 1, "POST": 1}
    assert not any(event["type"] == "model.failed" for event in case.events)
    public = json.dumps([case.events, case.observed_requests, case.observed_responses, case.model.model_dump()], default=str)
    assert FAKE_TOKEN not in public and "Authorization" not in public and FAKE_URL not in public
