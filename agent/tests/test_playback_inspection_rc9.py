"""F05 inspect is a pure dependency view, never a hidden pipeline."""
from copy import deepcopy

import pytest

from shengbo_contracts.common import ContractError,canonical_hash
from shengbo_prelive.models import RunScope
from shengbo_prelive.workflows import review_inspection,run_workflow
from test_optional_workflows import authorize
from test_runtime_business import env,human_command,new_run,prepared_text


def ref(kind):
    return {"kind":kind,"id":kind+"-exact","revision":3,"content_hash":canonical_hash([kind,3])}


def preparation(**updates):
    body={"script_refs":[],"playlist_ref":None,"compiled_plan_ref":None,"requirements_ref":None,"approval_ref":None}
    body.update(updates)
    return {"ref":ref("preparation"),"body":body}


def test_no_scripts_never_proposes_empty_playlist_or_fabricated_order():
    p=preparation();original=deepcopy(p)
    result=review_inspection(p,["submit_script_change"])
    assert p==original
    assert result["inspection_completed"] is True and result["preparation_readiness"]=="not_evaluated"
    assert result["missing_dependency_fields"]==list(p["body"])
    assert result["optional_next_actions"][0]["tool"]=="wf_script_execution"
    assert result["optional_next_actions"][0]["arguments"]=={"action":"status"}
    assert all(a.get("kind")!="playlist_order" for a in result["optional_next_actions"])
    assert result["text_review"]["status"]=="not_verified"


def test_present_scripts_request_true_user_decision_without_creating_items_or_order():
    scripts=[ref("script_revision"),{**ref("script_revision"),"id":"other-exact"}]
    p=preparation(script_refs=scripts);original=deepcopy(p)
    result=review_inspection(p,[])
    assembly,action=result["optional_next_actions"]
    assert assembly["tool"]=="wf_playback_review"
    assert assembly["arguments_template"]["action"]=="prepare_order"
    assert assembly["available_script_refs"]==scripts and assembly["order_proposed"] is False
    assert "items" not in assembly and "occurrence_id" not in assembly
    assert action["tool"]=="request_user_decision" and action["kind"]=="playlist_order"
    assert action["available_script_refs"]==scripts and action["order_proposed"] is False
    assert "goal_effect_allowed" not in action,"A question itself is not a business write permission"
    assert action["required_context_paths"]==["context.playlist.items","context.playlist.loop_mode"]
    assert "items" not in action and "occurrence_id" not in action
    assert result["preparation_ref"]==p["ref"] and p==original
    result["observed_dependencies"]["script_refs"][0]["id"]="changed-view"
    assert p==original,"Returned view must not mutate source references"


@pytest.mark.parametrize("missing",["compiled_plan_ref","requirements_ref","both"])
@pytest.mark.parametrize("allowed",[True,False])
def test_compile_is_only_an_option_with_goal_effect_label_not_authorization(missing,allowed):
    p=preparation(script_refs=[ref("script_revision")],playlist_ref=ref("playlist"),
        compiled_plan_ref=None if missing in {"compiled_plan_ref","both"} else ref("compiled_plan"),
        requirements_ref=None if missing in {"requirements_ref","both"} else ref("requirements"))
    result=review_inspection(p,["compile_playback_plan"] if allowed else [])
    action,=result["optional_next_actions"]
    assert action["tool"]=="wf_playback_review" and action["arguments"]=={"action":"compile"}
    assert action["goal_effect_allowed"] is allowed
    assert result["preparation_readiness"]=="not_evaluated"
    assert result["text_review"]["status"]=="not_verified"


@pytest.mark.parametrize("approval",[None,ref("approval")])
def test_all_refs_present_do_not_claim_valid_review_approval_or_ready(approval):
    p=preparation(script_refs=[ref("script_revision")],playlist_ref=ref("playlist"),
        compiled_plan_ref=ref("compiled_plan"),requirements_ref=ref("requirements"),approval_ref=approval)
    result=review_inspection(p,["prepare_required_audio"])
    assert result["text_review"]["status"]=="not_verified"
    assert result["approval_observation"]["status"]==("present_not_verified" if approval else "not_observed")
    assert result["approval_observation"]["ref"]==approval
    action=result["optional_next_actions"][0]
    assert action["tool"]=="read_text_review" and action["arguments"]=={"preparation_id":p["ref"]["id"]}
    assert result["optional_next_actions"][1]["arguments"]["kind"]=="text_audio_approval"
    assert not any(a["tool"] in {"prepare_required_audio","publish_preparation","start_prepared_room"} for a in result["optional_next_actions"])
    assert "not_evaluated"==result["preparation_readiness"]


@pytest.mark.asyncio
@pytest.mark.parametrize("compiled",[False,True])
async def test_real_inspect_preserves_prep_and_assets_with_only_two_existing_queries(env,monkeypatch,compiled):
    engine,service=env
    if compiled:
        room,pref,_=await prepared_text(service)
    else:
        room=(await human_command(service,"create_room",{"name":"纯检查依赖"}))["data"]["room_ref"]
        pref=(await human_command(service,"create_preparation",{"room_id":room["id"],"room_revision":room["revision"]}))["data"]["preparation_ref"]
    run=await new_run(engine,"检查现有本场，不自动修改",room_id=room["id"],preparation_id=pref["id"])
    await authorize(engine,run,["save_asset_checklist"])
    before=await service.query("read_preparation",{"preparation_id":pref["id"]})
    assets=await service.query("read_asset_checklist",{"preparation_id":pref["id"]})
    calls=[];original_query=engine.client.query
    async def tracked(key,args=None,**kwargs):
        calls.append(key);return await original_query(key,args,**kwargs)
    async def unexpected_write(*args,**kwargs):raise AssertionError("inspect must not execute any business command")
    monkeypatch.setattr(engine.client,"query",tracked)
    monkeypatch.setattr(engine.client,"command",unexpected_write)
    result=await run_workflow(engine,RunScope(run["id"],"inspect-only","execute"),"wf_playback_review",{"action":"inspect"})
    assert calls==["read_preparation","read_asset_checklist"],"No completion observation or extra review HTTP read"
    assert result["status"]=="COMPLETED" and result["result"]["preparation"]==before
    assert result["result"]["asset_checklist"]==assets
    inspection=result["result"]["inspection"]
    assert inspection["preparation_ref"]==before["ref"]
    assert inspection["preparation_readiness"]=="not_evaluated" and inspection["text_review"]["status"]=="not_verified"
    assert result["remaining_steps"],"COMPLETED is the inspect action, not preparation readiness"
    assert not engine.store.list("operation")
    assert not engine.store.list("question")
    assert (await service.query("read_preparation",{"preparation_id":pref["id"]}))==before


@pytest.mark.asyncio
async def test_inspect_options_never_expand_actual_compile_permission(env):
    engine,service=env
    room,pref,_=await prepared_text(service)
    run=await new_run(engine,"只检查素材",room_id=room["id"],preparation_id=pref["id"])
    await authorize(engine,run,["save_asset_checklist"])
    with pytest.raises(ContractError,match="GOAL_EFFECT_DENIED"):
        await run_workflow(engine,RunScope(run["id"],"no-compile-authority","execute"),"wf_playback_review",{"action":"compile"})
    assert not engine.store.list("operation") and not engine.store.list("question")
