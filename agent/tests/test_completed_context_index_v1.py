"""Completed history stays addressable without being replayed each model turn."""
import json

import pytest

from shengbo_contracts.common import canonical_hash
from shengbo_prelive.models import RunScope
from test_runtime_context import env, old_run


def ref(kind, identity):
    return {"kind": kind, "id": identity, "revision": 1, "content_hash": canonical_hash(identity)}


@pytest.mark.asyncio
async def test_accepted_branch_is_an_exact_index_with_money_unknowns_and_conflicts(env):
    engine, client = env
    _, run, _ = old_run(engine, status="RUNNING")
    report = {"summary": "已验收但无需每轮重读的细节。" * 500,
        "price": 107, "prior_price": 178, "campaign": "满180元减20元。", "distinct": "满300元减60元。",
        "missing_fields": ["完整成分表"], "hypotheses": ["顾客使用场景未验证"],
        "conflicts": [{"field": "expiry", "known": False}], "fields": {"unknown_sizes": ["鞋宽"]},
        "evidence": [{"ref": ref("source_manifest", "source")}], "body": "完整结果" * 2000}
    accepted = ref("analysis_report", "accepted")
    branch = engine.store.put("branch", {"id": "accepted-branch", "run_id": run["id"], "role": "product_facts",
        "status": "ACCEPTED", "report_hash": canonical_hash(report), "report": report, "accepted_ref": accepted})
    scope = RunScope(run["id"], "index-accepted", "execute")
    projected = await engine.project_context(scope)
    item = projected["branches"]["items"][0]
    assert item["history_index"] is True and item["accepted_ref"] == accepted
    assert "summary" not in item and "exact_refs" not in item
    assert item["missing_fields"] == report["missing_fields"] and item["conflicts"] == report["conflicts"]
    assert item["unresolved_fields"] == {"unknown_sizes": ["鞋宽"]}
    assert item["unverified_hypothesis_count"] == 1
    assert {107, 178} <= {row["value"] for row in item["financial_values"] if isinstance(row["value"], int)}
    for condition in ("满180元减20元。", "满300元减60元。"):
        assert any(row["value"] == condition for row in item["financial_values"])
    original = engine.run_history(scope, {"full": True, "ref": item["report_ref"]})["document"]["branch"]
    assert original == branch and original["report"] == report
    assert not client.commands


@pytest.mark.asyncio
async def test_answered_decision_is_small_but_pending_question_and_authority_stay_exact(env):
    engine, _ = env
    _, run, _ = old_run(engine, status="RUNNING")
    target = ref("product", "target")
    question = engine.store.put("question", {"id": "resolved", "run_id": run["id"], "kind": "task_scope",
        "status": "ANSWERED", "answer": "confirm", "answer_event_ref": "real-answer-receipt", "target_refs": [target],
        "context": {"input_hash": "sha256:input", "body": "冗长的已完成问题背景。" * 2000},
        "prompt": "按本次107元处理？", "options": [{"value": "confirm", "label": "继续" * 1000}],
        "approval_binding": {"review_hash": "sha256:review", "generation_epoch": 3}})
    pending = engine.store.put("question", {"id": "pending", "run_id": run["id"], "kind": "clarification",
        "status": "OPEN", "prompt": "尚未确认的实际选项", "options": [{"value": "one", "label": "第一种"}],
        "context": {"important": "未经确认的信息"}, "target_refs": [target]})
    scope = RunScope(run["id"], "resolved-index", "execute")
    projection = await engine.project_context(scope)
    decision = projection["decisions"][0]
    assert "options" not in decision and "context" not in decision and "prompt" not in decision
    assert decision["context_binding"] == {"input_hash": "sha256:input"}
    assert decision["target_refs"] == [target] and decision["answer_event_ref"] == "real-answer-receipt"
    assert decision["approval_binding"] == question["approval_binding"]
    assert engine.run_history(scope, {"full": True, "ref": decision["full_ref"]})["document"]["question"] == question
    for key in ("prompt", "options", "context", "target_refs"):
        assert projection["pending_questions"][0][key] == pending[key]
    for key in ("goal", "allowed_effects", "scope"):
        assert projection[key] == run[key]
    assert projection["original_user_request"] == run["input"]["text"]
    assert engine.store.get("run", run["id"]) == run


@pytest.mark.asyncio
async def test_old_success_receipts_are_indices_recent_and_unknown_keep_precise_anchors(env):
    engine, _ = env
    _, run, _ = old_run(engine, status="RUNNING")
    receipts = [{"operation_id": "op-" + str(index), "command": "save_product", "status": "committed",
        "data": {"product_ref": ref("product", "p-" + str(index)), "price": 107 + index,
                 "details": "完整历史回执正文。" * 1000}} for index in range(6)]
    receipts[0]["status"] = "unknown"
    engine.store.update("run", run["id"], receipts=receipts)
    scope = RunScope(run["id"], "receipts-index", "execute")
    projection = await engine.project_context(scope)
    items = projection["business_receipts"]
    assert "exact_refs" in items[0] and not items[0].get("history_index")
    assert all(items[index]["history_index"] is True and "exact_refs" not in items[index] for index in (1, 2, 3))
    assert all("exact_refs" in items[index] and not items[index].get("history_index") for index in (4, 5))
    for index, item in enumerate(items):
        assert any(row["value"] == 107 + index for row in item["financial_values"])
        assert engine.run_history(scope, {"full": True, "ref": item["full_ref"]})["document"]["receipt"] == receipts[index]
    assert projection["receipt_count"] == 6
