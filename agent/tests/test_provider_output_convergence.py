"""Real SDK/provider rounds converge using exact outputs of a large durable job."""
from collections import Counter
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import tiktoken
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from shengbo_contracts.business import COMMAND_ARGS, MODULE_KEYS
from shengbo_contracts.common import ContractError, WorkScope, canonical_hash, canonical_json
from shengbo_prelive.middleware import ActionBoundary, Audit, ContextProjection, ToolPolicy
from shengbo_prelive.models import RunScope
from shengbo_prelive.policy import EFFECTS
from shengbo_prelive.providers.chatgpt import build_chatgpt_model
from shengbo_prelive.runtime import CURRENT_SCOPE, GRAPH_RECURSION_LIMIT, RuntimeEngine
from shengbo_prelive.store import MODEL_CALL_LIMIT
from shengbo_prelive.tools import COMMAND_MAP


ENCODING = tiktoken.get_encoding("o200k_base")
MODULES = ("opening_long", "opening_short", "product_intro")


def tokens(value):
    return len(ENCODING.encode(canonical_json(value)))


def ref(kind, identity, value, revision=1):
    return {"kind": kind, "id": identity, "revision": revision, "content_hash": canonical_hash(value)}


def call(name, identity, args):
    return {"type": "function_call", "namespace": "functions", "name": name,
            "call_id": identity, "arguments": json.dumps(args, ensure_ascii=False)}


def final():
    return [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "测试完成"}]}]


def tool_outputs(payload):
    return {item["call_id"]: json.loads(item["output"]) for item in payload["input"]
            if item.get("type") == "function_call_output"}


class OfflineCredentials:
    calls = 0

    def get_access_token(self):
        self.calls += 1
        return "fake-provider-output-convergence-credential"


class OfflineBusiness:
    def __init__(self, job):
        self.job = job
        self.queries = []
        self.commands = []

    async def query(self, key, args=None, **kwargs):
        self.queries.append((key, deepcopy(args), deepcopy(kwargs)))
        if key == "read_preparation":
            return deepcopy(self.preparation)
        if key == "read_completion_observation":
            return {"pending_job_ids": [self.pending_job["ref"]["id"]], "unknown_job_ids": [],
                    "candidate_ready": False, "jobs": [deepcopy(self.pending_job)],
                    "preparation": deepcopy(self.preparation)}
        assert key == "read_job", "this isolated transport never invokes a real business service"
        assert args["job_id"] == self.job["ref"]["id"]
        assert kwargs["run_id"] == self.job["body"]["run_id"]
        return deepcopy(self.job)

    async def command(self, key, args, mutation, **kwargs):
        assert key == "save_script_plan"
        # Exercise the actual shared business DTO and the Runtime operation ID.
        assert COMMAND_ARGS[key].model_validate(args).model_dump(mode="json", exclude_none=True) == args
        assert mutation.operation_id.startswith("op_")
        assert mutation.grant_id == "grant-convergence" and mutation.expected_write_generation == 7
        assert kwargs["run_id"] == self.job["body"]["run_id"]
        assert kwargs["scope"]["preparation_id"] == args["preparation_id"]
        receipt = {"command": key, "operation_id": mutation.operation_id, "status": "committed", "data": {
            "script_plan_ref": ref("script_plan", "plan-converged", args),
            "preparation_ref": ref("preparation", args["preparation_id"], {"plan": args}, revision=2)}}
        self.commands.append({"key": key, "args": deepcopy(args), "mutation": mutation,
                              "receipt": deepcopy(receipt), "kwargs": deepcopy(kwargs)})
        return receipt

    async def close(self):
        pass


