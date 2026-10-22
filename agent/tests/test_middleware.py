"""Real SDK hook contracts against isolated runtime/trace dependencies."""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from types import SimpleNamespace

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain.tools.tool_node import ToolCallRequest, ToolRuntime
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.runtime import Runtime
from langgraph.types import Interrupt
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.middleware import (ActionBoundary, Audit, BranchCompletionGuard, Budget,
                                       ChildFence, CompletionGuard, ContextProjection, RunFence,
                                       ToolPolicy, main_middleware, worker_middleware)
from shengbo_prelive.models import RunScope
from shengbo_prelive.observability import sanitize


class FakeStore:
    def __init__(self):
        self.data = {("run", "r1"): {"id": "r1", "status": "RUNNING", "intent_assessment": None},
                     ("branch", "b1"): {"id": "b1", "status": "RUNNING"}}

    def get(self, kind, identity):
        return self.data[(kind, identity)].copy()

    def update(self, kind, identity, **changes):
        self.data[(kind, identity)].update(changes)
        return self.get(kind, identity)


class FakeTrace:
    def __init__(self):
        self.events = []
        self.payloads = []
        self.tail_fails = False

    def emit(self, run_id, kind, data, **ids):
        self.events.append({"event_id": str(len(self.events) + 1), "type": kind, "data": sanitize(data), **ids})
        return self.events[-1]

    def try_emit(self, *args, **kwargs):
        if self.tail_fails:
            raise RuntimeError("sink failed")
        return self.emit(*args, **kwargs)

    def payload(self, run_id, value):
        if self.tail_fails:
            raise RuntimeError("sink failed")
        self.payloads.append(sanitize(value))
        return "payload:" + str(len(self.payloads))

    def assert_ready_for_side_effect(self):
        return None


class FakeEngine:
    def __init__(self):
        self.store = FakeStore()
        self.trace = FakeTrace()
        self.locks = defaultdict(asyncio.Lock)
        self.fenced = False
        self.finished = False
        self.prepared_report = None
        self.main_tool_names = {"read_product", "save_product"}

    async def assert_scope(self, scope):
        if self.fenced:
            raise ContractError("RUN_FENCED", "任务已撤权")
        return self.store.get("run", scope.run_id)

    async def project_context(self, scope):
        return {"phase": scope.phase, "role": scope.role, "product_ref": "p1@2"}

    def allowed_tool_names(self, scope):
        return {"read_product", "task"} if scope.role != "main" else {"read_product", "save_product", "task"}

    def action_lock(self, run_id):
        return self.locks[run_id]

    async def verify_finish(self, scope):
        return {"ok": self.finished, "missing": [] if self.finished else ["saved product receipt"]}

    def branch_report(self, scope):
        return self.prepared_report


def runtime(role="main", phase="execute"):
    return Runtime(context=RunScope("r1", "i1", phase, role=role, branch_task_id=None if role == "main" else "b1"))


def model_request(role="main", phase="execute", system=None):
    return ModelRequest(model=FakeListChatModel(responses=["ok"]), messages=[HumanMessage(content="更新资料")],
                        system_message=system or SystemMessage(content="SDK原有系统内容"),
                        tools=[{"name": "read_product"}, {"name": "save_product"}, {"name": "task"}, {"name": "execute"}],
                        state={"messages": []}, runtime=runtime(role, phase))


def tool_request(name, role="main", args=None):
    context = runtime(role).context
    tr = ToolRuntime(state={}, context=context, config={}, stream_writer=lambda _: None, tool_call_id="tc1", store=None)
    return ToolCallRequest(tool_call={"name": name, "args": args or {}, "id": "tc1", "type": "tool_call"}, tool=None, state={}, runtime=tr)


@pytest.mark.asyncio
async def test_fences_use_trusted_context_and_recheck_revocation():
    engine = FakeEngine()
    await RunFence(engine).abefore_agent({}, runtime())
    await ChildFence(engine).abefore_agent({}, runtime("product_facts"))
    with pytest.raises(ContractError, match="CHILD_SCOPE_REQUIRED"):
        await ChildFence(engine).abefore_agent({}, runtime())
    with pytest.raises(ContractError, match="RUNTIME_SCOPE_REQUIRED"):
        await RunFence(engine).abefore_agent({"scope": runtime().context}, Runtime(context=None))
    engine.fenced = True
    with pytest.raises(ContractError, match="RUN_FENCED"):
        await RunFence(engine).abefore_model({}, runtime())
    assert engine.trace.events[-1]["data"]["status"] == "DENIED"


