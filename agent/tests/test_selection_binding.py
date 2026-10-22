"""Explicit UI selections constrain the real runtime and isolated voice service."""
from copy import deepcopy

import pytest

from test_runtime_business import env, human_command, product, prepared_text
from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import IntentAssessment, MessageRequest, RunScope


async def freeze(engine, text, selection, objective, *, prep=None, room=None, targets=None):
    scope = {"kind": "preparation", "preparation_id": prep["id"]} if prep else {"kind": "catalog"}
    conversation = engine.create_conversation({"scope": scope, "room_id": (room or {}).get("id")})
    request = MessageRequest(text=text, intent_mode="new_task", scope=scope, selection=selection,
                             observed_versions=[selection["target"]["object_ref"]])
    result = await engine.submit_message(conversation["id"], request)
    run = engine.store.get("run", result["run"]["id"])
    intent = IntentAssessment(objective=objective, summary=text, authorization_quote=text,
                              target_product_ids=targets or [])
    run = engine.store.update("run", run["id"], intent_assessment=intent.model_dump())
    await engine._freeze_goal(run)
    run = engine.store.get("run", run["id"])
    return run, RunScope(run["id"], "selection-test", "execute")


def select(ref, *, field=None, module=None, prep=None, digest=None):
    target = {"object_ref": ref}
    if field:
        target["field_path"] = field
    if module:
        target.update(module_key=module, selected_text_hash=digest)
    return {"label": "测试左侧明确选择", "scope": "preparation" if prep else "catalog",
            **({"preparation_id": prep["id"]} if prep else {}), "target": target}


@pytest.mark.asyncio
async def test_product_field_binding_is_projected_and_enforced_with_idempotent_receipt(env):
    engine, service = env
    ref = await product(service)
    selection = select(ref, field="/price")
    run, scope = await freeze(engine, "把选中的价格更新为180", selection, "catalog_update")
    assert run["target_product_ids"] == [ref["id"]]
    assert run["allowed_product_paths"] == ["/price"]
    context = await engine.project_context(scope)
    assert context["selection"] == selection
    assert context["observed_versions"] == [ref]
    assert context["selection_binding"]["base_ref"] == ref
    body = (await service.query("read_product", {"product_id": ref["id"]}))["body"]
    fields = {key: body[key] for key in service.schema()["schema"]["product"]["properties"] if key in body}
    fields["price"] = 180
    args = {"product_id": ref["id"], "expected_revision": ref["revision"], "schema_hash": service.schema()["schema_hash"],
            "fields": fields, "patches": [{"path": "/price", "op": "set", "value": 180}]}
    receipt = await engine.business_command(scope, "save_product", args, "selected-price-write")
    assert receipt["data"]["product_ref"]["revision"] == ref["revision"] + 1
    assert await engine.business_command(scope, "save_product", args, "selected-price-write") == receipt
    bad = deepcopy(args)
    bad["expected_revision"] += 1
    bad["patches"] = [{"path": "/name", "op": "set", "value": "不应改名"}]
    bad["fields"]["name"] = "不应改名"
    with pytest.raises(ContractError, match="FIELD_SCOPE_DENIED"):
        await engine.business_command(scope, "save_product", bad, "other-field")
    assert (await service.query("read_product", {"product_id": ref["id"]}))["body"]["name"] == body["name"]


@pytest.mark.asyncio
async def test_stale_product_ref_does_not_gain_write_permission_but_read_only_can_explain(env):
    engine, service = env
    ref = await product(service)
    old = await service.query("read_product", {"product_id": ref["id"]})
    fields = {key: old["body"][key] for key in service.schema()["schema"]["product"]["properties"] if key in old["body"]}
    fields["price"] = 188
    await human_command(service, "save_product", {"product_id": ref["id"], "expected_revision": ref["revision"],
        "schema_hash": service.schema()["schema_hash"], "fields": fields,
        "patches": [{"path": "/price", "op": "set", "value": 188}]})
    with pytest.raises(ContractError, match="SELECTION_STALE"):
        await freeze(engine, "修改选中商品价格", select(ref, field="/price"), "catalog_update")
    run, _ = await freeze(engine, "解释这个旧版商品价格", select(ref, field="/price"), "read_explain")
    assert run["allowed_effects"] == []
    assert run["selection_binding"]["base_ref"] == ref


@pytest.mark.asyncio
async def test_selected_product_cannot_expand_to_even_named_second_product(env):
    engine, service = env
    first = await product(service)
    second = await product(service, "另外一个商品")
    with pytest.raises(ContractError, match="SELECTION_TARGET_DENIED"):
        await freeze(engine, "更新这两个商品 " + second["id"], select(first), "catalog_update", targets=[second["id"]])


