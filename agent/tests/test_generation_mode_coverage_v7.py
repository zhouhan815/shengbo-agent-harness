"""Frozen delivery intent and formal generation roots; no paid adapters."""
from copy import deepcopy
import json

import pytest
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.middleware import ActionBoundary
from shengbo_prelive.models import IntentAssessment, RunScope
from shengbo_prelive.policy import validate_intent
from shengbo_prelive.target_scope import guard_command_targets, verify_script_coverage
from test_middleware import FakeEngine
from test_runtime_business import env, human_command, new_run, product, BindableFakeModel
from test_target_completion_scope import A, B, Client, Store, observe, operation, ref, run_for, script


def modern_run(mode="generate", objective="materials_to_script", count=2, **extra):
    return run_for((A,), objective, required_analyses=[], script_mode=mode,
        script_delivery_requirement="new_generation" if mode == "generate" else None,
        requested_script_variants=[{"count": count}] if count else [], **extra)


def document(identity, variant="v1", **extra):
    return script(identity, variant=variant, preparation_id="prep", **extra)


def successful_job(*documents, original=None):
    plan_ref = ref("script_plan", "real-plan")
    items = [{"item_id": "item-" + str(i), "status": "succeeded", "call_state": "result_received",
        "input": {"product_ref": doc["body"]["product_ref"], "variant_id": doc["body"]["variant_id"],
            **({"original_script_ref": original["ref"]} if original else {})},
        "output": {"script_ref": doc["ref"]}} for i, doc in enumerate(documents)]
    body = {"id": "real-job", "kind": "scripts", "run_id": "run", "preparation_id": "prep",
        "plan_ref": plan_ref, "items": items, "status": "succeeded"}
    op = operation("submit_script_change", {"plan_ref": plan_ref}, {"job_id": body["id"]})
    return {"ref": ref("prelive_job", body["id"]), "body": body}, op


async def coverage(run, documents, operations=(), jobs=(), current=None):
    observation = observe((A,), scripts=current if current is not None else [doc["ref"] for doc in documents])
    observation["jobs"] = list(jobs)
    return await verify_script_coverage(Client({doc["ref"]["id"]: doc for doc in documents}),
        Store(run, operations), run, list(operations), observation, [A])


