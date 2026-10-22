"""Declared unread visual blockers reach actual effects; no model calls."""
import pytest

from shengbo_contracts.common import ArtifactRef, ContractError, MutationContext, canonical_hash
from shengbo_prelive.models import RunScope
from shengbo_prelive.product_materials import save_accepted_product
from test_runtime_business import env, Product, product, human_command
from test_source_alias_bridge import accepted_sources
from test_visual_understanding_harness import image_branch, visual_report


async def authorized(engine, *, product_ids=(), preparation_id=None):
    run, source, child = await image_branch(engine)
    if preparation_id:
        run = engine.store.update("run", run["id"], scope={"kind": "preparation", "account_id": "local", "preparation_id": preparation_id})
    effects = ["save_product", "save_script_plan", "submit_script_change", "patch_script",
               "import_blob", "accept_analysis_report"]
    grant = await engine.client.command("issue_grant", {"run_id": run["id"], "scope": run["scope"],
        "capabilities": effects, "target_constraints": []},
        MutationContext(operation_id=run["id"] + "-visual-grant"), run_id=run["id"], controller=True)
    run = engine.store.update("run", run["id"], allowed_effects=effects,
        target_product_ids=list(product_ids), created_product_ids=[], receipts=[],
        goal={"objective": "prepare_session", "scope": run["scope"]},
        grant_id=grant["data"]["grant_id"], write_generation=grant["data"]["write_generation"])
    return run, source, child, RunScope(run["id"], "visual-main", "execute")


def unread_report(source, action="save_product", *, product_id=None, extra_candidate=False):
    report = visual_report(source, status="unreadable", read_receipt_ids=[], relevance="unknown",
        summary="关键规格图尚不可辨认，不能补造参数。", critical_for_actions=[action], missing_info_ids=["spec"])
    candidate = report["material_assessment"]["product_candidates"][0]
    if product_id:
        candidate.update(product_id=product_id, identity_status="existing")
    if extra_candidate:
        report["material_assessment"]["product_candidates"].append(
            {"candidate_id": "safe", "name": "其他商品", "identity_status": "new"})
    report["material_assessment"]["missing_info"] = [{"item_id": "spec", "product_candidate_id": "sample",
        "field_path": "specifications", "level": "block_required", "why": "唯一规格图未读",
        "question": "请补清晰规格图", "can_skip": False, "blocked_actions": [action]}]
    return report


def save_args(service, name="示意商品", **changes):
    return {"fields": Product(name=name, price=129).model_dump(), "patches": [],
            "schema_hash": service.schema()["schema_hash"], **changes}


def exact_ref(kind, identity):
    return ArtifactRef(kind=kind, id=identity, revision=1, content_hash=canonical_hash(identity)).model_dump(mode="json")


def plan_args(ref):
    return {"preparation_id": "prep", "expected_revision": 1, "bundle_ref": exact_ref("context", "bundle"),
        "items": [{"product_ref": ref, "variant_id": "v", "modules": [
            {"key": "product_intro", "action": "generate"}]}]}


@pytest.mark.asyncio
async def test_actual_product_save_blocks_declared_target_but_allows_other_product_and_updated_report(env):
    engine, service = env
    affected, other = await product(service, "规格待核商品"), await product(service, "无关商品")
    run, source, child, main = await authorized(engine, product_ids=[affected["id"], other["id"]])
    report = unread_report(source, product_id=affected["id"])
    engine.submit_report(child, report)
    current = (await service.query("read_product", {"ref": affected}))["body"]
    args = save_args(service, "规格待核商品", product_id=affected["id"], expected_revision=affected["revision"],
                     fields={key:current[key] for key in Product.model_fields})
    with pytest.raises(ContractError) as denied:
        await engine.business_command(main, "save_product", args, "blocked-product")
    assert denied.value.code == "MATERIAL_CRITICAL_VISUAL_UNREAD"
    assert denied.value.details["commit_state"] == "not_committed"
    assert not [op for op in engine.store.list("operation") if op.get("key") == "save_product"]
    current_other = (await service.query("read_product", {"ref": other}))["body"]
    unrelated = await engine.business_command(main, "save_product", save_args(service, "无关商品",
        product_id=other["id"], expected_revision=other["revision"],
        fields={key:current_other[key] for key in Product.model_fields}), "other-product")
    assert unrelated["data"]["product_ref"]["id"] == other["id"]
    # Reading remains possible, and replacing the current report really closes
    # this declaration instead of retaining a stale visual-review cache gate.
    assert (await engine.read_source(main, source, "overview"))["source_material_ref"] == source
    receipt = engine.materials.record_visual_delivery(source, run["conversation_id"], locator="image:1",
        run_id=run["id"], branch_task_id=child.branch_task_id)
    report["material_assessment"]["visual_reviews"][0].update(status="reviewed", read_receipt_ids=[receipt["id"]])
    engine.submit_report(child, report)
    saved = await engine.business_command(main, "save_product", args, "after-visual-read")
    assert saved["data"]["product_ref"]["id"] == affected["id"]


