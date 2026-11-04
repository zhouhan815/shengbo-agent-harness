from copy import deepcopy

import pytest

from shengbo_contracts.common import WorkScope, canonical_hash
from shengbo_prelive.models import MessageRequest, RunScope
from shengbo_prelive.runtime import RuntimeEngine
from shengbo_prelive.script_job_delivery import delivery_constraints, owned_delivery_items


def ref(kind, identity, value):
    return {"kind": kind, "id": identity, "revision": 1, "content_hash": canonical_hash(value)}


class OfflineVoice:
    def __init__(self, observation, products, scripts):
        self.observation, self.products, self.scripts = observation, products, scripts
        self.commands = []

    async def query(self, key, args, **_):
        if key == "read_completion_observation":
            return deepcopy(self.observation)
        if key == "read_preparation":
            return deepcopy(self.observation["preparation"])
        assert key in {"read_product", "read_script"}
        if key == "read_product":
            assert "schema_version" not in args["ref"], "Voice product lookups use RecordRef"
        records = self.products if key == "read_product" else self.scripts
        return deepcopy(records[args["ref"]["id"]])

    async def command(self, key, *_, **__):
        assert key in {"revoke_grant", "issue_grant"}
        self.commands.append(key)
        return {"status": "committed", "data": {"grant_id": "grant", "write_generation": 1}}


@pytest.fixture
def example(tmp_path):
    products, scripts, items = {}, {}, []
    for name, count in [("保冷袋 12L", 2088), ("保冷袋 20L", 1759)]:
        body = {"name": name}
        product = ref("product", name, body)
        products[name] = {"ref": product, "body": body}
        module = {"sentences": [{"text": "正式口播文本。"}]}
        script_body = {"product_ref": product, "variant_id": name, "preparation_id": "prep",
            "module_refs": {"opening_long": ref("module", name, module)}}
        script = ref("script_revision", name, script_body)
        scripts[name] = {"ref": script, "body": script_body, "modules": {"opening_long": module}}
        items.append({"status": "succeeded", "call_state": "result_received", "input": {"product_ref": product,
            "variant_id": name, "version_number": 1, "generation_mode": "whole_script"}, "output": {
            "script_ref": script, "content_diagnostics": {"by_opening": {"opening_long": {"spoken_characters": count}}}}})
    product_refs = [record["ref"] for record in products.values()]
    draft = ref("draft", "draft", {})
    job = {"kind": "scripts", "run_id": "run", "preparation_id": "prep", "status": "succeeded", "items": items}
    prep = {"room_id": "room", "draft_ref": draft, "bundle_ref": ref("context", "context", {}), "selected_product_refs": product_refs,
            "script_refs": [record["ref"] for record in scripts.values()]}
    observation = {"preparation": {"ref": ref("preparation", "prep", prep), "body": prep},
                   "jobs": [{"ref": ref("prelive_job", "job", job), "body": job}], "pending_job_ids": [], "unknown_job_ids": []}
    voice = OfflineVoice(observation, products, scripts)
    engine = RuntimeEngine(tmp_path, voice)
    scope = WorkScope(kind="preparation", preparation_id="prep").model_dump()
    conversation = engine.create_conversation({"scope": scope})
    counts = [{"product_ref": {**product, "schema_version": "PRELIVE-2.3"}, "count": 1}
              for product in product_refs]
    request = {"preparation_id": "prep", "draft_ref": draft, "product_counts": counts,
               "target_duration_seconds": {"min": 480, "max": 600},
               "writing_notes": "双十一预写稿。区分日常价与活动价，交代领券门槛和限购条件。"}
    run = engine.store.put("run", {"id": "run", "conversation_id": conversation["id"], "scope": scope,
        "scope_key": canonical_hash(scope), "status": "WAITING_JOBS", "phase": "execute", "grant_id": "grant",
        "write_generation": 1, "allowed_effects": ["save_script_plan", "submit_script_change"],
        "input": {"text": "新增文字稿，用于双十一。", "intent_mode": "new_task", "material_refs": [], "script_generation": request}, "goal": {"scope": scope,
        "objective": "materials_to_script", "script_mode": "generate", "script_delivery_requirement": "new_generation",
        "required_draft_ref": draft, "action_target_refs": product_refs, "script_generation": request,
        "target_product_ids": list(products), "requested_script_variants": [{"product_id": name, "count": 1} for name in products]}})
    operation = engine.store.put("operation", {"id": "operation", "run_id": "run", "key": "submit_script_change",
        "status": "committed", "receipt": {"data": {"job_id": "job"}}})
    return engine, voice, run, operation


