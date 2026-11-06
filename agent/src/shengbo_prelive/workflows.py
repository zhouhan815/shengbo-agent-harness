"""Optional, small business tool flows. No outer pipeline or second runtime.

The model selects a flow and its action. Dependencies and unknown outcomes are
returned to that model; none of these functions changes the task's Goal, grants,
human decisions or content plan. Every write uses RuntimeEngine.business_command.
"""
from __future__ import annotations

import time
from copy import deepcopy
from typing import Any, Literal

from pydantic import Field
from shengbo_contracts.common import ArtifactRef, RecordRef, ContractError, StrictModel, canonical_hash, now_iso
from .observability import sanitize


class MaterialsFlowInput(StrictModel):
    action: Literal["inspect", "assess", "save", "save_from_facts", "select"] = "inspect"
    source_refs: list[ArtifactRef] = Field(default_factory=list, max_length=64)
    detail: Literal["auto", "overview", "full"] = Field(default="auto",
        description="auto小资料正文只返回一次；复杂长材料先索引，按read_args取sheet/块或图片。full显式重读原文。")
    query: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict,
        description="save: 原save_product参数。save_from_facts: branch_task_id、精确accepted_report_ref，可选精确product_ref及显式overrides，原样使用已验收事实字段，不自动提炼或改写。两种save只写商品库；select: update_preparation_products参数，selected_product_refs须明确给保留原项的完整精确清单，服务端读本场最新CAS。inspect无需参数。")


class AnalysisFlowInput(StrictModel):
    action: Literal["inspect", "dispatch", "accept", "bind"] = "inspect"
    branch_task_ids: list[str] = Field(default_factory=list, max_length=30)
    parameters: dict[str, Any] = Field(default_factory=dict,
        description="dispatch为tasks/reason；bind为product_ref和validation_note，只选尚未绑定的product_facts/audience_scenes，session_config不绑定。有注释须逐条审阅具体report_hash，并给conflict_reviews=[{branch_task_id,report_hash,issue_dispositions:[{issue_index,issue_hash,classification:'nonblocking_boundary_note'|'unresolved',rationale,basis_refs:[精确ArtifactRef或RecordRef]}]}]覆盖全部注释。issue_hash=canonical_hash(原conflicts[index])，可用返回的review_inputs。basis_refs只从对应review_inputs.allowed_basis_refs选取，不能拿analysis_ref或任务ID代替事实来源；unresolved及明确事实/来源hash/价格矛盾仍拒绝，不自动验收或重跑研究。")


class SellingPointsFlowInput(StrictModel):
    action: Literal["inspect", "offer", "propose", "save"] = "inspect"
    parameters: dict[str, Any] = Field(default_factory=dict,
        description="inspect/offer的parameters仅{product_ref:精确商品引用}，不接受direction或items；offer记录生成意愿，propose前须有其正式generate回执，不能以文本同意冒充。propose给product_ref、items，items每项selling_point/audience_need/use_scene/limitations均为字符串（limitations不能数组），evidence为[{product_field,fact}]数组，可选research_refs/direction；只用当前商品事实，研究不能补造性能。候选等待确认。save仅给proposal_id，原样保存已批准候选，不改其他字段或话术。")


class ContextFlowInput(StrictModel):
    action: Literal["inspect", "save"] = "inspect"
    detail: Literal["index", "plan", "full"] = Field(default="index",
        description="inspect默认index：完整商品/草案/精确引用与研究摘要、章节回读路径；plan补齐可读研究计划字段；full显式全文。save始终绑定完整正式原文，不受detail影响。")
    user_decisions: list[dict[str, Any]] = Field(default_factory=list)
    source_refs: list[ArtifactRef] = Field(default_factory=list)
    analysis_refs: list[ArtifactRef] = Field(default_factory=list, max_length=64,
        description="显式追加绑定已正式验收、精确商品版本及读取范围合法的既有研究。与本场原analysis_refs合并，不替换或重做；save绑定完整正文，user_decisions中的ID不算绑定。")


class ScriptFlowInput(StrictModel):
    action: Literal["execute", "status", "retry", "review"] = "status"
    plan_ref: ArtifactRef | None = None
    job_id: str | None = None
    recovery_mode: Literal["retry", "revalidate_archived"] = "retry"


class ReviewFlowInput(StrictModel):
    action: Literal["inspect", "prepare_order", "save_playlist", "save_assets", "compile", "review"] = "inspect"
    parameters: dict[str, Any] = Field(default_factory=dict,
        description="prepare_order: ordered_script_refs为主Agent明确选择的精确话术引用顺序，可选expected_preparation_ref/prompt；仅装配并核验提案，不提问或保存。save_playlist使用原参数；save_assets使用checklist。compile/review无需参数，均不替代人工决定。")


class AudioFlowInput(StrictModel):
    action: Literal["inspect", "prepare", "retry"] = "inspect"
    job_id: str | None = None


class FinalizeFlowInput(StrictModel):
    expected_preparation_ref: RecordRef | ArtifactRef | None = None


WORKFLOW_SCHEMAS = {
    "wf_product_materials": MaterialsFlowInput,
    "wf_selling_points": SellingPointsFlowInput,
    "wf_analysis_coordination": AnalysisFlowInput,
    "wf_context_bundle": ContextFlowInput,
    "wf_script_execution": ScriptFlowInput,
    "wf_playback_review": ReviewFlowInput,
    "wf_required_audio": AudioFlowInput,
    "wf_finalize_preparation": FinalizeFlowInput,
}
WORKFLOW_EFFECTS = {
    "wf_product_materials": {"save_product", "update_preparation_products"},
    "wf_selling_points": {"save_product"},
    "wf_analysis_coordination": {"accept_analysis_report"},
    "wf_context_bundle": {"save_context_bundle"},
    "wf_script_execution": {"submit_script_change"},
    "wf_playback_review": {"save_playlist", "compile_playback_plan", "save_asset_checklist"},
    "wf_required_audio": {"prepare_required_audio"},
    "wf_finalize_preparation": {"publish_preparation"},
}


