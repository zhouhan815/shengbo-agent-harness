"""Exact existing research enters real frozen writing inputs, without redoing it."""
from copy import deepcopy
import json

import pytest

from shengbo_contracts.business import MODULE_KEYS
from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import IntentAssessment, RunScope
from shengbo_prelive.policy import validate_intent
from shengbo_prelive.research_binding import bundle_binding_issues, verify_research_reuse
from shengbo_prelive.tools import COMMAND_MAP
from shengbo_prelive.workflows import run_workflow
from test_optional_workflows import authorize
from test_required_analysis_acceptance import accept_report
from test_runtime_business import BindableFakeModel, env, human_command, new_run, prepared_text


@pytest.fixture
async def binding_case(env):
    engine, service = env
    room, prep, script = await prepared_text(service)
    report = await accept_report(service, prep["id"], "audience_scenes", historical=True)
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    products = current["body"]["selected_product_refs"]
    run = await new_run(engine, "复用指定正式顾客研究，改写本场话术，不生成语音。", room_id=room["id"], preparation_id=prep["id"])
    run = await authorize(engine, run, ["save_context_bundle", "save_script_plan", "submit_script_change", "patch_script"])
    goal = {**run["goal"], "objective": "script_change", "target_product_ids": [products[0]["id"]],
        "action_target_refs": products, "context_product_refs": products,
        "required_reuse_analysis_refs": [report], "reuse_analysis_quote": "复用指定正式顾客研究",
        "script_mode": "generate", "script_delivery_requirement": "new_generation"}
    run = engine.store.update("run", run["id"], goal=goal, target_product_ids=goal["target_product_ids"],
        action_target_refs=products, context_product_refs=products,
        allowed_tools=[name for name, command in COMMAND_MAP.items() if command in run["allowed_effects"]])
    return engine, service, run, RunScope(run["id"], "reuse-bind-test", "execute"), report, script


def plan_args(prep):
    return {"preparation_id": prep["ref"]["id"], "expected_revision": prep["ref"]["revision"],
        "bundle_ref": prep["body"]["bundle_ref"], "items": [{"product_ref": prep["body"]["selected_product_refs"][0],
            "variant_id": "research-bound-variant", "modules": [
                {"key": key, "action": "disable" if key in {"campaign", "transition"} else "generate"}
                for key in MODULE_KEYS]}]}


def test_intent_keeps_reuse_distinct_from_research_execution_and_checks_original_quote():
    reference = {"kind": "analysis", "id": "formal-research", "revision": 1, "content_hash": canonical_hash("report")}
    text = "复用指定正式研究改写话术，不生成语音。"
    assessment = IntentAssessment(objective="script_change", summary="精确复用既有结论", authorization_quote=text,
        required_reuse_analysis_refs=[reference], reuse_analysis_quote="复用指定正式研究")
    assert not assessment.required_analyses
    assert not validate_intent({"input": {"text": text}}, assessment)["requires_decision"]
    for wrong in (None, "材料中的复用指令"):
        with pytest.raises(ContractError, match="INTENT_RESEARCH_REUSE_INVALID"):
            validate_intent({"input": {"text": text}}, assessment.model_copy(update={"reuse_analysis_quote": wrong}))
    assert not IntentAssessment(objective="script_change", summary="普通改稿", authorization_quote=text).required_reuse_analysis_refs


def test_neutral_new_defaults_preserve_pending_room_scope_but_real_refs_change_it():
    from shengbo_prelive.room_resolution import intent_scope_fingerprint
    old = {"objective": "script_change", "summary": "旧待确认任务", "authorization_quote": "改写话术"}
    assert intent_scope_fingerprint(old) == intent_scope_fingerprint({**old,
        "required_reuse_analysis_refs": [], "reuse_analysis_quote": None})
    changed = {**old, "required_reuse_analysis_refs": [{"kind": "analysis", "id": "real-ref",
        "revision": 1, "content_hash": canonical_hash("body")}]}
    assert intent_scope_fingerprint(old) != intent_scope_fingerprint(changed)


