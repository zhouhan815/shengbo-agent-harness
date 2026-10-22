"""Missing observation is discoverable without granting new read authority."""
from copy import deepcopy
from dataclasses import replace

import pytest

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_contracts.business import AnalysisReport
from shengbo_prelive.research_access import MAX_DISCOVERY_QUERIES, resolve_analysis_observation
from test_runtime_business import env, human_command, product, prepared_text
from test_selection_binding import freeze, select


async def preparation_case(env):
    engine, service = env
    room, prep, script = await prepared_text(service)
    p = (await service.query("read_script", {"ref": script}))["body"]["product_ref"]
    report = AnalysisReport(summary="精确顾客研究", product_ids=[p["id"]],
                            fields={"scenes": ["自主练习"]}, dependencies=[p]).model_dump(mode="json")
    accepted = await human_command(service, "accept_analysis_report", {"role": "audience_scenes",
        "report": report, "report_hash": canonical_hash(report), "product_refs": [p], "readset": [p],
        "preparation_id": prep["id"], "expected_revision": prep["revision"]})
    prep = accepted["data"]["preparation_ref"]
    run, scope = await freeze(engine, "生成本场话术并复用商品研究 " + p["id"], select(prep, prep=prep),
        "materials_to_script", prep=prep, room=room, targets=[p["id"]])
    return engine, service, run, scope, prep, p, accepted["data"]["analysis_ref"], report


async def catalog_case(env, *, report_product=None, current_product=None):
    engine, service = env
    p = current_product or await product(service)
    dependency = report_product or p
    report = {"summary": "已验收顾客研究", "product_ids": [dependency["id"]],
              "fields": {"scenes": ["初学者自主练习"]}, "dependencies": [dependency]}
    report = AnalysisReport.model_validate(report).model_dump(mode="json")
    accepted = await human_command(service, "accept_analysis_report", {
        "role": "audience_scenes", "report": report, "report_hash": canonical_hash(report),
        "product_refs": [dependency], "readset": [dependency]})
    run, scope = await freeze(engine, "读取并分析商品 " + p["id"], select(p), "catalog_update", targets=[p["id"]])
    return engine, service, run, scope, p, accepted["data"]["analysis_ref"]


@pytest.mark.asyncio
async def test_current_product_formal_catalog_research_is_exactly_discovered_and_read(env):
    engine, service, run, scope, p, ref = await catalog_case(env)
    assert not any(value.get("id") == ref["id"] for value in run.get("observed_refs", []))
    assert await resolve_analysis_observation(engine, scope, run, ref)
    queries = [row for row in engine.store.list("query_evidence") if row["key"] == "find_audience_research"]
    assert len(queries) == 1
    assert queries[0]["args"]["product_id"] == p["id"]
    assert queries[0]["args"]["ref"]["content_hash"] == p["content_hash"]
    value = await engine.business_query(scope, "read_context", {"ref": ref, "sections": ["report"]})
    assert value["ref"] == ref
    assert value["body"]["report"]["summary"] == "已验收顾客研究"
    assert not engine.store.list("operation")


@pytest.mark.asyncio
async def test_current_preparation_pointer_is_discovered_through_backend_query(env):
    engine, service, run, scope, prep, p, ref, report = await preparation_case(env)
    assert await resolve_analysis_observation(engine, scope, run, ref)
    queries = [row for row in engine.store.list("query_evidence") if row["key"] in {"read_preparation", "find_audience_research"}]
    assert [row["key"] for row in queries] == ["read_preparation"]
    assert (await engine.business_query(scope, "read_context", {"ref": ref}))["body"]["report"] == report