@pytest.mark.asyncio
async def test_script_module_binding_rejects_wrong_hash_wrong_scope_and_extra_module(env):
    engine, service = env
    room, prep, script = await prepared_text(service)
    text = await service.query("read_script", {"ref": script})
    digest = canonical_hash(text["modules"]["selling_points"]["sentences"])
    selected = select(script, module="selling_points", prep=prep, digest=digest)
    run, scope = await freeze(engine, "帮我润色一下，更口语一点", selected, "script_change", prep=prep, room=room)
    assert run["target_product_ids"] == [text["body"]["product_id"]]
    assert run["module_allowlist"] == ["selling_points"]
    assert MessageRequest(text="test", observed_versions=[script]).observed_versions[0].kind == "script_revision"
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    args = {"preparation_id": prep["id"], "expected_revision": current["ref"]["revision"], "script_ref": script,
            "module_key": "opening_short", "selected_text_hash": canonical_hash(text["modules"]["opening_short"]["sentences"]),
            "sentences": ["不应修改未选中的短开场"]}
    with pytest.raises(ContractError, match="MODULE_SCOPE_DENIED"):
        await engine.business_command(scope, "patch_script", args, "wrong-module")
    args.update(module_key="selling_points", selected_text_hash=digest, sentences=["真实局部更新的卖点"])
    receipt = await engine.business_command(scope, "patch_script", args, "only-selected-module")
    updated = await service.query("read_script", {"ref": receipt["data"]["script_ref"]})
    assert [row["text"] for row in updated["modules"]["selling_points"]["sentences"]] == args["sentences"]
    assert updated["body"]["module_refs"]["opening_short"] == text["body"]["module_refs"]["opening_short"]
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    args.update(script_ref=receipt["data"]["script_ref"], expected_revision=current["ref"]["revision"],
                selected_text_hash=canonical_hash(updated["modules"]["selling_points"]["sentences"]), sentences=["再次只调整同一卖点"])
    second = await engine.business_command(scope, "patch_script", args, "selected-module-second-pass")
    latest = await service.query("read_script", {"ref": second["data"]["script_ref"]})
    assert latest["body"]["module_refs"]["opening_short"] == text["body"]["module_refs"]["opening_short"]
    invalid = select(second["data"]["script_ref"], module="selling_points", prep=prep, digest="sha256:wrong")
    with pytest.raises(ContractError, match="SELECTION_STALE"):
        await freeze(engine, "修改选中话术", invalid, "script_change", prep=prep, room=room)
    other = (await human_command(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}, operation="another-isolated-preparation"))["data"]["preparation_ref"]
    with pytest.raises(ContractError, match="SELECTION_SCOPE_CONFLICT"):
        await freeze(engine, "修改选中话术", selected, "script_change", prep=other, room=room)


@pytest.mark.asyncio
async def test_selected_script_plan_preserves_unselected_modules_and_cannot_execute_arbitrary_plan(env):
    engine, service = env
    room, prep, script = await prepared_text(service)
    text = await service.query("read_script", {"ref": script})
    selected = select(script, module="selling_points", prep=prep,
                      digest=canonical_hash(text["modules"]["selling_points"]["sentences"]))
    _, scope = await freeze(engine, "修改选中话术卖点", selected, "script_change", prep=prep, room=room)
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    modules = [{"key": key, "action": "generate" if key == "selling_points" else "reuse" if value else "disable",
                **({"base_module_ref": value} if value else {})} for key, value in text["body"]["module_refs"].items()]
    args = {"preparation_id": prep["id"], "expected_revision": current["ref"]["revision"], "bundle_ref": current["body"]["bundle_ref"],
            "items": [{"product_ref": text["body"]["product_ref"], "base_script_ref": script,
                       "variant_id": text["body"]["variant_id"], "modules": modules}]}
    invalid = deepcopy(args)
    next(action for action in invalid["items"][0]["modules"] if action["key"] == "opening_short")["action"] = "generate"
    with pytest.raises(ContractError, match="MODULE_SCOPE_DENIED"):
        await engine.business_command(scope, "save_script_plan", invalid, "wrong-plan")
    receipt = await engine.business_command(scope, "save_script_plan", args, "selected-plan")
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    fake_plan = {**receipt["data"]["script_plan_ref"], "id": "other-plan"}
    execute = {"preparation_id": prep["id"], "expected_revision": current["ref"]["revision"], "plan_ref": fake_plan,
               "context_bundle_ref": current["body"]["bundle_ref"], "expected_generation_epoch": current["body"]["generation_epoch"]}
    with pytest.raises(ContractError, match="SELECTION_PLAN_DENIED"):
        await engine.business_command(scope, "submit_script_change", execute, "arbitrary-existing-plan")


