"""Whole-script evidence association across Voice → Runtime → scoped readers.

No live provider, TTS, or business write. Whole raw stays a whole raw response;
validated selected module sentences use the complete output hash + journal.
"""
from copy import deepcopy
import json

import pytest

from shengbo_contracts.common import canonical_hash
from shengbo_prelive.generation_output_reads import read_generation_output
from test_generation_output_reads_rc13 import reader_case, args, item, refresh, assert_read_only
from test_recovery_workset_rc11 import reference, project

KEYS = ("opening_long", "product_intro", "selling_points", "cta")


def make_whole_job(*, failed=False):
    job_id, item_id = "job_recovery", "item_recovery"
    dependency = canonical_hash({"whole": "same inputs"})
    modules = {key: {"module_key": key, "sentences": ["可核对的" + key + "。"],
        "sentence_details": [{"display_text": "可核对的" + key + "。", "spoken_text": "可核对的" + key + "。",
                              "kind": "engagement", "fact_ids": []}]} for key in KEYS}
    refs = {key: reference("module", "whole-module-" + key) for key in KEYS}
    output = {"protocol": "whole-script-v1", "modules": modules,
              "usage": {"total_tokens": 300}, "usage_state": "known"}
    attempt = {"attempt_id": "whole-attempt", "purpose": "copywriting.whole_script",
        "input": {"generation_mode": "whole_script", "module_keys": list(KEYS), "dependency_hash": dependency},
        "state": "response_received" if failed else "result_received", "validation_state": "rejected" if failed else "accepted",
        "provider_response_ref": reference("provider_response", "whole-response"),
        "usage": {"total_tokens": 300}, "usage_state": "known",
        "generation_response": {"protocol": "whole-script-v1", "expected_module_keys": list(KEYS),
            "response_module_keys": list(KEYS), "is_formal_script": False, "reusable": False, "approvable": False}}
    attempt["public_response"] = {"content": '{"modules": [invalid entire response}' if failed else json.dumps({"modules": list(modules.values())}, ensure_ascii=False),
        "module_key": None, "expected_module_key": None, "receipt_ref": attempt["provider_response_ref"],
        "usage": attempt["usage"], "usage_state": "known", "validation_state": attempt["validation_state"],
        "state": "response_received", "is_formal_module": False, "reusable": False, "approvable": False}
    if failed:
        attempt["validation_error"] = {"code": "WHOLE_SCRIPT_RESPONSE_INVALID"}
    else:
        attempt.update(output=output, output_hash=canonical_hash(output), committed_module_refs=refs)
    journal = {} if failed else {key: {"status": "committed", "job_id": job_id, "item_id": item_id,
        "module_key": key, "module_ref": ref, "protocol": "whole-script-v1", "dependency_hash": dependency} for key, ref in refs.items()}
    body = {"id": job_id, "kind": "scripts", "status": "failed" if failed else "succeeded",
        "run_id": "run_recovery", "preparation_id": "preparation_recovery", "items": [{
            "item_id": item_id, "status": "failed" if failed else "succeeded", "call_state": attempt["state"],
            "input": {"product_ref": reference("product", "product_recovery"), "variant_id": "whole-v1", "version_number": 1,
                      "generation_mode": "whole_script"}, "provider_attempts": [attempt], "module_results": journal}]}
    return {"ref": reference("prelive_job", job_id, body), "body": body}


def test_successful_whole_job_has_no_false_attention_and_is_not_unindexed():
    job = make_whole_job(); before = deepcopy(job)
    filtered = project(job)
    assert filtered["attention_item_count"] == 0 and filtered["successful_item_count"] == 1
    assert filtered["unindexed_attempt_count"] == 0 and filtered["modules"] == []
    view = project(job, only_attention_items=False)
    assert len(view["modules"]) == len(KEYS)
    for row in view["modules"]:
        assert row["validation_state"] == "accepted"
        assert row["accepted_output_reader"]["path"] == (
            "/job/body/items/0/provider_attempts/0/output/modules/" + row["module_key"] + "/sentence_details")
    assert len(job["body"]["items"][0]["provider_attempts"]) == 1
    assert job == before


def test_failed_whole_raw_is_one_response_with_module_scoped_index_and_no_partial_formal_ref():
    job = make_whole_job(failed=True)
    view = project(job)
    assert view["attention_item_count"] == 1 and view["unindexed_attempt_count"] == 0
    assert len(view["modules"]) == len(KEYS)
    for row in view["modules"]:
        assert row["status"] == "validation_rejected"
        assert row["public_response_scope"] == "whole_script"
        assert row["public_response_reader"]["path"] == "/job/body/items/0/provider_attempts/0/public_response/content"
        assert "module_ref" not in row


