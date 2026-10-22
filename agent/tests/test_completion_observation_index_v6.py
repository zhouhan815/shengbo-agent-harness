"""Completion handoffs stay exact and small across real SDK wait/wake turns."""
from copy import copy, deepcopy
import json
from pathlib import Path
from types import MethodType

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from shengbo_contracts.common import ContractError, canonical_hash, canonical_json
from shengbo_prelive.models import RunScope
from shengbo_prelive.providers.chatgpt import build_chatgpt_model
import shengbo_prelive.runtime as runtime_module
from shengbo_prelive.runtime import RuntimeEngine
from test_waiting_context_projection import waiting_context, contains, tokens, OfflineCredentials


ROOT = Path(__file__).resolve().parents[1]
BEFORE = ROOT / "output/20261020-third-update-implementation/completion-index-v6-before.json"


def before_engine(engine):
    old = copy(engine)
    archived = json.loads(BEFORE.read_text(encoding="utf-8"))
    for name, source in archived["methods"].items():
        if name == "_drive_main": continue
        namespace = dict(vars(runtime_module))
        exec(compile(source, str(BEFORE) + ":" + name, "exec"), namespace)
        setattr(old, name, MethodType(namespace[name], old))
    return old


@pytest.mark.asyncio
async def test_completion_index_preserves_late_unknown_and_exact_body_readers(waiting_context):
    engine, client, run, scope, _, _, _ = waiting_context
    job = client.observation["jobs"][0]
    template = deepcopy(job["body"]["items"][0])
    job["body"]["items"] = [{**deepcopy(template), "item_id": "item-" + str(i),
        "call_state": "not_sent", "status": "succeeded", "provider_attempts": []} for i in range(20)]
    job["body"]["items"][-1] = {**template, "item_id": "late-unknown", "future_action_fence": {"retry_permitted": False}}
    job["body"]["future_job_fence"] = {"do_not_retry": True}
    client.observation["preparation"]["body"].update(goal="原活动和不可遗漏条件" * 2000,
        future_approval_fence={"requires_current_check": True})
    raw = deepcopy(client.observation)
    result = await engine.finish(scope, "WAITING_JOBS", "等待已持久任务，未知结果先对账。")
    view = result["acceptance"]["observation"]
    assert result["accepted"] is True and tokens(result) < 5000
    row = view["jobs"][0]
    assert row["items_projection"]["total"] == 20 and row["items_projection"]["unknown_item_count"] == 1
    assert row["body"]["future_job_fence"] == {"do_not_retry": True}
    assert row["attention_items"][0]["item_id"] == "late-unknown"
    assert row["attention_items"][0]["future_action_fence"] == {"retry_permitted": False}
    assert view["preparation"]["body"]["future_approval_fence"] == {"requires_current_check": True}
    for path, original in [("/observation", raw),
            ("/observation/preparation/body/goal", raw["preparation"]["body"]["goal"]),
            ("/observation/jobs/0/body/items/19", raw["jobs"][0]["body"]["items"][19])]:
        document = engine.run_history(scope, {"ref": view["full_ref"], "full": True})["document"]
        for key in path.strip("/").split("/"):
            document = document[int(key)] if isinstance(document, list) else document[key]
        assert document == original
        selected = engine.run_history(scope, {"ref": view["full_ref"], "path": path})
        assert selected["selection"]["content_hash"] == canonical_hash(original)
    assert engine.store.get("run", run["id"])["acceptance"]["observation"] == raw


@pytest.mark.asyncio
async def test_v2_resume_is_reprojected_only_after_real_reader_acl_and_hash(waiting_context):
    engine, _, run, scope, event, _, _ = waiting_context
    old = before_engine(engine)
    legacy = old._resume_event_view(run, event)
    original = deepcopy(legacy)
    view = engine._resume_event_view(run, legacy)
    assert view["projection_version"] == 3 and legacy == original
    assert engine._resume_event_view(run, view) == view
    assert engine.run_history(scope, {"ref": view["full_ref"], "full": True})["document"]["event"] == event
    stale = {**legacy, "full_ref": {**legacy["full_ref"], "content_hash": "sha256:" + "f" * 64}}
    with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
        engine._resume_event_view(run, stale)
    foreign = engine.create_conversation({"scope": {"kind": "catalog"}})
    foreign_run = engine.store.put("run", {**run, "id": "foreign-resume", "conversation_id": foreign["id"]})
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        engine._resume_event_view(foreign_run, legacy)
    sibling_run = engine.store.put("run", {**run, "id": "sibling-resume"})
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        engine._resume_event_view(sibling_run, legacy)
    fenced = old._resume_event_view(run, {**event, "future_action_fence": {"retry_permitted": False}})
    tampered = {**fenced, "future_action_fence": {"retry_permitted": True}, "new_future_field": "preserved"}
    with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
        engine._resume_event_view(run, tampered)
    restored = engine._resume_event_view(run, {**fenced, "new_future_field": "preserved"})
    assert restored["future_action_fence"] == {"retry_permitted": False}
    assert restored["new_future_field"] == "preserved"
    wrong_prep = deepcopy(event)
    wrong_prep["jobs"]["preparation"]["ref"]["id"] = "other-preparation"
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        engine._resume_event_view(run, old._resume_event_view(run, wrong_prep))
    forged = {**legacy, "full_ref": engine._snapshot_context(run, {"kind": "job", "job": {}})}
    with pytest.raises(ContractError, match="HISTORY_REF_INVALID"):
        engine._resume_event_view(run, forged)


