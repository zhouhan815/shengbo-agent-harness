"""Isolated continuity / exact context / real native compaction tests."""
from __future__ import annotations

import json
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from shengbo_contracts.common import ContractError, WorkScope, canonical_hash
from shengbo_prelive.middleware import ContextProjection
from shengbo_prelive.models import IntentAssessment, MessageRequest, RunScope
from shengbo_prelive.runtime import RuntimeEngine, CURRENT_SCOPE


class FakeClient:
    def __init__(self):
        self.commands = []

    async def query(self, key, args=None, **kwargs):
        if key == "read_room":
            return {"ref": {"id": "room1", "revision": 1}, "body": {"name": "测试间", "product_ids": []}}
        if key == "read_preparation":
            return {"ref": {"id": "prep1", "revision": 1}, "body": {"room_id": "room1", "selected_product_refs": []}}
        return {}

    async def command(self, key, args, mutation, **kwargs):
        self.commands.append((key, args, mutation))
        return {"data": {"grant_id": "new-" + args.get("run_id", ""), "write_generation": 1}}

    async def close(self):
        pass


class CapturingModel(FakeMessagesListChatModel):
    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.fixture
async def env(tmp_path):
    client = FakeClient()
    engine = RuntimeEngine(tmp_path, client, model_factory=lambda **kwargs: None)
    yield engine, client
    await engine.stop()


def old_run(engine, *, status="COMPLETED", conversation=None):
    conversation = conversation or engine.create_conversation({"scope": {"kind": "catalog"}})
    work_scope = WorkScope(kind="preparation", preparation_id="prep1").model_dump()
    source = engine.materials.accept(conversation["id"], "facts.txt", "text/plain", "原始资料178元".encode())["material_ref"]
    goal = {"objective": "prepare_session", "summary": "准备旧场次", "scope": work_scope,
        "allowed_effects": ["submit_script_change"], "original_input_hash": "old-hash"}
    prior = engine.store.put("run", {"id": "prior-" + conversation["id"], "conversation_id": conversation["id"],
        "scope": work_scope, "scope_key": canonical_hash(work_scope), "room_id": "room1", "goal": goal,
        "grant_id": "OLD-GRANT-NOT-REUSABLE", "write_generation": 9, "allowed_effects": goal["allowed_effects"],
        "status": status, "phase": "execute", "input": {"text": "准备旧直播", "material_refs": [source]},
        "summary": "旧任务已生成话术", "observed_refs": [], "receipts": [{"command": "submit_script_change", "operation_id": "old-op",
        "status": "committed", "data": {"script_ref": {"kind": "script_revision", "id": "s1", "revision": 1,
        "content_hash": canonical_hash("script")}}}]})
    return conversation, prior, source


@pytest.mark.asyncio
async def test_follow_up_uses_frozen_preparation_and_fresh_goal_grant(env):
    engine, client = env
    conversation, prior, source = old_run(engine)
    request = MessageRequest(text="更新本场主题配置", related_run_id=prior["id"], client_request_id="follow1",
        scope=WorkScope(kind="catalog"))  # Original UI conversation stayed catalog.
    result = await engine.submit_message(conversation["id"], request)
    run = result["run"]
    assert run["scope"] == prior["scope"] and run["room_id"] == "room1"
    assert run["scope_key"] == canonical_hash(prior["scope"])
    assert run["input"]["text"] == "更新本场主题配置"
    assert run["input"]["material_refs"] == [source]
    assert run["input"]["current_material_refs"] == []
    assert not run.get("goal") and not run.get("grant_id") and not run.get("allowed_effects")
    assert run["inherited_context"]["authorization_inherited"] is False
    assert run["inherited_context"]["reusable_refs"][0]["id"] == "s1"
    proposal = IntentAssessment(objective="session_config", summary="仅配置本场", authorization_quote=request.text)
    run = engine.store.update("run", run["id"], intent_assessment=proposal.model_dump())
    await engine._freeze_goal(run)
    saved = engine.store.get("run", run["id"])
    assert saved["goal"]["scope"] == prior["scope"]
    assert "submit_script_change" not in saved["allowed_effects"]
    assert saved["goal"]["original_input_hash"] == canonical_hash(saved["input"])
    assert saved["grant_id"] != prior["grant_id"]
    assert client.commands[-1][2].operation_id == run["id"] + "_grant"
    assert await engine.submit_message(conversation["id"], request) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["follow_up", "replace"])
