"""Retry is an optional capability, constrained by the original frozen task.

Uses the actual RuntimeEngine, selection guards and isolated voice service.
No real model or TTS requests are permitted by the shared test fixture.
"""
from copy import deepcopy
from unittest.mock import patch

import pytest

from test_runtime_business import env, prepared_text
from test_selection_binding import freeze, select
from app.prelive.storage import BusinessError
from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.workflows import run_workflow


async def failed_selected_module_case(engine, service, *, failures=100):
    room, prep, script = await prepared_text(service)
    service.wake_worker = lambda: None
    text = await service.query("read_script", {"ref": script})
    selection = select(script, module="selling_points", prep=prep,
        digest=canonical_hash(text["modules"]["selling_points"]["sentences"]))
    run, scope = await freeze(engine, "只修改选中的卖点话术，其他模块保持不变", selection, "script_change", prep=prep, room=room)
    state = await service.query("read_preparation", {"preparation_id": prep["id"]})
    actions = [{"key": key, "action": "generate" if key == "selling_points" else "reuse" if ref else "disable",
        **({"base_module_ref": ref} if ref else {}), "instruction": "恢复测试：只修改卖点表达。" if key == "selling_points" else ""}
        for key, ref in text["body"]["module_refs"].items()]
    plan = await engine.business_command(scope, "save_script_plan", {
        "preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "bundle_ref": state["body"]["bundle_ref"],
        "items": [{"product_ref": text["body"]["product_ref"], "base_script_ref": script,
            "variant_id": text["body"]["variant_id"], "version_number": text["body"]["version_number"], "modules": actions}]}, "retry-test-plan")
    state = await service.query("read_preparation", {"preparation_id": prep["id"]})
    calls = []
    async def controlled_generation(context, module_key, product, previous):
        calls.append(module_key)
        if len(calls) <= failures:
            raise BusinessError("OFFLINE_KNOWN_FAILURE", "明确的离线测试失败")
        return ["这款商品适合注重轻便体验的顾客。"]
    service.adapter.generate_module = controlled_generation
    receipt = await engine.business_command(scope, "submit_script_change", {
        "preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
        "plan_ref": plan["data"]["script_plan_ref"], "context_bundle_ref": state["body"]["bundle_ref"],
        "expected_generation_epoch": state["body"]["generation_epoch"]}, "retry-test-submit")
    await service.drain_jobs()
    job = await service.query("read_job", {"job_id": receipt["data"]["job_id"]})
    assert job["body"]["status"] == "failed"
    assert calls == ["selling_points"]
    return {"run": engine.store.get("run", run["id"]), "scope": scope, "prep": prep,
        "script": text, "job": job, "receipt": receipt, "calls": calls}


async def reject_with_job_projection(engine, case, changed_body, code):
    original_query = engine.client.query
    async def query(key, args=None, **kwargs):
        if key == "read_job":
            return {**deepcopy(case["job"]), "body": deepcopy(changed_body)}
        return await original_query(key, args, **kwargs)
    before = deepcopy(engine.store.list("operation"))
    with patch.object(engine.client, "query", side_effect=query), patch.object(engine.client, "command", side_effect=AssertionError("must not write")):
        with pytest.raises(ContractError, match=code):
            await engine.business_command(case["scope"], "retry_failed_items", {"job_id": case["job"]["body"]["id"]}, "rejected-retry")
    assert engine.store.list("operation") == before
    assert case["calls"] == ["selling_points"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["foreign_run", "foreign_preparation", "missing_parent_effect", "unsupported_kind"])
