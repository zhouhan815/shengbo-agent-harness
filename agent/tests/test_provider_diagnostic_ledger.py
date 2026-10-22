"""Provider diagnostics affect only the current attempt and retain ledger semantics."""
from collections import Counter
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from shengbo_contracts.common import WorkScope
from shengbo_prelive.models import RunScope
from shengbo_prelive.providers.chatgpt import ModelProviderError, build_chatgpt_model
from shengbo_prelive.runtime import CURRENT_ATTEMPT, CURRENT_SCOPE, RuntimeEngine
from test_provider_transport_diagnostics import FAKE_TOKEN, FAKE_URL, SENSITIVE_MESSAGE, TimeoutStream


class NoBusinessCalls:
    async def query(self, *args, **kwargs):
        raise AssertionError("provider diagnostics must not query business APIs")

    async def command(self, *args, **kwargs):
        raise AssertionError("provider diagnostics must not write business data")

    async def close(self):
        pass


class FakeCredentials:
    def get_access_token(self):
        return FAKE_TOKEN


def disk_attempt(case, identity):
    # A fresh connection observes committed SQLite state rather than a cached
    # object. Keep the serialized bytes as evidence that old records stay exact.
    with sqlite3.connect(case.engine.root / "agent.sqlite3") as connection:
        raw = connection.execute("SELECT body FROM attempts WHERE id=?", (identity,)).fetchone()[0]
    return raw.encode("utf-8"), json.loads(raw)


@pytest.fixture
async def ledger(tmp_path):
    engine = RuntimeEngine(tmp_path, NoBusinessCalls(), context_input_token_limit=48000)
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    scope = WorkScope(kind="catalog").model_dump()
    run = engine.store.put("run", {"id": "run-diagnostic-ledger", "conversation_id": conversation["id"],
        "scope": scope, "status": "RUNNING", "phase": "execute", "input": {"text": "读取诊断。", "material_refs": []},
        "goal": {"objective": "read_explain", "scope": scope, "allowed_effects": []},
        "allowed_effects": [], "allowed_tools": []})
    trusted_scope = RunScope(run["id"], "current-diagnostic-invocation", "execute")
    old = engine.store.reserve_attempt(run["id"], "old-unknown-invocation", "main")
    engine.store.settle_attempt(old["id"], state="outcome_unknown", provider_attempt_id="old-provider-attempt",
        observed_at="2026-10-16T01:02:03.123456+00:00", usage={"input_tokens": 77},
        failure_code="model_transport_failed", legacy_marker={"original": ["retain", 1, None]})
    case = SimpleNamespace(engine=engine, run=run, scope=trusted_scope, old_id=old["id"])
    case.old_bytes, case.old_value = disk_attempt(case, case.old_id)
    try:
        yield case
    finally:
        await engine.stop()


def assert_old_exact(case):
    raw, value = disk_attempt(case, case.old_id)
    assert raw == case.old_bytes and value == case.old_value
    assert value["state"] == "outcome_unknown" and "transport_diagnostic" not in value


def diagnostic_events(case):
    return [event for event in case.engine.events(case.run["id"])["events"]
            if event["type"] == "model.transport_diagnostic"]


def assert_no_sensitive_storage(case):
    with sqlite3.connect(case.engine.root / "agent.sqlite3") as connection:
        rows = connection.execute("SELECT body FROM attempts WHERE run_id=?", (case.run["id"],)).fetchall()
    public = json.dumps([rows, case.engine.events(case.run["id"])["events"]], ensure_ascii=False)
    for secret in (FAKE_TOKEN, FAKE_URL, "private failure", "Authorization", "Bearer "):
        assert secret not in public


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(("failure", "state", "phase", "exception_type", "started", "post_count"), [
    ("catalog_connect", "aborted_before_send", "catalog_get", "ConnectError", False, 0),
    ("post_read_timeout", "outcome_unknown", "responses_stream", "ReadTimeout", True, 1),
])
async def test_real_provider_failure_persists_current_diagnostic_without_reinterpreting_old_attempt(
        ledger, mode, failure, state, phase, exception_type, started, post_count):
    case, requests = ledger, []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            if failure == "catalog_connect":
                raise httpx.ConnectError(SENSITIVE_MESSAGE, request=request)
            return httpx.Response(200, json={"models": [{"slug": "gpt-6.1-sol", "visibility": "list"}]})
        assert request.method == "POST"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=TimeoutStream(request))

    transport = httpx.MockTransport(respond)
    model = build_chatgpt_model(credentials=FakeCredentials(), transport=transport, async_transport=transport,
        observer=case.engine._model_event, before_send=case.engine._before_send, verify_catalog=True)
    scope_token = CURRENT_SCOPE.set(case.scope)
    attempt_token = CURRENT_ATTEMPT.set(None)
    try:
        with pytest.raises(ModelProviderError) as caught:
            if mode == "sync":
                model.invoke("诊断测试。")
            else:
                await model.ainvoke("诊断测试。")
    finally:
        CURRENT_ATTEMPT.reset(attempt_token)
        CURRENT_SCOPE.reset(scope_token)
    # LangChain's async call runs in a copied context. The observer still sees
    # its reserved ticket, but ContextVar mutations need not flow to this caller.
    with sqlite3.connect(case.engine.root / "agent.sqlite3") as connection:
        current_ids = connection.execute("SELECT id FROM attempts WHERE run_id=? AND id<>?",
            (case.run["id"], case.old_id)).fetchall()
    assert len(current_ids) == 1
    current_id = current_ids[0][0]
    assert current_id and current_id != case.old_id
    _, current = disk_attempt(case, current_id)
    assert current["state"] == state
    assert current["invocation_id"] == case.scope.invocation_id
    assert current["failure_code"] == caught.value.code == "model_transport_failed"
    assert caught.value.outcome_unknown is True, "the provider's old unknown flag is unchanged even before POST"
    expected = {"phase": phase, "exception_type": exception_type, "responses_post_started": started}
    assert current["transport_diagnostic"] == expected
    assert all(getattr(caught.value, key) == value for key, value in expected.items())
    events = diagnostic_events(case)
    assert len(events) == 1 and events[0]["invocation_id"] == case.scope.invocation_id
    assert {key: events[0]["data"][key] for key in expected} == expected
    assert Counter(request.method for request in requests) == Counter({"GET": 1, "POST": post_count})
    assert_old_exact(case)
    assert_no_sensitive_storage(case)


