"""An optional transfer cannot bypass the existing no-commit correction fence."""
import json
import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from shengbo_contracts.business import COMMAND_ARGS
from shengbo_contracts.common import ArtifactRef, ContractError, canonical_hash
from shengbo_prelive.middleware import ActionBoundary
from shengbo_prelive.product_materials import SaveAcceptedProductInput
from shengbo_prelive.workflows import MaterialsFlowInput
from test_middleware import FakeEngine, tool_request
from test_runtime_business import env
from test_product_materials_exact_candidate import case as accepted_candidate
from test_selection_dependency_recovery_v1 import ScriptedModel
from langchain_core.messages import AIMessage
from test_product_assembly_recovery_rc8 import (
    CODE, SECRET, args_for_correction, attested, safe_details,
)


def candidate_case():
    engine = FakeEngine()
    engine.store.data[("run", "r1")]["scope"] = {"kind": "catalog", "account_id": "local"}
    helper = SaveAcceptedProductInput(
        branch_task_id="branch_verified",
        accepted_report_ref=ArtifactRef(kind="analysis", id="analysis_verified", revision=1,
                                       content_hash="sha256:" + "b" * 64),
    ).model_dump(mode="json", exclude_none=True)
    request = tool_request("wf_product_materials", args={"action": "save_from_facts", "parameters": helper})
    data = MaterialsFlowInput.model_validate(request.tool_call["args"]).model_dump(mode="json", exclude_none=True)
    flow = "flow_" + canonical_hash(["r1", "wf_product_materials", data])[7:39]
    command = COMMAND_ARGS["save_product"].model_validate(args_for_correction()).model_dump(mode="json", exclude_none=True)
    resolution = {"id": flow, "run_id": "r1", "scope_hash": canonical_hash(engine.store.get("run", "r1")["scope"]),
                  "input_hash": canonical_hash(helper), "command_args": command, "command_hash": canonical_hash(command)}
    engine.store.data[("product_candidate_save_resolution", flow)] = resolution
    identity = "op_" + canonical_hash(["r1", flow, "save_product"])[7:39]
    operation = {"id": identity, "run_id": "r1", "key": "save_product", "status": "known_failed",
                 "error_code": CODE, "args": command, "request_hash": canonical_hash(command)}
    engine.store.data[("operation", identity)] = operation
    error = ContractError(CODE, SECRET, status=422, details=safe_details())
    error.known_operation_id, error.known_operation_input_hash = identity, canonical_hash(command)
    return engine, request, error, resolution, operation


@pytest.mark.asyncio
async def test_attested_candidate_save_uses_same_budget_as_original_entrypoints():
    engine, candidate, error, _, _ = candidate_case()
    requests = [tool_request("save_product", args=args_for_correction()),
                tool_request("wf_product_materials", args={"action": "save", "parameters": args_for_correction()}),
                candidate]
    for number, request in enumerate(requests, 1):
        failure = attested(engine, request)[0] if number < 3 else error
        async def reject(_request, failure=failure):
            raise failure
        output = await ActionBoundary(engine).awrap_tool_call(request, reject)
        body = json.loads(output.content)
        assert body["correction_count"] == number and output.status == "error"
        assert body["recovery"]["commit_state"] == "not_committed"
        assert body["recovery"]["automatic_action_taken"] is False
        assert SECRET not in output.content
    async def reject(_request):
        raise error
    with pytest.raises(ContractError, match=CODE):
        await ActionBoundary(engine).awrap_tool_call(candidate, reject)
    assert engine.store.get("run", "r1")["correctable_tool_errors"] == {CODE: 3}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "missing_resolution", "wrong_resolution_run", "wrong_scope_hash", "wrong_input_hash",
    "changed_helper_input", "wrong_command_hash", "changed_command_fields", "missing_operation",
    "wrong_operation_run", "wrong_operation_key", "sent_operation", "operation_hash_mismatch",
    "wrong_attested_id", "wrong_attested_input", "no_attestation", "pending_unknown",
    "child", "intent", "permission_denied", "wrong_status",
])
async def test_candidate_feedback_requires_local_resolution_and_real_known_failure(case):
    engine, request, error, resolution, operation = candidate_case()
    if case == "missing_resolution":
        engine.store.data.pop(("product_candidate_save_resolution", resolution["id"]))
    elif case == "wrong_resolution_run":
        resolution["run_id"] = "foreign"
    elif case == "wrong_scope_hash":
        resolution["scope_hash"] = canonical_hash("foreign")
    elif case == "wrong_input_hash":
        resolution["input_hash"] = canonical_hash("foreign")
    elif case == "changed_helper_input":
        request.tool_call["args"]["parameters"]["branch_task_id"] = "foreign"
    elif case == "wrong_command_hash":
        resolution["command_hash"] = canonical_hash("foreign")
    elif case == "changed_command_fields":
        resolution["command_args"] = {**resolution["command_args"], "fields": {"name": "different"}}
        resolution["command_hash"] = canonical_hash(resolution["command_args"])
    elif case == "missing_operation":
        engine.store.data.pop(("operation", operation["id"]))
    elif case == "wrong_operation_run":
        operation["run_id"] = "foreign"
    elif case == "wrong_operation_key":
        operation["key"] = "publish_preparation"
    elif case == "sent_operation":
        operation["status"] = "sent"
    elif case == "operation_hash_mismatch":
        operation["request_hash"] = canonical_hash("foreign")
    elif case == "wrong_attested_id":
        error.known_operation_id = "op_foreign"
    elif case == "wrong_attested_input":
        error.known_operation_input_hash = canonical_hash("foreign")
    elif case == "no_attestation":
        del error.known_operation_id
    elif case == "pending_unknown":
        engine.store.data[("run", "r1")]["pending_unknown_operation"] = "unresolved"
    elif case in {"child", "intent"}:
        request.runtime.context = replace(request.runtime.context, **({"role": "product_facts"} if case == "child" else {"phase": "intent"}))
    elif case == "permission_denied":
        error = ContractError(CODE, "denied", status=403, details=safe_details())
    elif case == "wrong_status":
        error.status = 503
    async def reject(_request):
        raise error
    with pytest.raises(ContractError):
        await ActionBoundary(engine).awrap_tool_call(request, reject)
    assert not engine.store.get("run", "r1").get("correctable_tool_errors")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_first", [False, True])
