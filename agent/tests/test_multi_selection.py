"""Exact unions of field selections use the real runtime and isolated service."""
from copy import deepcopy

import pytest

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import IntentAssessment, MessageRequest, RunScope
from shengbo_prelive.selection import resolve_selection, guard_selected_write, advance_selection_binding
from shengbo_prelive.editor_locks import project_editor_locks
from test_runtime_business import env, product, prepared_text
from test_selection_binding import select


async def freeze_multi(engine, rows, objective="catalog_update", *, prep=None, room=None):
    scope = {"kind": "preparation", "preparation_id": prep["id"]} if prep else {"kind": "catalog"}
    conversation = engine.create_conversation({"scope": scope, "room_id": (room or {}).get("id")})
    text = ("修改左侧明确选中的话术模块，保留其他模块" if objective == "script_change"
            else "修改左侧明确选中的字段，保留其他资料")
    receipt = await engine.submit_message(conversation["id"], MessageRequest(text=text, intent_mode="new_task",
        scope=scope, selection={"label": "已选多项", "items": rows}))
    run = engine.store.update("run", receipt["run"]["id"], intent_assessment=IntentAssessment(
        objective=objective, summary=text, authorization_quote=text).model_dump())
    await engine._freeze_goal(run)
    return engine.store.get("run", run["id"]), RunScope(run["id"], "multi-test", "execute")


def fields(service, body):
    return {key: body[key] for key in service.schema()["schema"]["product"]["properties"] if key in body}


@pytest.mark.asyncio
async def test_multi_fields_commit_union_keep_unselected_and_advance_refs(env):
    engine, service = env
    ref = await product(service)
    run, scope = await freeze_multi(engine, [select(ref, field="/price"), select(ref, field="/knowledge")])
    assert set(run["allowed_product_paths"]) == {"/price", "/knowledge"}
    assert run["target_product_ids"] == [ref["id"]]
    body = fields(service, (await service.query("read_product", {"ref": ref}))["body"])
    body.update(price=180, knowledge="清洗后请晾干。")
    args = {"product_id": ref["id"], "expected_revision": ref["revision"], "schema_hash": service.schema()["schema_hash"],
            "fields": body, "patches": [{"path": "/" + key, "op": "set", "value": body[key]} for key in ("price", "knowledge")]}
    receipt = await engine.business_command(scope, "save_product", args, "multi-save")
    latest = engine.store.get("run", run["id"])
    assert all(x["current_ref"] == receipt["data"]["product_ref"] for x in latest["selection_binding"]["items"])
    bad = deepcopy(args)
    bad["expected_revision"] += 1
    bad["fields"]["name"] = "不应修改的名称"
    bad["patches"] = [{"path": "/name", "op": "set", "value": bad["fields"]["name"]}]
    with pytest.raises(ContractError, match="FIELD_SCOPE_DENIED"):
        await engine.business_command(scope, "save_product", bad, "outside-multi")
    targets = project_editor_locks(engine.store)["locks"][0]["targets"]
    product_locks = [x for x in targets if x["kind"] == "product"]
    assert {path for x in product_locks for path in x["field_paths"]} == {"/price", "/knowledge"}


@pytest.mark.asyncio
async def test_cross_product_selection_does_not_exchange_field_authority(env):
    engine, service = env
    a, b = await product(service), await product(service, "第二个隔离商品")
    run, scope = await freeze_multi(engine, [select(a, field="/price"), select(b, field="/knowledge")])
    assert set(run["target_product_ids"]) == {a["id"], b["id"]}
    value = fields(service, (await service.query("read_product", {"ref": b}))["body"])
    value["price"] = 200
    args = {"product_id": b["id"], "expected_revision": b["revision"], "fields": value,
            "schema_hash": service.schema()["schema_hash"], "patches": [{"path": "/price", "op": "set", "value": 200}]}
    with pytest.raises(ContractError, match="FIELD_SCOPE_DENIED"):
        await engine.business_command(scope, "save_product", args, "cannot-use-other-price")


@pytest.mark.asyncio
async def test_canonical_points_allow_only_identical_compatibility_projection(env):
    engine, service = env
    ref = await product(service)
    run, scope = await freeze_multi(engine, [select(ref, field="/core_selling_points")])
    assert set(run["allowed_product_paths"]) == {"/core_selling_points", "/selling_points"}
    value = fields(service, (await service.query("read_product", {"ref": ref}))["body"])
    value.update(core_selling_points=["便携：方便日常携带"], selling_points="便携：方便日常携带")
    args = {"product_id": ref["id"], "expected_revision": ref["revision"], "schema_hash": service.schema()["schema_hash"],
            "fields": value, "patches": [{"path": "/" + key, "op": "set", "value": value[key]} for key in ("core_selling_points", "selling_points")]}
    bad = deepcopy(args)
    bad["fields"]["selling_points"] = "不一致的兼容正文"
    with pytest.raises(ContractError, match="FIELD_SCOPE_DENIED"):
        await guard_selected_write(engine.client, run, "save_product", bad)
    saved = await engine.business_command(scope, "save_product", args, "points-projection")
    assert saved["data"]["product_ref"]["revision"] == ref["revision"] + 1


