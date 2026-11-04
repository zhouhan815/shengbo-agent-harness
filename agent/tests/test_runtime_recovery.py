"""Shared Harness recovery leaves failed work resumable on the original run."""
import asyncio

import pytest
from langchain_core.messages import AIMessage

from shengbo_contracts.common import ContractError
from shengbo_prelive.models import AnswerRequest
from shengbo_prelive.runtime_recovery import KIND
from test_runtime_business import BindableFakeModel, DirectBusinessClient, FakeAdapter, PreliveService, env, new_run

REQUEST = "查看当前声播状态"


def tool(name, identity, args):
    return AIMessage(content="", tool_calls=[{"name": name, "id": identity, "args": args}])


def intent():
    return [tool("save_task_assessment", "intent", {"assessment": {
        "objective": "read_explain", "summary": "读取状态", "authorization_quote": REQUEST}})]


def execution(outcome="COMPLETED"):
    return [tool("save_task_assessment", "execution", {"assessment": {
        "summary": "读取状态", "capabilities": [], "steps": [], "decision_basis": []}}),
        tool("finish_turn", "finish-" + outcome, {"outcome": outcome, "summary": (
            "当前状态已核对" if outcome == "COMPLETED" else "需要核对直播间是否仍然有效。")})]


def choose(strategy):
    return tool("choose_fixed_step_recovery", "recover", {"strategy": strategy})


async def wait(engine, identity, status):
    async with asyncio.timeout(10):
        while (current := engine.get_run(identity))["status"] != status:
            await asyncio.sleep(.025)
    return current


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_reported_blocker_keeps_original_run_and_resumes_after_answer(env, restart):
    engine, service = env
    engine._provided_model = BindableFakeModel(responses=[
        *intent(), *execution("BLOCKED"), choose("ask_user"),
        tool("finish_turn", "finish-again", {"outcome": "COMPLETED", "summary": "已按原任务核对"}),
        AIMessage(content="已完成")])
    await engine.start()
    identity = (await new_run(engine, REQUEST))["id"]
    paused = await wait(engine, identity, "WAITING_USER")
    question, = paused["questions"]
    assert question["kind"] == KIND
    assert question["allow_text"] and question["options"][0]["value"] == "retry"
    assert "直播间是否仍然有效" in question["prompt"]
    assert paused.get("closure_ref") is None and not paused["dispatch_frozen"]
    assert "run.closed" not in [event["type"] for event in engine.events(identity)["events"]]
    if restart:
        await engine.stop()
        engine.client = DirectBusinessClient(PreliveService(service.store.path.parent, FakeAdapter()))
        engine._provided_model = BindableFakeModel(responses=[
            tool("finish_turn", "finish-after-restart", {"outcome": "COMPLETED", "summary": "已按原任务核对"})])
        await engine.start()
        assert (await wait(engine, identity, "WAITING_USER"))["questions"][0]["id"] == question["id"]
    response = await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
        answer={"text": "请继续核对原任务。"}, client_request_id="continue-same"))
    assert response["run"]["id"] == identity
    complete = await wait(engine, identity, "COMPLETED")
    assert complete["goal"] == paused["goal"]
    assert complete.get("closure_ref") and not complete.get("recovery_pending")
    types = [event["type"] for event in engine.events(identity)["events"]]
    assert types.index("runtime.recovery.waiting") < types.index("runtime.recovery.resuming") < types.index("run.closed")


@pytest.mark.asyncio
async def test_transient_runner_exception_is_returned_to_main_for_resume(env, monkeypatch):
    engine, _ = env
    engine._provided_model = BindableFakeModel(responses=[*intent(), choose("retry_checkpoint"), *execution()])
    original = engine._invoke
    failures = []
    async def once(run, **kwargs):
        if run["phase"] == "execute" and not failures:
            failures.append(run["id"])
            raise ContractError("VOICE_UNAVAILABLE", "测试业务服务短时不可用", status=503)
        return await original(run, **kwargs)
    monkeypatch.setattr(engine, "_invoke", once)
    await engine.start()
    identity = (await new_run(engine, REQUEST))["id"]
    complete = await wait(engine, identity, "COMPLETED")
    assert failures == [identity] and not complete["questions"]
    events = engine.events(identity)["events"]
    assert any(event["type"] == "runtime.recovery.resuming" and event["data"]["selected_by"] == "model"
               for event in events)
    assert len([event for event in events if event["type"] == "run.closed"]) == 1


