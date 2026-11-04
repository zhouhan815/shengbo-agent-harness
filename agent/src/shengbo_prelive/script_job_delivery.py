"""Typed workbench generation and deterministic evidence after Voice saves."""

from collections import Counter
from decimal import Decimal, ROUND_CEILING
import re
from unicodedata import normalize

from .target_scope import ref_key


_MINIMUM_CUE = r"(?:至少|起码|最低(?:要求)?(?:是|为)?|不少于|不能少于|不得少于|不要少于|别少于|不低于|不得低于|不能低于|不短于|不得短于|不能短于)"
_LENGTH_UNIT = r"\s*(?:个?\s*(?:有效)?\s*(?:口播)?\s*)?(中文字符|中文字|汉字|字|分钟|秒)"
_SIZE = r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+(?:\.[0-9]+)?)" + _LENGTH_UNIT
_CHINESE_SIZE = r"[零〇一二两三四五六七八九十百千万亿]+" + _LENGTH_UNIT
_MINIMUM_SUFFIX = r"\s*(?:以上|起步|起(?!码)|为下限)"
_HARD_CUE = r"(?:必须|务必|一定要|非得|硬性要求)"
_OPTIONAL_HARD_PREFIX = rf"(?:{_HARD_CUE}\s*[:：]?\s*)?"
_MINIMUM_PATTERNS = (re.compile(_OPTIONAL_HARD_PREFIX + _MINIMUM_CUE + r"\s*[:：]?\s*" + _SIZE),
                     re.compile(_OPTIONAL_HARD_PREFIX + _SIZE + _MINIMUM_SUFFIX))
_UNPARSED_MINIMUM = re.compile(_MINIMUM_CUE + r"\s*[:：]?\s*" + _CHINESE_SIZE + "|" + _CHINESE_SIZE + _MINIMUM_SUFFIX)
_OTHER_ACCEPTANCE = re.compile(r"(?:验收|审核|校对|评分|打分|审稿|评测|复核后再|逐项核对|校验通过|原稿对比报告)")
_REVIEW_WAIVER = re.compile(r"(?:不用|无需|无须|不要|不需要|不必)(?:再|另行|额外|单独)?(?:验收|审核|校对|评分|打分|审稿|评测)")
_UNPARSED_LENGTH = re.compile(r"(?:字数|长度|时长|分钟|口播字|\d\s*字).{0,18}(?:必须|不得|不超过|为准|以内|下限|上限|精确|至少|以上|恰好)|(?:必须|务必|恰好|精确|不超过|不得|至少|不低于|不少于|最低).{0,18}(?:字数|分钟|秒|[\d一二三四五六七八九十百千万]\s*字)")
_SEMANTIC_REQUIREMENT = re.compile(_HARD_CUE + r"|(?:不可|不得|不许)(?:漏掉|遗漏|省略)|至少(?:提供|写出|包含|覆盖|列出)")
_TIME_SCALE = {"字": 1, "汉字": 1, "中文字": 1, "中文字符": 1, "秒": 4, "分钟": 240}
_UNSETTLED = {"sent", "timeout_unknown", "outcome_unknown", "reserved", "running", "pending"}


def delivery_constraints(request, text=""):
    """Freeze hard minima and decline unsupported completion assertions."""
    notes = normalize("NFKC", text + "\n" + request.get("writing_notes", ""))
    minimums = [request.get("minimum_spoken_characters") or 0]
    matched = []
    for pattern in _MINIMUM_PATTERNS:
        for match in pattern.finditer(notes):
            value = Decimal(match[1].replace(",", "")) * _TIME_SCALE[match[2]]
            if 0 < value <= 30000 and not (match[2] in {"秒", "分钟"} and re.search(r"(?:实际音频|实际播报|实测)", notes)):
                minimums.append(int(value.to_integral_value(rounding=ROUND_CEILING)))
                matched.append(match.span())
    remaining = notes
    for start, end in sorted(set(matched), reverse=True):
        remaining = remaining[:start] + " " * (end - start) + remaining[end:]
    return {"minimum_spoken_characters": max(minimums) or None,
            "require_semantic_review": bool(request.get("require_semantic_review")
                                            or _OTHER_ACCEPTANCE.search(_REVIEW_WAIVER.sub("", notes))
                                            or _UNPARSED_MINIMUM.search(remaining) or _UNPARSED_LENGTH.search(remaining)
                                            or _SEMANTIC_REQUIREMENT.search(remaining)
                                            or re.search(r"(?:只|仅)(?:算|统计|计入)(?:中文|汉字)|(?:不计|不含)(?:英文|字母|数字)", remaining))}


def typed_intent(run):
    request = run["input"]["script_generation"]
    return {"objective": "materials_to_script", "summary": "为工作台明确选择的商品新增完整文字话术",
            "preparation_id": request["preparation_id"], "script_mode": "generate",
            "script_delivery_requirement": "new_generation",
            "target_product_ids": [item["product_ref"]["id"] for item in request["product_counts"]],
            "requested_script_variants": [{"product_id": item["product_ref"]["id"], "count": item["count"]}
                                          for item in request["product_counts"]]}


def _version_key(spec):
    if (not spec.get("product_ref") or not spec.get("variant_id") or type(spec.get("version_number")) is not int
            or spec.get("generation_mode") != "whole_script" or spec.get("base_script_ref") or spec.get("original_script_ref")):
        return None
    return ref_key(spec["product_ref"]), spec["variant_id"], spec["version_number"]


