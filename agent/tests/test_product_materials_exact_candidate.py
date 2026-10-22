"""Exact accepted-candidate transfer over real isolated Agent/Voice state."""
from copy import deepcopy

import pytest

from shengbo_contracts.common import ContractError, Principal, canonical_hash
from shengbo_prelive.models import RunScope
from shengbo_prelive.product_materials import SaveAcceptedProductInput, save_accepted_product
from test_runtime_business import env, human_command, Product
from test_source_alias_bridge import accepted_sources


async def case(engine, service, fields=None):
    run, scope, sources, _, _ = await accepted_sources(engine, service)
    candidate = fields if fields is not None else Product(name="精确候选新商品", price=129, sku_id="EXACT-CANDIDATE",
        selling_points="按来源保留的卖点。", faq="资料只说明48小时安排发货，未说明送达。",
        knowledge="本候选据原始资料整理；未知仍未知。", material="EVA").model_dump()
    branch_id = "exact-product-facts"
    engine.store.put("branch", {"id": branch_id, "run_id": run["id"], "role": "product_facts", "status": "RUNNING",
        "task": {"role": "product_facts", "instruction": "整理精确商品候选", "product_ids": [],
            "source_refs": sources, "input_refs": []}})
    engine.submit_report(RunScope(run["id"], "facts-worker", "execute", "product_facts", branch_id),
        {"summary": "商家来源支持的候选，未接受前不写商品。", "fields": candidate, "source_refs": sources})
    reference = (await engine.accept_reports(scope, [branch_id], "accept-candidate"))["accepted_refs"][0]
    return run, scope, candidate, branch_id, reference


def stats(service):
    with service.store.transaction() as db:
        return {name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in
            ("records", "record_revisions", "prelive_operations", "prelive_events", "prelive_sequences")}


def assembly(service, ref):
    with service.store.transaction() as db:
        return service.store.exact(db, ref)["body"]


@pytest.mark.asyncio
async def test_exact_candidate_create_receipt_sources_resolution_and_replay(env):
    engine, service = env
    run, scope, fields, branch, ref = await case(engine, service)
    before_branch = deepcopy(engine.store.get("branch", branch))
    args = {"branch_task_id": branch, "accepted_report_ref": ref}
    result = await save_accepted_product(engine, scope, args, tool_call_id="candidate-create")
    saved = (await service.query("read_product", {"ref": result["data"]["product_ref"]}))["body"]
    assert {key: saved[key] for key in fields} == fields
    assert saved["faq"].startswith("资料只说明") and "本候选据" in saved["knowledge"]
    assert engine.store.get("branch", branch) == before_branch
    assert result["facts_transfer"]["automatic_selection"] is False
    assert result["facts_transfer"]["automatic_research_binding"] is False
    assert result["facts_transfer"]["approval_implied"] is False
    resolution = engine.store.get("product_candidate_save_resolution", "candidate-create")
    assert resolution["input_hash"] == canonical_hash(SaveAcceptedProductInput.model_validate(args).model_dump(mode="json", exclude_none=True))
    assert resolution["command_hash"] == canonical_hash(resolution["command_args"])
    op = next(o for o in engine.store.list("operation") if o["key"] == "save_product")
    assert op["args"] == resolution["command_args"] and op["status"] == "committed"
    formal = (await service.query("read_analysis_report", {"ref": ref}))["body"]
    assert op["args"]["source_manifest_refs"] == formal["report"]["source_refs"]
    assert all(source["id"].startswith("source_manifest_") for source in op["args"]["source_manifest_refs"])
    counts = stats(service)
    again = await save_accepted_product(engine, scope, args, tool_call_id="candidate-create")
    assert again["data"]["product_ref"] == result["data"]["product_ref"] and stats(service) == counts
    prep = await service.query("read_preparation", {"preparation_id": run["scope"]["preparation_id"]})
    assert prep["body"]["selected_product_refs"] == []
    assert prep["body"]["script_refs"] == [] and prep["body"]["approval_ref"] is None
    assert not engine.store.list("question")


@pytest.mark.asyncio
async def test_only_explicit_set_and_clear_override_candidate_without_mutating_report(env):
    engine, service = env
    _, scope, original, branch, ref = await case(engine, service)
    result = await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref,
        "overrides": [{"path": "/faq", "op": "set", "value": "明确的新问答。"}, {"path": "/material", "op": "clear"}]},
        tool_call_id="candidate-overrides")
    saved = (await service.query("read_product", {"ref": result["data"]["product_ref"]}))["body"]
    assert saved["faq"] == "明确的新问答。" and saved["material"] == ""
    assert {k: saved[k] for k in original if k not in {"faq", "material"}} == {k: v for k, v in original.items() if k not in {"faq", "material"}}
    assert engine.store.get("branch", branch)["report"]["fields"] == original


