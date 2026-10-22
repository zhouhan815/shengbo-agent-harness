"""Discover ancestor evidence locally, without refreshing jobs or inheriting rights."""
from copy import deepcopy
from unittest.mock import patch

import pytest
import tiktoken

from shengbo_contracts.common import ContractError, canonical_hash, canonical_json
from shengbo_prelive.models import RunScope
from test_related_job_queries import ancestor_job_case
from test_runtime_business import env


ENCODING = tiktoken.get_encoding("o200k_base")


def token_count(value):
    return len(ENCODING.encode(canonical_json(value)))


def contains(value, expected):
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(contains(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(contains(item, expected) for item in value)
    return False


def page(index, name):
    result = index[name]
    assert isinstance(result, dict) and isinstance(result["items"], list)
    assert {"total", "cursor", "next_cursor"} <= result.keys()
    return result


def safety_flags(index):
    assert index["state"] == "historical_not_refreshed"
    assert index["evidence_only"] is True
    assert index["authorization_inherited"] is False
    assert index["can_modify"] is False
    assert index["can_retry"] is False


def find_index(value):
    if isinstance(value, dict):
        if {"ancestors", "operations", "jobs"} <= value.keys():
            return value
        for item in value.values():
            result = find_index(item)
            if result is not None:
                return result
    elif isinstance(value, list):
        for item in value:
            result = find_index(item)
            if result is not None:
                return result
    return None


def owner_id(entry):
    return entry.get("owner_run_id") or entry.get("run_id") or entry.get("id")


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [1, 2])
async def test_follow_up_discovers_owner_job_without_previously_reading_it(env, depth):
    engine, service = env
    scope, origin, receipt = await ancestor_job_case(engine, service, depth=depth)
    current = engine.store.get("run", scope.run_id)
    assert current.get("goal") is None and not current.get("grant_id")
    assert not [item for item in engine.store.list("query_evidence")
                if item.get("run_id") == scope.run_id and item.get("key") == "read_job"]
    operations_before = deepcopy(engine.store.list("operation"))
    with patch.object(engine.client, "query", side_effect=AssertionError("discovery is local, not a job refresh")):
        index = engine._related_evidence_index(current)
        projection = await engine.project_context(scope)
    safety_flags(index)
    ancestors = page(index, "ancestors")["items"]
    assert origin["id"] in {owner_id(item) for item in ancestors}
    if depth == 2:
        middle_id = current["input"]["related_run_id"]
        middle = engine.store.get("run", middle_id)
        assert middle["phase"] == "intent" and middle.get("goal") is None
        assert middle_id in {owner_id(item) for item in ancestors}
        history = engine.run_history(scope, {"context": {"run_id": middle_id}, "sections": ["history"]})
        assert history["history"]["related_run_id"] == origin["id"]
    jobs = page(index, "jobs")["items"]
    discovered = next(item for item in jobs if item["job_id"] == receipt["data"]["job_id"])
    assert discovered["owner_run_id"] == origin["id"]
    operations = page(index, "operations")["items"]
    assert any(item.get("operation_id", item.get("id")) == receipt["operation_id"]
               and item["owner_run_id"] == origin["id"] for item in operations)
    assert contains(index, receipt["data"]["job_ref"])
    m3_index = find_index(projection)
    assert m3_index is not None and contains(m3_index, receipt["data"]["job_id"])
    safety_flags(m3_index)
    assert engine.store.list("operation") == operations_before

    # The index supplies an ID; the existing read-only owner/receipt bridge must
    # still verify the remote receipt before returning current job state.
    result = await engine.business_query(scope, "read_job", {"job_id": discovered["job_id"]})
    assert result["body"]["run_id"] == origin["id"]
    assert result["body"]["preparation_id"] == origin["scope"]["preparation_id"]
    assert result["ref"]["id"] == discovered["job_id"]
    after = engine.store.get("run", scope.run_id)
    assert not after.get("grant_id") and after.get("allowed_effects", []) == []
    with pytest.raises(ContractError, match="GOAL_EFFECT_DENIED"):
        await engine.business_command(scope, "retry_failed_items", {"job_id": discovered["job_id"]}, "no-inherited-retry")
    assert engine.store.list("operation") == operations_before


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["nonancestor", "broken_link", "foreign_conversation", "foreign_preparation", "unfrozen_owner"])
async def test_untrusted_ancestry_does_not_discover_old_job_or_receipt(env, invalid):
    engine, service = env
    scope, origin, receipt = await ancestor_job_case(engine, service, related=invalid != "nonancestor")
    current = engine.store.get("run", scope.run_id)
    if invalid == "broken_link":
        engine.store.update("run", scope.run_id,
            input={**current["input"], "related_run_id": "different-parent"})
    elif invalid == "foreign_conversation":
        foreign = engine.create_conversation({"scope": {"kind": "catalog"}})
        engine.store.update("run", origin["id"], conversation_id=foreign["id"])
    elif invalid == "foreign_preparation":
        engine.store.update("run", origin["id"], scope={**origin["scope"], "preparation_id": "foreign-preparation"})
    elif invalid == "unfrozen_owner":
        engine.store.update("run", origin["id"], goal=None, phase="execute")
    index = engine._related_evidence_index(engine.store.get("run", scope.run_id))
    safety_flags(index)
    assert receipt["data"]["job_id"] not in {item["job_id"] for item in page(index, "jobs")["items"]}
    assert origin["id"] not in {item["owner_run_id"] for item in page(index, "operations")["items"]}
    assert not contains(index, receipt["operation_id"])