async def test_cross_conversation_related_run_forbidden(env, mode):
    engine, client = env
    _, prior, _ = old_run(engine)
    other = engine.create_conversation({"scope": {"kind": "catalog"}})
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        await engine.submit_message(other["id"], MessageRequest(text="更新配置", intent_mode=mode, related_run_id=prior["id"]))
    assert client.commands == []


@pytest.mark.asyncio
async def test_active_follow_up_is_not_permanently_queued_and_replace_is_explicit(env, monkeypatch):
    engine, client = env
    conversation, prior, _ = old_run(engine, status="WAITING_USER")
    with pytest.raises(ContractError, match="RELATED_RUN_ACTIVE"):
        await engine.submit_message(conversation["id"], MessageRequest(text="继续吧", related_run_id=prior["id"]))
    assert len(engine.store.list("run")) == 1
    cancellations = []
    async def cancel(identity, outcome="CANCELLED"):
        cancellations.append((identity, outcome))
        return engine.store.update("run", identity, status=outcome)
    monkeypatch.setattr(engine, "cancel_run", cancel)
    request = MessageRequest(text="替换：只分析", intent_mode="replace", related_run_id=prior["id"], client_request_id="replace1")
    result = await engine.submit_message(conversation["id"], request)
    assert result["run"]["scope"] == prior["scope"]
    assert cancellations == [(prior["id"], "SUPERSEDED")]
    assert await engine.submit_message(conversation["id"], request) == result
    assert len(cancellations) == 1


@pytest.mark.asyncio
async def test_projection_preserves_anchors_and_full_bodies_are_exactly_readable(env):
    engine, _ = env
    conversation, prior, _ = old_run(engine, status="RUNNING")
    report = {"summary": "价格调整的分析", "facts": [{"path": "/price", "value": 107, "currency": "TWD"}],
        "body": "大报告正文，不能自动反复注入" * 4000}
    engine.store.put("branch", {"id": "branch1", "run_id": prior["id"], "role": "product_facts", "status": "PREPARED",
        "report": report, "report_hash": canonical_hash(report)})
    preview = {"texts": ["长文案" * 3000], "price": 107, "currency": "TWD"}
    question = engine.store.put("question", {"id": "q1", "run_id": prior["id"], "kind": "text_audio_approval", "status": "OPEN",
        "prompt": "确认以107新台币生成语音？", "options": [{"id": "approve", "label": "确认"}], "preview": preview,
        "approval_binding": {"review_hash": "sha256:fixed", "generation_epoch": 3}})
    engine.store.put("operation", {"id": "unknown1", "run_id": prior["id"], "status": "unknown", "key": "prepare_required_audio", "args": {"price": 107}})
    scope = RunScope(prior["id"], "invoke1", "execute")
    projected = await engine.project_context(scope)
    assert projected["scope"] == prior["scope"] and projected["goal"] == prior["goal"]
    assert projected["allowed_effects"] == prior["allowed_effects"]
    assert projected["pending_questions"][0]["prompt"] == question["prompt"]
    assert projected["pending_questions"][0]["approval_binding"] == question["approval_binding"]
    assert projected["unknown_side_effects"]["operations"][0]["status"] == "unknown"
    assert len(json.dumps(projected, ensure_ascii=False)) < 15000
    branch = projected["branches"]["items"][0]
    assert "report" not in branch
    assert any(a["value"] == {"path": "/price", "value": 107, "currency": "TWD"} for a in branch["financial_values"])
    assert engine.run_history(scope, {"full": True, "ref": branch["report_ref"]})["document"]["branch"]["report"] == report
    assert engine.run_history(scope, {"full": True, "ref": projected["pending_questions"][0]["preview"]["full_ref"]})["document"]["preview"] == preview
    index = engine.run_history(scope, {})
    full = engine.run_history(scope, {"full": True, "ref": index["full_ref"]})["document"]["history"]
    assert full["branches"]["items"][0]["report"] == report and full["questions"][0]["preview"] == preview
    other = engine.create_conversation({"scope": {"kind": "catalog"}})
    engine.store.put("run", {"id": "other-run", "conversation_id": other["id"], "scope": other["scope"], "input": {"text": "另一会话"}})
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        engine.run_history(RunScope("other-run", "x", "intent"), {"full": True, "ref": branch["report_ref"]})
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        engine.run_history(scope, {"context": {"run_id": "other-run"}})
    tampered = {**branch["report_ref"], "content_hash": canonical_hash("wrong")}
    with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
        engine.run_history(scope, {"full": True, "ref": tampered})


