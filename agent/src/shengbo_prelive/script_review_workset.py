"""Optional v8 read-only workset of this job's current formal Voice scripts.

Uses the existing business reader; no provider archive, job creation, approval,
semantic review verdict or completion transition is hidden in this projection.
"""
from __future__ import annotations

from copy import deepcopy
import re

from shengbo_contracts.common import ContractError, canonical_hash, canonical_json


def _reader(ref, section=None):
    args = {"ref": deepcopy(ref)}
    if section is not None:
        args["sections"] = [section]
    return {"tool": "read_script", "arguments": args}


def _same(left, right):
    return isinstance(left, dict) and isinstance(right, dict) and all(
        left.get(key) == right.get(key) for key in ("kind", "id", "revision", "content_hash"))


def _inherited_job_is_exact(run, prep, chosen, explicit_job_id):
    """Allow review of a predecessor's job with exactly frozen generation inputs.

    Request text, a matching room, or a known job ID alone grants nothing.
    A job is mutable while its frozen inputs are not: status, output, metrics
    and therefore revision/hash may advance after the inherited observation.
    Both observations come from trusted server readers, not a supplied body.
    Historical evidence never becomes a newly generated result of this run.
    """
    if not explicit_job_id:
        return False
    inherited = run.get("inherited_context") or {}
    related_id = inherited.get("related_run_id")
    body = chosen.get("body") or {}
    if (not related_id or related_id == run["id"] or body.get("run_id") != related_id
            or body.get("preparation_id") != prep["ref"]["id"]):
        return False
    prior = (inherited.get("prior_acceptance") or {}).get("observation") or {}

    def valid_ref(value, kind):
        return (isinstance(value, dict) and value.get("kind") == kind
            and isinstance(value.get("id"), str) and bool(value["id"])
            and type(value.get("revision")) is int and value["revision"] >= 1
            and isinstance(value.get("content_hash"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", value["content_hash"]) is not None)

    def frozen_job(value):
        keys = ("run_id", "preparation_id", "room_id", "created_at")
        if (not isinstance(value, dict) or value.get("kind") != "scripts"
                or any(not isinstance(value.get(key), str) or not value[key] for key in keys)
                or type(value.get("generation_epoch")) is not int or value["generation_epoch"] < 0
                or not valid_ref(value.get("plan_ref"), "script_plan")
                or not valid_ref(value.get("context_ref"), "context")
                or not isinstance(value.get("items"), list) or not value["items"]):
            return None
        items = []
        for item in value["items"]:
            if (not isinstance(item, dict) or not isinstance(item.get("item_id"), str)
                    or not item["item_id"] or not isinstance(item.get("input"), dict) or not item["input"]):
                return None
            items.append({"item_id": item["item_id"], "input": item["input"]})
        if len({item["item_id"] for item in items}) != len(items):
            return None
        return {**{key: value[key] for key in (*keys, "kind", "generation_epoch")},
            "plan_ref": value["plan_ref"], "context_ref": value["context_ref"], "items": items}

    current_ref, frozen = chosen.get("ref"), frozen_job(body)
    if not valid_ref(current_ref, "prelive_job") or current_ref["id"] != explicit_job_id or frozen is None:
        return False
    for job in prior.get("jobs", []):
        if not isinstance(job, dict):
            continue
        previous_ref = job.get("ref")
        if (not valid_ref(previous_ref, "prelive_job") or previous_ref["id"] != current_ref["id"]
                or current_ref["revision"] < previous_ref["revision"]
                or current_ref["revision"] == previous_ref["revision"]
                    and current_ref["content_hash"] != previous_ref["content_hash"]):
            continue
        if frozen_job(job.get("body")) == frozen:
            return True
    return False


async def script_review_workset(f, run, prep, observation, *, job_id=None,
                               max_scripts=8, max_content_chars=60000):
    """Deliver complete selected modules once, or explicit unread exact readers.

    Limits omit whole scripts, never cut a sentence and call it complete. All
    job item states stay visible even when formal text exceeds the read budget.
    """
    result = {"action": "review", "version": "v8", "status": "NEEDS_REPLAN",
        "preparation_ref": deepcopy(prep["ref"]), "items": [], "blockers": [], "remaining": [],
        "review_content_complete": False, "content_quality_passed": False,
        "approval_granted": False, "automatic_execution": False,
        "contract": "仅返回当前正式稿供主Agent自主审阅；COMPLETED仅表示本作业当前正式正文已完整提供，不代表内容质量通过、人工批准、任务finish、语音或发布。模块按正式module_refs原顺序；句子额外字段保留，spoken_text_equals_text=true表示原spoken_text与text完全相同，精确reader仍可读完整正本。"}
    blockers, remaining = result["blockers"], result["remaining"]
    jobs = [j for j in observation.get("jobs", []) if j.get("body", {}).get("kind") == "scripts"]
    if job_id:
        chosen = next((j for j in jobs if j.get("ref", {}).get("id") == job_id), None)
    else:
        owned = [j for j in jobs if j.get("body", {}).get("run_id") == run["id"]]
        chosen = max(owned, key=lambda j: (j["body"].get("created_at", ""), j.get("ref", {}).get("id", "")), default=None)
    if chosen is None:
        blockers.append({"code": "SCRIPT_REVIEW_JOB_NOT_OBSERVED", "message": "未观察到指定作业或本run的话术作业；不从其他本场商品猜选正式稿。"})
        if job_id:
            remaining.append({"tool": "read_job", "arguments": {"job_id": job_id}})
        return result
    body, prep_body = chosen["body"], prep["body"]
    result.update(job_ref=deepcopy(chosen["ref"]), job_status=body.get("status"),
        owner_run_id=body.get("run_id"), current_run_id=run["id"])
    items = body.get("items", [])
    counts = f.engine._job_item_counts(items)
    unknown = (run.get("pending_unknown_operation") or observation.get("unknown_job_ids")
        or counts.get("unknown_item_count") or counts.get("unknown_provider_attempt_count"))
    pending = (observation.get("pending_job_ids") or body.get("status") in {"queued", "running"}
        or counts.get("in_flight_provider_attempt_count"))
    if unknown:
        blockers.append({"code": "SCRIPT_REVIEW_OUTCOME_UNKNOWN", "message": "仍有未知作业或供应商结果，不能宣称本次成果已齐。"})
    elif pending:
        blockers.append({"code": "SCRIPT_REVIEW_PENDING", "message": "仍有未完成作业，本次只能读取已落成且当前的正式稿。"})
    if body.get("status") != "succeeded":
        blockers.append({"code": "SCRIPT_REVIEW_JOB_NOT_SUCCEEDED", "message": "原作业未成功完成；已提交模块不等于完整正式稿。"})
    inherited_job = _inherited_job_is_exact(run, prep, chosen, job_id)
    boundary_ok = (body.get("run_id") == run["id"]
        and body.get("preparation_id") == prep["ref"]["id"]) or inherited_job
    if inherited_job:
        result.update(historical_evidence_only=True, counts_as_current_run_generation=False)
        blockers.append({"code": "SCRIPT_REVIEW_HISTORICAL_EVIDENCE_ONLY",
            "message": "显式选择的前序任务成果与可信继承记录精确一致；提供仍属本场当前正式稿的正文供审阅，不把历史生成认证为本次新生成成果，也不授予修改、批准或完成权限。"})
    if not boundary_ok:
        blockers.append({"code": "SCRIPT_REVIEW_JOB_SCOPE_MISMATCH", "message": "历史run或其他本场作业不能作为本次当前成果；保留原read_job入口。"})
        remaining.append({"tool": "read_job", "arguments": {"job_id": chosen["ref"]["id"]}})
    if body.get("generation_epoch") != prep_body.get("generation_epoch") or not _same(body.get("plan_ref"), prep_body.get("plan_ref")):
        blockers.append({"code": "SCRIPT_REVIEW_INPUTS_CHANGED", "message": "作业计划或generation_epoch不再对应当前本场；不把旧输出认证为本次当前成果。"})
        boundary_ok = False
    current_refs = prep_body.get("script_refs") or []
    goal = run.get("goal") or {}
    targets = goal.get("action_target_refs") or run.get("action_target_refs") or []
    target_ids = set(goal.get("target_product_ids") or run.get("target_product_ids") or [])
    cached, read_count, content_chars = {}, 0, 0
    for position, item in enumerate(items):
        spec = item.get("input") or {}
        ref = (item.get("output") or {}).get("script_ref")
        row = {"position": position, "item_id": item.get("item_id"), "status": item.get("status"),
            "call_state": item.get("call_state"), "target": {key: deepcopy(spec[key]) for key in
                ("product_ref", "variant_id", "version_number") if key in spec},
            "script_ref": deepcopy(ref), "current_formal_ref": False, "content_complete": False}
        result["items"].append(row)
        if ref:
            row["reader"] = _reader(ref)
        if not boundary_ok:
            continue
        product = spec.get("product_ref") or {}
        if (targets and not any(_same(product, target) for target in targets)
                or target_ids and product.get("id") not in target_ids):
            row["review_status"] = "outside_current_targets"
            blockers.append({"code": "SCRIPT_REVIEW_TARGET_MISMATCH", "item_id": item.get("item_id")})
            continue
        if not ref or item.get("status") != "succeeded":
            row["review_status"] = "formal_output_unavailable"
            blockers.append({"code": "SCRIPT_REVIEW_ITEM_NOT_SUCCEEDED", "item_id": item.get("item_id")})
            continue
        if not any(_same(ref, current) for current in current_refs):
            row["review_status"] = "stale_or_not_current"
            blockers.append({"code": "SCRIPT_REVIEW_FORMAL_REF_NOT_CURRENT", "item_id": item.get("item_id")})
            remaining.append(row["reader"])
            continue
        row["current_formal_ref"] = True
        cache_key = canonical_hash(ref)
        if cache_key in cached:
            row.update(content_item_position=cached[cache_key], content_complete=True, review_status="content_shared_with_item")
            continue
        if read_count >= max_scripts:
            row["review_status"] = "unread_budget"
            remaining.append(row["reader"])
            continue
        try:
            script = await f.query("read_script", {"ref": ref})
        except ContractError as error:
            row["review_status"] = "reader_failed"
            blockers.append({"code": error.code, "item_id": item.get("item_id")})
            remaining.append(row["reader"])
            continue
        read_count += 1
        content = script.get("body", {})
        if (not _same(script.get("ref"), ref) or canonical_hash(content) != ref.get("content_hash")
                or content.get("preparation_id") != prep["ref"]["id"]
                or not _same(content.get("product_ref"), product)
                or content.get("variant_id") != spec.get("variant_id")
                or content.get("version_number") != spec.get("version_number")):
            row["review_status"] = "formal_reader_mismatch"
            blockers.append({"code": "SCRIPT_REVIEW_FORMAL_READER_MISMATCH", "item_id": item.get("item_id")})
            continue
        modules, edit_targets, valid = [], {}, True
        for key, module_ref in (content.get("module_refs") or {}).items():
            if module_ref is None:
                continue
            module = (script.get("modules") or {}).get(key)
            if (not isinstance(module, dict) or canonical_hash(module) != module_ref.get("content_hash")
                    or module.get("module_key") != key or not isinstance(module.get("sentences"), list)
                    or not module["sentences"]):
                valid = False
                break
            edit_target = {"script_ref": deepcopy(script["ref"]), "module_key": key,
                "module_ref": deepcopy(module_ref), "sentence_ids": [],
                "selected_text_hash": canonical_hash(module["sentences"])}
            supplied_targets = script.get("module_edit_targets")
            if supplied_targets is not None and (not isinstance(supplied_targets, dict)
                    or supplied_targets.get(key) != edit_target):
                valid = False
                break
            edit_targets[key] = edit_target
            sentences = deepcopy(module["sentences"])
            for sentence in sentences:
                if not isinstance(sentence, dict) or not isinstance(sentence.get("text"), str) or not sentence["text"].strip():
                    valid = False
                    break
                if sentence.get("spoken_text") == sentence["text"] and "spoken_text_equals_text" not in sentence:
                    sentence.pop("spoken_text")
                    sentence["spoken_text_equals_text"] = True
            modules.append({"position": len(modules), "module_key": key,
                "module_ref": deepcopy(module_ref), "sentences": sentences,
                **({"additional_fields": deepcopy({k: v for k, v in module.items()
                    if k not in {"module_key", "parent_ref", "sentences"}})}
                   if set(module) - {"module_key", "parent_ref", "sentences"} else {})})
        if not valid or not modules:
            row["review_status"] = "formal_text_incomplete"
            blockers.append({"code": "SCRIPT_REVIEW_FORMAL_TEXT_INCOMPLETE", "item_id": item.get("item_id")})
            remaining.append(row["reader"])
            continue
        chars = len(canonical_json(modules)) + len(canonical_json(edit_targets))
        if content_chars + chars > max_content_chars:
            row["review_status"] = "unread_budget"
            remaining.append(row["reader"])
            continue
        content_chars += chars
        row.update(modules=modules, module_edit_targets=edit_targets, content_complete=True,
            review_status="historical_current_content" if inherited_job else "formal_text_provided",
            script_revision=content.get("script_revision"))
        cached[cache_key] = position
    if not items:
        blockers.append({"code": "SCRIPT_REVIEW_NO_JOB_ITEMS", "message": "作业没有可核对的目标项。"})
    result["review_content_complete"] = bool(items) and all(row["content_complete"] for row in result["items"]) and not blockers and not remaining
    if inherited_job:
        result["historical_current_content_complete"] = bool(items) and all(
            row["content_complete"] for row in result["items"]) and not remaining and all(
                blocker["code"] == "SCRIPT_REVIEW_HISTORICAL_EVIDENCE_ONLY" for blocker in blockers)
    if remaining:
        blockers.append({"code": "SCRIPT_REVIEW_UNREAD_REMAINING", "message": "部分正文未提供，必须按remaining精确reader读取；未声称完整审阅。"})
    result["status"] = ("OUTCOME_UNKNOWN" if unknown else "WAITING" if pending else
        "COMPLETED" if result["review_content_complete"] else "NEEDS_REPLAN")
    result["content_projection"] = {"returned_scripts": len(cached), "read_scripts": read_count,
        "content_chars": content_chars, "max_scripts": max_scripts, "max_content_chars": max_content_chars}
    return result