@pytest.mark.asyncio
async def test_foreign_preparation_is_still_rejected_after_legitimate_discovery(env):
    engine, service, run, scope, prep, p, ref, report = await preparation_case(env)
    room = (await human_command(service, "create_room", {"name": "其他本场"}))["data"]["room_ref"]
    other = (await human_command(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    accepted = await human_command(service, "accept_analysis_report", {
        "role": "audience_scenes", "report": report, "report_hash": canonical_hash(report),
        "product_refs": [p], "readset": [p], "preparation_id": other["id"], "expected_revision": other["revision"]})
    foreign = accepted["data"]["analysis_ref"]
    # Discovery itself excludes foreign preparation bodies. Even a pointer
    # observed through historical context cannot authorize the exact reader.
    assert not await resolve_analysis_observation(engine, scope, run, foreign)
    engine.store.update("run", run["id"], observed_refs=[foreign])
    with pytest.raises(ContractError) as denied:
        await engine.business_query(scope, "read_context", {"ref": foreign})
    assert denied.value.status == 403
    assert denied.value.code in {"ARTIFACT_SCOPE_CONFLICT", "ANALYSIS_READ_DENIED"}


@pytest.mark.asyncio
async def test_non_target_product_is_not_discovered_by_a_message_pointer(env):
    engine, service = env
    p = await product(service, "授权商品")
    other = await product(service, "其他商品")
    engine, service, run, scope, p, ref = await catalog_case(env, current_product=p, report_product=other)
    assert not await resolve_analysis_observation(engine, scope, run, ref)
    queries = [row for row in engine.store.list("query_evidence") if row["key"] == "find_audience_research"]
    assert {row["args"]["product_id"] for row in queries} == {p["id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("content_hash", "sha256:wrong"), ("revision", 999),
    ("schema_version", "OTHER"), ("id", "analysis_not_in_product_index")])
async def test_changed_identity_never_becomes_resolved(env, field, value):
    engine, service, run, scope, p, ref = await catalog_case(env)
    assert not await resolve_analysis_observation(engine, scope, run, {**ref, field: value})


@pytest.mark.asyncio
async def test_stale_research_is_not_automatically_adopted(env):
    engine, service = env
    old = await product(service)
    fields = (await service.query("read_product", {"ref": old}))["body"]
    schema = service.schema()["schema"]["product"]["properties"]
    complete = {key: value for key, value in fields.items() if key in schema}
    complete["price"] = 200
    saved = await human_command(service, "save_product", {"product_id": old["id"], "expected_revision": old["revision"],
        "fields": complete, "patches": [{"path": "/price", "op": "set", "value": 200}], "schema_hash": service.schema()["schema_hash"]})
    p = saved["data"]["product_ref"]
    engine, service, run, scope, p, ref = await catalog_case(env, current_product=p, report_product=old)
    assert not await resolve_analysis_observation(engine, scope, run, ref)
    item = (await service.query("find_audience_research", {"ref": p}))["items"][0]
    assert item["compatibility"] == "requires_review"


@pytest.mark.asyncio
async def test_specialist_and_intent_are_not_given_discovery_authority(env):
    engine, service, run, scope, p, ref = await catalog_case(env)
    for restricted in [replace(scope, phase="intent"), replace(scope, role="audience_scenes", branch_task_id="child")]:
        assert not await resolve_analysis_observation(engine, restricted, run, ref)
    assert not [row for row in engine.store.list("query_evidence") if row["key"] == "find_audience_research"]


@pytest.mark.asyncio
async def test_observation_alone_does_not_grant_product_scope(env):
    engine, service, run, scope, p, ref = await catalog_case(env)
    unscoped = deepcopy(run)
    unscoped["goal"].update(action_target_refs=[], context_product_refs=[])
    unscoped.update(action_target_refs=[], context_product_refs=[], observed_refs=[p, ref])
    assert not await resolve_analysis_observation(engine, scope, unscoped, ref)


@pytest.mark.asyncio
async def test_receipted_new_product_can_use_its_observed_exact_reference(env):
    engine, service = env
    initial = await product(service, "原目标")
    created = await product(service, "本次新建目标")
    engine, service, run, scope, p, ref = await catalog_case(env, current_product=initial, report_product=created)
    # The runtime writes these only after a real save-product receipt; a
    # message or observed reference alone cannot populate created_product_ids.
    run = engine.store.update("run", run["id"], created_product_ids=[created["id"]], observed_refs=[created])
    assert await resolve_analysis_observation(engine, scope, run, ref)
    value = await engine.business_query(scope, "read_context", {"ref": ref})
    assert value["body"]["report"]["product_ids"] == [created["id"]]


@pytest.mark.asyncio
async def test_resolution_monitor_has_timing_and_no_report_body(env, monkeypatch):
    engine, service, run, scope, p, ref = await catalog_case(env)
    events = []
    emit = engine._emit
    def observed_emit(run_id, kind, data, active_scope=None):
        if kind == "analysis.reference_resolution":
            events.append(data)
        return emit(run_id, kind, data, active_scope)
    monkeypatch.setattr(engine, "_emit", observed_emit)
    assert await resolve_analysis_observation(engine, scope, run, ref)
    assert events[-1]["query_count"] == 1
    assert events[-1]["duration_ms"] >= 0
    assert events[-1]["discovery_is_authorization"] is False
    assert "report" not in events[-1] and "body" not in events[-1]


@pytest.mark.asyncio
async def test_resolution_cache_avoids_repeated_queries_and_observation_invalidates_negative(env):
    engine, service, run, scope, p, ref = await catalog_case(env)
    missing = {**ref, "id": "analysis_unknown"}
    assert not await resolve_analysis_observation(engine, scope, run, missing)
    count = len(engine.store.list("query_evidence"))
    assert not await resolve_analysis_observation(engine, scope, run, missing)
    assert len(engine.store.list("query_evidence")) == count
    await engine.business_query(scope, "find_audience_research", {"ref": p})
    assert not await resolve_analysis_observation(engine, scope, run, missing)
    assert len(engine.store.list("query_evidence")) == count + 2
    assert await resolve_analysis_observation(engine, scope, run, ref)
    count = len(engine.store.list("query_evidence"))
    assert await resolve_analysis_observation(engine, scope, run, ref)
    assert len(engine.store.list("query_evidence")) == count


@pytest.mark.asyncio
async def test_discovery_is_bounded_and_does_not_accept_partial_dependency_match(env, monkeypatch):
    engine, service, run, scope, p, ref = await catalog_case(env)
    other = await product(service, "未授权依赖")
    real_query, calls = engine.business_query, []
    async def endless(scope_arg, key, args):
        if key != "find_audience_research":
            return await real_query(scope_arg, key, args)
        calls.append(args)
        return {"items": [{"research_ref": ref, "compatibility": "exact", "product_refs": [p, other],
                            "report": {"product_ids": [p["id"], other["id"]]}}], "next_cursor": args["cursor"] + 100}
    monkeypatch.setattr(engine, "business_query", endless)
    assert not await resolve_analysis_observation(engine, scope, run, ref)
    assert 1 <= len(calls) <= MAX_DISCOVERY_QUERIES