@pytest.mark.asyncio
async def test_projection_deduplicates_exact_amounts_without_merging_conditions(env):
    engine, _ = env
    _, run, _ = old_run(engine, status="RUNNING")
    report = {"summary": "活动核验", "price": 107, "evidence": {"price": 107},
        "campaign": "满100元减20元。", "repeated": "满100元减20元。",
        "distinct": "满200元减20元。", "old_price": 178}
    engine.store.put("branch", {"id": "dedup-branch", "run_id": run["id"],
        "role": "product_facts", "status": "PREPARED", "report": report,
        "report_hash": canonical_hash(report)})
    projected = await engine.project_context(RunScope(run["id"], "dedup-projection", "execute"))
    branch = projected["branches"]["items"][0]
    amounts = branch["financial_values"]
    amount = [item for item in amounts if item["value"] == 107]
    assert len(amount) == 1
    assert {amount[0]["path"], *amount[0]["also_at"]} == {"/price", "/evidence/price"}
    assert sum(item["value"] == "满100元减20元。" for item in amounts) == 1
    assert any(item["value"] == "满200元减20元。" for item in amounts)
    assert any(item["value"] == 178 for item in amounts)
    assert engine.run_history(RunScope(run["id"], "dedup-read", "execute"),
        {"full": True, "ref": branch["report_ref"]})["document"]["branch"]["report"] == report


@pytest.mark.asyncio
async def test_native_compaction_runs_before_24k_and_preserves_raw_history_and_exact_m3(env):
    engine, _ = env
    engine.context_input_token_limit = 24000
    conversation, prior, _ = old_run(engine, status="RUNNING")
    engine.store.put("question", {"id": "pending", "run_id": prior["id"], "kind": "question", "status": "OPEN",
        "prompt": "不可丢失的待确认107元", "options": []})
    model = CapturingModel(responses=[AIMessage(content="有意不含金额的短摘要"), AIMessage(content="测试完成"), AIMessage(content="继续完成")])
    engine.model, engine.saver = model, InMemorySaver()
    # Keep real native Deep Agents graph, filesystem/checkpointer, summarizer
    # and M3. Skip business completion guards only: this tests compaction, not
    # fake goal completion, and cannot call a provider or a business writer.
    with patch("shengbo_prelive.runtime.main_middleware", lambda e: [ContextProjection(e)]):
        graph = engine._build_graph("main")
    old_text = "不可丢失的原始历史标记。" + "详细历史内容需要原样保存。" * 400
    messages = [HumanMessage(content=old_text + str(n)) for n in range(10)]
    config = {"configurable": {"thread_id": "compaction-test"}, "recursion_limit": 10}
    scope = RunScope(prior["id"], "compact-invoke", "execute")
    token = CURRENT_SCOPE.set(scope)
    try:
        result = await graph.ainvoke({"messages": messages, "files": {}}, config, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)
    snapshot = await graph.aget_state(config)
    event = snapshot.values["_summarization_event"]
    assert event["cutoff_index"] > 0 and event["file_path"]
    offloaded = snapshot.values["files"][event["file_path"]]["content"]
    assert "不可丢失的原始历史标记" in (offloaded if isinstance(offloaded, str) else "\n".join(offloaded))
    assert snapshot.values["messages"][0].content == messages[0].content
    assert len(model.seen) == 2, "one native summary call followed by one compacted model call"
    summary_prompt = model.seen[0][0].content
    assert "未知业务副作用/未知付费结果" in summary_prompt
    sent = model.seen[1]
    assert "有意不含金额的短摘要" in sent[1].content
    # Since 2026-10-22 M3 is split: phase-stable fields (scope, Goal, authority)
    # sit in the cached system prefix, changing state is the last message.
    # The exact facts must survive compaction across the two parts.
    def text(message):
        content = message.content
        return content if isinstance(content, str) else "\n".join(block.get("text", "") for block in content)
    assert "<shengbo_trusted_context>" not in text(sent[0])
    assert "<shengbo_trusted_context_stable>" in text(sent[0])
    assert "<shengbo_trusted_context>" in text(sent[-1])
    system = text(sent[0]) + text(sent[-1])
    assert "不可丢失的待确认107元" in system
    assert '"preparation_id": "prep1"' in system
    assert '"allowed_effects": ["submit_script_change"]' in system
    summary_events = [e for e in engine.events(prior["id"])["events"] if e["type"] == "context.summarized"]
    assert len(summary_events) == 1
    assert summary_events[0]["data"]["compressible_tokens_before"] > summary_events[0]["data"]["compressible_tokens_after"]
    assert summary_events[0]["data"]["trigger_tokens"] == 12000
    assert result["messages"][-1].content == "测试完成"
    token = CURRENT_SCOPE.set(scope)
    try:
        await graph.ainvoke({"messages": [HumanMessage(content="只读继续") ]}, config, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)
    assert len(model.seen) == 3, "raw checkpoint history must not cause repeated summarization"
    assert len([e for e in engine.events(prior["id"])["events"] if e["type"] == "context.summarized"]) == 1


