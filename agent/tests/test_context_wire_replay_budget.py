"""Opaque protocol occupancy and turn-local budgets, through the native slot."""
import asyncio
from copy import deepcopy
import json
import random
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from shengbo_prelive.models import RunScope
from shengbo_prelive.providers.chatgpt import preview_request_payload
from shengbo_prelive.runtime import CURRENT_SCOPE
from test_context_window_budget import setup


def opaque(index, length):
    rng = random.Random(index)
    return "SYNTHETIC_OPAQUE_" + str(index) + "_" + "".join(rng.choices("ABCDEFGHabcdef0123456789", k=length))


def replay_pair(index, length):
    identity = "wire-replay-" + str(index)
    args = {"source_id": "source-" + str(index)}
    call = {"id": identity, "name": "read_source", "args": args}
    replay = [
        {"type": "reasoning", "id": "reasoning-" + str(index), "encrypted_content": opaque(index, length)},
        {"type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"type": "output_text", "text": "核对这份来源。"}]},
        {"type": "function_call", "call_id": identity, "namespace": "functions", "name": "read_source",
            "arguments": json.dumps(args)},
    ]
    return [AIMessage(content="", tool_calls=[call], additional_kwargs={"responses_replay_items": replay}),
        ToolMessage("商品99元，满180减20；未知库存不能编造。原文标记-" + str(index), tool_call_id=identity)]


def capture_slot(engine):
    saved = {}
    def capture(**kwargs):
        saved.update(kwargs)
    with patch("shengbo_prelive.runtime.create_deep_agent", capture):
        engine._build_graph("main")
    return next(m for m in saved["middleware"] if m.name == "SummarizationMiddleware")


def request(engine, scope, text="仅只读预算检查"):
    return ModelRequest(model=engine.model, messages=[HumanMessage(text)], tools=[], state={"files": {}},
        system_message=SystemMessage("测试系统"), runtime=SimpleNamespace(context=scope))


@pytest.mark.asyncio
async def test_counter_uses_provider_wire_and_does_not_mutate_or_double_count_protocol(setup):
    engine, _, _, _, _ = setup
    messages = replay_pair(4, 8000)
    before = deepcopy(messages)
    # The counter previews with the engine's own model name (DeepSeek by default since 2026-10-22).
    expected = preview_request_payload(messages, [], model_name=engine.model_name)
    assert engine._conversation_wire_tokens(messages) == engine._estimate_payload(expected)
    assert engine._conversation_wire_tokens(messages) > engine._conversation_wire_tokens([
        AIMessage(content="", tool_calls=messages[0].tool_calls), messages[1]]) + 3000
    assert messages == before
    assert sum(i.get("type") == "function_call" for i in expected["input"]) == 1
    assert [i["call_id"] for i in expected["input"] if i.get("type") == "function_call_output"] == ["wire-replay-4"]
    assert next(i for i in expected["input"] if i.get("type") == "message")["phase"] == "commentary"


@pytest.mark.asyncio
async def test_semantic_summary_is_returned_and_retained_replay_groups_stay_complete(setup):
    engine, graph, run, sent, _ = setup
    engine.context_input_token_limit = 48000
    engine.test_summary_text = "真实摘要测试：已核对三份来源，价格99元及活动仍以可信正本为准；继续当前任务。"
    messages = [HumanMessage("之前的有效工作摘要", additional_kwargs={"lc_source": "summarization"}),
        *replay_pair(0, 42000), *replay_pair(1, 12000), *replay_pair(2, 128)]
    before = deepcopy(messages)
    scope = RunScope(run["id"], "opaque-semantic-summary", "execute")
    token = CURRENT_SCOPE.set(scope)
    config = {"configurable": {"thread_id": "opaque-semantic-summary"}, "recursion_limit": 10}
    try:
        await graph.ainvoke({"messages": messages, "files": {}}, config, context=scope)
        snapshot = await graph.aget_state(config)
    finally:
        CURRENT_SCOPE.reset(token)
    assert len(sent) == 2 and not sent[0].get("tools") and sent[1].get("tools")
    assert "<shengbo_summary_rules>" in json.dumps(sent[0]["input"], ensure_ascii=False)
    assert "SYNTHETIC_OPAQUE_" not in json.dumps(sent[0]["input"], ensure_ascii=False), "private protocol data is counted, never sent as semantic summary text"
    assert engine.test_summary_text in json.dumps(sent[1]["input"], ensure_ascii=False)
    assert "Previous conversation was too long to summarize" not in json.dumps(sent[1]["input"])
    assert engine._estimate_payload(sent[1]) <= 48000
    final = sent[1]["input"]
    assert any(i.get("id") == "reasoning-2" for i in final)
    assert sum(i.get("type") == "function_call" and i.get("call_id") == "wire-replay-2" for i in final) == 1
    assert sum(i.get("type") == "function_call_output" and i.get("call_id") == "wire-replay-2" for i in final) == 1
    assert any(i.get("type") == "message" and i.get("phase") == "commentary" for i in final)
    original_ais = [m for m in snapshot.values["messages"] if isinstance(m, AIMessage) and any(
        item.get("type") == "reasoning" for item in m.additional_kwargs.get("responses_replay_items", []))]
    assert len(original_ais) == 3
    assert original_ais[0].additional_kwargs == before[1].additional_kwargs
    event = snapshot.values["_summarization_event"]
    assert event["cutoff_index"] >= 3
    assert event["cutoff_index"] in {3, 5}, "archive only complete AI/Tool groups, preserving a current complete group"
    raw = snapshot.values["files"][event["file_path"]]["content"]
    assert "原文标记-0" in (raw if isinstance(raw, str) else "\n".join(raw))
    developer = json.dumps([i for i in final if i.get("role") == "developer"], ensure_ascii=False)
    assert "sha256:exact-review" in developer and "unknown-audio" in developer and "满180减20" in developer


@pytest.mark.asyncio
async def test_same_role_concurrent_budget_and_exception_reset_are_isolated(setup, monkeypatch):
    engine, _, run, sent, _ = setup
    engine.context_input_token_limit = 48000
    slot = capture_slot(engine)
    async def projected(scope):
        return {"case": scope.invocation_id, "exact": "固定业务信息" * (1 if scope.invocation_id == "small" else 700)}
    monkeypatch.setattr(engine, "project_context", projected)
    monkeypatch.setattr(engine, "allowed_tool_names", lambda scope: set())
    first = request(engine, RunScope(run["id"], "small", "execute"))
    second = request(engine, RunScope(run["id"], "larger", "execute"))
    baseline = (slot._input_budget(first), slot._get_profile_limits())
    reached, both = {}, asyncio.Event()
    async def handler(req):
        key = req.runtime.context.invocation_id
        reached[key] = (slot._input_budget(req), slot._get_profile_limits())
        if len(reached) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        assert reached[key] == (slot._input_budget(req), slot._get_profile_limits())
        return ModelResponse(result=[AIMessage(content="只读预算结果")])
    await asyncio.gather(slot.awrap_model_call(first, handler), slot.awrap_model_call(second, handler))
    assert 0 < reached["larger"][0] < reached["small"][0] < 48000
    assert reached["larger"][0] == reached["larger"][1]
    assert baseline == (slot._input_budget(first), slot._get_profile_limits())
    async def fail(req):
        assert slot._input_budget(req) < 48000
        raise RuntimeError("isolated synthetic failure")
    with pytest.raises(RuntimeError, match="isolated synthetic failure"):
        await slot.awrap_model_call(second, fail)
    assert baseline == (slot._input_budget(first), slot._get_profile_limits())
    assert not sent


@pytest.mark.asyncio
async def test_irreducible_current_protocol_group_blocks_without_paid_retry_or_deleting_replay(setup):
    engine, graph, run, sent, credentials = setup
    engine.context_input_token_limit = 48000
    messages = [HumanMessage("仍有效的旧摘要", additional_kwargs={"lc_source": "summarization"}), *replay_pair(5, 80000)]
    before = deepcopy(messages[1].additional_kwargs)
    scope = RunScope(run["id"], "irreducible-current-group", "execute")
    config = {"configurable": {"thread_id": "irreducible-current-group"}, "recursion_limit": 8}
    token = CURRENT_SCOPE.set(scope)
    try:
        with pytest.raises(ContextOverflowError):
            await graph.ainvoke({"messages": messages, "files": {}}, config, context=scope)
        snapshot = await graph.aget_state(config)
    finally:
        CURRENT_SCOPE.reset(token)
    assert not sent and credentials.calls == 0
    original = next(m for m in snapshot.values["messages"] if isinstance(m, AIMessage))
    assert original.additional_kwargs == before
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "wire-replay-5" for m in snapshot.values["messages"])