@pytest.mark.asyncio
@pytest.mark.parametrize(("phase", "exception_type", "post_started", "expected"), [
    (FAKE_TOKEN, FAKE_TOKEN, True, {"responses_post_started": True}),
    ({"secret": FAKE_TOKEN}, [FAKE_TOKEN], False, {"responses_post_started": False}),
    ([FAKE_TOKEN], {"secret": FAKE_TOKEN}, True, {"responses_post_started": True}),
    (FAKE_TOKEN, FAKE_TOKEN, {"secret": FAKE_TOKEN}, {}),
    (None, None, [FAKE_TOKEN], {}),
    (None, None, 1, {}),
    ("catalog_get", {"secret": FAKE_TOKEN}, False, {"phase": "catalog_get", "responses_post_started": False}),
    ({"secret": FAKE_TOKEN}, "ConnectError", False, {"exception_type": "ConnectError", "responses_post_started": False}),
])
async def test_receiver_drops_nonwhitelisted_or_nonstrings_and_retains_only_real_booleans(
        ledger, phase, exception_type, post_started, expected):
    case = ledger
    current = case.engine.store.reserve_attempt(case.run["id"], case.scope.invocation_id, "main")
    scope_token = CURRENT_SCOPE.set(case.scope)
    attempt_token = CURRENT_ATTEMPT.set(current["id"])
    try:
        case.engine._model_event({"type": "model.failed", "attempt_id": "synthetic-provider-current",
            "code": "model_transport_failed", "outcome_unknown": True,
            "phase": phase, "exception_type": exception_type, "responses_post_started": post_started,
            "message": SENSITIVE_MESSAGE, "headers": {"Authorization": FAKE_TOKEN}, "url": FAKE_URL})
    finally:
        CURRENT_ATTEMPT.reset(attempt_token)
        CURRENT_SCOPE.reset(scope_token)
    _, saved = disk_attempt(case, current["id"])
    assert saved["state"] == "aborted_before_send", "reserved state still controls the existing state algorithm"
    if expected:
        assert saved["transport_diagnostic"] == expected
        events = diagnostic_events(case)
        assert len(events) == 1
        assert {key: value for key, value in events[0]["data"].items() if key not in {"title", "status"}} == expected
    else:
        assert "transport_diagnostic" not in saved and diagnostic_events(case) == []
    assert_old_exact(case)
    assert_no_sensitive_storage(case)


@pytest.mark.asyncio
async def test_safe_diagnostic_boolean_does_not_change_existing_sent_unknown_state_rule(ledger):
    case = ledger
    current = case.engine.store.reserve_attempt(case.run["id"], case.scope.invocation_id, "main")
    case.engine.store.settle_attempt(current["id"], state="sent")
    scope_token = CURRENT_SCOPE.set(case.scope)
    attempt_token = CURRENT_ATTEMPT.set(current["id"])
    try:
        case.engine._model_event({"type": "model.failed", "attempt_id": "current-provider",
            "code": "model_transport_failed", "outcome_unknown": True,
            "phase": "catalog_get", "exception_type": "ConnectError", "responses_post_started": False})
    finally:
        CURRENT_ATTEMPT.reset(attempt_token)
        CURRENT_SCOPE.reset(scope_token)
    _, saved = disk_attempt(case, current["id"])
    assert saved["state"] == "outcome_unknown"
    assert saved["transport_diagnostic"]["responses_post_started"] is False
    assert_old_exact(case)


@pytest.mark.asyncio
async def test_safe_http_status_and_public_codes_survive_runtime_receiver(ledger):
    case=ledger
    current=case.engine.store.reserve_attempt(case.run['id'],case.scope.invocation_id,'main')
    case.engine.store.settle_attempt(current['id'],state='sent')
    scope_token=CURRENT_SCOPE.set(case.scope);attempt_token=CURRENT_ATTEMPT.set(current['id'])
    try:
        case.engine._model_event({'type':'model.failed','attempt_id':'safe-http',
            'code':'model_http_error','outcome_unknown':False,'phase':'responses_stream',
            'exception_type':'ModelProviderError','responses_post_started':True,
            'http_status':429,'provider_error_code':'rate_limit_exceeded','provider_error_type':'rate_limit_error',
            'message':SENSITIVE_MESSAGE,'headers':{'Authorization':FAKE_TOKEN}})
    finally:
        CURRENT_ATTEMPT.reset(attempt_token);CURRENT_SCOPE.reset(scope_token)
    _,saved=disk_attempt(case,current['id'])
    assert saved['state']=='known_failed'
    assert saved['transport_diagnostic']['http_status']==429
    assert saved['transport_diagnostic']['provider_error_code']=='rate_limit_exceeded'
    assert_no_sensitive_storage(case)
