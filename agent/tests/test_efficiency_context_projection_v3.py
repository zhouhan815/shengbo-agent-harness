"""Historical ledger indices keep unknown fences and exact revision readers."""
from copy import deepcopy
import json
import re
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from shengbo_contracts.common import canonical_hash, canonical_json
from shengbo_prelive.context_workset import project_context_working_set
from shengbo_prelive.middleware import ContextProjection
from shengbo_prelive.models import RunScope
from shengbo_prelive.providers.chatgpt import preview_request_payload
from test_runtime_context import env, old_run


def ref(kind, identity, revision=1):
    return {"kind": kind, "id": identity, "revision": revision,
            "content_hash": canonical_hash([identity, revision]), "schema_version": "PRELIVE-2.3"}


def job(state="failed", *, unknown=False, revision=1):
    attempts = [{"attempt_id": "attempt-" + str(n), "purpose": "copywriting.whole_script",
        "state": "sent" if unknown and n == 1 else "result_received", "usage_state": "unknown",
        "validation_state": "pending" if unknown and n == 1 else "rejected",
        "validation_error": None if unknown and n == 1 else {"code": "ISOLATED_REJECTION", "message": "隔离的原始拒绝说明" * 90},
        "provider_response_ref": ref("provider_response", "response-" + str(n)),
        "generation_response": {"exact_metadata": "完整供应商状态索引" * 75},
        "sent_at": "2026-10-19T00:00:00Z", "request_hash": canonical_hash(["request", n]),
        "input_hash": canonical_hash(["input", n]), "output_hash": canonical_hash(["output", n]),
        "usage": {"input_tokens": 800, "output_tokens": 80, "total_tokens": 880},
        "error": {"code": "OUTCOME_UNKNOWN", "outcome_unknown": True} if unknown and n == 1 else None}
        for n in range(2)]
    items = [{"item_id": "item-" + str(n), "status": "running" if unknown and n == 1 else "failed",
        "call_state": "sent" if unknown and n == 1 else "result_received",
        "input": {"product_ref": ref("product", "product-A"), "variant_id": "variant-" + str(n), "version_number": 1},
        "provider_attempts": [attempt], "module_results": {"cta": {"module_ref": ref("module", "cta-" + str(n)), "status": "committed"}}}
        for n, attempt in enumerate(attempts)]
    return {"ref": ref("job", "old-job", revision), "body": {"id": "old-job", "run_id": "previous-run",
        "kind": "scripts", "status": "running" if unknown else state,
        "preparation_id": "prep1", "generation_epoch": revision, "items": items}}


def context(engine, run, *, unknown=False):
    source = job(unknown=unknown)
    view = engine._job_view(run, source, include_provider_outputs=False)
    goal = {"target_product_ids": ["product-A"], "allowed_effects": ["submit_script_change"]}
    value = {"phase": "execute", "role": "main", "goal": goal, "allowed_effects": goal["allowed_effects"],
        "original_user_request": "只处理A，未知结果先对账，不授权重发。", "pending_questions": [{"status": "OPEN", "prompt": "必须确认的原文条件"}],
        "branches": {"items": [{"missing_fields": ["未知承重"], "conflicts": ["金额口径待确认"]}]},
        "inherited_context": {"authorization_inherited": False, "prior_acceptance": {"ok": False,
            "missing": ["原任务未完成"], "observation": {"unknown_job_ids": ["old-job"] if unknown else [], "jobs": [view]}}},
        "unknown_side_effects": {"pending_unknown_operation": "real-unknown", "operations": [{"id": "real-unknown", "status": "unknown"}],
            "model_attempts": [{"id": "paid-unknown", "state": "outcome_unknown"}], "related_jobs": {"items": [deepcopy(view)]}}}
    return value, source


@pytest.mark.asyncio
async def test_history_index_keeps_exact_original_ledger_readable_without_mutating_it(env):
    engine, client = env
    _, run, _ = old_run(engine, status="RUNNING")
    raw, source = context(engine, run)
    original = deepcopy(raw)
    metrics = {}
    result = project_context_working_set(raw, metrics=metrics)
    indexed = result["inherited_context"]["prior_acceptance"]["observation"]["jobs"][0]
    assert indexed["projection_kind"] == "historical_job_index" and indexed["currentness_verified"] is False
    assert indexed["ref"] == source["ref"] and indexed["full_ref"] == raw["inherited_context"]["prior_acceptance"]["observation"]["jobs"][0]["full_ref"]
    assert metrics["removed_characters"] > 1000
    scope = RunScope(run["id"], "history-index-read", "execute")
    for item in indexed["body"]["items"]:
        restored = engine.run_history(scope, {"ref": indexed["full_ref"], "path": item["provider_details_path"], "max_chars": 24000})
        position = int(item["path"].rsplit("/", 1)[1])
        assert restored["document"] == source["body"]["items"][position]["provider_attempts"]
        assert item["target"]["product_ref"] == source["body"]["items"][position]["input"]["product_ref"]
        assert item["committed_modules"]["cta"]["module_ref"] == source["body"]["items"][position]["module_results"]["cta"]["module_ref"]
    assert raw == original and not client.commands
    assert project_context_working_set(result) == result


