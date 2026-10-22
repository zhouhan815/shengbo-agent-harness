"""Many audio items and attempts must not become unbounded resume/M3 input."""
from copy import deepcopy
import json
from unittest.mock import patch

import httpx
import pytest

from shengbo_contracts.business import COMMAND_ARGS
from shengbo_contracts.common import ContractError, canonical_hash, canonical_json
from shengbo_prelive.middleware import Audit, ContextProjection, ToolPolicy
from shengbo_prelive.models import RunScope
from shengbo_prelive.providers.chatgpt import build_chatgpt_model
from test_provider_output_convergence import call, final, tool_outputs
from test_waiting_context_projection import waiting_context, ref, tokens, OfflineCredentials


def audio_job(client, count=64, attempts_per_item=1):
    job = client.observation["jobs"][0]
    body = job["body"]
    body.update(kind="audio", status="running", items=[])
    for position in range(count):
        output = {"asset": {"id": f"audio-{position}", "spoken_text": "这是一段普通话商品口播。" * 90,
            "audio_path": f"audio/{position}.wav", "duration_seconds": 4.2,
            "sha256": canonical_hash(position), "voice": {"provider": "system", "voice": "Huihui"}}}
        body["items"].append({"item_id": f"audio-item-{position}", "status": "succeeded",
            "call_state": "result_received", "attempt": attempts_per_item, "input": {"private": "input " * 200},
            "output": output, "provider_attempts": [{"attempt_id": f"audio-attempt-{position}-{number}",
                "purpose": "tts", "state": "result_received", "input_hash": canonical_hash([position, number]),
                "output_hash": canonical_hash(output), "output": output,
                "usage": {"characters": 300}, "received_at": "2026-10-17T00:00:00+00:00"}
                for number in range(attempts_per_item)]})
    job["ref"] = ref("prelive_job", body["id"], body)
    client.observation["unknown_job_ids"] = []
    return job


@pytest.mark.asyncio
async def test_many_items_and_attempts_are_bounded_exact_and_keep_all_hidden_unknowns(waiting_context):
    engine, client, run, scope, _, sent, credentials = waiting_context
    job = audio_job(client, count=128, attempts_per_item=64)
    job["body"]["items"][-1]["provider_attempts"][-1]["state"] = "outcome_unknown"
    job["ref"] = ref("prelive_job", job["body"]["id"], job["body"])
    client.observation["unknown_job_ids"] = [job["body"]["id"]]
    original = deepcopy(job)
    view = await engine.business_query(scope, "read_job", {"job_id": job["body"]["id"]})
    assert tokens(view) < 5000
    assert 0 < len(view["body"]["items"]) <= 4
    index = view["items_projection"]
    assert index["total"] == 128 and index["provider_attempt_count"] == 8192
    assert index["unknown_item_count"] == index["unknown_provider_attempt_count"] == 1
    assert index["provider_state_counts"] == {"result_received": 8191, "outcome_unknown": 1}
    assert index["next_cursor"] == len(view["body"]["items"])
    for row in view["body"]["items"]:
        assert len(row["provider_attempts"]) <= 3
        assert row["provider_attempts_projection"]["total"] == 64
    unknown = engine.run_history(scope, {"ref": view["full_ref"],
        "path": "/job/body/items/127/provider_attempts/63"})["document"]
    assert unknown == original["body"]["items"][-1]["provider_attempts"][-1]
    page = engine.run_history(scope, {"ref": index["full_ref"], "path": index["path"],
        "cursor": index["next_cursor"], "limit": 1, "max_chars": 2000})
    assert page["selection"]["cursor"] == index["next_cursor"]
    assert engine.store.list("query_evidence")[-1]["result"] == original
    assert client.observation["jobs"][0] == original
    engine.store.update("run", run["id"], finish={"outcome": "READY_FOR_REVIEW", "summary": "不能隐藏未知调用"})
    acceptance = await engine.verify_finish(scope)
    assert not acceptance["ok"] and "业务任务尚未成功结束或结果未知" in acceptance["missing"]
    assert not sent and credentials.calls == 0


