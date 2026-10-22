"""Real Runtime/workflow -> isolated Voice -> archived local recovery; no model calls."""
from copy import deepcopy
import inspect
import json
from unittest.mock import patch

import pytest

from test_runtime_business import env
from test_retry_runtime_v1 import failed_selected_module_case
from app.prelive import module_generation
from app.prelive.storage import BusinessError
from shengbo_contracts.common import ContractError
from shengbo_prelive.workflows import run_workflow


async def archived_selected_case(engine, service, *, paid_attempts=3):
    """Use a real frozen selected-module Goal, real grant and submit receipt.

    The first attempt is the existing fake known failure; subsequent fake
    provider attempts durably archive valid content but simulate an old local
    rejection. The operation/job/attempt history is never manually rewritten.
    """
    case = await failed_selected_module_case(engine, service)
    archived_calls = []

    async def archived_generation(context, key, product, previous, *, response_observer):
        archived_calls.append(key)
        sentence = {"display_text": "有需要可以了解一下。", "spoken_text": "有需要可以了解一下。",
            "kind": "engagement", "fact_ids": []}
        receipt = {"provider": "offline", "model": "offline-r12", "module_key": key,
            "prompt_revision": module_generation.module_prompt_revision(key),
            "content": json.dumps({"module_key": key, "sentences": [sentence]}, ensure_ascii=False),
            "validation_product": deepcopy(product), "finish_reason": "stop", "unsupported_response": False,
            "usage": {"total_tokens": 42}, "usage_state": "known"}
        observed = response_observer(receipt)
        if inspect.isawaitable(observed):
            await observed
        raise BusinessError("GENERATED_MODULE_NEEDS_REVIEW", "隔离测试模拟旧规则拒绝；不调用外部供应商")

    service.adapter.generate_module = archived_generation
    job_id = case["job"]["body"]["id"]
    for index in range(1, paid_attempts):
        await engine.business_command(case["scope"], "retry_failed_items", {"job_id": job_id}, f"r12-seed-rejected-{index}")
        await service.drain_jobs()
    current = await service.query("read_job", {"job_id": job_id})
    assert current["body"]["status"] == "failed"
    assert current["body"]["items"][0]["attempt"] == paid_attempts
    assert current["body"]["items"][0]["provider_attempts"][-1]["validation_state"] == "rejected"
    assert current["body"]["run_id"] == case["run"]["id"]
    original = next(op for op in engine.store.list("operation") if op["key"] == "submit_script_change")
    assert original["status"] == "committed"
    assert original["receipt"]["data"]["job_id"] == job_id
    case.update(job=current, archived_calls=archived_calls)
    return case


@pytest.mark.asyncio
async def test_workflow_local_revalidation_passes_paid_three_limit_and_commits_real_script(env):
    engine, service = env
    case = await archived_selected_case(engine, service)
    job_id = case["job"]["body"]["id"]
    original_attempts = deepcopy(case["job"]["body"]["items"][0]["provider_attempts"])
    with patch.object(service.adapter, "generate_module", side_effect=AssertionError("NO NEW PROVIDER CALL")):
        with pytest.raises(ContractError, match="RETRY_BUDGET_EXHAUSTED"):
            await engine.business_command(case["scope"], "retry_failed_items", {"job_id": job_id}, "r12-paid-fourth-denied")
        result = await run_workflow(engine, case["scope"], "wf_script_execution", {
            "action": "retry", "job_id": job_id, "recovery_mode": "revalidate_archived"})
        assert result["status"] == "WAITING", result
        assert result["result"]["data"]["recovery_mode"] == "revalidate_archived"
        await service.drain_jobs()
    final = await service.query("read_job", {"job_id": job_id})
    assert final["body"]["status"] == "succeeded", final
    item = final["body"]["items"][0]
    assert item["provider_attempts"] == original_attempts
    assert item["attempt"] == 4  # Three fake provider executions, one local validation.
    assert item["generation_attempts"] == 3
    assert item["local_revalidation_attempts"] == 1
    assert item["archive_revalidations"][0]["status"] == "completed"
    assert item["archive_revalidations"][0]["script_ref"] == item["output"]["script_ref"]
    state = await service.query("read_preparation", {"preparation_id": case["prep"]["id"]})
    assert state["body"]["script_refs"] == [item["output"]["script_ref"]]
    script = await service.query("read_script", {"ref": item["output"]["script_ref"]})
    for key, old_ref in case["script"]["body"]["module_refs"].items():
        if key != "selling_points":
            assert script["body"]["module_refs"][key] == old_ref
    operation = next(op for op in engine.store.list("operation") if op["id"] == result["result"]["operation_id"])
    assert operation["status"] == "committed"
    assert operation["args"] == {"job_id": job_id, "recovery_mode": "revalidate_archived"}
    assert case["calls"] == ["selling_points"]
    assert case["archived_calls"] == ["selling_points", "selling_points"]


