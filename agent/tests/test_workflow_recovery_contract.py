import pytest
from pydantic import ValidationError

from shengbo_contracts.common import ContractError
from shengbo_contracts.workflow_recovery import WorkflowRecoveryReceipt, WorkflowStepProgress
from shengbo_prelive.fixed_step_recovery import fixed_step_recovery_call_allowance
from shengbo_prelive.models import RunScope
from test_runtime_business import env, new_run


async def test_single_safe_recovery_strategy_is_persisted_without_model_request(env, monkeypatch):
    engine, _ = env
    run = await new_run(engine, "核对当前商品资料")
    engine.store.update("run", run["id"], phase="execute", status="RUNNING")
    scope = RunScope(run["id"], "fixture-recovery", "execute")
    failure = {"workflow": "catalog_intake", "step": "extract", "code": "SOURCE_LAYOUT_UNAVAILABLE",
               "known_no_commit": True, "disposition": "evidence_repair"}
    choices = {"reread_source": "按原文定位重新读取"}

    async def no_model(*args, **kwargs):
        pytest.fail("The only safe strategy must not ask the model to choose")

    monkeypatch.setattr("shengbo_prelive.fixed_step_recovery._choose", no_model)
    for _ in range(2):
        assert await engine.recover_fixed_step(scope, failure, strategies=choices,
                                               automatic_if_single=True) == "reread_source"
    saved = engine.store.get("run", run["id"])
    receipt = WorkflowRecoveryReceipt.model_validate(saved["fixed_step_recovery"]["receipt"])
    assert (receipt.selected_by, receipt.model_requests, receipt.disposition) == ("program", 0, "evidence_repair")
    assert fixed_step_recovery_call_allowance(saved, "catalog_intake") == 1
    events = [event for event in engine.events(run["id"])["events"]
              if event["type"] == "fixed_step_recovery.selected"]
    assert len(events) == 1 and events[0]["data"]["recovery"] == saved["fixed_step_recovery"]["receipt"]

    engine.store.update("run", run["id"], pending_unknown_operation="fixture-write")
    with pytest.raises(ContractError, match="FIXED_STEP_RECOVERY_DENIED"):
        await engine.recover_fixed_step(scope, failure, strategies=choices, automatic_if_single=True)


def test_progress_cannot_attach_an_unrelated_or_overcounted_receipt():
    receipt = WorkflowRecoveryReceipt(workflow="catalog_intake", step_id="extract", strategy="link_currency",
        status="succeeded", selected_by="program", model_requests=0, disposition="evidence_repair")
    with pytest.raises(ValidationError, match="must match"):
        WorkflowStepProgress(workflow="script_execution", step_id="extract", title="正在核对",
                             status="RECOVERING", recovery=receipt)
    with pytest.raises(ValidationError, match="cannot exceed"):
        WorkflowStepProgress(workflow="catalog_intake", step_id="extract", title="正在核对",
                             status="RECOVERING", recovery=receipt, completed_count=2, total_count=1)