class Flow:
    def __init__(self, engine, scope, name, args):
        self.engine, self.scope, self.name, self.args = engine, scope, name, args
        self.id = "flow_" + canonical_hash([scope.run_id, name, args])[7:39]
        self.completed = []
        self.started = time.perf_counter()

    async def step(self, name, callback, parameters=None):
        await self.engine.assert_scope(self.scope)
        started = time.perf_counter()
        self.engine._emit(self.scope.run_id, "workflow.step.started", {
            "title": name, "flow_id": self.id, "workflow": self.name,
            "step": name, "parameters": sanitize(parameters or {}), "started_at": now_iso()}, self.scope)
        try:
            value = await callback()
        except BaseException as error:
            self.engine.trace.try_emit(self.scope.run_id, "workflow.step.failed", {
                "title": name, "flow_id": self.id, "workflow": self.name, "step": name,
                "duration_ms": round((time.perf_counter()-started)*1000, 3),
                "error_code": getattr(error, "code", type(error).__name__)},
                invocation_id=self.scope.invocation_id, branch_task_id=self.scope.branch_task_id)
            raise
        evidence = self.engine.trace.payload(self.scope.run_id, sanitize(value))
        self.completed.append(name)
        self.engine._emit(self.scope.run_id, "workflow.step.completed", {
            "title": name, "flow_id": self.id, "workflow": self.name, "step": name,
            "duration_ms": round((time.perf_counter()-started)*1000, 3), "payload_ref": evidence}, self.scope)
        return value

    async def query(self, key, args=None):
        return await self.step(key, lambda: self.engine.business_query(self.scope, key, args or {}), args)

    async def prep(self):
        run = await self.engine.assert_scope(self.scope)
        identity = run["scope"].get("preparation_id")
        if not identity:
            raise ContractError("PREPARATION_REQUIRED", "此工具流需要本次已授权的本场")
        return await self.query("read_preparation", {"preparation_id": identity})

    async def command(self, key, args, *, version_key=None):
        # Stable per logical inputs, across different model tool-call IDs. The
        # business fence still verifies scope, target, CAS and approvals.
        logical = self.id + ":" + key + ":" + canonical_hash([args, version_key])
        return await self.step(key, lambda: self.engine.business_command(self.scope, key, args, logical), args)

    def result(self, status="COMPLETED", *, result=None, blockers=None, remaining=None):
        value = {"flow_id": self.id, "version": "v1", "workflow": self.name,
            "status": status, "input_refs": self._refs(self.args), "completed_steps": self.completed,
            "remaining_steps": remaining or [], "blockers": blockers or [],
            "result": result or {}, "committed_refs": self._refs(result or {}),
            "resume_ref": {"run_id": self.scope.run_id, "flow_id": self.id},
            "duration_ms": round((time.perf_counter()-self.started)*1000, 3)}
        ref = self.engine.trace.payload(self.scope.run_id, value)
        value["evidence_ref"] = ref
        self.engine.store.put("workflow", {"id": self.id, "run_id": self.scope.run_id,
            "workflow": self.name, "status": status, "completed_steps": self.completed,
            "evidence_ref": ref, "updated_at": now_iso()})
        self.engine._emit(self.scope.run_id, "workflow.completed", {
            "title": self.name, "flow_id": self.id, "workflow": self.name,
            "status": status, "duration_ms": value["duration_ms"], "payload_ref": ref}, self.scope)
        return sanitize(value)

    @staticmethod
    def _refs(value):
        found = []
        def walk(v):
            if isinstance(v, dict):
                if {"kind", "id", "revision", "content_hash"} <= set(v):
                    if v not in found:
                        found.append(v)
                else:
                    for child in v.values(): walk(child)
            elif isinstance(v, list):
                for child in v: walk(child)
        walk(value)
        return found


async def run_workflow(engine, scope, name, args, *, tool_call_id=None):
    from .policy import check_tool_allowed
    run = await engine.assert_scope(scope)
    check_tool_allowed(scope, run, name)
    if scope.role != "main" or scope.phase != "execute":
        raise ContractError("TOOL_SCOPE_DENIED", "工具流限本次主Agent已授权的执行阶段", status=403)
    data = WORKFLOW_SCHEMAS[name].model_validate(args).model_dump(mode="json", exclude_none=True)
    f = Flow(engine, scope, name, data)
    if name == "wf_selling_points" and tool_call_id:
        # Human edit/reject decisions are scoped to one actual invocation.
        # New model calls may submit the same text for a fresh review, while
        # interrupt replay of that call must return its original answer.
        f.id = "flow_" + canonical_hash([scope.run_id, name, tool_call_id, data])[7:39]
    engine._emit(scope.run_id, "workflow.started", {"title": name, "workflow": name,
        "flow_id": f.id, "parameters": data}, scope)
    try:
        return await _HANDLERS[name](f, data)
    except ContractError as error:
        from .action_contracts import recoverable_input_error
        unknown = engine.store.get("run", scope.run_id).get("pending_unknown_operation")
        if recoverable_input_error(error, pending_unknown=bool(unknown)):
            # M5 gives both native and workflow tools the same bounded correction.
            # Persist the interrupted helper's real progress as well; a thrown
            # tool error must not leave its UI/history permanently 'started'.
            f.result("NEEDS_REPLAN", blockers=[sanitize(error.as_error().model_dump(mode="json"))],
                remaining=["由模型按原目标修正输入或另选合法工具；没有自动业务动作"])
            raise
        if unknown and error.status not in {401, 403}:
            return f.result("OUTCOME_UNKNOWN", blockers=[sanitize(error.as_error().model_dump(mode="json"))],
                remaining=["先对账原操作，不能盲重发"])
        raise