async def test_native_deep_agent_selects_optional_transfer_and_explicitly_corrects(env, invalid_first):
    engine, service = env
    run, scope, _, branch, reference = await accepted_candidate(engine, service,
        {"name": "原生工具流事实候选", "price": [129] if invalid_first else 129})
    original_goal = deepcopy(run["goal"])
    original_effects = deepcopy(run["allowed_effects"])
    await engine.start()
    engine.scheduler.cancel()
    await asyncio.gather(engine.scheduler, return_exceptions=True)
    stages = []
    arguments = {"branch_task_id": branch, "accepted_report_ref": reference}
    state = {}

    async def logic(messages):
        stage = len(stages)
        stages.append(stage)
        if stage == 0:
            args = {"action": "save_from_facts", "parameters": deepcopy(arguments)}
            return AIMessage(content="", tool_calls=[{"id": "native-candidate-1", "name": "wf_product_materials", "args": args}])
        value = json.loads(messages[-1].content)
        if stage == 1 and invalid_first:
            assert messages[-1].status == "error" and value["error"]["code"] == CODE
            assert value["correction_count"] == 1 and value["recovery"]["automatic_action_taken"] is False
            assert not engine.store.get("run", run["id"]).get("created_product_ids")
            args = {"action": "save_from_facts", "parameters": {**arguments,
                "overrides": [{"path": "/price", "op": "set", "value": 129}]}}
            return AIMessage(content="", tool_calls=[{"id": "native-candidate-2", "name": "wf_product_materials", "args": args}])
        assert stage == (2 if invalid_first else 1)
        assert value["status"] == "COMPLETED"
        state["ref"] = value["result"]["data"]["product_ref"]
        return AIMessage(content="", tool_calls=[{"id": "native-candidate-finish", "name": "finish_turn",
            "args": {"outcome": "BLOCKED", "summary": "离线保存检查点；未完成全场，不生成或批准语音。"}}])

    engine.model = ScriptedModel(logic)
    engine.main = engine._build_graph("main")
    await engine._invoke(run)
    current = engine.store.get("run", run["id"])
    assert current["goal"] == original_goal and current["allowed_effects"] == original_effects
    assert not current.get("pending_unknown_operation")
    assert current.get("correctable_tool_errors", {}) == ({CODE: 1} if invalid_first else {})
    operations = [o for o in engine.store.list("operation") if o["key"] == "save_product"]
    assert sorted(o["status"] for o in operations) == (["committed", "known_failed"] if invalid_first else ["committed"])
    saved = await service.query("read_product", {"ref": state["ref"]})
    assert saved["body"]["price"] == 129 and saved["body"]["name"] == "原生工具流事实候选"
    preparation = await service.query("read_preparation", {"preparation_id": run["scope"]["preparation_id"]})
    assert preparation["body"]["selected_product_refs"] == [] and preparation["body"]["script_refs"] == []
    assert preparation["body"]["approval_ref"] is None and not engine.store.list("question")
