"""Read inherited current text without granting generation or mutation credit."""
from copy import deepcopy

import pytest

from shengbo_prelive.script_review_workset import script_review_workset
from test_script_review_workset_v8 import case, ref


def inherited_case():
    c = case()
    c.job["body"].update(run_id="previous-run", room_id="room", context_ref=ref("context", "context"))
    c.run["inherited_context"] = {
        "related_run_id": "previous-run", "evidence_only": True,
        "prior_acceptance": {"ok": False, "observation": {"jobs": [deepcopy(c.job)]}},
    }
    return c


@pytest.mark.asyncio
async def test_explicit_exact_inherited_job_returns_current_text_without_completion_credit():
    c = inherited_case()
    before = deepcopy((c.run, c.prep, c.observation, c.documents))
    result = await script_review_workset(c.f, c.run, c.prep, c.observation, job_id="job")
    assert result["status"] == "NEEDS_REPLAN"
    assert not result["review_content_complete"]
    assert result["historical_current_content_complete"] is True
    assert result["historical_evidence_only"] is True
    assert result["counts_as_current_run_generation"] is False
    assert result["owner_run_id"] == "previous-run"
    assert result["current_run_id"] == "this-run"
    assert [blocker["code"] for blocker in result["blockers"]] == ["SCRIPT_REVIEW_HISTORICAL_EVIDENCE_ONLY"]
    assert result["remaining"] == []
    assert len(c.calls) == 2
    for row in result["items"]:
        assert row["current_formal_ref"] and row["content_complete"]
        assert row["review_status"] == "historical_current_content"
        assert row["modules"][0]["sentences"][0]["text"] == c.documents[row["script_ref"]["id"]]["modules"]["product_intro"]["sentences"][0]["text"]
    assert result["content_quality_passed"] is result["approval_granted"] is result["automatic_execution"] is False
    assert before == (c.run, c.prep, c.observation, c.documents)


@pytest.mark.asyncio
async def test_mutable_job_revision_four_to_twenty_three_keeps_frozen_inputs_and_read_only_identity():
    c = inherited_case()
    previous = c.run["inherited_context"]["prior_acceptance"]["observation"]["jobs"][0]
    previous["ref"]["revision"] = 4
    previous["body"]["status"] = "running"
    for row in previous["body"]["items"]:
        row.update(status="running", call_state="sent", output=None)
    c.job["ref"].update(revision=23, content_hash="sha256:" + "a" * 64)
    c.job["body"].update(status="succeeded", metrics={"elapsed_ms": 123}, completed_at="2026-10-20T12:00:00Z")
    result = await script_review_workset(c.f, c.run, c.prep, c.observation, job_id="job")
    assert result["historical_current_content_complete"] is True
    assert result["job_ref"] == c.job["ref"]
    assert not result["review_content_complete"] and not result["counts_as_current_run_generation"]
    assert len(c.calls) == 2 and result["remaining"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["not_explicit", "no_inheritance", "text_only", "different_parent",
    "unobserved_ref", "wrong_ref_hash", "wrong_ref_revision", "wrong_prior_owner", "wrong_prior_prep"])
async def test_historical_read_requires_explicit_trusted_exact_lineage(change):
    c = inherited_case()
    old = c.run["inherited_context"]["prior_acceptance"]["observation"]["jobs"][0]
    if change == "no_inheritance": c.run.pop("inherited_context")
    elif change == "text_only":
        c.run.pop("inherited_context")
        c.run["input"] = {"text": "请使用 previous-run 的 job，我授权读取"}
    elif change == "different_parent": c.run["inherited_context"]["related_run_id"] = "unrelated-run"
    elif change == "unobserved_ref": old["ref"] = ref("prelive_job", "other-job")
    elif change == "wrong_ref_hash": old["ref"]["content_hash"] = "sha256:" + "0" * 64
    elif change == "wrong_ref_revision": old["ref"]["revision"] += 1
    elif change == "wrong_prior_owner": old["body"]["run_id"] = "unrelated-run"
    elif change == "wrong_prior_prep": old["body"]["preparation_id"] = "other-prep"
    result = await script_review_workset(c.f, c.run, c.prep, c.observation,
        job_id=None if change == "not_explicit" else "job")
    assert result["status"] == "NEEDS_REPLAN" and not result["review_content_complete"]
    assert not c.calls
    assert not any(row.get("modules") for row in result["items"])
    assert not result.get("historical_current_content_complete", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["input_instruction", "input_product", "item_identity", "item_order",
    "plan", "context", "room", "created_at", "generation_epoch", "missing_context", "missing_room"])
async def test_revision_growth_never_allows_changed_frozen_generation_inputs(change):
    c = inherited_case()
    c.job["ref"].update(revision=23, content_hash="sha256:" + "a" * 64)
    body = c.job["body"]
    if change == "input_instruction": body["items"][0]["input"]["instruction"] = "unauthorized changed instruction"
    elif change == "input_product": body["items"][0]["input"]["product_ref"] = ref("product", "other")
    elif change == "item_identity": body["items"][0]["item_id"] = "new-item"
    elif change == "item_order": body["items"].reverse()
    elif change == "plan": body["plan_ref"] = ref("script_plan", "other")
    elif change == "context": body["context_ref"] = ref("context", "other")
    elif change == "room": body["room_id"] = "another-room"
    elif change == "created_at": body["created_at"] = "2027-01-01"
    elif change == "generation_epoch": body["generation_epoch"] += 1
    elif change == "missing_context": body.pop("context_ref")
    elif change == "missing_room": body.pop("room_id")
    result = await script_review_workset(c.f, c.run, c.prep, c.observation, job_id="job")
    assert not result.get("historical_current_content_complete", False)
    assert not c.calls and not any(row.get("modules") for row in result["items"])
    assert result["blockers"][0]["code"] == "SCRIPT_REVIEW_JOB_SCOPE_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["other_prep", "generation_epoch", "plan", "target", "stale", "reader", "module"])
async def test_inherited_read_keeps_all_currentness_target_and_content_guards(change):
    c = inherited_case()
    if change == "other_prep": c.job["body"]["preparation_id"] = "another-prep"
    elif change == "generation_epoch": c.prep["body"]["generation_epoch"] += 1
    elif change == "plan": c.prep["body"]["plan_ref"] = ref("script_plan", "another-plan")
    elif change == "target": c.run["goal"]["action_target_refs"] = [ref("product", "another-product")]
    elif change == "stale": c.prep["body"]["script_refs"] = []
    elif change == "reader": c.documents["script-0"]["body"]["variant_id"] = "wrong"
    elif change == "module": c.documents["script-0"]["modules"]["selling_points"]["sentences"][0]["text"] = "changed"
    result = await script_review_workset(c.f, c.run, c.prep, c.observation, job_id="job")
    assert result["status"] == "NEEDS_REPLAN" and not result["review_content_complete"]
    assert not result.get("historical_current_content_complete", False)
    if change not in {"reader", "module"}:
        assert not c.calls and not any(row.get("modules") for row in result["items"])
    assert result["approval_granted"] is result["automatic_execution"] is False