@pytest.mark.asyncio
async def test_real_large_tool_schema_does_not_trigger_short_history_compaction(env):
    from langchain_core.utils.function_calling import convert_to_openai_tool
    import tiktoken
    from shengbo_prelive.tools import build_tools
    engine, _ = env
    _, prior, _ = old_run(engine, status="RUNNING")
    model = CapturingModel(responses=[AIMessage(content="短历史只调用一次")])
    engine.model, engine.saver = model, InMemorySaver()
    schemas = [convert_to_openai_tool(t) for t in build_tools(engine, "main")]
    schema_tokens = len(tiktoken.get_encoding("o200k_base").encode(json.dumps(schemas, ensure_ascii=False)))
    assert schema_tokens > 10000, "exercise the actual large business tool set, not a tiny stub"
    with patch("shengbo_prelive.runtime.main_middleware", lambda e: [ContextProjection(e)]):
        graph = engine._build_graph("main")
    scope = RunScope(prior["id"], "short-schema-invoke", "execute")
    token = CURRENT_SCOPE.set(scope)
    config = {"configurable": {"thread_id": "static-schema-test"}}
    try:
        # Provider usage also includes fixed tool/system tokens. It must not
        # re-trigger compaction after the precise message counter says small.
        prior_message = AIMessage(content="已读取商品", usage_metadata={"input_tokens": 15000, "output_tokens": 20, "total_tokens": 15020},
            response_metadata={"model_provider": "openai"})
        await graph.ainvoke({"messages": [HumanMessage(content="分析商品"), prior_message, HumanMessage(content="继续")], "files": {}}, config, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)
    snapshot = await graph.aget_state(config)
    assert not snapshot.values.get("_summarization_event")
    assert len(model.seen) == 1
    assert not any(e["type"] == "context.summarized" for e in engine.events(prior["id"])["events"])