@pytest.mark.asyncio
async def test_native_sdk_wait_wake_uses_two_invocations_and_summary_not_tool_json(waiting_context):
    engine, client, run, _, _, _, _ = waiting_context
    sent, credentials = [], OfflineCredentials()
    summaries = ["等待已持久后台任务。", "新进度已观察，未知结果仍须对账。"]
    def respond(request):
        payload = json.loads(request.content)
        assert engine._estimate_payload(payload) <= 48000
        sent.append(payload)
        if payload.get("tools"):
            number = sum(bool(p.get("tools")) for p in sent) - 1
            assert number < 2
            output = [{"type": "function_call", "namespace": "functions", "name": "finish_turn",
                "call_id": "finish-wait-" + str(number), "arguments": json.dumps({"outcome": "WAITING_JOBS", "summary": summaries[number]})}]
        else:
            output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "保留未知与原快照，按精确reader继续。"}]}]
        event = {"type": "response.completed", "response": {"status": "completed", "output": output,
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text="data: " + json.dumps(event) + "\n\n")
    engine.model = build_chatgpt_model(credentials=credentials, transport=httpx.MockTransport(respond), verify_catalog=False,
        before_send=engine._before_send, observer=engine._model_event, on_request=engine._model_request, on_response=engine._model_response)
    engine.saver = InMemorySaver()
    engine.main = engine._build_graph("main")  # Real M7 and native summary; no middleware patch.
    # The native summarizer wraps the model node rather than adding a graph node.
    await engine._drive_main(run["id"])
    first = engine.store.get("run", run["id"])
    assert first["status"] == "WAITING_JOBS" and tokens(first["acceptance"]) > 50000
    assert engine.get_run(run["id"])["messages"][-1]["content"] == summaries[0]
    job = client.observation["jobs"][0]
    job["body"]["generation_epoch"] += 1
    job["ref"] = {**job["ref"], "revision": job["ref"]["revision"] + 1, "content_hash": canonical_hash(job["body"])}
    await engine._wake_waiting(first)
    resumed = engine.store.get("run", run["id"])
    assert resumed["status"] == "RUNNING" and resumed["append_event"]["projection_version"] == 3
    await engine._drive_main(run["id"])
    final = engine.store.get("run", run["id"])
    assert final["status"] == "WAITING_JOBS" and final["acceptance"]["observation"]["unknown_job_ids"]
    messages = engine.get_run(run["id"])["messages"]
    assert [m["content"] for m in messages if m["role"] == "assistant"][-2:] == summaries
    main = [p for p in sent if p.get("tools")]
    assert len(main) == 2
    assert "EXACT_CONTEXT_TAIL" not in canonical_json(main[-1])
    events = engine.events(run["id"])["events"]
    jumps = [e for e in events if e.get("data", {}).get("reason_code") == "VERIFIED_FINISH_READY"]
    assert len(jumps) == 2 and len({e.get("invocation_id") for e in jumps}) == 2
    assert not any(e["type"] in {"context.window_rejected", "model.window_rejected"} for e in events)


def test_actual_v5_finish_and_unconsumed_v2_wake_replay_shrink_and_restore(tmp_path):
    from verification.probe_v4_context_projection_readonly import MemoryStore
    from shengbo_prelive.timing_report import Evidence
    root = ROOT / "verification/third-update-merchant-chain-v5-20261020/round-01"
    if not (root / "audio-extension-follow-up/ids.json").is_file():
        pytest.skip("local readonly v5 evidence unavailable")
    evidence = Evidence(root)
    identity = json.loads((root / "audio-extension-follow-up/ids.json").read_text(encoding="utf-8"))["run_id"]
    engine = object.__new__(RuntimeEngine)
    engine.store = MemoryStore(evidence.objects, evidence.attempts)
    run = engine.store.get("run", identity)
    before_hash = canonical_hash(run)
    old = before_engine(engine)
    legacy = deepcopy(run["append_event"])
    assert legacy["projection_version"] == 2 and tokens(legacy) > 9000
    view = engine._resume_event_view(run, legacy)
    assert tokens(view) < tokens(legacy) - 1500
    scope = RunScope(identity, "v5-readonly-replay", "execute")
    assert engine.run_history(scope, {"ref": view["full_ref"], "full": True})["document"]["event"] == engine.run_history(
        scope, {"ref": legacy["full_ref"], "full": True})["document"]["event"]
    raw_acceptance = run["acceptance"]
    old_finish = {"accepted": True, "outcome": "WAITING_JOBS", "acceptance": old._acceptance_view(run, raw_acceptance)}
    new_finish = {"accepted": True, "outcome": "WAITING_JOBS", "acceptance": engine._acceptance_view(run, raw_acceptance)}
    assert tokens(new_finish) < tokens(old_finish) - 1500
    assert engine.run_history(scope, {"ref": new_finish["acceptance"]["full_ref"], "full": True})["document"]["acceptance"] == raw_acceptance
    assert canonical_hash(engine.store.get("run", identity)) == before_hash
