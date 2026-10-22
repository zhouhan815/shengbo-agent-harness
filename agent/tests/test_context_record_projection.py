"""Large business contexts expose exact record leaves through bounded model views."""
from collections import Counter
from copy import deepcopy

import pytest

from shengbo_contracts.common import ArtifactRef, ContractError, canonical_hash, canonical_json
from shengbo_prelive.models import RunScope
from test_provider_output_convergence import (
    MODULES, assert_no_context_churn, call, convergence, final, invoke, plan_args, ref, tokens, tool_outputs,
)


def exact(reference):
    return ArtifactRef.model_validate(reference).model_dump(mode="json")


def contains(value, expected):
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(contains(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(contains(item, expected) for item in value)
    return False


@pytest.fixture
async def context_case(convergence):
    case = convergence
    prep_id = case.run["scope"]["preparation_id"]
    records = []
    for position, role in enumerate(("product_facts", "session_config", "audience_scenes")):
        report = {"summary": role + "已核实的正式分析", "fields": {
            "public_field_marker": role.upper() + "_EXACT_REPORT", "detail": "public_fact " * (150 if position == 0 else 14000)},
            "source_refs": [], "dependencies": []}
        body = {"role": role, "accepted": True, "preparation_id": prep_id, "report": report,
            "lineage": {"original_prepared_report": deepcopy(report), "prepared_report_hash": canonical_hash(report),
                "accepted_report_hash": canonical_hash(report)}, "source_ref_replacements": []}
        records.append({"ref": exact(ref("analysis", "analysis-context-" + role, body)), "body": body})
    draft_body = {"preparation_id": prep_id, "draft": {"theme": "精确保留的本场主题", "currency": "CNY",
        "price": 99, "unknowns": ["不编造未知优惠"], "tts_selection": {"voice_id": "public-test-voice"}}}
    records.append({"ref": exact(ref("draft", "draft-record-context", draft_body)), "body": draft_body})
    product_body = {"id": "product-convergence", "name": "公开测试商品", "price": 99, "currency": "CNY"}
    records.append({"ref": exact(ref("product", product_body["id"], product_body)), "body": product_body})
    body = {"preparation_id": prep_id, "records": records, "analysis_refs": [record["ref"] for record in records[:3]],
        "product_refs": [records[4]["ref"]], "draft_ref": records[3]["ref"], "source_refs": [], "recap_refs": [],
        "prior_script_refs": [], "user_decisions": [{"decision": "只按本次已固定授权执行。", "related_run_id": "local-prior"}]}
    case.business_context = {"ref": exact(ref("context", "context-record-working-set", body)), "body": body,
        "available_sections": list(body), "reader_contract": "context/draft业务正本，runtime_context走history。"}
    case.original_context = deepcopy(case.business_context)
    assert tokens(case.business_context) > 48000, "the unprojected return must exceed the complete wire budget by itself"
    original_query = case.client.query

    async def query(key, args=None, **kwargs):
        if key != "read_context":
            return await original_query(key, args, **kwargs)
        case.client.queries.append((key, deepcopy(args), deepcopy(kwargs)))
        assert kwargs["run_id"] == case.run["id"] and kwargs["scope"] == case.run["scope"]
        assert exact(args["ref"]) == case.business_context["ref"]
        assert not args.get("sections"), "this case intentionally exercises a full business context read"
        # Return the same client-owned object, so an in-place model projection
        # would corrupt this baseline and fail the evidence-preservation check.
        return case.business_context

    case.client.query = query
    return case


async def read_context(case, scope=None):
    return await case.engine.business_query(scope or case.scope, "read_context",
        {"ref": case.business_context["ref"], "sections": []})


def assert_exact_business_evidence(case):
    assert case.business_context == case.original_context
    evidence = [item for item in case.engine.store.list("query_evidence") if item["key"] == "read_context"]
    assert evidence and all(item["result"] == case.original_context for item in evidence)
    assert all(tokens(item["result"]) > 48000 for item in evidence)
    assert case.business_context["ref"]["content_hash"] == canonical_hash(case.business_context["body"])
    assert case.business_context["ref"] in case.engine.store.get("run", case.run["id"])["observed_refs"]


def assert_record_indexes(case, view):
    assert view["ref"] == case.original_context["ref"]
    assert view["available_sections"] == list(case.original_context["body"])
    assert "projection_contract" in view and view["projection_contract"]
    assert tokens(view) < 5000
    for key, value in case.original_context["body"].items():
        if key != "records":
            assert view["body"][key] == value
    indexes = view["body"]["records"]
    assert isinstance(indexes, list) and len(indexes) == len(case.original_context["body"]["records"])
    for index, record in zip(indexes, case.original_context["body"]["records"]):
        assert index["ref"] == record["ref"] and index["kind"] == record["ref"]["kind"]
        assert index["evidence_only"] is True and index["record_body_omitted"] is True
        assert isinstance(index["title"], str) and len(index["title"]) <= 400
        assert set(index["available_fields"]) == set(record["body"])
        assert index["full_ref"]["kind"] == "runtime_context"
        assert "body" not in index and "lineage" not in index
    # A model view is not the preimage of the immutable business reference.
    assert canonical_hash(view["body"]) != view["ref"]["content_hash"]
    return indexes


@pytest.mark.asyncio
async def test_large_context_index_and_one_report_leaf_converge_to_save_plan_in_four_sdk_calls(context_case):
    case = context_case

    def respond(payload):
        round_number, seen = len(case.sent), tool_outputs(payload)
        if round_number == 1:
            return [call("read_context", "read-large-context-once", {"ref": case.business_context["ref"], "sections": []})]
        if round_number == 2:
            indexes = assert_record_indexes(case, seen["read-large-context-once"])
            return [call("read_run_history", "read-exact-record-report", {
                "ref": indexes[0]["full_ref"], "path": "/record/body/report"})]
        if round_number == 3:
            report = seen["read-exact-record-report"]["document"]
            assert report == case.original_context["body"]["records"][0]["body"]["report"]
            assert seen["read-exact-record-report"]["selection"]["mode"] == "value"
            received = {module: {"sentences": [{"text": report["fields"]["public_field_marker"]}]} for module in MODULES}
            args = plan_args(case, received)
            args["bundle_ref"] = case.business_context["ref"]
            return [call("save_script_change_plan", "save-plan-after-record-leaf", args)]
        assert round_number == 4 and seen["save-plan-after-record-leaf"]["status"] == "committed"
        return final()

    case.respond = respond
    result = await invoke(case, thread="context-record-four-call-convergence")
    assert result["messages"][-1].content == "测试完成"
    assert len(case.sent) == case.credentials.calls == 4
    assert len(case.client.queries) == len(case.client.commands) == 1
    assert case.client.commands[0]["args"]["bundle_ref"] == case.original_context["ref"]
    starts = assert_no_context_churn(case)
    assert Counter(item["tool_name"] for item in starts) == {
        "read_context": 1, "read_run_history": 1, "save_script_change_plan": 1}
    assert_exact_business_evidence(case)


@pytest.mark.asyncio
async def test_record_and_context_snapshots_preserve_exact_originals_and_draft_leaf(context_case):
    case = context_case
    view = await read_context(case)
    indexes = assert_record_indexes(case, view)
    for index, original in zip(indexes, case.original_context["body"]["records"]):
        document = case.engine.run_history(case.scope, {"ref": index["full_ref"], "full": True})["document"]
        assert document["kind"] == "business_context_record"
        assert document["context_ref"] == case.original_context["ref"]
        assert document["record"] == original and document["branch"]["id"] is None
    draft = case.engine.run_history(case.scope, {"ref": indexes[3]["full_ref"], "path": "/record/body/draft"})
    assert draft["document"] == case.original_context["body"]["records"][3]["body"]["draft"]
    assert draft["selection"]["mode"] == "value"
    document = case.engine.run_history(case.scope, {"ref": view["full_ref"], "full": True})["document"]
    assert contains(document, case.original_context)
    assert_exact_business_evidence(case)
    assert not case.sent and case.credentials.calls == 0 and not case.client.commands


@pytest.mark.asyncio
async def test_record_snapshots_check_conversation_branch_and_hash_before_leaf_selection(context_case):
    case = context_case
    view = await read_context(case)
    foreign_conversation = case.engine.create_conversation({"scope": {"kind": "catalog"}})
    foreign = case.engine.store.put("run", {**case.run, "id": "foreign-context-record-reader",
        "conversation_id": foreign_conversation["id"]})
    foreign_scope = RunScope(foreign["id"], "foreign-record", "execute")
    case.engine.store.put("branch", {"id": "record-child-one", "run_id": case.run["id"], "role": "product_facts",
        "status": "RUNNING", "task": {"product_ids": [], "source_refs": [], "input_refs": []}})
    case.engine.store.put("branch", {"id": "record-child-two", "run_id": case.run["id"], "role": "product_facts",
        "status": "RUNNING", "task": {"product_ids": [], "source_refs": [], "input_refs": []}})
    child = RunScope(case.run["id"], "child-record", "execute", "product_facts", "record-child-one")
    other = RunScope(case.run["id"], "other-record", "execute", "product_facts", "record-child-two")
    references = [view["full_ref"], *[index["full_ref"] for index in view["body"]["records"]]]
    for reference in references:
        for reader, reason in ((foreign_scope, "RUN_SCOPE_DENIED"), (child, "BRANCH_SCOPE_DENIED")):
            for selector in ({"path": "/missing"}, {"full": True}):
                with pytest.raises(ContractError) as caught:
                    case.engine.run_history(reader, {"ref": reference, **selector})
                assert caught.value.code == reason and caught.value.status == 403
        with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
            case.engine.run_history(case.scope, {"ref": {**reference, "content_hash": canonical_hash("tampered")},
                "path": "/record/body/report"})
    child_view = await read_context(case, child)
    child_ref = child_view["body"]["records"][0]["full_ref"]
    report = case.engine.run_history(child, {"ref": child_ref, "path": "/record/body/report"})
    assert report["document"] == case.original_context["body"]["records"][0]["body"]["report"]
    with pytest.raises(ContractError, match="BRANCH_SCOPE_DENIED"):
        case.engine.run_history(other, {"ref": child_ref, "path": "/record/body/report"})
    evidence = case.engine.store.list("query_evidence")
    assert {item["branch_task_id"] for item in evidence} == {None, "record-child-one"}
    assert_exact_business_evidence(case)


@pytest.mark.asyncio
async def test_read_context_without_records_retains_the_exact_business_result(context_case):
    case = context_case
    original = {key: deepcopy(value) for key, value in case.business_context.items() if key != "body"}
    original["body"] = {"draft": {"theme": "明确主题", "commerce": {"price": 99}}}
    original["ref"] = exact(ref("draft", "standalone-draft-no-records", original["body"]))
    original["available_sections"] = ["draft"]
    case.business_context = original
    case.original_context = deepcopy(original)
    result = await read_context(case)
    assert result == original and case.business_context == case.original_context
    evidence = case.engine.store.list("query_evidence")
    assert len(evidence) == 1 and evidence[0]["result"] == original
    assert not case.sent and case.credentials.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("wide_fields", [False, True])
async def test_record_count_and_character_caps_keep_the_remaining_records_exactly_readable(context_case, wide_fields):
    case = context_case
    records = []
    for position in range(75):
        body = ({("field_" + str(number) + "_" + "x" * 60): number for number in range(40)}
                if wide_fields else {"draft": {"theme": "本场主题"}, "preparation_id": case.run["scope"]["preparation_id"]})
        records.append({"ref": exact(ref("draft", f"bounded-record-{position}", body)), "body": body})
    body = {"records": records, "preparation_id": case.run["scope"]["preparation_id"], "source_refs": []}
    original = {"ref": exact(ref("context", "context-bounded-records", body)), "body": body,
                "available_sections": list(body)}
    unchanged = deepcopy(original)
    view = case.engine._context_view(case.run, original, case.scope)
    indexes = view["body"]["records"]
    assert 0 < len(indexes) <= 20
    assert len(canonical_json(indexes)) <= 12000
    if wide_fields:
        assert len(indexes) < 20, "the character budget must bound wide metadata before the count cap"
    else:
        assert len(indexes) == 20
    projection = view["records_projection"]
    assert projection["total"] == 75 and projection["returned"] == len(indexes)
    assert projection["next_cursor"] == len(indexes)
    assert projection["full_ref"] == view["full_ref"]
    remaining = case.engine.run_history(case.scope, {"ref": projection["full_ref"],
        "path": projection["records_path"], "cursor": projection["next_cursor"], "limit": 1})
    assert remaining["document"] == [records[len(indexes)]]
    assert remaining["selection"]["next_cursor"] == len(indexes) + 1
    assert original == unchanged and not case.sent and not case.client.commands