@pytest.mark.asyncio
async def test_answered_text_approval_projects_duplicate_review_but_keeps_exact_verified_binding(env):
    engine, _ = env
    _, prior, _ = old_run(engine, status="RUNNING")
    product_ref = {"kind": "product_revision", "id": "p1", "revision": 3, "content_hash": canonical_hash("p1")}
    review = {"review_hash": "sha256:review", "preparation_id": "prep1", "generation_epoch": 3,
        "voice": {"provider": "local", "model": "windows", "voice": "Huihui"}, "texts": ["本场价格107元。" + "完整文案。" * 4000],
        "product_refs": [product_ref], "cost_scope": {"price": 107, "currency": "TWD"}}
    verified = {"status": "current", "approval_ref": {"kind": "text_approval", "id": "approval1", "revision": 1, "content_hash": canonical_hash("approval1")},
        "approval": {"actor_id": "human1", "approved_at": "2026-10-16T00:00:00Z", "operation_id": "human-op1",
            "review_hash": review["review_hash"], "preparation_id": "prep1", "bound_review": review}}
    question = engine.store.put("question", {"id": "answered-approval", "run_id": prior["id"], "kind": "text_audio_approval", "status": "ANSWERED",
        "answer": "approve", "answer_event_ref": "answer1", "prompt": "确认此完整文本？", "options": [],
        "preview": {**review, "revision_currentness": "current"}, "verified_human_receipt": verified,
        "approval_binding": {"review_hash": "sha256:review", "operation_id": "human-op1", "generation_epoch": 3}})
    scope = RunScope(prior["id"], "verified-projection", "execute")
    projection = await engine.project_context(scope)
    decision = projection["decisions"][0]
    receipt = decision["verified_human_receipt"]
    assert decision["approval_binding"] == question["approval_binding"]
    assert decision["answer"] == "approve" and decision["answer_event_ref"] == "answer1"
    assert receipt["status"] == "current" and receipt["approval_ref"] == verified["approval_ref"]
    assert receipt["approval"] == {**{key: value for key, value in verified["approval"].items() if key != "bound_review"},
        "bound_review_hash": canonical_hash(review), "bound_review_binding": {key: review[key] for key in ("preparation_id", "review_hash", "generation_epoch", "voice")}}
    assert "bound_review_anchors" not in receipt
    assert engine.run_history(scope, {"full": True, "ref": receipt["full_ref"]})["document"]["receipt"] == verified
    assert product_ref in decision["preview"]["exact_refs"]
    assert any(value["value"] == 107 for value in decision["preview"]["financial_values"])
    # Distinct verified evidence must retain its own exact amount anchors.
    verified["approval"]["bound_review"] = {**review, "cost_scope": {"price": 108, "currency": "TWD"}}
    engine.store.update("question", question["id"], verified_human_receipt=verified)
    changed = (await engine.project_context(scope))["decisions"][0]["verified_human_receipt"]
    assert any(value["value"] == 108 for value in changed["bound_review_anchors"]["financial_values"])


@pytest.mark.asyncio
async def test_history_sections_are_bounded_indexes_and_full_ref_remains_exact(env):
    engine, _ = env
    _, prior, _ = old_run(engine, status="RUNNING")
    large = "只在完整快照读取的正文" * 5000
    engine.store.update("run", prior["id"], input={**prior["input"], "text": large},
        artifacts=[{"kind": "script_revision", "id": str(index), "revision": 1, "content_hash": canonical_hash(index)} for index in range(25)])
    engine.store.put("branch", {"id": "branch-index", "run_id": prior["id"], "role": "product_facts", "status": "PREPARED",
        "report_hash": canonical_hash(large), "report": {"summary": "已整理", "body": large}})
    engine.store.put("operation", {"id": "op-index", "run_id": prior["id"], "key": "save_product", "status": "unknown", "args": {"document": large}})
    scope = RunScope(prior["id"], "history-sections", "execute")
    selected = engine.run_history(scope, {"sections": ["artifacts"], "cursor": 10, "limit": 4})
    assert set(selected) == {"run_id", "conversation_id", "sections", "full_ref", "authorization_note", "artifacts"}
    assert [item["id"] for item in selected["artifacts"]["items"]] == ["10", "11", "12", "13"]
    assert selected["artifacts"]["next_cursor"] == 14 and selected["artifacts"]["total"] == 25
    index = engine.run_history(scope, {"sections": ["history", "artifacts", "analysis_reports", "operations"]})
    assert len(json.dumps(index, ensure_ascii=False)) < 9000 and large not in json.dumps(index, ensure_ascii=False)
    assert index["analysis_reports"]["items"][0]["summary"] == "已整理"
    assert "args" not in index["operations"]["items"][0] and "input" not in index and "goal" not in index
    full = engine.run_history(scope, {"full": True, "ref": index["full_ref"]})["document"]["history"]
    assert full["input"]["text"] == large and full["branches"]["items"][0]["report"]["body"] == large
    assert full["operations"][0]["args"]["document"] == large
    with pytest.raises(ContractError, match="HISTORY_SECTION_INVALID"):
        engine.run_history(scope, {"sections": ["invented"]})


