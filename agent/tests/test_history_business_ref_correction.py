"""Observed business refs can be corrected without rewriting or broadening them."""
from collections import Counter
from copy import deepcopy

import pytest
from langchain_core.messages import ToolMessage

from shengbo_contracts.common import ArtifactRef, ContractError, canonical_hash, canonical_json
from shengbo_prelive.models import RunScope
from test_provider_output_convergence import (
    MODULES, assert_no_context_churn, call, convergence, final, invoke, plan_args, ref, tool_outputs,
)


def exact(reference):
    return ArtifactRef.model_validate(reference).model_dump(mode="json")


@pytest.fixture
async def history_case(convergence):
    case = convergence
    prep_id = case.run["scope"]["preparation_id"]
    case.draft_body = {"preparation_id": prep_id, "draft": {
        "theme": "已核实的居家主题", "commerce": {"currency": "CNY", "price": 99},
        "preserved_detail": "原有活动与声音配置必须保留。"}}
    case.draft_ref = exact(ref("draft", "observed-session-draft", case.draft_body, revision=3))
    case.bundle_ref = exact(case.client.job["body"]["context_ref"])
    prep_body = {"id": prep_id, "draft_ref": case.draft_ref, "bundle_ref": case.bundle_ref, "status": "draft"}
    case.client.preparation = {"ref": exact(ref("preparation", prep_id, prep_body, revision=7)), "body": prep_body}
    original_query = case.client.query

    async def query(key, args=None, **kwargs):
        if key != "read_context":
            return await original_query(key, args, **kwargs)
        case.client.queries.append((key, deepcopy(args), deepcopy(kwargs)))
        assert kwargs["run_id"] == case.run["id"] and kwargs["scope"] == case.run["scope"]
        assert exact(args["ref"]) == case.draft_ref
        sections = args.get("sections") or []
        if set(sections) - set(case.draft_body):
            raise ContractError("CONTEXT_SECTION_INVALID", "业务上下文章节无效", status=422,
                details={"available_sections": list(case.draft_body), "ref_kind": "draft", "reader": "read_context"})
        return {"ref": deepcopy(case.draft_ref), "body": {key: deepcopy(value) for key, value in case.draft_body.items()
                if not sections or key in sections}, "available_sections": list(case.draft_body)}

    case.client.query = query
    return case


async def observe(case, scope=None):
    return await case.engine.business_query(scope or case.scope, "read_preparation",
        {"preparation_id": case.run["scope"]["preparation_id"]})


def history_error(case, reference, scope=None):
    query_count = len(case.client.queries)
    with pytest.raises(ContractError) as caught:
        case.engine.run_history(scope or case.scope, {"ref": reference})
    assert len(case.client.queries) == query_count, "correction only returns a query hint; it executes no reader"
    return caught.value


@pytest.mark.asyncio
async def test_sdk_wrong_draft_kind_refreshes_reads_exact_business_draft_and_saves_plan(history_case):
    case = history_case
    await observe(case)
    wrong = {**case.draft_ref, "kind": "session_draft"}

    def respond(payload):
        round_number, seen = len(case.sent), tool_outputs(payload)
        if round_number == 1:
            return [call("read_run_history", "wrong-draft-kind", {"ref": wrong})]
        if round_number == 2:
            error = seen["wrong-draft-kind"]
            assert error["status"] == "error" and error["error"]["code"] == "HISTORY_REF_KIND_MISMATCH"
            details = error["error"]["details"]
            assert details["reference_was_not_corrected"] is True
            assert "reader_query" not in details and "ref" not in details
            assert case.draft_ref not in details.values(), "no corrected ref may be supplied as an alias"
            refresh = details["refresh_query"]
            assert refresh == {"query": "read_preparation", "args": {"preparation_id": case.run["scope"]["preparation_id"]}}
            return [call(refresh["query"], "refresh-exact-draft", refresh["args"])]
        if round_number == 3:
            reference = seen["refresh-exact-draft"]["body"]["draft_ref"]
            assert reference == case.draft_ref and reference["kind"] == "draft"
            return [call("read_context", "read-real-draft", {"ref": reference, "sections": ["draft"]})]
        if round_number == 4:
            body = seen["read-real-draft"]["body"]
            assert body == {"draft": case.draft_body["draft"]}
            received = {module: {"sentences": [{"text": body["draft"]["theme"]}]} for module in MODULES}
            return [call("save_script_change_plan", "commit-after-draft-correction", plan_args(case, received))]
        assert round_number == 5 and seen["commit-after-draft-correction"]["status"] == "committed"
        return final()

    case.respond = respond
    result = await invoke(case, thread="wrong-kind-business-reader-correction")
    assert result["messages"][-1].content == "测试完成"
    assert len(case.sent) == case.credentials.calls == 5
    errors = [message for message in result["messages"] if isinstance(message, ToolMessage) and message.status == "error"]
    assert len(errors) == 1 and errors[0].tool_call_id == "wrong-draft-kind"
    assert Counter(key for key, _, _ in case.client.queries) == {"read_preparation": 2, "read_context": 1}
    assert len(case.client.commands) == 1
    operation = case.engine.store.list("operation")[0]
    assert operation["status"] == "committed" and operation["request_hash"] == canonical_hash(operation["args"])
    assert case.engine.store.get("run", case.run["id"])["status"] == "RUNNING"
    assert case.client.preparation["body"]["draft_ref"] == case.draft_ref and wrong["kind"] == "session_draft"
    assert_no_context_churn(case)


