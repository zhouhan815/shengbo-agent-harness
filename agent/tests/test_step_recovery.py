"""Verify step recovery through the actual Deep Agents middleware and checkpoints."""
from collections import Counter
import json

from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.action_contracts import recoverable_read_error
from shengbo_prelive.middleware import ActionBoundary, main_middleware, worker_middleware
from shengbo_prelive.models import RunScope
from shengbo_prelive.store import RuntimeStore
from test_middleware import FakeEngine, runtime, tool_request
from test_priority_eight_runtime import report_branch
from test_runtime_business import env


def action(tool_name, call_id, **args):
    return {"name": tool_name, "id": call_id, "args": args}


class RecoveryModel(FakeMessagesListChatModel):
    program: list
    position: int = 0

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        assert self.position < len(self.program), "The accepted result should end without another request"
        expected_call_id, next_action = self.program[self.position]
        self.position += 1
        if expected_call_id:
            feedback = next(msg for msg in reversed(messages) if isinstance(msg, ToolMessage))
            assert feedback.tool_call_id == expected_call_id
            assert feedback.status == "error"
            assert json.loads(feedback.content)["step_recovery"]
        message = AIMessage(content="", tool_calls=[next_action])
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.parametrize("role", ["main", "product_facts"])
async def test_recovery_is_model_guided_and_does_not_repeat_the_same_failed_action(tmp_path, role):
    engine = FakeEngine()
    engine.store = RuntimeStore(tmp_path / "runtime.db")
    engine.store.put("run", {"id": "r1", "status": "RUNNING", "intent_assessment": None})
    engine.store.put("branch", {"id": "b1", "status": "RUNNING"})
    calls = Counter()

    @tool
    async def read_source(section: str) -> dict:
        """Read a different view of the same authorized material."""
        calls[section] += 1
        if section == "full":
            raise recoverable_read_error("MATERIAL_READ_ROUTE_INVALID", "当前读取方式无法打开整份附件")
        return {"status": "ok", "facts": ["long-life variant", "55 Wh"]}

    @tool
    async def finish_turn(outcome: str, summary: str) -> dict:
        """Persist the main Agent's verified task outcome."""
        calls[outcome] += 1
        assert outcome == "COMPLETED"
        engine.finished = True
        engine.store.update("run", "r1", finish={"outcome": outcome}, acceptance={"ok": True})
        return {"accepted": True}

    @tool
    async def submit_product_facts_report(summary: str) -> dict:
        """Persist a worker's structured product-facts report."""
        calls["report"] += 1
        engine.prepared_report = {"status": "PREPARED", "summary": summary}
        return {"status": "PREPARED"}

    @tool
    async def save_product() -> dict:
        """Persist the main Agent's already authorized product result."""
        calls["save_product"] += 1
        return {"status": "committed", "product_id": "p1"}

    middleware = main_middleware if role == "main" else worker_middleware
    tools = [read_source, save_product, finish_turn] if role == "main" else [read_source, submit_product_facts_report]
    engine.allowed_tool_names = lambda scope: {tool.name for tool in tools}
    program = [(None, action("read_source", "first-read", section="full")),
               ("first-read", action("read_source", "same-read", section="full")),
               ("same-read", action("read_source", "alternate-read", section="overview"))]
    if role == "main":
        program.extend([(None, action("save_product", "save-once")),
                        (None, action("finish_turn", "complete", outcome="COMPLETED", summary="已保存商品"))])
    else:
        program.append((None, action("submit_product_facts_report", "publish", summary="商品规格已核对")))
    model = RecoveryModel(responses=[], program=program)
    graph = create_deep_agent(model=model, tools=tools, middleware=middleware(engine), context_schema=RunScope,
                              checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "test-step-recovery-" + role}}
    result = await graph.ainvoke({"messages": [HumanMessage("读取附件并保存核对后的成果")]},
                                 config, context=runtime(role).context)
    assert not (await graph.aget_state(config)).next
    errors = {message.tool_call_id: json.loads(message.content)["step_recovery"] for message in result["messages"]
              if isinstance(message, ToolMessage) and message.status == "error"}
    assert set(errors) == {"first-read", "same-read"}
    assert errors["first-read"]["tool"] == "read_source"
    assert model.position == len(program)
    assert calls == ({"full": 1, "overview": 1, "save_product": 1, "COMPLETED": 1}
                     if role == "main" else {"full": 1, "overview": 1, "report": 1})