@pytest.mark.asyncio
async def test_projection_preserves_structured_system_and_does_not_mutate_request():
    engine = FakeEngine()
    original = SystemMessage(content=[{"type": "text", "text": "SDK Skill规则"}], id="sys1")
    request = model_request(system=original)
    captured = []
    async def handler(changed):
        captured.append(changed)
        return ModelResponse(result=[AIMessage(content="ok")])
    await ContextProjection(engine).awrap_model_call(request, handler)
    assert captured[0].system_message.id == "sys1"
    # Default tail placement: structured system content is passed through unchanged.
    assert captured[0].system_message.content == original.content
    assert "p1@2" in captured[0].messages[-1].content
    assert captured[0].messages[-1].additional_kwargs["lc_source"] == "shengbo_trusted_context"
    assert request.system_message.content == [{"type": "text", "text": "SDK Skill规则"}]
    assert len(request.messages) == 1


@pytest.mark.asyncio
async def test_tool_filter_and_actual_forged_call_denied_for_role():
    engine = FakeEngine()
    captured = []
    async def handler(request):
        captured.append(request)
        return ModelResponse(result=[AIMessage(content="ok")])
    await ToolPolicy(engine).awrap_model_call(model_request("product_facts"), handler)
    assert [tool["name"] for tool in captured[0].tools] == ["read_product"]
    executed = False
    async def tool_handler(request):
        nonlocal executed
        executed = True
    for tool in ("task", "execute", "save_product", "sql", "write_file"):
        with pytest.raises(ContractError, match="TOOL_SCOPE_DENIED"):
            await ToolPolicy(engine).awrap_tool_call(tool_request(tool, "product_facts"), tool_handler)
    assert not executed


@pytest.mark.asyncio
async def test_action_boundary_serializes_writes_and_releases_on_interrupt():
    engine = FakeEngine()
    live = maximum = 0
    async def work(request):
        nonlocal live, maximum
        live += 1
        maximum = max(maximum, live)
        await asyncio.sleep(0.01)
        live -= 1
        return ToolMessage(content="saved", tool_call_id="tc1")
    boundary = ActionBoundary(engine)
    await asyncio.gather(boundary.awrap_tool_call(tool_request("save_product"), work), boundary.awrap_tool_call(tool_request("save_product"), work))
    assert maximum == 1
    async def interrupted(request):
        raise GraphInterrupt((Interrupt(value={"decision_id": "d1"}),))
    with pytest.raises(GraphInterrupt):
        await boundary.awrap_tool_call(tool_request("save_product"), interrupted)
    assert not engine.action_lock("r1").locked()


@pytest.mark.asyncio
async def test_budget_timeout_does_not_reserve_duplicate_provider_attempts():
    engine = FakeEngine()
    async def slow(request):
        await asyncio.sleep(1)
    with pytest.raises(ContractError, match="MODEL_TIMEOUT"):
        await Budget(engine, timeout_seconds=0.001).awrap_model_call(model_request(), slow)
    assert engine.trace.events[-1]["data"]["reason_code"] == "MODEL_TIMEOUT"


def _streaming(seconds, gap=0.02, silent_after=None):
    from shengbo_prelive.providers.deepseek import note_stream_activity

    async def handler(request):
        loop = asyncio.get_running_loop()
        started = loop.time()
        while loop.time() - started < seconds:
            if silent_after is None or loop.time() - started < silent_after:
                note_stream_activity()
            await asyncio.sleep(gap)
        return "done"
    return handler


@pytest.mark.asyncio
async def test_budget_lets_a_live_stream_run_past_the_legacy_total_limit():
    """2026-10-22: a streaming branch call was cut at 180 s while still sending data."""
    engine = FakeEngine()
    budget = Budget(engine, timeout_seconds=0.15, idle_seconds=0.2, max_seconds=2)
    assert await budget.awrap_model_call(model_request(), _streaming(0.6)) == "done"


@pytest.mark.asyncio
async def test_budget_cancels_a_silent_stream_and_caps_total_time():
    engine = FakeEngine()
    with pytest.raises(ContractError, match="MODEL_TIMEOUT"):
        await Budget(engine, timeout_seconds=5, idle_seconds=0.15, max_seconds=5).awrap_model_call(
            model_request(), _streaming(2, silent_after=0.1))
    assert engine.trace.events[-1]["data"]["timeout_kind"] == "idle"
    with pytest.raises(ContractError, match="MODEL_TIMEOUT"):
        # Wide idle window so a busy test machine cannot turn the total-cap case into an idle one.
        await Budget(engine, timeout_seconds=5, idle_seconds=1.0, max_seconds=0.4).awrap_model_call(
            model_request(), _streaming(2))
    assert engine.trace.events[-1]["data"]["timeout_kind"] == "total"