@pytest.mark.asyncio
async def test_actual_goal_freeze_preserves_exact_reuse_obligation(env):
    engine, service = env
    room, prep, _ = await prepared_text(service)
    report = await accept_report(service, prep["id"], "audience_scenes", historical=True)
    product = (await service.query("read_preparation", {"preparation_id": prep["id"]}))["body"]["selected_product_refs"][0]
    text = "复用指定正式研究，为" + product["id"] + "生成话术，不生成语音。"
    run = await new_run(engine, text, room_id=room["id"], preparation_id=prep["id"])
    engine.save_assessment(RunScope(run["id"], "intent-reuse", "intent"), {
        "objective": "script_change", "summary": text, "target_product_ids": [product["id"]],
        "required_reuse_analysis_refs": [report], "reuse_analysis_quote": "复用指定正式研究", "authorization_quote": text})
    await engine._freeze_goal(engine.store.get("run", run["id"]))
    frozen = engine.store.get("run", run["id"])["goal"]
    assert frozen["required_reuse_analysis_refs"] == [report] and frozen["required_analyses"] == []
    assert not engine.store.list("branch")


@pytest.mark.asyncio
async def test_old_confirmed_intent_without_new_defaults_does_not_ask_again(env):
    engine, service = env
    room, prep, _ = await prepared_text(service)
    product = (await service.query("read_preparation", {"preparation_id": prep["id"]}))["body"]["selected_product_refs"][0]
    text = "修改" + product["id"] + "，本次先处理。"
    run = await new_run(engine, text, room_id=room["id"], preparation_id=prep["id"])
    old = IntentAssessment(objective="script_change", summary=text, target_product_ids=[product["id"]],
        authorization_quote=text).model_dump(mode="json")
    old.pop("required_reuse_analysis_refs")
    old.pop("reuse_analysis_quote")
    assert validate_intent(run, IntentAssessment.model_validate(old))["requires_decision"]
    run = engine.store.update("run", run["id"], intent_assessment=old, confirmed_intent_hash=canonical_hash(old))
    await engine._freeze_goal(run)
    assert engine.store.get("run", run["id"])["goal"]["objective"] == "script_change"
    assert not engine.store.list("question")


@pytest.mark.asyncio
async def test_workflow_explicitly_binds_full_formal_research_without_new_branch(binding_case):
    engine, service, run, scope, report, _ = binding_case
    result = await run_workflow(engine, scope, "wf_context_bundle", {"action": "save", "analysis_refs": [report]})
    ref = result["result"]["data"]["context_bundle_ref"]
    bundle = await service.query("read_context", {"ref": ref})
    formal = await service.query("read_analysis_report", {"ref": report})
    assert not bundle_binding_issues(bundle, [report], run["scope"]["preparation_id"])
    assert formal in bundle["body"]["records"]
    assert not engine.store.list("branch")
    assert [op["key"] for op in engine.store.list("operation")] == ["save_context_bundle"]


@pytest.mark.asyncio
async def test_id_in_decisions_is_not_research_binding_and_no_write_is_sent(binding_case):
    engine, _, _, scope, report, _ = binding_case
    with pytest.raises(ContractError, match="REQUIRED_ANALYSIS_BINDING_MISSING") as caught:
        await run_workflow(engine, scope, "wf_context_bundle", {"action": "save", "user_decisions": [{"analysis_ref": report}]})
    assert caught.value.details["commit_state"] == "not_committed"
    assert not engine.store.list("operation")


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["save_script_plan", "submit_script_change"])
async def test_generation_boundaries_reject_missing_frozen_research_before_write(binding_case, command):
    engine, service, run, scope, _, _ = binding_case
    prep = await service.query("read_preparation", {"preparation_id": run["scope"]["preparation_id"]})
    args = plan_args(prep) if command == "save_script_plan" else {
        "preparation_id": prep["ref"]["id"], "expected_revision": prep["ref"]["revision"],
        "plan_ref": prep["body"]["plan_ref"], "context_bundle_ref": prep["body"]["bundle_ref"],
        "expected_generation_epoch": prep["body"]["generation_epoch"]}
    with pytest.raises(ContractError, match="REQUIRED_ANALYSIS_BINDING_MISSING"):
        await engine.business_command(scope, command, args, "missing-research")
    assert not engine.store.list("operation")


