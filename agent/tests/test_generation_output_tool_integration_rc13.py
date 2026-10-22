"""RC13 stable-reader wiring over the real native SDK, with offline providers.

The helper's exact-content/association cases live in its own 39-case suite.
These tests target registration, disclosure, policy and M5 composition instead.
"""
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.middleware import ActionBoundary, Audit, ContextProjection, RunFence, ToolPolicy
from shengbo_prelive.models import RunScope
from shengbo_prelive.policy import EXECUTE_READ_TOOLS, INTENT_TOOLS, READ_TOOLS, check_tool_allowed
from shengbo_prelive.runtime import CURRENT_SCOPE
from shengbo_prelive.tool_presentation import CONTROL_TOOLS, CORE_TOOLS, expanded_names
from shengbo_prelive.tools import build_tools
from test_context_window_budget import setup
from test_generation_output_reads_rc13 import PRIVATE, ReadOnlyJobClient
from test_middleware import tool_request
from test_recovery_workset_rc11 import make_job


READER = "read_generation_output"


def prepare(engine, run, *, unknown=False):
    engine.tool_presentation_enabled = True
    engine.main_tool_names.update(CONTROL_TOOLS)
    job = make_job(keys=("opening_long",), failed="cta")
    job["body"].update(run_id=run["id"], preparation_id=run["scope"]["preparation_id"],
        context={"private_unselected": PRIVATE})
    if unknown:
        last = deepcopy(job["body"]["items"][0]["provider_attempts"][-1])
        last.update(attempt_id="last-unknown-cta", state="outcome_unknown", validation_state="unknown")
        job["body"]["items"][0]["provider_attempts"].append(last)
    job["ref"]["content_hash"] = canonical_hash(job["body"])
    client = ReadOnlyJobClient(job)
    engine.client = client
    return client, RunScope(run["id"], "stable-reader-main", "execute")


def call(name, args, call_id):
    return {"type": "function_call", "namespace": "functions", "name": name,
        "call_id": call_id, "arguments": json.dumps(args, ensure_ascii=False)}