@pytest.mark.asyncio
@pytest.mark.parametrize(("slot", "sections"), [("draft_ref", ["draft"]), ("bundle_ref", [])])
async def test_exact_observed_business_ref_only_offers_the_explicit_reader(history_case, slot, sections):
    case = history_case
    observed = await observe(case)
    reference = observed["body"][slot]
    baseline = deepcopy(case.engine.store.get("run", case.run["id"]))
    error = history_error(case, reference)
    assert error.code == "HISTORY_BUSINESS_REF_REQUIRES_READER" and error.status == 422
    assert error.details["reader_query"] == {"query": "read_context", "args": {"ref": reference, "sections": sections}}
    assert error.details["reference_was_not_corrected"] is True
    assert case.engine.store.get("run", case.run["id"]) == baseline
    assert not case.sent and case.credentials.calls == 0 and not case.client.commands


@pytest.mark.asyncio
async def test_current_branch_can_only_use_its_own_successfully_observed_preparation(history_case):
    case = history_case
    for identity in ("child-one", "child-two"):
        case.engine.store.put("branch", {"id": identity, "run_id": case.run["id"], "role": "product_facts",
            "status": "RUNNING", "task": {"product_ids": [], "source_refs": [], "input_refs": []}})
    first = RunScope(case.run["id"], "first-child", "execute", "product_facts", "child-one")
    second = RunScope(case.run["id"], "second-child", "execute", "product_facts", "child-two")
    await observe(case, first)
    evidence = case.engine.store.list("query_evidence")
    assert evidence[0]["branch_task_id"] == "child-one" and evidence[0]["scope"] == case.run["scope"]
    assert history_error(case, case.draft_ref, first).code == "HISTORY_BUSINESS_REF_REQUIRES_READER"
    for reader in (case.scope, second, RunScope(case.run["id"], "wrong-role", "execute", "audience_scenes", "child-one")):
        assert history_error(case, case.draft_ref, reader).code == "HISTORY_REF_INVALID"
    await observe(case, second)
    assert history_error(case, case.draft_ref, second).code == "HISTORY_BUSINESS_REF_REQUIRES_READER"
    assert len(case.client.queries) == 2 and not case.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("case_name", ["unobserved", "wrong_hash", "wrong_revision", "wrong_id", "unknown_kind",
    "context_renamed_to_draft", "other_run", "other_branch", "foreign_preparation_args", "foreign_preparation_ref",
    "wrong_preparation_kind", "foreign_evidence_scope", "foreign_body_owner", "tampered_body_hash", "nested_ref_only"])