@pytest.mark.asyncio
async def test_no_id_candidate_and_source_match_cannot_skip_save_gate_but_other_candidate_can(env):
    engine, service = env
    run, source, child, main = await authorized(engine)
    engine.submit_report(child, unread_report(source))
    with pytest.raises(ContractError, match="MATERIAL_CRITICAL_VISUAL_UNREAD"):
        await engine.business_command(main, "save_product", save_args(service), "new-no-id")
    with pytest.raises(ContractError, match="MATERIAL_CRITICAL_VISUAL_UNREAD"):
        await engine._guard_declared_visual_actions(main, run, "save_product",
            save_args(service, "改名候选", source_manifest_refs=[source]), "same-source-renamed")
    engine.submit_report(child, unread_report(source, extra_candidate=True))
    # One shared source can contain another explicitly declared candidate.
    await engine._guard_declared_visual_actions(main, run, "save_product",
        save_args(service, "其他商品", source_manifest_refs=[source]), "safe-shared-source")
    saved = await engine.business_command(main, "save_product", save_args(service, "其他商品"), "safe-new")
    assert saved["data"]["product_ref"]["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["save_product", "request_script_generation"])
async def test_candidate_convenience_save_keeps_blocker_after_explicit_name_override(env, action):
    engine, service = env
    run, main, sources, _, _ = await accepted_sources(engine, service)
    source = engine.materials.accept(run["conversation_id"], "关键规格.png", "image/png",
        __import__("test_merchant_material_readers").image_bytes())["material_ref"]
    engine.store.update("run", run["id"], input={**run["input"], "material_refs": [*sources, source]})
    branch = "visual-new-candidate"
    engine.store.put("branch", {"id": branch, "run_id": run["id"], "role": "product_facts", "status": "RUNNING",
        "task": {"role": "product_facts", "instruction": "整理本次候选", "source_refs": [source], "input_refs": [], "product_ids": []}})
    report = unread_report(source, action, extra_candidate=True)
    report["fields"] = Product(name="示意商品", price=129).model_dump()
    engine.submit_report(RunScope(run["id"], "candidate-worker", "execute", "product_facts", branch), report)
    reference = (await engine.accept_reports(main, [branch], "accept-with-save-gap"))["accepted_refs"][0]
    parameters = {"branch_task_id": branch, "accepted_report_ref": reference,
        "overrides": [{"path": "/name", "op": "set", "value": "显式改名商品"}]}
    if action == "save_product":
        with pytest.raises(ContractError, match="MATERIAL_CRITICAL_VISUAL_UNREAD"):
            await save_accepted_product(engine, main, parameters, tool_call_id="renamed-candidate-save")
        assert not [op for op in engine.store.list("operation") if op.get("key") == "save_product"]
    else:
        saved = await save_accepted_product(engine, main, parameters, tool_call_id="renamed-candidate-save")
        with pytest.raises(ContractError, match="MATERIAL_CRITICAL_VISUAL_UNREAD"):
            await engine._guard_declared_visual_actions(main, engine.store.get("run", run["id"]), "save_script_plan",
                plan_args(saved["data"]["product_ref"]), "renamed-candidate-script")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["save_script_plan", "request_script_generation", "execute_script_plan"])