@pytest.mark.asyncio
async def test_main_completion_needs_saved_assessment_and_real_finish_receipt():
    engine = FakeEngine()
    guard = CompletionGuard(engine)
    state = {"messages": [AIMessage(content="全部完成") ]}
    assert (await guard.aafter_model(state, runtime(phase="intent")))["jump_to"] == "model"
    engine.store.update("run", "r1", intent_assessment={"objective": "catalog_update"})
    assert await guard.aafter_model(state, runtime(phase="intent")) is None
    assert (await guard.aafter_model(state, runtime()))["jump_to"] == "model"
    assert (await guard.aafter_model(state, runtime()))["jump_to"] == "model"
    with pytest.raises(ContractError, match="COMPLETION_EVIDENCE_MISSING"):
        await guard.aafter_model(state, runtime())
    engine.finished = True
    assert await guard.aafter_model(state, runtime()) is None
    assert getattr(CompletionGuard.aafter_model, "__can_jump_to__") == ["model"]
    pending = {"messages": [AIMessage(content="", tool_calls=[{"name": "save_product", "args": {}, "id": "tc"}])]}
    assert await guard.aafter_model(pending, runtime()) is None


@pytest.mark.asyncio
async def test_worker_completion_needs_prepared_structured_report():
    engine = FakeEngine()
    guard = BranchCompletionGuard(engine)
    state = {"messages": [AIMessage(content="分析好了")]}
    assert (await guard.aafter_model(state, runtime("product_facts")))["jump_to"] == "model"
    engine.prepared_report = {"status": "DRAFT"}
    assert (await guard.aafter_model(state, runtime("product_facts")))["jump_to"] == "model"
    with pytest.raises(ContractError, match="BRANCH_REPORT_MISSING"):
        await guard.aafter_model(state, runtime("product_facts"))
    engine.prepared_report = {"status": "PREPARED", "report_ref": "report1"}
    assert await guard.aafter_model(state, runtime("product_facts")) is None


@pytest.mark.asyncio
async def test_audit_safe_full_payload_and_tail_failure_preserves_success():
    engine = FakeEngine()
    audit = Audit(engine)
    async def successful(request):
        return ModelResponse(result=[AIMessage(content="公开说明", additional_kwargs={"reasoning_content": "PRIVATE_NEVER"})])
    response = await audit.awrap_model_call(model_request(), successful)
    assert response.result[0].content == "公开说明"
    assert "PRIVATE_NEVER" not in str(engine.trace.payloads)
    async def tool(request):
        engine.trace.tail_fails = True
        return ToolMessage(content="COMMITTED", tool_call_id="tc1")
    result = await audit.awrap_tool_call(tool_request("save_product", args={"api_key": "SECRET_NEVER", "large": "x" * 2000}), tool)
    assert result.content == "COMMITTED"
    assert "SECRET_NEVER" not in str(engine.trace.payloads)
    assert "x" * 2000 in str(engine.trace.payloads), "full safe args must not be truncated in storage"


@pytest.mark.asyncio
async def test_audit_never_replaces_graph_interrupt_or_cancellation():
    engine = FakeEngine()
    async def interrupted(request):
        engine.trace.tail_fails = True
        raise GraphInterrupt((Interrupt(value={"decision_id": "d1"}),))
    with pytest.raises(GraphInterrupt):
        await Audit(engine).awrap_tool_call(tool_request("read_product"), interrupted)


def test_factories_have_nine_distinct_boundaries_and_no_native_task_entry():
    engine = FakeEngine()
    main = main_middleware(engine)
    workers = worker_middleware(engine)
    from langchain.agents.middleware import ModelRetryMiddleware
    from shengbo_prelive.middleware import _Boundary
    # Nine project boundaries; the only addition is one native malformed-tool-call retry (batch 3 A4).
    assert len({type(item).__name__ for item in main + workers if isinstance(item, _Boundary)}) == 9
    for chain in (main, workers):
        retries = [item for item in chain if isinstance(item, ModelRetryMiddleware)]
        assert len(retries) == 1 and retries[0].max_retries == 1
        assert retries[0].retry_on(type("E", (Exception,), {"code": "model_tool_call_invalid"})())
        assert not retries[0].retry_on(type("E", (Exception,), {"code": "model_transport_failed"})())
        assert not any(type(item).__name__ not in {"ModelRetryMiddleware"} and not isinstance(item, _Boundary) for item in chain)
    assert all(isinstance(item, AgentMiddleware) for item in main + workers)


