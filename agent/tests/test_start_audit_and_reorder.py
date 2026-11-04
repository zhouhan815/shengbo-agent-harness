import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.models import IntentAssessment, RunScope
from shengbo_prelive.workflows import run_workflow
from test_audio_unknown_reissue import ReissuePCMAdapter
from test_optional_workflows import authorize
from test_runtime_business import BusinessError, Product, env, human_command, new_run, prepared_text


@pytest.mark.asyncio
async def test_start_audit_reads_exact_required_product_fields_without_side_effects(env):
    engine, service = env
    room = (await human_command(service, "create_room", {"name": "开播审核隔离房间"}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"],
        "room_revision": room["revision"]}))["data"]["preparation_ref"]
    fields = Product(name="折叠硅胶水杯", price=29.9).model_dump()
    product = (await human_command(service, "save_product", {"fields": fields,
        "schema_hash": service.schema()["schema_hash"], "patches": []}))["data"]["product_ref"]
    selected = await human_command(service, "update_preparation_products", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "selected_product_refs": [product]})
    before = await service.query("get_workspace_snapshot", {"preparation_id": prep["id"]})
    result = await service.query("read_start_readiness", {"preparation_id": prep["id"]})
    assert result["ready"] is False and result["publication_ref"] is None
    assert result["room_id"] == room["id"]
    assert result["preparation_ref"] == selected["data"]["preparation_ref"]
    assert {row["code"] for row in result["blockers"]} == {
        "START_PRODUCT_SELLING_POINTS_REQUIRED", "START_SESSION_REQUIRED", "START_SCRIPTS_REQUIRED", "START_PLAYLIST_REQUIRED"}
    assert result["blockers"][0]["product_ref"] == product
    assert result["blockers"][0]["product_name"] == "折叠硅胶水杯"
    assert before == await service.query("get_workspace_snapshot", {"preparation_id": prep["id"]})
    selling_points = "可折叠收纳，放入小包携带。"
    target = "只补核心卖点，暂不做顾客研究。"
    quote = target + "我确认这款水杯" + selling_points
    run = await new_run(engine, fields["name"] + "：" + quote, room_id=room["id"], preparation_id=prep["id"])
    assessment = IntentAssessment(objective="catalog_update", summary="只补明确提供的一条核心卖点",
        target_product_ids=[product["id"]], authorization_quote=quote, catalog_delivery="basic_facts_only",
        catalog_delivery_quote=target, requested_product_paths=["/core_selling_points"],
        product_field_scope_quote=target, proposed_effects=["save_product"])
    run = engine.store.update("run", run["id"], intent_assessment=assessment.model_dump())
    await engine._freeze_goal(run)
    current = (await service.query("read_product", {"product_id": product["id"]}))["body"]
    original_fields = {name: value for name, value in current.items() if name in Product.model_fields}
    updated = await engine.business_command(RunScope(run["id"], "audit-fill", "execute"), "save_product", {"product_id": product["id"],
        "expected_revision": product["revision"], "schema_hash": service.schema()["schema_hash"],
        "fields": {**original_fields, "selling_points": selling_points, "core_selling_points": [selling_points]},
        "patches": [{"path": "/core_selling_points", "op": "set", "value": [selling_points]}]}, "fill-required-points")
    review = await service.query("read_start_readiness", {"preparation_id": prep["id"]})
    blocker = next(row for row in review["blockers"] if row["code"] == "START_PRODUCT_VERSION_REQUIRED")
    assert blocker["pinned_product_ref"] == product
    assert blocker["product_ref"] == updated["data"]["product_ref"]
    assert "START_PRODUCT_SELLING_POINTS_REQUIRED" not in {row["code"] for row in review["blockers"]}
    await human_command(service, "record_product_revision_choice", {"preparation_id": prep["id"],
        "expected_revision": review["preparation_ref"]["revision"], "pinned_product_ref": blocker["pinned_product_ref"],
        "observed_head_ref": blocker["product_ref"], "diff_hash": blocker["diff_hash"], "choice": "refresh"})
    refreshed = await service.query("read_start_readiness", {"preparation_id": prep["id"]})
    assert {row["code"] for row in refreshed["blockers"]} == {
        "START_SESSION_REQUIRED", "START_SCRIPTS_REQUIRED", "START_PLAYLIST_REQUIRED"}