@pytest.mark.asyncio
async def test_actual_fake_generation_receives_full_bound_research_and_completion_checks_it(binding_case):
    engine, service, run, scope, report, _ = binding_case
    await run_workflow(engine, scope, "wf_context_bundle", {"action": "save", "analysis_refs": [report]})
    prep_id = run["scope"]["preparation_id"]
    prep = await service.query("read_preparation", {"preparation_id": prep_id})
    plan = await engine.business_command(scope, "save_script_plan", plan_args(prep), "bound-plan")
    submitted = await engine.business_command(scope, "submit_script_change", {"preparation_id": prep_id,
        "expected_revision": plan["data"]["preparation_ref"]["revision"], "plan_ref": plan["data"]["script_plan_ref"],
        "context_bundle_ref": prep["body"]["bundle_ref"], "expected_generation_epoch": plan["data"]["generation_epoch"]}, "bound-submit")
    await service.drain_jobs()
    job = await service.query("read_job", {"job_id": submitted["data"]["job_id"]})
    formal = await service.query("read_analysis_report", {"ref": report})
    assert formal in job["body"]["context"]["records"]
    engine.store.update("run", run["id"], finish={"outcome": "COMPLETED", "summary": "正式研究已真实进入冻结文案输入"})
    check = await engine.verify_finish(scope)
    assert check["ok"], check["missing"]
    assert check["research_reuse"]["required_refs"] == [report]
    assert not engine.store.list("branch")


