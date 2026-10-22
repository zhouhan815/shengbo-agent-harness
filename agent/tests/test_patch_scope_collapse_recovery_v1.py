"""Large whole-module deletion is explicit; exact sentence repair stays free."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from shengbo_contracts.business import PatchScriptArgs, command_wire_candidates, compatible_command_args
from shengbo_contracts.common import ContractError, MutationContext, canonical_hash
from shengbo_prelive.models import IntentAssessment, RunScope
from test_runtime_business import BindableFakeModel, env, human_command, new_run, prepared_text


async def long_case(engine, service, *, selected_index=None):
    room, prep, script_ref = await prepared_text(service)
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    script = await service.query("read_script", {"ref": script_ref})
    seeded = await human_command(service, "patch_script", {"preparation_id": prep["id"],
        "expected_revision": current["ref"]["revision"], "script_ref": script_ref, "module_key": "selling_points",
        "selected_text_hash": canonical_hash(script["modules"]["selling_points"]["sentences"]),
        "sentences": [f"TESTONLY原卖点第{index}句，按真实需要选择。" for index in range(37)]})
    script = await service.query("read_script", {"ref": seeded["data"]["script_ref"]})
    rows = script["modules"]["selling_points"]["sentences"]
    product = script["body"]["product_ref"]
    text = "只修改商品" + product["id"] + "的话术selling_points模块口吻，其余不动，不生成语音。"
    run = await new_run(engine, text, room_id=room["id"], preparation_id=prep["id"])
    if selected_index is not None:
        selection = {"label": "TESTONLY精确句子选区", "scope": "preparation", "preparation_id": prep["id"], "target": {
            "object_ref": script["ref"], "module_key": "selling_points", "sentence_ids": [rows[selected_index]["id"]],
            "selected_text_hash": canonical_hash([rows[selected_index]])}}
        run = engine.store.update("run", run["id"], input={**run["input"], "selection": selection,
            "observed_versions": [script["ref"]]})
    intent = IntentAssessment(objective="script_change", script_mode="edit", summary=text, authorization_quote=text,
        target_product_ids=[product["id"]], requested_modules=["selling_points"])
    run = engine.store.update("run", run["id"], intent_assessment=intent.model_dump())
    await engine._freeze_goal(run)
    run = engine.store.get("run", run["id"])
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    args = {"preparation_id": prep["id"], "expected_revision": current["ref"]["revision"],
        "script_ref": script["ref"], "module_key": "selling_points", "sentence_ids": [],
        "selected_text_hash": canonical_hash(rows), "sentences": ["第一个购买理由，就是一个人也能开始。"]}
    return run, RunScope(run["id"], "patch-scope-test", "execute"), script, args


@pytest.mark.asyncio
async def test_real_voice_guard_rejects_37_to_1_without_writing_and_returns_exact_sentence_targets(env):
    engine, service = env
    run, scope, before, args = await long_case(engine, service)
    with service.store.transaction() as db:
        before_modules = len(service.store.all(db, "module"))
        before_scripts = len(service.store.all(db, "script_revision"))
    with pytest.raises(ContractError, match="PATCH_SCOPE_COLLAPSE_REQUIRES_EXPLICIT_INTENT") as caught:
        await engine.business_command(scope, "patch_script", args, "collapse-precommit")
    details = caught.value.details
    assert caught.value.status == 422 and details["commit_state"] == "not_committed"
    assert details["original_sentence_count"] == 37 and details["replacement_sentence_count"] == 1
    assert details["reader"] == {"tool": "read_script", "args": {"ref": before["ref"], "sections": ["selling_points"]}}
    rows = before["modules"]["selling_points"]["sentences"]
    assert details["original_sentence_ids"] == [row["id"] for row in rows]
    assert details["single_sentence_edit_targets"] == [{"sentence_ids": [row["id"]], "selected_text_hash": canonical_hash([row])} for row in rows]
    with service.store.transaction() as db:
        assert len(service.store.all(db, "module")) == before_modules
        assert len(service.store.all(db, "script_revision")) == before_scripts
    prep = await service.query("read_preparation", {"preparation_id": args["preparation_id"]})
    assert prep["body"]["script_refs"] == [before["ref"]]


@pytest.mark.asyncio
async def test_explicit_whole_module_intent_allows_autonomous_shortening(env):
    engine, service = env
    _, scope, before, args = await long_case(engine, service)
    result = await engine.business_command(scope, "patch_script", {**args, "intentional_full_module_replacement": True}, "explicit-whole-shorten")
    after = await service.query("read_script", {"ref": result["data"]["script_ref"]})
    assert len(after["modules"]["selling_points"]["sentences"]) == 1
    assert after["body"]["parent_ref"] == before["ref"]
    assert {key: ref for key, ref in after["body"]["module_refs"].items() if key != "selling_points"} == {
        key: ref for key, ref in before["body"]["module_refs"].items() if key != "selling_points"}


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_count", [8, 36, 37])
async def test_ordinary_module_edits_remain_compatible_without_intent_flag(env, replacement_count):
    engine, service = env
    _, scope, _, args = await long_case(engine, service)
    result = await engine.business_command(scope, "patch_script", {**args,
        "sentences": [f"TESTONLY修订后卖点第{index}句。" for index in range(replacement_count)]}, "ordinary-module-edit")
    after = await service.query("read_script", {"ref": result["data"]["script_ref"]})
    assert len(after["modules"]["selling_points"]["sentences"]) == replacement_count


@pytest.mark.asyncio
async def test_explicit_flag_cannot_expand_exact_ui_sentence_scope_in_agent_or_voice(env):
    engine, service = env
    run, scope, before, args = await long_case(engine, service, selected_index=7)
    expanded = {**args, "intentional_full_module_replacement": True}
    with pytest.raises(ContractError) as local:
        await engine.business_command(scope, "patch_script", expanded, "cannot-expand-selected-sentence")
    assert local.value.status == 403
    with pytest.raises(ContractError) as remote:
        await engine.client.command("patch_script", expanded, MutationContext(operation_id="testonly-direct-expand",
            grant_id=run["grant_id"], expected_write_generation=run["write_generation"]), scope=run["scope"], run_id=run["id"])
    assert remote.value.status == 403
    current = await service.query("read_preparation", {"preparation_id": args["preparation_id"]})
    assert current["body"]["script_refs"] == [before["ref"]]


def test_new_false_flag_is_wire_neutral_but_true_is_a_distinct_explicit_request():
    args = {"preparation_id": "p", "expected_revision": 1,
        "script_ref": {"kind": "script_revision", "id": "s", "revision": 1, "content_hash": canonical_hash("s")},
        "module_key": "selling_points", "selected_text_hash": canonical_hash([]), "sentences": ["替换正文。"]}
    wire = PatchScriptArgs.model_validate(args).model_dump(mode="json", exclude_none=True)
    assert "intentional_full_module_replacement" not in wire and "sentence_ids" not in wire
    assert compatible_command_args("patch_script", {**wire, "intentional_full_module_replacement": False}) == wire
    assert {**wire, "intentional_full_module_replacement": False} in command_wire_candidates("patch_script", wire)
    assert compatible_command_args("patch_script", {**wire, "intentional_full_module_replacement": True}) != wire
    with pytest.raises(ValidationError):
        PatchScriptArgs.model_validate({**args, "intentional_full_module_replacement": "true"})


@pytest.mark.parametrize("original_count,replacement_count,blocked", [(9, 1, False), (10, 2, True),
    (10, 3, False), (37, 7, True), (37, 8, False)])
def test_scope_threshold_is_a_narrow_row_count_signal(original_count, replacement_count, blocked):
    from types import SimpleNamespace
    from app.prelive.script_patch_scope import guard_patch_scope
    from app.prelive.storage import BusinessError
    args = SimpleNamespace(sentence_ids=[], intentional_full_module_replacement=False, module_key="selling_points")
    original = {"sentences": [{"id": str(index), "text": "TESTONLY原文"} for index in range(original_count)]}
    replacement = {"sentences": [{"id": str(index), "text": "TESTONLY新文"} for index in range(replacement_count)]}
    if blocked:
        with pytest.raises(BusinessError) as caught:
            guard_patch_scope(args, {"ref": {}}, original, replacement)
        assert caught.value.code == "PATCH_SCOPE_COLLAPSE_REQUIRES_EXPLICIT_INTENT"
    else:
        guard_patch_scope(args, {"ref": {}}, original, replacement)


class SentenceScopeRecoveryModel(BindableFakeModel):
    bad_args: dict
    call_count: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessage, ToolMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        self.call_count += 1
        feedback = [json.loads(message.content) for message in messages if isinstance(message, ToolMessage)]
        if self.call_count == 1:
            call = {"name": "patch_script", "id": "wrong-whole-module-scope", "args": self.bad_args}
        elif self.call_count == 2:
            error = feedback[-1]
            assert error["error"]["code"] == "PATCH_SCOPE_COLLAPSE_REQUIRES_EXPLICIT_INTENT"
            assert error["recovery"]["existing_modules_preserved"] is True
            assert error["recovery"]["commit_state"] == "not_committed"
            reader = error["error"]["details"]["reader"]
            call = {"name": reader["tool"], "id": "read-exact-original-sentences", "args": reader["args"]}
        elif self.call_count == 3:
            assert len(feedback[-1]["modules"]["selling_points"]["sentences"]) == 37
            error = next(value for value in feedback if value.get("error", {}).get("code") == "PATCH_SCOPE_COLLAPSE_REQUIRES_EXPLICIT_INTENT")
            target = error["error"]["details"]["single_sentence_edit_targets"][7]
            call = {"name": "patch_script", "id": "explicit-single-sentence-repair", "args": {**self.bad_args, **target}}
        else:
            assert self.call_count == 4 and feedback[-1]["status"] == "committed"
            call = {"name": "finish_turn", "id": "finish-isolated-scope-test", "args": {
                "outcome": "BLOCKED", "summary": "TESTONLY仅验证单句范围恢复，不替生产内容验收或人工批准。"}}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])


@pytest.mark.asyncio
async def test_real_sdk_feedback_then_exact_sentence_repair_preserves_36_other_sentences(env):
    from test_binding_input_recovery_v4 import invoke_main
    from langchain_core.messages import ToolMessage
    engine, service = env
    run, scope, before, args = await long_case(engine, service)
    model = SentenceScopeRecoveryModel(responses=[], bad_args=args)
    result = await invoke_main(engine, scope, model, "patch-scope-collapse-sdk-recovery")
    errors = [message for message in result["messages"] if isinstance(message, ToolMessage) and message.status == "error"]
    assert len(errors) == 1 and model.call_count == 4
    prep = await service.query("read_preparation", {"preparation_id": args["preparation_id"]})
    after = await service.query("read_script", {"ref": prep["body"]["script_refs"][0]})
    old_rows, new_rows = before["modules"]["selling_points"]["sentences"], after["modules"]["selling_points"]["sentences"]
    assert len(new_rows) == 37 and new_rows[7]["text"] == args["sentences"][0]
    assert new_rows[:7] == old_rows[:7] and new_rows[8:] == old_rows[8:]
    assert engine.store.get("run", run["id"])["correctable_tool_errors"]["PATCH_SCOPE_COLLAPSE_REQUIRES_EXPLICIT_INTENT"] == 1