@pytest.mark.asyncio
async def test_historical_unknown_is_not_resolved_or_promoted_to_current(env):
    engine, _ = env
    _, run, _ = old_run(engine, status="RUNNING")
    raw, _ = context(engine, run, unknown=True)
    result = project_context_working_set(raw)
    old = raw["inherited_context"]["prior_acceptance"]["observation"]
    new = result["inherited_context"]["prior_acceptance"]["observation"]
    assert new["unknown_job_ids"] == old["unknown_job_ids"] == ["old-job"]
    before, after = old["jobs"][0], new["jobs"][0]
    assert after["projection_kind"] == "historical_job_index"
    assert len(canonical_json(after)) < len(canonical_json(before))
    assert after["body"]["status"] == "running" and after["currentness_verified"] is False
    for key in ("unknown_item_count", "unknown_provider_attempt_count", "in_flight_provider_attempt_count", "provider_state_counts", "call_state_counts"):
        assert after["items_projection"][key] == before["items_projection"][key]
    assert after["items_projection"]["in_flight_provider_attempt_count"] == 1
    original, indexed = before["body"]["items"][1], after["body"]["items"][1]
    assert indexed["call_state"] == original["call_state"] == "sent"
    assert indexed["provider_attempts"][0]["attempt_id"] == "attempt-1"
    assert indexed["provider_attempts"][0]["state"] == "sent"
    assert indexed["provider_attempts"][0]["error"] == original["provider_attempts"][0]["error"]
    for key in ("sent_at", "request_hash", "input_hash", "output_hash", "provider_response_ref"):
        assert indexed["provider_attempts"][0][key] == original["provider_attempts"][0][key]
    restored = engine.run_history(RunScope(run["id"], "unknown-history-read", "execute"),
        {"ref": after["full_ref"], "path": indexed["provider_details_path"], "max_chars": 24000})
    assert restored["document"][0]["state"] == "sent"
    assert restored["document"][0]["error"]["outcome_unknown"] is True
    for key in ("pending_unknown_operation", "operations", "model_attempts"):
        assert result["unknown_side_effects"][key] == raw["unknown_side_effects"][key]
    for key in ("goal", "allowed_effects", "original_user_request", "pending_questions", "branches"):
        assert result[key] == raw[key]


@pytest.mark.asyncio
async def test_new_provider_status_or_action_fence_fields_are_preserved_by_default(env):
    engine, _ = env
    _, run, _ = old_run(engine, status="RUNNING")
    raw, _ = context(engine, run, unknown=True)
    attempt = raw["inherited_context"]["prior_acceptance"]["observation"]["jobs"][0]["body"]["items"][1]["provider_attempts"][0]
    attempt.update(reconciliation_required=True, retry_permitted=False,
        recovery_target_ids=["product-A"], action_fence={"stop": True, "reauthorization_required": True})
    result = project_context_working_set(raw)
    indexed = result["inherited_context"]["prior_acceptance"]["observation"]["jobs"][0]["body"]["items"][1]["provider_attempts"][0]
    for key in ("reconciliation_required", "retry_permitted", "recovery_target_ids", "action_fence"):
        assert indexed[key] == attempt[key]