@pytest.mark.asyncio
async def test_collection_rejects_duplicates_nested_and_stale_items(env):
    engine, service = env
    ref = await product(service)
    run = {"input": {}, "scope": {"kind": "catalog"}}
    row = select(ref, field="/price")
    for items in ([row, row], [{"items": [row]}], [], [row] * 21, [{}]):
        with pytest.raises(ContractError, match="SELECTION_INVALID"):
            await resolve_selection(engine.client, {**run, "input": {"selection": {"items": items}}}, writing=True)
    stale = deepcopy(row)
    stale["target"]["object_ref"]["revision"] += 1
    with pytest.raises(ContractError):
        await resolve_selection(engine.client, {**run, "input": {"selection": {"items": [row, stale]}}}, writing=True)


@pytest.mark.asyncio
async def test_multiple_script_modules_form_one_plan_preserving_other_modules(env):
    engine, service = env
    room, prep, script = await prepared_text(service)
    text = await service.query("read_script", {"ref": script})
    chosen = ("selling_points", "product_intro")
    rows = [select(script, module=key, prep=prep, digest=canonical_hash(text["modules"][key]["sentences"])) for key in chosen]
    run, scope = await freeze_multi(engine, rows, "script_change", prep=prep, room=room)
    assert set(run["module_allowlist"]) == set(chosen)
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    modules = [{"key": key, "action": "generate" if key in chosen else "reuse" if value else "disable",
                **({"base_module_ref": value} if value else {})} for key, value in text["body"]["module_refs"].items()]
    args = {"preparation_id": prep["id"], "expected_revision": current["ref"]["revision"], "bundle_ref": current["body"]["bundle_ref"],
        "items": [{"product_ref": text["body"]["product_ref"], "base_script_ref": script, "variant_id": text["body"]["variant_id"], "modules": modules}]}
    receipt = await engine.business_command(scope, "save_script_plan", args, "multi-module-plan")
    assert receipt["data"]["script_plan_ref"]
    bad = deepcopy(args)
    next(x for x in bad["items"][0]["modules"] if x["key"] == "cta")["action"] = "generate"
    with pytest.raises(ContractError, match="MODULE_SCOPE_DENIED"):
        await guard_selected_write(engine.client, engine.store.get("run", run["id"]), "save_script_plan", bad)


def test_receipt_advancement_does_not_cross_script_or_replace_other_module_sentences():
    ref = {"kind": "script_revision", "id": "s1", "revision": 1, "content_hash": "h1"}
    other = {"kind": "script_revision", "id": "s2", "revision": 1, "content_hash": "h2"}
    product_ref = {"kind": "product", "id": "p1", "revision": 1, "content_hash": "p1"}
    a = {"kind": "script_revision", "current_ref": ref, "product_ref": product_ref, "variant_id": "v1",
         "module_key": "selling_points", "sentence_ids": ["a"], "selected_text_hash": "hash-a"}
    b = {**a, "module_key": "cta", "sentence_ids": ["b"], "selected_text_hash": "hash-b"}
    c = {**a, "current_ref": other, "variant_id": "v2"}
    bindings = {"kind": "selection_set", "items": [a, b, c], "product_refs": [product_ref]}
    plan = {"kind": "script_plan", "id": "plan-a"}
    advanced = advance_selection_binding(bindings, "save_script_plan", {"data": {"script_plan_ref": plan}},
        {"items": [{"base_script_ref": ref, "product_ref": product_ref, "variant_id": "v1"}]})
    assert advanced["items"][0]["validated_plan_ref"] == plan
    assert "validated_plan_ref" not in advanced["items"][2]
    new_ref = {**ref, "id": "s1-new", "revision": 2}
    patched = advance_selection_binding(advanced, "patch_script", {"data": {"script_ref": new_ref,
        "replacement": {"sentence_ids": ["a-new"]}}}, {"script_ref": ref, "module_key": "selling_points"})
    assert patched["items"][0]["sentence_ids"] == ["a-new"]
    assert patched["items"][1]["current_ref"] == new_ref
    assert patched["items"][1]["sentence_ids"] == ["b"]
    assert patched["items"][1]["selected_text_hash"] == "hash-b"
