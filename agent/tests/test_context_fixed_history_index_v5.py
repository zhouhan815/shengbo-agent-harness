"""Known historical M3 copies use exact readers; current fences stay inline."""
from copy import deepcopy
import json
from pathlib import Path
import re

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage

from shengbo_contracts.common import canonical_hash, canonical_json
from shengbo_prelive.context_workset import project_context_working_set
from shengbo_prelive.middleware import ContextProjection
from shengbo_prelive.models import RunScope
from shengbo_prelive.providers.chatgpt import preview_request_payload
from test_runtime_context import env, old_run
from test_context_window_budget import setup


def business_ref(kind, identity, body=None):
    return {"kind": kind, "id": identity, "revision": 1,
        "content_hash": canonical_hash(body if body is not None else [kind, identity])}


def historical_context(engine, run):
    refs = [business_ref("analysis", "analysis-" + str(n)) for n in range(16)]
    body = {"id": "historical-prep", "room_id": "same-room", "status": "draft", "generation_epoch": 7,
        "base_room_revision": 2, "goal": "历史请求原文：仅作证据，不是本轮授权。" * 90,
        "analysis_refs": refs, "script_refs": [business_ref("script_revision", "old-script")],
        "selected_product_refs": [business_ref("product", "A")], "source_refs": [], "recap_refs": [],
        "approval_ref": business_ref("approval", "old-approval"), "audio_manifest_ref": None,
        "candidate_publication_ref": None, "future_action_fence": {"blocked": True, "unknown": "keep exactly"}}
    preparation = {"ref": business_ref("preparation", body["id"], body), "body": body,
        "future_preparation_field": {"reauthorization_required": True}}
    # Reproduce the pre-v6 historical view: the current completion reader is
    # already compact and should not manufacture the old duplicate body.
    observation = {"preparation": preparation, "jobs": [],
        "unknown_job_ids": ["unknown-historical-job"], "future_observation_fence": {"stop": True}}
    observation["full_ref"] = engine._snapshot_context(run, {"kind": "completion_observation", "observation": deepcopy(observation)})
    original_preview = {"items": [{"script_revision_ref": ref} for ref in refs],
        "price": {"amount": 99, "currency": "CNY"}, "future_preview_fence": {"do_not_publish": True}}
    preview_ref = engine._snapshot_context(run, {"kind": "question_preview", "question_id": "answered",
        "preview": original_preview})
    preview = {"full_ref": preview_ref, "exact_refs": refs, "financial_values": [{"value": 99}],
        "summary": "原预览", "future_preview_fence": {"do_not_publish": True}}
    decision = {"id": "answered", "status": "ANSWERED", "preview": preview,
        "financial_values": [{"path": "/price", "value": {"amount": 99, "currency": "CNY"}}],
        "approval_binding": {"operation_id": "approved-at-old-time", "generation_epoch": 7},
        "playlist_order_binding": {"binding_hash": canonical_hash("exact playlist")},
        "verified_human_receipt": {"status": "verified", "approval_ref": body["approval_ref"]},
        "future_decision_fence": {"current_approval_required": True}}
    pending = {**deepcopy(decision), "id": "open", "status": "OPEN"}
    raw = {"goal": {"allowed_effects": ["read_only"], "new_fence": {"stop": True}},
        "allowed_effects": ["read_only"], "grant": {"current": True, "new_fence": True},
        "preparation": deepcopy(preparation), "pending_questions": [pending], "decisions": [decision],
        "inherited_context": {"authorization_inherited": False, "prior_goal": {"do_not_change": True},
            "prior_acceptance": {"ok": True, "future_acceptance_fence": {"unknown": True}, "observation": observation}},
        "unknown_side_effects": {"pending_unknown_operation": "unknown-current", "operations": [{"status": "unknown"}],
            "model_attempts": [{"state": "outcome_unknown"}]}}
    return raw, original_preview