@pytest.mark.asyncio
async def test_session_field_selection_compares_full_draft_not_only_label(env):
    engine, service = env
    room, prep, _ = await prepared_text(service)
    selected = select(prep, field="/draft/theme", prep=prep)
    _, scope = await freeze(engine, "更新选中的本场主题", selected, "session_config", prep=prep, room=room)
    snapshot = await service.query("get_workspace_snapshot", {"preparation_id": prep["id"]})
    draft = snapshot["preparation_details"]["draft"]["body"]["draft"]
    projection = await engine.project_context(scope)
    exact = engine.run_history(scope, {"full": True, "ref": projection["selection_binding"]["session_draft"]["full_ref"]})
    assert exact["document"]["draft"] == draft
    changed = {**draft, "theme": "新场主题", "objective": "不能偷偷添加的另一个字段"}
    args = {"preparation_id": prep["id"], "expected_revision": prep["revision"], "draft": changed}
    with pytest.raises(ContractError, match="FIELD_SCOPE_DENIED"):
        await engine.business_command(scope, "save_session_draft", args, "extra-session-field")
    args["draft"] = {**draft, "theme": "新场主题"}
    receipt = await engine.business_command(scope, "save_session_draft", args, "selected-theme")
    assert receipt["data"]["preparation_ref"]["revision"] == prep["revision"] + 1
    snapshot = await service.query("get_workspace_snapshot", {"preparation_id": prep["id"]})
    assert snapshot["preparation_details"]["draft"]["body"]["draft"] == {**draft, "theme": "新场主题"}


@pytest.mark.asyncio
async def test_selected_audience_report_is_readable_and_only_revises_same_research(env):
    engine, service = env
    room, prep, script = await prepared_text(service)
    old_script = await service.query("read_script", {"ref": script})
    p = old_script["body"]["product_ref"]
    report = {"summary": "之前的顾客假设", "product_ids": [p["id"]], "fields": {},
              "insights": [{"scene": "居家"}], "evidence": [], "hypotheses": ["待真实验证"],
              "conflicts": [], "missing_fields": [], "asset_suggestions": [], "source_refs": [], "dependencies": [p]}
    accepted = await human_command(service, "accept_analysis_report", {"role": "audience_scenes", "report": report,
        "report_hash": canonical_hash(report), "product_refs": [p], "readset": [p],
        "preparation_id": prep["id"], "expected_revision": prep["revision"]})
    prep = accepted["data"]["preparation_ref"]
    selection = select(accepted["data"]["analysis_ref"], prep=prep)
    run, scope = await freeze(engine, "更新这份顾客研究，保留待验证标记", selection, "audience_research", prep=prep, room=room)
    assert run["target_product_ids"] == [p["id"]]
    projection = await engine.project_context(scope)
    full = engine.run_history(scope, {"full": True, "ref": projection["selection_binding"]["analysis_report"]["full_ref"]})
    assert full["document"]["report"] == report
    revised = {**report, "summary": "更具体的顾客假设", "hypotheses": ["仍待真实用户验证"]}
    args = {"role": "audience_scenes", "report": revised, "report_hash": canonical_hash(revised),
            "product_refs": [p], "readset": [p], "preparation_id": prep["id"], "expected_revision": prep["revision"]}
    wrong = {**args, "role": "product_facts"}
    with pytest.raises(ContractError, match="SELECTION_TARGET_DENIED"):
        await engine.business_command(scope, "accept_analysis_report", wrong, "wrong-research-role")
    result = await engine.business_command(scope, "accept_analysis_report", args, "same-audience-research")
    assert result["data"]["analysis_ref"] != accepted["data"]["analysis_ref"]
    assert engine.store.get("run", run["id"])["selection_binding"]["current_ref"] == result["data"]["analysis_ref"]


@pytest.mark.asyncio
async def test_selected_write_unknown_result_reconciles_then_advances_exact_binding(env):
    from shengbo_prelive.client import RemoteOutcomeUnknown
    engine, service = env
    ref = await product(service)
    run, scope = await freeze(engine, "更新选中价格", select(ref, field="/price"), "catalog_update")
    body = (await service.query("read_product", {"product_id": ref["id"]}))["body"]
    fields = {key: body[key] for key in service.schema()["schema"]["product"]["properties"] if key in body}
    fields["price"] = 181
    args = {"product_id": ref["id"], "expected_revision": ref["revision"], "schema_hash": service.schema()["schema_hash"],
            "fields": fields, "patches": [{"path": "/price", "op": "set", "value": 181}]}
    original = engine.client.command
    sends = []
    async def commit_then_disconnect(key, args, mutation, **kwargs):
        receipt = await original(key, args, mutation, **kwargs)
        sends.append(receipt)
        raise RemoteOutcomeUnknown(mutation.operation_id)
    engine.client.command = commit_then_disconnect
    with pytest.raises(RemoteOutcomeUnknown):
        await engine.business_command(scope, "save_product", args, "lost-selected-receipt")
    current = engine.store.get("run", run["id"])
    assert current["selection_binding"]["current_ref"] == ref
    engine.client.command = original
    assert await engine._reconcile_pending_operation(current)
    current = engine.store.get("run", run["id"])
    assert current["selection_binding"]["current_ref"] == sends[0]["data"]["product_ref"]
    assert len(sends) == 1
    assert await engine.business_command(scope, "save_product", args, "lost-selected-receipt") == sends[0]
