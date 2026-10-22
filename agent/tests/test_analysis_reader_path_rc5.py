"""Existing read_context safely routes observed, scoped analysis references."""
from copy import deepcopy

import pytest

from test_runtime_business import env, human_command, prepared_text, product
from test_selection_binding import freeze, select
from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import AnalysisTask, RunScope


async def setup(env):
    engine, service = env
    room, prep, script = await prepared_text(service)
    p = (await service.query("read_script", {"ref": script}))["body"]["product_ref"]
    report = {"summary": "精确顾客研究", "product_ids": [p["id"]], "fields": {"scenes": ["通勤"]},
        "insights": [], "evidence": [], "hypotheses": ["仍待真实验证"], "conflicts": [],
        "missing_fields": [], "asset_suggestions": [], "source_refs": [], "dependencies": [p]}
    accepted = await human_command(service, "accept_analysis_report", {"role": "audience_scenes", "report": report,
        "report_hash": canonical_hash(report), "product_refs": [p], "readset": [p],
        "preparation_id": prep["id"], "expected_revision": prep["revision"]})
    prep = accepted["data"]["preparation_ref"]
    ref = accepted["data"]["analysis_ref"]
    run, scope = await freeze(engine, "为本场生成话术并复用商品研究 " + p["id"], select(prep, prep=prep),
        "materials_to_script", prep=prep, room=room, targets=[p["id"]])
    return engine, service, run, scope, prep, p, ref, report


def child(engine, run, p, ref, *, prior=False, delegated=True):
    task = AnalysisTask(role="audience_scenes", instruction="只回读本次明确派发的研究", product_ids=[p["id"]],
        input_refs=[ref] if delegated and not prior else [], prior_report_refs=[ref] if delegated and prior else [])
    identity = "reader-child"
    engine.store.put("branch", {"id": identity, "run_id": run["id"], "role": "audience_scenes",
        "status": "RUNNING", "task": task.model_dump(mode="json")})
    return RunScope(run["id"], "reader-child-invoke", "execute", "audience_scenes", identity)


@pytest.mark.asyncio
async def test_analysis_resolves_current_preparation_then_preserves_exact_kind_hash_and_full_body_evidence(env):
    engine, service, run, scope, prep, p, ref, report = await setup(env)
    first = await engine.business_query(scope, "read_context", {"ref": ref, "sections": ["report"]})
    assert first["body"] == {"report": report}
    await engine.business_query(scope, "read_preparation", {"preparation_id": prep["id"]})
    original = await service.query("read_analysis_report", {"ref": ref})
    result = await engine.business_query(scope, "read_context", {"ref": ref, "sections": ["report"]})
    assert result["ref"] == ref and result["ref"]["kind"] == "analysis"
    assert result["body"] == {"report": report}
    assert result["reader_query"] == {"query": "read_context", "args": {"ref": ref, "sections": ["report"]}}
    assert "full_ref" not in result
    stored = [e for e in engine.store.list("query_evidence") if e["key"] == "read_context"][-1]
    assert stored["result"] == original
    assert (await service.query("read_analysis_report", {"ref": ref})) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", [False, True])
async def test_specialist_exact_delegation_has_available_reader_without_history_permission(env, prior):
    engine, service, run, scope, prep, p, ref, report = await setup(env)
    await engine.business_query(scope, "read_preparation", {"preparation_id": prep["id"]})
    branch_scope = child(engine, run, p, ref, prior=prior)
    result = await engine.business_query(branch_scope, "read_context", {"ref": ref, "sections": ["report"]})
    assert result["body"]["report"] == report
    assert "read_context" in engine.allowed_tool_names(branch_scope)
    assert "read_run_history" not in engine.allowed_tool_names(branch_scope)
    assert "read_analysis_report" not in engine.allowed_tool_names(branch_scope)
    with pytest.raises(ContractError, match="CONTEXT_SECTION_INVALID") as bad_section:
        await engine.business_query(branch_scope, "read_context", {"ref": ref, "sections": ["fields"]})
    assert "report" in bad_section.value.details["available_sections"]
    assert bad_section.value.details["reader_query"]["query"] == "read_context"