def _replaced_empty_failure(job, successes):
    """Only a known zero-output rejection can yield to a newer exact version."""
    epoch, plan = job.get("generation_epoch"), job.get("plan_ref") or {}
    if type(epoch) is not int or plan.get("kind") != "script_plan" or not job.get("items"):
        return False
    for item in job["items"]:
        key = _version_key(item.get("input", {}))
        successor = successes.get(key)
        if (not successor or type(successor[0]) is not int or successor[0] <= epoch
                or successor[1][0] != "script_plan" or successor[1] == ref_key(plan)
                or item.get("status") != "failed" or not (item.get("error") or {}).get("code")
                or item.get("output") or item.get("whole_script_result") or item.get("module_results")
                or item.get("local_revalidation_attempts") or item.get("archive_revalidations")):
            return False
        attempts = item.get("provider_attempts", [])
        if item.get("call_state") == "not_sent" and not attempts:
            continue
        if (item.get("call_state") != "result_received" or not attempts
                or any(attempt.get("state") != "response_received" or attempt.get("validation_state") != "rejected"
                       or attempt.get("committed_module_ref") or attempt.get("committed_module_refs") for attempt in attempts)):
            return False
    return True


def owned_delivery_items(run, observation, operations):
    """Require all requested variants and their current formal script refs."""
    goal = run.get("goal") or {}
    request = goal.get("script_generation") or {}
    prep = observation.get("preparation", {}).get("body", {})
    prep_id = run.get("scope", {}).get("preparation_id")
    if (not request or run.get("dispatch_frozen")
            or run.get("status") not in {"WAITING_JOBS", "RUNNING"} or run.get("pending_unknown_operation")
            or observation.get("pending_job_ids") or observation.get("unknown_job_ids")
            or goal.get("objective") != "materials_to_script" or goal.get("script_mode") != "generate"
            or goal.get("script_delivery_requirement") != "new_generation" or goal.get("required_analyses")
            or goal.get("required_reuse_analysis_refs") or goal.get("requested_modules")
            or prep_id != request.get("preparation_id") or ref_key(prep.get("draft_ref")) != ref_key(request["draft_ref"])):
        return None
    required = {ref_key(row["product_ref"]): row["count"] for row in request["product_counts"]}
    submissions = {op.get("receipt", {}).get("data", {}).get("job_id") for op in operations
                   if op.get("run_id") == run["id"] and op.get("key") == "submit_script_change"
                   and op.get("status") == "committed"}
    jobs = [job["body"] for job in observation.get("jobs", []) if job.get("ref", {}).get("id") in submissions]
    if not jobs or len(jobs) != len(submissions):
        return None
    minimum = request.get("minimum_spoken_characters")
    duration = request["target_duration_seconds"]
    low, high = (duration[key] * _TIME_SCALE["秒"] for key in ("min", "max"))
    items, seen, counts, failed_jobs, successes = [], set(), Counter(), [], {}
    for job in jobs:
        if job.get("kind") != "scripts" or job.get("run_id") != run["id"] or job.get("preparation_id") != prep_id:
            return None
        if job.get("status") == "failed":
            failed_jobs.append(job)
            continue
        if job.get("status") != "succeeded":
            return None
        for item in job.get("items", []):
            spec, output = item.get("input", {}), item.get("output") or {}
            ref, product_ref = output.get("script_ref"), spec.get("product_ref")
            key = _version_key(spec)
            if (item.get("status") != "succeeded" or item.get("call_state") in _UNSETTLED
                    or any(attempt.get("state") in _UNSETTLED for attempt in item.get("provider_attempts", []))
                    or key is None or key in successes or not ref or ref not in prep.get("script_refs", [])
                    or ref_key(ref) in seen or not product_ref):
                return None
            openings = output.get("content_diagnostics", {}).get("by_opening", {})
            if len(openings) != 1:
                return None
            count = next(iter(openings.values())).get("spoken_characters")
            if type(count) is not int or count <= 0:
                return None
            status = "below_target" if count < low else "above_target" if count > high else "within_target"
            seen.add(ref_key(ref))
            counts[ref_key(product_ref)] += 1
            successes[key] = job.get("generation_epoch"), ref_key(job.get("plan_ref"))
            items.append({"script_ref": ref, "product_ref": product_ref, "version_number": spec.get("version_number"),
                          "spoken_characters": count, "length_status": status,
                          "minimum_met": count >= minimum if minimum else None})
    return items if dict(counts) == required and all(_replaced_empty_failure(job, successes) for job in failed_jobs) else None


def delivery_quality(items, request):
    minimum = request.get("minimum_spoken_characters")
    unmet = [{"code": "SCRIPT_MINIMUM_SPOKEN_CHARACTERS_UNMET", "script_ref": item["script_ref"],
              "minimum": minimum, "actual": item["spoken_characters"]}
             for item in items if item["minimum_met"] is False]
    pending = bool(request.get("require_semantic_review"))
    if pending:
        unmet.append({"code": "SCRIPT_SEMANTIC_REVIEW_PENDING"})
    return {"delivery_quality": "partial" if unmet else "complete", "unmet_requirements": unmet,
            "script_length": items, "semantic_review_status": "pending" if pending else "not_requested"}
