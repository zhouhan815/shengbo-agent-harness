"""One trusted room choice continues the unchanged task without a model round."""
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.models import AnswerRequest, IntentAssessment, MessageRequest
from test_runtime_business import env, human_command, product


async def intake(env):
    engine, service = env
    room = (await human_command(service, "create_room", {"name": "本次中性直播间"}))["data"]["room_ref"]
    p = await product(service)
    text = f"为商品{p['id']}生成两个版本的话术，不生成语音。"
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    result = await engine.submit_message(conversation["id"], MessageRequest(text=text, intent_mode="new_task"))
    intent = IntentAssessment(objective="materials_to_script", summary="只做两版文字", target_product_ids=[p["id"]],
        requested_script_variants=[{"product_id": p["id"], "count": 2}], authorization_quote=text)
    run = engine.store.update("run", result["run"]["id"], intent_assessment=intent.model_dump())
    await engine._freeze_goal(run)
    run = engine.store.get("run", run["id"])
    q = next(q for q in engine.store.list("question") if q["run_id"] == run["id"] and q["status"] == "OPEN")
    return engine, service, run, q, room, p


@pytest.mark.asyncio
async def test_room_answer_reuses_narrow_proof_and_freezes_without_new_intent_or_confirmation(env):
    engine, service, run, q, room, p = await intake(env)
    assert q["context"]["resolution_kind"] == "room_selection"
    assert {"value": "room:" + room["id"], "label": "本次中性直播间"} in q["options"]
    original = deepcopy(run["input"])
    answer = AnswerRequest(answer="room:" + room["id"], question_revision=q["revision"], client_request_id="room-once")
    await engine.answer(q["id"], answer)
    chosen = engine.store.get("run", run["id"])
    assert chosen["input"] == original and chosen["intent_assessment"]
    assert chosen["intent_assessment"]["room_id"] == room["id"]
    assert not chosen.get("confirmed_intent_hash")
    with patch.object(engine, "_invoke", new=AsyncMock(side_effect=AssertionError("No second intent model round"))) as model:
        await engine._drive_main(run["id"])
        model.assert_not_called()
    final = engine.store.get("run", run["id"])
    assert final["goal"]["objective"] == "materials_to_script" and final["phase"] == "execute"
    assert final["target_product_ids"] == [p["id"]]
    assert "prepare_required_audio" not in final["allowed_effects"] and "publish_preparation" not in final["allowed_effects"]
    assert final["scope"]["preparation_id"]
    assert len([v for v in engine.store.list("question") if v["run_id"] == run["id"]]) == 1
    retry = await engine.answer(q["id"], answer)
    assert retry["run"]["scope"]["preparation_id"] == final["scope"]["preparation_id"]


@pytest.mark.asyncio
async def test_ambiguous_choice_leaves_original_question_open(env):
    engine, _service, run, q, room, _p = await intake(env)
    with pytest.raises(ContractError, match="ANSWER_INVALID"):
        await engine.answer(q["id"], AnswerRequest(answer="不要用" + room["id"],
            question_revision=q["revision"], client_request_id="ambiguous-room"))
    assert engine.store.get("question", q["id"])["status"] == "OPEN"
    assert not engine.store.get("run", run["id"]).get("room_resolution")


@pytest.mark.asyncio
async def test_room_choice_is_not_authorization_to_switch_to_another_room(env):
    engine, service, run, q, room, _p = await intake(env)
    await engine.answer(q["id"], AnswerRequest(answer="room:" + room["id"], question_revision=q["revision"], client_request_id="choose-a"))
    other = (await human_command(service, "create_room", {"name": "未选择的直播间"}))["data"]["room_ref"]
    current = engine.store.get("run", run["id"])
    changed = {**current["intent_assessment"], "room_id": other["id"]}
    current = engine.store.update("run", run["id"], intent_assessment=changed)
    with patch.object(engine, "_invoke", new=AsyncMock(side_effect=AssertionError("Proof still requires scope verification"))) as model:
        await engine._drive_main(run["id"])
        model.assert_not_called()
    assert not engine.store.get("run", run["id"]).get("goal")
    assert any(v["status"] == "OPEN" and v["id"] != q["id"] for v in engine.store.list("question"))