@pytest.mark.asyncio
async def test_update_uses_explicit_exact_target_and_preserves_fields_absent_from_report(env, monkeypatch):
    engine, service = env
    run, scope, _, branch, ref = await case(engine, service, {"faq": "新问答。"})
    fields = Product(name="现有目标", price=179, material="原有材质", sku_id="OLD-EXACT", knowledge="不能被缺项清空").model_dump()
    target = (await human_command(service, "save_product", {"schema_hash": service.schema()["schema_hash"], "patches": [], "fields": fields}))["data"]["product_ref"]
    engine.store.update("run", run["id"], target_product_ids=[target["id"]], allowed_product_paths=["/faq"])
    queries = []
    original_query = engine.business_query
    async def counted_query(current_scope, key, args):
        queries.append(key)
        return await original_query(current_scope, key, args)
    monkeypatch.setattr(engine, "business_query", counted_query)
    result = await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref,
        "product_ref": target}, tool_call_id="candidate-update")
    saved = (await service.query("read_product", {"ref": result["data"]["product_ref"]}))["body"]
    assert saved["faq"] == "新问答。"
    assert {k: saved[k] for k in fields if k != "faq"} == {k: v for k, v in fields.items() if k != "faq"}
    operation = next(o for o in engine.store.list("operation") if o["key"] == "save_product")
    assert operation["args"]["patches"] == [{"path": "/faq", "op": "set", "value": "新问答。"}]
    assert operation["args"]["expected_revision"] == target["revision"]
    audit = assembly(service, result["data"]["assembly_ref"])
    dispositions = {row["path"]: row for row in audit["field_dispositions"]}
    assert set(dispositions) == {"/" + key for key in fields}
    assert dispositions["/faq"]["action"] == "set"
    assert dispositions["/faq"]["source_refs"] == operation["args"]["source_manifest_refs"]
    assert dispositions["/material"]["action"] == "preserve"
    assert dispositions["/material"]["source_refs"] == [target]
    assert "未重新核验原始来源" in dispositions["/material"]["reason"]
    assert dispositions["/knowledge"]["source_refs"] == [target]
    assert queries == ["read_context", "get_business_schema", "read_product"]
    assert audit["sources"] == operation["args"]["source_manifest_refs"]
    assert engine.store.get("branch", branch)["accepted_ref"] == ref
    prep = await service.query("read_preparation", {"preparation_id": run["scope"]["preparation_id"]})
    assert prep["body"]["selected_product_refs"] == [] and prep["body"]["approval_ref"] is None
    counts = stats(service)
    replay = await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref,
        "product_ref": target}, tool_call_id="candidate-update")
    assert replay["data"]["product_ref"] == result["data"]["product_ref"] and stats(service) == counts


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["prepared", "wrong_role", "wrong_run", "report_hash", "prepared_hash", "accepted_hash", "mutated_report", "missing_commit"])
async def test_local_accepted_label_never_replaces_formal_hash_and_receipted_lineage(env, tamper):
    engine, service = env
    _, scope, _, branch_id, ref = await case(engine, service)
    branch = engine.store.get("branch", branch_id)
    if tamper == "prepared": engine.store.update("branch", branch_id, status="PREPARED")
    if tamper == "wrong_role": engine.store.update("branch", branch_id, role="audience_scenes")
    if tamper == "wrong_run": engine.store.update("branch", branch_id, run_id="different-run")
    if tamper == "report_hash": engine.store.update("branch", branch_id, report_hash="sha256:" + "0"*64)
    if tamper == "prepared_hash": engine.store.update("branch", branch_id, prepared_report_hash="sha256:" + "0"*64)
    if tamper == "accepted_hash": engine.store.update("branch", branch_id, accepted_report_hash="sha256:" + "0"*64)
    if tamper == "mutated_report":
        report = deepcopy(branch["report"]); report["fields"]["price"] = 999
        engine.store.update("branch", branch_id, report=report, report_hash=canonical_hash(report), prepared_report_hash=canonical_hash(report))
    if tamper == "missing_commit":
        for operation in engine.store.list("operation"):
            if operation["key"] == "accept_analysis_report" and operation["args"].get("prepared_report_ref", {}).get("id") == branch_id:
                engine.store.update("operation", operation["id"], status="known_failed")
    before = stats(service)
    with pytest.raises(ContractError):
        await save_accepted_product(engine, scope, {"branch_task_id": branch_id, "accepted_report_ref": ref}, tool_call_id="candidate-denied")
    assert stats(service) == before
    assert not engine.store.list("product_candidate_save_resolution")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_fields", [{"product": {"name": "不自动展平", "price": 129}}, {"secret-extra-key": "do-not-log", "name": "错字段", "price": 129}, {}])
