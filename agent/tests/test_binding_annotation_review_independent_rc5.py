"""Independent contract review: evidence sets must not duplicate association."""
from copy import deepcopy

import pytest

from shengbo_contracts.common import canonical_hash
from test_binding_annotation_review_rc5 import case
from test_runtime_business import env


@pytest.mark.asyncio
async def test_equivalent_review_evidence_order_reuses_bound_association(env):
    engine, service = env
    _, scope, _, _, original, params = await case(engine, service)
    first = await engine.bind_source_analysis(scope, [original["id"]], params, "review-evidence-order-one")
    reordered = deepcopy(params)
    for issue in reordered["conflict_reviews"][0]["issue_dispositions"]:
        issue["basis_refs"].reverse()
    second = await engine.bind_source_analysis(scope, [original["id"]], reordered, "review-evidence-order-two")
    assert second["bound_branch_ids"] == first["bound_branch_ids"], "Evidence order changes no semantic decision"
    assert second["accepted_refs"] == first["accepted_refs"]
    assert len([branch for branch in engine.store.list("branch") if branch["id"].startswith("bound_")]) == 1


@pytest.mark.asyncio
async def test_verified_aliases_and_duplicate_basis_keep_one_formal_review(env):
    engine, service = env
    _, scope, sources, _, original, params = await case(engine, service)
    first = await engine.bind_source_analysis(scope, [original["id"]], params, "original-evidence")
    aliases = await engine._verified_source_aliases(scope, sources)
    source_alias = aliases[canonical_hash(sources[0])]["new_ref"]
    equivalent = deepcopy(params)
    for issue in equivalent["conflict_reviews"][0]["issue_dispositions"]:
        issue["basis_refs"] = [issue["basis_refs"][1], source_alias, sources[0], source_alias]
    second = await engine.bind_source_analysis(scope, [original["id"]], equivalent, "alias-duplicate-evidence")
    assert second["bound_branch_ids"] == first["bound_branch_ids"]
    assert second["accepted_refs"] == first["accepted_refs"]
    bound = engine.store.get("branch", first["bound_branch_ids"][0])
    formal = (await service.query("read_analysis_report", {"ref": first["accepted_refs"][0]}))["body"]["report"]
    review = formal["evidence"][-1]
    assert review["review_hash"] == canonical_hash(review["conflict_review"])
    assert review["review_hash"] == bound["binding_basis"]["review_hash"]
    assert all(len(issue["basis_refs"]) == 2 for issue in review["conflict_review"]["issue_dispositions"])
    assert engine.store.get("branch", original["id"]) == original


@pytest.mark.asyncio
async def test_nested_issue_sources_remap_without_destroying_original_issue_proof(env):
    engine, service = env
    _, scope, sources, _, original, params = await case(engine, service)
    report = deepcopy(original["report"])
    report["conflicts"][0]["source_ref"] = sources[0]
    original = engine.store.update("branch", original["id"], report=report, report_hash=canonical_hash(report))
    review = params["conflict_reviews"][0]
    review["report_hash"] = original["report_hash"]
    review["issue_dispositions"][0]["issue_hash"] = canonical_hash(report["conflicts"][0])
    result = await engine.bind_source_analysis(scope, [original["id"]], params, "nested-issue-evidence")
    bound = engine.store.get("branch", result["bound_branch_ids"][0])
    body = (await service.query("read_analysis_report", {"ref": result["accepted_refs"][0]}))["body"]
    formal_review = body["report"]["evidence"][-1]
    assert formal_review["review_hash"] == canonical_hash(formal_review["conflict_review"])
    assert body["report"]["conflicts"][0]["source_ref"] != sources[0]
    original_issue = bound["binding_basis"]["original_report"]["conflicts"][0]
    assert original_issue == body["lineage"]["original_prepared_report"]["conflicts"][0]
    assert formal_review["conflict_review"]["issue_dispositions"][0]["issue_hash"] == canonical_hash(original_issue)
    assert engine.store.get("branch", original["id"]) == original
