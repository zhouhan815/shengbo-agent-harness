"""Offline association/readability tests; no runtime graph or providers."""
from copy import deepcopy
import json

import pytest

from shengbo_contracts.common import canonical_hash
from shengbo_prelive.observability import sanitize
from shengbo_prelive.recovery_workset import project_job_recovery_workset
from shengbo_prelive.snapshot_views import project_snapshot


RUN, PREP, CONVERSATION = "run_recovery", "preparation_recovery", "conversation_recovery"


def reference(kind, name, value=None):
    return {"kind": kind, "id": name, "revision": 1, "content_hash": canonical_hash(value if value is not None else name)}


def public(attempt, key):
    return {"content": json.dumps({"module_key": key, "sentences": [{"spoken_text": "仅供公开审查"}]}, ensure_ascii=False),
        "module_key": key, "expected_module_key": key, "receipt_ref": attempt["provider_response_ref"],
        "usage": {}, "usage_state": "known", "validation_state": attempt["validation_state"],
        "state": "response_received", "is_formal_module": False, "reusable": False, "approvable": False}


def make_job(keys=("opening_long", "product_intro"), *, failed="selling_points"):
    job_id, item_id = "job_recovery", "item_recovery"
    item = {"item_id": item_id, "status": "failed" if failed else "succeeded", "call_state": "result_received",
        "input": {"product_ref": reference("product", "product_recovery"), "variant_id": "v1", "version_number": 1},
        "module_results": {}, "provider_attempts": []}
    for key in keys:
        ref = reference("module", "module_" + key)
        output = {"sentences": {"module_key": key,
            "sentence_details": [{"display_text": "可核对文字", "spoken_text": "可核对文字", "kind": "engagement", "fact_ids": []}],
            "sentences": ["可核对文字"]}}
        attempt = {"attempt_id": "attempt_" + key, "purpose": "copywriting." + key, "state": "result_received",
            "validation_state": "accepted", "committed_module_ref": ref, "output": output,
            "output_hash": canonical_hash(output), "provider_response_ref": reference("provider_response", "response_" + key)}
        attempt["public_response"] = public(attempt, key)
        item["provider_attempts"].append(attempt)
        item["module_results"][key] = {"status": "committed", "job_id": job_id, "item_id": item_id,
            "module_key": key, "module_ref": ref}
    if failed:
        attempt = {"attempt_id": "attempt_" + failed, "purpose": "copywriting." + failed,
            "state": "response_received", "validation_state": "rejected",
            "validation_error": {"code": "MODULE_RESPONSE_INVALID", "message": "private diagnostic must not be copied"},
            "provider_response_ref": reference("provider_response", "response_" + failed)}
        attempt["public_response"] = public(attempt, failed)
        item["provider_attempts"].append(attempt)
    body = {"id": job_id, "kind": "scripts", "status": "partially_failed" if failed else "succeeded",
        "run_id": RUN, "preparation_id": PREP, "items": [item]}
    return {"ref": reference("prelive_job", job_id, body), "body": body}


def snapshot(job):
    body = sanitize({"run_id": RUN, "conversation_id": CONVERSATION, "document": {"kind": "job", "job": job}})
    digest = canonical_hash(body)
    return {"kind": "runtime_context", "id": "context_" + digest.split(":")[1], "revision": 1, "content_hash": digest}, body


def project(job, **kwargs):
    ref, body = snapshot(job)
    return project_job_recovery_workset(job, snapshot_ref=ref, snapshot_body=body,
        expected_run_id=RUN, expected_preparation_id=PREP, expected_conversation_id=CONVERSATION, **kwargs)


def by_key(view, key):
    return next(row for row in view["modules"] if row["module_key"] == key)