@pytest.mark.asyncio
async def test_hooks_execute_inside_real_deep_agents_graph_without_provider_call():
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import InMemorySaver

    class BindableFakeModel(FakeListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    engine = FakeEngine()
    engine.finished = True
    graph = create_deep_agent(model=BindableFakeModel(responses=["已依据回执完成"]), tools=[],
                              system_prompt="原始Deep Agents系统规则", middleware=main_middleware(engine),
                              context_schema=RunScope, checkpointer=InMemorySaver())
    result = await graph.ainvoke({"messages": [HumanMessage(content="读取准备结果")]},
                                 config={"configurable": {"thread_id": "middleware-test"}}, context=runtime().context)
    assert result["messages"][-1].content == "已依据回执完成"
    names = {event["data"].get("middleware") for event in engine.trace.events}
    assert {"M1 RunFence", "M3 ContextProjection", "M4 ToolPolicy", "M6 Budget", "M7 CompletionGuard"} <= names
    assert any(event["type"] == "model.completed" for event in engine.trace.events)


@pytest.mark.asyncio
async def test_worker_report_tool_runs_through_real_graph_and_prepared_guard():
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver

    class BindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    engine = FakeEngine()
    engine.allowed_tool_names = lambda scope: {"submit_product_facts_report"}

    @tool
    async def submit_product_facts_report(summary: str) -> dict:
        """Persist a structured product-facts candidate report."""
        engine.prepared_report = {"status": "PREPARED", "summary": summary}
        return {"status": "PREPARED", "report_ref": "report1"}

    model = BindableFakeModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "submit_product_facts_report", "args": {"summary": "商品事实已整理"}, "id": "worker-tc1"}]),
        AIMessage(content="候选报告已提交，等待主 Agent 采纳")])
    graph = create_deep_agent(model=model, tools=[submit_product_facts_report], middleware=worker_middleware(engine),
                              context_schema=RunScope, checkpointer=InMemorySaver())
    result = await graph.ainvoke({"messages": [HumanMessage(content="整理商品事实")]},
                                config={"configurable": {"thread_id": "worker-middleware-test"}},
                                context=runtime("product_facts").context)
    assert engine.prepared_report["status"] == "PREPARED"
    # Once the structured report exists, end without a redundant model prose
    # turn. A transport failure in that unnecessary call must not lose a report.
    assert result["messages"][-1].type == "tool"
    assert json.loads(result["messages"][-1].content)["report_ref"] == "report1"
    assert len([m for m in result["messages"] if isinstance(m, AIMessage)]) == 1
    assert any(event["data"].get("reason_code") == "STRUCTURED_REPORT_READY" for event in engine.trace.events)
    assert not engine.action_lock("r1").locked()
    assert any(event["data"].get("middleware") == "M5 ActionBoundary" for event in engine.trace.events)
    assert any(event["data"].get("middleware") == "M8 BranchCompletionGuard" for event in engine.trace.events)
    assert any(event["type"] == "tool.completed" for event in engine.trace.events)


@pytest.mark.asyncio
async def test_session_worker_corrects_null_ref_locally_and_reads_real_schema_and_voice_tools():
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.memory import InMemorySaver
    from shengbo_prelive.policy import ROLE_TOOLS
    from shengbo_prelive.tools import build_tools

    class BindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    engine = FakeEngine()
    engine.allowed_tool_names = lambda scope: ROLE_TOOLS[scope.role]
    assert {"read_business_schema", "read_voice_options"} <= ROLE_TOOLS["session_config"]
    assert not {"save_session_draft", "save_product", "prepare_required_audio"} & ROLE_TOOLS["session_config"]
    calls = []

    async def query(scope, key, args):
        calls.append((key, args))
        assert key != "read_context", "invalid nullable refs must never reach a backend client"
        return {"session_draft_schema": {"type": "object"}} if key == "get_business_schema" else {"providers": ["system"], "voices": ["Huihui"]}

    def submit(scope, report):
        engine.prepared_report = {"status": "PREPARED", "report": report}
        return {"status": "PREPARED"}

    engine.business_query, engine.submit_report = query, submit
    model = BindableFakeModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "bad-null", "name": "read_context", "args": {"ref": None, "sections": ["session_draft_schema", "voice_options"]}}]),
        AIMessage(content="", tool_calls=[{"id": "typed-schema", "name": "read_business_schema", "args": {}}, {"id": "typed-voice", "name": "read_voice_options", "args": {}}]),
        AIMessage(content="", tool_calls=[{"id": "typed-report", "name": "submit_session_config_report", "args": {"report": {"summary": "已核对实际schema与本机声音"}}}])])
    graph = create_deep_agent(model=model, tools=build_tools(engine, "session_config"), middleware=worker_middleware(engine),
        context_schema=RunScope, checkpointer=InMemorySaver())
    result = await graph.ainvoke({"messages": [HumanMessage("分析本场配置，按真实schema选择声音")]},
        {"configurable": {"thread_id": "session-null-correction"}}, context=runtime("session_config").context)
    error = next(message for message in result["messages"] if isinstance(message, ToolMessage) and message.tool_call_id == "bad-null")
    assert error.status == "error" and "ref" in error.content
    assert {key for key, _ in calls} == {"get_business_schema", "voice_options"}
    assert engine.prepared_report["status"] == "PREPARED"
    assert len([message for message in result["messages"] if isinstance(message, AIMessage)]) == 3


