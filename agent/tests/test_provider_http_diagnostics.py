"""HTTP failure labels are bounded, safe, and never grant provider retries."""
from collections import Counter
import json

import httpx
import pytest

from shengbo_prelive.providers.chatgpt import (
    MAX_ERROR_METADATA_BYTES, ModelProviderError, build_chatgpt_model, safe_transport_diagnostic, _public_error_metadata,
)


SECRET = "fake_private_api_token_should_never_appear_in_diagnostics"


class FakeCredentials:
    def get_access_token(self):
        return SECRET


async def invoke(model, mode):
    return model.invoke("HTTP diagnostic fixture") if mode == "sync" else await model.ainvoke("HTTP diagnostic fixture")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("status,body,unknown,code,error_type", [
    (400, {"error": {"code": "context_length_exceeded", "type": "invalid_request_error", "message": SECRET}},
     False, "context_length_exceeded", "invalid_request_error"),
    (400, {"error": {"code": SECRET, "message": SECRET}}, True, None, None),
    (401, {"error": SECRET}, False, None, None),
    (403, {"error": {"code": "permission_denied", "type": "permission_error", "message": SECRET}},
     False, "permission_denied", "permission_error"),
    (408, {"error": {"code": "invalid_request_error"}}, True, "invalid_request_error", None),
    (409, {"error": {"type": "invalid_request_error"}}, True, None, "invalid_request_error"),
    (429, {"error": {"code": "rate_limit_exceeded", "type": "rate_limit_error", "message": SECRET}},
     False, "rate_limit_exceeded", "rate_limit_error"),
    (500, {"error": {"code": "invalid_request_error", "type": "invalid_request_error"}},
     True, "invalid_request_error", "invalid_request_error"),
    (502, "<html>" + SECRET + "</html>", True, None, None),
    (503, {"error": {"code": "server_error", "type": "server_error", "message": SECRET}},
     True, "server_error", "server_error"),
    (302, "<html>" + SECRET + "</html>", True, None, None),
    (499, {"error": SECRET}, True, None, None),
])
async def test_http_failures_preserve_status_public_labels_and_conservative_outcome(
        mode, status, body, unknown, code, error_type):
    requests, events, results = [], [], []
    def respond(request):
        requests.append(request)
        if isinstance(body, dict):
            return httpx.Response(status, json=body, headers={"X-Private-Fake": SECRET})
        return httpx.Response(status, text=body, headers={"X-Private-Fake": SECRET})
    transport = httpx.MockTransport(respond)
    model = build_chatgpt_model(credentials=FakeCredentials(), verify_catalog=False,
        transport=transport, async_transport=transport, observer=events.append, on_response=results.append)
    with pytest.raises(ModelProviderError) as caught:
        await invoke(model, mode)
    error = caught.value
    assert error.code == "model_http_error" and str(error) == "model_http_error"
    assert error.http_status == status
    assert error.outcome_unknown is unknown
    assert error.exception_type == "ModelProviderError" and error.phase == "responses_stream"
    assert getattr(error, "provider_error_code", None) == code
    assert getattr(error, "provider_error_type", None) == error_type
    failed = [event for event in events if event["type"] == "model.failed"]
    assert len(failed) == 1 and failed[0]["http_status"] == status
    assert failed[0].get("provider_error_code") == code
    assert failed[0].get("provider_error_type") == error_type
    assert failed[0]["outcome_unknown"] is unknown
    assert Counter(request.method for request in requests) == {"POST": 1}
    assert results == []
    public = json.dumps({"error": str(error), "fields": error.__dict__, "events": events,
                         "results": results, "model": model.model_dump()}, ensure_ascii=False, default=str)
    assert SECRET not in public and "X-Private-Fake" not in public
    assert "Authorization" not in public and "headers" not in error.__dict__ and "body" not in error.__dict__


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_oversized_error_body_is_not_saved_or_parsed_as_metadata(mode):
    events, count = [], 0
    payload = {"error": {"code": "context_length_exceeded", "type": "invalid_request_error",
                         "message": SECRET * (MAX_ERROR_METADATA_BYTES // len(SECRET) + 3)}}
    def respond(request):
        nonlocal count
        count += 1
        return httpx.Response(400, json=payload)
    transport = httpx.MockTransport(respond)
    model = build_chatgpt_model(credentials=FakeCredentials(), verify_catalog=False,
        transport=transport, async_transport=transport, observer=events.append)
    with pytest.raises(ModelProviderError) as caught:
        await invoke(model, mode)
    assert count == 1 and caught.value.outcome_unknown is True
    assert caught.value.http_status == 400
    assert "provider_error_code" not in caught.value.__dict__
    assert SECRET not in json.dumps([events, caught.value.__dict__])


def test_transport_diagnostic_receiver_requires_fixed_labels_and_real_status_integer():
    assert safe_transport_diagnostic(http_status=True, provider_error_code=SECRET, provider_error_type=SECRET) == {}
    assert safe_transport_diagnostic(http_status=999, provider_error_code="unknown_short_label") == {}
    assert safe_transport_diagnostic(http_status="429", provider_error_type=["rate_limit_error"]) == {}
    assert safe_transport_diagnostic(http_status=429, provider_error_code="rate_limit_exceeded",
                                     provider_error_type="rate_limit_error") == {
        "http_status": 429, "provider_error_code": "rate_limit_exceeded", "provider_error_type": "rate_limit_error"}


def test_invalid_or_overdeep_error_json_cannot_replace_safe_http_failure():
    assert _public_error_metadata(b"[" * 2000 + b"]" * 2000) == {}
    assert _public_error_metadata(b"not-json-private-data") == {}
