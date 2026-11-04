"""Durable public-answer and native checkpoint recovery; no live providers."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import patch

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from shengbo_contracts.common import ContractError, WorkScope, canonical_hash
from shengbo_prelive.api import create_app
from shengbo_prelive.decision_presentation import project_decision
from shengbo_prelive.models import AnswerRequest, DecisionRequest, RunScope
from shengbo_prelive.runtime import RuntimeEngine
from shengbo_prelive.store import RuntimeStore, TERMINAL
from shengbo_prelive.tools import build_tools
from test_runtime_business import BindableFakeModel, env, new_run, prepared_text, stop_at_interrupt


class IsolatedBusiness:
    def __init__(self):
        self.queries = []
        self.commands = []

    async def close(self):
        pass

    async def query(self, key, args=None, **kwargs):
        self.queries.append((key, deepcopy(args)))
        raise AssertionError(f"Unexpected business query: {key}")

    async def command(self, key, args, mutation, **kwargs):
        self.commands.append(key)
        raise AssertionError("Recovery must not replay business writes")


def saved_run(engine, *, status="RUNNING", phase="execute", **changes):
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    scope = WorkScope(kind="catalog").model_dump()
    source = {"text": "请核对当前资料，遇到不明确的信息询问我。", "material_refs": []}
    goal = {"objective": "read_explain", "summary": "核对当前资料", "scope": scope,
            "target_product_ids": [], "allowed_effects": [], "original_input_hash": canonical_hash(source)}
    return engine.store.put("run", {"id": "run-durable-decision", "conversation_id": conversation["id"],
        "scope": scope, "scope_key": canonical_hash(scope), "input": source, "status": status,
        "phase": phase, "dispatch_frozen": False, "goal": goal if phase == "execute" else None,
        "allowed_effects": [], "target_product_ids": [], "receipts": [], **changes})


class DecisionOpened(Exception):
    pass


async def open_question(engine, run, *, call_id="ask-facts", kind="fact_check"):
    with patch("shengbo_prelive.runtime.interrupt", side_effect=DecisionOpened):
        with pytest.raises(DecisionOpened):
            await engine.request_decision(RunScope(run["id"], "audit", run["phase"]),
                {"kind": kind, "prompt": "请确认材质以当前资料为准。"}, call_id)
    return engine.store.list("question")[-1]


def request(question, *, answer="按当前资料核对", request_id="answer-once"):
    return AnswerRequest(question_revision=question["revision"], answer=answer, client_request_id=request_id)


@pytest.fixture
async def isolated(tmp_path):
    engine = RuntimeEngine(tmp_path / "agent", IsolatedBusiness(), model_factory=lambda **kwargs: None)
    try:
        yield engine
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_catalog_question_uses_business_labels_and_retains_reply_contract(isolated):
    engine = isolated
    run = saved_run(engine)
    prompt = "【SKU结构】两个在售SKU。日常售价→price；直播售价→promo_price。缺少image，shelf_life/ingredients不适用。"
    option = {"id": "split_skus", "label": "拆分为2个SKU商品，按日常价=price、直播价=promo_price保存"}
    with patch("shengbo_prelive.runtime.interrupt", side_effect=DecisionOpened):
        with pytest.raises(DecisionOpened):
            await engine.request_decision(RunScope(run["id"], "audit", run["phase"]),
                {"kind": "catalog_confirm", "prompt": prompt, "options": [option]}, "catalog-options")
    view = engine.get_run(run["id"])
    question = view["questions"][0]
    assert question["allow_text"] is True
    assert question["prompt"] == view["summary"]
    assert "商品图片，保质期和配料不适用" in question["prompt"]
    assert "本场活动价需在直播间单独设置" in question["prompt"]
    assert question["options"] == [{"value": "split_skus", "label": "拆分为2个规格商品，按日常售价和促销参考价保存"}]
    assert engine.store.get("question", question["id"])["prompt"] == prompt
    custom = {"text": "先预览标准装的知识文档，暂不入库。"}
    await engine.answer(question["id"], request(question, answer=custom))
    assert engine.store.get("question", question["id"])["answer"] == custom
    assert not engine.client.commands


def test_decision_tool_advertises_required_label_and_value():
    tool = next(tool for tool in build_tools(None) if tool.name == "request_user_decision")
    items = convert_to_openai_tool(tool)["function"]["parameters"]["properties"]["options"]["items"]
    assert set(items["properties"]) == set(items["required"]) == {"value", "label"}
    request = DecisionRequest.model_validate({"kind": "catalog_confirm", "prompt": "确认入库方式",
        "options": [{"save_both": "标准装与加量装分别入库"}, {"id": "save_standard", "label": "只保存标准装"},
                    {"choice_id": "skip", "title": "暂不入库"}]})
    assert request.options == [
        {"value": "save_both", "label": "标准装与加量装分别入库"},
        {"value": "save_standard", "label": "只保存标准装"}, {"value": "skip", "label": "暂不入库"}]


@pytest.mark.asyncio
async def test_historical_single_key_options_project_and_answer_without_rewriting(isolated):
    engine = isolated
    run = saved_run(engine, status="WAITING_USER")
    raw = [{"save_both": "标准装与加量装分别入库"}, {"save_standard": "只保存标准装"}, {"skip": "暂不入库"}]
    question = engine.store.put("question", {"id": "legacy-options", "run_id": run["id"], "status": "OPEN",
        "kind": "catalog_confirm", "prompt": "确认入库方式", "options": raw})
    public = engine.get_conversation(run["conversation_id"])["runs"][0]["questions"][0]
    assert [option["value"] for option in public["options"]] == ["save_both", "save_standard", "skip"]
    assert [option["label"] for option in public["options"]] == [next(iter(option.values())) for option in raw]
    await engine.answer(question["id"], request(public, answer=public["options"][1]["value"]))
    stored = engine.store.get("question", question["id"])
    assert stored["options"] == raw and stored["answer"] == "save_standard"


@pytest.mark.parametrize("options", [
    [{"value": "one", "label": "标准装"}, {"value": "one", "label": "加量装"}],
    [{"one": "标准装", "two": "加量装"}],
    [{"choice_id": "只保存标准装"}],
    [{"label": "只保存标准装"}],
    [{"value": "one", "label": "选项 1"}],
    [{"value": "one", "label": "Option 1"}],
    [{"value": "one", "label": "1"}],
    [{"value": "one", "label": "只保存标准装", "requires_human_action": "true"}],
])
def test_decision_tool_rejects_ambiguous_or_placeholder_options(options):
    with pytest.raises(ValidationError):
        DecisionRequest.model_validate({"kind": "catalog_confirm", "prompt": "确认入库方式", "options": options})


@pytest.mark.asyncio
async def test_visible_labels_must_remain_distinct_after_display_translation(isolated):
    engine = isolated
    run = saved_run(engine)
    with pytest.raises(ContractError, match="DECISION_INPUT_INVALID"):
        await engine.request_decision(RunScope(run["id"], "audit", run["phase"]),
            {"kind": "catalog_confirm", "prompt": "选择报价", "options": [
                {"value": "one", "label": "price"}, {"value": "two", "label": "日常售价"}]}, "ambiguous-options")
    assert engine.store.list("question") == []


def test_legacy_option_action_metadata_is_exposed_only_with_exact_server_binding():
    action = {"command": {"command": "approve_text_for_audio", "args": {
        "preparation_id": "prep-1", "review_hash": "hash-1"}, "mutation": {"operation_id": "op-1"}}}
    binding = {"preparation_id": "prep-1", "review_hash": "hash-1", "operation_id": "op-1"}
    question = {"kind": "text_audio_approval", "prompt": "确认生成语音", "approval_binding": binding,
                "human_action": action, "options": [{"choice_id": "approve", "title": "确认生成语音"}]}
    assert project_decision(question)["human_action"] == action
    wrong = project_decision({**question, "approval_binding": {**binding, "operation_id": "wrong"}})
    assert "human_action" not in wrong and wrong["options"][0]["requires_human_action"]
    generic = project_decision({**question, "kind": "catalog_confirm", "options": [
        {"value": "approve", "label": "只保存标准装", "human_action": action, "requires_human_action": True}]})
    assert "human_action" not in generic
    assert generic["options"] == [{"value": "approve", "label": "只保存标准装"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,choice", [
    ("text_audio_approval", "edit_text"),
    ("script_audio_preview_approval", "reject"),
    ("playlist_order", "revise"),
    ("product_identity", "revise"),
    ("new_product_import", "revise"),
    ("task_scope", "revise"),
])
async def test_fixed_decisions_accept_written_revisions_without_approval(isolated, kind, choice):
    engine = isolated
    controller_question = kind in {"new_product_import", "task_scope"}
    run = saved_run(engine, status="WAITING_USER", phase="intent" if controller_question else "execute")
    context = {}
    if kind == "new_product_import":
        context = engine._new_product_import_binding(run)
    elif kind == "task_scope":
        context = {"resolution_kind": "room_selection"}
    question = engine.store.put("question", {"id": "edit-proposal", "run_id": run["id"], "status": "OPEN",
        "kind": kind, "prompt": "请确认本次提案。", "context": context, "controller_question": controller_question})
    public = engine.get_run(run["id"])["questions"][0]
    assert public["allow_text"] is False
    assert public["revision_text"]["choice"] == choice
    with pytest.raises(ContractError, match="ANSWER_INVALID"):
        await engine.answer(question["id"], request(question, answer={"choice": "approve", "text": "批一下"}))
    comment = {"choice": choice, "text": "请调整后把新提案给我看看。"}
    await engine.answer(question["id"], request(question, answer=comment))
    answered = engine.store.get("question", question["id"])
    assert answered["answer"] == comment and answered["verified_human_receipt"] is None
    if kind == "task_scope":
        engine.store.update("run", run["id"], status="WAITING_USER", resume_resolution=None)
        await engine._recover_decision_waits(run_id=run["id"])
        assert engine.get_run(run["id"])["status"] == "ASSESSING_INTENT"
    assert not engine.client.queries and not engine.client.commands


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_closed_run_cannot_project_or_resume_open_question(isolated, legacy):
    engine = isolated
    run = saved_run(engine)
    question = await open_question(engine, run)
    if legacy:
        engine.store.update("run", run["id"], status="CANCELLED", dispatch_frozen=True)
    else:
        engine.store.close_run(run["id"], "CANCELLED", {})
    assert engine.get_run(run["id"])["questions"] == []
    assert engine.get_conversation(run["conversation_id"])["runs"][0]["questions"] == []
    assert engine.store.get("question", question["id"])["status"] == ("OPEN" if legacy else "SUPERSEDED")
    with pytest.raises(ContractError, match="DECISION_STALE|DECISION_CLOSED"):
        await engine.answer(question["id"], request(question))


@pytest.mark.asyncio
async def test_crash_after_question_sql_before_run_sql_rolls_back_both(isolated):
    engine = isolated
    run = saved_run(engine)
    question = await open_question(engine, run)
    original = RuntimeStore._put

    def crash_at_run_write(db, kind, body):
        if kind == "run" and body.get("status") == "RUNNING":
            raise RuntimeError("simulated process exit before resume inbox write")
        return original(db, kind, body)

    with patch.object(engine.store, "_put", side_effect=crash_at_run_write):
        with pytest.raises(RuntimeError, match="simulated process exit"):
            await engine.answer(question["id"], request(question))
    assert engine.store.get("question", question["id"])["status"] == "OPEN"
    assert engine.store.get("run", run["id"])["status"] == "WAITING_USER"
    result = await engine.answer(question["id"], request(question))
    assert result["run"]["resume_resolution"]["answer_event_ref"] == result["answer_event_ref"]
    assert not engine.client.commands


@pytest.mark.asyncio
async def test_lost_response_after_commit_retries_through_both_public_routes(isolated):
    engine = isolated
    run = saved_run(engine)
    question = await open_question(engine, run)
    original_emit = engine._emit

    def lost_response(run_id, kind, data, scope=None):
        if kind == "decision.answered":
            raise RuntimeError("simulated response connection loss")
        return original_emit(run_id, kind, data, scope)

    with patch.object(engine, "_emit", side_effect=lost_response):
        with pytest.raises(RuntimeError, match="connection loss"):
            await engine.answer(question["id"], request(question))
    committed = engine.store.get("question", question["id"])
    assert committed["status"] == "ANSWERED"
    token = "isolated-decision-service-token"
    transport = httpx.ASGITransport(app=create_app(engine, token))
    async with httpx.AsyncClient(transport=transport, base_url="http://isolated",
                                headers={"X-Shengbo-Service-Token": token}) as client:
        body = request(question).model_dump(mode="json")
        for path in (f"/decisions/{question['id']}/answers", f"/runs/{run['id']}/answers"):
            response = await client.post("/api/assistant/v2" + path,
                                         json={**body, "decision_id": question["id"]})
            assert response.status_code == 200, response.text
            assert response.json()["answer_event_ref"] == committed["answer_event_ref"]
        engine.store.put("run", {**run, "id": "foreign-run"})
        response = await client.post("/api/assistant/v2/runs/foreign-run/answers",
                                     json={**body, "decision_id": question["id"]})
        assert response.status_code == 403
    assert engine.store.get("question", question["id"]) == committed
    assert not engine.client.commands


@pytest.mark.asyncio
async def test_duplicate_request_cannot_change_answer_or_resume_newer_wait(isolated):
    engine = isolated
    run = saved_run(engine)
    first = await open_question(engine, run)
    result = await engine.answer(first["id"], request(first))
    second = await open_question(engine, engine.store.get("run", run["id"]), call_id="ask-second")
    retried = await engine.answer(first["id"], request(first))
    assert retried["answer_event_ref"] == result["answer_event_ref"]
    assert retried["run"]["status"] == "WAITING_USER"
    assert [item["id"] for item in retried["run"]["questions"]] == [second["id"]]
    with pytest.raises(ContractError, match="IDEMPOTENCY_CONFLICT"):
        await engine.answer(first["id"], request(first, answer="修改为另一项"))
    with pytest.raises(ContractError, match="DECISION_CLOSED"):
        await engine.answer(first["id"], request(first, request_id="different-answer"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(TERMINAL | {"FINALIZING", "CANCELLING"}))
async def test_stale_or_closed_run_cannot_be_revived_by_answer_or_legacy_recovery(isolated, status):
    engine = isolated
    run = saved_run(engine)
    question = await open_question(engine, run)
    engine.store.update("run", run["id"], status=status)
    with pytest.raises(ContractError, match="DECISION_STALE"):
        await engine.answer(question["id"], request(question))
    engine.store.update("question", question["id"], status="ANSWERED", answer="reject",
        answer_event_ref="legacy-answer", answer_request_id="legacy-request")
    await engine._recover_decision_waits()
    assert engine.store.get("run", run["id"])["status"] == status


@pytest.mark.asyncio
async def test_wrong_revision_keeps_question_open_and_no_resume_event(isolated):
    engine = isolated
    run = saved_run(engine)
    question = await open_question(engine, run)
    stale = request(question)
    stale.question_revision += 1
    with pytest.raises(ContractError, match="DECISION_STALE"):
        await engine.answer(question["id"], stale)
    assert engine.store.get("question", question["id"])["status"] == "OPEN"
    assert not engine.store.get("run", run["id"]).get("resume_resolution")


@pytest.mark.asyncio
async def test_legacy_answered_wait_gap_restores_same_event_preserves_unknown_fence(isolated):
    engine = isolated
    run = saved_run(engine, pending_unknown_operation="operation-unknown")
    question = await open_question(engine, run)
    engine.store.update("question", question["id"], status="ANSWERED", answer="reject",
        answer_event_ref="legacy-answer", answer_request_id="legacy-request")
    await engine._recover_decision_waits()
    restored = engine.store.get("run", run["id"])
    assert restored["status"] == "RUNNING"
    assert restored["resume_resolution"] == {"resolution_kind": "human_answer", "answer_event_ref": "legacy-answer"}
    assert restored["pending_unknown_operation"] == "operation-unknown"
    assert restored["goal"] == run["goal"] and restored["allowed_effects"] == []
    assert not engine.client.commands
    revision = restored["revision"]
    await engine._recover_decision_waits()
    assert engine.store.get("run", run["id"])["revision"] == revision


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["confirm", "revise"])
async def test_legacy_controller_scope_choice_uses_same_projection_as_normal_answer(isolated, answer):
    engine = isolated
    run = saved_run(engine, phase="intent", intent_assessment={"objective": "read_explain"})
    engine._controller_question(run, "请确认本次范围")
    question = engine.store.list("question")[-1]
    engine.store.update("question", question["id"], status="ANSWERED", answer=answer,
        answer_event_ref="scope-answer", answer_request_id="scope-request")
    await engine._recover_decision_waits()
    restored = engine.store.get("run", run["id"])
    assert restored["status"] == "ASSESSING_INTENT" and restored["resume_resolution"] is None
    assert restored["append_event"] == {"user_decision": answer, "decision_id": question["id"]}
    if answer == "confirm":
        assert restored["confirmed_intent_hash"] == canonical_hash(run["intent_assessment"])
    else:
        assert restored["intent_assessment"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text_audio_approval", "selling_points_generation", "selling_points_confirmation"])
async def test_legacy_superseded_target_gap_resumes_replan_without_human_approval(isolated, kind):
    engine = isolated
    run = saved_run(engine, status="WAITING_USER")
    engine.store.put("question", {"id": "stale-target", "run_id": run["id"],
        "kind": kind, "status": "SUPERSEDED", "prompt": "当前目标已经变化"})
    await engine._recover_decision_waits()
    restored = engine.store.get("run", run["id"])
    assert restored["status"] == "RUNNING"
    assert restored["resume_resolution"] == {
        "resolution_kind": "system_resolution", "system_resolution_ref": "stale-target"}
    assert not engine.store.get("question", "stale-target").get("verified_human_receipt")
    assert not engine.client.commands


@pytest.mark.asyncio
async def test_stale_audio_review_resolution_and_resume_inbox_are_atomic(env, monkeypatch):
    from langgraph.errors import GraphInterrupt

    engine, service = env
    room, prep, _ = await prepared_text(service)
    run = await new_run(engine, "核对隔离测试文案", room_id=room["id"], preparation_id=prep["id"])
    engine.store.update("run", run["id"], phase="execute", status="RUNNING")
    monkeypatch.setattr("shengbo_prelive.runtime.interrupt", stop_at_interrupt)
    with pytest.raises(GraphInterrupt):
        await engine.request_decision(RunScope(run["id"], "audit-stale-review", "execute"),
            {"kind": "text_audio_approval", "prompt": "核对整包文案"}, "ask-stale-review")
    question = engine.get_run(run["id"])["questions"][0]
    original_query = engine.client.query

    async def changed_review(key, args=None, **kwargs):
        if key == "create_text_review":
            return {"review_hash": "changed-review"}
        return await original_query(key, args, **kwargs)

    monkeypatch.setattr(engine.client, "query", changed_review)
    offered = AnswerRequest(question_revision=question["revision"], answer="approve", client_request_id="stale-review",
        human_receipt={"operation_id": question["approval_binding"]["operation_id"]})
    original_put = RuntimeStore._put

    def fail_run_write(db, kind, body):
        if kind == "run" and body.get("status") == "RUNNING":
            raise RuntimeError("exit between stale question and resume")
        return original_put(db, kind, body)

    with patch.object(engine.store, "_put", side_effect=fail_run_write):
        with pytest.raises(RuntimeError, match="exit between"):
            await engine.answer(question["id"], offered)
    assert engine.store.get("question", question["id"])["status"] == "OPEN"
    assert engine.store.get("run", run["id"])["status"] == "WAITING_USER"
    with pytest.raises(ContractError, match="TEXT_REVIEW_STALE"):
        await engine.answer(question["id"], offered)
    assert engine.store.get("question", question["id"])["status"] == "SUPERSEDED"
    restored = engine.store.get("run", run["id"])
    assert restored["status"] == "RUNNING" and restored["resume_resolution"]["resolution_kind"] == "system_resolution"
    assert (await service.query("read_text_approval", {"preparation_id": prep["id"]}))["status"] != "current"


@pytest.mark.asyncio
async def test_legacy_room_answer_gap_revalidates_identity_without_second_model_or_broader_approval(env):
    from unittest.mock import AsyncMock
    from test_room_resolution_runtime_v1 import intake

    engine, _, run, question, room, product = await intake(env)
    engine.store.update("question", question["id"], status="ANSWERED", answer="room:" + room["id"],
        answer_event_ref="room-answer", answer_request_id="room-request")
    await engine._recover_decision_waits()
    restored = engine.store.get("run", run["id"])
    assert all(restored["room_resolution"]["room_ref"][key] == value for key, value in room.items())
    assert restored["room_resolution"]["scope_approval_granted"] is False
    assert restored["intent_assessment"]["room_id"] == room["id"]
    assert not restored.get("confirmed_intent_hash")
    with patch.object(engine, "_invoke", new=AsyncMock(side_effect=AssertionError("No second intent model"))):
        await engine._drive_main(run["id"])
    final = engine.store.get("run", run["id"])
    assert final["phase"] == "execute" and final["target_product_ids"] == [product["id"]]
    assert "prepare_required_audio" not in final["allowed_effects"]
    assert len(engine.store.list("question")) == 1


@pytest.mark.asyncio
async def test_competing_verified_answers_commit_one_event_and_loser_can_retry(env, monkeypatch):
    from test_room_resolution_runtime_v1 import intake

    engine, _, run, question, room, _ = await intake(env)
    original_query = engine.client.query
    ready, entered = asyncio.Event(), []

    async def gated_read(key, args=None, **kwargs):
        if key == "read_room":
            entered.append(1)
            if len(entered) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=5)
        return await original_query(key, args, **kwargs)

    monkeypatch.setattr(engine.client, "query", gated_read)
    offered = request(question, answer="room:" + room["id"], request_id="competing-room-answer")
    responses = await asyncio.gather(engine.answer(question["id"], offered),
        engine.answer(question["id"], offered), return_exceptions=True)
    committed = [item for item in responses if isinstance(item, dict)]
    failures = [item for item in responses if isinstance(item, ContractError)]
    assert len(committed) == len(failures) == 1 and failures[0].code == "RUNTIME_STALE"
    retry = await engine.answer(question["id"], offered)
    assert retry["answer_event_ref"] == committed[0]["answer_event_ref"]
    assert engine.store.get("question", question["id"])["revision"] == question["revision"] + 1


async def disabled_scheduler(self):
    await asyncio.Event().wait()


def ask_message(call_id, prompt):
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "request_user_decision",
        "args": {"kind": "fact_check", "prompt": prompt}}])


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_state", ["open", "answered", "legacy_gap"])
async def test_native_sqlite_checkpoint_and_public_answer_survive_restart(tmp_path, restart_state):
    root = tmp_path / "durable-runtime"
    first = RuntimeEngine(root, IsolatedBusiness(), model=BindableFakeModel(
        responses=[ask_message("first-question", "请核对第一项事实。")]))
    second = None
    with patch.object(RuntimeEngine, "_scheduler", new=disabled_scheduler):
        try:
            await first.start()
            run = saved_run(first)
            initial_goal = deepcopy(run["goal"])
            result, snapshot, _ = await first._invoke(run)
            assert result.get("__interrupt__") and any(task.interrupts for task in snapshot.tasks)
            question = first.get_run(run["id"])["questions"][0]
            if restart_state != "open":
                await first.answer(question["id"], request(question))
                if restart_state == "legacy_gap":
                    first.store.update("run", run["id"], status="WAITING_USER", resume_resolution=None)
            await first.stop()
            second = RuntimeEngine(root, IsolatedBusiness(), model=BindableFakeModel(
                responses=[ask_message("second-question", "第一项已收到，请核对第二项事实。")]))
            await second.start()
            if restart_state == "open":
                assert second.get_run(run["id"])["status"] == "WAITING_USER"
                await second.answer(question["id"], request(question))
            saved_answer = second.store.get("question", question["id"])
            result, snapshot, _ = await second._invoke(second.store.get("run", run["id"]))
            assert result.get("__interrupt__") and any(task.interrupts for task in snapshot.tasks)
            returned = [message for message in snapshot.values["messages"]
                        if isinstance(message, ToolMessage) and message.tool_call_id == "first-question"]
            assert len(returned) == 1 and "按当前资料核对" in returned[0].content
            current = second.get_run(run["id"])
            assert current["goal"] == initial_goal and current["allowed_effects"] == []
            assert current["questions"][0]["prompt"] == "第一项已收到，请核对第二项事实。"
            retry = await second.answer(question["id"], request(question))
            assert retry["answer_event_ref"] == saved_answer["answer_event_ref"]
            assert retry["run"]["status"] == "WAITING_USER"
            assert not second.client.commands
        finally:
            await first.stop()
            if second is not None:
                await second.stop()