@pytest.mark.parametrize("minimum", [None, 2000])
async def test_typed_ui_delivery_closes_saved_results_without_waking_model(example, minimum):
    engine, voice, run, _ = example
    run["goal"]["script_generation"]["minimum_spoken_characters"] = minimum
    run = engine.store.put("run", run)
    await engine._wake_waiting(run)
    saved = engine.store.get("run", run["id"])
    assert saved["status"] == "COMPLETED"
    assert saved["finish_metrics"]["extra_model_request_required"] is False
    assert [row["length_status"] for row in saved["delivery_record"]["script_length"]] == ["within_target", "below_target"]
    assert ("预计 7.3 分钟，未达明确最低字数" if minimum else "预计 7.3 分钟，低于篇幅目标") in saved["summary"]
    assert saved["delivery_record"]["delivery_quality"] == ("partial" if minimum else "complete")
    assert saved["delivery_record"]["semantic_approval_granted"] is False
    assert len(engine.store.list("message")) == 1
    assert voice.commands == ["revoke_grant"]


@pytest.mark.parametrize("reason", ["other_objective", "unknown", "foreign_job", "wrong_count"])
def test_no_autoclose_for_unproven_or_nonstandard_delivery(example, reason):
    _, voice, original, operation = example
    run, observation = deepcopy(original), deepcopy(voice.observation)
    if reason == "other_objective":
        run["goal"]["objective"] = "prepare_session"
    if reason == "unknown":
        observation["unknown_job_ids"] = ["job"]
    if reason == "foreign_job":
        observation["jobs"][0]["body"]["run_id"] = "other"
    if reason == "wrong_count":
        observation["jobs"][0]["body"]["items"].pop()
    assert owned_delivery_items(run, observation, [operation]) is None


@pytest.mark.parametrize(("notes", "minimum", "review"), [
    ("交代领券门槛和限购条件。", None, False),
    ("不得少于 2,000 个有效口播字", 2000, False),
    ("每篇至少 8.5 分钟。", 2040, False),
    ("字数必须恰好两千字。", None, True),
    ("按七版原稿评测并打分。", None, True),
    ("开场必须说满129减15。", None, True),
    ("一定要写出三条退款限制。", None, True),
    ("双十一预写稿，语气自然一点。", None, False),
    ("每篇必须不少于 2000 字。", 2000, False),
    ("开场必须说满129减15，每篇必须不少于 2000 字。", 2000, True),
    ("每版至少1800个汉字。", 1800, False),
    ("不要少于1900字。", 1900, False),
    ("每版两千字起。", None, True),
    ("请审核文案后再交付。", None, True),
    ("不用审核，语气自然一点。", None, False),
    ("至少1800个汉字，只算汉字不计数字。", 1800, True),
])
def test_conservative_notes_acceptance(notes, minimum, review):
    assert delivery_constraints({"writing_notes": notes}) == {
        "minimum_spoken_characters": minimum, "require_semantic_review": review}


async def test_typed_request_freezes_workbench_scope_without_intent_model(example):
    engine, voice, fixture, _ = example
    request = MessageRequest(text="请创作双十一文字稿", intent_mode="new_task",
        script_generation={**fixture["input"]["script_generation"], "writing_notes": "先写使用场景。每篇至少 2000 字。"})
    new = (await engine.submit_message(fixture["conversation_id"], request))["run"]
    await engine._drive_main(new["id"])
    run = engine.store.get("run", new["id"])
    assert run["status"] == "RUNNING" and run["phase"] == "execute"
    assert run["goal"]["script_generation"]["minimum_spoken_characters"] == 2000
    assert run["goal"]["script_generation"]["require_semantic_review"] is False
    assert run["goal"]["action_target_refs"] == fixture["goal"]["action_target_refs"]
    assert set(run["allowed_effects"]) == {"retry_failed_items", "save_context_bundle", "save_script_plan", "submit_script_change"}
    assert voice.commands == ["issue_grant"]