def test_committed_sentence_readers_are_exact_and_failure_first_without_mutation():
    job = make_job()
    original = deepcopy(job)
    view = project(job)
    assert view["status"] == "verified_job_snapshot"
    assert view["modules"][0]["module_key"] == "selling_points"
    assert view["modules"][0]["validation_state"] == "rejected"
    assert view["modules"][0]["error_code"] == "MODULE_RESPONSE_INVALID"
    assert view["modules"][0]["outcome"] == "known_result"
    assert view["modules"][0]["public_response_is_formal_module"] is False
    ref, body = snapshot(job)
    for key, index in [("opening_long", 0), ("product_intro", 1)]:
        row = by_key(view, key)
        assert row["status"] == "committed"
        assert row["module_ref"] == job["body"]["items"][0]["module_results"][key]["module_ref"]
        assert row["accepted_output_reader"] == {"tool": "read_run_history", "ref": ref,
            "path": f"/job/body/items/0/provider_attempts/{index}/output/sentences/sentence_details"}
        selected = project_snapshot(body["document"], path=row["accepted_output_reader"]["path"])
        assert selected["selection"]["next_cursor"] is None
        assert canonical_hash(selected["document"]) == row["accepted_output_hash"]
        assert selected["document"][0]["spoken_text"] == "可核对文字"
        assert row["module_reader_is_formal_body"] is False
    assert view["total_module_count"] == 3
    assert view["currentness_verified"] is False
    assert view["approval_implied"] is False
    assert view["acceptance_implied"] is False
    assert "private diagnostic" not in json.dumps(view)
    assert job == original


@pytest.mark.parametrize("field,other", [("expected_run_id", "another_run"),
    ("expected_preparation_id", "another_preparation"), ("expected_conversation_id", "another_conversation")])
def test_wrong_scope_never_produces_module_or_content_reader(field, other):
    job = make_job(); ref, body = snapshot(job)
    args = {"expected_run_id": RUN, "expected_preparation_id": PREP, "expected_conversation_id": CONVERSATION}
    args[field] = other
    view = project_job_recovery_workset(job, snapshot_ref=ref, snapshot_body=body, **args)
    assert view["status"] == "not_projected"
    assert view["modules"] == []
    assert "full_job_reader" not in view


@pytest.mark.parametrize("change", ["kind", "revision", "boolean_revision", "hash", "id", "schema"])
def test_invalid_exact_snapshot_ref_fails_closed(change):
    job = make_job(); ref, body = snapshot(job)
    if change == "kind": ref["kind"] = "module"
    if change == "revision": ref["revision"] = 2
    if change == "boolean_revision": ref["revision"] = True
    if change == "hash": ref["content_hash"] = "sha256:" + "0" * 64
    if change == "id": ref["id"] = "context_some_other_snapshot"
    if change == "schema": ref["schema_version"] = "UNTRUSTED"
    view = project_job_recovery_workset(job, snapshot_ref=ref, snapshot_body=body,
        expected_run_id=RUN, expected_preparation_id=PREP, expected_conversation_id=CONVERSATION)
    assert view["status"] == "not_projected" and view["modules"] == []


def test_job_must_match_the_already_archived_snapshot():
    job = make_job(); ref, body = snapshot(job)
    job["body"]["items"][0]["module_results"]["opening_long"]["status"] = "pending"
    view = project_job_recovery_workset(job, snapshot_ref=ref, snapshot_body=body,
        expected_run_id=RUN, expected_preparation_id=PREP, expected_conversation_id=CONVERSATION)
    assert view["reason_code"] == "JOB_SNAPSHOT_MISMATCH"


@pytest.mark.parametrize("field", ["job_id", "item_id", "module_key"])
def test_journal_must_prove_same_job_item_and_module(field):
    job = make_job(); journal = job["body"]["items"][0]["module_results"]["opening_long"]
    journal[field] = "other"
    row = by_key(project(job), "opening_long")
    assert row["module_ref"] == journal["module_ref"]
    assert row["accepted_output_status"] == "journal_association_unverified"
    assert "accepted_output_reader" not in row


