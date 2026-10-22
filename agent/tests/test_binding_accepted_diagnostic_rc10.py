"""An accepted exact product dependency is a diagnostic, never a new binding."""
from copy import deepcopy

import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.models import RunScope
from test_runtime_business import BindableFakeModel, Product, env
from test_source_alias_bridge import accepted_sources


def voice_records(service):
    with service.store.transaction() as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT kind,id,body,revision FROM records ORDER BY kind,id")]


def model_attempts(engine):
    with engine.store.tx() as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT id,run_id,body FROM attempts ORDER BY id")]


@pytest.mark.asyncio
async def test_accepted_same_exact_dependency_returns_existing_ref_without_rebinding(
    env, monkeypatch
):
    engine, service = env
    run, scope, sources, _, _ = await accepted_sources(engine, service)
    saved = await engine.business_command(scope, "save_product", {
        "schema_hash": service.schema()["schema_hash"], "patches": [],
        "fields": Product(name="已接受报告依赖诊断", price=129).model_dump(),
        "source_manifest_refs": sources,
    }, "accepted-diagnostic-create")
    product_ref = saved["data"]["product_ref"]
    branch_id = "accepted-exact-product-diagnostic"
    engine.store.put("branch", {
        "id": branch_id, "run_id": run["id"], "role": "audience_scenes",
        "status": "RUNNING",
        "task": {"role": "audience_scenes", "instruction": "仅分析这个精确商品",
                 "product_ids": [product_ref["id"]], "source_refs": sources,
                 "input_refs": [product_ref]},
    })
    engine.submit_report(RunScope(run["id"], "accepted-diagnostic-worker", "execute",
                                 "audience_scenes", branch_id), {
        "summary": "基于该精确商品分析顾客场景，不修改商品事实",
        "product_ids": [product_ref["id"]], "dependencies": [product_ref],
        "source_refs": sources,
    })
    accepted_ref, = (await engine.accept_reports(
        scope, [branch_id], "accepted-diagnostic-accept"))["accepted_refs"]
    branch_before = deepcopy(engine.store.get("branch", branch_id))
    assert branch_before["status"] == "ACCEPTED"
    assert branch_before["accepted_ref"] == accepted_ref
    product_before = await service.query("read_product", {"ref": product_ref})
    report_before = await service.query("read_analysis_report", {"ref": accepted_ref})
    records_before = voice_records(service)
    branches_before = deepcopy(engine.store.list("branch"))
    operations_before = deepcopy(engine.store.list("operation"))
    attempts_before = model_attempts(engine)
    unexpected_calls = []

    async def unexpected_accept(*args, **kwargs):
        unexpected_calls.append("accept_reports")
        raise AssertionError("A rejected source-only bind must not accept again")

    async def unexpected_model(*args, **kwargs):
        unexpected_calls.append("model")
        raise AssertionError("A binding diagnostic must not invoke a model")

    monkeypatch.setattr(engine, "accept_reports", unexpected_accept)
    monkeypatch.setattr(BindableFakeModel, "_agenerate", unexpected_model)
    with pytest.raises(ContractError) as caught:
        await engine.bind_source_analysis(scope, [branch_id], {
            "product_ref": product_ref,
            "validation_note": "核对已接受精确关联，仅请求诊断，不要求改绑。",
        }, "accepted-diagnostic-bind")

    error = caught.value
    assert error.code == "BINDING_REQUIRES_REPLAN"
    assert error.details["same_exact_product_dependency"] is True
    assert error.details["existing_accepted_ref"] == accepted_ref
    assert error.details["product_ref"] == product_ref
    assert error.details["automatic_acceptance"] is False
    assert error.details["automatic_fact_changes"] is False
    # The returned ref is only a reader hint, not proof of current validity.
    assert not {"currentness_verified", "approved", "ready"}.intersection(error.details)
    assert engine.store.get("branch", branch_id) == branch_before
    assert engine.store.list("branch") == branches_before
    assert not [branch for branch in engine.store.list("branch")
                if branch["id"].startswith("bound_")]
    assert engine.store.list("operation") == operations_before
    assert model_attempts(engine) == attempts_before
    assert voice_records(service) == records_before
    assert await service.query("read_product", {"ref": product_ref}) == product_before
    assert await service.query("read_analysis_report", {"ref": accepted_ref}) == report_before
    assert unexpected_calls == []