async def test_required_semantic_review_closes_only_as_partial_without_waking_model(example):
    engine, voice, run, _ = example
    run["goal"]["script_generation"]["require_semantic_review"] = True
    run = engine.store.put("run", run)
    await engine._wake_waiting(run)
    saved = engine.store.get("run", run["id"])
    assert saved["status"] == "COMPLETED"
    assert saved["delivery_record"]["delivery_quality"] == "partial"
    assert saved["delivery_record"]["semantic_review_status"] == "pending"
    assert saved["delivery_record"]["unmet_requirements"] == [{"code": "SCRIPT_SEMANTIC_REVIEW_PENDING"}]
    assert "额外内容要求待审核" in saved["summary"]
    assert voice.commands == ["revoke_grant"]


async def test_manual_finish_summary_cannot_override_pending_semantic_review(example):
    engine, _, run, _ = example
    run["goal"]["script_generation"]["require_semantic_review"] = True
    engine.store.put("run", {**run, "status": "RUNNING"})
    result = await engine.finish(RunScope(run["id"], "invoke", "execute"), "COMPLETED", "所有条件全部通过。")
    saved = engine.store.get("run", run["id"])
    assert result["accepted"] is True
    assert "所有条件全部通过" not in saved["finish"]["summary"]
    assert saved["delivery_record"]["delivery_quality"] == "partial"
    assert saved["delivery_record"]["semantic_review_status"] == "pending"


def add_failed_predecessor(example):
    engine, voice, _, operation = example
    newer = voice.observation["jobs"][0]["body"]
    newer.update(generation_epoch=12, plan_ref=ref("script_plan", "newer-plan", {}))
    failed_item = {**deepcopy(newer["items"][0]), "status": "failed", "output": None,
        "error": {"code": "SHORT_MODULE_TOO_LONG"}, "local_revalidation_attempts": 0,
        "provider_attempts": [{"state": "response_received", "validation_state": "rejected"}]}
    failed = {**newer, "status": "failed", "generation_epoch": 11,
        "plan_ref": ref("script_plan", "older-plan", {}), "items": [failed_item]}
    voice.observation["jobs"].insert(0, {"ref": ref("prelive_job", "failed-job", failed), "body": failed})
    engine.store.update("operation", operation["id"], receipt={"data": {"job_id": "failed-job"}})
    engine.store.put("operation", {**operation, "id": "successor-operation"})
    return failed, failed_item


async def test_higher_epoch_same_version_closes_after_known_empty_rejection(example):
    add_failed_predecessor(example)
    engine, voice, run, _ = example
    await engine._wake_waiting(run)
    saved = engine.store.get("run", run["id"])
    assert saved["status"] == "COMPLETED"
    assert len(saved["delivery_record"]["formal_script_refs"]) == 2
    assert saved["delivery_record"]["source"] == "typed_script_delivery"
    assert voice.commands == ["revoke_grant"]


@pytest.mark.parametrize("reason", ["unknown_attempt", "pending_job", "partial_modules", "partial_script",
    "same_epoch", "different_variant", "local_revalidation"])
def test_do_not_supersede_unresolved_partial_or_different_jobs(example, reason):
    failed, item = add_failed_predecessor(example)
    engine, voice, run, _ = example
    if reason == "unknown_attempt":
        item["provider_attempts"][0]["state"] = "timeout_unknown"
    elif reason == "pending_job":
        failed["status"] = "running"
    elif reason == "partial_modules":
        item["module_results"] = {"product_intro": {"status": "committed"}}
    elif reason == "partial_script":
        item["output"] = {"script_ref": ref("script_revision", "prior-output", {})}
    elif reason == "same_epoch":
        failed["generation_epoch"] = 12
    elif reason == "different_variant":
        item["input"]["variant_id"] = "not-the-replacement-version"
    elif reason == "local_revalidation":
        item["local_revalidation_attempts"] = 1
    assert owned_delivery_items(run, voice.observation, engine.store.list("operation")) is None
