"""Public rejected responses remain readable without becoming formal modules."""
from copy import deepcopy
import json

import pytest

from shengbo_contracts.common import ContractError, canonical_hash, canonical_json
from shengbo_prelive.models import RunScope
from shengbo_prelive.workflows import run_workflow
from test_provider_output_convergence import convergence, ref, tokens


def rejected_job(case, *, content=None, hidden_item=False):
    content = content if content is not None else json.dumps({"module_key": "cta", "sentences": ["喜欢这款就选好颜色下单。"]}, ensure_ascii=False)
    job = case.client.job
    body = job["body"]
    body.update(status="failed", cancel_requested=False, plan_ref=ref("script_plan", "plan-current", {"plan": True}))
    product = ref("product", "product-current", {"price": 129})
    item = {"item_id": "variant-rejected", "status": "failed", "call_state": "result_received", "attempt": 1,
        "input": {"product_ref": product, "variant_id": "v2", "version_number": 2, "private_input": "UNNEEDED_INPUT" * 500},
        "output": None, "error": {"code": "GENERATED_MODULE_NEEDS_REVIEW", "details": {"issues": ["促单缺不可叠加条件"]}},
        "module_results": {}, "provider_attempts": []}
    for number, key in enumerate(("opening_short", "product_intro", "selling_points", "campaign")):
        formal = ref("module", "module-" + key, {"key": key})
        item["module_results"][key] = {"status": "committed", "module_ref": formal}
        item["provider_attempts"].append({"attempt_id": "success-" + str(number), "purpose": "copywriting." + key,
            "state": "result_received", "validation_state": "accepted", "committed_module_ref": formal,
            "output": {"sentences": [key + "已保存"]}, "usage": {"input_tokens": 10, "output_tokens": 2}, "usage_state": "partial"})
    receipt = ref("provider_response", "response-rejected-cta", {"content": content})
    item["provider_attempts"].append({"attempt_id": "rejected-cta", "purpose": "copywriting.cta", "state": "response_received",
        "received_at": "2026-10-17T15:00:00+00:00", "validation_state": "rejected",
        "validation_error": deepcopy(item["error"]), "provider_response_ref": receipt,
        "usage": {"input_tokens": 100, "output_tokens": 12}, "usage_state": "partial",
        "public_response": {"content": content, "module_key": "cta", "expected_module_key": "cta", "receipt_ref": receipt,
            "state": "response_received", "validation_state": "rejected", "usage": {"prompt_tokens": 100, "completion_tokens": 12},
            "usage_state": "partial", "is_formal_module": False, "reusable": False, "approvable": False}})
    prefix = [{"item_id": "prefix-" + str(n), "status": "succeeded", "call_state": "result_received",
        "output": {"script_ref": ref("script_revision", "script-" + str(n), {"n": n})}, "provider_attempts": []} for n in range(7)]
    body["items"] = [*prefix, item] if hidden_item else [item]
    job["ref"] = ref("prelive_job", body["id"], body, revision=31)
    return item, content, receipt


def failed_item(view):
    return next(item for item in [*view["body"]["items"], *view["attention_items"]] if item["item_id"] == "variant-rejected")


@pytest.mark.asyncio
async def test_rejected_fifth_attempt_is_first_readable_and_four_formal_modules_stay_exact(convergence):
    case = convergence
    item, content, receipt = rejected_job(case)
    original = deepcopy(case.client.job)
    view = await case.engine.business_query(case.scope, "read_job", {"job_id": case.client.job["ref"]["id"]})
    row = failed_item(view)
    rejected = row["provider_attempts"][0]
    assert rejected["attempt_id"] == "rejected-cta"
    assert row["provider_attempts_projection"]["original_positions"][0] == 4
    assert rejected["state"] == "response_received" and rejected["validation_state"] == "rejected"
    assert rejected["usage_state"] == "partial" and rejected["usage"] == item["provider_attempts"][-1]["usage"]
    assert rejected["public_response"]["content"] == content
    assert rejected["provider_response_ref"] == receipt
    assert rejected["result_ref"]["kind"] == "runtime_context" and rejected["result_ref"] != receipt
    assert rejected["result_path"] == "/output/content"
    assert rejected["can_reuse_as_formal_module"] is False and rejected["formal_module_ref"] is None
    assert rejected["validation_error"]["details"]["issues"] == ["促单缺不可叠加条件"]
    assert row["target"]["variant_id"] == "v2" and row["target"]["product_ref"] == item["input"]["product_ref"]
    assert row["committed_modules"] == item["module_results"]
    source = case.engine.run_history(case.scope, {"ref": rejected["result_ref"], "path": rejected["result_path"]})
    assert source["document"] == content
    assert case.client.job == original and not case.client.commands
    assert not case.sent and case.credentials.calls == 0
    assert "UNNEEDED_INPUT" not in canonical_json(view)


