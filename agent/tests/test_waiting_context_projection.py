"""Large durable job evidence stays recoverable without entering resume messages."""
from copy import deepcopy
import json
from unittest.mock import patch

import httpx
import pytest
import tiktoken
from langgraph.checkpoint.memory import InMemorySaver

from shengbo_contracts.common import ContractError, WorkScope, canonical_hash, canonical_json
from shengbo_prelive.middleware import Audit, ContextProjection, ToolPolicy
from shengbo_prelive.models import RunScope
from shengbo_prelive.policy import EFFECTS
from shengbo_prelive.providers.chatgpt import build_chatgpt_model
from shengbo_prelive.runtime import RuntimeEngine
from shengbo_prelive.tools import COMMAND_MAP


ENCODING = tiktoken.get_encoding("o200k_base")


def tokens(value):
    return len(ENCODING.encode(canonical_json(value)))


def ref(kind, identity, body):
    return {"kind": kind, "id": identity, "revision": 1, "content_hash": canonical_hash(body)}


def contains(value, expected):
    """Find an exact original subtree, independent of snapshot envelope names."""
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(contains(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(contains(item, expected) for item in value)
    return False


def snapshot_refs(value):
    found = {}

    def visit(item):
        if isinstance(item, dict):
            if item.get("kind") == "runtime_context" and {"id", "revision", "content_hash"} <= item.keys():
                found[canonical_hash(item)] = item
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return list(found.values())


def recovered_documents(engine, scope, view):
    refs = snapshot_refs(view)
    assert refs, "a compact view must expose exact references to its durable originals"
    return [engine.run_history(scope, {"full": True, "ref": item})["document"] for item in refs]


class ObservationClient:
    def __init__(self, observation):
        self.observation = observation
        self.queries = []

    async def query(self, key, args=None, **kwargs):
        self.queries.append((key, args))
        if key == "read_completion_observation":
            return deepcopy(self.observation)
        if key == "read_job":
            assert args["job_id"] == self.observation["jobs"][0]["ref"]["id"]
            return deepcopy(self.observation["jobs"][0])
        raise AssertionError("unexpected offline business query: " + key)

    async def close(self):
        pass


class OfflineCredentials:
    calls = 0

    def get_access_token(self):
        self.calls += 1
        return "fake-waiting-projection-test-credential"


@pytest.fixture
async def waiting_context(tmp_path):
    context = {"body": "evidence " * 50000, "tail": "EXACT_CONTEXT_TAIL"}
    assert tokens(context) >= 50000
    output_ref = ref("script_revision", "script-output", "exact-output")
    attempt_input = {"body": "public synthetic attempt input " * 6000, "tail": "EXACT_ATTEMPT_INPUT"}
    attempt_output = {"body": "public synthetic attempt output " * 6000, "tail": "EXACT_ATTEMPT_OUTPUT"}
    attempt = {"attempt_id": "isolated-provider-attempt-2", "state": "timeout_unknown",
        "sent_at": "2026-10-16T15:00:01.123456+00:00", "received_at": "2026-10-16T15:00:05.654321+00:00",
        "usage": {"input_tokens": 12345, "output_tokens": 678, "total_tokens": 13023},
        "request_hash": canonical_hash({"purpose": "isolated-test", "input": attempt_input}),
        "input_hash": canonical_hash(attempt_input), "output_hash": canonical_hash(attempt_output),
        "input": attempt_input, "output": attempt_output}
    job_body = {"id": "job-waiting", "kind": "scripts", "preparation_id": "prep-waiting",
        "room_id": "room-waiting", "run_id": "run-waiting", "status": "running",
        "generation_epoch": 7, "cancel_requested": False, "context": context,
        "context_ref": ref("context", "context-job", context),
        "items": [{"item_id": "item-waiting", "status": "running", "attempt": 2,
            "call_state": "timeout_unknown", "input": {"body": "full input " * 3000},
            "output": {"script_ref": output_ref}, "provider_attempts": [attempt]}]}
    job_ref = ref("prelive_job", job_body["id"], job_body)
    prep_body = {"id": "prep-waiting", "room_id": "room-waiting", "status": "draft",
        "generation_epoch": 7, "selected_product_refs": [], "analysis_refs": [],
        "bundle_ref": job_body["context_ref"], "approval_ref": None,
        "candidate_publication_ref": None}
    prep_ref = ref("preparation", "prep-waiting", prep_body)
    observation = {"jobs": [{"ref": job_ref, "body": job_body}],
        "pending_job_ids": [job_body["id"]], "unknown_job_ids": [job_body["id"]],
        "candidate_ready": False, "preparation": {"ref": prep_ref, "body": prep_body}}
    client = ObservationClient(observation)
    engine = RuntimeEngine(tmp_path, client, context_input_token_limit=48000)
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    work_scope = WorkScope(kind="preparation", preparation_id="prep-waiting").model_dump()
    effects = sorted(EFFECTS["prepare_session"])
    run = engine.store.put("run", {"id": "run-waiting", "conversation_id": conversation["id"],
        "scope": work_scope, "status": "RUNNING", "phase": "execute", "room_id": "room-waiting",
        "input": {"text": "继续当前任务。", "material_refs": []},
        "goal": {"objective": "prepare_session", "scope": work_scope, "allowed_effects": effects},
        "allowed_effects": effects,
        "allowed_tools": [name for name, command in COMMAND_MAP.items() if command in effects]})
    for role in ("product_facts", "session_config", "audience_scenes"):
        report = {"summary": role + "公开报告", "fields": {"body": "report " * 7000,
            "tail": role.upper() + "_REPORT_TAIL"}, "source_refs": [], "dependencies": []}
        engine.store.put("branch", {"id": "branch-" + role, "run_id": run["id"], "role": role,
            "status": "PREPARED", "report": report, "report_hash": canonical_hash(report),
            "task": {"source_refs": [], "input_refs": [], "product_ids": []}})
    scope = RunScope(run["id"], "waiting-projection", "execute")
    branches = engine.read_batch(scope, [])
    assert tokens(branches) >= 20000
    raw_event = {"event": "后台任务状态已变化", "branches": branches, "jobs": observation}
    sent, credentials = [], OfflineCredentials()

    def respond(request):
        payload = json.loads(request.content)
        assert engine._estimate_payload(payload) <= 48000, "resume must pass the complete real wire fence"
        sent.append(payload)
        assert payload.get("tools"), "compact recovery must not need a paid summary call"
        event = {"type": "response.completed", "response": {"status": "completed",
            "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "测试完成"}]}],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
            text="data: " + json.dumps(event) + "\n\n")

    engine.model = build_chatgpt_model(credentials=credentials, transport=httpx.MockTransport(respond),
        verify_catalog=False, before_send=engine._before_send, observer=engine._model_event,
        on_request=engine._model_request, on_response=engine._model_response)
    engine.saver = InMemorySaver()
    with patch("shengbo_prelive.runtime.main_middleware",
               lambda e: [ContextProjection(e), ToolPolicy(e), Audit(e)]):
        engine.main = engine._build_graph("main")
    try:
        yield engine, client, run, scope, raw_event, sent, credentials
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_waiting_finish_returns_bounded_acceptance_and_keeps_exact_full_evidence(waiting_context):
    engine, client, run, scope, _, sent, credentials = waiting_context
    result = await engine.finish(scope, "WAITING_JOBS", "等待已受理任务。")
    assert result["accepted"] is True and result["outcome"] == "WAITING_JOBS"
    assert tokens(result) < 5000
    saved = engine.store.get("run", run["id"])["acceptance"]
    assert saved["ok"] is True and saved["observation"] == client.observation
    assert tokens(saved) > 50000, "durable verification must retain the original evidence"
    view = result["acceptance"]
    assert view["ok"] is True and view["missing"] == []
    assert contains(view, client.observation["pending_job_ids"])
    assert contains(view, client.observation["jobs"][0]["ref"])
    assert contains(view, client.observation["preparation"]["ref"])
    assert contains(view, client.observation["jobs"][0]["body"]["items"][0]["output"]["script_ref"])
    raw_attempt = client.observation["jobs"][0]["body"]["items"][0]["provider_attempts"][0]
    attempt_index = view["observation"]["jobs"][0]["body"]["items"][0]["provider_attempts_projection"]
    assert attempt_index["state_counts"] == {"timeout_unknown": 1}
    assert attempt_index["unknown_count"] == 1
    assert attempt_index["attention_attempt_ids"] == [raw_attempt["attempt_id"]]
    documents = recovered_documents(engine, scope, result)
    assert any(contains(document, saved) for document in documents)
    assert any(contains(document, raw_attempt) for document in documents)
    assert not sent and credentials.calls == 0


@pytest.mark.asyncio
async def test_read_job_projects_attempt_payloads_but_preserves_exact_query_evidence(waiting_context):
    engine, client, run, scope, _, sent, credentials = waiting_context
    original = client.observation["jobs"][0]
    job_id = original["ref"]["id"]
    view = await engine.business_query(scope, "read_job", {"job_id": job_id})
    assert tokens(view) < 5000
    assert view["ref"] == original["ref"]
    assert view["body"]["status"] == original["body"]["status"]
    item_view = view["body"]["items"][0]
    original_item = original["body"]["items"][0]
    assert item_view["call_state"] == "timeout_unknown"
    assert item_view["attempt"] == original_item["attempt"]
    assert item_view["output"] == original_item["output"]
    assert "context" not in view["body"] and "input" not in item_view
    attempt_view = item_view["provider_attempts"][0]
    original_attempt = original_item["provider_attempts"][0]
    assert set(attempt_view) == {"attempt_id", "state", "sent_at", "received_at", "usage",
                                 "request_hash", "input_hash", "output_hash"}
    assert attempt_view == {key: value for key, value in original_attempt.items() if key not in {"input", "output"}}
    assert attempt_view["state"] == "timeout_unknown"
    assert attempt_view["received_at"] == "2026-10-16T15:00:05.654321+00:00"
    assert attempt_view["usage"] == {"input_tokens": 12345, "output_tokens": 678, "total_tokens": 13023}

    evidence = engine.store.list("query_evidence")
    assert len(evidence) == 1 and evidence[0]["key"] == "read_job"
    assert evidence[0]["result"] == original, "query evidence must retain full context and provider input/output"
    assert engine.store.get("run", run["id"])["observed_refs"] == [original["ref"]]
    documents = recovered_documents(engine, scope, view)
    assert any(contains(document, original) for document in documents)
    assert any(contains(document, original_attempt["input"]) and contains(document, original_attempt["output"])
               for document in documents)
    assert client.queries == [("read_job", {"job_id": job_id})]
    assert not sent and credentials.calls == 0


@pytest.mark.asyncio
async def test_unchanged_waiting_signature_does_not_wake_again_even_with_prepared_reports(waiting_context):
    engine, _, run, _, _, sent, credentials = waiting_context
    assert all(branch["status"] == "PREPARED" for branch in engine.store.list("branch"))
    waiting = engine.store.update("run", run["id"], status="WAITING_JOBS", last_progress_hash=None)
    await engine._wake_waiting(waiting)
    first = engine.store.get("run", run["id"])
    assert first["status"] == "RUNNING" and first["last_progress_hash"]
    first_event = deepcopy(first["append_event"])
    first_refs = snapshot_refs(first_event)
    assert first_refs

    # The main may hand control back while the same job is still pending.
    # Already PREPARED branches must not make unchanged progress actionable.
    waiting_again = engine.store.update("run", run["id"], status="WAITING_JOBS")
    snapshot_count = len(engine.store.list("context_snapshot"))
    for _ in range(2):
        await engine._wake_waiting(engine.store.get("run", run["id"]))
        unchanged = engine.store.get("run", run["id"])
        assert unchanged == waiting_again, "an unchanged poll must not update state or wake the main"
        assert unchanged["append_event"] == first_event
        assert snapshot_refs(unchanged["append_event"]) == first_refs
        assert len(engine.store.list("context_snapshot")) == snapshot_count
    assert not sent and credentials.calls == 0


@pytest.mark.asyncio
async def test_scheduler_wake_indexes_large_reports_jobs_and_preserves_unknown_state(waiting_context):
    engine, client, run, scope, raw_event, sent, credentials = waiting_context
    waiting = engine.store.update("run", run["id"], status="WAITING_JOBS", last_progress_hash=None)
    await engine._wake_waiting(waiting)
    resumed = engine.store.get("run", run["id"])
    assert resumed["status"] == "RUNNING"
    view = resumed["append_event"]
    assert tokens(view) < 5000
    assert contains(view, client.observation["pending_job_ids"])
    assert contains(view, client.observation["unknown_job_ids"])
    assert "EXACT_CONTEXT_TAIL" not in canonical_json(view)
    assert not any(role.upper() + "_REPORT_TAIL" in canonical_json(view)
                   for role in ("product_facts", "session_config", "audience_scenes"))
    documents = recovered_documents(engine, scope, view)
    assert any(contains(document, client.observation) for document in documents)
    for branch in raw_event["branches"]["items"]:
        assert any(contains(document, branch["report"]) for document in documents)
    assert not sent and credentials.calls == 0


@pytest.mark.asyncio
async def test_real_sdk_projects_legacy_resume_human_and_inherited_acceptance_before_wire(waiting_context):
    engine, client, run, scope, raw_event, sent, credentials = waiting_context
    original_acceptance = {"ok": True, "missing": [], "observation": deepcopy(client.observation)}
    run = engine.store.update("run", run["id"], inherited_context={"related_run_id": "prior-run",
        "evidence_only": True, "authorization_inherited": False, "prior_acceptance": original_acceptance})
    # Seed an actual completed SDK checkpoint, then resume with a legacy event
    # that predates projection. _invoke must project it before HumanMessage.
    await engine._invoke(run, invocation_id="seed-waiting-checkpoint")
    run = engine.store.update("run", run["id"], append_event=deepcopy(raw_event))
    await engine._invoke(run, invocation_id="resume-legacy-event")
    assert len(sent) == credentials.calls == 2
    latest_user = [item for item in sent[-1]["input"] if item.get("role") == "user"][-1]
    assert isinstance(latest_user["content"], str)
    human_view = json.loads(latest_user["content"])
    assert tokens(human_view) < 5000
    assert "EXACT_CONTEXT_TAIL" not in latest_user["content"]
    documents = recovered_documents(engine, scope, human_view)
    assert any(contains(document, client.observation) for document in documents)
    projection = await engine.project_context(scope)
    inherited = projection["inherited_context"]["prior_acceptance"]
    assert tokens(inherited) < 5000 and inherited["ok"] is True and inherited["missing"] == []
    inherited_documents = recovered_documents(engine, scope, inherited)
    assert any(contains(document, original_acceptance) for document in inherited_documents)
    assert engine.store.get("run", run["id"])["inherited_context"]["prior_acceptance"] == original_acceptance
    assert not any(event["type"] in {"context.summarized", "model.window_rejected"}
                   for event in engine.events(run["id"])["events"])


@pytest.mark.asyncio
async def test_waiting_evidence_snapshots_reject_foreign_conversations_and_child_scopes(waiting_context):
    engine, _, run, scope, raw_event, sent, credentials = waiting_context
    finish = await engine.finish(scope, "WAITING_JOBS", "等待已受理任务。")
    resume = engine._resume_event_view(run, raw_event)
    refs = snapshot_refs(finish) + snapshot_refs(resume)
    assert refs
    foreign_conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    foreign_run = engine.store.put("run", {**run, "id": "foreign-waiting-run",
        "conversation_id": foreign_conversation["id"]})
    foreign_scope = RunScope(foreign_run["id"], "foreign-waiting-reader", "execute")
    child_scope = RunScope(run["id"], "child-waiting-reader", "execute", "product_facts", "branch-product_facts")
    for evidence_ref in refs:
        assert engine.run_history(scope, {"full": True, "ref": evidence_ref})["document"]
        with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
            engine.run_history(foreign_scope, {"full": True, "ref": evidence_ref})
        with pytest.raises(ContractError, match="BRANCH_SCOPE_DENIED"):
            engine.run_history(child_scope, {"full": True, "ref": evidence_ref})
    assert not sent and credentials.calls == 0
