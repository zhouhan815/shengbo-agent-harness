"""Scoped reader over real Runtime snapshots; no network, model or writes."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from shengbo_contracts.common import ContractError, WorkScope, canonical_hash, canonical_json
from shengbo_prelive.generation_output_reads import GenerationOutputReadInput, read_generation_output
from shengbo_prelive.models import RunScope
from shengbo_prelive.runtime import RuntimeEngine
from test_recovery_workset_rc11 import make_job, public, reference

PRIVATE = "unselected-context-must-never-be-forwarded"


class ReadOnlyJobClient:
    def __init__(self, job):
        self.job = job; self.queries = []; self.commands = []

    async def query(self, key, args=None, **kwargs):
        self.queries.append((key, deepcopy(args)))
        assert key == "read_job", "no additional business query or remote call"
        assert args["job_id"] == self.job["ref"]["id"]
        return deepcopy(self.job)

    async def command(self, *args, **kwargs):
        self.commands.append(args)
        raise AssertionError("the generation reader cannot mutate business state")

    async def close(self):
        pass


def refresh(case):
    body = case.client.job["body"]
    case.client.job["ref"] = {**case.client.job["ref"], "content_hash": canonical_hash(body)}


@pytest.fixture
async def reader_case(tmp_path):
    job = make_job(keys=("opening_long", "product_intro"), failed="cta")
    job["body"].update(run_id="run-reader", preparation_id="prep-reader", context={"secret": PRIVATE})
    job["ref"]["content_hash"] = canonical_hash(job["body"])
    client = ReadOnlyJobClient(job)
    engine = RuntimeEngine(tmp_path, client)
    conv = engine.create_conversation({"scope": {"kind": "catalog"}})
    work_scope = WorkScope(kind="preparation", preparation_id="prep-reader").model_dump()
    goal = {"objective": "prepare_session", "scope": work_scope, "allowed_effects": []}
    run = engine.store.put("run", {"id": "run-reader", "conversation_id": conv["id"], "scope": work_scope,
        "status": "RUNNING", "phase": "execute", "input": {"text": "只读核对本次任务", "material_refs": []},
        "goal": goal, "frozen_goal_hash": canonical_hash(goal), "allowed_effects": [], "allowed_tools": []})
    case = SimpleNamespace(engine=engine, client=client, run=run, scope=RunScope(run["id"], "reader-call", "execute"))
    try:
        yield case
    finally:
        await engine.stop()


def args(case, *, module="opening_long", view="accepted_module", attempt_id=None):
    result = {"job_id": case.client.job["ref"]["id"], "item_id": "item_recovery", "module_key": module, "view": view}
    if attempt_id is not None:
        result["attempt_id"] = attempt_id
    return result


def item(case):
    return next(i for i in case.client.job["body"]["items"] if i["item_id"] == "item_recovery")


def assert_read_only(case, result):
    assert not case.client.commands
    assert len(case.client.queries) == 1
    assert result["automatic_action_taken"] is False
    assert result["approval_implied"] is False
    assert result["can_execute_or_approve"] is False
    assert result["currentness_verified"] is False
    assert PRIVATE not in canonical_json(result)
    assert not case.engine.store.list("attempt")


@pytest.mark.asyncio
async def test_accepted_sentence_view_is_exact_with_separate_committed_ref(reader_case):
    case = reader_case; before = deepcopy(case.client.job)
    result = await read_generation_output(case.engine, case.scope, args(case))
    assert result["status"] == "AVAILABLE" and result["content_complete"] is True
    assert result["content_kind"] == "archived_accepted_sentence_view" and result["is_formal_module_body"] is False
    assert result["content"] == item(case)["provider_attempts"][0]["output"]["sentences"]["sentence_details"]
    assert result["committed_module_ref"] == item(case)["module_results"]["opening_long"]["module_ref"]
    assert result["content_hash"] == canonical_hash(result["content"])
    assert result["reader"]["path"] == "/job/body/items/0/provider_attempts/0/output/sentences/sentence_details"
    assert "private diagnostic" not in canonical_json(result)
    assert case.client.job == before
    assert_read_only(case, result)


@pytest.mark.asyncio
async def test_latest_failed_public_raw_is_exact_without_parsing_or_promotion(reader_case):
    case = reader_case
    first = item(case)["provider_attempts"][-1]
    latest = deepcopy(first); latest["attempt_id"] = "latest-rejected-cta"
    latest["public_response"]["content"] = "{not valid JSON; 原文不能自动修复 满180减20}"
    item(case)["provider_attempts"].append(latest); refresh(case)
    result = await read_generation_output(case.engine, case.scope, args(case, module="cta", view="latest_failed_response"))
    assert result["status"] == "AVAILABLE" and result["attempt_id"] == latest["attempt_id"]
    assert result["content"] == latest["public_response"]["content"]
    assert result["content_kind"] == "archived_public_rejected_response"
    assert result["committed_module_ref"] is None and result["validation_state"] == "rejected"
    assert_read_only(case, result)


@pytest.mark.asyncio
async def test_explicit_rejected_attempt_does_not_silently_select_latest(reader_case):
    case = reader_case; first = item(case)["provider_attempts"][-1]
    other = deepcopy(first); other.update(attempt_id="later-cta")
    other["public_response"]["content"] = "第二次的独立公开响应"
    item(case)["provider_attempts"].append(other); refresh(case)
    result = await read_generation_output(case.engine, case.scope, args(case, module="cta", view="latest_failed_response", attempt_id=first["attempt_id"]))
    assert result["attempt_id"] == first["attempt_id"]
    assert result["content"] == first["public_response"]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state,expected", [("outcome_unknown", "OUTCOME_UNKNOWN"), ("timeout_unknown", "OUTCOME_UNKNOWN"), ("sent", "NOT_READY"), ("reserved", "NOT_READY")])
async def test_unknown_or_inflight_never_falls_back_to_old_known_content(reader_case, state, expected):
    case = reader_case; last = deepcopy(item(case)["provider_attempts"][-1])
    last.update(state=state, attempt_id="newer-cta", validation_state="unknown" if "unknown" in state else "pending")
    item(case)["provider_attempts"].append(last); refresh(case)
    result = await read_generation_output(case.engine, case.scope, args(case, module="cta", view="latest_failed_response", attempt_id="attempt_cta"))
    assert result["status"] == expected and result["content"] is None
    assert_read_only(case, result)


@pytest.mark.asyncio
async def test_cache_only_formal_ref_without_same_job_output_stays_metadata(reader_case):
    case = reader_case
    item(case)["provider_attempts"] = [a for a in item(case)["provider_attempts"] if a["purpose"] != "copywriting.opening_long"]
    refresh(case)
    result = await read_generation_output(case.engine, case.scope, args(case))
    assert result["status"] == "UNAVAILABLE" and result["content"] is None
    assert result["observed_committed_module_ref"] == item(case)["module_results"]["opening_long"]["module_ref"]
    assert result["committed_module_ref"] is None and "reader" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["no_public", "bad_public", "http402", "wrong_receipt"])
async def test_unproven_original_response_is_not_guessed_or_legacy_fallback(reader_case, mutation):
    case = reader_case; attempt = item(case)["provider_attempts"][-1]
    if mutation == "no_public": attempt.pop("public_response")
    elif mutation == "bad_public": attempt["public_response"]["private_extra"] = PRIVATE
    elif mutation == "wrong_receipt": attempt["provider_response_ref"] = reference("provider_response", "wrong")
    else:
        attempt.update(state="known_failed", validation_state=None, usage_state="unknown", error={"code":"MODULE_PROVIDER_REJECTED","details":{"provider_http_status":402}})
        attempt.pop("public_response"); attempt.pop("provider_response_ref")
    attempt["output"] = {"unproven_raw": PRIVATE}; refresh(case)
    result = await read_generation_output(case.engine, case.scope, args(case, module="cta", view="latest_failed_response"))
    assert result["content"] is None and result["status"] == "UNAVAILABLE"
    if mutation == "http402":
        assert result["observed_error_code"] == "MODULE_PROVIDER_REJECTED"
        assert result["observed_latest_attempt_state"] == "known_failed"
    assert_read_only(case, result)


@pytest.mark.asyncio
async def test_target_after_more_than_default_workset_limit_is_still_exact(reader_case):
    case = reader_case
    prefixes = []
    for number in range(60):
        prefix = deepcopy(item(case)); prefix["item_id"] = "other-" + str(number)
        prefix["module_results"] = {}; prefix["provider_attempts"] = []; prefix["status"] = "succeeded"
        prefixes.append(prefix)
    case.client.job["body"]["items"] = [*prefixes, item(case)]; refresh(case)
    result = await read_generation_output(case.engine, case.scope, args(case, module="cta", view="latest_failed_response"))
    assert result["status"] == "AVAILABLE" and "/items/60/" in result["reader"]["path"]
    assert result["item_id"] == "item_recovery"
    assert_read_only(case, result)


@pytest.mark.asyncio
async def test_oversized_selected_content_is_honestly_bounded_with_original_reader(reader_case):
    case = reader_case; content = "公开原文优惠条件，不能丢失。" * 2000
    item(case)["provider_attempts"][-1]["public_response"]["content"] = content; refresh(case)
    result = await read_generation_output(case.engine, case.scope, args(case, module="cta", view="latest_failed_response"))
    assert result["status"] == "AVAILABLE" and result["content_complete"] is False
    assert result["selection"]["next_cursor"] is not None and result["content_hash"] == canonical_hash(content)
    original = case.engine.run_history(case.scope, {k:v for k,v in result["reader"].items() if k != "tool"} | {"max_chars":1000000})
    assert original["document"] == content
    assert len(canonical_json(result["content"])) <= 12000


@pytest.mark.asyncio
async def test_explicit_accepted_attempt_does_not_bypass_ambiguous_accepted_history(reader_case):
    case = reader_case; original = item(case)["provider_attempts"][0]
    other = deepcopy(original); other["attempt_id"] = "ambiguous-accepted"
    other["output"]["sentences"]["sentence_details"][0]["spoken_text"] = "另一份不相同但错误关联同ref的原文"
    other["output_hash"] = canonical_hash(other["output"])
    item(case)["provider_attempts"].append(other); refresh(case)
    result = await read_generation_output(case.engine, case.scope, args(case, attempt_id=original["attempt_id"]))
    assert result["content"] is None and result["reason_code"] == "ambiguous_accepted_outputs"


@pytest.mark.asyncio
async def test_pending_business_unknown_is_not_cleared_and_read_does_not_authorize_action(reader_case):
    case = reader_case
    case.engine.store.update("run", case.run["id"], pending_unknown_operation="pending-original-operation")
    result = await read_generation_output(case.engine, case.scope, args(case))
    assert result["status"] == "AVAILABLE" and result["can_execute_or_approve"] is False
    assert case.engine.store.get("run", case.run["id"])["pending_unknown_operation"] == "pending-original-operation"
    assert_read_only(case, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("case_name", ["missing_item", "duplicate_item", "missing_attempt", "other_module_attempt", "duplicate_attempt", "wrong_journal", "bad_output_hash"])
async def test_exact_target_or_proof_failure_returns_no_other_content(reader_case, case_name):
    case = reader_case; request = args(case)
    if case_name == "missing_item": request["item_id"] = "not-in-job"
    elif case_name == "duplicate_item": case.client.job["body"]["items"].append(deepcopy(item(case)))
    elif case_name == "missing_attempt": request["attempt_id"] = "not-in-module"
    elif case_name == "other_module_attempt": request["attempt_id"] = "attempt_product_intro"
    elif case_name == "duplicate_attempt":
        request["attempt_id"] = "attempt_opening_long"
        item(case)["provider_attempts"].append(deepcopy(item(case)["provider_attempts"][0]))
    elif case_name == "wrong_journal": item(case)["module_results"]["opening_long"]["item_id"] = "other-item"
    elif case_name == "bad_output_hash": item(case)["provider_attempts"][0]["output_hash"] = canonical_hash("wrong")
    refresh(case)
    result = await read_generation_output(case.engine, case.scope, request)
    assert result["status"] == "UNAVAILABLE" and result["content"] is None
    assert_read_only(case, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("role,phase,branch", [("product_facts","execute","b1"),("main","intent",None),("main","execute","b1")])
async def test_reader_has_no_child_or_intent_authority(reader_case, role, phase, branch):
    case = reader_case
    with pytest.raises(ContractError, match="GENERATION_OUTPUT_READ_DENIED"):
        await read_generation_output(case.engine, RunScope(case.run["id"],"wrong",phase,role,branch), args(case))
    assert not case.client.queries and not case.client.commands


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["foreign_run", "foreign_prep", "wrong_job_id", "not_scripts", "tampered_history_ref", "foreign_account"])
async def test_existing_owner_scope_and_hash_boundaries_are_not_bypassed(reader_case, mutation):
    case = reader_case; scope = case.scope
    if mutation == "foreign_run": case.client.job["body"]["run_id"] = "other-run"
    elif mutation == "foreign_prep": case.client.job["body"]["preparation_id"] = "other-prep"
    elif mutation == "wrong_job_id": case.client.job["body"]["id"] = "other-job"
    elif mutation == "not_scripts": case.client.job["body"]["kind"] = "audio"
    elif mutation == "foreign_account": scope = RunScope(scope.run_id, "other-account", "execute", account_id="other")
    else:
        original = case.engine.business_query
        async def tampered(*a, **kw):
            value = await original(*a, **kw)
            value["full_ref"] = {**value["full_ref"], "content_hash":canonical_hash("tampered")}
            return value
        case.engine.business_query = tampered
    refresh(case)
    if mutation in {"tampered_history_ref", "foreign_account"}:
        with pytest.raises(ContractError):
            await read_generation_output(case.engine, scope, args(case))
    else:
        result = await read_generation_output(case.engine, scope, args(case))
        assert result["status"] == "UNVERIFIED" and result["content"] is None
    assert not case.client.commands


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["extra_path", "unknown_module", "unknown_view", "long_id", "bool_attempt", "empty_item", "constructed_bad"])
async def test_closed_bounded_input_rejects_without_echoing_private_input(reader_case, mutation):
    case = reader_case; request = args(case)
    if mutation == "extra_path": request["path"] = PRIVATE
    elif mutation == "unknown_module": request["module_key"] = PRIVATE
    elif mutation == "unknown_view": request["view"] = PRIVATE
    elif mutation == "long_id": request["job_id"] = "x" * 257
    elif mutation == "bool_attempt": request["attempt_id"] = True
    elif mutation == "empty_item": request["item_id"] = ""
    else: request = GenerationOutputReadInput.model_construct(**(request | {"module_key":PRIVATE}))
    with pytest.raises(ContractError) as caught:
        await read_generation_output(case.engine, case.scope, request)
    assert caught.value.code == "GENERATION_OUTPUT_READ_INPUT_INVALID"
    assert PRIVATE not in str(caught.value) and not case.client.queries
