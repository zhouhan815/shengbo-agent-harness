from copy import deepcopy

import pytest

from shengbo_contracts.business import SaveScriptPlanArgs
from shengbo_contracts.common import ContractError, SCHEMA_VERSION
from shengbo_prelive.workflows import Flow, ScriptFlowInput, run_workflow
from test_runtime_business import env
from test_split_script_completion_v3 import setup_task


async def typed_task(engine, service):
    async def write_script(context, actions, product):
        return {"modules": {item["key"]: {"sentences": ["现在来看这款商品。"]}
                            for item in actions if item["action"] == "generate"}}

    service.adapter.generate_script = write_script
    task = await setup_task(engine, service, count=1)
    preparation = await service.query("read_preparation", {"preparation_id": task["preparation_id"]})
    projected_ref = {**task["product_ref"], "schema_version": SCHEMA_VERSION}
    run = engine.store.get("run", task["run_id"])
    engine.store.update("run", run["id"], goal={**run["goal"], "script_generation": {
        "preparation_id": task["preparation_id"], "draft_ref": preparation["body"]["draft_ref"],
        "product_counts": [{"product_ref": projected_ref, "count": 1}],
    }})
    arguments = {"action": "plan_execute_wait", "bundle_ref": task["bundle_ref"], "items": [
        {"product_ref": projected_ref, "variant_id": "native-product-ref",
         "modules": [{"key": key, "action": "generate"}
                     for key in ("opening_long", "product_intro", "selling_points", "cta")]}
    ]}
    return task, preparation, arguments


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_failed", [False, True])
async def test_typed_script_plan_uses_exact_voice_product_record(env, resume_failed):
    engine, service = env
    task, prep, arguments = await typed_task(engine, service)
    name = "wf_script_execution"
    old_checkpoint = None
    if resume_failed:
        data = ScriptFlowInput.model_validate(arguments).model_dump(mode="json", exclude_none=True)
        flow = Flow(engine, task["scope"], name, data)
        old = SaveScriptPlanArgs.model_validate({"preparation_id": task["preparation_id"],
            "expected_revision": prep["ref"]["revision"], "bundle_ref": task["bundle_ref"],
            "items": data["items"]}).model_dump(mode="json", exclude_none=True)
        old["items"][0]["generation_mode"] = "whole_script"
        old_checkpoint = engine.store.put("script_plan_handoff", {"id": flow.id, "run_id": task["run_id"],
            "plan_args": old})
        with pytest.raises(ContractError, match="REF_SCHEMA_MISMATCH"):
            await flow.command("save_script_plan", old)

    response = await run_workflow(engine, task["scope"], name, arguments)
    assert response["status"] == "WAITING", response
    assert response["result"]["waiting_registered"] is True
    plans = [op for op in engine.store.list("operation")
             if op["key"] == "save_script_plan" and op["run_id"] == task["run_id"]]
    committed, = [op for op in plans if op["status"] == "committed"]
    assert committed["args"]["items"][0]["product_ref"] == task["product_ref"]
    assert "schema_version" not in committed["args"]["items"][0]["product_ref"]
    if old_checkpoint:
        failed, = [op for op in plans if op["status"] == "known_failed"]
        assert failed["error_code"] == "REF_SCHEMA_MISMATCH"
        assert engine.store.get("script_plan_handoff", old_checkpoint["id"]) == old_checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"revision": 999}, {"content_hash": "sha256:" + "0" * 64}, {"schema_version": "PRELIVE-9.9"},
])
async def test_typed_script_plan_rejects_different_reference_before_any_plan_write(env, change):
    engine, service = env
    task, _, arguments = await typed_task(engine, service)
    bad = deepcopy(arguments)
    bad["items"][0]["product_ref"].update(change)
    with pytest.raises(ContractError, match="TARGET_SCOPE_UNVERIFIED"):
        await run_workflow(engine, task["scope"], "wf_script_execution", bad)
    assert not [op for op in engine.store.list("operation") if op["key"] == "save_script_plan"]