@pytest.mark.asyncio
async def test_explicit_agent_reorder_compiles_real_order_without_rewriting_scripts(env):
    engine, service = env
    room, prep, script = await prepared_text(service)
    old = await service.query("get_workspace_snapshot", {"preparation_id": prep["id"]})
    base = old["preparation_details"]["playlist"]["body"]["items"][0]
    items = [{**base, "occurrence_id": identity} for identity in ("o1", "o2", "o3")]
    saved = await human_command(service, "save_playlist", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "items": items, "loop_mode": "infinite"})
    playlist = saved["data"]["playlist_ref"]
    quote = "把现有话术队列随机重排，保持原来的内容。"
    run = await new_run(engine, quote, room_id=room["id"], preparation_id=prep["id"])
    run = await authorize(engine, run, ["save_playlist", "compile_playback_plan"])
    scope = RunScope(run["id"], "reorder-test", "execute")
    spec = {"mode": "full_shuffle", "product_group_order": [base["product_id"]], "seed": "0123456789abcdef" * 2}
    request = {"action": "reorder", "reorder_spec": spec}
    with pytest.raises(ContractError, match="GOAL_EFFECT_DENIED"):
        await run_workflow(engine, scope, "wf_playback_review", request)
    engine.store.update("run", run["id"], goal={**run["goal"], "objective": "playlist_reorder",
        "target_product_ids": [base["product_id"]]})
    expected = await service.query("preview_playlist_reorder", {"preparation_id": prep["id"],
        "ref": playlist, "spec": spec})
    result = await run_workflow(engine, scope, "wf_playback_review", request)
    assert result["completed_steps"] == [
        "read_preparation", "preview_playlist_reorder", "save_playlist", "compile_playback_plan"]
    after = await service.query("get_workspace_snapshot", {"preparation_id": prep["id"]})
    assert after["preparation_details"]["playlist"]["body"]["items"] == expected["proposed_items"]
    assert after["preparation"]["body"]["script_refs"] == [script]
    assert after["preparation_details"]["scripts"] == old["preparation_details"]["scripts"]
    plan = after["preparation_details"]["compiled_plan"]["body"]
    assert [row["occurrence_id"] for row in plan["entries"]] == [item["occurrence_id"] for item in expected["proposed_items"]]
    assert not (await service.query("read_completion_observation", {"preparation_id": prep["id"]}))["pending_job_ids"]


@pytest.mark.asyncio
async def test_audio_workflow_retries_only_the_known_failed_item(env, tmp_path):
    engine, service = env
    room, prep, _ = await prepared_text(service)
    review = await service.query("create_text_review", {"preparation_id": prep["id"]})
    await human_command(service, "approve_text_for_audio", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "review_hash": review["review_hash"]})
    rejected = review["texts"][0]["spoken_text"]

    class KnownFailureAdapter(ReissuePCMAdapter):
        async def render_audio(self, text, room_id, voice, *, style=None):
            if text == rejected and not self.calls[text]:
                self.calls[text] += 1
                raise BusinessError("TEST_AUDIO_REJECTED")
            return await super().render_audio(text, room_id, voice, style=style)

    adapter = KnownFailureAdapter(tmp_path / "voice")
    adapter.fail_next = 0
    service.adapter = adapter
    run = await new_run(engine, "生成本场所需语音，失败时只补失败句。", room_id=room["id"], preparation_id=prep["id"])
    run = await authorize(engine, run, ["prepare_required_audio", "retry_failed_items"])
    engine.store.update("run", run["id"], goal={**run["goal"], "objective": "audio_prepare"})
    scope = RunScope(run["id"], "audio-retry", "execute")
    prepare = {"action": "prepare"}
    submitted = await run_workflow(engine, scope, "wf_required_audio", prepare)
    job_id = submitted["result"]["data"]["job_id"]
    await service.drain_jobs()
    job = await service.query("read_job", {"job_id": job_id})
    assert job["body"]["status"] == "partially_failed"
    original_attempts = dict(adapter.calls)
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    await human_command(service, "save_asset_checklist", {"preparation_id": prep["id"],
        "expected_revision": current["ref"]["revision"], "checklist": {"items": [], "notes": ["整理直播间陈列"]}})
    observed = await run_workflow(engine, scope, "wf_required_audio", prepare)
    assert observed["status"] == "NEEDS_REPLAN"
    assert observed["result"]["data"]["job_id"] == job_id
    retried = await run_workflow(engine, scope, "wf_required_audio", {"action": "retry", "job_id": job_id})
    assert retried["status"] == "WAITING"
    assert retried["result"]["data"]["job_id"] == job_id
    await service.drain_jobs()
    ready = await service.query("read_audio_readiness", {"preparation_id": prep["id"], "limit": 1})
    assert ready["ready_for_publish"] is True
    assert dict(adapter.calls) == {**original_attempts, rejected: 2}
    jobs = (await service.query("read_completion_observation", {"preparation_id": prep["id"]}))["jobs"]
    assert [job["ref"]["id"] for job in jobs if job["body"]["kind"] == "audio"] == [job_id]
