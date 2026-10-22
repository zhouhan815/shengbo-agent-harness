"""2026-10-22 Deep Agents optimization batch: offline contracts, no model/TTS calls.

Covers: role-local Skill split + objective pinning (SkillsMiddleware.pinned_skills),
M3 tail placement and one-projection-per-call cache, job-wake review entry,
HarnessProfile resolution for the ChatGPT-plan model, usage-detail capture and
the private SummarizationMiddleware hooks this app relies on.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends import StateBackend
from deepagents.backends.utils import file_data_to_string
from deepagents.middleware.skills import SkillsMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from shengbo_prelive import middleware as mw
from shengbo_prelive.models import RunScope
from shengbo_prelive.runtime import HARNESS_PROVIDER_KEY, SKILL_PINS_BY_OBJECTIVE, RuntimeEngine
from shengbo_prelive.store import RuntimeStore

ROOT = Path(__file__).resolve().parents[1]
MAIN_SKILLS = {"prelive-orchestration", "prelive-materials", "prelive-script-plan", "prelive-delivery",
               "prelive-recovery", "core-selling-points"}


def main_files():
    return RuntimeEngine._initial_files(None, "main")


def skill_metadata(files):
    backend = StateBackend()
    backend._read_files = lambda: files  # same isolation trick as the v6 Skill test
    return SkillsMiddleware(backend=backend, sources=["/skills/main/"]).before_agent({"files": files}, None, {})


def test_main_skill_split_exports_every_manual_with_valid_frontmatter():
    files = main_files()
    metadata = skill_metadata(files)
    assert not metadata["skills_load_errors"]
    assert {item["name"] for item in metadata["skills_metadata"]} == MAIN_SKILLS
    # The legacy virtual path of the core manual is unchanged for old receipts.
    assert "/skills/main/prelive-orchestration/SKILL.md" in files
    core = file_data_to_string(files["/skills/main/prelive-orchestration/SKILL.md"])
    assert "## 按任务预加载的分册" in core
    for name in ("prelive-materials", "prelive-script-plan", "prelive-delivery", "prelive-recovery"):
        assert name in core


def test_every_original_section_lives_in_exactly_one_main_manual():
    headings = []
    for path in sorted((ROOT / "skills/main").glob("prelive*/SKILL.md")):
        headings += [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("## ")]
    expected = {"## 先确定任务与范围", "## 资料到话术：一次理解、分级补料与按需继续", "## 三类分析的调用与汇合",
                "## 商品与本场的边界", "## 完整上下文与模块计划", "## 编排、批准与交付", "## 记录与上下文",
                "## 可选择的轻量工具流", "## 已返回但校验失败的恢复", "## 本次文本分支与精确恢复"}
    assert expected <= set(headings)
    assert all(headings.count(item) == 1 for item in expected)


def test_pin_map_names_only_existing_main_manuals_for_every_objective():
    from typing import get_args
    from shengbo_prelive.models import OBJECTIVES
    assert set(SKILL_PINS_BY_OBJECTIVE) == set(get_args(OBJECTIVES))
    for names in SKILL_PINS_BY_OBJECTIVE.values():
        assert names[0] == "prelive-orchestration" and set(names) <= MAIN_SKILLS


def pin_engine(tmp_path, objective):
    store = RuntimeStore(tmp_path / "agent.sqlite3")
    store.put("run", {"id": "run1", "status": "RUNNING", "phase": "execute", "goal": {"objective": objective}})
    events = []
    engine = SimpleNamespace(store=store, _emit=lambda run_id, kind, data, scope=None: events.append((kind, data)))
    engine._pin_skills = lambda *args, **kwargs: RuntimeEngine._pin_skills(engine, *args, **kwargs)
    return engine, events


def test_pinning_is_objective_based_recorded_as_read_and_once_per_run(tmp_path):
    engine, events = pin_engine(tmp_path, "materials_to_script")
    files = main_files()
    scope = RunScope("run1", "inv1", "execute", "main", None)
    names = RuntimeEngine._pin_execute_skills(engine, engine.store.get("run", "run1"), scope, files)
    assert names == ["prelive-orchestration", "prelive-materials", "prelive-script-plan"]
    reads = [item for item in engine.store.list("skill_read") if item["run_id"] == "run1"]
    assert {item["path"] for item in reads} == {f"/skills/main/{name}/SKILL.md" for name in names}
    assert all(item["line_ranges"] == [[1, item["line_count"]]] and item["delivery"] == "pinned_skill_message" for item in reads)
    assert events[-1][0] == "skills.pinned" and events[-1][1]["skills"] == names
    # A second execute turn of the same run never pins again.
    assert RuntimeEngine._pin_execute_skills(engine, engine.store.get("run", "run1"), scope, files) == []


def test_old_checkpoint_without_split_manuals_only_pins_what_it_has(tmp_path):
    engine, _ = pin_engine(tmp_path, "prepare_session")
    files = {path: value for path, value in main_files().items() if "/prelive-orchestration/" in path}
    scope = RunScope("run1", "inv1", "execute", "main", None)
    assert RuntimeEngine._pin_execute_skills(engine, engine.store.get("run", "run1"), scope, files) == ["prelive-orchestration"]


@pytest.mark.asyncio
async def test_native_skills_middleware_appends_pinned_bodies_before_model():
    files = main_files()
    backend = StateBackend()
    backend._read_files = lambda: files

    async def download(paths):
        from deepagents.backends.protocol import FileDownloadResponse
        return [FileDownloadResponse(path=path, content=file_data_to_string(files[path]).encode("utf-8")) for path in paths]

    backend.adownload_files = download
    middleware = SkillsMiddleware(backend=backend, sources=["/skills/main/"])
    metadata = middleware.before_agent({"files": files}, None, {})
    update = await middleware.abefore_model({"pinned_skills": ["prelive-orchestration", "prelive-delivery"],
                                             "skills_metadata": metadata["skills_metadata"]}, None)
    messages = update["messages"]
    assert [m.additional_kwargs["skill"]["name"] for m in messages] == ["prelive-orchestration", "prelive-delivery"]
    assert all(m.additional_kwargs["lc_source"] == "pinned_skill" for m in messages)
    assert "## 编排、批准与交付" in messages[1].content


class ProjectionEngine:
    def __init__(self, placement="tail"):
        self.trusted_context_placement = placement
        self.calls = 0
        self.trace = SimpleNamespace(emit=lambda *a, **k: {"event_id": "e"}, try_emit=lambda *a, **k: None)

    async def assert_scope(self, scope):
        return {"id": scope.run_id}

    async def project_context(self, scope):
        self.calls += 1
        return {"goal": {"objective": "materials_to_script"}, "call": self.calls}


def request_for(scope, system=None):
    return ModelRequest(model=FakeListChatModel(responses=["ok"]), messages=[HumanMessage(content="开始")],
                        system_message=system or SystemMessage(content="固定系统规则"), tools=[],
                        state={"messages": []}, runtime=Runtime(context=scope))


@pytest.mark.asyncio
async def test_m3_tail_keeps_system_byte_stable_and_appends_one_trusted_message():
    engine = ProjectionEngine()
    scope = RunScope("run1", "inv1", "execute", "main", None)
    captured = []

    async def handler(changed):
        captured.append(changed)
        return ModelResponse(result=[AIMessage(content="ok")])

    request = request_for(scope)
    await mw.ContextProjection(engine).awrap_model_call(request, handler)
    sent = captured[0]
    assert sent.system_message.content == "固定系统规则"
    tail = sent.messages[-1]
    assert isinstance(tail, SystemMessage) and tail.additional_kwargs["lc_source"] == mw.TRUSTED_CONTEXT_SOURCE
    assert re.search(r"<shengbo_trusted_context>\s*\{.*\}\s*</shengbo_trusted_context>", tail.content, re.S)
    assert len(request.messages) == 1  # the original request (and graph state) is not mutated


@pytest.mark.asyncio
async def test_m3_system_placement_remains_available_as_rollback():
    engine = ProjectionEngine("system")
    scope = RunScope("run1", "inv1", "execute", "main", None)
    captured = []

    async def handler(changed):
        captured.append(changed)
        return ModelResponse(result=[AIMessage(content="ok")])

    await mw.ContextProjection(engine).awrap_model_call(request_for(scope), handler)
    assert "<shengbo_trusted_context>" in captured[0].system_message.content
    assert len(captured[0].messages) == 1


@pytest.mark.asyncio
async def test_projection_is_computed_once_per_call_and_recomputed_after_summary():
    engine = ProjectionEngine()
    scope = RunScope("run1", "inv1", "execute", "main", None)
    projection, metrics, text, hit, ms = await mw.projected_context(engine, scope)
    assert not hit and engine.calls == 1
    token = mw.remember_projection(scope, projection, metrics, text, ms)
    try:
        again = await mw.projected_context(engine, scope)
        assert again[3] is True and again[2] == text and engine.calls == 1
        other = await mw.projected_context(engine, RunScope("run1", "inv2", "execute", "main", None))
        assert other[3] is False  # a different invocation never reuses it
        mw.invalidate_projection()
        fresh = await mw.projected_context(engine, scope)
        assert fresh[3] is False and engine.calls == 3
    finally:
        mw.forget_projection(token)
    assert (await mw.projected_context(engine, scope))[3] is False


def test_wake_review_entry_names_only_this_runs_finished_script_jobs(tmp_path):
    store = RuntimeStore(tmp_path / "agent.sqlite3")
    store.put("operation", {"id": "op1", "run_id": "run1", "status": "committed", "receipt": {"data": {"job_id": "job_own"}}})
    store.put("operation", {"id": "op2", "run_id": "other", "status": "committed", "receipt": {"data": {"job_id": "job_other"}}})
    engine = SimpleNamespace(store=store)
    observation = {"jobs": [
        {"ref": {"id": "job_own"}, "body": {"kind": "scripts", "status": "succeeded"}},
        {"ref": {"id": "job_other"}, "body": {"kind": "scripts", "status": "succeeded"}}]}
    entry = RuntimeEngine._ready_script_review_entry(engine, {"id": "run1"}, observation)
    assert entry["script_job_ids"] == ["job_own"]
    assert entry["tool"] == "wf_script_execution" and entry["args"] == {"action": "review", "job_id": "job_own"}
    running = {"jobs": [{"ref": {"id": "job_own"}, "body": {"kind": "scripts", "status": "running"}}]}
    assert RuntimeEngine._ready_script_review_entry(engine, {"id": "run1"}, running) is None


def test_harness_profile_disables_general_purpose_subagent_for_chatgpt_plan_model():
    from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model
    from shengbo_prelive.providers.chatgpt import ChatGPTPlanModel, HARNESS_PROVIDER_KEY as PROVIDER_KEY
    assert PROVIDER_KEY == HARNESS_PROVIDER_KEY
    model = ChatGPTPlanModel(credentials=object())
    assert model._get_ls_params()["ls_provider"] == HARNESS_PROVIDER_KEY
    profile = _harness_profile_for_model(model, None)
    assert profile.general_purpose_subagent is not None and profile.general_purpose_subagent.enabled is False
    assert {"write_file", "edit_file", "execute"} <= set(profile.excluded_tools)


def test_compiled_graph_with_plan_model_has_no_task_tool():
    from deepagents import create_deep_agent
    from shengbo_prelive.providers.chatgpt import ChatGPTPlanModel
    graph = create_deep_agent(model=ChatGPTPlanModel(credentials=object()), tools=[], system_prompt="x")
    names = set()
    for node in graph.nodes.values():
        bound = getattr(node, "bound", None)
        tools_by_name = getattr(bound, "tools_by_name", None) or getattr(getattr(bound, "_tool_node", None), "tools_by_name", None)
        if tools_by_name:
            names |= set(tools_by_name)
    assert names and "task" not in names


def test_usage_details_are_separate_from_strict_usage_triple():
    from shengbo_prelive.providers.chatgpt import _public_usage, _public_usage_details
    raw = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
           "input_tokens_details": {"cached_tokens": 64}, "output_tokens_details": {"reasoning_tokens": 7}}
    assert _public_usage(raw) == {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    assert _public_usage_details(raw) == {"cached_input_tokens": 64, "reasoning_output_tokens": 7}
    assert _public_usage_details({"input_tokens": 1, "input_tokens_details": {"cached_tokens": -1}}) == {}
    assert _public_usage_details(None) == {}


def test_private_summarization_hooks_this_app_overrides_still_exist_in_installed_sdk():
    """Upgrade guard: AuditedSummarization overrides these private SDK methods."""
    from deepagents.middleware.summarization import SummarizationMiddleware
    expected = {"_should_summarize": ["messages", "total_tokens"], "_input_budget": ["request"],
                "_get_profile_limits": [], "_check_reduction": ["original", "reduced", "error"],
                "_acreate_summary": ["messages_to_summarize"], "_determine_cutoff_index": ["messages"],
                "_get_effective_messages": ["request"]}
    for name, params in expected.items():
        method = getattr(SummarizationMiddleware, name, None)
        assert callable(method), name
        assert list(inspect.signature(method).parameters)[1:] == params, name
    assert hasattr(SummarizationMiddleware, "_raise_recovery_exhausted")
    assert "_large_tool_results_prefix" in inspect.getsource(SummarizationMiddleware)


@pytest.mark.parametrize("role", ["product_facts", "session_config", "audience_scenes"])
def test_branch_role_manual_is_pinned_with_its_task(tmp_path, role):
    engine, events = pin_engine(tmp_path, "prepare_session")
    files = RuntimeEngine._initial_files(None, role)
    scope = RunScope("run1", "inv1", "execute", role, "branch1")
    name = f"prelive-{role.replace('_', '-')}"
    assert RuntimeEngine._pin_skills(engine, engine.store.get("run", "run1"), scope, files, (name,),
                                     reason=role, once_key=None) == [name]
    read = next(item for item in engine.store.list("skill_read") if item["branch_task_id"] == "branch1")
    assert read["path"] == f"/skills/{role}/{name}/SKILL.md" and read["role"] == role


class _EchoModel(FakeListChatModel):
    """Answers once; records the messages it was actually sent."""
    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _call(self, messages, stop=None, run_manager=None, **kwargs):
        type(self).seen = list(messages)
        return "done"


@pytest.mark.asyncio
async def test_graph_input_pinned_skills_reach_the_model_before_first_call():
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import InMemorySaver
    files = main_files()
    graph = create_deep_agent(model=_EchoModel(responses=["done"]), tools=[], system_prompt="x",
                              backend=StateBackend(), skills=["/skills/main/"], checkpointer=InMemorySaver())
    await graph.ainvoke({"messages": [HumanMessage(content="开始")], "files": files,
                         "pinned_skills": ["prelive-orchestration", "prelive-script-plan"]},
                        config={"configurable": {"thread_id": "t1"}})
    pinned = [m for m in _EchoModel.seen if getattr(m, "additional_kwargs", {}).get("lc_source") == "pinned_skill"]
    assert [m.additional_kwargs["skill"]["name"] for m in pinned] == ["prelive-orchestration", "prelive-script-plan"]
    assert "## 完整上下文与模块计划" in pinned[1].content


def test_provider_wire_puts_tail_m3_last_as_developer_after_tool_output():
    from langchain_core.messages import ToolMessage
    from shengbo_prelive.providers.chatgpt import ChatGPTPlanModel
    model = ChatGPTPlanModel(credentials=object())
    tail = mw.trusted_context_message(mw.trusted_context_text({"goal": {"objective": "x"}}))
    payload = model._payload([SystemMessage(content="固定系统规则"), HumanMessage(content="开始"),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "read_context", "args": {}}]),
        ToolMessage(content="{}", tool_call_id="c1"), tail])
    items = payload["input"]
    assert items[0]["role"] == "developer" and "shengbo_trusted_context" not in str(items[0])
    assert items[-2]["type"] == "function_call_output"
    assert items[-1]["role"] == "developer" and "shengbo_trusted_context" in str(items[-1])