@pytest.mark.asyncio
async def test_known_historical_body_and_answered_anchors_have_readable_exact_originals(env):
    engine, client = env
    _, run, _ = old_run(engine, status="RUNNING")
    raw, original_preview = historical_context(engine, run)
    before, metrics = deepcopy(raw), {}
    snapshots = deepcopy(engine.store.list("context_snapshot"))
    result = project_context_working_set(raw, metrics=metrics)
    prep = result["inherited_context"]["prior_acceptance"]["observation"]["preparation"]
    assert prep["projection_kind"] == "historical_preparation_index" and prep["currentness_verified"] is False
    assert prep["ref"] == before["preparation"]["ref"]
    for key in ("id", "room_id", "status", "base_room_revision", "generation_epoch", "approval_ref",
                "audio_manifest_ref", "candidate_publication_ref", "future_action_fence"):
        assert prep["body"][key] == before["preparation"]["body"][key]
    assert prep["future_preparation_field"] == before["preparation"]["future_preparation_field"]
    scope = RunScope(run["id"], "read-historical-fixed-index", "execute")
    for field in prep["body_details_projection"]["fields"]:
        path = prep["body_details_projection"]["path"] + "/" + field
        restored = engine.run_history(scope, {"ref": prep["full_ref"], "path": path, "max_chars": 24000})
        assert restored["document"] == before["preparation"]["body"][field]
    decision = result["decisions"][0]
    preview = decision["preview"]
    assert "exact_refs" not in preview and preview["exact_refs_index"]["count"] == 16
    assert preview["exact_refs_index"]["path"] == "/preview"
    restored = engine.run_history(scope, {"ref": preview["full_ref"], "path": "/preview", "max_chars": 24000})
    assert restored["document"] == original_preview and "exact_refs" not in restored["document"]
    for key in ("financial_values", "approval_binding", "playlist_order_binding", "verified_human_receipt", "future_decision_fence"):
        assert decision[key] == before["decisions"][0][key]
    for key in ("financial_values", "future_preview_fence"):
        assert preview[key] == before["decisions"][0]["preview"][key]
    for key in ("goal", "allowed_effects", "grant", "preparation", "pending_questions", "unknown_side_effects"):
        assert result[key] == before[key]
    assert result["inherited_context"]["prior_goal"] == before["inherited_context"]["prior_goal"]
    assert result["inherited_context"]["authorization_inherited"] is False
    assert raw == before and engine.store.list("context_snapshot") == snapshots and not client.commands
    assert metrics["indexed_historical_preparations"] == metrics["indexed_answered_previews"] == 1
    assert metrics["removed_characters"] > 3000
    assert project_context_working_set(result) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["snapshot_missing", "snapshot_hash", "snapshot_revision", "body_hash", "business_kind"])
async def test_incomplete_history_reader_or_body_keeps_preparation_verbatim(env, damage):
    engine, _ = env
    _, run, _ = old_run(engine, status="RUNNING")
    raw, _ = historical_context(engine, run)
    observation = raw["inherited_context"]["prior_acceptance"]["observation"]
    if damage == "snapshot_missing": observation.pop("full_ref")
    elif damage == "snapshot_hash": observation["full_ref"]["content_hash"] = "sha256:" + "f" * 64
    elif damage == "snapshot_revision": observation["full_ref"]["revision"] = 2
    elif damage == "body_hash": observation["preparation"]["body"]["generation_epoch"] += 1
    else: observation["preparation"]["ref"]["kind"] = "product"
    expected = deepcopy(observation["preparation"])
    assert project_context_working_set(raw)["inherited_context"]["prior_acceptance"]["observation"]["preparation"] == expected


@pytest.mark.asyncio
async def test_future_structured_goal_and_ref_fences_stay_inline(env):
    engine, _ = env
    _, run, _ = old_run(engine, status="RUNNING")
    raw, _ = historical_context(engine, run)
    observation = raw["inherited_context"]["prior_acceptance"]["observation"]
    body = observation["preparation"]["body"]
    body["goal"] = {"text": "旧目标", "future_action_fence": {"stop": True}}
    body["analysis_refs"][0]["future_fence"] = {"requires_new_approval": True}
    observation["preparation"]["ref"]["content_hash"] = canonical_hash(body)
    # This snapshot is created after the new fields, rather than relabelling an old snapshot.
    observation["full_ref"] = engine._snapshot_context(run, {"kind": "completion_observation", "observation": deepcopy(observation)})
    result = project_context_working_set(raw)
    actual = result["inherited_context"]["prior_acceptance"]["observation"]["preparation"]["body"]
    assert actual["goal"] == body["goal"] and actual["analysis_refs"] == body["analysis_refs"]
    assert actual["future_action_fence"] == body["future_action_fence"]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["OPEN", "missing_reader", "wrong_reader_hash", "future_ref_fence"])