@pytest.mark.asyncio
async def test_runtime_local_revalidation_has_its_own_three_request_limit(env):
    engine, service = env
    case = await archived_selected_case(engine, service)
    job_id = case["job"]["body"]["id"]
    original_attempts = deepcopy(case["job"]["body"]["items"][0]["provider_attempts"])
    args = {"action": "retry", "job_id": job_id, "recovery_mode": "revalidate_archived"}
    with patch.object(service.adapter, "generate_module", side_effect=AssertionError("NO NEW PROVIDER CALL")), \
            patch.object(module_generation, "finish_received_module", side_effect=BusinessError("MODULE_RESPONSE_INVALID", "仍无法通过当前本地校验")):
        for _ in range(3):
            result = await run_workflow(engine, case["scope"], "wf_script_execution", args)
            assert result["status"] == "WAITING"
            await service.drain_jobs()
        with pytest.raises(ContractError, match="REVALIDATION_BUDGET_EXHAUSTED"):
            await run_workflow(engine, case["scope"], "wf_script_execution", args)
    final = await service.query("read_job", {"job_id": job_id})
    item = final["body"]["items"][0]
    assert final["body"]["status"] == "failed"
    assert len(item["archive_revalidations"]) == 3
    assert item["attempt"] == 6
    assert item["generation_attempts"] == 3
    assert item["local_revalidation_attempts"] == 3
    assert item["provider_attempts"] == original_attempts
    assert all(x["status"] == "rejected" for x in item["archive_revalidations"])
    local_ops = [op for op in engine.store.list("operation") if op["key"] == "retry_failed_items"
        and op["args"].get("recovery_mode") == "revalidate_archived"]
    assert len(local_ops) == 3 and all(op["status"] == "committed" for op in local_ops)
    assert case["archived_calls"] == ["selling_points", "selling_points"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_counters", [False, True])
async def test_local_validation_claim_does_not_consume_remaining_paid_retry_budget(env, legacy_counters):
    engine, service = env
    case = await archived_selected_case(engine, service, paid_attempts=2)
    job_id = case["job"]["body"]["id"]
    if legacy_counters:
        # Only remove the newly introduced counters to simulate an old-schema
        # persisted job. Do not alter its real run/grant/receipt/attempt archive.
        with service.store.transaction(write=True) as c:
            saved = service.store.get(c, "prelive_job", job_id)
            body = deepcopy(saved["body"])
            body["items"][0].pop("generation_attempts")
            body["items"][0].pop("local_revalidation_attempts")
            service.store.put(c, "prelive_job", job_id, body, expected=saved["ref"]["revision"])
    with patch.object(service.adapter, "generate_module", side_effect=AssertionError("NO NEW PROVIDER CALL")), \
            patch.object(module_generation, "finish_received_module", side_effect=BusinessError("MODULE_RESPONSE_INVALID", "本地校验仍拒绝")):
        await run_workflow(engine, case["scope"], "wf_script_execution", {
            "action": "retry", "job_id": job_id, "recovery_mode": "revalidate_archived"})
        await service.drain_jobs()
    current = await service.query("read_job", {"job_id": job_id})
    assert current["body"]["items"][0]["attempt"] == 3
    assert current["body"]["items"][0]["generation_attempts"] == 2
    assert current["body"]["items"][0]["local_revalidation_attempts"] == 1
    assert len(current["body"]["items"][0]["provider_attempts"]) == 2
    # This is the third explicit fake provider execution, not a fourth paid one.
    retry = await engine.business_command(case["scope"], "retry_failed_items", {"job_id": job_id}, "r12-third-paid-after-local")
    assert retry["data"]["recovery_mode"] == "retry"
    await service.drain_jobs()
    final = await service.query("read_job", {"job_id": job_id})
    assert len(final["body"]["items"][0]["provider_attempts"]) == 3
    assert final["body"]["items"][0]["generation_attempts"] == 3
    assert final["body"]["items"][0]["local_revalidation_attempts"] == 1
    assert case["archived_calls"] == ["selling_points", "selling_points"]