@pytest.mark.asyncio
async def test_failure_item_after_successful_prefix_is_visible_without_breaking_original_pagination(convergence):
    case = convergence
    item, content, _ = rejected_job(case, hidden_item=True)
    view = await case.engine.business_query(case.scope, "read_job", {"job_id": case.client.job["ref"]["id"]})
    assert view["items_projection"]["next_cursor"] == len(view["body"]["items"])
    row = failed_item(view)
    assert row in view["attention_items"]
    assert row["path"] == "/job/body/items/7"
    assert row["provider_attempts"][0]["public_response"]["content"] == content
    assert len(row["committed_modules"]) == 4
    assert view["items_projection"]["status_counts"] == {"succeeded": 7, "failed": 1}
    assert tokens(view) < 7000
    exact = case.engine.run_history(case.scope, {"ref": view["full_ref"], "path": row["path"], "full": False, "max_chars": 24000})
    assert exact["document"]["item_id"] == item["item_id"]


@pytest.mark.asyncio
async def test_long_public_response_is_exact_scoped_snapshot_reader_not_business_archive_ref(convergence):
    case = convergence
    _, content, receipt = rejected_job(case, content="公开CTA内容，保留优惠不可叠加条件。" * 900)
    view = await case.engine.business_query(case.scope, "read_job", {"job_id": case.client.job["ref"]["id"]})
    attempt = failed_item(view)["provider_attempts"][0]
    public = attempt["public_response"]
    assert "content" not in public and public["content_omitted"] is True
    assert public["content_hash"] == canonical_hash(content)
    assert public["reader"] == attempt["reader"] == {"tool": "read_run_history", "ref": attempt["result_ref"], "path": "/output/content"}
    assert content not in canonical_json(view)
    cursor, chunks = 0, []
    while True:
        page = case.engine.run_history(case.scope, {"ref": attempt["result_ref"], "path": "/output/content", "cursor": cursor, "max_chars": 1000})
        chunks.append(page["document"]["text"])
        cursor = page["selection"]["next_cursor"]
        if cursor is None:
            break
    assert "".join(chunks) == content
    full = case.engine.run_history(case.scope, {"ref": attempt["result_ref"], "full": True})["document"]
    assert full["output"]["content"] == content and full["provider_response_ref"] == receipt
    assert full["evidence_only"] is True and full["can_reuse_as_formal_module"] is False
    other = case.engine.create_conversation({"scope": {"kind": "catalog"}})
    foreign = case.engine.store.put("run", {**case.run, "id": "foreign-public-response", "conversation_id": other["id"]})
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        case.engine.run_history(RunScope(foreign["id"], "foreign", "execute"), {"ref": attempt["result_ref"], "path": "/output/content"})
    with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
        case.engine.run_history(case.scope, {"ref": {**attempt["result_ref"], "content_hash": canonical_hash("tampered")}, "path": "/output/content"})
    with pytest.raises(ContractError, match="HISTORY_REF_INVALID"):
        case.engine.run_history(case.scope, {"ref": receipt, "path": "/output/content"})