async def test_runtime_retry_rejects_wrong_owner_scope_or_parent_effect(env, bad):
    engine, service = env
    case = await failed_selected_module_case(engine, service)
    body = deepcopy(case["job"]["body"])
    if bad == "foreign_run":
        body["run_id"] = "foreign-run"
    elif bad == "foreign_preparation":
        body["preparation_id"] = "foreign-preparation"
    elif bad == "missing_parent_effect":
        # This real script_change Goal grants script generation and retry,
        # but does not grant audio generation.
        assert "retry_failed_items" in case["run"]["allowed_effects"]
        assert "prepare_required_audio" not in case["run"]["allowed_effects"]
        body["kind"] = "audio"
    else:
        body["kind"] = "unregistered-job-kind"
    await reject_with_job_projection(engine, case, body, "RETRY_SCOPE_DENIED")


@pytest.mark.asyncio
@pytest.mark.parametrize("call_state", ["sent", "timeout_unknown", "outcome_unknown"])
async def test_runtime_retry_requires_reconciliation_for_any_inflight_or_unknown_item(env, call_state):
    engine, service = env
    case = await failed_selected_module_case(engine, service)
    body = deepcopy(case["job"]["body"])
    body["items"][0]["call_state"] = call_state
    await reject_with_job_projection(engine, case, body, "RECONCILIATION_REQUIRED")


@pytest.mark.asyncio
async def test_runtime_retry_requires_original_committed_operation_receipt(env):
    engine, service = env
    case = await failed_selected_module_case(engine, service)
    operation = next(op for op in engine.store.list("operation") if op["key"] == "submit_script_change")
    engine.store.update("operation", operation["id"], status="known_failed")
    before = deepcopy(engine.store.list("operation"))
    with patch.object(engine.client, "command", side_effect=AssertionError("must not write")):
        with pytest.raises(ContractError, match="RETRY_SCOPE_DENIED"):
            await engine.business_command(case["scope"], "retry_failed_items", {"job_id": case["job"]["body"]["id"]}, "missing-original")
    assert engine.store.list("operation") == before
    assert case["calls"] == ["selling_points"]


