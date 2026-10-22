"""Successful query receipts only: real temp HTTP, no providers or production."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from unittest.mock import patch

from fastapi import FastAPI
import httpx
import pytest

from shengbo_contracts.common import ContractError, Principal, canonical_hash
from shengbo_prelive.client import ShengboClient
from shengbo_prelive.document_reads import BUSINESS_READ_KIND, record_business_read
from shengbo_prelive.models import RunScope
from shengbo_prelive.observability import sanitize
from test_optional_workflows import authorize
from test_runtime_business import env, human_command, new_run, prepared_text
from app.prelive import create_router


@pytest.fixture
async def case(env):
    engine, service = env
    room, prep, script = await prepared_text(service)
    run = await new_run(engine, "只检查本场与现有话术，不写入或批准", room_id=room["id"], preparation_id=prep["id"])
    run = await authorize(engine, run, [])
    scope = RunScope(run["id"], "query-receipt-main", "execute")
    identity = {"principal": Principal(kind="agent", actor_id="fixture-reader", run_id=run["id"]),
        "scope": run["scope"]}
    app = FastAPI()
    app.include_router(create_router(service, lambda request: identity))
    client = ShengboClient("http://isolated-read-receipts", "fake-agent", "fake-controller",
        transport=httpx.ASGITransport(app=app))
    original = engine.client
    engine.client = client
    try:
        yield engine, service, run, scope, prep, script, identity
    finally:
        engine.client = original
        await client.close()


def reads(engine, scope):
    return [row for row in engine.store.list("document_read")
        if row["run_id"] == scope.run_id and row["branch_task_id"] == scope.branch_task_id
        and row["kind"] == BUSINESS_READ_KIND]


def restore(engine, scope, record):
    return engine.run_history(scope, {"ref": record["ref"], "path": record["reader_path"],
        "limit": 100, "max_chars": 1000000})


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["read_script", "read_preparation"])
async def test_real_http_query_unchanged_exact_snapshot_and_never_acceptance(case, query):
    engine, service, run, scope, prep, script, _ = case
    args = {"ref": script} if query == "read_script" else {"preparation_id": prep["id"]}
    expected = await service.query(query, args)
    result = await engine.business_query(scope, query, args)
    assert result == expected and "read_receipt" not in result
    record, = reads(engine, scope)
    assert record["query"] == query and record["read_count"] == 1
    assert record["ref"]["kind"] == "runtime_context"
    assert record["artifact_ref"] == expected["ref"]
    assert record["artifact_ref"]["kind"] != record["ref"]["kind"]
    assert record["document_hash"] == canonical_hash(sanitize(result))
    assert record["coverage"] == "full" and record["status"] == "full_result_returned_by_tool"
    assert record["acceptance_implied"] is record["approval_implied"] is False
    saved = engine.store.get("context_snapshot", record["ref"]["id"])
    assert canonical_hash(saved["body"]) == record["ref"]["content_hash"]
    readback = restore(engine, scope, record)
    assert readback["document"] == sanitize(expected)
    assert not readback["selection"]["paginated"]
    assert engine.store.get("run", run["id"])["allowed_effects"] == []
    assert not engine.store.list("operation") and not engine.store.list("question")


@pytest.mark.asyncio
async def test_duplicate_normalized_args_across_invocations_increment_one_snapshot(case):
    engine, _, run, scope, _, script, _ = case
    await engine.business_query(scope, "read_script", {"ref": script})
    first, = reads(engine, scope)
    await engine.business_query(RunScope(run["id"], "next-model-round", "execute"),
        "read_script", {"sections": [], "ref": script})
    second, = reads(engine, scope)
    assert second["id"] == first["id"] and second["ref"] == first["ref"]
    assert second["read_count"] == 2
    snapshots = [row for row in engine.store.list("context_snapshot")
        if row["body"]["document"].get("kind") == BUSINESS_READ_KIND]
    assert len(snapshots) == 1
    assert len([row for row in engine.store.list("query_evidence") if row["key"] == "read_script"]) == 2


@pytest.mark.asyncio
async def test_preparation_empty_default_keeps_legacy_wire_and_nonempty_view_distinct(case):
    engine, _, _, scope, prep, _, _ = case
    args = {"preparation_id": prep["id"]}
    seen = []
    original_query = engine.client.query

    async def observe(key, value=None, **kwargs):
        seen.append(deepcopy(value))
        return await original_query(key, value, **kwargs)

    with patch.object(engine.client, "query", side_effect=observe):
        full = await engine.business_query(scope, "read_preparation", args)
        explicit_empty = await engine.business_query(scope, "read_preparation", {**args, "sections": []})
        await engine.business_query(scope, "read_preparation", {**args, "sections": ["script_plan"]})
    assert full == explicit_empty
    assert seen == [args, args, {**args, "sections": ["script_plan"]}]
    complete, = [row for row in reads(engine, scope) if row["coverage"] == "full"]
    partial, = [row for row in reads(engine, scope) if row["coverage"] == "section_view"]
    assert complete["read_count"] == 2 and complete["args_hash"] == canonical_hash(args)
    assert partial["read_count"] == 1 and partial["sections"] == ["script_plan"]
    assert partial["args_hash"] == canonical_hash({**args, "sections": ["script_plan"]})
    assert not engine.store.list("operation")


@pytest.mark.asyncio
async def test_section_view_is_distinct_even_when_legacy_backend_returns_full_body(case):
    engine, _, _, scope, _, script, _ = case
    full = await engine.business_query(scope, "read_script", {"ref": script})
    original_query = engine.client.query

    async def legacy_query(key, args=None, **kwargs):
        # This case proves the receipt layer preserves a legacy backend's full
        # response. The current Voice backend now implements real projections.
        if key == "read_script" and (args or {}).get("sections"):
            return deepcopy(full)
        return await original_query(key, args, **kwargs)

    with patch.object(engine.client, "query", side_effect=legacy_query):
        section = await engine.business_query(scope, "read_script", {"ref": script, "sections": ["module_refs"]})
    assert section == full, "receipt must not silently implement or reshape backend sections"
    records = reads(engine, scope)
    assert len(records) == 2 and records[0]["document_hash"] == records[1]["document_hash"]
    partial = next(row for row in records if row["coverage"] == "section_view")
    assert partial["status"] == "section_result_returned_by_tool"
    assert partial["sections"] == ["module_refs"] and partial["sections_count"] == 1
    assert partial["args_hash"] != records[0]["args_hash"]
    assert restore(engine, scope, partial)["document"] == full


@pytest.mark.asyncio
async def test_current_module_refs_projection_has_distinct_hash_and_exact_snapshot(case):
    engine, service, run, scope, _, script, _ = case
    full = await engine.business_query(scope, "read_script", {"ref": script})
    args = {"ref": script, "sections": ["module_refs"]}
    expected = await service.query("read_script", args)
    result = await engine.business_query(scope, "read_script", args)
    assert result == expected and result != full
    assert result["ref"] == full["ref"]
    assert result["body"] == {"module_refs": full["body"]["module_refs"]}
    assert "modules" not in result and result["coverage"] == "metadata_only"
    ledger = reads(engine, scope)
    assert len(ledger) == 2
    partial = next(row for row in ledger if row["coverage"] == "section_view")
    complete = next(row for row in ledger if row["coverage"] == "full")
    assert partial["document_hash"] == canonical_hash(sanitize(result))
    assert partial["document_hash"] != complete["document_hash"]
    assert partial["args_hash"] != complete["args_hash"]
    assert partial["sections"] == ["module_refs"] and partial["sections_count"] == 1
    assert partial["acceptance_implied"] is partial["approval_implied"] is False
    assert restore(engine, scope, partial)["document"] == sanitize(expected)
    assert restore(engine, scope, complete)["document"] == sanitize(full)
    assert engine.store.get("run", run["id"])["allowed_effects"] == []
    assert not engine.store.list("operation") and not engine.store.list("question")


@pytest.mark.asyncio
async def test_new_preparation_result_revision_is_not_old_read_count(case):
    engine, service, _, scope, prep, _, _ = case
    original = await engine.business_query(scope, "read_preparation", {"preparation_id": prep["id"]})
    await human_command(service, "save_session_draft", {"preparation_id": prep["id"],
        "expected_revision": original["ref"]["revision"], "draft": {"theme": "下次独立活动", "commerce": {}}})
    current = await engine.business_query(scope, "read_preparation", {"preparation_id": prep["id"]})
    records = reads(engine, scope)
    assert len(records) == 2 and all(row["read_count"] == 1 for row in records)
    assert records[0]["args_hash"] == records[1]["args_hash"]
    assert records[0]["document_hash"] != records[1]["document_hash"]
    assert records[0]["artifact_ref"]["revision"] < records[1]["artifact_ref"]["revision"]
    assert restore(engine, scope, records[0])["document"] == original
    assert restore(engine, scope, records[1])["document"] == current


@pytest.mark.asyncio
async def test_failed_http_or_foreign_preparation_never_records_success(case):
    engine, _, run, scope, _, script, _ = case
    for query, args in [("read_script", {"ref": {**script, "content_hash": "sha256:wrong"}}),
                        ("read_preparation", {"preparation_id": "foreign-preparation"})]:
        before = len(engine.store.list("query_evidence"))
        with pytest.raises(ContractError):
            await engine.business_query(scope, query, args)
        assert reads(engine, scope) == []
        assert len(engine.store.list("query_evidence")) == before
    assert not engine.store.get("run", run["id"]).get("pending_unknown_operation")
    assert not engine.store.list("operation")


@pytest.mark.asyncio
async def test_branch_and_work_scope_have_separate_counts_and_existing_history_acl(case):
    engine, _, run, scope, prep, _, _ = case
    await engine.business_query(scope, "read_preparation", {"preparation_id": prep["id"]})
    main_record, = reads(engine, scope)
    for identity in ("reader-child-a", "reader-child-b"):
        engine.store.put("branch", {"id": identity, "run_id": run["id"], "role": "session_config",
            "status": "RUNNING", "task": {"product_ids": [], "source_refs": [], "input_refs": []}})
    child = RunScope(run["id"], "child-invoke", "execute", "session_config", "reader-child-a")
    await engine.business_query(child, "read_preparation", {"preparation_id": prep["id"]})
    child_record, = reads(engine, child)
    assert child_record["read_count"] == main_record["read_count"] == 1
    assert child_record["scope_hash"] != main_record["scope_hash"]
    assert restore(engine, child, child_record)["document"]["ref"]["id"] == prep["id"]
    sibling = RunScope(run["id"], "sibling-invoke", "execute", "session_config", "reader-child-b")
    with pytest.raises(ContractError, match="BRANCH_SCOPE_DENIED"):
        restore(engine, sibling, child_record)
    with pytest.raises(ContractError, match="BRANCH_SCOPE_DENIED"):
        restore(engine, child, main_record)
    assert "read_run_history" not in engine.allowed_tool_names(child), "receipt grants no specialist tools"

    other_scope = {"kind": "catalog", "account_id": "local", "preparation_id": None}
    # Exercise exact index identity independently, without changing a live run's
    # scope or calling a backend with an invalid WorkScope.
    changed_run = {**run, "scope": other_scope}
    result = restore(engine, scope, main_record)["document"]
    record_business_read(engine, scope, changed_run, "read_preparation", {"preparation_id": prep["id"]}, result)
    records = reads(engine, scope)
    assert len(records) == 2 and records[0]["scope_hash"] != records[1]["scope_hash"]


@pytest.mark.asyncio
async def test_other_conversation_and_tampered_snapshot_still_denied(case):
    engine, _, run, scope, prep, _, _ = case
    await engine.business_query(scope, "read_preparation", {"preparation_id": prep["id"]})
    record, = reads(engine, scope)
    other = engine.create_conversation({"scope": {"kind": "catalog"}})
    foreign = engine.store.put("run", {**run, "id": "foreign-reader", "conversation_id": other["id"]})
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        restore(engine, RunScope(foreign["id"], "foreign-invoke", "execute"), record)
    with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
        restore(engine, scope, {**record, "ref": {**record["ref"], "content_hash": "sha256:bad"}})


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["_snapshot_context", "_record_document_read"])
async def test_local_receipt_failure_is_never_an_unknown_business_write(case, failure_point):
    engine, service, run, scope, prep, _, _ = case
    before = await service.query("read_preparation", {"preparation_id": prep["id"]})
    with patch.object(engine, failure_point, side_effect=OSError("private-sqlite-path-do-not-echo")):
        with pytest.raises(ContractError) as caught:
            await engine.business_query(scope, "read_preparation", {"preparation_id": prep["id"]})
    assert caught.value.code == "DOCUMENT_READ_RECORD_UNAVAILABLE"
    assert caught.value.details == {"query": "read_preparation", "query_completed": True,
        "business_write_attempted": False, "side_effect_outcome_unknown": False}
    assert "private-sqlite" not in str(caught.value)
    assert reads(engine, scope) == [] and not engine.store.list("operation")
    assert not engine.store.get("run", run["id"]).get("pending_unknown_operation")
    assert (await service.query("read_preparation", {"preparation_id": prep["id"]})) == before
    assert len([row for row in engine.store.list("query_evidence") if row["key"] == "read_preparation"]) == 1


@pytest.mark.asyncio
async def test_parallel_counter_uses_existing_store_transaction_without_lost_reads(case):
    engine, _, run, scope, prep, _, _ = case
    result = await engine.business_query(scope, "read_preparation", {"preparation_id": prep["id"]})
    args = {"preparation_id": prep["id"]}
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: record_business_read(engine, scope, run, "read_preparation",
            args if index % 2 else {**args, "sections": []}, result), range(24)))
    record, = reads(engine, scope)
    assert record["read_count"] == 25 and record["args_hash"] == canonical_hash(args)
    assert restore(engine, scope, record)["document"] == result


@pytest.mark.asyncio
async def test_safe_snapshot_index_does_not_duplicate_text_or_unbounded_sections(case):
    engine, _, run, scope, _, script, _ = case
    original = await engine.business_query(scope, "read_script", {"ref": script})
    result = deepcopy(original)
    result["body"]["test_public_note"] = "正文标记不应在索引重复。" * 100
    result["body"]["access_token"] = "fixture-secret-never-in-snapshot"
    sections = ["very-long-section-" + str(i) + "z" * 120 for i in range(20)]
    record = record_business_read(engine, scope, run, "read_script", {"ref": script, "sections": sections}, result)
    assert "sections" not in record and record["sections_count"] == 20
    assert record["sections_hash"] == canonical_hash(sections)
    assert "正文标记" not in json.dumps(record, ensure_ascii=False)
    assert "fixture-secret-never-in-snapshot" not in json.dumps(record)
    restored = restore(engine, scope, record)["document"]
    assert restored == sanitize(result) and restored["body"]["access_token"] == "[REDACTED]"
    assert record["document_hash"] == canonical_hash(restored)
    assert record["artifact_ref"] == original["ref"], "safe view hash must never replace a formal artifact hash"


@pytest.mark.asyncio
async def test_unrecognized_or_unrelated_query_does_not_create_false_full_receipt(case):
    engine, _, run, scope, prep, script, _ = case
    assert record_business_read(engine, scope, run, "read_script", {"ref": script}, {"status": "error"}) is None
    assert record_business_read(engine, scope, run, "read_preparation", {"preparation_id": prep["id"]},
        {"ref": script, "body": {}}) is None
    assert record_business_read(engine, scope, run, "get_business_schema", {}, {"ref": script, "body": {}}) is None
    assert record_business_read(engine, scope, run, "read_script", {"ref": script}, {"body": {}}) is None
    assert record_business_read(engine, scope, run, "read_script", {"ref": script},
        {"ref": {"kind": "script_revision", "id": "bad"}, "body": {}}) is None
    assert reads(engine, scope) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("sections", [[1], "module_refs", 1, None, [True]])
async def test_malformed_section_metadata_never_crashes_or_claims_a_full_view(case, sections):
    engine, service, run, scope, _, script, _ = case
    result = await service.query("read_script", {"ref": script})
    original = deepcopy(result)
    record = record_business_read(engine, scope, run, "read_script", {"ref": script, "sections": sections}, result)
    assert record["coverage"] == "unknown_view"
    assert record["status"] == "unknown_view_result_returned_by_tool"
    assert "sections" not in record
    assert record["sections_hash"] == canonical_hash(sections)
    assert restore(engine, scope, record)["document"] == original == result
    assert record["acceptance_implied"] is record["approval_implied"] is False


@pytest.mark.asyncio
async def test_existing_schema_read_record_still_works_with_atomic_counter(case):
    engine, _, _, scope, _, _, _ = case
    schema = await engine.business_query(scope, "get_business_schema", {})
    await engine.business_query(scope, "get_business_schema", {})
    record, = [row for row in engine.store.list("document_read") if row["kind"] == "business_schema"]
    assert record["read_count"] == 2 and record["schema_hash"] == schema["schema_hash"]
    assert record["status"] == "full_result_returned_by_tool" and record["acceptance_implied"] is False
    assert "query" not in record and "approval_implied" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["read_script", "read_preparation"])
@pytest.mark.parametrize("rejection", [{"error": {"code": "QUERY_FAILED", "message": "synthetic offline error"}},
    {"status": "error"}])
async def test_http200_error_envelope_keeps_original_result_and_evidence_without_receipt(case, query, rejection):
    engine, service, run, scope, prep, script, _ = case
    args = {"ref": script} if query == "read_script" else {"preparation_id": prep["id"]}
    result = {**(await service.query(query, args)), **deepcopy(rejection)}
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=result)

    client = ShengboClient("http://offline-200-envelope", "fake-agent", "fake-controller",
        transport=httpx.MockTransport(respond))
    original = engine.client
    snapshots_before = engine.store.list("context_snapshot")
    engine.client = client
    try:
        returned = await engine.business_query(scope, query, args)
    finally:
        engine.client = original
        await client.close()
    assert returned == result and seen == [{"query": query, "args": args if query == "read_preparation"
        else {**args, "sections": []}}]
    assert reads(engine, scope) == []
    assert engine.store.list("context_snapshot") == snapshots_before
    evidence = [row for row in engine.store.list("query_evidence") if row["key"] == query]
    assert len(evidence) == 1 and evidence[0]["result"] == sanitize(result)
    assert not engine.store.list("operation")
    assert not engine.store.get("run", run["id"]).get("pending_unknown_operation")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "success", "ok", "completed"])
async def test_http200_missing_or_explicit_success_status_preserves_normal_receipt(case, status):
    engine, service, run, scope, _, script, _ = case
    args = {"ref": script}
    result = await service.query("read_script", args)
    if status is not None:
        result["status"] = status
    # Empty error is not a rejection; the guard specifically checks nonempty
    # errors and known success status, without inventing a new response schema.
    result["error"] = None
    client = ShengboClient("http://offline-200-success", "fake-agent", "fake-controller",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=result)))
    original = engine.client
    engine.client = client
    try:
        returned = await engine.business_query(scope, "read_script", args)
    finally:
        engine.client = original
        await client.close()
    assert returned == result
    record, = reads(engine, scope)
    assert record["read_count"] == 1 and record["coverage"] == "full"
    assert restore(engine, scope, record)["document"] == sanitize(result)
    assert record["acceptance_implied"] is record["approval_implied"] is False
    assert not engine.store.list("operation")
    assert not engine.store.get("run", run["id"]).get("pending_unknown_operation")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unknown", "partial", "pending", "", None, ["ok"], {"status": "ok"}, True])
async def test_unknown_or_malformed_explicit_status_never_creates_receipt(case, status):
    engine, service, run, scope, _, script, _ = case
    result = {**(await service.query("read_script", {"ref": script})), "status": status}
    before = engine.store.list("context_snapshot")
    assert record_business_read(engine, scope, run, "read_script", {"ref": script}, result) is None
    assert reads(engine, scope) == [] and engine.store.list("context_snapshot") == before