@pytest.mark.parametrize("change", ["different_ref", "rejected", "output_hash", "output_key", "empty_sentences"])
def test_accepted_output_is_not_inferred_from_a_module_ref_alone(change):
    job = make_job(); a = job["body"]["items"][0]["provider_attempts"][0]
    if change == "different_ref": a["committed_module_ref"] = reference("module", "another_module")
    if change == "rejected": a["validation_state"] = "rejected"
    if change == "output_hash": a["output_hash"] = "sha256:" + "0" * 64
    if change == "output_key":
        a["output"]["sentences"]["module_key"] = "cta"; a["output_hash"] = canonical_hash(a["output"])
    if change == "empty_sentences":
        a["output"]["sentences"]["sentence_details"] = []; a["output_hash"] = canonical_hash(a["output"])
    row = by_key(project(job), "opening_long")
    assert row["module_ref"] and "accepted_output_reader" not in row
    assert row["validation_state"] == "unverified"


def test_two_different_accepted_bodies_for_the_same_module_ref_are_ambiguous():
    job = make_job(); attempts = job["body"]["items"][0]["provider_attempts"]
    duplicate = deepcopy(attempts[0]); duplicate["attempt_id"] = "another_attempt"
    duplicate["output"]["sentences"]["sentence_details"][0]["spoken_text"] = "不同内容"
    duplicate["output_hash"] = canonical_hash(duplicate["output"]); attempts.append(duplicate)
    row = by_key(project(job), "opening_long")
    assert row["accepted_output_status"] == "ambiguous_accepted_outputs"
    assert "accepted_output_reader" not in row


@pytest.mark.parametrize("state", ["outcome_unknown", "timeout_unknown", "sent"])
def test_unknown_or_inflight_is_not_hidden_by_a_committed_ref_and_no_raw_reader(state):
    job = make_job(); attempts = job["body"]["items"][0]["provider_attempts"]
    pending = deepcopy(attempts[0]); pending["state"] = state; pending["validation_state"] = "unknown" if state != "sent" else "pending"
    attempts.append(pending)
    row = by_key(project(job), "opening_long")
    assert row["outcome"] == ("in_flight" if state == "sent" else "outcome_unknown")
    assert "public_response_reader" not in row
    assert row["module_ref"]
    assert project(job)["outcome_unknown_count"] == (0 if state == "sent" else 1)


@pytest.mark.parametrize("change", ["wrong_receipt", "formal_claim", "wrong_module", "missing_public"])
def test_rejected_public_response_reader_requires_strict_archived_association(change):
    job = make_job(); a = job["body"]["items"][0]["provider_attempts"][-1]
    if change == "wrong_receipt": a["public_response"]["receipt_ref"] = reference("provider_response", "unrelated")
    if change == "formal_claim": a["public_response"]["is_formal_module"] = True
    if change == "wrong_module": a["public_response"]["module_key"] = "cta"
    if change == "missing_public": del a["public_response"]
    row = by_key(project(job), "selling_points")
    assert row["validation_state"] == "rejected" and row["error_code"] == "MODULE_RESPONSE_INVALID"
    assert "public_response_reader" not in row


def test_tail_failure_unknown_and_inflight_counts_survive_a_bounded_index():
    job = make_job(("cta",), failed=None); first = job["body"]["items"][0]
    items = []
    for i in range(20):
        item = deepcopy(first); item["item_id"] = f"item_{i}"
        item["module_results"]["cta"]["item_id"] = item["item_id"]
        items.append(item)
    for pos, state, validation in [(17, "response_received", "rejected"), (18, "outcome_unknown", "unknown"), (19, "sent", "pending")]:
        item = items[pos]; item["module_results"] = {}; a = item["provider_attempts"][0]
        a["state"], a["validation_state"] = state, validation
        a["public_response"]["validation_state"] = validation
        a["validation_error"] = {"code": "MODULE_RESPONSE_INVALID"}
    job["body"]["items"] = items
    view = project(job, max_modules=2)
    assert view["total_module_count"] == 20 and view["returned_module_count"] == 2
    assert view["omitted_module_count"] == 18
    assert view["outcome_unknown_count"] == 1 and view["in_flight_count"] == 1
    assert [r["item_position"] for r in view["modules"]] == [18, 17]
    assert view["full_job_reader"]["path"] == "/job/body/items"


