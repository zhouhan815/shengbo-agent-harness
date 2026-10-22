"""One bounded connection establishment retry; no response/body replay.

All credentials and transport responses are fake and remain local.
"""
import json
from types import SimpleNamespace

import httpx
import pytest

from shengbo_prelive.providers.chatgpt import ModelProviderError, build_chatgpt_model
from test_provider_chatgpt import sse_response


SECRET = "fake-connect-retry-token-never-log"
PRIVATE_URL = "https://fake-private.example.test/?token=" + SECRET
ERROR_TEXT = "Authorization Bearer " + SECRET + " " + PRIVATE_URL


class Credentials:
    def __init__(self):
        self.calls = 0

    def get_access_token(self):
        self.calls += 1
        return SECRET


class BrokenStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, error_type, request, before_output=False):
        self.error_type, self.request, self.before_output = error_type, request, before_output

    def __iter__(self):
        if not self.before_output:
            yield b'data: {"type":"response.usage","usage":{"input_tokens":11}}\n\n'
            yield b'data: {"type":"response.output_text.delta","delta":"provisional"}\n\n'
        raise self.error_type(ERROR_TEXT, request=self.request)

    async def __aiter__(self):
        for chunk in self:
            yield chunk


def make_case(handler):
    case = SimpleNamespace(events=[], requests=[], observations=[], responses=[], credentials=Credentials())
    def transport(request):
        case.requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + SECRET
        return handler(request, len(case.requests))
    mock = httpx.MockTransport(transport)
    case.model = build_chatgpt_model(credentials=case.credentials, transport=mock, async_transport=mock,
        verify_catalog=False, observer=case.events.append, on_request=case.observations.append,
        on_response=case.responses.append)
    return case


async def invoke(case, mode):
    return case.model.invoke("本地连接测试。") if mode == "sync" else await case.model.ainvoke("本地连接测试。")


def assert_safe(case, error=None):
    public = json.dumps({"events": case.events, "requests": case.observations,
        "responses": case.responses, "model": case.model.model_dump(),
        "error": getattr(error, "__dict__", {})}, ensure_ascii=False, default=str)
    for forbidden in (SECRET, PRIVATE_URL, "Authorization", "Bearer "):
        assert forbidden not in public
    assert len(case.observations) == 1
    assert case.credentials.calls == 1
    assert len({event["attempt_id"] for event in case.events}) == 1
    assert len([event for event in case.events if event["type"] == "model.sent"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
@pytest.mark.parametrize("failures", [1, 2])
async def test_connect_failure_retries_once_within_same_attempt_then_succeeds_or_stops(mode, error_type, failures):
    def handler(request, number):
        if number <= failures:
            raise error_type(ERROR_TEXT, request=request)
        return sse_response()
    case = make_case(handler)
    error = None
    if failures == 2:
        with pytest.raises(ModelProviderError) as caught:
            await invoke(case, mode)
        error = caught.value
        assert error.phase == "responses_connect"
        assert error.exception_type == error_type.__name__
        # Preserve the existing final-failure outcome contract; the retry grant
        # is strictly local, never an application-level unknown-result replay.
        assert error.outcome_unknown is True
        assert len([event for event in case.events if event["type"] == "model.failed"]) == 1
    else:
        result = await invoke(case, mode)
        assert result.content == "已完成"
        assert len(case.responses) == 1
        assert not any(event["type"] == "model.failed" for event in case.events)
    assert len(case.requests) == 2
    retries = [event for event in case.events if event["type"] == "model.connection_retry"]
    assert len(retries) == 1
    assert retries[0]["retry_number"] == retries[0]["max_retries"] == 1
    assert retries[0]["delay_ms"] == 200
    assert retries[0]["exception_type"] == error_type.__name__
    assert_safe(case, error)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.WriteError])
@pytest.mark.parametrize("before_output", [False, True])
async def test_entered_response_never_retries_even_if_no_output_or_connection_error_type(mode, error_type, before_output):
    case = make_case(lambda request, _: httpx.Response(200, headers={"Content-Type": "text/event-stream"},
        stream=BrokenStream(error_type, request, before_output)))
    with pytest.raises(ModelProviderError) as caught:
        await invoke(case, mode)
    assert caught.value.phase == "responses_stream"
    assert caught.value.outcome_unknown is True
    assert len(case.requests) == 1
    assert not any(event["type"] == "model.connection_retry" for event in case.events)
    if not before_output:
        assert case.events[-1]["usage"] == {"input_tokens": 11}
    assert_safe(case, caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("error_type", [httpx.ReadError, httpx.WriteError, httpx.ReadTimeout, httpx.WriteTimeout])
async def test_non_connection_transport_error_before_headers_is_not_retried(mode, error_type):
    def handler(request, _):
        raise error_type(ERROR_TEXT, request=request)
    case = make_case(handler)
    with pytest.raises(ModelProviderError) as caught:
        await invoke(case, mode)
    assert caught.value.phase == "responses_connect"
    assert len(case.requests) == 1
    assert not any(event["type"] == "model.connection_retry" for event in case.events)
    assert_safe(case, caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("status", [400, 401, 429, 503])
async def test_http_status_is_never_a_connection_retry(mode, status):
    case = make_case(lambda _, __: httpx.Response(status, json={"error": {"message": ERROR_TEXT}}))
    with pytest.raises(ModelProviderError) as caught:
        await invoke(case, mode)
    assert caught.value.http_status == status
    assert len(case.requests) == 1
    assert not any(event["type"] == "model.connection_retry" for event in case.events)
    assert_safe(case, caught.value)
