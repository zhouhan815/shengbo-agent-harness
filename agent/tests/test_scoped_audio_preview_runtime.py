"""Normal RuntimeEngine/HITL/tool boundary to Voice, no paid provider calls."""
from copy import deepcopy
from types import SimpleNamespace
import wave

import pytest
import httpx
from langgraph.errors import GraphInterrupt

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import AnswerRequest, IntentAssessment, RunScope
from shengbo_prelive.tools import build_tools
from shengbo_prelive.client import ShengboClient, RemoteOutcomeUnknown
from test_runtime_business import env, human_command, new_run, prepared_text, stop_at_interrupt


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "unknown"])
async def test_scoped_preview_normal_goal_human_receipt_and_business_tool(env, monkeypatch, outcome):
    engine, service = env
    room, prep, script = await prepared_text(service)
    spoken = await service.query("read_script", {"ref": script})
    product_id = spoken["body"]["product_id"]
    product_name = (await service.query("read_product", {"product_id": product_id}))["body"]["name"]
    quote = "只试听第一版长开场和商品介绍，使用MiniMax语音连续合成一段。"
    text = product_name + "（" + product_id + "）：" + quote
    run = await new_run(engine, text, room_id=room["id"], preparation_id=prep["id"])
    proposal = IntentAssessment(objective="audio_preview", summary="只做已授权两模块连续试听",
        authorization_quote=quote, preparation_id=prep["id"], room_id=room["id"],
        target_product_ids=[product_id], requested_modules=["opening_long", "product_intro"])
    run = engine.store.update("run", run["id"], intent_assessment=proposal.model_dump())
    await engine._freeze_goal(run)
    run = engine.store.get("run", run["id"])
    assert run["allowed_effects"] == ["preview_script_audio"]
    assert "preview_script_audio" in engine.allowed_tool_names(RunScope(run["id"], "audition", "execute"))
    assert "approve_script_audio_preview" not in {tool.name for tool in build_tools(engine)}
    before = deepcopy(await service.query("read_preparation", {"preparation_id": prep["id"]}))
    calls = []

    def render(text, directory, voice, provider, model, style):
        calls.append((text, voice, provider, model, style))
        if outcome == "unknown":
            raise RuntimeError("Fake provider sent, outcome uncertain")
        directory.mkdir(parents=True, exist_ok=True)
        with wave.open(str(directory / "runtime.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(32000)
            wav.writeframes(b"\0\0" * 3200)
        return {"url": "/audio/runtime.wav", "duration": .1, "usage_characters": len(text), "cache_hit": False}

    service.adapter.server = SimpleNamespace(RUNTIME=service.runtime, POOL=None, render=render, voice_options=lambda *_: None)
    scope = RunScope(run["id"], "audition", "execute")
    monkeypatch.setattr("shengbo_prelive.runtime.interrupt", stop_at_interrupt)
    with pytest.raises(GraphInterrupt):
        await engine.request_decision(scope, {"kind": "script_audio_preview_approval", "prompt": "登记当前已授权的两模块试听回执", "context": {"operation_id": "current-human-preview"}}, "wait-for-receipt")
    question = engine.get_run(run["id"])["questions"][0]
    assert question["options"][0]["human_action"] == {
        "kind": "verify_existing_receipt", "operation_id": "current-human-preview"}
    answer = AnswerRequest(question_revision=question["revision"], answer="approve", client_request_id="preview-answer",
        human_receipt={"operation_id": "current-human-preview"})
    with pytest.raises(ContractError, match="PREVIEW_HUMAN_RECEIPT_MISSING"):
        await engine.answer(question["id"], answer)
    assert not calls
    modules = ["opening_long", "product_intro"]
    ordered = "\n".join("".join(row["text"] for row in spoken["modules"][key]["sentences"]) for key in modules)
    receipt = await human_command(service, "approve_script_audio_preview", {"preparation_id": prep["id"],
        "run_id": run["id"], "script_ref": script, "module_keys": modules,
        "module_refs": [spoken["body"]["module_refs"][key] for key in modules],
        "ordered_text_hash": canonical_hash(ordered), "tts_selection": {"provider": "minimax", "voice": "Wise_Woman", "model": "speech-2.8-turbo"},
        "style": {"emotion": "happy", "speed": 1.02, "pitch": 0, "vol": 1}, "authorization_quote": quote}, operation="current-human-preview")
    answer.human_receipt = receipt
    await engine.answer(question["id"], answer)
    saved = engine.store.get("question", question["id"])
    assert saved["verified_human_receipt"]["whole_package_approval"] is False
    args = {"preparation_id": prep["id"], "preview_approval_ref": receipt["data"]["preview_approval_ref"], "binding_hash": receipt["data"]["binding_hash"]}
    if outcome == "unknown":
        with pytest.raises(ContractError, match="PREVIEW_OUTCOME_UNKNOWN"):
            await engine.business_command(scope, "preview_script_audio", args, "actual-audition-tool-call")
        latest = engine.store.get("run", run["id"])
        identity = latest["pending_unknown_operation"]
        operation = engine.store.get("operation", identity)
        assert operation["status"] == "unknown" and operation["reconciliation_operation_id"] == identity
        assert await engine._reconcile_pending_operation(latest) is False
        with pytest.raises(ContractError, match="RECONCILIATION_REQUIRED"):
            await engine.business_command(scope, "preview_script_audio", args, "different-audition-tool-call")
        assert len(calls) == 1
        return
    result = await engine.business_command(scope, "preview_script_audio", args, "actual-audition-tool-call")
    assert len(calls) == 1 and calls[0][0] == ordered
    assert result["data"]["output_mode"] == "continuous"
    assert result["data"]["module_keys"] == modules
    assert await engine.business_command(scope, "preview_script_audio", args, "actual-audition-tool-call") == result
    assert len(calls) == 1
    assert await service.query("read_preparation", {"preparation_id": prep["id"]}) == before
    assert (await engine.finish(scope, "COMPLETED", "两模块连续试听已完成，原稿及整包状态未改动。"))["accepted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("code,state", [("PREVIEW_OUTCOME_UNKNOWN", "unknown"), ("PREVIEW_IN_PROGRESS", "running")])
async def test_only_scoped_sent_or_running_409_maps_to_unknown_and_keeps_original_id(code, state):
    async def handler(request):
        return httpx.Response(409, json={"error": {"code": code, "message": "safe preview status",
            "details": {"operation_id": "requested-op", "original_operation_id": "original-paid-op"}}})
    client = ShengboClient("http://isolated.invalid", "agent-fixture", "controller-fixture", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RemoteOutcomeUnknown) as error:
            await client.command("preview_script_audio", {}, {"operation_id": "requested-op"})
        assert error.value.details == {"operation_id": "requested-op", "original_operation_id": "original-paid-op", "preview_state": state}
        with pytest.raises(ContractError) as unrelated:
            await client.command("patch_script", {}, {"operation_id": "requested-op"})
        assert not isinstance(unrelated.value, RemoteOutcomeUnknown) and unrelated.value.status == 409
    finally:
        await client.close()
