"""v8 optional formal-text review: deterministic fixtures, no live calls."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from shengbo_contracts.common import canonical_hash, canonical_json
from shengbo_prelive.script_review_workset import script_review_workset
from shengbo_prelive.workflows import ScriptFlowInput, scripts


def ref(kind, identity, body=None):
    return {"kind": kind, "id": identity, "revision": 1,
        "content_hash": canonical_hash(body if body is not None else [kind, identity])}


def case():
    product = ref("product", "selected-product")
    plan = ref("script_plan", "plan")
    run = {"id": "this-run", "goal": {"action_target_refs": [product], "target_product_ids": [product["id"]]}}
    documents, items = {}, []
    for index in range(2):
        modules = {key: {"module_key": key, "parent_ref": None,
            "sentences": [{"id": f"sentence-{index}-{key}", "text": str(index) + key + "完整原文。" * 125,
                "spoken_text": str(index) + key + "完整原文。" * 125,
                "lang": "zh-CN", "future_semantic_field": {"safe": True}}]}
            for key in ("product_intro", "selling_points")}
        module_refs = {key: ref("module", f"module-{index}-{key}", body) for key, body in modules.items()}
        module_refs["campaign"] = None
        body = {"preparation_id": "prep", "product_ref": product, "product_id": product["id"],
            "variant_id": f"variant-{index}", "version_number": index + 1, "script_revision": 1,
            "module_refs": module_refs, "generation_provenance": {"job_id": "job"}}
        script_ref = ref("script_revision", f"script-{index}", body)
        documents[script_ref["id"]] = {"ref": script_ref, "body": body, "modules": modules}
        items.append({"item_id": f"item-{index}", "status": "succeeded", "call_state": "result_received",
            "input": {key: body[key] for key in ("product_ref", "variant_id", "version_number")},
            "output": {"script_ref": script_ref},
            "provider_attempts": [{"state": "response_received", "output": "ARCHIVED_PROVIDER_CANARY"}]})
    job = {"ref": ref("prelive_job", "job"), "body": {"run_id": run["id"], "kind": "scripts",
        "preparation_id": "prep", "status": "succeeded", "generation_epoch": 2,
        "plan_ref": plan, "created_at": "2026-10-20", "items": items}}
    prep = {"ref": ref("preparation", "prep"), "body": {"generation_epoch": 2, "plan_ref": plan,
        "script_refs": [item["output"]["script_ref"] for item in items] + [ref("script_revision", "unrelated-prep-script")]}}
    observation = {"jobs": [job], "pending_job_ids": [], "unknown_job_ids": []}
    calls = []
    async def query(key, args):
        calls.append((key, deepcopy(args)))
        assert key == "read_script", "review must only use the formal business reader"
        return deepcopy(documents[args["ref"]["id"]])
    def counts(rows):
        return {"unknown_item_count": sum(row.get("call_state") == "timeout_unknown" for row in rows),
            "unknown_provider_attempt_count": 0, "in_flight_provider_attempt_count": 0}
    f = SimpleNamespace(query=query, engine=SimpleNamespace(_job_item_counts=counts))
    return SimpleNamespace(f=f, run=run, prep=prep, observation=observation, job=job, documents=documents, calls=calls)


@pytest.mark.asyncio
async def test_two_formal_scripts_all_selected_text_once_in_original_order_with_exact_readers():
    c = case()
    before = deepcopy((c.run, c.prep, c.observation, c.documents))
    result = await script_review_workset(c.f, c.run, c.prep, c.observation)
    assert result["status"] == "COMPLETED" and result["review_content_complete"] is True
    assert not result["blockers"] and not result["remaining"]
    assert result["content_quality_passed"] is result["approval_granted"] is result["automatic_execution"] is False
    assert [item["target"]["variant_id"] for item in result["items"]] == ["variant-0", "variant-1"]
    for item in result["items"]:
        assert item["reader"] == {"tool": "read_script", "arguments": {"ref": item["script_ref"]}}
        assert [module["module_key"] for module in item["modules"]] == ["product_intro", "selling_points"]
        for module in item["modules"]:
            original = c.documents[item["script_ref"]["id"]]["modules"][module["module_key"]]["sentences"][0]
            delivered = module["sentences"][0]
            assert delivered["text"] == original["text"] and delivered["future_semantic_field"] == original["future_semantic_field"]
            assert delivered["spoken_text_equals_text"] is True and "spoken_text" not in delivered
            assert canonical_json(result).count(original["text"]) == 1
    assert len(c.calls) == 2 and "ARCHIVED_PROVIDER_CANARY" not in canonical_json(result)
    assert "unrelated-prep-script" not in canonical_json(result)
    assert before == (c.run, c.prep, c.observation, c.documents)


@pytest.mark.asyncio
async def test_default_chooses_latest_owned_job_over_newer_historical_job():
    c = case()
    historical = deepcopy(c.job)
    historical["ref"]["id"] = "history"
    historical["body"].update(run_id="old-run", created_at="2099")
    c.observation["jobs"].append(historical)
    result = await script_review_workset(c.f, c.run, c.prep, c.observation)
    assert result["job_ref"] == c.job["ref"] and result["status"] == "COMPLETED"
    c.calls.clear()
    historical_result = await script_review_workset(c.f, c.run, c.prep, c.observation, job_id="history")
    assert historical_result["status"] == "NEEDS_REPLAN" and not historical_result["review_content_complete"]
    assert not c.calls and historical_result["blockers"][0]["code"] == "SCRIPT_REVIEW_JOB_SCOPE_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["other_prep", "generation_epoch", "plan", "target", "stale", "reader", "module"])
async def test_scope_stale_and_formal_reader_mismatches_are_explicit_blockers(change):
    c = case()
    if change == "other_prep": c.job["body"]["preparation_id"] = "another-prep"
    if change == "generation_epoch": c.prep["body"]["generation_epoch"] += 1
    if change == "plan": c.prep["body"]["plan_ref"] = ref("script_plan", "another-plan")
    if change == "target": c.run["goal"]["action_target_refs"] = [ref("product", "another-product")]
    if change == "stale": c.prep["body"]["script_refs"] = []
    if change == "reader": c.documents["script-0"]["body"]["variant_id"] = "wrong"
    if change == "module": c.documents["script-0"]["modules"]["selling_points"]["sentences"][0]["text"] = "changed"
    result = await script_review_workset(c.f, c.run, c.prep, c.observation)
    assert result["status"] == "NEEDS_REPLAN" and result["blockers"]
    assert result["review_content_complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "expected"), [("running", "WAITING"), ("failed", "NEEDS_REPLAN"), ("unknown", "OUTCOME_UNKNOWN")])
async def test_pending_failed_unknown_never_claim_complete(state, expected):
    c = case()
    c.job["body"]["status"] = "running" if state == "unknown" else state
    c.job["body"]["items"][1]["status"] = "failed" if state == "failed" else "running"
    if state == "unknown": c.job["body"]["items"][1]["call_state"] = "timeout_unknown"
    result = await script_review_workset(c.f, c.run, c.prep, c.observation)
    assert result["status"] == expected and not result["review_content_complete"] and result["blockers"]
    assert [row["status"] for row in result["items"]] == ["succeeded", c.job["body"]["items"][1]["status"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [{"max_scripts": 1}, {"max_content_chars": 1}])
async def test_budget_returns_unread_exact_readers_without_truncation(budget):
    c = case()
    result = await script_review_workset(c.f, c.run, c.prep, c.observation, **budget)
    assert result["status"] == "NEEDS_REPLAN" and not result["review_content_complete"]
    assert result["remaining"] and all(row["tool"] == "read_script" and row["arguments"]["ref"] for row in result["remaining"])
    assert any(item["review_status"] == "unread_budget" and "modules" not in item for item in result["items"])


@pytest.mark.asyncio
async def test_missing_explicit_job_returns_reader_and_never_selects_another_job():
    c = case()
    result = await script_review_workset(c.f, c.run, c.prep, c.observation, job_id="not-observed")
    assert result["status"] == "NEEDS_REPLAN" and not result["items"] and not c.calls
    assert result["remaining"] == [{"tool": "read_job", "arguments": {"job_id": "not-observed"}}]


@pytest.mark.asyncio
async def test_schema_and_workflow_review_entry_leave_execute_status_retry_optional():
    c = case()
    for action in ("execute", "status", "retry", "review"):
        assert ScriptFlowInput(action=action).action == action
    c.f.scope = SimpleNamespace(run_id=c.run["id"])
    c.f.engine.store = SimpleNamespace(get=lambda *args: c.run)
    async def prep(): return c.prep
    async def step(name, callback): return await callback()
    async def observation(run): return c.observation
    c.f.prep, c.f.step = prep, step
    c.f.engine._completion_observation = observation
    c.f.result = lambda status, **kwargs: {"status": status, **kwargs}
    result = await scripts(c.f, {"action": "review"})
    assert result["status"] == "COMPLETED" and result["result"]["review_content_complete"]
    assert len(c.calls) == 2
