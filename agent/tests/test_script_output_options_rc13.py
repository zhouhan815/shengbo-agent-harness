"""Output reader hints never grant permission, hide unknowns or read extra data."""
from copy import deepcopy

import pytest

from shengbo_prelive.workflows import run_workflow, script_output_read_options
from test_provider_output_convergence import convergence, ref
from test_public_provider_response_recovery_v1 import rejected_job


def verified_view():
    reference = ref("prelive_job", "job-one", {"job": True})
    return {"ref": reference, "recovery_workset": {"status": "verified_job_snapshot", "job_ref": reference,
        "outcome_unknown_count": 0, "in_flight_count": 0,
        "modules": [{"item_id": "item-one", "module_key": "cta", "status": "validation_rejected",
            "public_response_reader": {"tool": "read_run_history"}}]}}


def test_exact_bounded_optional_reader_does_not_mutate_job_or_offer_action():
    job = verified_view()
    job["recovery_workset"]["modules"] *= 7
    before = deepcopy(job)
    hints = script_output_read_options(job, "NEEDS_REPLAN")
    assert len(hints) == 4 and job == before
    assert hints[0]["arguments"] == {"job_id": "job-one", "item_id": "item-one", "module_key": "cta", "view": "latest_failed_response"}
    assert hints[0]["tool"] == "read_generation_output"
    assert hints[0]["execution_authorized"] is False and hints[0]["approval_implied"] is False


@pytest.mark.parametrize("tamper", ["unknown", "in_flight", "unverified", "wrong_ref", "unproved", "committed", "invalid_module", "invalid_item"])
def test_unverified_or_not_failed_rows_do_not_supply_hint(tamper):
    job = verified_view()
    work = job["recovery_workset"]
    row = work["modules"][0]
    if tamper == "unknown": work["outcome_unknown_count"] = 1
    elif tamper == "in_flight": work["in_flight_count"] = 1
    elif tamper == "unverified": work["status"] = "unverified"
    elif tamper == "wrong_ref": work["job_ref"] = ref("prelive_job", "foreign", {})
    elif tamper == "unproved": row.pop("public_response_reader")
    elif tamper == "committed": row["status"] = "committed"
    elif tamper == "invalid_module": row["module_key"] = "foreign"
    elif tamper == "invalid_item": row["item_id"] = ""
    assert script_output_read_options(job, "NEEDS_REPLAN") == []


@pytest.mark.parametrize("outcome", ["OUTCOME_UNKNOWN", "WAITING", "COMPLETED"])
def test_nonreplan_state_never_suggests_rejected_body(outcome):
    assert script_output_read_options(verified_view(), outcome) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["status", "execute"])
async def test_f04_offers_verified_exact_reader_with_no_extra_query_or_command(convergence, action):
    case = convergence
    item, _, _ = rejected_job(case)
    job = case.client.job
    prep = {"ref": ref("preparation", case.run["scope"]["preparation_id"], {"revision": 1}),
        "body": {"id": case.run["scope"]["preparation_id"], "plan_ref": job["body"]["plan_ref"],
                 "bundle_ref": job["body"]["context_ref"], "generation_epoch": 3}}

    async def query(key, args=None, **kwargs):
        case.client.queries.append((key, deepcopy(args), deepcopy(kwargs)))
        if key == "read_preparation": return deepcopy(prep)
        if key == "read_completion_observation":
            return {"preparation": deepcopy(prep), "jobs": [deepcopy(job)], "pending_job_ids": [], "unknown_job_ids": []}
        assert key == "read_job"
        return deepcopy(job)

    case.client.query = query
    result = await run_workflow(case.engine, case.scope, "wf_script_execution", {"action": action})
    hint, = result["result"]["optional_output_reads"]
    assert hint["arguments"]["item_id"] == item["item_id"]
    assert hint["arguments"]["job_id"] == job["ref"]["id"]
    assert hint["arguments"]["module_key"] == "cta"
    assert [key for key, _, _ in case.client.queries] == ["read_preparation", "read_completion_observation", "read_job"]
    assert not case.client.commands and not case.sent