@pytest.mark.asyncio
async def test_default_48k_final_wire_fence_is_bounded_and_reserves_only_allowed_attempts(env, monkeypatch):
    engine, _ = env
    _, prior, _ = old_run(engine, status="RUNNING")
    assert engine.context_input_token_limit == 48000
    assert engine.context_summary_trigger_tokens == 12000 and engine.context_summary_keep_tokens == 3000
    token = CURRENT_SCOPE.set(RunScope(prior["id"], "bounded-48k", "execute"))
    try:
        for estimate in (24001, 48000):
            monkeypatch.setattr(engine, "_estimate_payload", lambda payload, value=estimate: value)
            engine._before_send({})
        monkeypatch.setattr(engine, "_estimate_payload", lambda payload: 48001)
        with pytest.raises(ContractError, match="CONTEXT_LIMIT") as failure:
            engine._before_send({})
        assert failure.value.details == {"estimated_input_tokens": 48001, "limit": 48000}
        with engine.store.tx() as db:
            assert db.execute("SELECT COUNT(*) FROM attempts WHERE run_id=?", (prior["id"],)).fetchone()[0] == 2
    finally:
        CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_failed_or_invalid_skill_read_is_never_recorded_as_loaded(env):
    engine, _ = env
    _, prior, _ = old_run(engine, status="RUNNING")
    scope = RunScope(prior["id"], "invalid-skill-read", "execute")
    files = engine._initial_files("main")
    path = next(iter(files))
    for args, result in [({"file_path": {}}, ToolMessage("invalid argument", tool_call_id="bad", status="error")),
                         ({"file_path": path}, ToolMessage("error: missing file", tool_call_id="bad", status="error")),
                         ({"file_path": path}, ToolMessage("unknown non-native response", tool_call_id="bad"))]:
        engine.record_skill_read(scope, args, result, files)
    assert not (await engine.project_context(scope))["read_skills"]["items"]


def history_revalidation_runs(engine):
    conversation, prior, source = old_run(engine, status="BLOCKED")
    report = {"summary": "旧资料事实候选", "product_ids": [], "fields": {}, "source_refs": [source]}
    branch = engine.store.put("branch", {"id": "old-prepared", "run_id": prior["id"], "role": "product_facts", "status": "PREPARED",
        "report": report, "report_hash": canonical_hash(report), "task": {"role": "product_facts", "instruction": "旧任务分析", "product_ids": [], "source_refs": [source], "input_refs": []}})
    current = engine.store.put("run", {"id": "current-revalidation", "conversation_id": conversation["id"], "scope": prior["scope"],
        "phase": "execute", "status": "RUNNING", "input": {"text": "按当前目标重新核对这份资料", "material_refs": [source]},
        "goal": {"objective": "facts_analysis", "summary": "当前资料事实核对", "required_analyses": ["product_facts"]},
        "allowed_effects": ["accept_analysis_report"], "allowed_tools": [], "task_assessment": {"summary": "复核当前证据", "capabilities": ["product_facts"]},
        "target_product_ids": [], "receipts": [], "grant_id": "FRESH-CURRENT-GRANT", "write_generation": 1})
    return prior, branch, current, source