async def materials(f, a):
    if a["action"] == "assess":
        return f.result(result=await f.step("assess_material_readiness",
            lambda: f.engine.material_readiness(f.scope, a["parameters"]), a["parameters"]))
    if a["action"] == "inspect":
        run = await f.engine.assert_scope(f.scope)
        refs = a["source_refs"] or run["input"].get("material_refs", [])
        section = {"auto": None, "overview": "overview", "full": "full"}[a["detail"]]
        documents = [await f.step("read_source", lambda ref=ref: f.engine.read_source(f.scope, ref, section), {"ref": ref, "section": section}) for ref in refs]
        schema = await f.query("get_business_schema")
        candidates = await f.query("search_products", {"q": a["query"], "limit": 20}) if a["query"] else None
        result = {"documents": documents, "schema": schema, "identity_candidates": candidates,
            "note": "请主Agent/商品事实分支整理完整配置与缺项，检查歧义；此动作未写商品。商品库保存与本场选品是两个独立动作"}
        if run["scope"].get("preparation_id"):
            result["preparation_selection"] = preparation_dependencies(await f.prep())
        return f.result(result=result)
    if a["action"] == "save_from_facts":
        from .product_materials import SaveAcceptedProductInput, save_accepted_product
        candidate = SaveAcceptedProductInput.model_validate(a["parameters"]).model_dump(mode="json", exclude_none=True)
        receipt = await f.step("save_accepted_product", lambda: save_accepted_product(
            f.engine, f.scope, candidate, tool_call_id=f.id), candidate)
    else:
        key = "save_product" if a["action"] == "save" else "update_preparation_products"
        parameters = a["parameters"]
        if key == "update_preparation_products":
            prep = await f.prep()
            parameters = {"preparation_id": prep["ref"]["id"], "expected_revision": prep["ref"]["revision"], **parameters}
        receipt = await f.command(key, parameters)
    # Preserve the receipt/data path. This readback supplies the next exact CAS
    # and an optional recovery entry; it never selects a saved product itself.
    result = dict(receipt)
    if a["action"] in {"save", "save_from_facts"}:
        # Project the committed command; do not re-read the product or mutate its receipt.
        operation = f.engine.store.get("operation", receipt.get("operation_id", ""), required=False)
        fields = (operation or {}).get("args", {}).get("fields", {})
        product_ref = receipt.get("data", {}).get("product_ref")
        if fields and product_ref and not fields.get("core_selling_points") and not str(fields.get("selling_points", "")).strip():
            result["selling_points_completion"] = {"status": "missing", "product_ref": product_ref,
                "note": "基础商品已保存，核心卖点尚未整理；可询问用户是否让AI依据事实与顾客研究提炼。缺商品事实不能靠AI补造。",
                "next_action": {"tool": "wf_selling_points", "action": "offer", "parameters": {"product_ref": product_ref}}}
    run = await f.engine.assert_scope(f.scope)
    if run["scope"].get("preparation_id"):
        candidate = receipt.get("data", {}).get("product_ref")
        result["preparation_selection"] = preparation_dependencies(await f.prep(),
            candidate_product_refs=[candidate] if candidate else [])
    return f.result(result=result)


def preparation_dependencies(prep, *, candidate_product_refs=None):
    """A deterministic, read-only hint, not an automatic business transition."""
    body, ref = prep["body"], prep["ref"]
    selected = deepcopy(body.get("selected_product_refs") or [])
    candidates = deepcopy(candidate_product_refs or [])
    unselected = [candidate for candidate in candidates if candidate not in selected]
    next_actions = []
    if not selected or unselected:
        next_actions.append({"tool": "wf_product_materials", "action": "select",
            "preparation_ref": deepcopy(ref),
            "required_parameters": ["parameters.selected_product_refs"],
            "current_selected_product_refs": selected, "available_exact_product_refs": candidates,
            "constraint": "主Agent只在本次Goal允许时显式给出完整精确选品清单，保留原已选商品；商品身份或授权不明则请求用户决定。不会自动选品或刷新旧版本。"})
    if not body.get("draft_ref"):
        next_actions.append({"tool": "save_session_draft",
            "args": {"preparation_id": ref["id"], "expected_revision": ref["revision"]},
            "required_parameters": ["draft"], "depends_on": "活动引用的商品必须已选入本场",
            "constraint": "本场草案来自用户资料与已验收分析；任何选品成功后重新使用返回的最新revision，不猜版本。"})
    return {"preparation_ref": deepcopy(ref), "current_selected_product_refs": selected,
        "available_exact_product_refs": candidates, "unselected_candidate_refs": unselected,
        "draft_ref": deepcopy(body.get("draft_ref")), "automatic_action_taken": False,
        "next_actions": next_actions,
        "refresh_query": {"tool": "read_preparation", "args": {"preparation_id": ref["id"]}}}


async def analyses(f, a):
    if a["action"] == "dispatch":
        from .tools import DispatchInput
        dispatch = DispatchInput.model_validate(a["parameters"])
        value = await f.step("dispatch_analysis_batch", lambda: f.engine.dispatch(f.scope, dispatch.tasks, dispatch.reason, f.id), a["parameters"])
        return f.result("WAITING", result=value)
    if a["action"] == "bind":
        value = await f.step("bind_analysis_product", lambda: f.engine.bind_source_analysis(
            f.scope, a["branch_task_ids"], a["parameters"], f.id), a["parameters"])
        return f.result(result=value)
    batch = await f.step("read_analysis_batch", lambda: _async_value(f.engine.read_batch_view(f.scope, a["branch_task_ids"])))
    if a["action"] == "accept":
        value = await f.step("accept_analysis_reports", lambda: f.engine.accept_reports(f.scope, a["branch_task_ids"], f.id))
        return f.result(result=value)
    return f.result(result=batch)