async def test_unrecognized_candidate_shape_is_rejected_without_silent_field_loss(env, bad_fields):
    engine, service = env
    _, scope, _, branch, ref = await case(engine, service, bad_fields)
    before = stats(service)
    with pytest.raises(ContractError, match="FACTS_SAVE_FIELDS_INVALID") as error:
        await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref}, tool_call_id="bad-fields")
    assert "secret-extra-key" not in str(error.value.details)
    assert stats(service) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    [{"path": "/faq", "op": "set", "value": "一"}, {"path": "/faq", "op": "set", "value": "二"}],
    [{"path": "/specifications/0/value", "op": "set", "value": "不支持嵌套"}],
    [{"path": "/id", "op": "set", "value": "not-a-target"}],
    [{"path": "/faq", "op": "preserve", "value": "不准带值"}],
    [{"path": "/price", "op": "clear"}],
])
async def test_overrides_do_not_allow_implicit_paths_duplicate_assignment_or_required_clear(env, overrides):
    engine, service = env
    _, scope, _, branch, ref = await case(engine, service)
    before = stats(service)
    with pytest.raises(ContractError):
        await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref,
            "overrides": overrides}, tool_call_id="bad-overrides")
    assert stats(service) == before


@pytest.mark.asyncio
async def test_native_product_validation_keeps_unmodified_failure_and_resolution_proof(env):
    engine, service = env
    run, scope, _, branch, ref = await case(engine, service, {"name": "格式错候选", "price": [129]})
    before = stats(service)
    with pytest.raises(ContractError) as error:
        await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref}, tool_call_id="invalid-product")
    assert error.value.code == "PRODUCT_ASSEMBLY_INVALID" and error.value.status == 422
    resolution = engine.store.get("product_candidate_save_resolution", "invalid-product")
    operation = engine.store.get("operation", error.value.known_operation_id)
    assert operation["run_id"] == run["id"] and operation["status"] == "known_failed"
    assert operation["args"] == resolution["command_args"]
    assert operation["request_hash"] == resolution["command_hash"] == error.value.known_operation_input_hash
    assert stats(service) == before


@pytest.mark.asyncio
async def test_unknown_and_missing_effect_stop_before_transfer_or_resolution(env):
    engine, service = env
    run, scope, _, branch, ref = await case(engine, service)
    before = stats(service)
    args = {"branch_task_id": branch, "accepted_report_ref": ref}
    engine.store.update("run", run["id"], pending_unknown_operation="op-unknown")
    with pytest.raises(ContractError, match="RECONCILIATION_REQUIRED"):
        await save_accepted_product(engine, scope, args, tool_call_id="not-retry")
    engine.store.update("run", run["id"], pending_unknown_operation=None, allowed_effects=[])
    with pytest.raises(ContractError, match="GOAL_EFFECT_DENIED"):
        await save_accepted_product(engine, scope, args, tool_call_id="not-authorized")
    assert stats(service) == before and not engine.store.list("product_candidate_save_resolution")


@pytest.mark.asyncio
async def test_same_name_remains_human_identity_gate_then_real_proof_can_create_distinct(env):
    engine, service = env
    _, scope, _, branch, ref = await case(engine, service)
    fields = Product(name="精确候选新商品", price=90, sku_id="EXISTING-DIFFERENT-SKU").model_dump()
    old = (await human_command(service, "save_product", {"schema_hash": service.schema()["schema_hash"], "patches": [], "fields": fields}))["data"]["product_ref"]
    args = {"branch_task_id": branch, "accepted_report_ref": ref}
    before = stats(service)
    with pytest.raises(ContractError, match="PRODUCT_IDENTITY_CONFIRMATION_REQUIRED"):
        await save_accepted_product(engine, scope, args, tool_call_id="needs-identity")
    assert stats(service) == before
    human = service.record_identity_decision("精确候选新商品", [old], principal=Principal(kind="human"))
    result = await save_accepted_product(engine, scope, {**args, "identity_decision_ref": human["identity_decision_ref"]}, tool_call_id="identity-approved")
    assert result["data"]["product_ref"]["id"] != old["id"]