def schemas(payload):
    return {tool["name"] for namespace in payload.get("tools", []) for tool in namespace["tools"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("view,unknown,expected", [
    ("accepted_module", False, "AVAILABLE"),
    ("latest_failed_response", False, "AVAILABLE"),
    ("latest_failed_response", True, "OUTCOME_UNKNOWN"),
])
async def test_native_activation_then_stable_read_is_read_only_and_preserves_unknown(setup, view, unknown, expected):
    engine, _, run, sent, _ = setup
    client, scope = prepare(engine, run, unknown=unknown)
    # A pending unknown business outcome stays recorded. Read-only diagnostics
    # remain available; no business retry or reconciliation is performed here.
    engine.store.update("run", run["id"], pending_unknown_operation="unknown-audio")
    original_operations = deepcopy(engine.store.list("operation"))
    original_questions = deepcopy(engine.store.list("question"))
    key = "opening_long" if view == "accepted_module" else "cta"
    args = {"job_id": client.job["ref"]["id"], "item_id": "item_recovery", "module_key": key, "view": view}
    engine.test_response_tool_calls = [[call("activate_tools", {"names": [READER]}, "disclose-stable-reader")],
        [call(READER, args, "read-stable-generation")]]
    # M5 must classify the new reader as read-only rather than taking the write
    # lock or requiring a side-effect audit gate. Original assert_scope remains.
    def no_write(*_):
        raise AssertionError("read_generation_output reached a write-only boundary")
    with patch("shengbo_prelive.runtime.main_middleware", lambda e: [RunFence(e), ContextProjection(e),
            ToolPolicy(e), ActionBoundary(e), Audit(e)]), \
            patch.object(engine, "action_lock", no_write), \
            patch.object(engine.trace, "assert_ready_for_side_effect", no_write):
        graph = engine._build_graph("main")
        token = CURRENT_SCOPE.set(scope)
        try:
            result = await graph.ainvoke({"messages": [HumanMessage("自主展开稳定读取工具，只读核对指定模块。")],
                "files": engine._initial_files("main")},
                {"configurable": {"thread_id": "stable-reader-" + view + str(unknown)}, "recursion_limit": 15}, context=scope)
        finally:
            CURRENT_SCOPE.reset(token)
    normal = [value for value in sent if value.get("tools")]
    assert len(normal) == 3
    assert READER not in schemas(normal[0]) and "activate_tools" in schemas(normal[0])
    assert READER in schemas(normal[1]) and READER in schemas(normal[2])
    assert READER in expanded_names(engine, scope)
    outputs = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "read-stable-generation"]
    assert len(outputs) == 1 and outputs[0].status == "success"
    content = json.loads(outputs[0].content)
    assert content["status"] == expected
    assert content["approval_implied"] is False and content["automatic_action_taken"] is False
    assert content["can_execute_or_approve"] is False and content["is_formal_module_body"] is False
    assert PRIVATE not in outputs[0].content
    assert client.queries == [("read_job", {"job_id": args["job_id"]})]
    assert not client.commands
    assert engine.store.list("operation") == original_operations
    assert engine.store.list("question") == original_questions
    assert engine.store.get("run", run["id"])["pending_unknown_operation"] == "unknown-audio"
    assert not engine.store.get("run", run["id"]).get("correctable_tool_errors")
    wire_outputs = [entry for entry in normal[-1]["input"] if entry.get("type") == "function_call_output"
        and entry.get("call_id") == "read-stable-generation"]
    assert len(wire_outputs) == 1 and json.loads(wire_outputs[0]["output"])["status"] == expected
    if unknown:
        assert content["content"] is None and content["reason_code"] == "GENERATION_OUTCOME_UNKNOWN"
    else:
        assert content["content_complete"] is True and content["content"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("role,phase", [("main", "intent"), ("product_facts", "execute"),
    ("session_config", "execute"), ("audience_scenes", "execute")])
async def test_registration_does_not_grant_intent_or_child_execution(setup, role, phase):
    engine, _, run, _, _ = setup
    client, main = prepare(engine, run)
    restricted = replace(main, role=role, phase=phase, branch_task_id="restricted-child" if role != "main" else None)
    assert READER in EXECUTE_READ_TOOLS and READER not in READ_TOOLS and READER not in INTENT_TOOLS
    assert READER not in CORE_TOOLS
    assert READER in {tool.name for tool in build_tools(engine, "main")}
    if role != "main":
        assert READER not in {tool.name for tool in build_tools(engine, role)}
    assert READER not in engine.allowed_tool_names(restricted)
    with pytest.raises(ContractError, match="TOOL_SCOPE_DENIED"):
        check_tool_allowed(restricted, run, READER)
    request = tool_request(READER)
    request.runtime.context = restricted
    reached = False
    async def handler(_):
        nonlocal reached
        reached = True
    with pytest.raises(ContractError, match="TOOL_SCOPE_DENIED"):
        await ToolPolicy(engine).awrap_tool_call(request, handler)
    assert not reached and not client.queries and not client.commands


@pytest.mark.asyncio
async def test_native_read_still_obeys_current_run_fence_before_business_query(setup):
    engine, _, run, _, _ = setup
    client, scope = prepare(engine, run)
    engine.store.update("run", run["id"], status="CANCELLING")
    request = tool_request(READER)
    request.runtime.context = scope
    invoked = False
    async def handler(_):
        nonlocal invoked
        invoked = True
    with pytest.raises(ContractError, match="RUN_FENCED"):
        await ActionBoundary(engine).awrap_tool_call(request, handler)
    assert not invoked and not client.queries and not client.commands


@pytest.mark.asyncio
async def test_m5_does_not_convert_reader_authorization_errors_to_correction(setup):
    engine, _, run, _, _ = setup
    client, scope = prepare(engine, run)
    request = tool_request(READER)
    request.runtime.context = scope
    async def denied(_):
        raise ContractError("GENERATION_OUTPUT_READ_DENIED", "scoped read denied", status=403)
    with pytest.raises(ContractError, match="GENERATION_OUTPUT_READ_DENIED"):
        await ActionBoundary(engine).awrap_tool_call(request, denied)
    assert not engine.store.get("run", run["id"]).get("correctable_tool_errors")
    assert not client.queries and not client.commands
