"""Model receipt indexes are lean; exact durable observations stay intact."""
from copy import deepcopy

import pytest

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import RunScope
from test_business_document_reads_rc9 import case, env, reads


@pytest.mark.asyncio
async def test_debug_hashes_remain_in_ledger_and_exact_snapshot_not_model_index(case):
    engine, _, run, scope, prep, script, _ = case
    for query, args in (
        ("read_script", {"ref": script}),
        ("read_preparation", {"preparation_id": prep["id"]}),
        ("read_script", {"ref": script, "sections": ["module_refs"]}),
    ):
        await engine.business_query(scope, query, args)
    ledger = deepcopy(reads(engine, scope))
    snapshots = {row["ref"]["id"]: deepcopy(engine.store.get("context_snapshot", row["ref"]["id"]))
        for row in ledger}
    context = await engine.project_context(scope)
    items = context["retrieved_documents"]["items"]
    assert len(items) == len(ledger) == 3
    removed = {"args_hash", "scope_hash", "document_hash"}
    for row, item in zip(ledger, items):
        assert not removed.intersection(item)
        assert all(key in row for key in removed)
        for key in ("kind", "query", "artifact_ref", "ref", "reader_path", "coverage",
                    "read_count", "status", "acceptance_implied", "approval_implied", "return_summary"):
            assert item[key] == row[key]
        assert item["approval_implied"] is item["acceptance_implied"] is False
        for key in ("sections", "sections_hash", "sections_count"):
            assert item.get(key) == row.get(key)
        snapshot = snapshots[row["ref"]["id"]]["body"]
        assert canonical_hash(snapshot) == item["ref"]["content_hash"]
        document = snapshot["document"]
        assert canonical_hash(document["normalized_args"]) == row["args_hash"]
        assert canonical_hash(document["reader_scope"]) == row["scope_hash"]
        exact = engine.run_history(scope, {"ref": item["ref"], "path": item["reader_path"],
            "limit": 100, "max_chars": 1000000})["document"]
        assert exact == document["result"]
        assert canonical_hash(exact) == row["document_hash"]
    assert ledger == reads(engine, scope)
    assert snapshots == {identity: engine.store.get("context_snapshot", identity) for identity in snapshots}
    assert engine.store.get("run", run["id"])["allowed_effects"] == []
    assert not engine.store.list("operation") and not engine.store.list("question")

    other = engine.create_conversation({"scope": {"kind": "catalog"}})
    foreign = engine.store.put("run", {**run, "id": "foreign-lean-receipt-reader", "conversation_id": other["id"]})
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        engine.run_history(RunScope(foreign["id"], "foreign", "execute"),
            {"ref": items[0]["ref"], "path": items[0]["reader_path"]})
    with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
        engine.run_history(scope, {"ref": {**items[0]["ref"], "content_hash": "sha256:bad"},
            "path": items[0]["reader_path"]})


@pytest.mark.asyncio
async def test_business_receipt_usage_note_is_only_injected_after_matching_read(case):
    engine, _, run, scope, prep, _, _ = case
    original_note = "这些精确全文或schema已由工具成功返回，摘要后仍保留此记录；不代表模型理解、正式验收或新增授权。具体字段仍可按精确ref回读，避免无变化时反复整包读取。"
    initial = await engine.project_context(scope)
    assert initial["retrieved_documents"]["note"] == original_note
    engine.store.put("document_read", {"id": "foreign-note-receipt", "run_id": "not-current-run",
        "branch_task_id": None, "kind": "business_query_result"})
    assert (await engine.project_context(scope))["retrieved_documents"] == initial["retrieved_documents"]
    await engine.business_query(scope, "read_preparation", {"preparation_id": prep["id"]})
    current = await engine.project_context(scope)
    assert len(current["retrieved_documents"]["items"]) == 1
    note = current["retrieved_documents"]["note"]
    assert "business_query_result" in note and "coverage" in note and "read_count" in note
    assert "完整、节选或未知" in note and "批准" in note and "新增授权" in note
    assert engine.store.get("run", run["id"])["allowed_effects"] == []