@pytest.mark.asyncio
async def test_related_index_pages_are_bounded_and_m3_only_exposes_small_unrefreshed_index(env):
    engine, service = env
    scope, origin, receipt = await ancestor_job_case(engine, service, depth=6)
    original = engine.store.get("operation", receipt["operation_id"])
    # Persisted, structurally valid local receipts stress paging. They are not
    # remote proof: none of these synthetic job IDs is read or retried.
    for number in range(12):
        identity = "historical-index-operation-" + str(number)
        job_id = "historical-index-job-" + str(number)
        historical = deepcopy(original)
        historical.update(id=identity, status="committed")
        historical["receipt"].update(operation_id=identity)
        historical["receipt"]["data"].update(job_id=job_id,
            job_ref={"kind": "prelive_job", "id": job_id, "revision": 1, "content_hash": canonical_hash(job_id)},
            unindexed_original_body="large durable receipt evidence " * 1000)
        engine.store.put("operation", historical)
    current = engine.store.get("run", scope.run_id)
    with patch.object(engine.client, "query", side_effect=AssertionError("paging must not refresh or execute jobs")):
        first = engine._related_evidence_index(current, cursor=0, limit=2)
        second = engine._related_evidence_index(current, cursor=2, limit=2)
        projection = await engine.project_context(scope)
    for index in (first, second):
        safety_flags(index)
        assert token_count(index) < 5000
        for name in ("ancestors", "operations", "jobs"):
            assert len(page(index, name)["items"]) <= 2
    for name in ("ancestors", "operations", "jobs"):
        assert page(first, name)["total"] > 3
        assert page(first, name)["next_cursor"] == 2
        assert not {canonical_hash(item) for item in page(first, name)["items"]} & {
            canonical_hash(item) for item in page(second, name)["items"]}
    m3 = find_index(projection)
    assert m3 is not None
    safety_flags(m3)
    assert engine._estimate_payload({"input": [], "index": m3}) <= 768
    assert m3.get("full_ref"), "the small M3 index must offer exact access to the larger index"
    complete_index = engine.run_history(scope, {"full": True, "ref": m3["full_ref"]})["document"]["index"]
    safety_flags(complete_index)
    for name in ("ancestors", "operations", "jobs"):
        assert len(page(m3, name)["items"]) <= 3
        assert len(complete_index[name]) == page(first, name)["total"]
        if len(page(m3, name)["items"]) < page(m3, name)["total"]:
            assert page(m3, name)["next_cursor"] == len(page(m3, name)["items"])
    assert "large durable receipt evidence" not in canonical_json(m3)