@pytest.mark.asyncio
async def test_many_jobs_keep_global_counts_and_scoped_pagination(waiting_context):
    engine, client, run, scope, _, sent, credentials = waiting_context
    original = deepcopy(audio_job(client, count=64))
    jobs = []
    for position in range(32):
        job = deepcopy(original)
        job["body"]["id"] = f"many-job-{position}"
        if position == 31:
            job["body"]["items"][-1]["call_state"] = "timeout_unknown"
            job["body"]["items"][-1]["provider_attempts"][0]["state"] = "timeout_unknown"
        job["ref"] = ref("prelive_job", job["body"]["id"], job["body"])
        jobs.append(job)
    observation = {**client.observation, "jobs": jobs, "unknown_job_ids": ["many-job-31"]}
    view = engine._completion_observation_view(run, observation)
    assert tokens(view) < 11000 and 0 < len(view["jobs"]) <= 4
    assert view["jobs_projection"]["total"] == 32
    assert view["jobs_projection"]["unknown_item_count"] == 1
    assert view["jobs_projection"]["unknown_provider_attempt_count"] == 1
    assert view["unknown_job_ids"] == ["many-job-31"]
    exact = engine.run_history(scope, {"ref": view["full_ref"], "full": True})
    assert exact["document"]["observation"]["jobs"][31] == jobs[31]
    foreign_conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    foreign = engine.store.put("run", {**run, "id": "foreign-many-jobs", "conversation_id": foreign_conversation["id"]})
    for reader, code in ((RunScope(foreign["id"], "foreign", "execute"), "RUN_SCOPE_DENIED"),
        (RunScope(run["id"], "child", "execute", "product_facts", "branch-product_facts"), "BRANCH_SCOPE_DENIED")):
        with pytest.raises(ContractError, match=code):
            engine.run_history(reader, {"ref": view["full_ref"], "path": "/observation/jobs/31"})
    with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
        engine.run_history(scope, {"ref": {**view["full_ref"], "content_hash": canonical_hash("tampered")}, "full": True})
    assert not sent and credentials.calls == 0


