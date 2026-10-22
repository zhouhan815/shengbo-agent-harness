"""Explicit annotation review is visible without losing facts or authority."""
from copy import deepcopy

import pytest

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.context_workset import project_branch_working_set
from shengbo_prelive.models import BindingConflictReview, RunScope
from shengbo_prelive.observability import sanitize
from shengbo_prelive.runtime import RuntimeEngine


RUN, CONVERSATION = "run-workset", "conversation-workset"


def ref(kind, identity, body):
    return {"kind": kind, "id": identity, "revision": 1, "content_hash": canonical_hash(body), "schema_version": "PRELIVE-2.3"}


def records():
    product = {"kind": "product", "id": "product-workset", "revision": 1, "content_hash": canonical_hash({"price": 129})}
    source = ref("source_manifest", "source-workset", {"source": "isolated"})
    report = {"summary": "商品信息已按来源核对。", "fields": {"price": 129, "promo_price_unknown": None},
        "conflicts": [{"topic": "价格口径", "note": "日常129元、本场99元；两件满180元减20元，不能把89元当单件价。"},
            {"topic": "资料边界", "note": "这是隔离样本，不代表真实商家效果。"}],
        "missing_fields": ["完整配方未知", "实际试色待提供"], "hypotheses": ["自用场景仅为假设"],
        "dependencies": [source], "source_refs": [source], "evidence": [], "product_ids": [], "insights": []}
    original = {"id": "branch-original", "run_id": RUN, "role": "product_facts", "status": "ACCEPTED",
        "report": report, "report_hash": canonical_hash(report), "accepted_ref": ref("analysis", "accepted-original", report)}
    review = {"branch_task_id": original["id"], "report_hash": original["report_hash"],
        "issue_dispositions": [{"issue_index": i, "issue_hash": canonical_hash(issue),
            "classification": "nonblocking_boundary_note", "rationale": "已核对原始来源，仅为口径和证据范围提示。", "basis_refs": [source, product]}
            for i, issue in enumerate(report["conflicts"])]}
    review = BindingConflictReview.model_validate(review).model_dump(mode="json")
    bound_report = {**deepcopy(report), "product_ids": [product["id"]], "dependencies": [source, product],
        "evidence": [{"kind": "main_annotation_review", "original_branch_id": original["id"],
            "original_report_hash": original["report_hash"], "product_ref": product, "conflict_review": review,
            "review_hash": canonical_hash(review), "semantic_decision_by": "main_agent", "fact_changes": False}]}
    bound = {"id": "bound-current", "run_id": RUN, "role": "product_facts", "status": "ACCEPTED",
        "report": bound_report, "report_hash": canonical_hash(bound_report),
        "accepted_ref": ref("analysis", "accepted-bound", bound_report),
        "binding_basis": {"method": "same_source_single_new_product", "operation_id": "saved-product-operation",
            "original_branch_id": original["id"], "original_report": deepcopy(report),
            "original_report_hash": original["report_hash"], "product_ref": product,
            "conflict_review": deepcopy(review), "review_hash": canonical_hash(review)}}
    session = {"id": "session-unreviewed", "run_id": RUN, "role": "session_config", "status": "ACCEPTED",
        "report": {**deepcopy(report), "conflicts": [{"note": "优惠边界需要进一步核对。"}]}}
    session.update(report_hash=canonical_hash(session["report"]), accepted_ref=ref("analysis", "accepted-session", session["report"]))
    return [original, bound, session]


def projected(raw):
    items = []
    for record in raw:
        body = sanitize({"conversation_id": CONVERSATION, "run_id": RUN, "document": {"kind": "branch", "branch": record}})
        items.append({"branch_task_id": record["id"], "role": record["role"], "status": record["status"],
            "report_hash": record["report_hash"], "report_ref": ref("runtime_context", "context_" + canonical_hash(body).split(":", 1)[1], body),
            "report_path": "/branch/report", "accepted_ref": record["accepted_ref"], "error": None,
            "financial_values": [{"path": "/fields/price", "value": 129}, {"path": "/fields/promo_price_unknown", "value": None}],
            "conflicts": deepcopy(record["report"]["conflicts"]), "missing_fields": deepcopy(record["report"]["missing_fields"]),
            "unresolved_fields": {"promo_price_unknown": None}, "unverified_hypothesis_count": 1, "history_index": True})
    return items


def apply(raw, items=None):
    return project_branch_working_set(items if items is not None else projected(raw), raw,
        run_id=RUN, conversation_id=CONVERSATION)