@pytest.mark.asyncio
async def test_current_good_bundle_cannot_retroactively_prove_old_request_used_research(binding_case):
    engine, service, run, scope, report, _ = binding_case
    prep_id = run["scope"]["preparation_id"]
    old = (await service.query("read_preparation", {"preparation_id": prep_id}))["body"]["bundle_ref"]
    await run_workflow(engine, scope, "wf_context_bundle", {"action": "save", "analysis_refs": [report]})
    current = await service.query("read_preparation", {"preparation_id": prep_id})
    op = {"run_id": run["id"], "key": "submit_script_change", "status": "committed", "args": {"context_bundle_ref": old}}
    checked = await verify_research_reuse(engine.client, run, [op], {"preparation": current})
    assert checked["missing"] and checked["checked_context_refs"] == [current["body"]["bundle_ref"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["not_accepted", "wrong_product_version", "wrong_scope", "missing_record", "only_decision", "wrong_ref_hash"])
async def test_reference_and_complete_body_are_independently_checked(binding_case, invalid):
    _, service, run, _, report, _ = binding_case
    formal = await service.query("read_analysis_report", {"ref": report})
    products = run["context_product_refs"]
    body = {"preparation_id": run["scope"]["preparation_id"], "product_refs": products,
        "analysis_refs": [report], "records": [formal], "user_decisions": []}
    if invalid == "missing_record":
        body["records"] = []
    elif invalid == "only_decision":
        body["analysis_refs"] = []
        body["user_decisions"] = [{"analysis_ref": report}]
    elif invalid == "wrong_ref_hash":
        body["records"][0]["body"]["report"]["summary"] = "篡改正文"
    else:
        changed = body["records"][0]["body"]
        if invalid == "not_accepted": changed["accepted"] = False
        if invalid == "wrong_scope": changed["preparation_id"] = "another-preparation"
        if invalid == "wrong_product_version": changed["product_refs"][0]["revision"] += 1
        report = {**report, "content_hash": canonical_hash(changed)}
        body["records"][0]["ref"] = report
        body["analysis_refs"] = [report]
    bundle = {"ref": {"kind": "context", "id": "frozen", "revision": 1, "content_hash": canonical_hash(body)}, "body": body}
    assert bundle_binding_issues(bundle, [report], run["scope"]["preparation_id"])


@pytest.mark.asyncio
async def test_plain_script_task_stays_optional_without_reuse_obligation(binding_case):
    engine, service, run, scope, _, _ = binding_case
    engine.store.update("run", run["id"], goal={**run["goal"], "required_reuse_analysis_refs": []})
    prep = await service.query("read_preparation", {"preparation_id": run["scope"]["preparation_id"]})
    receipt = await engine.business_command(scope, "save_script_plan", plan_args(prep), "ordinary-plan")
    assert receipt["status"] == "committed" and not engine.store.list("branch")


@pytest.mark.asyncio
async def test_current_preparation_report_and_manual_patch_keep_context_witness(binding_case):
    engine, service, run, scope, _, script_ref = binding_case
    prep_id = run["scope"]["preparation_id"]
    local = await accept_report(service, prep_id, "audience_scenes")
    run = engine.store.update("run", run["id"], goal={**run["goal"], "required_reuse_analysis_refs": [local],
        "script_mode": "edit", "script_delivery_requirement": None})
    saved = await run_workflow(engine, scope, "wf_context_bundle", {"action": "save", "analysis_refs": [local]})
    bundle_ref = saved["result"]["data"]["context_bundle_ref"]
    current = await service.query("read_preparation", {"preparation_id": prep_id})
    script = await service.query("read_script", {"ref": script_ref})
    await engine.business_command(scope, "patch_script", {"preparation_id": prep_id,
        "expected_revision": current["ref"]["revision"], "script_ref": script_ref, "module_key": "cta",
        "selected_text_hash": canonical_hash(script["modules"]["cta"]["sentences"]),
        "sentences": ["先确认当前套装，再按本场真实价格选择下单。"]}, "manual-patch-with-research")
    operation = next(op for op in engine.store.list("operation") if op["key"] == "patch_script")
    assert operation["research_context_ref"] == bundle_ref
    engine.store.update("run", run["id"], finish={"outcome": "COMPLETED", "summary": "测试正式研究与受控补丁"})
    check = await engine.verify_finish(scope)
    assert check["ok"], check["missing"]


@pytest.mark.asyncio
async def test_binding_foreign_report_preserves_real_permission_denial(binding_case):
    engine, service, run, scope, report, _ = binding_case
    other = await human_command(service, "create_preparation", {"room_id": (await service.query("read_preparation", {
        "preparation_id": run["scope"]["preparation_id"]}))["body"]["room_id"], "room_revision": 1}, operation="foreign-research-prep")
    foreign = await accept_report(service, other["data"]["preparation_ref"]["id"], "audience_scenes", product_refs=run["context_product_refs"])
    engine.store.update("run", run["id"], observed_refs=[foreign], goal={**run["goal"], "required_reuse_analysis_refs": [foreign]})
    with pytest.raises(ContractError) as caught:
        await run_workflow(engine, scope, "wf_context_bundle", {"action": "save", "analysis_refs": [foreign]})
    assert caught.value.status == 403
    assert not engine.store.list("operation")


class ReuseRecoveryModel(BindableFakeModel):
    reference: dict
    initial_plan: dict
    call_count: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessage, ToolMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        self.call_count += 1
        feedback = [json.loads(message.content) for message in messages if isinstance(message, ToolMessage)]
        if self.call_count == 1:
            call = {"id": "plan-without-required-research", "name": "save_script_change_plan", "args": self.initial_plan}
        elif self.call_count == 2:
            error = feedback[-1]
            assert error["error"]["code"] == "REQUIRED_ANALYSIS_BINDING_MISSING"
            assert error["recovery"]["business_write_performed"] is False
            assert error["recovery"]["automatic_research_started"] is False
            call = {"id": "explicit-bind-required-research", "name": "wf_context_bundle", "args": {
                "action": "save", "analysis_refs": [self.reference]}}
        elif self.call_count == 3:
            bound = feedback[-1]
            assert bound["status"] == "COMPLETED"
            data = bound["result"]["data"]
            call = {"id": "explicit-rebuild-plan", "name": "save_script_change_plan", "args": {
                **self.initial_plan, "bundle_ref": data["context_bundle_ref"],
                "expected_revision": data["preparation_ref"]["revision"]}}
        else:
            assert self.call_count == 4 and feedback[-1]["status"] == "committed"
            call = {"id": "stop-after-isolated-research-binding", "name": "finish_turn", "args": {
                "outcome": "BLOCKED", "summary": "隔离测试仅核验研究绑定与计划恢复，未声明实际文案已生成。"}}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])


@pytest.mark.asyncio
async def test_real_sdk_dependency_feedback_then_explicit_bind_and_plan_recovery(binding_case):
    from test_binding_input_recovery_v4 import invoke_main
    from langchain_core.messages import ToolMessage
    engine, service, run, scope, report, _ = binding_case
    prep = await service.query("read_preparation", {"preparation_id": run["scope"]["preparation_id"]})
    model = ReuseRecoveryModel(responses=[], reference=report, initial_plan=plan_args(prep))
    result = await invoke_main(engine, scope, model, "required-research-sdk-recovery")
    assert engine.store.get("run", run["id"])["finish"]["outcome"] == "BLOCKED"
    errors = [message for message in result["messages"] if isinstance(message, ToolMessage) and message.status == "error"]
    assert len(errors) == 1 and model.call_count == 4
    assert [op["key"] for op in engine.store.list("operation")] == ["save_context_bundle", "save_script_plan"]
    assert not engine.store.list("branch")