async def test_main_has_one_alternative_after_trying_to_end_on_a_recoverable_blocker(tmp_path):
    engine = FakeEngine()
    engine.store = RuntimeStore(tmp_path / "runtime.db")
    engine.store.put("run", {"id": "r1", "status": "RUNNING", "intent_assessment": None})
    calls = Counter()

    @tool
    async def read_source(section: str) -> dict:
        """Read authorized material without mutation."""
        calls[section] += 1
        raise recoverable_read_error("MATERIAL_READ_ROUTE_INVALID", "当前读取方式不可用")

    @tool
    async def finish_turn(outcome: str, summary: str) -> dict:
        """Persist the main Agent's decision to stop."""
        calls["finish"] += 1
        assert outcome == "BLOCKED"
        engine.store.update("run", "r1", finish={"outcome": outcome}, acceptance={"ok": True})
        return {"accepted": True}

    tools = [read_source, finish_turn]
    engine.allowed_tool_names = lambda scope: {tool.name for tool in tools}
    program = [(None, action("read_source", "first-read", section="full")),
               ("first-read", action("finish_turn", "early-block", outcome="BLOCKED", summary="无法读取")),
               ("early-block", action("read_source", "alternate-read", section="overview")),
               ("alternate-read", action("finish_turn", "final-block", outcome="BLOCKED", summary="两种合法视图都不可读"))]
    model = RecoveryModel(responses=[], program=program)
    graph = create_deep_agent(model=model, tools=tools, middleware=main_middleware(engine),
                              context_schema=RunScope, checkpointer=InMemorySaver())
    result = await graph.ainvoke({"messages": [HumanMessage("读取附件")]},
                                 {"configurable": {"thread_id": "test-step-recovery-bounded-block"}}, context=runtime().context)
    assert calls == {"full": 1, "overview": 1, "finish": 1}
    assert model.position == len(program)
    assert result["messages"][-1].tool_call_id == "final-block"


async def test_invalid_write_recovers_with_corrected_arguments_and_one_committed_write(tmp_path):
    engine = FakeEngine()
    engine.store = RuntimeStore(tmp_path / "runtime.db")
    engine.store.put("run", {"id": "r1", "status": "RUNNING", "intent_assessment": None})
    attempts = []
    writes = []

    @tool
    async def save_product(name: str) -> dict:
        """Validate and save the authorized product."""
        attempts.append(name)
        if not name:
            raise ContractError("TOOL_ARGUMENTS_INVALID", "商品名称不能为空", status=422,
                                details={"commit_state": "not_committed", "issues": [{"loc": ["name"]}]})
        writes.append(name)
        engine.store.update("run", "r1", finish={"outcome": "COMPLETED"}, acceptance={"ok": True})
        return {"status": "committed", "product_id": "p1"}

    engine.allowed_tool_names = lambda scope: {"save_product"}
    program = [(None, action("save_product", "bad-write", name="")),
               ("bad-write", action("save_product", "duplicate-write", name="")),
               ("duplicate-write", action("save_product", "corrected-write", name="营地灯"))]
    model = RecoveryModel(responses=[], program=program)
    graph = create_deep_agent(model=model, tools=[save_product], middleware=main_middleware(engine),
                              context_schema=RunScope, checkpointer=InMemorySaver())
    result = await graph.ainvoke({"messages": [HumanMessage("按附件保存商品营地灯")]},
                                 {"configurable": {"thread_id": "test-step-recovery-write"}}, context=runtime().context)
    assert attempts == ["", "营地灯"]
    assert writes == ["营地灯"]
    assert model.position == len(program)
    assert json.loads(result["messages"][-1].content)["status"] == "committed"


@pytest.mark.parametrize("code,status,pending_unknown", [
    ("WRITE_OUTCOME_UNKNOWN", 502, "op1"),
    ("SOURCE_SCOPE_DENIED", 403, None),
    ("TOOL_ARGUMENTS_INVALID", 422, "op1"),
])
async def test_unknown_or_unauthorized_actions_stay_hard(code, status, pending_unknown):
    engine = FakeEngine()
    engine.store.update("run", "r1", pending_unknown_operation=pending_unknown)
    original_error = ContractError(code, "Blocked by a safety boundary", status=status,
                                   details={"commit_state": "not_committed"})
    calls = Counter()

    async def handler(request):
        calls["handler"] += 1
        raise original_error

    with pytest.raises(ContractError) as caught:
        await ActionBoundary(engine).awrap_tool_call(tool_request("save_product"), handler)
    assert caught.value is original_error
    assert calls == {"handler": 1}
    assert not any(event["data"]["status"] == "CORRECTING" for event in engine.trace.events)