@pytest.mark.asyncio
async def test_real_sdk_64_audio_items_wait_wake_readiness_publish_and_finish(waiting_context):
    engine, client, run, scope, _, _, _ = waiting_context
    job = audio_job(client)
    for branch in engine.store.list("branch"):
        engine.store.update("branch", branch["id"], status="ACCEPTED")
    manifest_body = {"id": "manifest-many-audio", "entries": [{"asset_id": f"audio-{i}",
        "path": f"audio/{i}.wav", "sha256": canonical_hash(i)} for i in range(64)]}
    manifest_ref = ref("audio_manifest", manifest_body["id"], manifest_body)
    approval_ref = ref("approval", "approval-many-audio", {"approved": True})
    prep = client.observation["preparation"]
    prep["body"].update(audio_manifest_ref=manifest_ref, approval_ref=approval_ref)
    prep["ref"] = ref("preparation", prep["body"]["id"], prep["body"])
    inherited = {"ok": True, "missing": [], "observation": deepcopy(client.observation)}
    goal = {**run["goal"], "objective": "audio_prepare"}
    run = engine.store.update("run", run["id"], goal=goal, frozen_goal_hash=canonical_hash(goal),
        grant_id="grant-many-audio", write_generation=2,
        inherited_context={"related_run_id": "prior-many-audio", "prior_acceptance": inherited,
            "evidence_only": True, "authorization_inherited": False})
    original_query = client.query
    commands, requests = [], []

    async def query(key, args=None, **kwargs):
        if key == "read_audio_readiness":
            assert args["preparation_id"] == prep["body"]["id"]
            return {"preparation_ref": prep["ref"], "audio_manifest_ref": manifest_ref,
                "approval_ref": approval_ref, "ready_for_publish": True, "blockers": [],
                "counts": {"required": 64, "covered": 64, "file_valid": 64}}
        if key == "read_preparation":
            return deepcopy(prep)
        return await original_query(key, args, **kwargs)

    async def command(key, args, mutation, **kwargs):
        assert key == "publish_preparation"
        COMMAND_ARGS[key].model_validate(args)
        assert args["approval_ref"] == approval_ref
        commands.append((key, deepcopy(args)))
        client.observation["candidate_ready"] = True
        return {"command": key, "operation_id": mutation.operation_id, "status": "committed",
            "data": {"publication_ref": ref("publication", "publication-many-audio", {"ready": True})}}

    client.query, client.command = query, command
    credentials = OfflineCredentials()

    def respond(request):
        payload = json.loads(request.content)
        assert engine._estimate_payload(payload) <= 48000
        assert payload.get("tools"), "bounded wait/resume must not need a summary call"
        requests.append(payload)
        round_number = len(requests)
        if round_number == 1:
            output = [call("finish_turn", "many-wait", {"outcome": "WAITING_JOBS", "summary": "等待本机音频任务"})]
        elif round_number == 2:
            user = [item for item in payload["input"] if item.get("role") == "user"][-1]
            event = json.loads(user["content"])
            assert event["jobs"]["jobs_projection"]["provider_attempt_count"] == 64
            assert event["jobs"]["jobs"][0]["items_projection"]["total"] == 64
            output = [call("read_audio_readiness", "many-read-readiness", {"preparation_id": prep["body"]["id"]})]
        elif round_number == 3:
            assert tool_outputs(payload)["many-read-readiness"]["counts"]["file_valid"] == 64
            output = [call("publish_preparation", "many-publish", {"preparation_id": prep["body"]["id"],
                "expected_revision": prep["ref"]["revision"], "approval_ref": approval_ref})]
        elif round_number == 4:
            assert tool_outputs(payload)["many-publish"]["status"] == "committed"
            output = [call("finish_turn", "many-finish", {"outcome": "READY_FOR_REVIEW", "summary": "64条音频已核实，仅发布未采用候选"})]
        else:
            raise AssertionError("bounded wait/resume should converge in four actual model calls")
        event = {"type": "response.completed", "response": {"status": "completed", "output": output,
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text="data: " + json.dumps(event) + "\n\n")

    engine.model = build_chatgpt_model(credentials=credentials, transport=httpx.MockTransport(respond),
        verify_catalog=False, before_send=engine._before_send, observer=engine._model_event,
        on_request=engine._model_request, on_response=engine._model_response)
    engine.main = engine._build_graph("main")
    await engine._invoke(run, invocation_id="many-audio-first")
    waiting = engine.store.update("run", run["id"], status="WAITING_JOBS", last_progress_hash=None)
    assert engine.store.get("run", run["id"])["finish"]["outcome"] == "WAITING_JOBS"
    job["body"]["status"] = "succeeded"
    job["ref"] = ref("prelive_job", job["body"]["id"], job["body"])
    client.observation["pending_job_ids"] = []
    await engine._wake_waiting(waiting)
    resumed = engine.store.get("run", run["id"])
    assert resumed["status"] == "RUNNING" and tokens(resumed["append_event"]) < 6000
    # _drive_main clears the previous finish before each resumed invocation.
    resumed = engine.store.update("run", run["id"], finish=None)
    await engine._invoke(resumed, invocation_id="many-audio-resumed")
    saved = engine.store.get("run", run["id"])
    assert saved["finish"]["outcome"] == "READY_FOR_REVIEW" and saved["acceptance"]["ok"]
    assert saved["acceptance"]["observation"] == client.observation
    assert saved["inherited_context"]["prior_acceptance"] == inherited
    assert [name for name, _ in commands] == ["publish_preparation"]
    assert len(requests) == credentials.calls == 4
    assert max(engine._estimate_payload(payload) for payload in requests) <= 48000
    assert not any(event["type"] in {"context.summarized", "context.tools_offloaded", "model.window_rejected"}
        for event in engine.events(run["id"], limit=1000)["events"])
