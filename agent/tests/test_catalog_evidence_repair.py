"""Offline regression for the catalog report that stalled after human confirmation."""
from copy import deepcopy

import pytest

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.report_corrections import correct_report
from test_report_corrections_v3 import correction_branch, rich_report
from test_runtime_business import env


@pytest.mark.asyncio
async def test_worker_receives_enum_and_missing_evidence_paths_and_repairs_once(env):
    engine, _ = env
    run, source, scope = await correction_branch(engine)
    report = rich_report(source, role="existing_script")
    report["material_assessment"]["field_observations"][0]["claim_kind"] = "campaign"
    report["evidence"] = [{"id": f"e{i}", "locator": "text:1", "quote": "测试商品"} for i in range(1, 11)]
    report["material_assessment"]["product_candidates"][0]["evidence_ids"] = [
        row["id"] for row in report["evidence"]]

    with pytest.raises(ContractError) as rejected:
        engine.submit_report(scope, report, strict_evidence=True)
    assert rejected.value.code == "MATERIAL_ASSESSMENT_INVALID"
    details = rejected.value.details
    assert details["issue_count"] == 11
    claim_path = "/material_assessment/field_observations/0/claim_kind"
    missing_paths = [f"/evidence/{index}/source_ref" for index in range(10)]
    assert {issue["path"] for issue in details["issues"]} == {claim_path, *missing_paths}
    draft_ref = details["report_correction"]["draft_ref"]
    original = deepcopy(engine.store.get("report_retry_draft", draft_ref["id"]))
    assert original["strict_evidence"] is True

    patches = [{"path": claim_path, "value": "merchant_claim"}]
    patches.extend({"path": path, "value": source} for path in missing_paths)
    result = correct_report(engine, scope, draft_ref, patches)
    branch = engine.store.get("branch", scope.branch_task_id)
    assert result["status"] == branch["status"] == "PREPARED"
    assert all(row["source_ref"] == source for row in branch["report"]["evidence"])
    assert branch["report_hash"] == canonical_hash(branch["report"])
    assert engine.store.get("report_retry_draft", draft_ref["id"]) == original
    assert not engine.store.list("operation")
    assert any(event["type"] == "branch.report_corrected" for event in engine.trace.events(run["id"])["events"])


@pytest.mark.asyncio
async def test_typed_basic_report_without_assessment_still_checks_immutable_quote(env, monkeypatch):
    engine, _ = env
    _, source, scope = await correction_branch(engine)
    report = rich_report(source, role="existing_script")
    report.pop("material_assessment")
    report["evidence"][0]["quote"] = "资料中没有这句"
    calls = []
    record = engine.materials._record

    def checked_once(ref, conversation_id):
        calls.append(ref)
        return record(ref, conversation_id)

    monkeypatch.setattr(engine.materials, "_record", checked_once)
    monkeypatch.setattr(engine.materials, "read", lambda *_: pytest.fail("must not reread the document"))
    with pytest.raises(ContractError) as rejected:
        engine.submit_report(scope, report, strict_evidence=True)
    assert rejected.value.code == "MATERIAL_EVIDENCE_QUOTE_MISMATCH"
    assert rejected.value.details["issues"][0]["path"] == "/evidence/0/quote"
    assert calls == [source]
    draft_ref = rejected.value.details["report_correction"]["draft_ref"]
    with pytest.raises(ContractError) as still_wrong:
        correct_report(engine, scope, draft_ref, [{"path": "/evidence/0/quote", "value": "仍不是原文"}])
    assert still_wrong.value.code == "MATERIAL_EVIDENCE_QUOTE_MISMATCH"
    result = correct_report(engine, scope, draft_ref, [{"path": "/evidence/0/quote", "value": "测试商品"}])
    assert result["status"] == "PREPARED" and "material_readiness" not in result
    assert calls == [source, source, source]


@pytest.mark.asyncio
async def test_present_out_of_scope_ref_wins_over_correctable_schema_issue(env):
    engine, _ = env
    _, source, scope = await correction_branch(engine)
    report = rich_report(source)
    report["evidence"][0]["source_ref"] = {**source, "content_hash": canonical_hash("wrong content")}
    with pytest.raises(ContractError) as rejected:
        engine.submit_report(scope, report, strict_evidence=True)
    assert rejected.value.code == "MATERIAL_EVIDENCE_SCOPE_DENIED" and rejected.value.status == 403
    assert not engine.store.list("report_retry_draft")


@pytest.mark.asyncio
async def test_add_only_repair_checks_exact_grant_and_rejects_unrelated_new_paths(env):
    engine, _ = env
    run, source, scope = await correction_branch(engine)
    report = rich_report(source, role="existing_script")
    report["evidence"][0].pop("source_ref")
    foreign = engine.materials.accept(run["conversation_id"], "other.txt", "text/plain", b"ungranted")["material_ref"]
    with pytest.raises(ContractError) as rejected:
        engine.submit_report(scope, report, strict_evidence=True)
    assert rejected.value.code == "MATERIAL_EVIDENCE_MISSING"
    draft_ref = rejected.value.details["report_correction"]["draft_ref"]
    with pytest.raises(ContractError) as unauthorized:
        correct_report(engine, scope, draft_ref, [{"path": "/evidence/0/source_ref", "value": foreign}])
    assert unauthorized.value.code == "MATERIAL_EVIDENCE_SCOPE_DENIED"
    with pytest.raises(ContractError) as new_path:
        correct_report(engine, scope, draft_ref, [{"path": "/evidence/0/unexpected", "value": True}])
    assert new_path.value.code == "MATERIAL_ASSESSMENT_INVALID"
    assert engine.store.get("branch", scope.branch_task_id)["status"] == "RUNNING"


@pytest.mark.asyncio
async def test_invalid_outer_report_keeps_repairable_raw_draft(env):
    engine, _ = env
    _, source, scope = await correction_branch(engine)
    report = rich_report(source, role="existing_script")
    valid = report["evidence"][0]
    report["evidence"] = ["原文证据不是结构化对象"]
    with pytest.raises(ContractError) as rejected:
        engine.submit_report(scope, report, strict_evidence=True)
    assert rejected.value.code == "ANALYSIS_REPORT_INVALID"
    assert rejected.value.details["issues"][0]["path"] == "/evidence/0"
    draft_ref = rejected.value.details["report_correction"]["draft_ref"]
    assert correct_report(engine, scope, draft_ref, [], action="read")["report"] == report
    assert correct_report(engine, scope, draft_ref, [{"path": "/evidence/0", "value": valid}])["status"] == "PREPARED"
