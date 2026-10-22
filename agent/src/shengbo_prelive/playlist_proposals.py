"""Optional exact-order proposal assembly; never asks, approves or writes.

The main Agent selects every occurrence. This helper only resolves exact
ownership and constructs the existing decision contract. It does not replace
content review, choose order, or grant permission to save the proposal.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from shengbo_contracts.common import (
    ArtifactRef, RecordRef, SCHEMA_VERSION, ContractError, canonical_hash,
)


def _ref(value, kind):
    """Keep RecordRef representation while rejecting coercions and bad schema."""
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    if not isinstance(raw, dict) or type(raw.get("revision")) is not int:
        raise ValueError("exact reference required")
    cls = ArtifactRef if "schema_version" in raw else RecordRef
    ref = cls.model_validate(raw, strict=True)
    if (ref.kind != kind or not ref.id or len(ref.id) > 256
            or re.fullmatch(r"sha256:[0-9a-f]{64}", ref.content_hash) is None
            or isinstance(ref, ArtifactRef) and ref.schema_version != SCHEMA_VERSION):
        raise ValueError("exact reference required")
    return ref


def _identity(value):
    return tuple(value[key] for key in ("kind", "id", "revision", "content_hash"))


class PlaylistProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")
    ordered_script_refs: list[ArtifactRef | RecordRef] = Field(min_length=1, max_length=1000,
        description="主Agent明确选定的正式话术精确引用；顺序原样保留，可明确重复同一版本。")
    expected_preparation_ref: ArtifactRef | RecordRef | None = None
    prompt: str = Field(default="请确认此商品与话术版本顺序，并持续循环播放。", min_length=1, max_length=2000)

    @field_validator("ordered_script_refs", mode="before")
    @classmethod
    def exact_scripts(cls, value):
        if not isinstance(value, list):
            raise ValueError("ordered script references must be a list")
        return [_ref(item, "script_revision") for item in value]

    @field_validator("expected_preparation_ref", mode="before")
    @classmethod
    def exact_preparation(cls, value):
        return None if value is None else _ref(value, "preparation")

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value):
        if not value.strip():
            raise ValueError("nonblank prompt required")
        return value


async def _authorized_run(engine, scope):
    if scope.role != "main" or scope.phase != "execute" or scope.branch_task_id:
        raise ContractError("PLAYLIST_PROPOSAL_SCOPE_DENIED", "顺序提案仅供当前主Agent执行阶段使用", status=403)
    run = await engine.assert_scope(scope)
    if "save_playlist" not in run.get("allowed_effects", []):
        raise ContractError("GOAL_EFFECT_DENIED", "本次目标未授权播放顺序操作", status=403)
    if run.get("pending_unknown_operation"):
        raise ContractError("RECONCILIATION_REQUIRED", "原操作结果待核实，先对账后再准备顺序提案")
    if not (run.get("scope") or {}).get("preparation_id"):
        raise ContractError("PREPARATION_REQUIRED", "顺序提案需要明确的当前本场", status=422)
    return run


async def prepare_playlist_proposal(engine, scope, args):
    """Resolve an explicitly selected order and return optional HITL arguments.

    Original Runtime binding/target checks are reused, including their fresh
    reads. Decision creation, human confirmation and save remain separate.
    """
    run = await _authorized_run(engine, scope)
    try:
        request = PlaylistProposalInput.model_validate(args)
    except (ValidationError, TypeError, ValueError):
        raise ContractError("PLAYLIST_PROPOSAL_INPUT_INVALID", "需要合法有序的精确script_revision引用、可选本场引用和非空问题文本", status=422) from None
    prep_id = run["scope"]["preparation_id"]
    prep = await engine.business_query(scope, "read_preparation", {"preparation_id": prep_id})
    try:
        prep_ref = _ref(prep.get("ref"), "preparation").model_dump(mode="json")
        body = prep.get("body")
        if (prep_ref["id"] != prep_id or not isinstance(body, dict)
                or canonical_hash(body) != prep_ref["content_hash"]):
            raise ValueError("preparation snapshot mismatch")
        selected_scripts = {_identity(_ref(value, "script_revision").model_dump(mode="json"))
                            for value in body.get("script_refs", [])}
    except (ValidationError, KeyError, TypeError, ValueError):
        raise ContractError("PLAYLIST_ORDER_SCOPE_DENIED", "当前本场正本及精确话术清单无法核对", status=403) from None
    if request.expected_preparation_ref is not None:
        expected = request.expected_preparation_ref.model_dump(mode="json")
        if _identity(expected) != _identity(prep_ref):
            raise ContractError("PLAYLIST_PROPOSAL_STALE", "本场已变化，请核对新的精确版本后再提出顺序", status=409)
    ordered = [value.model_dump(mode="json") for value in request.ordered_script_refs]
    if any(_identity(value) not in selected_scripts for value in ordered):
        raise ContractError("PLAYLIST_ORDER_TARGET_INVALID", "顺序包含非当前本场正式话术，不能猜测替代版本", status=403)
    metadata, occurrence_counts, items = {}, Counter(), []
    for ref in ordered:
        identity = _identity(ref)
        if identity not in metadata:
            # Only metadata is needed to resolve product_id. The authoritative
            # Runtime verifier below rechecks exact script/product ownership.
            script = await engine.business_query(scope, "read_script", {"ref": ref, "sections": ["body"]})
            try:
                returned_ref = _ref(script.get("ref"), "script_revision").model_dump(mode="json")
                content = script.get("body")
                if (not isinstance(content, dict) or _identity(returned_ref) != identity
                        or canonical_hash(content) != ref["content_hash"]
                        or content.get("preparation_id") != prep_id
                        or not isinstance(content.get("product_id"), str)
                        or not 0 < len(content["product_id"]) <= 256):
                    raise ValueError("script snapshot mismatch")
            except (ValidationError, KeyError, TypeError, ValueError):
                raise ContractError("PLAYLIST_ORDER_TARGET_INVALID", "话术元数据及本场精确归属无法核对", status=403) from None
            metadata[identity] = content
        occurrence_counts[identity] += 1
        occurrence_id = "occ_" + canonical_hash(["explicit-playlist-proposal-v1", prep_id,
            list(identity), occurrence_counts[identity]])[7:39]
        items.append({"occurrence_id": occurrence_id, "product_id": metadata[identity]["product_id"],
                      "script_revision_ref": deepcopy(ref)})
    if len({item["occurrence_id"] for item in items}) != len(items):
        raise ContractError("PLAYLIST_PROPOSAL_ID_COLLISION", "提案条目身份不唯一，请停止并检查", status=409)
    binding = engine._playlist_order_binding(run, {"preparation_id": prep_id,
        "expected_revision": prep_ref["revision"], "items": items, "loop_mode": "infinite",
        "product_group_order": list(dict.fromkeys(item["product_id"] for item in items))})
    # These original checks are not replaced with locally suggested verified
    # flags, nor bypassed by cached summaries or model-provided product IDs.
    preview = await engine._verify_playlist_order_targets(run, binding)
    await _authorized_run(engine, scope)
    return {"status": "PROPOSAL_READY_FOR_OPTIONAL_DECISION", "base_preparation_ref": deepcopy(prep_ref),
        "ordered_input_refs": deepcopy(ordered), "playlist_proposal": deepcopy(binding),
        "proposal_hash": canonical_hash(binding), "preview": deepcopy(preview),
        "optional_next_action": {"tool": "request_user_decision", "arguments": {
            "kind": "playlist_order", "prompt": request.prompt,
            "context": {"playlist": deepcopy(binding)}}},
        "question_created": False, "human_approved": False, "playlist_saved": False,
        "automatic_execution": False, "content_review_completed": False,
        "snapshot_only": True, "requires_revalidation_on_decision_or_save": True,
        "note": "主Agent明确选择的顺序未被改写。此工具仅核对并装配提案，不替代内容主审或用户确认；后续确认、答复和保存仍按原权限、当前性与CAS门禁重新核验。"}
