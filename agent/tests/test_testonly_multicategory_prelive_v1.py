"""TESTONLY: real SDK/services, scripted models and local silence; no live approval."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import wave

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from shengbo_contracts.business import MODULE_KEYS
from shengbo_contracts.common import MutationContext, Principal
from shengbo_prelive.models import AnswerRequest, MessageRequest
from shengbo_prelive.runtime import CURRENT_SCOPE, RuntimeEngine
from test_runtime_business import BindableFakeModel, DirectBusinessClient, FakeAdapter, PreliveService, Product, human_command


class ScriptedNativeModel(BaseChatModel):
    _logic = PrivateAttr()

    def __init__(self, logic):
        super().__init__()
        self._logic = logic

    @property
    def _llm_type(self):
        return "testonly-multicategory-scripted-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        raise AssertionError("Only the actual async native SDK path is allowed")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=await self._logic(messages))])


@pytest.mark.asyncio
@pytest.mark.parametrize("name,price,material,scene", [
    ("TESTONLY通勤随行杯", 59, "不锈钢", "办公室饮水"),
    ("TESTONLY日常帆布袋", 29, "棉质帆布", "日常收纳"),
])
async def test_materials_to_formal_research_scripts_testonly_approval_and_local_audio(tmp_path, name, price, material, scene):
    class LocalSilenceAdapter(FakeAdapter):
        rendered = 0

        async def render_audio(self, text, room_id, voice):
            self.rendered += 1
            identity = hashlib.sha256((text + str(voice)).encode()).hexdigest()
            path = tmp_path / "voice" / "room_audio" / room_id / (identity + ".wav")
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\x00\x00" * 1600)
            return {"path": str(path), "audio_id": identity, "duration": .1, "url": "/testonly-audio/" + identity + ".wav"}

    fields = Product(name=name, price=price, currency="CNY", material=material,
        selling_points="已有资料明确基础材质与单件组成。", knowledge="单件装；使用前检查完整性。",
        specifications=[{"name": "包装数量", "value": "1件"}]).model_dump()
    adapter = LocalSilenceAdapter()
    service = PreliveService(tmp_path / "voice", adapter)
    engine = RuntimeEngine(tmp_path / "agent", DirectBusinessClient(service),
        model=BindableFakeModel(responses=[AIMessage(content="unused")]))
    room = (await human_command(service, "create_room", {"name": "TESTONLY非网球闭环测试房间"}))["data"]["room_ref"]
    text = "为" + room["id"] + "准备整场直播，按上传资料新建商品，开展顾客分析、生成话术并准备语音。所有人工答复仅限隔离TESTONLY测试。"
    conversation = engine.create_conversation({"room_id": room["id"], "scope": {"kind": "catalog"}})
    upload = engine.accept_material(conversation["id"], "testonly-product.json.txt", "text/plain",
        json.dumps(fields, ensure_ascii=False).encode("utf-8"))["material_ref"]
    call_count = 0

    def call(tool, args):
        nonlocal call_count
        call_count += 1
        return AIMessage(content="", tool_calls=[{"id": "testonly-chain-" + str(call_count), "name": tool, "args": args}])

    def saved_product():
        operations = [op for op in engine.store.list("operation") if op.get("key") == "save_product" and op.get("status") == "committed"]
        return operations[-1]["receipt"]["data"]["product_ref"] if operations else None

    async def logic(messages):
        scope = CURRENT_SCOPE.get()
        run = engine.store.get("run", scope.run_id)
        if scope.role != "main":
            task = engine.store.get("branch", scope.branch_task_id)["task"]
            if scope.role == "product_facts":
                if not any(getattr(message, "name", "") == "read_source" for message in messages):
                    return call("read_source", {"ref": task["source_refs"][0], "section": "text:1"})
                report_fields = fields
            else:
                if not any(getattr(message, "name", "") == "read_product" for message in messages):
                    return call("read_product", {"ref": saved_product()})
                report_fields = {"context": {"market": "中国", "language": "普通话"},
                    "segments": [{"audience": "需要" + scene + "的顾客", "use_scene": scene}]}
            return call("submit_" + scope.role + "_report", {"report": {
                "summary": "TESTONLY已读正式来源的" + scope.role + "分析",
                "product_ids": task.get("product_ids", []), "fields": report_fields,
                "source_refs": task.get("source_refs", []), "dependencies": task.get("input_refs", []),
                "insights": [{"use_scene": scene, "buyer_need": "按真实材质和数量选择"}],
                "hypotheses": ["场景偏好待真实顾客验证，不能变成产品功效"], "conflicts": [],
                "missing_fields": [], "asset_suggestions": [], "evidence": []}})
        if scope.phase == "intent":
            return call("save_task_assessment", {"assessment": {"objective": "prepare_session",
                "script_delivery_requirement": "new_generation", "summary": "TESTONLY从完整材料到未采用播放候选",
                "authorization_quote": text, "room_id": room["id"],
                "required_analyses": ["product_facts", "audience_scenes", "session_config"]}})
        if not run.get("task_assessment"):
            return call("save_task_assessment", {"assessment": {"summary": "先材料建品，再按最终版本研究并生成",
                "capabilities": ["product_facts", "audience_scenes", "session_config"],
                "steps": ["读取材料", "验收事实并保存商品", "正式顾客研究", "正式话术", "TESTONLY人工批准", "本地静音fixture"]}})
        branches = [branch for branch in engine.store.list("branch") if branch["run_id"] == run["id"]]
        if not branches:
            return call("dispatch_analysis_batch", {"reason": "先核对上传商品事实", "tasks": [{
                "role": "product_facts", "instruction": "读取材料，逐字保留明确字段，不补未知功效。",
                "product_ids": [], "source_refs": [upload], "input_refs": []}]})
        pending = [branch["id"] for branch in branches if branch["status"] in {"QUEUED", "RUNNING", "RECOVERING"}]
        if pending:
            return call("finish_turn", {"outcome": "WAITING_JOBS", "summary": "等待已派发的隔离研究", "wait_for_branch_ids": pending})
        prepared = [branch["id"] for branch in branches if branch["status"] == "PREPARED"]
        if prepared:
            return call("accept_analysis_reports", {"branch_task_ids": prepared})
        product_ref = saved_product()
        if product_ref is None:
            facts = next(branch for branch in branches if branch["role"] == "product_facts")
            return call("wf_product_materials", {"action": "save_from_facts", "parameters": {
                "branch_task_id": facts["id"], "accepted_report_ref": facts["accepted_ref"]}})
        prep_id = run["scope"]["preparation_id"]
        prep = await service.query("read_preparation", {"preparation_id": prep_id})
        body = prep["body"]
        base = {"preparation_id": prep_id, "expected_revision": prep["ref"]["revision"]}
        if not body["selected_product_refs"]:
            return call("update_preparation_products", {**base, "selected_product_refs": [product_ref]})
        if not any(branch["role"] == "audience_scenes" for branch in branches):
            return call("dispatch_analysis_batch", {"reason": "已保存精确商品，按实际材质和数量研究本次顾客及本场", "tasks": [
                {"role": role, "instruction": "读取最终精确商品，提出有边界的场景建议，不推导性能。",
                    "product_ids": [product_ref["id"]], "source_refs": [upload], "input_refs": [product_ref]}
                for role in ("audience_scenes", "session_config")]})
        if not body["draft_ref"]:
            return call("save_session_draft", {**base, "draft": {"theme": scene, "commerce": {}}})
        if not body["asset_checklist_ref"]:
            return call("save_asset_checklist", {**base, "checklist": {"items": [], "notes": ["TESTONLY静音音频验证"]}})
        if not body["bundle_ref"]:
            return call("wf_context_bundle", {"action": "save"})
        if not body["plan_ref"]:
            return call("save_script_change_plan", {**base, "bundle_ref": body["bundle_ref"], "items": [{
                "product_ref": product_ref, "variant_id": "testonly-source-to-audio", "modules": [
                    {"key": key, "action": "disable" if key in {"campaign", "transition"} else "generate"} for key in MODULE_KEYS]}]})
        observation = await service.query("read_completion_observation", {"preparation_id": prep_id})
        if observation["pending_job_ids"]:
            return call("finish_turn", {"outcome": "WAITING_JOBS", "summary": "等待隔离业务生成任务"})
        if not body["script_refs"]:
            return call("execute_script_plan", {**base, "plan_ref": body["plan_ref"], "context_bundle_ref": body["bundle_ref"],
                "expected_generation_epoch": body["generation_epoch"]})
        questions = [question for question in engine.store.list("question") if question["run_id"] == run["id"]]
        playlist = {"preparation_id": prep_id, "items": [{"occurrence_id": "testonly-one", "product_id": product_ref["id"],
            "script_revision_ref": body["script_refs"][0]}], "loop_mode": "infinite"}
        if not body["playlist_ref"]:
            if not any(question["kind"] == "playlist_order" and question["status"] == "ANSWERED" for question in questions):
                return call("request_user_decision", {"kind": "playlist_order", "prompt": "TESTONLY确认隔离播放顺序", "context": {"playlist": playlist}})
            return call("save_playlist", {**base, **playlist})
        if not body["compiled_plan_ref"]:
            return call("compile_playback_plan", {**base, "playlist_ref": body["playlist_ref"]})
        if not body["approval_ref"]:
            return call("request_user_decision", {"kind": "text_audio_approval", "prompt": "TESTONLY仅批准当前临时文案与静音fixture，绝非生产批准"})
        if not body["audio_manifest_ref"]:
            review = await service.query("create_text_review", {"preparation_id": prep_id})
            return call("prepare_required_audio", {**base, "approval_ref": body["approval_ref"], "review_hash": review["review_hash"],
                "expected_generation_epoch": body["generation_epoch"]})
        if not body["candidate_publication_ref"]:
            return call("publish_preparation", {**base, "approval_ref": body["approval_ref"]})
        return call("finish_turn", {"outcome": "READY_FOR_REVIEW", "summary": "TESTONLY完整材料、研究、话术、隔离人工批准和本地静音音频均完成，候选未采用"})

    engine._provided_model = ScriptedNativeModel(logic)
    answers = []
    try:
        await engine.start()
        accepted = await engine.submit_message(conversation["id"], MessageRequest(text=text, intent_mode="new_task", material_refs=[upload],
            client_request_id="testonly-materials-to-audio"))
        for _ in range(1000):
            current = engine.get_run(accepted["run"]["id"])
            for question in current["questions"]:
                receipt = None
                if question["kind"] == "new_product_import":
                    answer = "confirm_new_products"
                elif question["kind"] == "playlist_order":
                    answer = "confirmed"
                elif question["kind"] == "text_audio_approval":
                    assert adapter.rendered == 0, "No audio fixture is created before exact test approval"
                    command = question["human_action"]["command"]
                    receipt = await service.execute_command(command["command"], command["args"],
                        MutationContext.model_validate(command["mutation"]), Principal(kind="human", actor_id="testonly-fixture-human"))
                    answer = "approve"
                else:
                    raise AssertionError("Unexpected confirmation entry: " + question["kind"])
                await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"], answer=answer,
                    client_request_id="testonly-answer-" + question["id"], human_receipt=receipt))
                answers.append(question["kind"])
            if current["status"] in {"READY_FOR_REVIEW", "COMPLETED", "BLOCKED", "FAILED"}:
                break
            await asyncio.sleep(.03)
        assert current["status"] == "READY_FOR_REVIEW", current
        assert answers == ["new_product_import", "playlist_order", "text_audio_approval"]
        assert {branch["role"] for branch in current["branches"] if branch["status"] == "ACCEPTED"} == {
            "product_facts", "audience_scenes", "session_config"}
        product_ref = saved_product()
        product_record = await service.query("read_product", {"ref": product_ref})
        assert product_record["body"]["name"] == name and product_record["body"]["price"] == price
        prep = await service.query("read_preparation", {"preparation_id": current["scope"]["preparation_id"]})
        bundle = await service.query("read_context", {"ref": prep["body"]["bundle_ref"]})
        assert len(bundle["body"]["analysis_refs"]) == 3
        assert any(record["body"].get("role") == "audience_scenes" for record in bundle["body"]["records"])
        assert prep["body"]["script_refs"] and prep["body"]["audio_manifest_ref"] and prep["body"]["candidate_publication_ref"]
        assert (await service.query("read_text_approval", {"preparation_id": prep["ref"]["id"]}))["status"] == "current"
        audio_files = list((tmp_path / "voice" / "room_audio").rglob("*.wav"))
        assert adapter.rendered > 0 and audio_files
        assert all(path.is_relative_to(tmp_path) for path in audio_files)
        assert current["acceptance"]["ok"]
    finally:
        await engine.stop()
