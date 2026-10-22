"""Connection retry visibility does not settle usage or create reservations."""
import json
import pytest

from shengbo_prelive.runtime import CURRENT_ATTEMPT, CURRENT_SCOPE
from test_provider_diagnostic_ledger import ledger, disk_attempt, assert_old_exact


@pytest.mark.asyncio
async def test_retry_is_visible_and_stays_on_current_sent_attempt(ledger):
    case = ledger
    ticket = case.engine.store.reserve_attempt(case.run["id"], case.scope.invocation_id, "main")
    case.engine.store.settle_attempt(ticket["id"], state="sent", sent_at="preserve-original-send")
    scope_token = CURRENT_SCOPE.set(case.scope)
    attempt_token = CURRENT_ATTEMPT.set(ticket["id"])
    try:
        event = {"type": "model.connection_retry", "phase": "responses_connect", "retry_number": 1,
                 "max_retries": 1, "delay_ms": 200, "exception_type": "ConnectError",
                 "token": "do-not-store-this-secret", "url": "https://private.invalid/?token=do-not-store-this-secret"}
        case.engine._model_event(event)
        case.engine._model_event(event)  # replayed diagnostic is not another attempt
        _, state = disk_attempt(case, ticket["id"])
        assert state["state"] == "sent" and state["sent_at"] == "preserve-original-send"
        assert state["connection_retry_count"] == 1 and state.get("usage") is None
        records = case.engine.events(case.run["id"])["events"]
        assert any(row["type"] == "model.connection_retry" for row in records)
        assert "do-not-store-this-secret" not in json.dumps([state, records])
        assert_old_exact(case)
    finally:
        CURRENT_ATTEMPT.reset(attempt_token)
        CURRENT_SCOPE.reset(scope_token)
