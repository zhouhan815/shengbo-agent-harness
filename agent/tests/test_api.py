"""Consumer and security checks for HTTP routes; no model or voice calls."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.api import MAX_UPLOAD_BYTES, create_app
from shengbo_prelive.runtime import RuntimeEngine

TOKEN = "test-service-credential-never-displayed"
BASE = "/api/assistant/v2"


class FakeEngine:
    def __init__(self):
        self.started = False
        self.starts = 0
        self.stops = 0
        self.calls = []

    async def start(self):
        self.started = True
        self.starts += 1

    async def stop(self):
        self.started = False
        self.stops += 1

    def health(self):
        return {"status": "ok", "service_token": TOKEN}

    def create_conversation(self, body):
        self.calls.append(("create", body))
        return {"conversation_id": "c1", "scope": body.get("scope")}

    def list_conversations(self, room_id=None, preparation_id=None):
        return {"items": [{"conversation_id": "c1"}]}

    def get_conversation(self, conversation_id):
        if conversation_id == "missing":
            raise ContractError("not_found", "会话不存在", status=404)
        return {"conversation_id": conversation_id, "messages": [], "runs": []}

    async def submit_message(self, conversation_id, body):
        self.calls.append(("message", conversation_id, body.model_dump(mode="json")))
        return {"run_id": "r1", "status": "QUEUED"}

    def accept_material(self, conversation_id, filename, content_type, data):
        self.calls.append(("material", conversation_id, filename, len(data)))
        return {"material_ref": {"kind": "material", "id": "mat1", "revision": 1, "content_hash": "sha256:fixture"}, "name": filename}

    def get_run(self, run_id):
        if run_id == "crash":
            raise RuntimeError("accidental secret " + TOKEN)
        return {"run_id": run_id, "status": "WAITING_USER", "questions": [{"decision_id": "d1", "revision": 2}]}

    async def cancel_run(self, run_id):
        self.calls.append(("cancel", run_id))
        return {"run_id": run_id, "status": "CANCELLING"}

    async def answer(self, decision_id, body):
        self.calls.append(("answer", decision_id, body.model_dump(mode="json")))
        return {"decision_id": decision_id, "status": "answered"}

    def decision_belongs_to_run(self, decision_id, run_id):
        return decision_id == "d1" and run_id == "r1"

    def events(self, run_id, after, limit=200):
        return {"events": [{"schema_version": "PRELIVE-2.3", "event_id": "e1", "sequence": after + 1,
                            "run_id": run_id, "type": "tool.started", "timestamp": "2026-10-16T08:00:00Z",
                            "data": {"arguments": {"api_key": TOKEN}}}], "next_cursor": after + 1}

    def all_events(self, after):
        event = self.events("r1", 0)["events"][0]
        return {"events": [{"event": event, "stream_sequence": after + 41}], "next_cursor": after + 41}

    def read_payload(self, run_id, ref):
        self.calls.append(("payload", run_id, ref))
        return {"payload": {"authorization": TOKEN, "reasoning_content": "private", "result": {"value": 3}}}

    def workspace(self, conversation_id=None, scope=None):
        return {"conversation_id": conversation_id, "scope": scope}


@pytest.fixture
def client():
    engine = FakeEngine()
    with TestClient(create_app(engine, TOKEN), headers={"X-Shengbo-Service-Token": TOKEN}) as client:
        client.fake_engine = engine
        yield client
    assert engine.starts == 1
    assert engine.stops == 1


def test_health_is_safe_and_routes_require_service_identity():
    engine = FakeEngine()
    with TestClient(create_app(engine, TOKEN)) as client:
        assert client.get("/health").status_code == 200
        assert TOKEN not in client.get("/health").text
        assert client.get(BASE + "/conversations").status_code == 401
        assert client.get(BASE + "/conversations", headers={"X-Shengbo-Service-Token": "wrong"}).status_code == 401
        # Browser-looking role and cookie claims do not confer identity.
        assert client.get(BASE + "/conversations", headers={"Cookie": "role=human", "X-Actor-Id": "admin"}).status_code == 401


def test_conversation_message_and_scope_contract(client):
    response = client.post(BASE + "/conversations", json={"scope": {"kind": "catalog", "account_id": "local"}})
    assert response.status_code == 201
    assert client.get(BASE + "/conversations").json()["items"][0]["conversation_id"] == "c1"
    assert client.get(BASE + "/conversations/c1").status_code == 200
    result = client.post(BASE + "/conversations/c1/messages", json={"text": "只更新商品", "client_request_id": "ui1", "intent_mode": "new_task", "scope": {"kind": "catalog", "account_id": "local"}})
    assert result.status_code == 202
    assert result.json()["status"] == "QUEUED"
    assert client.post(BASE + "/conversations/c1/messages", json={"text": "x", "client_request_id": "x", "scope": {"kind": "catalog", "account_id": "another"}}).status_code == 403
    assert client.post(BASE + "/conversations/c1/messages", json={"text": "", "client_request_id": "x"}).status_code == 422
    assert client.post(BASE + "/conversations/c1/messages", json={"text": "x"}).status_code == 422


async def test_delete_conversation_preserves_run_and_blocks_active_work(tmp_path):
    # ASGITransport does not run the lifespan, so the queued task never dispatches.
    engine = RuntimeEngine(tmp_path, business_client=None)
    try:
        transport = ASGITransport(app=create_app(engine, TOKEN))
        async with AsyncClient(transport=transport, base_url="http://testserver",
                               headers={"X-Shengbo-Service-Token": TOKEN}) as client:
            first = await client.post(BASE + "/conversations", json={"title": "原对话"})
            other = await client.post(BASE + "/conversations", json={"title": "保留的对话"})
            identity = first.json()["conversation_id"]
            path = BASE + "/conversations/" + identity
            assert (await client.delete(path, headers={"X-Shengbo-Service-Token": "wrong"})).status_code == 401
            request = {"text": "核对商品资料", "client_request_id": "message-1"}
            queued = await client.post(path + "/messages", json=request)
            assert queued.status_code == 202
            run_id = queued.json()["run"]["id"]
            for status in ("QUEUED", "WAITING_USER", "RUNNING"):
                engine.store.update("run", run_id, status=status)
                refused = await client.delete(path)
                assert refused.status_code == 409
                assert refused.json()["error"]["code"] == "CONVERSATION_HAS_ACTIVE_RUN"
                assert (await client.get(path)).status_code == 200
            engine.store.update("run", run_id, status="COMPLETED")
            deleted = await client.delete(path)
            assert deleted.json() == {"conversation_id": identity, "deleted": True}
            assert deleted.status_code == 200
            tombstone = engine.store.get("conversation", identity)
            assert tombstone["deleted_at"]
            assert (await client.delete(path)).status_code == 200
            assert engine.store.get("conversation", identity) == tombstone
            remaining = (await client.get(BASE + "/conversations")).json()["items"]
            assert [item["conversation_id"] for item in remaining] == [other.json()["conversation_id"]]
            assert (await client.get(path)).status_code == 404
            assert (await client.post(path + "/messages", json=request)).status_code == 404
            assert (await client.post(BASE + "/materials", data={"conversation_id": identity},
                                      files={"file": ("facts.txt", b"test", "text/plain")})).status_code == 404
            assert (await client.get(BASE + "/runs/" + run_id)).json()["status"] == "COMPLETED"
            assert engine.store.list("message")[0]["content"] == request["text"]
    finally:
        engine.instance.release()


def test_invalid_dto_never_echoes_input_or_principal(client):
    response = client.post(BASE + "/conversations/c1/messages", json={"text": "x", "client_request_id": "ui1", "actor_id": TOKEN})
    assert response.status_code == 422
    assert TOKEN not in response.text
    assert "input" not in response.text
    assert client.post(BASE + "/conversations", json={"actor_id": "human"}).status_code == 422


def test_upload_limit_filename_scope_and_empty(client):
    good = client.post(BASE + "/materials", data={"conversation_id": "c1", "scope": json.dumps({"kind": "catalog", "account_id": "local"})}, files={"file": ("商品.txt", "商品内容".encode(), "text/plain")})
    assert good.status_code == 201
    assert good.json()["name"] == "商品.txt"
    bad_name = client.post(BASE + "/materials", data={"conversation_id": "c1"}, files={"file": ("../outside.txt", b"x", "text/plain")})
    assert bad_name.status_code == 422
    oversize = client.post(BASE + "/materials", data={"conversation_id": "c1"}, files={"file": ("large.txt", b"x" * (MAX_UPLOAD_BYTES + 1), "text/plain")})
    assert oversize.status_code == 413
    empty = client.post(BASE + "/materials", data={"conversation_id": "c1"}, files={"file": ("empty.txt", b"", "text/plain")})
    assert empty.status_code == 422
    wrong_account = client.post(BASE + "/materials", data={"conversation_id": "c1", "scope": '{"kind":"catalog","account_id":"another"}'}, files={"file": ("a.txt", b"x", "text/plain")})
    assert wrong_account.status_code == 403
    assert [call[0] for call in client.fake_engine.calls].count("material") == 1


def test_decisions_exact_membership_and_cancel(client):
    answer = {"decision_id": "d1", "question_revision": 2, "decision_revision": 2, "answer": "new", "operation_id": "op1", "human_receipt": {"operation_id": "human-op"}}
    assert client.post(BASE + "/decisions/d1/answers", json=answer).status_code == 200
    assert client.post(BASE + "/runs/r1/answers", json=answer).status_code == 200
    assert client.post(BASE + "/runs/r2/answers", json={**answer, "decision_id": "foreign"}).status_code == 403
    assert client.post(BASE + "/decisions/d2/answers", json=answer).status_code == 409
    assert client.post(BASE + "/runs/r1/cancel", json={"operation_id": "cancel-op", "expected_run_version": 2}).json()["status"] == "CANCELLING"


def test_events_payload_workspace_and_secret_filter(client):
    events = client.get(BASE + "/runs/r1/events?after=4&limit=100")
    assert events.json()["events"][0]["sequence"] == 5
    assert TOKEN not in events.text
    assert client.get(BASE + "/runs/r1/events?after=-1").status_code == 422
    assert client.get(BASE + "/runs/r1/events?limit=10000").status_code == 422
    payload = client.get(BASE + "/runs/r1/payloads/opaque-01")
    assert payload.json()["payload"]["result"]["value"] == 3
    assert TOKEN not in payload.text
    assert '"private"' not in payload.text
    scope = json.dumps({"kind": "catalog", "account_id": "local"})
    assert client.get(BASE + "/workspace", params={"conversation_id": "c1", "scope": scope}).status_code == 200
    assert client.get(BASE + "/workspace", params={"scope": '{"kind":"catalog","account_id":"another"}'}).status_code == 403
    assert client.get(BASE + "/events", params={"run_id": "r1"}, headers={"Last-Event-ID": "../../bad"}).status_code == 422


def test_known_and_unexpected_errors_do_not_disclose_secrets(client):
    assert client.get(BASE + "/conversations/missing").status_code == 404
    response = client.get(BASE + "/runs/crash")
    assert response.status_code == 500
    assert TOKEN not in response.text
    assert "RuntimeError" not in response.text


def test_prestarted_engine_not_started_or_stopped_twice():
    engine = FakeEngine()
    engine.started = True
    with TestClient(create_app(engine, TOKEN)):
        pass
    assert engine.starts == 0
    assert engine.stops == 0


@pytest.mark.asyncio
async def test_stream_disconnect_does_not_cancel_business_run():
    engine = FakeEngine()
    app = create_app(engine, TOKEN)
    endpoint = next(route.endpoint for route in app.routes if route.path == BASE + "/events")

    class DisconnectRequest:
        checks = 0

        async def is_disconnected(self):
            self.checks += 1
            return self.checks > 1

    response = await endpoint(DisconnectRequest(), run_id="r1", after=0, agent_after=None, last_event_id=None)
    chunks = [chunk async for chunk in response.body_iterator]
    assert len(chunks) == 1
    assert "id: 1" in chunks[0]
    assert "event: tool.started" in chunks[0]
    assert TOKEN not in chunks[0]
    assert not any(call[0] == "cancel" for call in engine.calls)


@pytest.mark.asyncio
async def test_global_stream_uses_independent_cursor_without_rewriting_run_sequence():
    engine = FakeEngine()
    app = create_app(engine, TOKEN)
    endpoint = next(route.endpoint for route in app.routes if route.path == BASE + "/events")

    class DisconnectRequest:
        checks = 0

        async def is_disconnected(self):
            self.checks += 1
            return self.checks > 1

    response = await endpoint(DisconnectRequest(), run_id=None, after=20, agent_after=None, last_event_id=None)
    chunks = [chunk async for chunk in response.body_iterator]
    assert len(chunks) == 1
    assert "id: 61" in chunks[0]
    data_line = next(line for line in chunks[0].splitlines() if line.startswith("data: "))
    event = json.loads(data_line[6:])
    assert event["sequence"] == 1
    assert event["run_id"] == "r1"