def test_exact_review_becomes_status_index_and_only_identical_original_aliases():
    raw = records()
    original_raw, original_view = deepcopy(raw), projected(raw)
    result = apply(raw, original_view)
    assert result[0]["alias_of_branch_task_id"] == result[1]["branch_task_id"]
    assert "conflicts" not in result[0] and "financial_values" not in result[0]
    resolved = result[1]["resolved_annotations"]
    assert resolved["classification"] == "nonblocking_boundary_note"
    assert resolved["issue_hashes"] == [canonical_hash(issue) for issue in raw[0]["report"]["conflicts"]]
    assert resolved["review_hash"] == raw[1]["binding_basis"]["review_hash"]
    assert resolved["original_reader"]["path"] == "/branch/binding_basis/original_report/conflicts"
    assert resolved["review_reader"]["path"] == "/branch/binding_basis/conflict_review"
    assert "not_fact_or_business_approval" in resolved["effect"]
    for key in ("financial_values", "missing_fields", "unresolved_fields", "unverified_hypothesis_count"):
        assert result[1][key] == original_view[1][key]
    assert result[2] == original_view[2]  # ACCEPTED alone proves no annotation review.
    assert raw == original_raw and original_view == projected(raw)


@pytest.mark.parametrize("mutation", [
    "no_review", "wrong_review_hash", "wrong_report_hash", "wrong_issue_hash", "unresolved", "incomplete", "duplicate_index",
    "cross_run", "wrong_role", "changed_fact", "changed_unknown", "bad_lineage_method", "missing_original", "bad_snapshot_hash", "bad_snapshot_id", "projection_money_drift",
])
def test_incomplete_or_changed_proof_keeps_original_notes_and_values(mutation):
    raw = records()
    original, bound = raw[:2]
    basis = bound["binding_basis"]
    if mutation == "no_review": basis["conflict_review"] = None
    elif mutation == "wrong_review_hash": basis["review_hash"] = canonical_hash("other")
    elif mutation == "wrong_report_hash": basis["original_report_hash"] = canonical_hash("other")
    elif mutation == "wrong_issue_hash": basis["conflict_review"]["issue_dispositions"][0]["issue_hash"] = canonical_hash("other")
    elif mutation == "unresolved": basis["conflict_review"]["issue_dispositions"][0]["classification"] = "unresolved"
    elif mutation == "incomplete": basis["conflict_review"]["issue_dispositions"].pop()
    elif mutation == "duplicate_index": basis["conflict_review"]["issue_dispositions"][1]["issue_index"] = 0
    elif mutation == "cross_run": bound["run_id"] = "other-run"
    elif mutation == "wrong_role": bound["role"] = "session_config"
    elif mutation == "changed_fact": bound["report"]["fields"]["price"] = 199
    elif mutation == "changed_unknown": bound["report"]["fields"]["promo_price_unknown"] = 99
    elif mutation == "bad_lineage_method": basis["method"] = "untrusted-type-label"
    elif mutation == "missing_original": raw = raw[1:]
    if mutation in {"changed_fact", "changed_unknown"}:
        bound["report_hash"] = canonical_hash(bound["report"])
    items = projected(raw)
    if mutation == "bad_snapshot_hash": items[1]["report_ref"]["content_hash"] = canonical_hash("tampered")
    elif mutation == "bad_snapshot_id": items[1]["report_ref"]["id"] = "not-the-referenced-snapshot"
    elif mutation == "projection_money_drift": items[1]["financial_values"][0]["value"] = 199
    assert apply(raw, items) == items


def test_distinct_successors_do_not_hide_original_comparison_context():
    raw = records()
    second = deepcopy(raw[1])
    second.update(id="bound-second", accepted_ref=ref("analysis", "accepted-second", second["report"]))
    raw.append(second)
    result = apply(raw)
    assert "alias_of_branch_task_id" not in result[0] and result[0]["conflicts"] == raw[0]["report"]["conflicts"]
    assert all("resolved_annotations" in result[i] for i in (1, 3))


