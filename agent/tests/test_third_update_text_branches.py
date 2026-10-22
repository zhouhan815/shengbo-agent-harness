"""Public runtime/business entry checks for the third update, no paid calls."""
from copy import deepcopy
import json

import pytest
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from shengbo_contracts.common import ContractError, MutationContext, canonical_hash
from shengbo_prelive.models import IntentAssessment, RunScope
from shengbo_prelive.middleware import ActionBoundary
from shengbo_prelive.action_contracts import capability_manifest, matching_actions, recoverable_input_error
from shengbo_prelive.selection import guard_selected_write, resolve_selection
from test_runtime_business import env, human_command, product, new_run, BindableFakeModel
from test_middleware import FakeEngine


async def import_env(engine, service):
    ref = await product(service, "新商家原稿测试")
    room = (await human_command(service, "create_room", {"name": "原稿入口验收"}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    selected = await human_command(service, "update_preparation_products", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "selected_product_refs": [ref]})
    text = " 大家好，今天介绍这款商品。\n它售价一百七十八元。它售价一百七十八元。\n请按需要选择。  "
    run = await new_run(engine, f"为商品{ref['id']}原样导入现成话术一版，只保存文本，不生成语音", room_id=room["id"], preparation_id=prep["id"])
    source = engine.materials.accept(run["conversation_id"], "原稿.txt", "text/plain", text.encode())["material_ref"]
    engine.store.update("run", run["id"], input={**run["input"], "material_refs": [source]})
    run = engine.store.get("run", run["id"])
    proposal = IntentAssessment(objective="materials_to_script", script_mode="import_original",
        summary="原样导入", target_product_ids=[ref["id"]], requested_script_variants=[{"product_id": ref["id"], "count": 1}],
        authorization_quote=run["input"]["text"])
    run = engine.store.update("run", run["id"], intent_assessment=proposal.model_dump())
    await engine._freeze_goal(run)
    assert engine.store.get("run", run["id"])["phase"] == "execute", engine.store.list("question")
    return engine.store.get("run", run["id"]), RunScope(run["id"], "import-test", "execute"), ref, source, text


@pytest.mark.asyncio
async def test_runtime_lossless_original_import_and_compile_without_model(env):
    engine, service = env
    run, scope, ref, source, text = await import_env(engine, service)
    prep = await service.query("read_preparation", {"preparation_id": run["scope"]["preparation_id"]})
    args = {"preparation_id": prep["ref"]["id"], "expected_revision": prep["ref"]["revision"],
        "product_ref": ref, "variant_id": "merchant-original", "source_ref": source, "locator": "full", "text": text}
    result = await engine.business_command(scope, "import_script", args, "import-once")
    assert result["data"]["provider_calls"] == 0
    assert await engine.business_command(scope, "import_script", args, "import-once") == result
    script = await service.query("read_script", {"ref": result["data"]["script_ref"]})
    rows = script["modules"]["product_intro"]["sentences"]
    assert "".join(x["text"] for x in rows) == text == script["body"]["raw_text"]
    assert script["body"]["source_provenance"]["text_hash"] == canonical_hash(text)
    outcome = await engine.finish(scope, "COMPLETED", "已原样保存全部正式话术。")
    assert outcome["accepted"], outcome
    prep = await service.query("read_preparation", {"preparation_id": prep["ref"]["id"]})
    configured = await human_command(service, "save_session_draft", {"preparation_id": prep["ref"]["id"],
        "expected_revision": prep["ref"]["revision"], "draft": {"theme": "直接原稿播放", "commerce": {}}})
    prep = await service.query("read_preparation", {"preparation_id": prep["ref"]["id"]})
    playlist = await human_command(service, "save_playlist", {"preparation_id": prep["ref"]["id"], "expected_revision": prep["ref"]["revision"],
        "items": [{"occurrence_id": "original-1", "product_id": ref["id"], "script_revision_ref": script["ref"]}]})
    compiled = await human_command(service, "compile_playback_plan", {"preparation_id": prep["ref"]["id"],
        "expected_revision": playlist["data"]["preparation_ref"]["revision"], "playlist_ref": playlist["data"]["playlist_ref"]})
    with service.store.transaction() as c:
        body = service.store.exact(c, compiled["data"]["compiled_plan_ref"])["body"]
    assert "".join(row["text"] for row in body["entries"][0]["sentences"]) == text
    assert body["entries"][0]["opening_mode"] == "merchant_original"
    assert not engine.store.list("branch")


@pytest.mark.asyncio
async def test_import_rejects_rewritten_text_and_foreign_source_without_formal_script(env):
    engine, service = env
    run, scope, ref, source, text = await import_env(engine, service)
    prep = await service.query("read_preparation", {"preparation_id": run["scope"]["preparation_id"]})
    args = {"preparation_id": prep["ref"]["id"], "expected_revision": prep["ref"]["revision"],
        "product_ref": ref, "variant_id": "bad-import", "source_ref": source, "locator": "full", "text": text + "库存仅剩两件"}
    with pytest.raises(ContractError, match="连续原文|SCRIPT_SOURCE_TEXT_MISMATCH") as error:
        await engine.business_command(scope, "import_script", args, "bad-original")
    assert error.value.code == "SCRIPT_SOURCE_TEXT_MISMATCH"
    other = engine.materials.accept("other-conversation", "other.txt", "text/plain", b"foreign")["material_ref"]
    with pytest.raises(ContractError) as denied:
        await engine.business_command(scope, "import_script", {**args, "source_ref": other}, "foreign")
    assert denied.value.status == 403
    assert not (await service.query("read_preparation", {"preparation_id": prep["ref"]["id"]}))["body"]["script_refs"]


@pytest.mark.asyncio
async def test_narrow_sentence_patch_preserves_unselected_exact_objects(env):
    engine, service = env
    run, scope, ref, source, text = await import_env(engine, service)
    prep = await service.query("read_preparation", {"preparation_id": run["scope"]["preparation_id"]})
    result = await engine.business_command(scope, "import_script", {"preparation_id": prep["ref"]["id"],
        "expected_revision": prep["ref"]["revision"], "product_ref": ref, "variant_id": "slice-import",
        "source_ref": source, "locator": "full", "text": text}, "slice-original")
    script = await service.query("read_script", {"ref": result["data"]["script_ref"]})
    rows = script["modules"]["product_intro"]["sentences"]
    target = rows[1]
    prep = await service.query("read_preparation", {"preparation_id": prep["ref"]["id"]})
    patched = await human_command(service, "patch_script", {"preparation_id": prep["ref"]["id"],
        "expected_revision": prep["ref"]["revision"], "script_ref": script["ref"], "module_key": "product_intro",
        "sentence_ids": [target["id"]], "selected_text_hash": canonical_hash([target]), "sentences": ["新替换句子。"]})
    after = await service.query("read_script", {"ref": patched["data"]["script_ref"]})
    revised = after["modules"]["product_intro"]["sentences"]
    assert revised[0] == rows[0] and revised[2:] == rows[2:]
    assert revised[1]["text"] == "新替换句子。"
    assert script["ref"] != after["ref"] and script["body"]["raw_text"] == text
    assert after["body"]["original_raw_text"] == text


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["MATERIAL_CRITICAL_VISUAL_UNREAD", "PLAYLIST_PROPOSAL_INPUT_INVALID", "REVISION_CONFLICT"])
async def test_m5_known_error_returns_to_sdk_model_loop(code):
    engine = FakeEngine()
    @tool
    async def read_source():
        """Read test source at a real tool boundary."""
        raise ContractError(code, "明确输入需要修正", status=409,
            details={"commit_state": "not_committed", "blockers": [{"locator": "image:1"}]})
    model = BindableFakeModel(responses=[AIMessage(content="", tool_calls=[{"name": "read_source", "args": {}, "id": "visual"}]),
        AIMessage(content="发现缺项，可定向补图后继续。")])
    graph = create_agent(model, tools=[read_source], middleware=[ActionBoundary(engine)], context_schema=RunScope)
    out = await graph.ainvoke({"messages": [HumanMessage(content="理解原图")]}, context=RunScope("r1", "i1", "execute"))
    errors = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert len(errors) == 1 and errors[0].status == "error"
    assert out["messages"][-1].content == "发现缺项，可定向补图后继续。"
    assert engine.store.get("run", "r1")["correctable_tool_errors"] == {code: 1}