@pytest.mark.asyncio
async def test_sdk_old_prepared_report_is_corrected_then_current_run_branch_is_dispatched(env):
    engine, client = env
    prior, branch, current, source = history_revalidation_runs(engine)
    model = CapturingModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "accept-old", "name": "accept_analysis_reports", "args": {"branch_task_ids": [branch["id"]]}}]),
        AIMessage(content="", tool_calls=[{"id": "revalidate-current", "name": "dispatch_analysis_batch", "args": {"reason": "旧报告仅作证据，当前目标需重新验证", "tasks": [
            {"role": "product_facts", "instruction": "按当前Goal与本次资料复核旧候选，不继承旧授权", "source_refs": [source], "product_ids": [], "input_refs": []}]}}]),
        AIMessage(content="", tool_calls=[{"id": "wait-current", "name": "finish_turn", "args": {"outcome": "WAITING_JOBS", "summary": "已派发本次复核，等待真实候选"}}])])
    engine.model, engine.saver = model, InMemorySaver()
    graph = engine._build_graph("main")
    scope = RunScope(current["id"], "sdk-revalidate", "execute")
    token = CURRENT_SCOPE.set(scope)
    try:
        result = await graph.ainvoke({"messages": [HumanMessage("验收本次分析")], "files": engine._initial_files("main")},
            {"configurable": {"thread_id": "report-revalidation"}, "recursion_limit": 40}, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)
    error = next(message for message in result["messages"] if isinstance(message, ToolMessage) and message.tool_call_id == "accept-old")
    detail = json.loads(error.content)
    assert error.status == "error" and detail["error"]["code"] == "REPORT_REQUIRES_CURRENT_RUN_REVALIDATION"
    assert detail["error"]["details"]["authorization_inherited"] is False
    assert detail["correction_count"] == 1 and detail["correction_limit"] == 3
    new = [item for item in engine.store.list("branch") if item["run_id"] == current["id"]]
    assert len(new) == 1 and new[0]["status"] == "QUEUED" and new[0]["id"] != branch["id"]
    assert engine.store.get("branch", branch["id"])["status"] == "PREPARED"
    assert not engine.store.get("branch", branch["id"]).get("accepted_ref")
    assert engine.store.get("run", current["id"])["grant_id"] == "FRESH-CURRENT-GRANT"
    assert len(model.seen) == 3 and not client.commands and not engine.store.list("operation")


@pytest.mark.asyncio
async def test_history_report_acceptability_is_explicit_and_foreign_or_forged_ids_stay_hard(env):
    engine, client = env
    prior, branch, current, _ = history_revalidation_runs(engine)
    scope = RunScope(current["id"], "report-boundaries", "execute")
    index = engine.run_history(scope, {"context": {"run_id": prior["id"]}, "sections": ["analysis_reports"]})
    item = index["analysis_reports"]["items"][0]
    assert item["owner_run_id"] == prior["id"] and item["can_accept_now"] is False
    assert "不能直接接受旧branch" in item["next_step"]
    assert engine.run_history(scope, {"full": True, "ref": item["full_ref"]})["document"]["branch"] == branch
    other = engine.create_conversation({"scope": {"kind": "catalog"}})
    engine.store.put("run", {**prior, "id": "foreign-run", "conversation_id": other["id"]})
    engine.store.put("branch", {**branch, "id": "foreign-branch", "run_id": "foreign-run"})
    for ids in [["fabricated"], ["foreign-branch"], [branch["id"], "foreign-branch"], [branch["id"], "fabricated"]]:
        with pytest.raises(ContractError, match="BRANCH_SCOPE_DENIED") as failure:
            await engine.accept_reports(scope, ids, "denied")
        assert failure.value.status == 403
    engine.store.update("branch", branch["id"], report_hash="sha256:forged")
    with pytest.raises(ContractError, match="BRANCH_SCOPE_DENIED") as failure:
        await engine.accept_reports(scope, [branch["id"]], "forged-hash")
    assert failure.value.status == 403 and not client.commands and not engine.store.list("operation")


@pytest.mark.asyncio
async def test_sdk_old_report_revalidation_correction_is_limited_to_three_attempts(env):
    engine, client = env
    _, branch, current, _ = history_revalidation_runs(engine)
    model = CapturingModel(responses=[AIMessage(content="", tool_calls=[{"id": "repeat-old-report-" + str(index), "name": "accept_analysis_reports",
        "args": {"branch_task_ids": [branch["id"]]}}]) for index in range(4)])
    engine.model, engine.saver = model, InMemorySaver()
    graph = engine._build_graph("main")
    scope = RunScope(current["id"], "bounded-report-correction", "execute")
    token = CURRENT_SCOPE.set(scope)
    try:
        with pytest.raises(ContractError, match="REPORT_REQUIRES_CURRENT_RUN_REVALIDATION") as failure:
            await graph.ainvoke({"messages": [HumanMessage("核对旧报告")], "files": engine._initial_files("main")},
                {"configurable": {"thread_id": "bounded-report-revalidation"}, "recursion_limit": 80}, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)
    assert failure.value.status == 409 and len(model.seen) == 4
    assert engine.store.get("run", current["id"])["correctable_tool_errors"]["REPORT_REQUIRES_CURRENT_RUN_REVALIDATION"] == 3
    assert not client.commands and not engine.store.list("operation")