@pytest.mark.asyncio
async def test_G01_explicit_generate_two_imports_cannot_satisfy_delivery_count():
    one, two = document("import-one", origin="imported"), document("import-two", "v2", origin="imported")
    operations = [operation("import_script", {"product_ref": A}, {"script_ref": doc["ref"]}, doc["ref"]["id"]) for doc in (one, two)]
    result = await coverage(modern_run(), [one, two], operations)
    assert result["missing"] and result["delivery_counts"]["generated"] == 0
    assert any("版数不足" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_G02_import_patch_is_editing_not_generated_even_without_explicit_count():
    original = document("original", origin="imported")
    edited = document("edited", parent_ref=original["ref"], origin="imported")
    operations = [operation("import_script", {"product_ref": A}, {"script_ref": original["ref"]}),
        operation("patch_script", {"script_ref": original["ref"]}, {"script_ref": edited["ref"]})]
    result = await coverage(modern_run(count=0), [original, edited], operations, current=[edited["ref"]])
    assert any("本run成功生成" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_G03_successful_same_run_jobs_count_while_import_is_reference_only():
    one, two, original = document("generated-one"), document("generated-two", "v2"), document("basis", "basis", origin="imported")
    job, op = successful_job(one, two)
    imported = operation("import_script", {"product_ref": A}, {"script_ref": original["ref"]})
    result = await coverage(modern_run(), [one, two, original], [imported, op], [job])
    assert not result["missing"] and result["delivery_counts"]["generated"] == 2
    assert result["delivery_counts"]["imported"] == 1


@pytest.mark.asyncio
async def test_G04_generated_root_two_real_patches_still_one_variant_without_new_provenance_fields():
    root = document("generated-root")
    middle = document("middle", parent_ref=root["ref"])
    final = document("final", parent_ref=middle["ref"])
    job, submitted = successful_job(root)
    patches = [operation("patch_script", {"script_ref": old["ref"]}, {"script_ref": new["ref"]}, "patch-" + str(i))
        for i, (old, new) in enumerate(((root, middle), (middle, final)))]
    # List order is not the lineage authority, exact edges and formal parents are.
    result = await coverage(modern_run(count=1), [root, middle, final], [submitted, *reversed(patches)], [job], [final["ref"]])
    assert not result["missing"] and result["delivery_counts"]["generated"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["foreign_run", "foreign_preparation", "wrong_kind", "failed", "partial", "unknown", "missing_output", "wrong_variant", "wrong_product", "provenance_conflict"])
async def test_G05_non_generated_or_inconsistent_job_evidence_never_becomes_success(defect):
    doc = document("formal")
    job, op = successful_job(doc)
    body, item = job["body"], job["body"]["items"][0]
    if defect == "foreign_run": body["run_id"] = "other-run"
    elif defect == "foreign_preparation": body["preparation_id"] = "other-prep"
    elif defect == "wrong_kind": body["kind"] = "audio"
    elif defect in {"failed", "partial"}: item["status"] = defect
    elif defect == "unknown": item["call_state"] = "outcome_unknown"
    elif defect == "missing_output": item["output"] = None
    elif defect == "wrong_variant": doc["body"]["variant_id"] = "other-variant"
    elif defect == "wrong_product": doc["body"]["product_ref"] = B
    else: doc["body"]["generation_provenance"] = {"job_id": "invented", "item_id": "wrong"}
    assert (await coverage(modern_run(count=1), [doc], [op], [job]))["missing"]


@pytest.mark.asyncio
async def test_G06_expansion_counts_only_new_job_variants_and_their_patches():
    original, first, second = document("original", "basis", origin="imported"), document("new-1"), document("new-2", "v2")
    final = document("new-2-edited", "v2", parent_ref=second["ref"])
    job, op = successful_job(first, second, original=original)
    patched = operation("patch_script", {"script_ref": second["ref"]}, {"script_ref": final["ref"]})
    imported = operation("import_script", {"product_ref": A}, {"script_ref": original["ref"]})
    result = await coverage(modern_run("expand_original"), [original, first, second, final], [imported, op, patched], [job],
        [original["ref"], first["ref"], final["ref"]])
    assert not result["missing"] and result["delivery_counts"]["expanded"] == 2
    del job["body"]["items"][1]["input"]["original_script_ref"]
    assert (await coverage(modern_run("expand_original"), [original, first, second, final], [imported, op, patched], [job],
        [original["ref"], first["ref"], final["ref"]]))["missing"]


@pytest.mark.asyncio
async def test_G07_import_mode_counts_only_actual_imports_not_generated_job_outputs():
    doc = document("original", origin="imported")
    imported = operation("import_script", {"product_ref": A}, {"script_ref": doc["ref"]})
    assert not (await coverage(modern_run("import_original", count=1), [doc], [imported]))["missing"]
    job, submitted = successful_job(doc)
    assert (await coverage(modern_run("import_original", count=1), [doc], [submitted], [job]))["missing"]


@pytest.mark.asyncio
async def test_G08_edit_can_patch_an_import_without_full_generation_or_seven_modules():
    original = document("original", origin="imported")
    edited = document("edited", parent_ref=original["ref"], origin="imported")
    patched = operation("patch_script", {"script_ref": original["ref"]}, {"script_ref": edited["ref"]})
    assert not (await coverage(modern_run("edit", objective="script_change", count=1), [original, edited], [patched], current=[edited["ref"]]))["missing"]
    edited["body"]["parent_ref"] = ref("script_revision", "wrong-parent")
    assert (await coverage(modern_run("edit", objective="script_change", count=1), [original, edited], [patched], current=[edited["ref"]]))["missing"]


@pytest.mark.asyncio
async def test_G09_G13_immutable_legacy_goals_keep_compatibility_but_null_is_not_legacy():
    doc = document("original", origin="imported")
    imported = operation("import_script", {"product_ref": A}, {"script_ref": doc["ref"]})
    run = run_for((A,), "materials_to_script", required_analyses=[])
    digest = canonical_hash(run["goal"])
    assert not (await coverage(run, [doc], [imported]))["missing"]
    assert canonical_hash(run["goal"]) == digest
    run = run_for((A,), "prepare_session", required_analyses=[], script_mode="generate")
    assert (await coverage(run, [doc]))["legacy_compatibility"] is True
    run["goal"]["script_delivery_requirement"] = None
    result = await coverage(run, [doc])
    assert result["legacy_compatibility"] is False and result["missing"]


@pytest.mark.asyncio
async def test_G10_G11_prepare_new_generation_cannot_use_old_or_imported_but_reuse_needs_no_job():
    one, two = document("old-one"), document("old-two", "v2")
    run = modern_run(objective="prepare_session")
    run["task_assessment"] = {"reuse_refs": [one["ref"], two["ref"]], "steps": ["改为复用"]}
    assert (await coverage(run, [one, two]))["missing"]
    imports = [operation("import_script", {"product_ref": A}, {"script_ref": doc["ref"]}, doc["ref"]["id"]) for doc in (one, two)]
    assert (await coverage(run, [one, two], imports))["missing"]
    run["goal"]["script_delivery_requirement"] = "reuse_existing"
    result = await coverage(run, [one, two])
    assert not result["missing"] and result["delivery_counts"]["generated"] == 0


@pytest.mark.asyncio
async def test_reuse_existing_prewrite_guard_blocks_writing_but_not_reference_import_or_audio():
    run = modern_run(objective="prepare_session")
    run["goal"]["script_delivery_requirement"] = "reuse_existing"
    client = Client({})
    for command in ("submit_script_change", "patch_script"):
        with pytest.raises(ContractError, match="SCRIPT_MODE_CONFLICT") as caught:
            await guard_command_targets(client, Store(run), run, command, {})
        assert caught.value.details["commit_state"] == "not_committed"
    await guard_command_targets(client, Store(run), run, "import_script", {"product_ref": A})
    await guard_command_targets(client, Store(run), run, "prepare_required_audio", {})
    assert not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", [None, "new_generation", "reuse_existing"])
async def test_G12_real_intent_save_and_freeze_requires_explicit_prepare_delivery(env, delivery):
    engine, service = env
    target = await product(service, "交付模式范围商品")
    room = (await human_command(service, "create_room", {"name": "交付模式房间"}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    await human_command(service, "update_preparation_products", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "selected_product_refs": [target]})
    text = "为" + target["id"] + "准备整场直播，" + ("沿用当前有效话术，不改稿" if delivery == "reuse_existing" else "生成新话术")
    run = await new_run(engine, text, room_id=room["id"], preparation_id=prep["id"])
    assessment = IntentAssessment(objective="prepare_session", summary="本场准备", authorization_quote=text,
        target_product_ids=[target["id"]], script_delivery_requirement=delivery)
    scope = RunScope(run["id"], "intent-delivery", "intent")
    if delivery is None:
        with pytest.raises(ContractError, match="SCRIPT_MODE_CONFLICT"):
            engine.save_assessment(scope, assessment.model_dump())
        assert not engine.store.get("run", run["id"]).get("intent_assessment")
        run = engine.store.update("run", run["id"], intent_assessment=assessment.model_dump(), confirmed_intent_hash=canonical_hash(assessment))
        with pytest.raises(ContractError, match="SCRIPT_MODE_CONFLICT"):
            await engine._freeze_goal(run)
        assert not engine.store.get("run", run["id"]).get("goal")
    else:
        engine.save_assessment(scope, assessment.model_dump())
        await engine._freeze_goal(engine.store.get("run", run["id"]))
        saved = engine.store.get("run", run["id"])
        assert saved["goal"]["script_delivery_requirement"] == delivery
        if delivery == "reuse_existing":
            execute = RunScope(run["id"], "deny-new-writing", "execute")
            with pytest.raises(ContractError, match="SCRIPT_MODE_CONFLICT") as caught:
                await engine.business_command(execute, "submit_script_change", {"preparation_id": prep["id"], "expected_revision": 2,
                    "plan_ref": ref("script_plan", "not-sent"), "context_bundle_ref": ref("context", "not-sent"), "expected_generation_epoch": 1}, "deny-before-send")
            assert caught.value.details["commit_state"] == "not_committed"
            assert not engine.store.list("operation")


@pytest.mark.asyncio
async def test_prepare_missing_delivery_returns_existing_M5_feedback_and_can_be_corrected():
    text = "沿用当前稿准备整场直播"
    @tool
    async def assess(delivery: str | None):
        """Validate current preparation intent without side effects."""
        return validate_intent({"input": {"text": text}}, IntentAssessment(objective="prepare_session", summary="本场准备",
            authorization_quote=text, script_delivery_requirement=delivery))
    engine = FakeEngine()
    model = BindableFakeModel(responses=[AIMessage(content="", tool_calls=[{"name": "assess", "args": {"delivery": None}, "id": "missing"}]),
        AIMessage(content="", tool_calls=[{"name": "assess", "args": {"delivery": "reuse_existing"}, "id": "corrected"}]), AIMessage(content="已明确复用原稿")])
    graph = create_agent(model, tools=[assess], middleware=[ActionBoundary(engine)], context_schema=RunScope)
    result = await graph.ainvoke({"messages": [HumanMessage(text)]}, context=RunScope("r1", "correct-delivery", "intent"))
    outputs = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert outputs[0].status == "error" and json.loads(outputs[0].content)["error"]["code"] == "SCRIPT_MODE_CONFLICT"
    assert outputs[1].status == "success"
    assert engine.store.get("run", "r1")["correctable_tool_errors"] == {"SCRIPT_MODE_CONFLICT": 1}


def test_narrow_generate_normalizes_delivery_and_rejects_downgrade():
    for objective in ("materials_to_script", "script_change"):
        text = "为商品生成新话术"
        assessment = IntentAssessment(objective=objective, summary="新创作", authorization_quote=text)
        assert validate_intent({"input": {"text": text}}, assessment)["script_delivery_requirement"] == "new_generation"
        with pytest.raises(ContractError, match="SCRIPT_MODE_CONFLICT"):
            validate_intent({"input": {"text": text}}, assessment.model_copy(update={"script_delivery_requirement": "reuse_existing"}))