async def context(f, a):
    prep = await f.prep()
    b = prep["body"]
    if not b.get("draft_ref") or not b.get("selected_product_refs"):
        missing = []
        if not b.get("selected_product_refs"):
            missing.append({"code": "PREPARATION_PRODUCTS_REQUIRED", "field": "selected_product_refs",
                "message": "商品库内已有商品也需由主Agent显式选择进入本场"})
        if not b.get("draft_ref"):
            missing.append({"code": "SESSION_DRAFT_REQUIRED", "field": "draft_ref", "message": "缺少本场已保存草案"})
        return f.result("NEEDS_REPLAN", blockers=missing,
            result={"context_inputs": prep.get("context_inputs"), "preparation_selection": preparation_dependencies(prep)},
            remaining=["主Agent按原Goal与精确引用选择缺失动作；当前未保存上下文，不会自动选品或配置"])
    if a["action"] == "save":
        parameters = dict(prep["context_inputs"])
        parameters["user_decisions"] = a["user_decisions"]
        parameters["analysis_refs"] = list(parameters.get("analysis_refs", []))
        for ref in a.get("analysis_refs", []):
            if not any(all(old.get(field) == ref.get(field) for field in ("kind", "id", "revision", "content_hash"))
                       for old in parameters["analysis_refs"]):
                parameters["analysis_refs"].append(ref)
        for ref in a["source_refs"]:
            if ref not in parameters["source_refs"]: parameters["source_refs"].append(ref)
        receipt = await f.command("save_context_bundle", parameters)
        return f.result(result=receipt)
    # Queries and their trace remain complete. Only the model-facing view is
    # selected; formal bundle creation below still uses preparation refs.
    products = [await f.query("read_product", {"ref": ref}) for ref in b["selected_product_refs"]]
    draft = await f.query("read_context", {"ref": b["draft_ref"], "sections": ["draft"]})
    research = [await f.query("read_analysis_report", {"ref": ref}) for ref in b.get("analysis_refs", [])]
    recaps = [await f.query("read_recap", {"ref": ref}) for ref in b.get("recap_refs", [])]
    inputs = {"context_inputs": prep.get("context_inputs"), "products": products,
        "research": research, "draft": draft, "recaps": recaps,
        "prior_script_refs": b.get("script_refs", []), "note": "主Agent按本次目标决定生成/局改/复用；原文完整保留"}
    if a["detail"] == "full":
        return f.result(result=inputs)
    run = await f.engine.assert_scope(f.scope)
    full_ref = f.engine._snapshot_context(run, {"kind": "workflow_context_inputs", "inputs": inputs,
        "branch": {"id": f.scope.branch_task_id}})
    return f.result(result=_context_inputs_view(inputs, a["detail"], full_ref))


def _context_inputs_view(inputs, detail, full_ref):
    """Deterministic delivery view; never replaces a formal business record.

    Index uses existing summaries, not a model-written new summary. Plan retains
    every report key except top-level evidence, whose exact source remains
    addressable. Equal planning content may be shared only after exact equality;
    each distinct formal ref and its dependency/product binding is retained.
    """
    view = deepcopy(inputs)
    shared = {}
    for position, record in enumerate(view.get("research", [])):
        original = inputs["research"][position]
        body = record.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("report"), dict):
            continue
        report = original["body"]["report"]
        path = f"/inputs/research/{position}"
        body.pop("lineage", None)
        body.pop("source_ref_replacements", None)
        record["full_ref"] = deepcopy(full_ref)
        record["full_path"] = path
        record["report_path"] = path + "/body/report"
        record["available_report_sections"] = list(report)
        if detail == "index":
            body["report"] = {"summary": deepcopy(report.get("summary")), "index_only": True,
                **{key: deepcopy(report[key]) for key in ("dependencies", "source_refs", "product_ids") if key in report}}
        else:
            # These fields vary with accepted product/source binding, even when
            # every planning conclusion is exactly equal. Never merge bindings.
            bindings = {key: deepcopy(report[key]) for key in ("dependencies", "source_refs", "product_ids") if key in report}
            content = {key: deepcopy(value) for key, value in report.items()
                       if key not in {"evidence", "dependencies", "source_refs", "product_ids"}}
            digest = canonical_hash(content)
            prior = shared.get(digest)
            if prior is not None and prior["content"] == content:
                body["report"] = {**bindings, "shared_content": {
                    "research_index": prior["position"], "content_hash": digest,
                    "view_path": f"/research/{prior['position']}/body/report"}}
            else:
                body["report"] = {**content, **bindings}
                shared[digest] = {"position": position, "content": content}
            record["planning_content_hash"] = digest
    view.update(detail=detail, full_ref=deepcopy(full_ref),
        readback={"tool": "read_run_history", "ref": deepcopy(full_ref),
            "root_path": "/inputs", "context_inputs_path": "/inputs/context_inputs",
            "research_path": "/inputs/research", "section_path_rule": "report_path + '/' + 原始章节key(JSON pointer转义)"},
        projection_contract="展示视图不是业务正本；完整商品、完整草案和context_inputs原样返回。研究已验收未变时可按精确版本复用，不强制重读；需要具体内容时用每条full_ref及report_path/章节路径调用read_run_history，可分页，或显式detail=plan/full。full_ref指向本次完整只读结果，不授予写入、验收或批准。save始终绑定完整正式原文，文案后端仍读取完整上下文。")
    # Keep any exact reference that existed only in omitted provenance. This is
    # an index, not a claim that its body was read/accepted in this invocation.
    delivered = Flow._refs(view)
    retained = [ref for ref in Flow._refs(inputs) if ref not in delivered]
    if retained:
        view["additional_exact_refs"] = retained
    return view