@pytest.mark.asyncio
async def test_registered_strict_tool_receives_trusted_tool_runtime_in_real_graph():
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.memory import InMemorySaver
    from shengbo_prelive.tools import build_tools

    class BindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    engine = FakeEngine()
    engine.finished = True
    calls = []
    async def business_query(scope, key, args):
        calls.append((scope, key, args))
        return {"product_id": "p1", "revision": 2}
    engine.business_query = business_query
    read_product = next(tool for tool in build_tools(engine) if tool.name == "read_product")
    model = BindableFakeModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "read_product", "args": {"product_id": "p1", "runtime": {"context": {"run_id": "forged"}}}, "id": "strict-tc1"}]),
        AIMessage(content="商品已读取")])
    graph = create_deep_agent(model=model, tools=[read_product], middleware=main_middleware(engine),
                              context_schema=RunScope, checkpointer=InMemorySaver())
    result = await graph.ainvoke({"messages": [HumanMessage(content="读取商品")]},
                        config={"configurable": {"thread_id": "strict-tool-test"}}, context=runtime().context)
    assert len(calls) == 1, [(message.type, message.content) for message in result["messages"]]
    assert isinstance(calls[0][0], RunScope)
    assert calls[0][0].run_id == "r1"
    assert calls[0][1] == "read_product"
    assert calls[0][2]["product_id"] == "p1"
    assert "runtime" not in read_product.tool_call_schema.model_json_schema()["properties"]


@pytest.mark.asyncio
async def test_completion_correction_edges_are_bounded_in_actual_sdk_graph():
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import InMemorySaver

    class BindableFakeModel(FakeListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    engine = FakeEngine()
    graph = create_deep_agent(model=BindableFakeModel(responses=["我宣称已完成"]), middleware=main_middleware(engine),
                              context_schema=RunScope, checkpointer=InMemorySaver())
    with pytest.raises(ContractError, match="COMPLETION_EVIDENCE_MISSING"):
        await graph.ainvoke({"messages": [HumanMessage(content="更新商品")]},
                            config={"configurable": {"thread_id": "bounded-guard-test"}}, context=runtime().context)
    assert engine.store.get("run", "r1")["guard_retries"]["execute"] == 2
    assert len([event for event in engine.trace.events if event["type"] == "model.completed"]) == 3


@pytest.mark.asyncio
async def test_correctable_tool_inputs_are_bounded_but_security_and_unknown_stay_hard():
    engine = FakeEngine()
    boundary = ActionBoundary(engine)
    async def bad_quote(request):
        raise ContractError('INTENT_EVIDENCE_INVALID', '原文引用不正确')
    for count in range(1, 4):
        message = await boundary.awrap_tool_call(tool_request('save_task_assessment'), bad_quote)
        assert message.status == 'error'
        assert 'original_user_request' in message.content
    with pytest.raises(ContractError, match='INTENT_EVIDENCE_INVALID'):
        await boundary.awrap_tool_call(tool_request('save_task_assessment'), bad_quote)
    for code in ('RUN_FENCED', 'TOOL_SCOPE_DENIED', 'REMOTE_OUTCOME_UNKNOWN'):
        async def denied(request, error_code=code):
            raise ContractError(error_code, 'hard boundary')
        with pytest.raises(ContractError, match=code):
            await boundary.awrap_tool_call(tool_request('save_product'), denied)
