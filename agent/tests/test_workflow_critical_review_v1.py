"""Independent offline review of optional-flow approval and idempotency edges.

Uses only temporary business/runtime stores and deterministic fake adapters.
No pilot process, credentials or live business state is touched.
"""
from copy import deepcopy
import pytest

from shengbo_prelive.models import RunScope
from shengbo_prelive.workflows import run_workflow
from test_runtime_business import env, human_command, new_run, prepared_text
from test_optional_workflows import authorize


async def approved_case(engine, service):
    room, prep, _ = await prepared_text(service)
    # Hold audio jobs queued deterministically. The shared fake deliberately
    # rejects actual TTS, so an automatically scheduled worker racing this
    # metadata-save test would turn it into an unrelated known-failure case.
    await service.close()
    service.wake_worker = lambda: None
    review = await service.query("create_text_review", {"preparation_id": prep["id"]})
    approval = await human_command(service, "approve_text_for_audio", {
        "preparation_id": prep["id"], "expected_revision": prep["revision"], "review_hash": review["review_hash"]})
    run = await new_run(engine, "生成本场所需语音，保留当前批准和话术", room_id=room["id"], preparation_id=prep["id"])
    await authorize(engine, run, ["prepare_required_audio", "save_asset_checklist"])
    return prep, review, approval, RunScope(run["id"], "independent-audio-flow", "execute")


@pytest.mark.asyncio
async def test_approved_audio_starts_job_and_identical_prepare_reuses_it(env):
    engine, service = env
    prep, review, approval, scope = await approved_case(engine, service)
    first = await run_workflow(engine, scope, "wf_required_audio", {"action": "prepare"})
    assert first["status"] == "WAITING"
    job = first["result"]["data"]["job_id"]
    second = await run_workflow(engine, scope, "wf_required_audio", {"action": "prepare"})
    assert second["status"] == "WAITING"
    assert second["result"]["data"]["job_id"] == job
    observation = await service.query("read_completion_observation", {"preparation_id": prep["id"]})
    assert len([j for j in observation["jobs"] if j["body"]["kind"] == "audio"]) == 1


@pytest.mark.asyncio
async def test_unrelated_checklist_save_does_not_duplicate_approved_pending_audio(env):
    engine, service = env
    prep, review, approval, scope = await approved_case(engine, service)
    first = await run_workflow(engine, scope, "wf_required_audio", {"action": "prepare"})
    job = first["result"]["data"]["job_id"]
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    await human_command(service, "save_asset_checklist", {"preparation_id": prep["id"], "expected_revision": current["ref"]["revision"],
        "checklist": {"items": [{"item_id": "photo-1", "category": "product_overview", "purpose": "商品外观", "recommended_content": "请提供清晰正面图片", "status": "missing"}], "notes": []}})
    new_review = await service.query("create_text_review", {"preparation_id": prep["id"]})
    assert new_review["review_hash"] == review["review_hash"]
    second = await run_workflow(engine, scope, "wf_required_audio", {"action": "prepare"})
    assert second["status"] == "WAITING"
    assert second["result"]["data"]["job_id"] == job
    observation = await service.query("read_completion_observation", {"preparation_id": prep["id"]})
    assert len([j for j in observation["jobs"] if j["body"]["kind"] == "audio"]) == 1


@pytest.mark.asyncio
async def test_revoked_approval_does_not_reuse_or_create_audio_job(env):
    engine, service = env
    prep, review, approval, scope = await approved_case(engine, service)
    first = await run_workflow(engine, scope, "wf_required_audio", {"action": "prepare"})
    assert first["status"] == "WAITING"
    await human_command(service, "revoke_text_approval", {"approval_ref": approval["data"]["approval_ref"], "reason": "独立回归：批准撤回"})
    result = await run_workflow(engine, scope, "wf_required_audio", {"action": "prepare"})
    assert result["status"] == "NEEDS_DECISION"
    observation = await service.query("read_completion_observation", {"preparation_id": prep["id"]})
    assert len([j for j in observation["jobs"] if j["body"]["kind"] == "audio"]) == 1


@pytest.mark.asyncio
async def test_late_unknown_item_in_full_aggregate_prevents_replacement_audio(env):
    engine, service = env
    prep, review, approval, scope = await approved_case(engine, service)
    first = await run_workflow(engine, scope, "wf_required_audio", {"action": "prepare"})
    job_id = first["result"]["data"]["job_id"]
    with service.store.transaction(write=True) as connection:
        job = service.store.get(connection, "prelive_job", job_id)
        body = deepcopy(job["body"])
        body["status"] = "failed"
        body["items"][-1].update(status="failed", call_state="result_received",
            provider_attempts=[{"state": "outcome_unknown"}])
        service.store.put(connection, "prelive_job", job_id, body, expected=job["ref"]["revision"])
    result = await run_workflow(engine, scope, "wf_required_audio", {"action": "prepare"})
    assert result["status"] == "OUTCOME_UNKNOWN"
    assert result["result"]["counts"]["unknown_job_items"] == 1
    observation = await service.query("read_completion_observation", {"preparation_id": prep["id"]})
    assert len([j for j in observation["jobs"] if j["body"]["kind"] == "audio"]) == 1


@pytest.mark.asyncio
async def test_known_failed_audio_returns_original_job_for_explicit_replanning(env):
    engine, service = env
    prep, review, approval, scope = await approved_case(engine, service)
    first = await run_workflow(engine, scope, "wf_required_audio", {"action": "prepare"})
    job_id = first["result"]["data"]["job_id"]
    with service.store.transaction(write=True) as connection:
        job = service.store.get(connection, "prelive_job", job_id)
        body = deepcopy(job["body"])
        body["status"] = "failed"
        for item in body["items"]:
            item.update(status="failed", call_state="not_sent")
        service.store.put(connection, "prelive_job", job_id, body, expected=job["ref"]["revision"])
    result = await run_workflow(engine, scope, "wf_required_audio", {"action": "prepare"})
    assert result["status"] == "NEEDS_REPLAN"
    assert result["result"]["data"]["job_id"] == job_id
    assert not any(item.get("status") == "sent" for item in engine.store.list("operation"))
    observation = await service.query("read_completion_observation", {"preparation_id": prep["id"]})
    assert len([j for j in observation["jobs"] if j["body"]["kind"] == "audio"]) == 1