async def test_unknown_tampered_or_unrelated_refs_remain_hard_history_ref_invalid(history_case, case_name):
    case = history_case
    reference = deepcopy(case.draft_ref)
    evidence = {"id": "isolated-proof", "run_id": case.run["id"], "branch_task_id": None,
        "scope": deepcopy(case.run["scope"]), "key": "read_preparation",
        "args": {"preparation_id": case.run["scope"]["preparation_id"]}, "result": deepcopy(case.client.preparation)}
    if case_name == "wrong_hash":
        reference["content_hash"] = canonical_hash("wrong")
    elif case_name == "wrong_revision":
        reference["revision"] += 1
    elif case_name == "wrong_id":
        reference["id"] = "unobserved-draft"
    elif case_name == "unknown_kind":
        reference["kind"] = "product"
    elif case_name == "context_renamed_to_draft":
        reference = {**case.bundle_ref, "kind": "session_draft"}
    elif case_name == "other_run":
        evidence["run_id"] = "another-run-same-conversation"
    elif case_name == "other_branch":
        evidence["branch_task_id"] = "another-branch"
    elif case_name == "foreign_preparation_args":
        evidence["args"]["preparation_id"] = "foreign-preparation"
    elif case_name == "foreign_preparation_ref":
        evidence["result"]["ref"]["id"] = "foreign-preparation"
    elif case_name == "wrong_preparation_kind":
        evidence["result"]["ref"]["kind"] = "product"
    elif case_name == "foreign_evidence_scope":
        evidence["scope"]["preparation_id"] = "foreign-preparation"
    elif case_name == "foreign_body_owner":
        evidence["result"]["body"]["id"] = "foreign-preparation"
        evidence["result"]["ref"]["content_hash"] = canonical_hash(evidence["result"]["body"])
    elif case_name == "tampered_body_hash":
        evidence["result"]["body"]["unverified"] = True
    elif case_name == "nested_ref_only":
        draft = evidence["result"]["body"].pop("draft_ref")
        evidence["result"]["body"]["nested"] = {"draft_ref": draft}
        evidence["result"]["ref"]["content_hash"] = canonical_hash(evidence["result"]["body"])
    if case_name != "unobserved":
        case.engine.store.put("query_evidence", evidence)
    # Neither a loose observed_refs entry nor a matching spelling is proof.
    case.engine.store.update("run", case.run["id"], observed_refs=[reference])
    error = history_error(case, reference)
    assert error.code == "HISTORY_REF_INVALID"
    assert "reader_query" not in error.details and "refresh_query" not in error.details
    assert not case.client.queries and not case.sent and not case.client.commands


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["HISTORY_REF_KIND_MISMATCH", "HISTORY_BUSINESS_REF_REQUIRES_READER", "CONTEXT_SECTION_INVALID"])
async def test_new_correctable_errors_share_the_original_three_correction_limit(history_case, code):
    case = history_case
    await observe(case)

    def respond(payload):
        round_number = len(case.sent)
        seen = tool_outputs(payload)
        assert round_number <= 4
        if round_number > 1:
            previous = seen[f"bad-input-{round_number - 1}"]
            assert previous["status"] == "error" and previous["error"]["code"] == code
            assert previous["correction_count"] == round_number - 1 and previous["correction_limit"] == 3
        if code == "CONTEXT_SECTION_INVALID":
            return [call("read_context", f"bad-input-{round_number}", {"ref": case.draft_ref, "sections": ["artifacts"]})]
        reference = {**case.draft_ref, "kind": "session_draft"} if code == "HISTORY_REF_KIND_MISMATCH" else case.draft_ref
        return [call("read_run_history", f"bad-input-{round_number}", {"ref": reference})]

    case.respond = respond
    with pytest.raises(ContractError) as caught:
        await invoke(case, thread="bounded-business-correction-" + code)
    assert caught.value.code == code
    assert len(case.sent) == case.credentials.calls == 4
    assert case.engine.store.get("run", case.run["id"])["correctable_tool_errors"] == {code: 3}
    assert not case.client.commands


@pytest.mark.asyncio
async def test_sdk_unobserved_business_ref_is_not_softened_into_a_correction(history_case):
    case = history_case
    case.respond = lambda payload: [call("read_run_history", "unobserved-draft", {"ref": case.draft_ref})]
    with pytest.raises(ContractError, match="HISTORY_REF_INVALID"):
        await invoke(case, thread="hard-unobserved-business-ref")
    assert len(case.sent) == case.credentials.calls == 1
    assert not case.engine.store.get("run", case.run["id"]).get("correctable_tool_errors")
    assert not case.client.queries and not case.client.commands