@pytest.mark.asyncio
async def test_static_job_projection_never_reinjects_public_content_or_exposes_unknown_raw(convergence):
    case = convergence
    item, content, _ = rejected_job(case, content="仅在显式公开响应读取时展示" * 1000)
    view = case.engine._job_view(case.run, case.client.job, include_provider_outputs=False)
    wire = canonical_json(view)
    assert content not in wire and "public_response" not in failed_item(view)["provider_attempts"][0]
    assert not any("result_ref" in attempt for attempt in failed_item(view)["provider_attempts"])
    failed = item["provider_attempts"][-1]
    failed.update(state="outcome_unknown", validation_state="unknown", validation_error=None, usage_state="unknown")
    projected = case.engine._provider_attempt_view(case.run, case.client.job, item, failed)
    assert projected["state"] == "outcome_unknown" and projected["usage_state"] == "unknown"
    assert "result_ref" not in projected and "public_response" not in projected
    assert projected["provider_response_ref"] == failed["provider_response_ref"]
    failed.update(state="response_received", validation_state="rejected", output={"unverified_raw": "must-not-project"})
    failed.pop("public_response")
    projected = case.engine._provider_attempt_view(case.run, case.client.job, item, failed)
    assert "result_ref" not in projected and "must-not-project" not in canonical_json(projected)


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_in_observation,state,expected", [
    (True, "response_received", "NEEDS_REPLAN"),
    (False, "response_received", "NEEDS_REPLAN"),
    (False, "outcome_unknown", "OUTCOME_UNKNOWN"),
    (False, "sent", "WAITING"),
])
async def test_f04_status_fetches_public_failure_once_and_returns_replan_without_generation(convergence, visible_in_observation, state, expected):
    case = convergence
    rejected_job(case)
    job = case.client.job
    if state != "response_received":
        job["body"]["items"][0]["provider_attempts"][-1].update(state=state, validation_state="unknown", validation_error=None)
    prep = {"ref": ref("preparation", case.run["scope"]["preparation_id"], {"revision": 1}),
        "body": {"id": case.run["scope"]["preparation_id"], "selected_product_refs": [], "plan_ref": job["body"]["plan_ref"],
            "bundle_ref": job["body"]["context_ref"], "generation_epoch": 3}}
    async def query(key, args=None, **kwargs):
        case.client.queries.append((key, deepcopy(args), deepcopy(kwargs)))
        if key == "read_preparation":
            return deepcopy(prep)
        if key == "read_completion_observation":
            return {"preparation": deepcopy(prep), "jobs": [deepcopy(job)] if visible_in_observation else [], "pending_job_ids": [], "unknown_job_ids": []}
        assert key == "read_job"
        return deepcopy(job)
    case.client.query = query
    result = await run_workflow(case.engine, case.scope, "wf_script_execution", {"action": "status", "job_id": job["ref"]["id"]})
    assert result["status"] == expected
    existing = result["result"]["existing_job"]
    assert "rejected-cta" in {attempt["attempt_id"] for attempt in failed_item(existing)["provider_attempts"]} or state == "sent"
    assert len(failed_item(existing)["committed_modules"]) == 4
    assert sum(key == "read_job" for key, _, _ in case.client.queries) == 1
    assert not case.client.commands and not case.sent
    assert "不自动" in result["remaining_steps"][0] or "由主Agent" in result["remaining_steps"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [
    ("content", {"PRIVATE_DIAGNOSTIC_SENTINEL": "not a public string"}),
    ("module_key", "PRIVATE_DIAGNOSTIC_SENTINEL"),
    ("usage", {"private_token": "PRIVATE_DIAGNOSTIC_SENTINEL"}),
    ("usage", {"prompt_tokens": True}),
    ("reusable", True),
    ("is_formal_module", True),
    ("private_extra", "PRIVATE_DIAGNOSTIC_SENTINEL"),
])
async def test_invalid_public_dto_never_leaks_validation_input_or_falls_back_to_raw_output(convergence, key, value):
    case = convergence
    item, _, _ = rejected_job(case)
    attempt = item["provider_attempts"][-1]
    attempt["public_response"][key] = value
    attempt["output"] = {"content": "PRIVATE_DIAGNOSTIC_SENTINEL"}
    attempt["state"] = "result_received"
    projected = case.engine._provider_attempt_view(case.run, case.client.job, item, attempt)
    assert projected["public_response_error"]["code"] == "PUBLIC_PROVIDER_RESPONSE_INVALID"
    assert "PRIVATE_DIAGNOSTIC_SENTINEL" not in canonical_json(projected)
    assert "result_ref" not in projected and "public_response" not in projected
    assert projected["state"] == "result_received" and projected["usage_state"] == "partial"
    assert not case.sent and not case.client.commands


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_status,expected", [(None, "NEEDS_REPLAN"), ("succeeded", "COMPLETED"), ("queued", "WAITING")])
async def test_f04_requires_actual_script_success_instead_of_empty_observation(convergence, actual_status, expected):
    case = convergence
    job = deepcopy(case.client.job)
    job["body"].update(status=actual_status, items=[])
    prep = {"ref": ref("preparation", case.run["scope"]["preparation_id"], {"revision": 1}), "body": {}}
    async def query(key, args=None, **kwargs):
        if key == "read_preparation":
            return deepcopy(prep)
        if key == "read_completion_observation":
            return {"preparation": deepcopy(prep), "jobs": [], "pending_job_ids": [], "unknown_job_ids": []}
        assert key == "read_job"
        return deepcopy(job)
    case.client.query = query
    result = await run_workflow(case.engine, case.scope, "wf_script_execution", {"action": "status", "job_id": job["ref"]["id"]})
    assert result["status"] == expected
    assert not case.client.commands and not case.sent