@pytest.mark.asyncio
async def test_unknown_write_is_not_replayed_while_receipt_is_pending(env, monkeypatch):
    engine, _ = env
    run = await new_run(engine, REQUEST)
    identity = run["id"]
    from shengbo_prelive.runtime_recovery import record
    run = engine.store.update("run", identity, phase="execute", status="RUNNING", pending_unknown_operation="op_original")
    record(engine, run, ContractError("REMOTE_OUTCOME_UNKNOWN", "回执待核对", status=503))
    checked = []
    async def unknown(value):
        checked.append(value["pending_unknown_operation"])
        return False
    async def never(*args, **kwargs):
        raise AssertionError("结果未知时不可重放执行图")
    monkeypatch.setattr(engine, "_reconcile_pending_operation", unknown)
    monkeypatch.setattr(engine, "_invoke", never)
    await engine._drive_main(identity)
    paused = engine.get_run(identity)
    assert checked == ["op_original"] and paused["status"] == "RECOVERING"
    assert paused["pending_unknown_operation"] == "op_original" and not paused.get("closure_ref")
    assert paused["recovery_pending"]["error_code"] == "REMOTE_OUTCOME_UNKNOWN"


@pytest.mark.asyncio
async def test_original_reupload_restores_same_bytes_and_keeps_frozen_ref(env, monkeypatch):
    engine, _ = env
    run = await new_run(engine, REQUEST)
    identity, conversation = run["id"], run["conversation_id"]
    original = engine.materials.accept(conversation, "商品原件.txt", "text/plain", b"original file")["material_ref"]
    different = engine.materials.accept(conversation, "商品补充.txt", "text/plain", b"changed file")["material_ref"]
    repeat = engine.materials.accept(conversation, "商品原件.txt", "text/plain", b"original file")["material_ref"]
    row = engine.store.get("material", original["id"])
    from pathlib import Path
    Path(row["path"]).unlink()
    run = engine.store.update("run", identity, status="RUNNING", phase="execute",
        input={**run["input"], "material_refs": [original]})
    from shengbo_prelive.runtime_recovery import drive, record
    record(engine, run, ContractError("MATERIAL_STORAGE_INVALID", "原件找不到", status=422))
    async def ask(*args, **kwargs):
        return "ask_user"
    monkeypatch.setattr("shengbo_prelive.fixed_step_recovery._choose", ask)
    await drive(engine, engine.store.get("run", identity))
    paused = engine.get_run(identity)
    question, = paused["questions"]
    assert question["context"]["can_restore_source"]
    with pytest.raises(ContractError, match="RECOVERY_SOURCE_MISMATCH"):
        await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
            answer={"text": "重新上传原件", "source_ref": different}, client_request_id="wrong-file"))
    assert engine.get_run(identity)["status"] == "WAITING_USER"
    await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
        answer={"text": "重新上传原件", "source_ref": repeat}, client_request_id="same-file"))
    engine.store.recover()  # persisted decision/inbox survives service recovery
    await drive(engine, engine.store.get("run", identity))
    resumed = engine.get_run(identity)
    assert resumed["status"] == "RUNNING" and resumed["input"]["material_refs"] == [original]
    assert engine.materials.blob_bytes(original, conversation) == b"original file"
    assert not resumed.get("closure_ref")


@pytest.mark.asyncio
async def test_finalizer_rejects_an_unverified_automatic_close(env):
    engine, _ = env
    identity = (await new_run(engine, REQUEST))["id"]
    await engine._finalize(identity, "FAILED", "test")
    retained = engine.get_run(identity)
    assert retained["status"] == "RECOVERING" and not retained.get("closure_ref")
    assert not retained.get("dispatch_frozen")
    assert retained["recovery_pending"]["error_code"] == "FINISH_BLOCKED"



def test_newer_archived_reply_releases_only_historical_unknown_sends():
    from shengbo_prelive.models import RunScope
    from shengbo_prelive.runtime_recovery import step_request_pending

    scope = RunScope("run", "next-step", "execute", "product_facts", "branch")
    attempts = [
        {"id": "a", "state": "outcome_unknown", "invocation_id": "first"},
        {"id": "b", "state": "outcome_unknown", "invocation_id": "second"},
        {"id": "c", "state": "succeeded", "invocation_id": "third"},
    ]
    assert step_request_pending(attempts, scope, None)
    assert not step_request_pending(attempts, scope, None, adopted="c")
    assert not step_request_pending(attempts, scope, {"acknowledged_model_attempt_ids": ["a", "b"]})
    assert step_request_pending([*attempts, {"id": "d", "state": "outcome_unknown"}], scope, None, adopted="c")
    assert step_request_pending([*attempts, {"id": "d", "state": "sent"}], scope, None, adopted="c")