@pytest.mark.asyncio
async def test_child_whitelist_is_not_observation_and_other_raw_refs_are_denied(env):
    engine, service, run, scope, prep, p, ref, report = await setup(env)
    branch_scope = child(engine, run, p, ref)
    with pytest.raises(ContractError, match="ANALYSIS_READ_DENIED"):
        await engine.business_query(branch_scope, "read_context", {"ref": ref})
    await engine.business_query(scope, "read_preparation", {"preparation_id": prep["id"]})
    child(engine, run, p, ref, delegated=False)
    with pytest.raises(ContractError, match="BRANCH_SCOPE_DENIED"):
        await engine.business_query(branch_scope, "read_context", {"ref": ref})
    child(engine, run, p, ref)
    for field, value in (("revision", ref["revision"] + 1), ("content_hash", "sha256:wrong"),
                         ("schema_version", "OTHER-SCHEMA"), ("id", "unknown-analysis")):
        bad = {**ref, field: value}
        with pytest.raises(ContractError, match="ANALYSIS_READ_DENIED"):
            await engine.business_query(branch_scope, "read_context", {"ref": bad})


@pytest.mark.asyncio
async def test_observed_foreign_report_still_hits_backend_preparation_fence(env):
    engine, service, run, scope, prep, p, ref, report = await setup(env)
    other_room = (await human_command(service, "create_room", {"name": "不能跨入的场次"}))["data"]["room_ref"]
    other = (await human_command(service, "create_preparation", {"room_id": other_room["id"],
        "room_revision": other_room["revision"]}))["data"]["preparation_ref"]
    foreign = await human_command(service, "accept_analysis_report", {"role": "audience_scenes", "report": report,
        "report_hash": canonical_hash(report), "product_refs": [p], "readset": [p],
        "preparation_id": other["id"], "expected_revision": other["revision"]})
    bad_ref = foreign["data"]["analysis_ref"]
    # An observed historical pointer alone is neither task nor preparation authority.
    engine.store.update("run", run["id"], observed_refs=[bad_ref])
    branch_scope = child(engine, run, p, bad_ref)
    with pytest.raises(ContractError) as denied:
        await engine.business_query(branch_scope, "read_context", {"ref": bad_ref, "sections": ["report"]})
    assert denied.value.status == 403
    assert denied.value.code in {"ARTIFACT_SCOPE_CONFLICT", "ANALYSIS_READ_DENIED"}


@pytest.mark.asyncio
async def test_same_preparation_report_cannot_expand_product_goal(env):
    engine, service, run, scope, prep, p, ref, report = await setup(env)
    outside = await product(service, "研究目标以外商品")
    outside_report = {**report, "product_ids": [outside["id"]], "dependencies": [outside]}
    value = await human_command(service, "accept_analysis_report", {"role": "audience_scenes", "report": outside_report,
        "report_hash": canonical_hash(outside_report), "product_refs": [outside], "readset": [outside],
        "preparation_id": prep["id"], "expected_revision": prep["revision"]})
    bad_ref = value["data"]["analysis_ref"]
    await engine.business_query(scope, "read_preparation", {"preparation_id": prep["id"]})
    for active_scope in (scope, child(engine, run, p, bad_ref)):
        with pytest.raises(ContractError, match="ANALYSIS_READ_DENIED") as denied:
            await engine.business_query(active_scope, "read_context", {"ref": bad_ref})
        assert denied.value.status == 403


@pytest.mark.asyncio
async def test_m3_report_path_is_exact_for_its_snapshot_shape(env):
    engine, service, run, scope, prep, p, ref, report = await setup(env)
    task = AnalysisTask(role="audience_scenes", instruction="已完成的原报告", product_ids=[p["id"]])
    engine.store.put("branch", {"id": "complete-branch", "run_id": run["id"], "role": "audience_scenes",
        "status": "ACCEPTED", "task": task.model_dump(mode="json"), "report": deepcopy(report),
        "report_hash": canonical_hash(report), "accepted_ref": ref})
    projection = await engine.project_context(scope)
    row = next(item for item in projection["branches"]["items"] if item["branch_task_id"] == "complete-branch")
    assert row["report_path"] == "/branch/report"
    restored = engine.run_history(scope, {"ref": row["report_ref"], "path": row["report_path"], "max_chars": 20000})
    assert restored["document"] == report
    with pytest.raises(ContractError, match="HISTORY_SNAPSHOT_PATH_INVALID") as wrong:
        engine.run_history(scope, {"ref": row["report_ref"], "path": "/report"})
    assert any(item["path"] == "/branch" for item in wrong.value.details["available_paths"])
