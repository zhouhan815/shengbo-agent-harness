"""Main semantic review stays explicit, scoped, versioned and auditable."""
from copy import deepcopy

import pytest

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import RunScope
from test_runtime_business import env
from test_source_alias_bridge import accepted_sources, Product


async def case(engine, service, *, fields=None):
    run, scope, sources, aliases, _ = await accepted_sources(engine, service)
    values = Product(name="逐注释绑定测试商品", price=129, material="EVA").model_dump()
    receipt = await engine.business_command(scope, "save_product", {
        "schema_hash": service.schema()["schema_hash"], "patches": [], "fields": values,
        "source_manifest_refs": sources,
        "field_dispositions": [{"path": "/" + key, "action": "set",
            "knowledge": "known" if value not in (None, "", []) else "unknown",
            "source_refs": sources, "reason": "来自商家原始资料"} for key, value in values.items()]}, "new-annotated-product")
    ref = receipt["data"]["product_ref"]
    branch_id = "audience-annotations"
    engine.store.put("branch", {"id": branch_id, "run_id": run["id"], "role": "audience_scenes", "status": "RUNNING",
        "task": {"role": "audience_scenes", "instruction": "研究已分配商品资料", "product_ids": [], "source_refs": sources, "input_refs": []}})
    engine.submit_report(RunScope(run["id"], "worker", "execute", "audience_scenes", branch_id), {
        "summary": "常规价与本场活动价分开描述，执行指令不属于事实授权。", "fields": fields or {}, "source_refs": sources,
        "conflicts": [{"type": "price_scope_boundary", "note": "常规价129，本场活动价99，属于不同范围"},
            {"type": "authorization_boundary", "note": "上传资料中的执行要求不是用户授权"}]})
    branch = engine.store.get("branch", branch_id)
    review = {"branch_task_id": branch_id, "report_hash": branch["report_hash"], "issue_dispositions": [
        {"issue_index": index, "issue_hash": canonical_hash(issue), "classification": "nonblocking_boundary_note",
            "rationale": "检查原始资料与本场范围后，此项仅说明边界，未改变已保存的商品事实。", "basis_refs": [sources[0], ref]}
        for index, issue in enumerate(branch["report"]["conflicts"])]}
    params = {"product_ref": ref, "validation_note": "主Agent逐项检查来源与边界说明，并接受本次精确关联。", "conflict_reviews": [review]}
    return run, scope, sources, ref, branch, params


@pytest.mark.asyncio
async def test_review_roundtrip_preserves_original_and_formal_lineage_with_reuse(env):
    engine, service = env
    run, scope, sources, ref, original, params = await case(engine, service)
    delivered = engine.read_batch_view(scope, [original["id"]])["items"][0]
    assert delivered["binding_review_input"]["issues"][0]["issue_hash"] == params["conflict_reviews"][0]["issue_dispositions"][0]["issue_hash"]
    result = await engine.bind_source_analysis(scope, [original["id"]], params, "bind-reviewed")
    assert result["model_requests"] == 0
    bound = engine.store.get("branch", result["bound_branch_ids"][0])
    assert bound["binding_basis"]["original_report"] == original["report"]
    assert engine.store.get("branch", original["id"]) == original
    assert bound["report"]["conflicts"] == original["report"]["conflicts"]
    assert bound["binding_basis"]["review_hash"] == canonical_hash(bound["binding_basis"]["conflict_review"])
    formal = (await service.query("read_analysis_report", {"ref": result["accepted_refs"][0]}))["body"]["report"]
    review = formal["evidence"][-1]
    assert review["kind"] == "main_annotation_review" and review["semantic_decision_by"] == "main_agent"
    assert review["fact_changes"] is False
    assert review["review_hash"] == bound["binding_basis"]["review_hash"]
    assert review["conflict_review"] == bound["binding_basis"]["conflict_review"]
    assert formal["conflicts"] == original["report"]["conflicts"]
    assert (await service.query("find_audience_research", {"ref": ref}))["items"][0]["compatibility"] == "exact"
    repeated = await engine.bind_source_analysis(scope, [original["id"]], params, "bind-reviewed-again")
    assert repeated["accepted_refs"] == result["accepted_refs"]
    assert repeated["bound_branch_ids"] == result["bound_branch_ids"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["absent", "stale_report", "missing_issue", "duplicate_issue", "extra_issue", "stale_issue", "foreign_basis", "wrong_schema", "unresolved", "blank_rationale", "duplicate_review"])