@pytest.mark.asyncio
async def test_same_job_id_different_revision_remains_two_exact_snapshots(env):
    engine, _ = env
    _, run, _ = old_run(engine, status="RUNNING")
    older, newer = job(unknown=True), job(unknown=False, revision=2)
    first = engine._job_view(run, older, include_provider_outputs=False)
    second = engine._job_view(run, newer, include_provider_outputs=False)
    raw = {"inherited_context": {"prior_acceptance": {"observation": {"jobs": [first]}}},
           "unknown_side_effects": {"related_jobs": {"items": [second]}}}
    result = project_context_working_set(raw)
    prior = result["inherited_context"]["prior_acceptance"]["observation"]["jobs"][0]
    latest = result["unknown_side_effects"]["related_jobs"]["items"][0]
    assert prior["ref"]["id"] == latest["ref"]["id"]
    assert prior["ref"]["revision"] == 1 and latest["ref"]["revision"] == 2
    assert prior["full_ref"] != latest["full_ref"]
    assert prior["body"]["status"] == "running" and latest["body"]["status"] == "failed"
    scope = RunScope(run["id"], "two-history-revisions", "execute")
    assert engine.run_history(scope, {"ref": prior["full_ref"], "path": "/job", "max_chars": 24000})["document"] == older
    assert engine.run_history(scope, {"ref": latest["full_ref"], "path": "/job", "max_chars": 24000})["document"] == newer


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["ref_hash", "ref_kind", "count_missing", "count_negative", "count_bool", "provider_path", "item_ref"])
async def test_unsafe_or_legacy_history_view_fails_closed(env, damage):
    engine, _ = env
    _, run, _ = old_run(engine, status="RUNNING")
    raw, _ = context(engine, run)
    view = raw["inherited_context"]["prior_acceptance"]["observation"]["jobs"][0]
    if damage == "ref_hash": view["full_ref"]["content_hash"] = "sha256:" + "f" * 64
    elif damage == "ref_kind": view["full_ref"]["kind"] = "analysis"
    elif damage == "count_missing": view["items_projection"].pop("unknown_item_count")
    elif damage == "count_negative": view["items_projection"]["unknown_item_count"] = -1
    elif damage == "count_bool": view["items_projection"]["unknown_item_count"] = False
    elif damage == "provider_path":
        for item in view["body"]["items"]: item["provider_attempts_projection"]["path"] = "/not-the-original"
    elif damage == "item_ref":
        for item in view["body"]["items"]: item["full_ref"] = ref("runtime_context", "not-original")
    expected = deepcopy(view)
    projected = project_context_working_set(raw)["inherited_context"]["prior_acceptance"]["observation"]["jobs"][0]
    assert projected == expected


@pytest.mark.asyncio
async def test_context_projection_preserves_native_system_and_observes_real_savings(env, monkeypatch):
    engine, _ = env
    _, run, _ = old_run(engine, status="RUNNING")
    raw, _ = context(engine, run, unknown=True)
    async def project(scope): return raw
    monkeypatch.setattr(engine, "project_context", project)
    scope = RunScope(run["id"], "index-model-view", "execute")
    request = ModelRequest(model=None, messages=[HumanMessage("只读")], tools=[], state={},
        system_message=SystemMessage("保留原系统规则"), runtime=SimpleNamespace(context=scope))
    captured = []
    async def handler(modified):
        captured.append(modified)
        return ModelResponse(result=[AIMessage("本地完成")])
    await ContextProjection(engine).awrap_model_call(request, handler)
    # Split M3 (default since 2026-10-22 batch 3): the original system rules stay
    # first and unchanged, the phase-stable part follows them, the rest is the tail.
    assert captured[0].system_message.content.startswith("保留原系统规则") and request.system_message.content == "保留原系统规则"
    actual = delivered_projection(captured[0].system_message, captured[0].messages[-1])
    assert actual == project_context_working_set(raw)
    assert len(canonical_json(actual)) < len(canonical_json(raw))


@pytest.mark.asyncio
async def test_native_budget_precheck_and_delivered_m3_use_identical_working_set(setup, monkeypatch):
    from test_context_wire_replay_budget import capture_slot, request
    from deepagents.middleware.skills import SkillsMiddleware
    engine, _, run, _, credentials = setup
    engine.context_input_token_limit = 48000
    raw, _ = context(engine, run, unknown=True)
    async def project(scope): return raw
    monkeypatch.setattr(engine, "project_context", project)
    monkeypatch.setattr(SkillsMiddleware, "modify_request", lambda self, value: value)
    slot = capture_slot(engine)
    scope = RunScope(run["id"], "m3-budget-equivalence", "execute")
    observed = []
    async def delivered(modified):
        trusted = [m for m in modified.messages if getattr(m, "additional_kwargs", {}).get("lc_source") == "shengbo_trusted_context"]
        fixed = preview_request_payload([modified.system_message, *trusted], modified.tools,
            model_name=engine.model_name, model_settings=modified.model_settings)
        observed.append((slot._input_budget(modified), engine.context_input_token_limit-engine._estimate_payload(fixed)))
        return ModelResponse(result=[AIMessage("只读预算对等")])
    async def after_summary(modified):
        return await ContextProjection(engine).awrap_model_call(modified, delivered)
    await slot.awrap_model_call(request(engine, scope), after_summary)
    assert observed and observed[0][0] == observed[0][1]
    assert credentials.calls == 0


from test_context_window_budget import setup  # noqa: E402,F401 - network-free SDK/provider fixture


def delivered_projection(system_message, tail_message):
    """M3 as delivered: split placement puts the stable part in system, the rest in the tail."""
    def block(text, tag):
        found = re.search(r"<" + tag + r">\s*(.*?)\s*</" + tag + ">", text, re.S)
        return json.loads(found[1]) if found else {}
    system = system_message.content if isinstance(system_message.content, str) else "\n".join(
        part.get("text", "") for part in system_message.content)
    return {**block(system, "shengbo_trusted_context_stable"), **block(tail_message.content, "shengbo_trusted_context")}