async def test_invalid_attachment_location_recommends_real_views_of_the_exact_source(env):
    engine, _ = env
    _, ref, scope = await report_branch(engine)
    with pytest.raises(ContractError) as caught:
        await engine.read_source(scope, ref, "text:99999")
    error = caught.value
    assert error.code == "MATERIAL_READ_ROUTE_INVALID"
    assert error.details["previous_section"] == "text:99999"
    actions = error.details["next_actions"]
    assert actions
    assert all(item["tool"] == "read_source" and item["args"]["ref"] == ref for item in actions)
    index = next(item["args"] for item in actions if item["args"]["section"] == "locator_index:1")
    result = await engine.read_source(scope, **index)
    assert result["source_material_ref"] == ref
    text = await engine.read_source(scope, ref, "text:1")
    assert text["text"] == "测试商品，129元"


@pytest.mark.parametrize("status, recorded_workflow, evidence_matches, should_replan", [
    ("NEEDS_REPLAN", "wf_product_materials", True, True),
    ("NEEDS_REPLAN", "wf_script_execution", True, False),
    ("NEEDS_REPLAN", "wf_product_materials", False, False),
    ("NEEDS_DECISION", "wf_product_materials", True, False),
    ("OUTCOME_UNKNOWN", "wf_product_materials", True, False),
])
async def test_only_attested_replan_workflow_result_delays_blocked_finish(tmp_path, status, recorded_workflow, evidence_matches, should_replan):
    engine = FakeEngine()
    engine.store = RuntimeStore(tmp_path / "runtime.db")
    engine.store.put("run", {"id": "r1", "status": "RUNNING", "intent_assessment": None})
    calls = Counter()

    @tool
    async def wf_product_materials(action: str) -> dict:
        """Return an already completed workflow step and remaining blockers."""
        calls["workflow"] += 1
        engine.store.put("workflow", {"id": "flow1", "run_id": "r1", "workflow": recorded_workflow,
                                      "status": status, "evidence_ref": "payload:verified",
                                      "completed_steps": ["prior step"]})
        return {"status": status, "flow_id": "flow1", "workflow": "wf_product_materials",
                "evidence_ref": "payload:verified" if evidence_matches else "payload:unverified",
                "blockers": [{"code": "PREPARATION_PRODUCTS_REQUIRED"}],
                "completed_steps": ["prior step"], "committed_refs": [],
                "remaining_steps": ["读取当前商品核对依赖。"]}

    @tool
    async def read_product() -> dict:
        """Inspect current data through another authorized tool."""
        calls["alternate"] += 1
        return {"product_id": "p1"}

    @tool
    async def finish_turn(outcome: str, summary: str) -> dict:
        """Persist the main Agent's final outcome."""
        calls[outcome] += 1
        engine.store.update("run", "r1", finish={"outcome": outcome}, acceptance={"ok": True})
        return {"accepted": True}

    tools = [wf_product_materials, read_product, finish_turn]
    engine.allowed_tool_names = lambda scope: {tool.name for tool in tools}
    program = [(None, action("wf_product_materials", "workflow", action="inspect")),
               (None, action("finish_turn", "initial-block", outcome="BLOCKED", summary="缺少依赖"))]
    if should_replan:
        program.extend([("initial-block", action("read_product", "alternate")),
                        (None, action("finish_turn", "complete", outcome="COMPLETED", summary="已核对商品"))])
    model = RecoveryModel(responses=[], program=program)
    graph = create_deep_agent(model=model, tools=tools, middleware=main_middleware(engine),
                              context_schema=RunScope, checkpointer=InMemorySaver())
    await graph.ainvoke({"messages": [HumanMessage("核对商品资料")]},
                        {"configurable": {"thread_id": "workflow-recovery"}}, context=runtime().context)
    assert model.position == len(program)
    assert calls == ({"workflow": 1, "alternate": 1, "COMPLETED": 1} if should_replan
                     else {"workflow": 1, "BLOCKED": 1})