def test_whole_unknown_attempt_counts_once_globally_and_never_falls_back_to_old_accepted():
    job = make_whole_job()
    pending = deepcopy(job["body"]["items"][0]["provider_attempts"][0])
    pending.update(attempt_id="pending-whole", state="timeout_unknown", validation_state="unknown")
    job["body"]["items"][0]["provider_attempts"].append(pending)
    view = project(job)
    assert view["outcome_unknown_count"] == 1 and view["unindexed_unknown_attempt_count"] == 0
    assert all(row["status"] == "outcome_unknown" for row in view["modules"])
    assert all("public_response_reader" not in row for row in view["modules"])


@pytest.fixture
async def whole_case(reader_case):
    case = reader_case
    case.client.job = make_whole_job()
    case.client.job["body"].update(run_id=case.run["id"], preparation_id=case.run["scope"]["preparation_id"])
    refresh(case)
    return case


@pytest.mark.asyncio
async def test_whole_accepted_reader_returns_only_requested_module_and_runtime_labels_whole_correctly(whole_case):
    case = whole_case
    result = await read_generation_output(case.engine, case.scope, args(case, module="selling_points"))
    assert result["status"] == "AVAILABLE"
    assert result["content"] == item(case)["provider_attempts"][0]["output"]["modules"]["selling_points"]["sentence_details"]
    assert result["committed_module_ref"] == item(case)["module_results"]["selling_points"]["module_ref"]
    assert "opening_long" not in json.dumps(result["content"])
    assert_read_only(case, result)
    attempt = item(case)["provider_attempts"][0]
    view = case.engine._provider_attempt_view(case.run, case.client.job, item(case), attempt)
    assert view["module_key"] is None and view["expected_module_key"] is None
    assert view["generation_protocol"] == "whole-script-v1" and view["module_keys"] == list(KEYS)
    assert view["committed_module_refs"] == attempt["committed_module_refs"]
    assert view["generation_response"] == attempt["generation_response"]
    assert view["provider_attempt_count"] == 1 and view["can_reuse_as_formal_module"] is False


@pytest.mark.asyncio
async def test_whole_failed_reader_returns_raw_whole_without_parsing_into_fake_module(whole_case):
    case = whole_case
    failed = make_whole_job(failed=True)["body"]["items"][0]
    case.client.job["body"]["items"] = [failed]; case.client.job["body"]["status"] = "failed"; refresh(case)
    result = await read_generation_output(case.engine, case.scope, args(case, module="cta", view="latest_failed_response"))
    assert result["status"] == "AVAILABLE"
    assert result["content"] == failed["provider_attempts"][0]["public_response"]["content"]
    assert result["content_kind"] == "archived_public_rejected_whole_response"
    assert result["response_scope"] == "whole_script" and result["requested_module_keys"] == list(KEYS)
    assert result["committed_module_ref"] is None
    assert_read_only(case, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["output_hash", "wrong_ref", "missing_ref", "missing_module", "wrong_key", "wrong_protocol", "wrong_dependency"])
async def test_whole_accepted_evidence_requires_complete_protocol_and_matching_commit_association(whole_case, mutation):
    case = whole_case; attempt = item(case)["provider_attempts"][0]
    if mutation == "output_hash": attempt["output_hash"] = canonical_hash("wrong")
    elif mutation == "wrong_ref": attempt["committed_module_refs"]["cta"] = reference("module", "wrong-cta")
    elif mutation == "missing_ref": attempt["committed_module_refs"].pop("opening_long")
    elif mutation == "missing_module": attempt["output"]["modules"].pop("opening_long")
    elif mutation == "wrong_key": attempt["output"]["modules"]["cta"]["module_key"] = "selling_points"
    elif mutation == "wrong_protocol": attempt["output"]["protocol"] = "other"
    else: item(case)["module_results"]["cta"]["dependency_hash"] = canonical_hash("wrong")
    if mutation != "output_hash": attempt["output_hash"] = canonical_hash(attempt["output"])
    refresh(case)
    result = await read_generation_output(case.engine, case.scope, args(case, module="cta"))
    assert result["status"] == "UNAVAILABLE" and result["content"] is None
    assert_read_only(case, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["metadata", "receipt", "formal", "single_module_mislabelling"])
async def test_whole_rejected_raw_requires_voice_association_and_never_claims_formal_authority(whole_case, mutation):
    case = whole_case
    failed = make_whole_job(failed=True)["body"]["items"][0]
    case.client.job["body"]["items"] = [failed]
    attempt = failed["provider_attempts"][0]
    if mutation == "metadata": attempt.pop("generation_response")
    elif mutation == "receipt": attempt["provider_response_ref"] = reference("provider_response", "foreign-receipt")
    elif mutation == "formal": attempt["generation_response"]["is_formal_script"] = True
    else: attempt["public_response"].update(module_key="cta", expected_module_key="cta")
    refresh(case)
    result = await read_generation_output(case.engine, case.scope, args(case, module="cta", view="latest_failed_response"))
    assert result["status"] == "UNAVAILABLE" and result["content"] is None
    assert_read_only(case, result)
