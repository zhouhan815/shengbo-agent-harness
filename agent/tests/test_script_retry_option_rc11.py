"""Known rejected output offers an optional original-job retry, never a write."""
from copy import deepcopy

import pytest

from shengbo_prelive.workflows import run_workflow, script_recovery_options
from test_provider_output_convergence import convergence, ref
from test_public_provider_response_recovery_v1 import rejected_job, failed_item


def known_job():
    return {"ref": ref("prelive_job", "original-job", {"job": True}),
        "body": {"id": "original-job", "kind": "scripts", "status": "partially_failed", "items": [{
            "call_state": "result_received", "error": {"code": "MODULE_RESPONSE_INVALID", "retryable": False},
            "committed_modules": {"opening_long": {"module_ref": ref("module", "keep-module", {"text": "已保存"})}},
            "provider_attempts": [{"state": "response_received", "validation_state": "rejected",
                "validation_error": {"code": "MODULE_RESPONSE_INVALID", "retryable": False},
                "public_response": {"content": "已收到但不合格的公开原文"}}]}]},
        "items_projection": {"unknown_item_count": 0, "unknown_provider_attempt_count": 0,
                             "in_flight_provider_attempt_count": 0}}


def test_known_received_rejection_offers_exact_original_job_without_mutating_false_or_raw():
    job = known_job()
    original = deepcopy(job)
    option, = script_recovery_options(job, "NEEDS_REPLAN")
    assert option["tool"] == "wf_script_execution"
    assert option["arguments"] == {"action": "retry", "job_id": "original-job"}
    assert option["execution_authorized"] is False
    assert option["observed_validation_rejection_codes"] == ["MODULE_RESPONSE_INVALID"]
    assert job == original
    assert job["body"]["items"][0]["error"]["retryable"] is False
    assert "不自动" in option["note"] and "局改" in option["note"] and "不合法会拒绝" in option["note"]


@pytest.mark.parametrize("state", ["sent", "outcome_unknown", "unknown", None, "unrecognized"])
def test_unknown_or_inflight_attempt_never_offers_direct_retry(state):
    job = known_job()
    job["body"]["items"][0]["provider_attempts"].append({"state": state})
    assert script_recovery_options(job, "NEEDS_REPLAN") == []


@pytest.mark.parametrize("count", ["unknown_item_count", "unknown_provider_attempt_count", "in_flight_provider_attempt_count"])
def test_global_projection_counts_suppress_retry_even_with_a_known_rejection(count):
    job = known_job()
    job["items_projection"][count] = 1
    assert script_recovery_options(job, "NEEDS_REPLAN") == []


@pytest.mark.parametrize("outcome", ["OUTCOME_UNKNOWN", "WAITING", "COMPLETED"])
def test_only_known_replan_state_can_expose_the_option(outcome):
    assert script_recovery_options(known_job(), outcome) == []


@pytest.mark.parametrize("kind,status,code", [("audio", "failed", "MODULE_RESPONSE_INVALID"),
    ("scripts", "succeeded", "MODULE_RESPONSE_INVALID"), ("scripts", "failed", "TRANSPORT_ERROR")])
def test_wrong_job_or_nonvalidation_failure_is_not_marked_as_this_retry_case(kind, status, code):
    job = known_job()
    job["body"].update(kind=kind, status=status)
    job["body"]["items"][0]["provider_attempts"][0]["validation_error"]["code"] = code
    assert script_recovery_options(job, "NEEDS_REPLAN") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["status", "execute"])
@pytest.mark.parametrize("unknown", [False, True])
async def test_f04_option_preserves_existing_job_modules_and_adds_no_read_or_command(convergence, action, unknown):
    case = convergence
    item, _, _ = rejected_job(case)
    item["error"]["retryable"] = False
    item["provider_attempts"][-1]["validation_error"]["retryable"] = False
    job = case.client.job
    original = deepcopy(job)
    prep = {"ref": ref("preparation", case.run["scope"]["preparation_id"], {"revision": 1}),
        "body": {"id": case.run["scope"]["preparation_id"], "plan_ref": job["body"]["plan_ref"],
                 "bundle_ref": job["body"]["context_ref"], "generation_epoch": 3}}

    async def query(key, args=None, **kwargs):
        case.client.queries.append((key, deepcopy(args), deepcopy(kwargs)))
        if key == "read_preparation":
            return deepcopy(prep)
        if key == "read_completion_observation":
            return {"preparation": deepcopy(prep), "jobs": [deepcopy(job)], "pending_job_ids": [],
                    "unknown_job_ids": [job["ref"]["id"]] if unknown else []}
        assert key == "read_job"
        return deepcopy(job)

    case.client.query = query
    result = await run_workflow(case.engine, case.scope, "wf_script_execution", {"action": action})
    assert result["status"] == ("OUTCOME_UNKNOWN" if unknown else "NEEDS_REPLAN")
    view = result["result"]["existing_job"]
    assert failed_item(view)["committed_modules"] == item["module_results"]
    assert failed_item(view)["error"]["retryable"] is False
    assert "public_response" in failed_item(view)["provider_attempts"][0]
    if unknown:
        assert "optional_next_actions" not in result["result"]
    else:
        option, = result["result"]["optional_next_actions"]
        assert option["arguments"] == {"action": "retry", "job_id": job["ref"]["id"]}
    assert [key for key, _, _ in case.client.queries] == ["read_preparation", "read_completion_observation", "read_job"]
    assert case.client.job == original and not case.client.commands and not case.sent