@pytest.mark.asyncio
async def test_history_receipt_entry_preserves_nested_status_and_exact_readback_acl(env):
    engine, service = env
    scope, origin, receipt = await ancestor_job_case(engine, service, depth=2)
    operation = engine.store.get("operation", receipt["operation_id"])
    history = engine.run_history(scope, {"context": {"run_id": origin["id"]},
        "sections": ["history", "operations", "receipts"]})
    entries = history["receipts"]["items"]
    entry = next(item for item in entries if item["operation_id"] == receipt["operation_id"])
    assert entry["status"] == receipt["status"] == "accepted"
    assert entry["owner_run_id"] == origin["id"]
    assert entry["job_id"] == receipt["data"]["job_id"]
    assert receipt["data"]["job_ref"] in entry["result_refs"]
    document = engine.run_history(scope, {"full": True, "ref": entry["full_ref"]})["document"]
    assert contains(document, receipt), "readback must recover the exact original receipt, not its index"
    assert contains(document, operation["receipt"])

    foreign_conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    current = engine.store.get("run", scope.run_id)
    foreign_run = engine.store.put("run", {**current, "id": "foreign-discovery-reader",
        "conversation_id": foreign_conversation["id"]})
    foreign_scope = RunScope(foreign_run["id"], "foreign-discovery", "intent")
    child_scope = RunScope(scope.run_id, "child-discovery", "execute", "product_facts", "child-only")
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        engine.run_history(foreign_scope, {"full": True, "ref": entry["full_ref"]})
    with pytest.raises(ContractError, match="BRANCH_SCOPE_DENIED"):
        engine.run_history(child_scope, {"full": True, "ref": entry["full_ref"]})


@pytest.mark.asyncio
async def test_receipt_index_keeps_artifact_schema_version_and_exact_original_metadata(env):
    engine, _ = env
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    owner = engine.store.put("run", {"id": "artifact-metadata-owner", "conversation_id": conversation["id"],
        "scope": conversation["scope"], "phase": "intent", "status": "RUNNING",
        "input": {"text": "读取历史引用元数据", "material_refs": []}, "goal": None, "allowed_effects": []})
    draft_ref = {"kind": "draft", "id": "draft-original", "revision": 3,
        "content_hash": canonical_hash("exact-draft"), "schema_version": "PRELIVE-2.3"}
    context_ref = {"kind": "context", "id": "context-original", "revision": 5,
        "content_hash": canonical_hash("exact-context"), "schema_version": "PRELIVE-2.3"}
    receipt = {"schema_version": "PRELIVE-2.3", "command": "save_session_draft",
        "operation_id": "artifact-metadata-operation", "status": "committed",
        "data": {"draft_ref": draft_ref, "context_bundle_ref": context_ref}}
    wrapper = {"command": receipt["command"], "operation_id": receipt["operation_id"], "receipt": receipt}
    original = deepcopy(wrapper)
    with patch.object(engine.client, "query", side_effect=AssertionError("metadata indexing must stay local")):
        index = engine._receipt_index(owner, wrapper)
        assert index["result_refs"] == [draft_ref, context_ref]
        assert all(item["schema_version"] == "PRELIVE-2.3" for item in index["result_refs"])
        scope = RunScope(owner["id"], "read-artifact-metadata", "intent")
        recovered = engine.run_history(scope, {"full": True, "ref": index["full_ref"]})
    assert recovered["document"]["receipt"] == original
    assert recovered["document"]["receipt"]["receipt"]["data"]["draft_ref"] == draft_ref
    assert recovered["document"]["receipt"]["receipt"]["data"]["context_bundle_ref"] == context_ref
    assert wrapper == original, "validation must not normalize or discard the original ref metadata"