@pytest.mark.asyncio
async def test_stale_product_cas_and_field_scope_cannot_be_bypassed(env):
    engine, service = env
    run, scope, _, branch, ref = await case(engine, service, {"faq": "新问答", "price": 99})
    original = Product(name="只允许FAQ", price=129, sku_id="CAS-TARGET").model_dump()
    old = (await human_command(service, "save_product", {"schema_hash": service.schema()["schema_hash"], "patches": [], "fields": original}))["data"]["product_ref"]
    engine.store.update("run", run["id"], target_product_ids=[old["id"]], allowed_product_paths=["/faq"])
    before = stats(service)
    args = {"branch_task_id": branch, "accepted_report_ref": ref, "product_ref": old}
    with pytest.raises(ContractError, match="FIELD_SCOPE_DENIED"):
        await save_accepted_product(engine, scope, args, tool_call_id="field-scope")
    assert stats(service) == before
    engine.store.update("run", run["id"], allowed_product_paths=[])
    await human_command(service, "save_product", {"product_id": old["id"], "expected_revision": old["revision"],
        "schema_hash": service.schema()["schema_hash"], "patches": [{"path": "/faq", "op": "set", "value": "人工新版本"}],
        "fields": {**original, "faq": "人工新版本"}})
    before = stats(service)
    with pytest.raises(ContractError, match="REVISION_CONFLICT"):
        await save_accepted_product(engine, scope, args, tool_call_id="stale-cas")
    assert stats(service) == before


def test_four_field_record_refs_preserve_representation_and_explicit_bad_schema_is_rejected():
    analysis = {"kind": "analysis", "id": "analysis-exact", "revision": 1, "content_hash": "sha256:" + "1"*64}
    product = {"kind": "product", "id": "product-exact", "revision": 2, "content_hash": "sha256:" + "2"*64}
    value = SaveAcceptedProductInput.model_validate({"branch_task_id": "facts", "accepted_report_ref": analysis,
        "product_ref": product}).model_dump(mode="json", exclude_none=True)
    assert value["accepted_report_ref"] == analysis and value["product_ref"] == product
    with pytest.raises(ContractError, match="FACTS_SAVE_REF_INVALID"):
        SaveAcceptedProductInput.model_validate({"branch_task_id": "facts", "accepted_report_ref": analysis,
            "product_ref": {**product, "schema_version": "WRONG"}})


@pytest.mark.asyncio
async def test_minimum_native_required_fields_do_not_require_full_candidate_schema(env):
    engine, service = env
    _, scope, fields, branch, ref = await case(engine, service, {"name": "仅原业务必填", "price": 129})
    result = await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref},
        tool_call_id="minimum-fields")
    saved = (await service.query("read_product", {"ref": result["data"]["product_ref"]}))["body"]
    assert {key: saved[key] for key in fields} == fields
    operation = next(o for o in engine.store.list("operation") if o["key"] == "save_product")
    assert operation["args"]["fields"] == fields
    assert operation["args"]["field_dispositions"] == []