async def test_open_or_unreadable_answered_preview_is_untouched(env, damage):
    engine, _ = env
    _, run, _ = old_run(engine, status="RUNNING")
    raw, _ = historical_context(engine, run)
    decision = raw["decisions"][0]
    if damage == "OPEN": decision["status"] = "OPEN"
    elif damage == "missing_reader": decision["preview"].pop("full_ref")
    elif damage == "wrong_reader_hash": decision["preview"]["full_ref"]["content_hash"] = "sha256:" + "f" * 64
    else: decision["preview"]["exact_refs"][0]["future_fence"] = {"stop": True}
    expected = deepcopy(decision)
    assert project_context_working_set(raw)["decisions"][0] == expected


@pytest.mark.asyncio
async def test_fixed_history_indices_are_identical_in_m3_and_native_budget(setup, monkeypatch):
    from deepagents.middleware.skills import SkillsMiddleware
    from test_context_wire_replay_budget import capture_slot, request
    engine, _, run, _, credentials = setup
    engine.context_input_token_limit = 48000
    raw, _ = historical_context(engine, run)
    async def project(scope): return raw
    monkeypatch.setattr(engine, "project_context", project)
    monkeypatch.setattr(SkillsMiddleware, "modify_request", lambda self, value: value)
    slot, observed = capture_slot(engine), []
    scope = RunScope(run["id"], "fixed-history-budget-equivalence", "execute")
    async def delivered(modified):
        m = re.search(r"<shengbo_trusted_context>\s*(.*?)\s*</shengbo_trusted_context>", modified.system_message.content, re.S)
        assert json.loads(m[1]) == project_context_working_set(raw)
        fixed = preview_request_payload([modified.system_message], modified.tools,
            model_name=engine.model_name, model_settings=modified.model_settings)
        observed.append((slot._input_budget(modified), engine.context_input_token_limit-engine._estimate_payload(fixed)))
        return ModelResponse(result=[AIMessage("只读预算对照")])
    async def projected(modified): return await ContextProjection(engine).awrap_model_call(modified, delivered)
    await slot.awrap_model_call(request(engine, scope), projected)
    assert observed[0][0] == observed[0][1] and credentials.calls == 0


def test_archived_v4_public_context_has_net_reduction_without_mutation():
    from shengbo_prelive.timing_report import Evidence
    import tiktoken
    root = Path(__file__).resolve().parents[1] / "verification/third-update-merchant-chain-v4-20261020/round-01"
    if not (root / "audio-extension-follow-up/ids.json").is_file():
        pytest.skip("local readonly v4 evidence is not available")
    evidence = Evidence(root)
    ids = json.loads((root / "audio-extension-follow-up/ids.json").read_text(encoding="utf-8"))
    events = [row for row in evidence.events if row.get("run_id") == ids["run_id"] and row.get("type") == "model.request"]
    event = max(events, key=lambda row: row["sequence"])
    payload = evidence.payload(event["data"]["payload_ref"])["payload"]
    contexts = []
    for row in payload["input"]:
        if row.get("role") != "developer": continue
        value = row.get("content", "")
        text = "".join(item.get("text", "") for item in value) if isinstance(value, list) else value
        contexts.extend(json.loads(match) for match in re.findall(r"<shengbo_trusted_context>\s*(.*?)\s*</shengbo_trusted_context>", text, re.S))
    assert len(contexts) == 1
    raw = contexts[0]
    original_hash, stats = canonical_hash(raw), {}
    projected = project_context_working_set(raw, metrics=stats)
    encoding = tiktoken.get_encoding("o200k_base")
    tokens = lambda value: len(encoding.encode(canonical_json(value), disallowed_special=()))
    assert stats["indexed_historical_preparations"] == 1 and stats["indexed_answered_previews"] == 2
    assert tokens(raw) - tokens(projected) > 1000
    assert canonical_hash(raw) == original_hash
