"""Exact research reuse obligations, separate from executing new research.

The frozen Goal names requirements; the Voice ContextBundle owns the body.
Neither a user_decision string nor a plan's reuse_refs proves that body was used.
"""
from __future__ import annotations

from shengbo_contracts.common import ContractError, canonical_hash, same_artifact_ref
from .action_contracts import uncommitted_input_error


def required_reuse_refs(run):
    return list((run.get("goal") or {}).get("required_reuse_analysis_refs", []))


def _contains(references, ref):
    return any(same_artifact_ref(value, ref) for value in references)


def report_binding_issue(record, reference, products, preparation_id):
    """Pure validation of a complete, exact formal report and product scope."""
    if not isinstance(record, dict) or not same_artifact_ref(record.get("ref"), reference):
        return "研究精确引用不一致"
    body = record.get("body") or {}
    if canonical_hash(body) != reference.get("content_hash"):
        return "研究完整正文与引用哈希不一致"
    if reference.get("kind") != "analysis" or body.get("accepted") is not True:
        return "研究尚未正式验收"
    if body.get("preparation_id") not in {None, preparation_id}:
        return "研究不属于当前本场或全局商品研究"
    report = body.get("report")
    if not isinstance(report, dict) or not report:
        return "研究缺少完整报告正文"
    dependencies = [*body.get("product_refs", []), *body.get("readset", []), *report.get("dependencies", [])]
    product_dependencies = [ref for ref in dependencies if isinstance(ref, dict) and ref.get("kind") == "product"]
    if not product_dependencies or any(not _contains(products, ref) for ref in product_dependencies):
        return "研究未绑定当前精确商品版本"
    if set(report.get("product_ids", [])) - {ref["id"] for ref in products}:
        return "研究声明了当前范围以外的商品"
    return None


def bundle_binding_issues(bundle, required, preparation_id):
    body = (bundle or {}).get("body") or {}
    if (body.get("preparation_id") != preparation_id
            or canonical_hash(body) != (bundle or {}).get("ref", {}).get("content_hash")):
        return [{"reason": "缺少当前本场可核验的完整冻结上下文", "reference": ref} for ref in required]
    issues = []
    for ref in required:
        if not _contains(body.get("analysis_refs", []), ref):
            issues.append({"reference": ref, "reason": "精确研究未进入analysis_refs"})
            continue
        record = next((record for record in body.get("records", [])
                       if same_artifact_ref(record.get("ref"), ref)), None)
        reason = report_binding_issue(record, ref, body.get("product_refs", []), preparation_id)
        if reason:
            issues.append({"reference": ref, "reason": reason})
    return issues


def _missing(command, issues):
    raise uncommitted_input_error("REQUIRED_ANALYSIS_BINDING_MISSING",
        "指定正式研究尚未完整进入此动作使用的冻结上下文，未提交本次动作", details={
            "command": command, "issues": issues, "automatic_research_started": False,
            "required_action": {"tool": "wf_context_bundle", "action": "save",
                "analysis_refs": [issue["reference"] for issue in issues]},
            "correction": "受控读取核验指定精确研究，显式绑定正文后重建当前计划；不能仅写user_decisions、改用其他报告或为此重做研究"})


async def guard_research_bindings(engine, scope, run, command, args):
    """Pre-send only. Return a frozen context witness for manual patch audit."""
    required = required_reuse_refs(run)
    prep_id = run["scope"].get("preparation_id")
    if command == "save_context_bundle":
        supplied = args.get("analysis_refs", [])
        if not supplied and not required:
            return None
        absent = [{"reference": ref, "reason": "指定引用不在待保存analysis_refs中"}
                  for ref in required if not _contains(supplied, ref)]
        if absent:
            _missing(command, absent)
        # Existing adopted source-only facts retain their receipt-based legacy
        # binding. Explicit historical additions and required reuse are strict.
        preparation = await engine.client.query("read_preparation", {"preparation_id": prep_id},
            scope=run["scope"], run_id=run["id"])
        adopted = preparation.get("body", {}).get("analysis_refs", [])
        for ref in supplied:
            if _contains(adopted, ref) and not _contains(required, ref):
                continue
            # This reader performs bounded discovery plus all existing ACLs.
            try:
                record = await engine.business_query(scope, "read_context", {"ref": ref})
            except ContractError as error:
                if (error.code != "ANALYSIS_REFERENCE_UNRESOLVED" or error.status != 409
                        or error.details.get("read_state") != "not_read"):
                    raise
                raise uncommitted_input_error("ANALYSIS_BINDING_INVALID", "指定研究尚未在当前授权范围内找到，未保存上下文",
                    details={"command": command, "reference": ref, "read_state": "not_read",
                             "automatic_research_started": False}) from None
            reason = report_binding_issue(record, ref, args.get("product_refs", []), prep_id)
            if reason:
                raise uncommitted_input_error("ANALYSIS_BINDING_INVALID", reason,
                    details={"command": command, "reference": ref, "automatic_research_started": False})
        return None
    if not required or command not in {"save_script_plan", "submit_script_change", "patch_script", "import_script"}:
        return None
    context_ref = args.get("bundle_ref") or args.get("context_bundle_ref")
    if not context_ref:
        preparation = await engine.client.query("read_preparation", {"preparation_id": prep_id},
            scope=run["scope"], run_id=run["id"])
        context_ref = preparation.get("body", {}).get("bundle_ref")
    if not context_ref:
        _missing(command, [{"reference": ref, "reason": "本场尚无冻结ContextBundle"} for ref in required])
    bundle = await engine.client.query("read_context", {"ref": context_ref}, scope=run["scope"], run_id=run["id"])
    issues = bundle_binding_issues(bundle, required, prep_id)
    if issues:
        _missing(command, issues)
    return bundle["ref"]


async def verify_research_reuse(client, run, operations, observation):
    """Verify current bundle AND contexts actually frozen before script work."""
    required = required_reuse_refs(run)
    if not required:
        return {"missing": [], "required_refs": [], "checked_context_refs": []}
    prep_id = run["scope"].get("preparation_id")
    current = observation.get("preparation", {}).get("body", {}).get("bundle_ref")
    contexts = [current] if current else [None]
    missing, checked = [], []
    jobs = {job.get("ref", {}).get("id"): job.get("body", {}) for job in observation.get("jobs", [])}
    for op in operations:
        if op.get("run_id") != run["id"] or op.get("status") != "committed":
            continue
        if op.get("key") == "submit_script_change":
            frozen = op.get("args", {}).get("context_bundle_ref")
            contexts.append(frozen)
            job = jobs.get(op.get("receipt", {}).get("data", {}).get("job_id"))
            if job is not None and not same_artifact_ref(job.get("context_ref"), frozen):
                missing.append("实际文案任务的冻结研究上下文与提交引用不一致")
        elif op.get("key") in {"patch_script", "import_script"}:
            contexts.append(op.get("research_context_ref"))
    for ref in contexts:
        if ref and _contains(checked, ref):
            continue
        try:
            bundle = await client.query("read_context", {"ref": ref}, scope=run["scope"], run_id=run["id"]) if ref else None
            issues = bundle_binding_issues(bundle, required, prep_id)
        except ContractError:
            issues = [{"reference": reference, "reason": "冻结上下文不可核验"} for reference in required]
        if issues:
            missing.extend("指定复用研究未进入实际话术冻结上下文：" + issue["reference"]["id"] + "（" + issue["reason"] + "）"
                           for issue in issues)
        elif ref:
            checked.append(ref)
    return {"missing": list(dict.fromkeys(missing)), "required_refs": required, "checked_context_refs": checked,
            "semantic_support_verified": False}