def script_recovery_options(job, outcome):
    """Describe a controlled retry option, without granting or executing it."""
    if outcome != "NEEDS_REPLAN" or not isinstance(job, dict):
        return []
    body, counts = job.get("body"), job.get("items_projection", {})
    if (not isinstance(body, dict) or body.get("kind") != "scripts"
            or body.get("status") not in {"failed", "partially_failed"}
            or not isinstance(counts, dict)
            or any(counts.get(key) for key in ("unknown_item_count", "unknown_provider_attempt_count", "in_flight_provider_attempt_count"))):
        return []
    try:
        ref = ArtifactRef.model_validate(job.get("ref"))
    except (ValueError, TypeError):
        return []
    if ref.kind != "prelive_job" or body.get("id") != ref.id:
        return []
    items, attention = body.get("items", []), job.get("attention_items", [])
    if not isinstance(items, list) or not isinstance(attention, list):
        return []
    rejection_codes = {"MODULE_RESPONSE_INVALID", "MODULE_RESPONSE_MISMATCH", "WHOLE_SCRIPT_RESPONSE_INVALID", "WHOLE_SCRIPT_RESULT_MISMATCH", "SHORT_MODULE_TOO_LONG",
        "GENERATED_MODULE_NEEDS_REVIEW", "INVALID_MODULE_TEXT", "DUPLICATE_MODULE_SENTENCE", "MODULE_DETAILS_MISMATCH"}
    observed = set()
    for item in [*items, *attention]:
        if not isinstance(item, dict) or item.get("call_state") in {"sent", "unknown", "outcome_unknown"}:
            return []
        attempts = item.get("provider_attempts", [])
        if not isinstance(attempts, list):
            return []
        for attempt in attempts:
            if (not isinstance(attempt, dict) or attempt.get("state") not in
                    {"response_received", "result_received", "known_failed", "not_sent", "reserved"}):
                return []  # Never infer a retryable outcome from missing/unknown state.
            error = attempt.get("validation_error")
            if (attempt["state"] in {"response_received", "result_received"}
                    and attempt.get("validation_state") == "rejected" and isinstance(error, dict)
                    and error.get("code") in rejection_codes):
                observed.add(error["code"])
    if not observed:
        return []
    return [{"option": "request_controlled_retry_of_original_job", "tool": "wf_script_execution",
        "arguments": {"action": "retry", "job_id": ref.id},
        "observed_validation_rejection_codes": sorted(observed), "execution_authorized": False,
        "note": "仅为主Agent可选择的原job受控重试入口，也可核查或局改，不自动执行。原error.retryable=false及公开响应仍保留；该字段不能单独证明后端永不允许受控重试。实际工具仍重新核验Goal、权限、CAS/当前输入、未知结果及后端可重试条件，不合法会拒绝。已提交module_ref继续保留；rejected原文不是正式模块。"}]


def script_output_read_options(job, outcome):
    """Bounded hints from an already verified workset; no new read or action."""
    if outcome != "NEEDS_REPLAN" or not isinstance(job, dict):
        return []
    workset = job.get("recovery_workset")
    if not isinstance(workset, dict) or workset.get("status") != "verified_job_snapshot":
        return []
    if workset.get("outcome_unknown_count") or workset.get("in_flight_count"):
        return []
    try:
        ref = ArtifactRef.model_validate(job.get("ref"))
    except (ValueError, TypeError):
        return []
    if ref.kind != "prelive_job" or workset.get("job_ref") != job.get("ref"):
        return []
    from .generation_output_reads import GenerationOutputReadInput
    options = []
    for row in workset.get("modules", []):
        if (not isinstance(row, dict) or row.get("status") != "validation_rejected"
                or not row.get("public_response_reader")):
            continue
        try:
            arguments = GenerationOutputReadInput(job_id=ref.id, item_id=row.get("item_id"),
                module_key=row.get("module_key"), view="latest_failed_response").model_dump(exclude_none=True)
        except (ValueError, TypeError):
            continue
        options.append({"tool": "read_generation_output", "arguments": arguments,
            "execution_authorized": False, "approval_implied": False,
            "note": "可选读取单个失败模块的最新归档正文；未展开时activate_tools(names=['read_generation_output'])。实际读取重新核验作用域及档案，不重试、不批准。"})
        if len(options) == 4:
            break
    return options