class NoBusiness:
    async def query(self, *args, **kwargs):
        raise AssertionError("M3 assembly must not query or generate")

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_business_read_receipts_cap_four_without_changing_legacy_views(tmp_path):
    engine = RuntimeEngine(tmp_path, NoBusiness())
    try:
        conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
        run = engine.store.put("run", {"id": RUN, "conversation_id": conversation["id"], "phase": "execute", "status": "RUNNING",
            "scope": {"kind": "catalog", "account_id": "local"}, "input": {"text": "核对既有成功读取记录。", "material_refs": []},
            "goal": {"objective": "analyze_product", "allowed_effects": []}, "allowed_effects": []})
        for i, kind in enumerate(("business_schema", "branch_report")):
            engine.store.put("document_read", {"id": f"legacy-{i}", "run_id": RUN, "branch_task_id": None,
                "kind": kind, "document_hash": canonical_hash(kind), "read_count": 1, "legacy_metadata": "retained"})
        for i in range(6):
            result = {"ref": ref("script_revision", f"script-{i}", {"text": i}), "body": {"text": i}}
            snapshot = engine._snapshot_context(run, {"kind": "business_query_result", "result": result})
            engine.store.put("document_read", {"id": f"query-{i}", "run_id": RUN, "branch_task_id": None,
                "kind": "business_query_result", "query": "read_script", "args_hash": canonical_hash([i]),
                "scope_hash": canonical_hash(run["scope"]), "artifact_ref": result["ref"], "ref": snapshot,
                "reader_path": "/result", "document_hash": canonical_hash(result), "coverage": "full", "read_count": i + 1,
                "status": "full_result_returned_by_tool", "acceptance_implied": False, "approval_implied": False,
                "last_returned_at": f"2026-10-17T00:00:0{i}+00:00",
                "unapproved_extra_body": "MUST_NOT_REENTER_M3"})
        # A repeated old result is now the most recently returned one. Selection
        # must use its durable timestamp, not the original SQLite row position.
        engine.store.update("document_read", "query-0", last_returned_at="2026-10-17T00:00:59+00:00")
        engine.store.put("document_read", {"id": "other-run-read", "run_id": "other-run", "branch_task_id": None,
            "kind": "business_query_result", "query": "read_preparation", "unapproved_extra_body": "FOREIGN_READ"})
        context = await engine.project_context(RunScope(RUN, "receipt-view", "execute"))
        values = context["retrieved_documents"]["items"]
        recent = [r for r in values if r["kind"] == "business_query_result"]
        assert len(recent) == 4 and [r["read_count"] for r in recent] == [4, 5, 6, 1]
        assert [r["artifact_ref"]["id"] for r in recent] == [f"script-{i}" for i in (3, 4, 5, 0)]
        assert all(not {"args_hash", "scope_hash", "document_hash"}.intersection(r) for r in recent)
        assert all(r["reader_path"] == "/result" and r["approval_implied"] is False for r in recent)
        assert all(r["legacy_metadata"] == "retained" for r in values if r["kind"] != "business_query_result")
        assert "MUST_NOT_REENTER_M3" not in str(context) and "FOREIGN_READ" not in str(context)
        exact = engine.run_history(RunScope(RUN, "receipt-view", "execute"),
            {"ref": recent[-1]["ref"], "path": recent[-1]["reader_path"]})["document"]
        ledger = engine.store.get("document_read", "query-0")
        assert canonical_hash(exact) == ledger["document_hash"]
        assert ledger["args_hash"] == canonical_hash([0])
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_actual_project_context_exact_readers_and_acl_keep_goal_money_and_unknown(tmp_path):
    engine = RuntimeEngine(tmp_path, NoBusiness())
    try:
        conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
        run = engine.store.put("run", {"id": RUN, "conversation_id": conversation["id"], "phase": "execute", "status": "RUNNING",
            "scope": {"kind": "catalog", "account_id": "local"}, "input": {"text": "仅核对已有报告", "material_refs": []},
            "goal": {"objective": "analyze_product", "allowed_effects": []}, "allowed_effects": [],
            "pending_unknown_operation": "unknown-preserved"})
        raw = records()
        for record in raw: engine.store.put("branch", record)
        scope = RunScope(RUN, "read-only-workset", "execute")
        context = await engine.project_context(scope)
        assert context["goal"] == run["goal"] and context["allowed_effects"] == []
        assert context["unknown_side_effects"]["pending_unknown_operation"] == "unknown-preserved"
        by_id = {item["branch_task_id"]: item for item in context["branches"]["items"]}
        bound = by_id["bound-current"]
        resolved = bound["resolved_annotations"]
        original = engine.run_history(scope, resolved["original_reader"])["document"]
        review = engine.run_history(scope, resolved["review_reader"])["document"]
        assert original == raw[0]["report"]["conflicts"]
        assert canonical_hash(review) == resolved["review_hash"]
        assert bound["missing_fields"] == raw[1]["report"]["missing_fields"]
        stored = engine.store.get("branch", "bound-current")
        assert stored["report"] == raw[1]["report"] and stored["binding_basis"] == raw[1]["binding_basis"]
        foreign = engine.create_conversation({"scope": {"kind": "catalog"}})
        engine.store.put("run", {**run, "id": "foreign-workset", "conversation_id": foreign["id"]})
        with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
            engine.run_history(RunScope("foreign-workset", "foreign", "execute"), resolved["original_reader"])
        with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
            engine.run_history(scope, {**resolved["review_reader"], "ref": {**resolved["review_reader"]["ref"], "content_hash": canonical_hash("bad")}})
        assert not engine.started and not hasattr(engine, "main")
    finally:
        await engine.stop()