def test_old_rejection_does_not_become_new_blocker_when_same_module_is_committed():
    job = make_job(("selling_points",), failed=None); item = job["body"]["items"][0]
    old = deepcopy(item["provider_attempts"][0]); old.pop("committed_module_ref")
    old["validation_state"] = "rejected"; item["provider_attempts"].insert(0, old)
    # An explicit all-items view remains available for diagnostics; the default
    # recovery surface does not duplicate a successfully completed item.
    assert project(job)["modules"] == []
    row = by_key(project(job, only_attention_items=False), "selling_points")
    assert row["status"] == "committed" and row["accepted_output_reader"]


def test_unindexed_unknown_purpose_does_not_echo_user_text():
    job = make_job(); item = job["body"]["items"][0]
    item["provider_attempts"].append({"purpose": "copywriting.secret_user_supplied_value", "state": "outcome_unknown"})
    view = project(job)
    assert view["unindexed_attempt_count"] == 1
    assert view["outcome_unknown_count"] == 1
    assert view["unindexed_unknown_attempt_count"] == 1
    assert "secret_user_supplied_value" not in json.dumps(view)


def test_unknown_in_an_unindexable_item_is_still_counted_and_keeps_full_reader():
    job = make_job(); item = job["body"]["items"][0]
    item["item_id"] = None
    item["provider_attempts"][0]["state"] = "outcome_unknown"
    view = project(job)
    assert view["modules"] == [] and view["unindexed_item_count"] == 1
    assert view["outcome_unknown_count"] == 1 and view["unindexed_unknown_attempt_count"] == 1
    assert view["full_job_reader"]["path"] == "/job/body/items"


def test_non_script_job_is_not_projected_as_modules():
    job = make_job(); job["body"]["kind"] = "audio"
    assert project(job)["reason_code"] == "NOT_A_SCRIPT_JOB"


def test_known_provider_failure_remains_known_without_a_raw_result_reader():
    job = make_job(); attempt = job["body"]["items"][0]["provider_attempts"][-1]
    attempt["state"] = "failed"
    attempt["error"] = {"code": "PROVIDER_REJECTED", "message": "not projected"}
    attempt.pop("validation_error")
    row = by_key(project(job), "selling_points")
    assert row["status"] == "known_failure" and row["outcome"] == "known_failure"
    assert row["error_code"] == "PROVIDER_REJECTED"
    assert "public_response_reader" not in row


def test_default_recovery_surface_omits_successful_item_but_preserves_exact_total_and_reader():
    job = make_job(); successful = make_job(("opening_short", "cta"), failed=None)["body"]["items"][0]
    successful["item_id"] = "item_success"
    for value in successful["module_results"].values(): value["item_id"] = "item_success"
    job["body"]["items"].append(successful)
    view = project(job)
    assert view["only_attention_items"] is True
    assert view["successful_item_count"] == view["filtered_successful_item_count"] == 1
    assert view["attention_item_count"] == 1
    assert view["total_module_count"] == 5 and view["returned_module_count"] == 3
    assert view["omitted_module_count"] == view["omitted_successful_module_count"] == 2
    assert view["omitted_selected_module_count"] == 0
    assert {r["item_position"] for r in view["modules"]} == {0}
    full = project(job, only_attention_items=False)
    assert full["returned_module_count"] == 5 and full["omitted_module_count"] == 0
    assert full["full_job_reader"] == view["full_job_reader"]


@pytest.mark.parametrize("shape", ["direct_details", "spoken_lines"])
def test_validated_legacy_output_shapes_keep_exact_readers(shape):
    job = make_job(); a = job["body"]["items"][0]["provider_attempts"][0]
    a["output"] = {"sentences": ([{"text": "旧格式", "spoken_text": "旧格式"}]
        if shape == "direct_details" else ["旧格式"])}
    a["output_hash"] = canonical_hash(a["output"])
    row = by_key(project(job), "opening_long")
    assert row["accepted_output_reader"]["path"].endswith("/output/sentences")
    assert row["sentence_count"] == 1


def test_attention_filter_requires_real_boolean():
    with pytest.raises(ValueError): project(make_job(), only_attention_items="true")


@pytest.mark.parametrize("limit", [0, 57, True])
def test_invalid_bound_is_not_silently_changed(limit):
    with pytest.raises(ValueError): project(make_job(), max_modules=limit)