async def scripts(f, a):
    if a["action"] == "retry":
        return await _retry(f, a)
    prep = await f.prep()
    b = prep["body"]
    run = f.engine.store.get("run", f.scope.run_id)
    observation = await f.step("read_completion_observation", lambda: f.engine._completion_observation(run))
    if a["action"] == "review":
        from .script_review_workset import script_review_workset
        value = await script_review_workset(f, run, prep, observation, job_id=a.get("job_id"))
        return f.result(value["status"], result=value, blockers=value["blockers"],
            remaining=value["remaining"])
    if a["action"] == "status":
        value = f.engine._completion_observation_view(run, observation)
        script_jobs = [job for job in observation.get("jobs", []) if job.get("body", {}).get("kind") == "scripts"]
        chosen = next((job for job in script_jobs if job.get("ref", {}).get("id") == a.get("job_id")), None)
        if chosen is None and not a.get("job_id") and script_jobs:
            chosen = max(script_jobs, key=lambda job: (job["body"].get("created_at", ""), job.get("ref", {}).get("id", "")))
        if chosen is not None and chosen["body"].get("status") in {"queued","running","succeeded"}:
            # The current voice-owned observation already contains this exact
            # ledger. Normal polling needs neither another remote job read nor
            # the archived copywriting response repeated into model context.
            value["existing_job"] = f.engine._job_view(run, chosen, include_provider_outputs=False)
        elif a.get("job_id") or chosen is not None and chosen["body"].get("status") in {"failed", "partially_failed", "interrupted", "cancelled", "blocked"}:
            # read_completion_observation carries stored ledger state. Only
            # read_job derives the association-checked public response reader.
            identity = a.get("job_id") or chosen["ref"]["id"]
            value["existing_job"] = await f.query("read_job", {"job_id": identity})
        actual_job = value.get("existing_job") or chosen or {}
        actual_body = actual_job.get("body", {})
        counts = actual_job.get("items_projection") or f.engine._job_item_counts(actual_body.get("items", []))
        unknown = (run.get("pending_unknown_operation") or observation.get("unknown_job_ids")
            or counts.get("unknown_item_count") or counts.get("unknown_provider_attempt_count"))
        failed = actual_body.get("status") in {"failed", "partially_failed", "interrupted", "cancelled", "blocked"}
        pending = (observation.get("pending_job_ids") or actual_body.get("status") in {"queued", "running"}
            or counts.get("in_flight_provider_attempt_count"))
        succeeded = actual_body.get("kind") == "scripts" and actual_body.get("status") == "succeeded"
        status = "OUTCOME_UNKNOWN" if unknown else "WAITING" if pending else "COMPLETED" if succeeded else "NEEDS_REPLAN"
        owned_job = actual_body.get("run_id") == run.get("id")
        options = script_recovery_options(actual_job, status) if owned_job else []
        if failed and not owned_job:
            value["historical_job_boundary"]={"owner_run_id":actual_body.get("run_id"),
                "current_run_id":run.get("id"),"retry_authorized":False,
                "note":"历史任务结果可读，但旧任务授权不继承；不能用当前任务重试其原job。主Agent可复用有效商品、研究和上下文，在当前目标内另建合法计划；不必重做无变化资料。"}
        if options:
            value["optional_next_actions"] = options
        reads = script_output_read_options(actual_job, status)
        if reads:
            value["optional_output_reads"] = reads
        return f.result(status, result=value,
            blockers=["原话术任务未完成或缺少真实succeeded任务证据；已提交模块仍保留，未自动重新生成"] if failed or status == "NEEDS_REPLAN" else [],
            remaining=["先看existing_job的rejected/unknown与public_response，长原文按result_ref/result_path回读。已提交模块保留；没有整版base_script_ref的partial不能直接新plan reuse，可选原job受控retry或保持未变模块原生成输入由后台核验缓存。由主Agent决定，公开原文不是正式module_ref"] if failed else [])
    plan = a.get("plan_ref") or b.get("plan_ref")
    if not plan or not b.get("bundle_ref"):
        return f.result("NEEDS_REPLAN", blockers=["请主Agent先保存当前上下文及明确模块变更计划"])
    if plan != b.get("plan_ref"):
        raise ContractError("GENERATION_INPUTS_CHANGED", "选择的计划不是本场当前计划")
    same = [j for j in observation.get("jobs", []) if j.get("body", {}).get("kind") == "scripts"
        and j.get("body", {}).get("plan_ref") == plan]
    if same:
        job = same[-1]
        status = job["body"]["status"]
        job_view = (await f.query("read_job", {"job_id": job["ref"]["id"]})
            if status not in {"queued", "running", "succeeded"} else f.engine._job_view(run, job))
        current_status = job_view.get("body", {}).get("status")
        counts = job_view.get("items_projection", {})
        unknown = (run.get("pending_unknown_operation") or observation.get("unknown_job_ids")
            or counts.get("unknown_item_count") or counts.get("unknown_provider_attempt_count"))
        pending = current_status in {"queued", "running"} or counts.get("in_flight_provider_attempt_count")
        outcome = "OUTCOME_UNKNOWN" if unknown else "WAITING" if pending else "COMPLETED" if current_status == "succeeded" else "NEEDS_REPLAN"
        result = {"existing_job": job_view, "reused": True}
        options = script_recovery_options(job_view, outcome)
        if options:
            result["optional_next_actions"] = options
        reads = script_output_read_options(job_view, outcome)
        if reads:
            result["optional_output_reads"] = reads
        return f.result(outcome,
            result=result,
            remaining=["已失败任务不自动重提；优先核对校验问题、公开响应及committed_modules精确refs，由主Agent选择局改或合法重试"] if status not in {"queued", "running", "succeeded"} else [])
    receipt = await f.command("submit_script_change", {"preparation_id": prep["ref"]["id"],
        "expected_revision": prep["ref"]["revision"], "plan_ref": plan, "context_bundle_ref": b["bundle_ref"],
        "expected_generation_epoch": b["generation_epoch"]})
    return f.result("WAITING", result=receipt)


def text_audio_approval_option():
    """An optional real HITL entry; no model-supplied binding or approval."""
    return {"option": "request_current_text_audio_approval", "tool": "request_user_decision",
        "arguments": {"kind": "text_audio_approval",
            "prompt": "请审阅当前整包文本、播放顺序和音色；批准后生成本次所需语音，不采用或开播。"},
        "approval_granted": False,
        "note": "需要语音时可选。若已合法审读当前稿，无需为了组装批准参数重新读取全部话术或重建bundle；只传kind和prompt，后端会读取当前整包并绑定真实review/hash/音色/顺序。此入口只申请人工确认，不批准；当前性、Goal、未知结果及真实人工回执仍由原门禁核验。"}