@pytest.mark.asyncio
async def test_runtime_retry_rechecks_original_plan_binding_for_narrow_selected_module(env):
    engine, service = env
    case = await failed_selected_module_case(engine, service)
    assert case["run"]["module_allowlist"] == ["selling_points"]
    original = next(op for op in engine.store.list("operation") if op["key"] == "submit_script_change")
    # Simulate an inconsistent/untrusted original-action argument. Keeping the
    # job ID/receipt alone must not bypass the validated selected-module plan.
    changed = deepcopy(original["args"])
    changed["plan_ref"] = {**changed["plan_ref"], "id": "different-whole-script-plan"}
    engine.store.update("operation", original["id"], args=changed)
    before = deepcopy(engine.store.list("operation"))
    with patch.object(engine.client, "command", side_effect=AssertionError("must not write")):
        with pytest.raises(ContractError, match="SELECTION_PLAN_DENIED"):
            await engine.business_command(case["scope"], "retry_failed_items", {"job_id": case["job"]["body"]["id"]}, "wrong-plan-retry")
    assert engine.store.list("operation") == before
    assert case["calls"] == ["selling_points"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_counters", [False, True])
async def test_runtime_retry_attempt_budget_rejects_before_requeue(env, legacy_counters):
    engine, service = env
    case = await failed_selected_module_case(engine, service)
    body = deepcopy(case["job"]["body"])
    body["items"][0]["attempt"] = 3
    if legacy_counters:
        body["items"][0].pop("generation_attempts", None)
    else:
        body["items"][0]["generation_attempts"] = 3
    await reject_with_job_projection(engine, case, body, "RETRY_BUDGET_EXHAUSTED")


@pytest.mark.asyncio
async def test_workflow_retry_second_job_revision_gets_new_operation_and_preserves_other_modules(env):
    engine, service = env
    case = await failed_selected_module_case(engine, service, failures=2)
    job_id = case["job"]["body"]["id"]
    first = await run_workflow(engine, case["scope"], "wf_script_execution", {"action": "retry", "job_id": job_id})
    assert first["status"] == "WAITING", first
    await service.drain_jobs()
    intermediate = await service.query("read_job", {"job_id": job_id})
    assert intermediate["body"]["status"] == "failed"
    assert intermediate["ref"]["revision"] > case["job"]["ref"]["revision"]
    second = await run_workflow(engine, case["scope"], "wf_script_execution", {"action": "retry", "job_id": job_id})
    assert second["status"] == "WAITING", second
    assert first["result"]["operation_id"] != second["result"]["operation_id"]
    await service.drain_jobs()
    final = await service.query("read_job", {"job_id": job_id})
    assert final["body"]["status"] == "succeeded", final
    assert case["calls"] == ["selling_points"] * 3
    retries = [op for op in engine.store.list("operation") if op["key"] == "retry_failed_items"]
    assert len(retries) == 2 and all(op["status"] == "committed" for op in retries)
    current = await service.query("read_preparation", {"preparation_id": case["prep"]["id"]})
    updated = await service.query("read_script", {"ref": current["body"]["script_refs"][0]})
    old_refs = case["script"]["body"]["module_refs"]
    for key, ref in old_refs.items():
        if key != "selling_points":
            assert updated["body"]["module_refs"][key] == ref
    assert updated["body"]["module_refs"]["selling_points"] != old_refs["selling_points"]
    assert "prepare_required_audio" not in engine.store.get("run", case["run"]["id"])["allowed_effects"]


@pytest.mark.asyncio
async def test_committed_retry_replay_returns_original_receipt_after_job_replaces_selected_script(env):
    engine, service = env
    case = await failed_selected_module_case(engine, service, failures=1)
    args = {"job_id": case["job"]["body"]["id"]}
    first = await engine.business_command(case["scope"], "retry_failed_items", args, "same-committed-retry")
    await service.drain_jobs()
    completed = await service.query("read_job", {"job_id": args["job_id"]})
    assert completed["body"]["status"] == "succeeded"
    operations_before = deepcopy(engine.store.list("operation"))
    # This is a receipt replay, not authorization for another generation.
    # The old selected script legitimately became the new generated revision.
    repeated = await engine.business_command(case["scope"], "retry_failed_items", args, "same-committed-retry")
    assert repeated == first
    assert engine.store.list("operation") == operations_before
    assert case["calls"] == ["selling_points", "selling_points"]


@pytest.mark.asyncio
async def test_committed_retry_replay_is_not_a_fourth_attempt_after_budget_exhausted(env):
    engine, service = env
    case = await failed_selected_module_case(engine, service)
    args = {"job_id": case["job"]["body"]["id"]}
    await engine.business_command(case["scope"], "retry_failed_items", args, "budget-retry-first")
    await service.drain_jobs()
    second = await engine.business_command(case["scope"], "retry_failed_items", args, "budget-retry-second")
    await service.drain_jobs()
    final = await service.query("read_job", {"job_id": args["job_id"]})
    assert final["body"]["items"][0]["attempt"] == 3
    assert case["calls"] == ["selling_points"] * 3
    repeated = await engine.business_command(case["scope"], "retry_failed_items", args, "budget-retry-second")
    assert repeated == second
    with pytest.raises(ContractError, match="RETRY_BUDGET_EXHAUSTED"):
        await engine.business_command(case["scope"], "retry_failed_items", args, "actually-new-fourth-attempt")
    assert case["calls"] == ["selling_points"] * 3


@pytest.mark.asyncio
async def test_committed_retry_replay_rejects_different_arguments_for_same_tool_call_id(env):
    engine, service = env
    case = await failed_selected_module_case(engine, service)
    args = {"job_id": case["job"]["body"]["id"]}
    await engine.business_command(case["scope"], "retry_failed_items", args, "same-key-changed-args")
    with pytest.raises(ContractError) as error:
        await engine.business_command(case["scope"], "retry_failed_items", {"job_id": "another-job"}, "same-key-changed-args")
    assert error.value.code in {"OPERATION_CHANGED", "NOT_FOUND", "RETRY_SCOPE_DENIED"}
    # No new queue item or supplier call is created for the altered request.
    assert case["calls"] == ["selling_points"]