def test_action_aliases_are_matching_not_authority_and_capabilities_are_reachable():
    assert matching_actions(["request_script_generation"], ["save_script_plan"]) == ["request_script_generation"]
    assert not matching_actions(["不播参数"], ["save_script_plan"])
    assert not matching_actions(["save_product"], [])
    for row in capability_manifest():
        assert row["public_tools"] or row["workflows"] or row["internal_only"] or row["human_only"], row
    for error in (ContractError("MATERIAL_CRITICAL_VISUAL_UNREAD", "test", status=403, details={"commit_state": "not_committed"}),
                  ContractError("MATERIAL_CRITICAL_VISUAL_UNREAD", "test", details={"commit_state": "sent"}),
                  ContractError("SOURCE_INTEGRITY_INVALID", "test")):
        assert not recoverable_input_error(error)


def test_visual_gap_dto_and_readiness_share_aliases_without_rewriting_report():
    from shengbo_prelive.material_understanding import validate_material_assessment, assess_material_readiness
    from test_visual_execution_gate import unread_report, exact_ref
    raw = unread_report(exact_ref("source_manifest", "material"), action="generate_script")["material_assessment"]
    raw["missing_info"][0]["blocked_actions"] = ["save_script_plan"]
    before = canonical_hash(raw)
    parsed = validate_material_assessment(raw)
    ready = assess_material_readiness(parsed, purpose="script_generation", requested_actions=["execute_script_plan"])
    assert not ready["ready"] and ready["blockers"]
    assert canonical_hash(raw) == before


def test_script_workflow_exposes_explicit_local_recovery():
    from shengbo_prelive.workflows import ScriptFlowInput
    assert ScriptFlowInput(action="retry", job_id="job", recovery_mode="revalidate_archived").recovery_mode == "revalidate_archived"