def review_inspection(prep, allowed_effects):
    """Observed dependencies and optional actions, not a readiness certificate.

    No extra query, authorization, ordering decision or write occurs here.
    Present references are deliberately not labelled current/approved.
    """
    body = prep["body"]
    observed = {key: deepcopy(body.get(key)) for key in
        ("script_refs", "playlist_ref", "compiled_plan_ref", "requirements_ref", "approval_ref")}
    scripts = observed["script_refs"] or []
    missing = [key for key, value in observed.items() if not value]
    actions = []
    if not scripts:
        actions.append({"option": "inspect_or_plan_scripts", "tool": "wf_script_execution",
            "arguments": {"action": "status"}, "required_goal_effect": "submit_script_change",
            "goal_effect_allowed": "submit_script_change" in allowed_effects,
            "note": "未观察到正式话术引用；由主Agent判断等待现有任务或在Goal内制定生成/局改计划，不发起空顺序确认。"})
    elif not observed["playlist_ref"]:
        actions.append({"option": "assemble_explicit_order_proposal", "tool": "wf_playback_review",
            "arguments_template": {"action": "prepare_order", "parameters": {"ordered_script_refs": "主Agent明确选择的完整精确话术ref序列"}},
            "available_script_refs": deepcopy(scripts), "order_proposed": False,
            "note": "可选机械装配器：你明确给序列，程序从正本核商品归属并生成条目ID和合法提问参数；不选择顺序、不提问、不批准。先完成必要内容审查，发现具体问题时仍可局改或补查。"})
        actions.append({"option": "request_exact_playlist_decision", "tool": "request_user_decision",
            "kind": "playlist_order", "available_script_refs": deepcopy(scripts),
            "required_context_paths": ["context.playlist.items", "context.playlist.loop_mode"],
            "order_proposed": False,
            "note": "由主Agent按用户选择构造完整精确context.playlist提案，再请求真实排序决定；本检查不选择顺序、条目或occurrence_id。答复不自动授予保存权限。"})
    elif not observed["compiled_plan_ref"] or not observed["requirements_ref"]:
        actions.append({"option": "compile_saved_playlist", "tool": "wf_playback_review",
            "arguments": {"action": "compile"}, "required_goal_effect": "compile_playback_plan",
            "goal_effect_allowed": "compile_playback_plan" in allowed_effects,
            "note": "仅为可选入口；原顺序确认、Goal、CAS、当前性和未知结果仍由原工具核验，不表示编译已获授权。"})
    if scripts and observed["playlist_ref"] and observed["compiled_plan_ref"] and observed["requirements_ref"]:
        actions.append({"option": "read_current_text_review", "tool": "read_text_review",
            "arguments": {"preparation_id": prep["ref"]["id"]},
            "note": "需要额外核查时可选；本inspect没有验证有效审核或批准。申请text_audio_approval会由后端另取当前整包并绑定，不要求模型先拼接review参数。"})
        actions.append(text_audio_approval_option())
    return {"action": "inspect", "inspection_completed": True,
        "preparation_readiness": "not_evaluated", "preparation_ref": deepcopy(prep["ref"]),
        "observed_dependencies": observed, "missing_dependency_fields": missing,
        "text_review": {"status": "not_verified", "reader": "read_text_review",
                        "note": "有效text-review需独立动态查询，ref存在不等于当前有效批准。"},
        "approval_observation": {"status": "present_not_verified" if observed["approval_ref"] else "not_observed",
                                 "ref": deepcopy(observed["approval_ref"])},
        "optional_next_actions": actions,
        "contract": "COMPLETED仅表示本次inspect动作完成，不是整场就绪或批准。选项不是固定管线、权限或已执行动作；主Agent仍可选其他合法工具、核查、局改或提问。"}


async def review(f, a):
    if a["action"] == "prepare_order":
        from .playlist_proposals import prepare_playlist_proposal
        # The helper owns nested input validation and its fixed, public-safe
        # ContractError. Do not leak raw Pydantic input from a duplicate parse.
        parameters = a["parameters"]
        value = await f.step("prepare_playlist_proposal", lambda: prepare_playlist_proposal(f.engine, f.scope, parameters), parameters)
        return f.result(result=value,
            remaining=["本次只读提案装配完成；主Agent仍需选择后续动作，排序和文字转语音分别需要原人工确认。"])
    prep = await f.prep()
    b = prep["body"]
    common = {"preparation_id": prep["ref"]["id"], "expected_revision": prep["ref"]["revision"]}
    if a["action"] in {"save_playlist", "save_assets"}:
        key = "save_playlist" if a["action"] == "save_playlist" else "save_asset_checklist"
        value = await f.command(key, {**common, **a["parameters"]})
        return f.result(result=value)
    if a["action"] == "compile":
        if not b.get("playlist_ref"):
            return f.result("NEEDS_DECISION", blockers=["缺少用户确认后保存的播放顺序"])
        value = await f.command("compile_playback_plan", {**common, "playlist_ref": b["playlist_ref"]})
        # Preserve the exact existing receipt/data path. A successful compile
        # does not approve text, request a decision, render audio or publish.
        result = {**value, "compile_action_completed": True,
            "preparation_readiness": "not_evaluated", "approval_status": "not_verified",
            "optional_next_actions": [text_audio_approval_option()]}
        return f.result(result=result,
            remaining=["需要语音且准备请求确认时，可仅以kind=text_audio_approval和prompt调用request_user_decision；不代表已批准或已生成语音"])
    if a["action"] == "review":
        value = await f.query("create_text_review", {"preparation_id": prep["ref"]["id"]})
        return f.result(result={"review": value, "next": "需要语音时由主Agent调用request_user_decision(kind=text_audio_approval)，此工具不批准"})
    inspection = review_inspection(prep, f.engine.store.get("run", f.scope.run_id).get("allowed_effects", []))
    return f.result(result={"preparation": prep,
        "asset_checklist": await f.query("read_asset_checklist", {"preparation_id": prep["ref"]["id"]}),
        "inspection": inspection},
        remaining=["观测缺项：" + key for key in inspection["missing_dependency_fields"]]
            + ["尚未验证当前整包text-review及批准有效性；需要时由主Agent选择实际审核查询或其他合法步骤"])


