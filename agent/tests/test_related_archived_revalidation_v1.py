"""Explicit follow-up local archive recovery, fresh grants; no supplier calls."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from test_runtime_business import env
from shengbo_contracts.common import ContractError, MutationContext
from shengbo_prelive.models import IntentAssessment, MessageRequest, RunScope
from shengbo_prelive.workflows import run_workflow
from test_runtime_archived_revalidation_r12 import archived_selected_case


async def followup_case(engine, service, *, mode="follow_up", modules=None):
    case = await archived_selected_case(engine, service, paid_attempts=2)
    owner=engine.store.get("run",case["run"]["id"])
    await engine.client.command("revoke_grant", {"run_id":owner["id"],"grant_id":owner["grant_id"],
        "expected_write_generation":owner["write_generation"],"reason":"isolated owner closed"},
        MutationContext(operation_id=owner["id"]+"-test-close"),controller=True)
    engine.store.close_run(owner["id"],"BLOCKED",{"cause":"isolated owner closed after real grant revocation"})
    text="继续本场原任务，修改商品"+owner["target_product_ids"][0]+"的话术：只在已有归档上本地重验并保存，不重新请求供应商。"
    result=await engine.submit_message(owner["conversation_id"],MessageRequest(text=text,intent_mode=mode,
        related_run_id=owner["id"] if mode=="follow_up" else None,client_request_id="local-followup-test"))
    run=engine.store.get("run",result["run"]["id"])
    intent=IntentAssessment(objective="script_change",summary=text,authorization_quote=text,
        target_product_ids=owner["target_product_ids"],script_mode="edit",requested_modules=modules or [])
    run=engine.store.update("run",run["id"],intent_assessment=intent.model_dump())
    await engine._freeze_goal(run)
    run=engine.store.get("run",run["id"])
    assert run["phase"]=="execute",[{k:q.get(k) for k in ("kind","prompt","preview")}for q in engine.store.list("question")if q["run_id"]==run["id"]]
    case.update(owner=owner,followup=run,
        followup_scope=RunScope(run["id"],"followup-local-recovery","execute"))
    return case


@pytest.mark.asyncio
async def test_followup_restores_rejected_archive_with_new_grant_and_unchanged_owner_attempts(env):
    engine,service=env
    case=await followup_case(engine,service)
    job_id=case["job"]["body"]["id"]
    attempts=deepcopy(case["job"]["body"]["items"][0]["provider_attempts"])
    with patch.object(service.adapter,"generate_module",side_effect=AssertionError("NO NEW SUPPLIER CALL")):
        result=await run_workflow(engine,case["followup_scope"],"wf_script_execution",{
            "action":"retry","job_id":job_id,"recovery_mode":"revalidate_archived"})
        assert result["status"]=="WAITING",result
        await service.drain_jobs()
    final=await service.query("read_job",{"job_id":job_id})
    assert final["body"]["status"]=="succeeded",final
    assert final["body"]["run_id"]==case["owner"]["id"]
    assert final["body"]["items"][0]["provider_attempts"]==attempts
    assert final["body"]["items"][0]["archive_revalidations"][-1]["requesting_run_id"]==case["followup"]["id"]
    assert "archive_revalidation_execution" not in final["body"]
    observed=await service.query("read_completion_observation",{"preparation_id":case["prep"]["id"]})
    assert all("archive_revalidation_execution" not in row["body"] for row in observed["jobs"])
    operation=engine.store.get("operation",result["result"]["operation_id"])
    assert operation["run_id"]==case["followup"]["id"]
    assert operation["controller_archived_revalidation"]["original_run_id"]==case["owner"]["id"]
    assert operation["args"]=={"job_id":job_id,"recovery_mode":"revalidate_archived"}
    with service.store.transaction() as c:
        raw=service.store.get(c,"prelive_job",job_id)["body"]
        assert raw["principal"]["run_id"]==case["owner"]["id"]
        assert raw["archive_revalidation_execution"]["principal"]["run_id"]==case["followup"]["id"]
        assert raw["archive_revalidation_execution"]["mutation"]["grant_id"]==case["followup"]["grant_id"]
        old=c.execute("SELECT status FROM prelive_grants WHERE grant_id=?",(case["owner"]["grant_id"],)).fetchone()
        assert old["status"]=="revoked"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad",["new_task","paid_retry","wrong_parent","bad_receipt","wrong_modules","wrong_product","unknown"])
async def test_unrelated_or_broadened_recovery_rejected_before_controller_write(env,bad):
    engine,service=env
    case=await followup_case(engine,service,mode="new_task" if bad=="new_task" else "follow_up",
        modules=["cta"] if bad=="wrong_modules" else None)
    run=case["followup"];scope=case["followup_scope"];job_id=case["job"]["body"]["id"]
    if bad=="wrong_parent":
        engine.store.update("run",run["id"],input={**run["input"],"related_run_id":"untrusted-other-run"})
    elif bad=="bad_receipt":
        original=next(o for o in engine.store.list("operation") if o.get("run_id")==case["owner"]["id"] and o["key"]=="submit_script_change")
        engine.store.update("operation",original["id"],request_hash="sha256:"+"0"*64)
    elif bad=="wrong_product":
        engine.store.update("run",run["id"],target_product_ids=["different-product"],action_target_refs=[])
    elif bad=="unknown":
        with service.store.transaction(write=True) as c:
            saved=service.store.get(c,"prelive_job",job_id);body=deepcopy(saved["body"])
            body["items"][0]["call_state"]="timeout_unknown"
            service.store.put(c,"prelive_job",job_id,body,expected=saved["ref"]["revision"])
    args={"job_id":job_id,"recovery_mode":"retry" if bad=="paid_retry" else "revalidate_archived"}
    before=deepcopy(engine.store.list("operation"))
    with patch.object(engine.client,"command",side_effect=AssertionError("NO WRITE")):
        with pytest.raises(ContractError):
            await engine.business_command(scope,"retry_failed_items",args,"rejected-related-retry")
    assert engine.store.list("operation")==before


@pytest.mark.asyncio
@pytest.mark.parametrize("bad",["no_new_grant","revoked_new_grant","paid_retry","wrong_module"])
async def test_backend_controller_recovery_still_requires_current_narrow_grant(env,bad):
    engine,service=env
    case=await followup_case(engine,service,modules=["cta"] if bad=="wrong_module" else None)
    current=case["followup"];job_id=case["job"]["body"]["id"]
    if bad=="revoked_new_grant":
        await engine.client.command("revoke_grant",{"run_id":current["id"],"grant_id":current["grant_id"],
            "expected_write_generation":current["write_generation"],"reason":"test fence"},
            MutationContext(operation_id="revoke-child-grant"),controller=True)
    mutation=MutationContext(operation_id="direct-controller-recovery",grant_id=None if bad=="no_new_grant" else current["grant_id"],
        expected_write_generation=current["write_generation"])
    before=await service.query("read_job",{"job_id":job_id})
    with pytest.raises(ContractError):
        await engine.client.command("retry_failed_items",{"job_id":job_id,
            "recovery_mode":"retry" if bad=="paid_retry" else "revalidate_archived"},
            mutation,scope=current["scope"],run_id=current["id"],controller=True)
    assert await service.query("read_job",{"job_id":job_id})==before


@pytest.mark.asyncio
async def test_current_grant_revocation_fences_worker_without_resurrecting_original_grant(env):
    engine,service=env
    case=await followup_case(engine,service);current=case["followup"];job_id=case["job"]["body"]["id"]
    await engine.business_command(case["followup_scope"],"retry_failed_items",{
        "job_id":job_id,"recovery_mode":"revalidate_archived"},"enqueue-before-revoke")
    await engine.client.command("revoke_grant",{"run_id":current["id"],"grant_id":current["grant_id"],
        "expected_write_generation":current["write_generation"],"reason":"test revocation after queue"},
        MutationContext(operation_id="revoke-child-after-enqueue"),controller=True)
    with patch.object(service.adapter,"generate_module",side_effect=AssertionError("NO NEW SUPPLIER CALL")):
        await service.drain_jobs()
    final=await service.query("read_job",{"job_id":job_id})
    assert final["body"]["status"]!="succeeded"
    assert final["body"]["items"][0]["error"]["code"]=="GRANT_REVOKED"


@pytest.mark.asyncio
async def test_controller_revalidation_unknown_receipt_reconciles_same_operation(env):
    from shengbo_prelive.client import RemoteOutcomeUnknown
    engine,service=env
    case=await followup_case(engine,service);run=case["followup"];job_id=case["job"]["body"]["id"]
    original_command=engine.client.command
    async def lost_receipt(key,args,mutation,**kwargs):
        await original_command(key,args,mutation,**kwargs)
        raise RemoteOutcomeUnknown(mutation.operation_id)
    with patch.object(engine.client,"command",side_effect=lost_receipt):
        with pytest.raises(RemoteOutcomeUnknown):
            await engine.business_command(case["followup_scope"],"retry_failed_items",{
                "job_id":job_id,"recovery_mode":"revalidate_archived"},"lost-controller-receipt")
    current=engine.store.get("run",run["id"])
    operation_id=current["pending_unknown_operation"]
    assert await engine._reconcile_pending_operation(current)
    assert engine.store.get("operation",operation_id)["status"]=="committed"
    assert not engine.store.get("run",run["id"]).get("pending_unknown_operation")
    await service.drain_jobs()
    assert (await service.query("read_job",{"job_id":job_id}))["body"]["status"]=="succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry",["runtime","controller"])
async def test_disabling_populated_unselected_module_is_not_a_noop_exemption(env,entry):
    engine,service=env
    case=await followup_case(engine,service,modules=["selling_points"])
    current=case["followup"];job_id=case["job"]["body"]["id"]
    # Isolated hostile historical plan projection: a disable of a previously
    # populated opening must count as a change outside the new narrow grant.
    with service.store.transaction(write=True) as c:
        saved=service.store.get(c,"prelive_job",job_id);body=deepcopy(saved["body"])
        opening=next(a for a in body["items"][0]["input"]["modules"] if a["key"]=="opening_long")
        opening["action"]="disable"
        service.store.put(c,"prelive_job",job_id,body,expected=saved["ref"]["revision"])
    if entry=="runtime":
        with pytest.raises(ContractError,match="MODULE_SCOPE_DENIED"):
            await engine.business_command(case["followup_scope"],"retry_failed_items",{
                "job_id":job_id,"recovery_mode":"revalidate_archived"},"deny-unselected-disable")
    else:
        with pytest.raises(ContractError,match="SCOPE_CONFLICT"):
            await engine.client.command("retry_failed_items",{"job_id":job_id,"recovery_mode":"revalidate_archived"},
                MutationContext(operation_id="deny-unselected-controller-disable",grant_id=current["grant_id"],
                    expected_write_generation=current["write_generation"]),
                scope=current["scope"],run_id=current["id"],controller=True)