async def test_script_action_aliases_block_actual_plan_save_only_for_affected_product(env, action):
    engine, service = env
    affected = await product(service)
    room = (await human_command(service, "create_room", {"name": "视觉门禁测试"}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    run, source, child, main = await authorized(engine, product_ids=[affected["id"]], preparation_id=prep["id"])
    engine.submit_report(child, unread_report(source, action, product_id=affected["id"]))
    args = plan_args(affected)
    args["preparation_id"] = prep["id"]
    with pytest.raises(ContractError, match="MATERIAL_CRITICAL_VISUAL_UNREAD"):
        await engine.business_command(main, "save_script_plan", args, "blocked-plan")
    await engine._guard_declared_visual_actions(main, run, "save_script_plan", plan_args(exact_ref("product", "other")), "other-plan")
    # A save-only declaration does not prohibit script planning.
    engine.submit_report(child, unread_report(source, "save_product", product_id=affected["id"]))
    await engine._guard_declared_visual_actions(main, run, "save_script_plan", plan_args(affected), "unrelated-action")


@pytest.mark.asyncio
@pytest.mark.parametrize("historical_plan", [False, True])
async def test_generation_uses_exact_receipted_plan_subset_and_new_candidate_created_identity(env, monkeypatch, historical_plan):
    engine, service = env
    run, source, child, main = await authorized(engine)
    created = await engine.business_command(main, "save_product", save_args(service), "create-before-gap")
    affected = created["data"]["product_ref"]
    engine.submit_report(child, unread_report(source, "request_script_generation"))
    plan_ref = exact_ref("script_plan", "already-saved-plan")
    op_args = plan_args(exact_ref("product", "other"))
    owner_id = run["id"]
    if historical_plan:
        owner_id = "same-conversation-plan-owner"
        engine.store.put("run", {**run, "id": owner_id, "status": "COMPLETED"})
    engine.store.put("operation", {"id": "plan-op", "run_id": owner_id, "key": "save_script_plan",
        "status": "committed", "args": op_args, "request_hash": canonical_hash(op_args),
        "receipt": {"data": {"script_plan_ref": plan_ref}}})
    async def no_broader_context(*args, **kwargs):
        pytest.fail("exact plan products must not be widened to all context products")
    monkeypatch.setattr(engine.client, "query", no_broader_context)
    generation = {"plan_ref": plan_ref, "context_bundle_ref": exact_ref("context", "bundle")}
    await engine._guard_declared_visual_actions(main, run, "submit_script_change", generation, "other-generation")
    op_args["items"][0]["product_ref"] = affected
    engine.store.update("operation", "plan-op", args=op_args, request_hash=canonical_hash(op_args))
    with pytest.raises(ContractError, match="MATERIAL_CRITICAL_VISUAL_UNREAD"):
        await engine._guard_declared_visual_actions(main, run, "submit_script_change", generation, "affected-generation")


@pytest.mark.asyncio
async def test_opaque_plan_does_not_misrepresent_context_as_the_actual_plan_scope(env, monkeypatch):
    engine, _ = env
    run, source, child, main = await authorized(engine)
    engine.submit_report(child, unread_report(source, "request_script_generation", product_id="product-A"))
    async def read_context(*args, **kwargs):
        return {"body": {"product_refs": [exact_ref("product", "product-A"), exact_ref("product", "product-B")]}}
    monkeypatch.setattr(engine.client, "query", read_context)
    with pytest.raises(ContractError, match="MATERIAL_VISUAL_PLAN_SCOPE_UNVERIFIED") as denied:
        await engine._guard_declared_visual_actions(main, run, "submit_script_change",
            {"plan_ref": exact_ref("script_plan", "external-plan"), "context_bundle_ref": exact_ref("context", "bundle")}, "opaque-plan")
    assert denied.value.details["context_is_plan_scope"] is False
    assert denied.value.details["required_action"] == "save_script_plan"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["request_script_generation", "retry_failed_items"])
async def test_retry_rechecks_a_newly_declared_blocker_without_another_generation_attempt(env, action):
    from test_retry_runtime_v1 import failed_selected_module_case
    engine, service = env
    case = await failed_selected_module_case(engine, service)
    run, main = case["run"], case["scope"]
    source = engine.materials.accept(run["conversation_id"], "参数图.png", "image/png",
        __import__("test_merchant_material_readers").image_bytes())["material_ref"]
    run = engine.store.update("run", run["id"], input={**run["input"], "material_refs": [source]})
    product_id = case["script"]["body"]["product_id"]
    branch = "retry-spec-review"
    engine.store.put("branch", {"id": branch, "run_id": run["id"], "role": "product_facts", "status": "RUNNING",
        "task": {"role": "product_facts", "instruction": "补核关键参数图", "source_refs": [source], "input_refs": [], "product_ids": [product_id]}})
    engine.submit_report(RunScope(run["id"], "retry-spec-worker", "execute", "product_facts", branch),
                         unread_report(source, action, product_id=product_id))
    with pytest.raises(ContractError, match="MATERIAL_CRITICAL_VISUAL_UNREAD"):
        await engine.business_command(main, "retry_failed_items", {"job_id": case["job"]["body"]["id"]}, "blocked-retry")
    assert case["calls"] == ["selling_points"]
    assert not [op for op in engine.store.list("operation") if op.get("key") == "retry_failed_items"]
    await engine._guard_declared_visual_actions(main, run, "submit_script_change", {}, "unrelated-failed-subset",
                                               target_product_ids={"unrelated-product"})


@pytest.mark.asyncio
async def test_declared_acceptance_blocker_stops_only_that_report_before_accept_command(env):
    engine, _ = env
    run, source, child, main = await authorized(engine)
    engine.submit_report(child, unread_report(source, "accept_analysis_reports"))
    with pytest.raises(ContractError, match="MATERIAL_CRITICAL_VISUAL_UNREAD"):
        await engine.accept_reports(main, [child.branch_task_id], "blocked-accept")
    assert engine.store.get("branch", child.branch_task_id)["status"] == "PREPARED"
    assert not [op for op in engine.store.list("operation") if op.get("key") == "accept_analysis_report"]
    await engine._guard_declared_visual_actions(main, run, "accept_analysis_report",
        {"report": {"summary": "另一个报告"}, "report_hash": canonical_hash("other")}, "other-accept")


@pytest.mark.asyncio
async def test_cross_branch_receipt_reuse_requires_basis_but_same_branch_does_not(env):
    engine, _ = env
    run, source, child, _ = await authorized(engine)
    receipt = engine.materials.record_visual_delivery(source, run["conversation_id"], locator="image:1",
        run_id=run["id"], branch_task_id="other-worker")
    with pytest.raises(ContractError, match="MATERIAL_VISUAL_REUSE_BASIS_REQUIRED"):
        engine.submit_report(child, visual_report(source, receipt["id"]))
    engine.submit_report(child, visual_report(source, receipt["id"], reuse_note="同一不可变原图及定位；复用其他分支外观判断，不新增参数。"))
    own = engine.materials.record_visual_delivery(source, run["conversation_id"], locator="image:1",
        run_id=run["id"], branch_task_id=child.branch_task_id)
    assert engine.submit_report(child, visual_report(source, own["id"]))["status"] == "PREPARED"


@pytest.mark.asyncio
async def test_empty_visual_product_list_stays_with_declared_candidate_and_conflicting_sku_is_unrelated(env):
    engine, service = env
    run, source, child, main = await authorized(engine)
    report = unread_report(source, "request_script_generation", product_id="product-A")
    report["material_assessment"]["visual_reviews"][0]["product_candidate_ids"] = []
    report["material_assessment"]["missing_info"][0]["product_candidate_id"] = None
    engine.submit_report(child, report)
    await engine._guard_declared_visual_actions(main, run, "save_script_plan",
        plan_args(exact_ref("product", "product-B")), "other-declared-product")
    branch = engine.store.get("branch", child.branch_task_id)
    engine.store.update("branch", branch["id"], task={**branch["task"], "product_ids": ["product-B"]})
    with pytest.raises(ContractError, match="MATERIAL_VISUAL_SCOPE_DENIED"):
        engine.submit_report(child, report)
    engine.store.update("branch", branch["id"], task=branch["task"])
    report = unread_report(source)
    report["material_assessment"]["product_candidates"][0].update(name="同名商品", sku_keys=["sku-A"])
    engine.submit_report(child, report)
    await engine._guard_declared_visual_actions(main, run, "save_product",
        save_args(service, "同名商品", product_id="product-B",
                  fields=Product(name="同名商品", price=129, sku_id="sku-B").model_dump()), "other-sku")


@pytest.mark.asyncio
async def test_unreported_unknowns_and_old_report_revision_do_not_create_blanket_gate(env):
    engine, service = env
    run, source, child, main = await authorized(engine)
    engine.submit_report(child, unread_report(source))
    engine.submit_report(child, {"summary": "当前仅做有依据的中性外观整理，未声明关键未读动作", "source_refs": [source]})
    saved = await engine.business_command(main, "save_product", save_args(service), "current-report-only")
    assert saved["data"]["product_ref"]
    # A committed replay returns its original receipt even if a later report
    # adds a blocker; replay does not submit another business mutation.
    engine.submit_report(child, unread_report(source))
    assert await engine.business_command(main, "save_product", save_args(service), "current-report-only") == saved