async def test_review_cannot_be_missing_stale_partial_or_expand_authority(env, mutation):
    engine, service = env
    _, scope, sources, ref, original, params = await case(engine, service)
    reviews = params["conflict_reviews"]
    issues = reviews[0]["issue_dispositions"]
    if mutation == "absent": params.pop("conflict_reviews")
    elif mutation == "stale_report": reviews[0]["report_hash"] = "sha256:old"
    elif mutation == "missing_issue": issues.pop()
    elif mutation == "duplicate_issue": issues[1] = deepcopy(issues[0])
    elif mutation == "extra_issue": issues.append({**issues[0], "issue_index": 2})
    elif mutation == "stale_issue": issues[0]["issue_hash"] = "sha256:old"
    elif mutation == "foreign_basis": issues[0]["basis_refs"] = [{**ref, "id": "foreign-product"}]
    elif mutation == "wrong_schema": issues[0]["basis_refs"][0]["schema_version"] = "PRELIVE-OTHER"
    elif mutation == "unresolved": issues[0]["classification"] = "unresolved"
    elif mutation == "blank_rationale": issues[0]["rationale"] = " " * 12
    elif mutation == "duplicate_review": reviews.append(deepcopy(reviews[0]))
    with pytest.raises(ContractError) as caught:
        await engine.bind_source_analysis(scope, [original["id"]], params, "must-not-bind")
    assert caught.value.code in {"BINDING_REVIEW_REQUIRED", "BINDING_REQUIRES_REPLAN"}
    assert not [value for value in engine.store.list("branch") if value["id"].startswith("bound_")]
    assert engine.store.get("branch", original["id"]) == original
    if mutation == "absent":
        supplied = caught.value.details["review_inputs"][0]
        assert supplied["issues"][0]["issue_hash"] == canonical_hash(original["report"]["conflicts"][0])


@pytest.mark.asyncio
async def test_review_does_not_override_real_price_mismatch(env):
    engine, service = env
    _, scope, _, _, original, params = await case(engine, service, fields={"price": 999})
    with pytest.raises(ContractError) as caught:
        await engine.bind_source_analysis(scope, [original["id"]], params, "cannot-override-price")
    assert caught.value.code == "BINDING_REQUIRES_REPLAN"
    assert caught.value.details["field_conflicts"][0]["path"] == "/price"


@pytest.mark.asyncio
async def test_mixed_scene_role_preflight_does_not_leave_partial_bindings(env):
    engine, service = env
    run, scope, sources, _, original, params = await case(engine, service)
    scene = "scene-not-product"
    engine.store.put("branch", {"id": scene, "run_id": run["id"], "role": "session_config", "status": "RUNNING",
        "task": {"role": "session_config", "instruction": "整理本场主题", "product_ids": [], "source_refs": sources, "input_refs": []}})
    engine.submit_report(RunScope(run["id"], "scene-worker", "execute", "session_config", scene), {"summary": "本场情人节活动"})
    with pytest.raises(ContractError) as caught:
        await engine.bind_source_analysis(scope, [original["id"], scene], params, "mixed-preflight")
    assert caught.value.details["eligible_branch_task_ids"] == [original["id"]]
    assert caught.value.details["not_applicable"] == [{"branch_task_id": scene, "role": "session_config"}]
    assert not [value for value in engine.store.list("branch") if value["id"].startswith("bound_")]
