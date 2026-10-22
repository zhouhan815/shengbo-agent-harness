"""Explicit human consent for one additional paid attempt per lost audio item.

This is not retry permission for an Agent. Original attempts and their billing
uncertainty stay untouched. A separate durable slot names the consented attempt.
"""
from copy import deepcopy
import json
import re

from .storage import BusinessError, digest, now

UNKNOWN = {"timeout_unknown", "outcome_unknown"}
MAX_ITEMS = 10


def _get(service, connection, kind, identity):
    try:
        return service.store.get(connection, kind, identity)
    except BusinessError as error:
        if error.code != "NOT_FOUND":
            raise
        return None


def _wire(value):
    return value.model_dump(exclude_none=True) if hasattr(value, "model_dump") else deepcopy(value)


def _valid_asset(service, asset, requirement):
    return bool(isinstance(asset, dict) and service._audio_valid(asset)
        and isinstance(asset.get("sha256_bytes"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", asset["sha256_bytes"])
        and asset.get("requirement_id") == requirement["requirement_id"]
        and asset.get("spoken_text_hash") == digest(requirement["spoken_text"])
        and asset.get("voice") == requirement["voice"]
        and service._audio_style_matches(asset, requirement))


def _guard_id(job_id, item_id, attempt_ids):
    return digest({"source_job_id": job_id, "source_item_id": item_id,
        "unknown_attempt_ids": sorted(attempt_ids)})


def _input_matches(supplied, requirement):
    return (supplied.get("requirement_id") == requirement["requirement_id"]
        and supplied.get("spoken_text") == requirement["spoken_text"]
        and supplied.get("voice") == requirement["voice"]
        and ("style" not in requirement or supplied.get("style") == requirement["style"]))


def _inspect(service, connection, preparation_id, job_id):
    prep = service._prep(connection, preparation_id)
    body = prep["body"]
    source = service.store.get(connection, "prelive_job", job_id)
    job = source["body"]
    if job.get("kind") != "audio" or job.get("preparation_id") != preparation_id:
        raise BusinessError("AUDIO_REISSUE_SOURCE_SCOPE_CONFLICT", status=403)
    blockers = []
    if not body.get("approval_ref") or not body.get("requirements_ref"):
        raise BusinessError("AUDIO_REISSUE_CURRENT_APPROVAL_REQUIRED")
    approval = service._approval(connection, body, body["approval_ref"])
    requirements = service._artifact(connection, body["requirements_ref"], "requirements", preparation_id)["body"]
    if (job.get("approval_ref") != body["approval_ref"] or job.get("review_hash") != approval["review_hash"]
            or job.get("requirements_ref") != body["requirements_ref"]
            or job.get("generation_epoch") != body["generation_epoch"]):
        raise BusinessError("AUDIO_REISSUE_SOURCE_STALE")
    for record in service.store.all(connection, "prelive_job"):
        value = record["body"]
        if value.get("preparation_id") == preparation_id and (value.get("status") in {"queued", "running"}
                or any(item.get("status") in {"queued", "running"} for item in value.get("items", []))):
            blockers.append({"code": "BUSINESS_JOB_PENDING", "job_id": record["ref"]["id"]})
    unknown_items, bindings, missing = [], [], []
    for requirement in requirements.get("items", []):
        cached = _get(service, connection, "prelive_audio_asset", requirement["requirement_id"])
        if cached and _valid_asset(service, cached["body"], requirement):
            continue
        missing.append(requirement["requirement_id"])
        matches = [item for item in job.get("items", [])
            if item.get("input", {}).get("requirement_id") == requirement["requirement_id"]]
        if len(matches) != 1:
            blockers.append({"code": "AUDIO_REISSUE_SOURCE_ITEM_REQUIRED", "requirement_id": requirement["requirement_id"]})
            continue
        item = matches[0]
        attempts = [row for row in item.get("provider_attempts", []) if row.get("state") in UNKNOWN]
        if (item.get("status") not in {"failed", "interrupted", "cancelled"}
                or digest(item.get("input", {})) != digest(requirement)
                or not attempts or any(row.get("purpose") != "tts" or not _input_matches(row.get("input", {}), requirement)
                    for row in attempts)):
            blockers.append({"code": "AUDIO_REISSUE_UNKNOWN_ATTEMPT_REQUIRED", "requirement_id": requirement["requirement_id"]})
            continue
        # A returned WAV can be recovered locally; do not buy it again.
        if any(row.get("purpose") == "tts" and row.get("state") == "result_received"
                and _input_matches(row.get("input", {}), requirement) and service._audio_valid(row.get("output") or {})
                for row in item.get("provider_attempts", [])):
            blockers.append({"code": "AUDIO_REISSUE_LOCAL_RESULT_AVAILABLE", "requirement_id": requirement["requirement_id"]})
            continue
        ids = sorted(row["attempt_id"] for row in attempts)
        last = attempts[-1]
        index = _get(service, connection, "prelive_external_request", last.get("request_key")) if last.get("request_key") else None
        if (not index or index["body"].get("job_id") != job_id or index["body"].get("item_id") != item["item_id"]
                or index["body"].get("attempt_id") != last["attempt_id"]
                or index["body"].get("state") not in UNKNOWN):
            blockers.append({"code": "AUDIO_REISSUE_REQUEST_INDEX_UNRESOLVED", "requirement_id": requirement["requirement_id"]})
            continue
        guard = _guard_id(job_id, item["item_id"], ids)
        if _get(service, connection, "audio_reissue_slot", guard):
            blockers.append({"code": "AUDIO_REISSUE_ALREADY_REQUESTED", "requirement_id": requirement["requirement_id"]})
            continue
        public = {"item_id": item["item_id"], "requirement_id": requirement["requirement_id"], "unknown_attempt_ids": ids}
        unknown_items.append(public)
        bindings.append({**public, "slot_id": guard, "source_request_key": last["request_key"],
            "logical_request_key": digest({"purpose": "tts", "identity": requirement["requirement_id"]}),
            "requirement": deepcopy(requirement), "requirement_hash": digest(requirement)})
    if not missing:
        blockers.append({"code": "AUDIO_REISSUE_NOT_NEEDED"})
    if len(missing) > MAX_ITEMS:
        blockers.append({"code": "AUDIO_REISSUE_ITEM_LIMIT", "limit": MAX_ITEMS, "missing_count": len(missing)})
    from .job_outcome_dependencies import observe_job_outcomes
    observation = observe_job_outcomes(service, connection, body)
    selected = {row["item_id"] for row in unknown_items}
    for unknown in observation["unknown_items"]:
        if unknown["job_id"] != job_id or unknown["item_id"] not in selected:
            blockers.append({"code": "AUDIO_REISSUE_UNSELECTED_UNKNOWN", "job_id": unknown["job_id"], "item_id": unknown["item_id"]})
    binding = {"preparation_id": preparation_id, "source_job_ref": source["ref"],
        "approval_ref": body["approval_ref"], "requirements_ref": body["requirements_ref"],
        "review_hash": approval["review_hash"], "expected_generation_epoch": body["generation_epoch"],
        "items": bindings, "max_attempts_per_requirement": 1, "maximum_items": MAX_ITEMS}
    public = {"status": "blocked" if blockers else "ready_for_explicit_approval", "preparation_ref": prep["ref"],
        **{key: binding[key] for key in ("source_job_ref", "approval_ref", "requirements_ref", "review_hash", "expected_generation_epoch")},
        "unknown_items": unknown_items, "missing_count": len(missing), "blockers": blockers,
        "binding_hash": digest(binding), "maximum_items": MAX_ITEMS,
        "notice": "原请求可能已计费；此预览不是批准。只有真实人工确认才能对最终缺失句各补一次，原未知账本保留。"}
    return public, binding


def inspect(service, connection, args):
    return _inspect(service, connection, args.preparation_id, args.job_id)[0]


def approve(service, connection, args, mutation, principal, scope):
    if principal.kind != "human":
        raise BusinessError("HUMAN_REQUIRED", status=403)
    prep = service._prep(connection, args.preparation_id, args.expected_revision)
    public, binding = _inspect(service, connection, args.preparation_id, args.source_job_ref.id)
    if public["blockers"]:
        raise BusinessError("AUDIO_REISSUE_NOT_ELIGIBLE", details={"blockers": public["blockers"]})
    if (binding["source_job_ref"] != _wire(args.source_job_ref) or binding["approval_ref"] != _wire(args.approval_ref)
            or binding["requirements_ref"] != _wire(args.requirements_ref) or binding["review_hash"] != args.review_hash
            or binding["expected_generation_epoch"] != args.expected_generation_epoch
            or public["binding_hash"] != args.binding_hash or public["unknown_items"] != [_wire(row) for row in args.unknown_items]):
        raise BusinessError("AUDIO_REISSUE_BINDING_STALE")
    approval_ref = service.store.artifact(connection, "audio_reissue_approval", {"preparation_id": args.preparation_id, "binding": binding,
        "binding_hash": args.binding_hash, "human_actor_id": principal.actor_id, "operation_id": mutation.operation_id,
        "approved_at": now(), "authorization_quote": args.authorization_quote,
        "possible_prior_billing_acknowledged": True})
    receipt = service._enqueue(connection, "audio", prep, [row["requirement"] for row in binding["items"]],
        mutation, principal, scope, {"approval_ref": binding["approval_ref"], "review_hash": binding["review_hash"],
            "requirements_ref": binding["requirements_ref"], "audio_reissue_approval_ref": approval_ref})
    for row in binding["items"]:
        service.store.put(connection, "audio_reissue_slot", row["slot_id"], {"state": "reserved", "allowed_attempts": 1,
            "consumed_attempts": 0, "approval_ref": approval_ref, "job_id": receipt["job_id"],
            "requirement_id": row["requirement_id"], "source_job_ref": binding["source_job_ref"],
            "source_item_id": row["item_id"], "unknown_attempt_ids": row["unknown_attempt_ids"]}, create=True)
    return {**receipt, "reissue_approval_ref": approval_ref, "binding_hash": args.binding_hash,
        "original_outcomes_unchanged": True, "maximum_paid_attempts": len(binding["items"])}


def _approved_binding(service, connection, prep, approval_ref, job_id):
    record = service._artifact(connection, approval_ref, "audio_reissue_approval", prep["id"])
    body, binding = record["body"], record["body"]["binding"]
    receipt = connection.execute("SELECT command,response_json FROM prelive_operations WHERE principal_id=? AND operation_id=?",
        (body["human_actor_id"], body["operation_id"])).fetchone()
    data = json.loads(receipt["response_json"]).get("data", {}) if receipt else {}
    if (not receipt or receipt["command"] != "approve_missing_audio_reissue" or data.get("job_id") != job_id
            or data.get("reissue_approval_ref") != record["ref"] or body["binding_hash"] != digest(binding)
            or not body.get("possible_prior_billing_acknowledged") or len(binding["items"]) > MAX_ITEMS
            or binding.get("max_attempts_per_requirement") != 1):
        raise BusinessError("AUDIO_REISSUE_HUMAN_RECEIPT_REQUIRED", status=403)
    if (binding["preparation_id"] != prep["id"] or binding["approval_ref"] != prep.get("approval_ref")
            or binding["requirements_ref"] != prep.get("requirements_ref")
            or binding["expected_generation_epoch"] != prep["generation_epoch"]):
        raise BusinessError("AUDIO_REISSUE_BINDING_STALE")
    service._approval(connection, prep, binding["approval_ref"], binding["review_hash"])
    service._artifact(connection, binding["source_job_ref"], "prelive_job", prep["id"])
    return binding


def validate_job(service, connection, prep, job):
    binding = _approved_binding(service, connection, prep, job["audio_reissue_approval_ref"], job["id"])
    if (job.get("approval_ref") != binding["approval_ref"] or job.get("requirements_ref") != binding["requirements_ref"]
            or job.get("review_hash") != binding["review_hash"]
            or len(job["items"]) != len(binding["items"])
            or sorted(digest(row["input"]) for row in job["items"]) != sorted(row["requirement_hash"] for row in binding["items"])):
        raise BusinessError("AUDIO_REISSUE_BINDING_STALE")
    return binding


def provider_slot(service, connection, prep, job, item_id, supplied):
    binding = validate_job(service, connection, prep, job)
    item = next(row for row in job["items"] if row["item_id"] == item_id)
    matched = [row for row in binding["items"] if row["requirement_id"] == item["input"]["requirement_id"]]
    if len(matched) != 1 or not _input_matches(supplied, matched[0]["requirement"]):
        raise BusinessError("AUDIO_REISSUE_BINDING_STALE")
    row = matched[0]
    slot = service.store.get(connection, "audio_reissue_slot", row["slot_id"])
    if (slot["body"].get("state") != "reserved" or slot["body"].get("consumed_attempts") != 0
            or slot["body"].get("job_id") != job["id"] or slot["body"].get("approval_ref") != job["audio_reissue_approval_ref"]):
        raise BusinessError("AUDIO_REISSUE_ALLOWANCE_CONSUMED", "这份人工批准的一次补合成额度已使用，不自动重复计费")
    prior = service.store.get(connection, "prelive_external_request", row["source_request_key"])["body"]
    if prior.get("attempt_id") not in row["unknown_attempt_ids"] or prior.get("state") not in UNKNOWN:
        raise BusinessError("AUDIO_REISSUE_SOURCE_CHANGED")
    key = digest({"purpose": "tts.authorized_reissue", "logical_request_key": row["logical_request_key"],
        "approval_ref": job["audio_reissue_approval_ref"]})
    if _get(service, connection, "prelive_external_request", key):
        raise BusinessError("AUDIO_REISSUE_ALLOWANCE_CONSUMED")
    return {"request_key": key, "logical_request_key": row["logical_request_key"], "slot_ref": slot["ref"],
        "audio_reissue_approval_ref": job["audio_reissue_approval_ref"], "source_unknown_attempt_ids": row["unknown_attempt_ids"]}


def consume_slot(service, connection, proof, attempt_id):
    slot = service.store.exact(connection, proof["slot_ref"])
    service.store.put(connection, "audio_reissue_slot", slot["ref"]["id"], {**slot["body"],
        "state": "sent", "consumed_attempts": 1, "attempt_id": attempt_id, "sent_at": now()}, expected=slot["ref"]["revision"])


def record_resolution(service, connection, prep, job, item, asset_ref):
    binding = validate_job(service, connection, prep, job)
    matches = [row for row in binding["items"] if row["requirement_id"] == item["input"]["requirement_id"]]
    if len(matches) != 1:
        raise BusinessError("AUDIO_REISSUE_BINDING_STALE")
    row = matches[0]
    slot = service.store.get(connection, "audio_reissue_slot", row["slot_id"])["body"]
    attempt = next((value for value in item.get("provider_attempts", []) if value.get("attempt_id") == slot.get("attempt_id")), None)
    asset = service.store.exact(connection, asset_ref)["body"]
    if (slot.get("state") != "sent" or not attempt or attempt.get("state") != "result_received"
            or attempt.get("audio_reissue_approval_ref") != job["audio_reissue_approval_ref"]
            or slot.get("job_id") != job["id"] or slot.get("approval_ref") != job["audio_reissue_approval_ref"]
            or not _input_matches(attempt.get("input", {}), row["requirement"])
            or not _valid_asset(service, asset, row["requirement"])):
        raise BusinessError("AUDIO_REISSUE_RESULT_NOT_PROVEN")
    resolution = {"preparation_id": prep["id"], "approval_ref": job["audio_reissue_approval_ref"],
        "job_id": job["id"], "item_id": item["item_id"], "attempt_id": attempt["attempt_id"],
        "asset_ref": asset_ref, "requirement_id": row["requirement_id"],
        "source_job_ref": binding["source_job_ref"], "source_item_id": row["item_id"],
        "source_unknown_attempt_ids": row["unknown_attempt_ids"], "committed_at": now()}
    service.store.put(connection, "audio_reissue_resolution", row["slot_id"], resolution, create=True)


def complete_manifest(service, connection, prep, job):
    validate_job(service, connection, prep, job)
    requirements = service._artifact(connection, prep["requirements_ref"], "requirements", prep["id"])["body"]
    assets = []
    for requirement in requirements["items"]:
        cached = _get(service, connection, "prelive_audio_asset", requirement["requirement_id"])
        if not cached or not _valid_asset(service, cached["body"], requirement):
            return None
        assets.append(cached["body"])
    return service.store.artifact(connection, "audio_manifest", {"preparation_id": prep["id"],
        "requirements_ref": prep["requirements_ref"], "approval_ref": prep["approval_ref"], "assets": assets,
        "all_ready": True}) if assets else None


def replacement_evidence(service, connection, prep, source, item):
    """Only exact, human-approved, committed replacement audio satisfies old loss."""
    if source.get("kind") != "audio":
        return None
    ids = sorted(row.get("attempt_id") for row in item.get("provider_attempts", []) if row.get("state") in UNKNOWN)
    if not ids or None in ids:
        return None
    identity = _guard_id(source["id"], item.get("item_id"), ids)
    resolution = _get(service, connection, "audio_reissue_resolution", identity)
    if not resolution:
        return None
    result = resolution["body"]
    try:
        binding = _approved_binding(service, connection, prep, result["approval_ref"], result["job_id"])
        row = next(value for value in binding["items"] if value["slot_id"] == identity)
        job = service.store.get(connection, "prelive_job", result["job_id"])["body"]
        current_item = next(value for value in job["items"] if value["item_id"] == result["item_id"])
        attempt = next(value for value in current_item.get("provider_attempts", []) if value["attempt_id"] == result["attempt_id"])
        asset = service.store.exact(connection, result["asset_ref"])["body"]
        slot = service.store.get(connection, "audio_reissue_slot", identity)["body"]
        expected_request_key = digest({"purpose": "tts.authorized_reissue", "logical_request_key": row["logical_request_key"],
            "approval_ref": result["approval_ref"]})
        index = service.store.get(connection, "prelive_external_request", expected_request_key)["body"]
        if (result["source_job_ref"] != binding["source_job_ref"] or result["source_item_id"] != item["item_id"]
                or binding["source_job_ref"]["id"] != source["id"]
                or result["source_unknown_attempt_ids"] != ids or row["unknown_attempt_ids"] != ids
                or job.get("audio_reissue_approval_ref") != result["approval_ref"]
                or current_item.get("status") != "succeeded" or current_item.get("call_state") != "result_received"
                or (current_item.get("output") or {}).get("asset_ref") != result["asset_ref"]
                or attempt.get("state") != "result_received" or not _input_matches(attempt.get("input", {}), row["requirement"])
                or attempt.get("audio_reissue_approval_ref") != result["approval_ref"]
                or attempt.get("request_key") != expected_request_key or index.get("attempt_id") != attempt["attempt_id"]
                or index.get("state") != "result_received" or index.get("job_id") != job["id"]
                or index.get("item_id") != current_item["item_id"]
                or slot.get("attempt_id") != attempt["attempt_id"] or slot.get("consumed_attempts") != 1
                or slot.get("job_id") != job["id"] or slot.get("approval_ref") != result["approval_ref"]
                or not _valid_asset(service, asset, row["requirement"])):
            return None
        return {"reissue_approval_ref": result["approval_ref"], "replacement_job_id": result["job_id"],
            "replacement_item_id": result["item_id"], "replacement_attempt_id": result["attempt_id"],
            "replacement_asset_ref": result["asset_ref"], "requirement_id": result["requirement_id"]}
    except (BusinessError, KeyError, StopIteration, TypeError, ValueError):
        return None