async def audio(f, a):
    if a["action"] == "retry":
        return await _retry(f, a)
    prep = await f.prep()
    ready = await f.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"], "limit": 1})
    if a["action"] == "inspect" or ready.get("ready_for_publish"):
        return f.result(result=ready)
    run = await f.engine.assert_scope(f.scope)
    if run.get("pending_unknown_operation") or ready.get("counts", {}).get("unknown_job_items"):
        return f.result("OUTCOME_UNKNOWN", result=ready,
            blockers=ready.get("blockers") or ["存在待对账的业务操作或供应商结果，不能重新提交语音任务"],
            remaining=["按原操作或任务核实未知结果，不创建替代音频任务"])
    b = prep["body"]
    if not b.get("approval_ref"):
        return f.result("NEEDS_DECISION", blockers=["需要精确整包文字与声音的真实人工批准"], result=ready)
    approval = await f.query("read_text_approval", {"ref": b["approval_ref"], "preparation_id": prep["ref"]["id"]})
    if approval.get("status") != "current":
        return f.result("NEEDS_DECISION", result=approval, blockers=["文字批准已撤回或不再对应当前内容，请重新人工确认"])
    approval_body = approval["approval"]
    # A preparation revision is a CAS fence, not the identity of paid work.
    # Saving an unrelated checklist may advance it while this exact approved
    # requirements/voice package is still rendering. Read the complete current
    # job set inside the flow, rather than relying on a model-visible page.
    observation = await f.step("inspect_current_audio_jobs", lambda: f.engine._completion_observation(run))
    matching = [job for job in observation.get("jobs", [])
        if job.get("body", {}).get("kind") == "audio"
        and job["body"].get("run_id") == f.scope.run_id
        and job["body"].get("preparation_id") == prep["ref"]["id"]
        and job["body"].get("requirements_ref") == b.get("requirements_ref")
        and job["body"].get("approval_ref") == b["approval_ref"]
        and job["body"].get("review_hash") == approval_body["review_hash"]
        and job["body"].get("generation_epoch") == b["generation_epoch"]]
    pending = [job for job in matching if job["body"].get("status") in {"queued", "running"}]
    if pending:
        job = pending[-1]
        return f.result("WAITING", result={"data": {"job_id": job["ref"]["id"],
            "job_ref": job["ref"], "status": job["body"]["status"]}, "reused": True,
            "existing_job": f.engine._job_view(run, job, include_provider_outputs=False),
            "requirements_ref": b["requirements_ref"], "approval_ref": b["approval_ref"]})
    if matching:
        latest = max(matching, key=lambda job: (job["body"].get("created_at", ""), job["ref"]["id"]))
        if latest["body"].get("status") in {"failed", "interrupted", "cancelled"}:
            return f.result("NEEDS_REPLAN", result={"data": {"job_id": latest["ref"]["id"],
                "job_ref": latest["ref"], "status": latest["body"]["status"]},
                "existing_job": f.engine._job_view(run, latest, include_provider_outputs=False)},
                blockers=["原音频任务已失败、中断或取消；未提交替代任务"],
                remaining=["主Agent检查原任务，明确选择retry原job或调整合法计划"])
    receipt = await f.command("prepare_required_audio", {"preparation_id": prep["ref"]["id"],
        "expected_revision": prep["ref"]["revision"], "approval_ref": b["approval_ref"],
        "review_hash": approval_body["review_hash"], "expected_generation_epoch": b["generation_epoch"]})
    return f.result("WAITING", result=receipt)


async def finalize(f, a):
    prep = await f.prep()
    if a.get("expected_preparation_ref") and a["expected_preparation_ref"] != prep["ref"]:
        return f.result("NEEDS_REPLAN", blockers=["本场版本已变化，请核对差异"], result={"preparation_ref": prep["ref"]})
    run = await f.engine.assert_scope(f.scope)
    # Readiness is required even when a candidate already exists. No blind
    # success from cached publication references or a stale approval.
    ready = await f.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"], "limit": 1})
    if not ready.get("ready_for_publish"):
        return f.result("WAITING" if ready.get("counts", {}).get("pending_jobs") else "NEEDS_REPLAN",
            result=ready, blockers=ready.get("blockers") or ["尚未满足发布条件"])
    observation = await f.step("read_completion_observation", lambda: f.engine._completion_observation(run))
    if observation.get("candidate_ready"):
        return f.result(result={"publication_ref": observation.get("publication_ref"), "reused": True,
            "candidate_ready": True, "adopted": False, "started": False})
    receipt = await f.command("publish_preparation", {"preparation_id": prep["ref"]["id"],
        "expected_revision": prep["ref"]["revision"], "approval_ref": ready["approval_ref"]})
    observation = await f.step("verify_publication_receipt", lambda: f.engine._completion_observation(f.engine.store.get("run", f.scope.run_id)))
    if not observation.get("candidate_ready"):
        return f.result("NEEDS_REPLAN", result={"receipt": receipt}, blockers=["候选回执未通过当前性验收"])
    return f.result(result={"receipt": receipt, "publication_ref": observation.get("publication_ref"),
        "candidate_ready": True, "adopted": False, "started": False})


async def _async_value(value):
    return value


async def _retry(f, a):
    if not a.get("job_id"):
        return f.result("NEEDS_REPLAN", blockers=["retry需要明确原job_id"])
    job = await f.query("read_job", {"job_id": a["job_id"]})
    receipt = await f.command("retry_failed_items", {"job_id": a["job_id"],
        "recovery_mode": a.get("recovery_mode", "retry")}, version_key=job["ref"])
    return f.result("WAITING", result=receipt)


async def selling_points(f, a):
    from .selling_points import execute
    return f.result(result=await execute(f.engine, f.scope, a, f.id))


_HANDLERS = {"wf_product_materials": materials, "wf_selling_points": selling_points, "wf_analysis_coordination": analyses,
    "wf_context_bundle": context, "wf_script_execution": scripts,
    "wf_playback_review": review, "wf_required_audio": audio, "wf_finalize_preparation": finalize}