@pytest.mark.asyncio
async def test_preserve_override_keeps_original_and_coupled_fields_are_not_silently_rewritten(env):
    engine, service = env
    run, scope, _, branch, ref = await case(engine, service, {"faq": "候选问答", "selling_points": "候选卖点"})
    original = Product(name="显式保留目标", price=129, sku_id="PRESERVE-TARGET", faq="原问答",
        selling_points="原卖点", core_selling_points=["原卖点"]).model_dump()
    target = (await human_command(service, "save_product", {"schema_hash": service.schema()["schema_hash"],
        "fields": original, "patches": []}))["data"]["product_ref"]
    engine.store.update("run", run["id"], target_product_ids=[target["id"]], allowed_product_paths=[])
    before = stats(service)
    with pytest.raises(ContractError) as error:
        await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref,
            "product_ref": target, "overrides": [{"path": "/faq", "op": "preserve"}]}, tool_call_id="preserve-no-coupling-guess")
    assert error.value.code in {"ASSEMBLY_PATCH_MISMATCH", "CANONICAL_FIELD_CONFLICT"}
    assert stats(service) == before
    resolution = engine.store.get("product_candidate_save_resolution", "preserve-no-coupling-guess")
    assert resolution["command_args"]["fields"]["faq"] == "原问答"
    assert resolution["command_args"]["fields"]["core_selling_points"] == ["原卖点"]
    assert resolution["command_args"]["fields"]["selling_points"] == "候选卖点"
    assert {p["path"] for p in resolution["command_args"]["patches"]} == {"/faq", "/selling_points"}


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["set", "clear"])
async def test_update_explicit_override_has_no_invented_report_source_and_preserve_uses_old_ref(env, op):
    engine, service = env
    run, scope, candidate, branch, ref = await case(engine, service,
        {"faq": "报告问答", "material": "报告材质", "shipping_time": "报告发货时间"})
    original = Product(name="覆盖来源目标", price=129, sku_id="OVERRIDE-LINEAGE",
        faq="旧问答", material="旧材质").model_dump()
    target = (await human_command(service, "save_product", {"schema_hash": service.schema()["schema_hash"],
        "fields": original, "patches": []}))["data"]["product_ref"]
    engine.store.update("run", run["id"], target_product_ids=[target["id"]], allowed_product_paths=[])
    override = {"path": "/faq", "op": op, **({"value": "显式替换问答"} if op == "set" else {})}
    result = await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref,
        "product_ref": target, "overrides": [override, {"path": "/material", "op": "preserve"}]},
        tool_call_id="explicit-source-" + op)
    saved = (await service.query("read_product", {"ref": result["data"]["product_ref"]}))["body"]
    audit = assembly(service, result["data"]["assembly_ref"])
    rows = {row["path"]: row for row in audit["field_dispositions"]}
    assert set(rows) == {"/" + key for key in original}
    assert saved["faq"] == ("显式替换问答" if op == "set" else "")
    assert rows["/faq"]["action"] == op and rows["/faq"]["source_refs"] == []
    assert rows["/faq"]["knowledge"] == ("known" if op == "set" else "unknown")
    assert saved["material"] == "旧材质"
    assert rows["/material"]["action"] == "preserve" and rows["/material"]["source_refs"] == [target]
    assert rows["/shipping_time"]["source_refs"] == audit["sources"] and audit["sources"]
    assert engine.store.get("branch", branch)["report"]["fields"] == candidate
    assert {key: saved[key] for key in original if key not in {"faq", "shipping_time"}} == {
        key: value for key, value in original.items() if key not in {"faq", "shipping_time"}}


@pytest.mark.asyncio
async def test_explicit_complete_field_dispositions_are_not_rewritten_by_default_lineage(env):
    engine, service = env
    run, scope, _, branch, ref = await case(engine, service, {"faq": "新问答"})
    original = Product(name="明确处置目标", price=129, sku_id="EXPLICIT-DISPOSITIONS", material="旧材质").model_dump()
    target = (await human_command(service, "save_product", {"schema_hash": service.schema()["schema_hash"],
        "fields": original, "patches": []}))["data"]["product_ref"]
    engine.store.update("run", run["id"], target_product_ids=[target["id"]], allowed_product_paths=[])
    dispositions = [{"path": "/" + key, "action": "set" if key == "faq" else "preserve",
        "knowledge": "unknown", "source_refs": [target], "reason": "明确保留的自定义未知状态"}
        for key in sorted(original)]
    result = await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref,
        "product_ref": target, "field_dispositions": dispositions}, tool_call_id="explicit-dispositions")
    operation = next(o for o in engine.store.list("operation") if o["key"] == "save_product")
    assert operation["args"]["field_dispositions"] == dispositions
    assert assembly(service, result["data"]["assembly_ref"])["field_dispositions"] == dispositions


@pytest.mark.asyncio
async def test_explicit_partial_dispositions_are_not_automatically_completed(env):
    engine, service = env
    run, scope, _, branch, ref = await case(engine, service, {"faq": "新问答"})
    original = Product(name="部分处置仍拒绝", price=129, sku_id="PARTIAL-DISPOSITIONS").model_dump()
    target = (await human_command(service, "save_product", {"schema_hash": service.schema()["schema_hash"],
        "fields": original, "patches": []}))["data"]["product_ref"]
    engine.store.update("run", run["id"], target_product_ids=[target["id"]], allowed_product_paths=[])
    before = stats(service)
    explicit = [{"path": "/faq", "action": "set", "knowledge": "unknown", "source_refs": [], "reason": "只有一项"}]
    with pytest.raises(ContractError, match="INCOMPLETE_FIELD_DISPOSITIONS"):
        await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": ref,
            "product_ref": target, "field_dispositions": explicit}, tool_call_id="do-not-complete")
    assert stats(service) == before
    resolution = engine.store.get("product_candidate_save_resolution", "do-not-complete")
    assert resolution["command_args"]["field_dispositions"] == explicit