@pytest.fixture
async def convergence(tmp_path):
    context = {"body": "public_context " * 50000, "tail": "FULL_CONTEXT_ONLY"}
    assert tokens(context) >= 50000
    outputs = {module: {"sentences": [{"text": module + "的供应商原始结果，仅作为本次计划复核的证据。", "source_refs": []}]}
               for module in MODULES}
    assert all(tokens(output) < 1000 for output in outputs.values())
    run_id, prep_id = "run-output-convergence", "prep-output-convergence"
    body = {"id": "job-output-convergence", "kind": "scripts", "status": "cancelled",
        "run_id": run_id, "preparation_id": prep_id, "generation_epoch": 3, "cancel_requested": True,
        "context": context, "context_ref": ref("context", "context-convergence", context), "items": []}
    for module, output in outputs.items():
        input_value = {"public_input": "input " * 10000, "module": module}
        body["items"].append({"item_id": "item-" + module, "status": "failed", "output": None,
            "call_state": "result_received", "attempt": 1, "provider_attempts": [{
                "attempt_id": "attempt-" + module, "state": "result_received", "purpose": "copywriting." + module,
                "input": input_value, "output": output, "input_hash": canonical_hash(input_value),
                "output_hash": canonical_hash(output), "received_at": "2026-10-17T00:00:00.123456+00:00",
                "usage": {"input_tokens": 50000, "output_tokens": 100, "total_tokens": 50100}}]})
    job = {"ref": ref("prelive_job", body["id"], body, revision=27), "body": body}
    client = OfflineBusiness(job)
    engine = RuntimeEngine(tmp_path, client, context_input_token_limit=48000)
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    work_scope = WorkScope(kind="preparation", preparation_id=prep_id).model_dump()
    effects = sorted(EFFECTS["prepare_session"])
    product_ref = ref("product", "product-convergence", {"name": "测试商品"})
    # This offline fixture manually freezes a real write goal. Reading a
    # product-shaped context is not write authorization in the production gate.
    goal = {"objective": "prepare_session", "scope": work_scope, "allowed_effects": effects,
        "target_product_ids": [product_ref["id"]], "action_target_refs": [product_ref], "context_product_refs": [product_ref]}
    run = engine.store.put("run", {"id": run_id, "conversation_id": conversation["id"], "scope": work_scope,
        "status": "RUNNING", "phase": "execute", "input": {"text": "继续当前任务。", "material_refs": []},
        "goal": goal, "frozen_goal_hash": canonical_hash(goal), "allowed_effects": effects,
        "target_product_ids": goal["target_product_ids"], "action_target_refs": [product_ref], "context_product_refs": [product_ref],
        "grant_id": "grant-convergence", "write_generation": 7,
        "allowed_tools": [name for name, command in COMMAND_MAP.items() if command in effects]})
    scope = RunScope(run_id, "output-convergence", "execute")
    state = SimpleNamespace(engine=engine, client=client, run=run, scope=scope, outputs=outputs,
        sent=[], credentials=OfflineCredentials(), respond=lambda payload: final())

    def respond(request):
        payload = json.loads(request.content)
        assert engine._estimate_payload(payload) <= 48000, "measure the actual full serialized provider request"
        assert payload.get("tools"), "fitting isolated output reads must not invoke native summarization"
        state.sent.append(payload)
        output = state.respond(payload)
        event = {"type": "response.completed", "response": {"status": "completed", "output": output,
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
            text="data: " + json.dumps(event) + "\n\n")

    engine.model = build_chatgpt_model(credentials=state.credentials, transport=httpx.MockTransport(respond),
        verify_catalog=False, before_send=engine._before_send, observer=engine._model_event,
        on_request=engine._model_request, on_response=engine._model_response)
    engine.saver = InMemorySaver()
    with patch("shengbo_prelive.runtime.main_middleware",
               lambda e: [ContextProjection(e), ToolPolicy(e), ActionBoundary(e), Audit(e)]):
        engine.main = engine._build_graph("main")
    try:
        yield state
    finally:
        await engine.stop()


async def invoke(case, *, thread="provider-output-convergence"):
    token = CURRENT_SCOPE.set(case.scope)
    try:
        return await case.engine.main.ainvoke({"messages": [HumanMessage("继续。")],
            "files": case.engine._initial_files("main")},
            {"configurable": {"thread_id": thread}, "recursion_limit": GRAPH_RECURSION_LIMIT}, context=case.scope)
    finally:
        CURRENT_SCOPE.reset(token)


def plan_args(case, received):
    assert set(received) == set(MODULES)
    return {"preparation_id": case.run["scope"]["preparation_id"], "expected_revision": 1,
        "bundle_ref": case.client.job["body"]["context_ref"], "items": [{
            "product_ref": ref("product", "product-convergence", {"name": "测试商品"}), "variant_id": "converged-version",
            "modules": [{"key": module, "action": "disable" if module in {"campaign", "transition"} else "generate",
                "instruction": received[module]["sentences"][0]["text"] if module in received else "使用当前已核实事实。"}
                for module in MODULE_KEYS]}]}


def assert_no_context_churn(case):
    events = case.engine.events(case.run["id"], limit=1000)["events"]
    assert not any(event["type"] in {"context.summarized", "context.tools_offloaded", "model.window_rejected"}
                   for event in events)
    starts = [event["data"] for event in events if event["type"] == "tool.started"]
    assert not any(event["tool_name"] == "read_file" for event in starts)
    assert max(case.engine._estimate_payload(payload) for payload in case.sent) <= 48000
    for payload in case.sent:
        wire = canonical_json(payload)
        assert "FULL_CONTEXT_ONLY" not in wire and '"public_input"' not in wire
    return starts


@pytest.mark.asyncio
async def test_three_exact_outputs_from_50k_context_converge_to_committed_plan_in_four_calls(convergence):
    case = convergence

    def respond(payload):
        round_number = len(case.sent)
        seen = tool_outputs(payload)
        if round_number == 1:
            return [call("read_job", "read-job-once", {"job_id": case.client.job["ref"]["id"]})]
        if round_number == 2:
            job = seen["read-job-once"]
            attempts = [item["provider_attempts"][0] for item in job["body"]["items"]]
            assert {attempt["module_key"] for attempt in attempts} == set(MODULES)
            assert all(attempt["formal_module_ref"] is None and attempt["can_reuse_as_formal_module"] is False
                       for attempt in attempts)
            return [call("read_run_history", "read-output-" + attempt["module_key"],
                         {"ref": attempt["result_ref"], "path": "/output"}) for attempt in attempts]
        if round_number == 3:
            received = {module: seen["read-output-" + module]["document"] for module in MODULES}
            assert received == case.outputs
            assert all(seen["read-output-" + module]["selection"]["mode"] == "value" for module in MODULES)
            return [call("save_script_change_plan", "commit-plan-once", plan_args(case, received))]
        assert round_number == 4
        assert seen["commit-plan-once"]["status"] == "committed"
        assert seen["commit-plan-once"]["data"]["script_plan_ref"]["kind"] == "script_plan"
        return final()

    case.respond = respond
    result = await invoke(case)
    assert result["messages"][-1].content == "测试完成"
    assert len(case.sent) == case.credentials.calls == 4
    assert len(case.client.queries) == len(case.client.commands) == 1
    starts = assert_no_context_churn(case)
    assert Counter(event["tool_name"] for event in starts) == {
        "read_job": 1, "read_run_history": 3, "save_script_change_plan": 1}
    operations = case.engine.store.list("operation")
    assert len(operations) == 1 and operations[0]["status"] == "committed"
    assert operations[0]["request_hash"] == canonical_hash(operations[0]["args"])
    assert operations[0]["receipt"] == case.client.commands[0]["receipt"]
    assert case.engine.store.get("run", case.run["id"])["receipts"][0]["receipt"] == operations[0]["receipt"]
    assert sum(isinstance(message, ToolMessage) for message in result["messages"]) == 5


@pytest.mark.asyncio
async def test_unknown_snapshot_section_is_corrected_to_exact_output_and_plan(convergence):
    case = convergence

    def respond(payload):
        round_number, seen = len(case.sent), tool_outputs(payload)
        if round_number == 1:
            return [call("read_job", "job-for-correction", {"job_id": case.client.job["ref"]["id"]})]
        attempts = [item["provider_attempts"][0] for item in seen["job-for-correction"]["body"]["items"]]
        if round_number == 2:
            return [call("read_run_history", "wrong-section", {"ref": attempts[0]["result_ref"], "sections": ["artifacts"]})]
        if round_number == 3:
            error = seen["wrong-section"]
            assert error["status"] == "error" and error["correction_count"] == 1
            assert error["error"]["code"] == "HISTORY_SNAPSHOT_SECTION_INVALID"
            assert "output" in error["error"]["details"]["available_sections"]
            assert any(item["path"] == "/output" for item in error["error"]["details"]["available_paths"])
            return [call("read_run_history", "corrected-" + attempt["module_key"],
                         {"ref": attempt["result_ref"], "path": "/output"}) for attempt in attempts]
        if round_number == 4:
            received = {module: seen["corrected-" + module]["document"] for module in MODULES}
            assert received == case.outputs
            return [call("save_script_change_plan", "commit-corrected-plan", plan_args(case, received))]
        assert round_number == 5 and seen["commit-corrected-plan"]["status"] == "committed"
        return final()

    case.respond = respond
    result = await invoke(case, thread="correctable-output-selection")
    assert result["messages"][-1].content == "测试完成"
    assert len(case.sent) == case.credentials.calls == 5
    errors = [message for message in result["messages"] if isinstance(message, ToolMessage) and message.status == "error"]
    assert len(errors) == 1 and errors[0].tool_call_id == "wrong-section"
    assert case.engine.store.get("run", case.run["id"])["status"] == "RUNNING"
    assert case.engine.store.get("run", case.run["id"])["correctable_tool_errors"] == {"HISTORY_SNAPSHOT_SECTION_INVALID": 1}
    assert len(case.client.commands) == 1
    assert_no_context_churn(case)


@pytest.mark.asyncio
async def test_provider_output_snapshots_check_acl_and_hash_before_any_selection(convergence):
    case = convergence
    view = await case.engine.business_query(case.scope, "read_job", {"job_id": case.client.job["ref"]["id"]})
    references = [item["provider_attempts"][0]["result_ref"] for item in view["body"]["items"]]
    foreign_conversation = case.engine.create_conversation({"scope": {"kind": "catalog"}})
    foreign = case.engine.store.put("run", {**case.run, "id": "foreign-output-reader",
        "conversation_id": foreign_conversation["id"]})
    foreign_scope = RunScope(foreign["id"], "foreign-output", "execute")
    case.engine.store.put("branch", {"id": "unrelated-child", "run_id": case.run["id"], "role": "product_facts",
        "status": "RUNNING", "task": {"product_ids": [], "source_refs": [], "input_refs": []}})
    child_scope = RunScope(case.run["id"], "child-output", "execute", "product_facts", "unrelated-child")
    for module, reference in zip(MODULES, references):
        document = case.engine.run_history(case.scope, {"ref": reference, "full": True})["document"]
        assert document["output"] == case.outputs[module] and document["module_key"] == module
        assert document["owner_run_id"] == case.run["id"]
        assert document["job_ref"] == case.client.job["ref"]
        assert document["formal_module_ref"] is None and document["can_reuse_as_formal_module"] is False
        assert document["evidence_only"] is True
        assert "input" not in document and "context" not in document
        assert document["output_hash"] == canonical_hash(document["output"])
        for scope, reason in ((foreign_scope, "RUN_SCOPE_DENIED"), (child_scope, "BRANCH_SCOPE_DENIED")):
            # Deliberately invalid selectors must not reveal available paths
            # or bypass the original full-snapshot ACL through a small view.
            for selector in ({"path": "/missing"}, {"sections": ["artifacts"]}, {"full": True}):
                with pytest.raises(ContractError) as caught:
                    case.engine.run_history(scope, {"ref": reference, **selector})
                assert caught.value.code == reason and caught.value.status == 403
                assert "available_paths" not in caught.value.details
        with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
            case.engine.run_history(case.scope, {"ref": {**reference, "content_hash": canonical_hash("tampered")}, "path": "/output"})
    assert not case.sent and case.credentials.calls == 0 and not case.client.commands


@pytest.mark.asyncio
async def test_full_main_middleware_completes_thirty_read_rounds_beyond_old_recursion_limit(convergence):
    case = convergence
    engine, client = case.engine, case.client
    prep_body = {"id": case.run["scope"]["preparation_id"], "status": "draft", "generation_epoch": 3}
    client.preparation = {"ref": ref("preparation", prep_body["id"], prep_body), "body": prep_body}
    pending_body = {**deepcopy(client.job["body"]), "status": "running", "cancel_requested": False, "items": []}
    client.pending_job = {"ref": ref("prelive_job", pending_body["id"], pending_body, revision=28), "body": pending_body}
    # Build with every production middleware, including M1/M6/M7. Thirty
    # actual main cycles use >160 SDK graph steps, while all calls are local.
    engine.main = engine._build_graph("main")

    def respond(payload):
        round_number = len(case.sent)
        if round_number <= 30:
            return [call("read_preparation", f"cheap-read-{round_number:02d}", {"preparation_id": prep_body["id"]})]
        assert round_number == 31
        assert len(tool_outputs(payload)) == 30
        return [call("finish_turn", "finish-real-waiting", {"outcome": "WAITING_JOBS", "summary": "等待已观察到的后台任务。"})]

    case.respond = respond
    await engine._drive_main(case.run["id"])
    saved = engine.store.get("run", case.run["id"])
    assert saved["status"] == "WAITING_JOBS" and saved["acceptance"]["ok"] is True
    assert saved["acceptance"]["observation"]["pending_job_ids"] == [client.pending_job["ref"]["id"]]
    assert len(case.sent) == case.credentials.calls == 31
    state = await engine.main.aget_state({"configurable": {"thread_id": "main:" + case.run["id"]}})
    assert 160 < state.metadata["step"] < GRAPH_RECURSION_LIMIT
    assert GRAPH_RECURSION_LIMIT == MODEL_CALL_LIMIT * 8 + 16 == 976
    assert not state.next
    events = engine.events(case.run["id"], limit=1000)["events"]
    checks = [event["data"] for event in events if event["type"] == "middleware.check"]
    assert {"M1 RunFence", "M6 Budget", "M7 CompletionGuard"} <= {item["middleware"] for item in checks}
    assert any(item.get("reason_code") == "VERIFIED_FINISH_READY" and item.get("model_call_skipped") is True for item in checks)
    starts = assert_no_context_churn(case)
    assert Counter(item["tool_name"] for item in starts) == {"read_preparation": 30, "finish_turn": 1}
    assert len({item["tool_call_id"] for item in starts}) == 31
    assert Counter(key for key, _, _ in client.queries) == {"read_preparation": 30, "read_completion_observation": 1}
    assert not client.commands


@pytest.mark.asyncio
async def test_one_hundred_twenty_cumulative_reservations_remain_a_hard_cap(convergence):
    case = convergence
    assert MODEL_CALL_LIMIT == 120
    for position in range(MODEL_CALL_LIMIT):
        ticket = case.engine.store.reserve_attempt(case.run["id"], f"reservation-{position}", "main")
        case.engine.store.settle_attempt(ticket["id"], state="completed", usage={"total_tokens": 1})
    with pytest.raises(ContractError, match="BUDGET_EXHAUSTED"):
        case.engine.store.reserve_attempt(case.run["id"], "over-the-hard-cap", "main")
    with case.engine.store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM attempts WHERE run_id=?", (case.run["id"],)).fetchone()[0] == 120
    assert not case.sent and case.credentials.calls == 0 and not case.client.commands
