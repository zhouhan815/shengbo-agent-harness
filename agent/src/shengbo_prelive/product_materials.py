"""Optional exact accepted-facts transfer; no new authority or research action.

This helper is not a second planner. It reads a receipted formal report, copies
its field values, applies only explicit overrides and calls the existing product
command. Original identity, Goal, CAS, unknown and business validation still run.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from pydantic import Field, ValidationError, field_validator

from shengbo_contracts.business import COMMAND_ARGS, FieldDisposition, FieldPatch
from shengbo_contracts.common import (
    ArtifactRef, RecordRef, SCHEMA_VERSION, StrictModel, ContractError, canonical_hash,
)
from .action_contracts import uncommitted_input_error


_REF_KEYS = {"kind", "id", "revision", "content_hash"}


def _ref(value, kind, *, input_value=False):
    raw = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    try:
        if not isinstance(raw, dict) or type(raw.get("revision")) is not int:
            raise ValueError
        model = (ArtifactRef if "schema_version" in raw else RecordRef).model_validate(raw, strict=True)
        if (model.kind != kind or not model.id or len(model.id) > 256
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", model.content_hash)
                or isinstance(model, ArtifactRef) and model.schema_version != SCHEMA_VERSION):
            raise ValueError
        return model.model_dump(mode="json")
    except (ValidationError, TypeError, ValueError):
        if input_value:
            # Only model-supplied helper DTO values are correctable input.
            # Stored/formal proof references below retain the hard error.
            raise uncommitted_input_error("FACTS_SAVE_REF_INVALID", "需提供正确类型、版本和哈希的精确引用", status=422,
                details={"expected_kind": kind, "correction": "从当前可信读回结果复制精确引用，不猜测ID或哈希"}) from None
        raise ContractError("FACTS_SAVE_REF_INVALID", "需提供正确类型、版本和哈希的精确引用", status=422) from None


def _identity(value):
    return tuple(value.get(key) for key in ("kind", "id", "revision", "content_hash"))


class SellingPointsHandoff(StrictModel):
    branch_task_id: str = Field(min_length=1, max_length=160)
    accepted_report_ref: ArtifactRef | RecordRef
    user_quote: str = Field(min_length=1, max_length=160,
        description="逐字引用用户本轮明确要求缺少卖点时先根据顾客研究生成并保存核心卖点的独立指令。")


class SaveAcceptedProductInput(StrictModel):
    branch_task_id: str = Field(min_length=1, max_length=160)
    accepted_report_ref: ArtifactRef | RecordRef = Field(
        description="当前run已正式接受的精确事实报告。fields须为消费者商品结构；供货渠道/供应商/进价/供货库存评价仅留Agent原件与分析审计，不能借knowledge或internal_only进入商品库。")
    candidate_sku_id: str | None = Field(default=None, min_length=1, max_length=200,
        description="正式事实报告包含fields.products时，明确指定要保存的唯一原始规格编号。每次保存一个候选，不自动展开或猜选。")
    product_ref: ArtifactRef | RecordRef | None = Field(default=None,
        description="明确更新时提供精确product引用；null表示新建，不根据报告product_ids猜更新目标。")
    overrides: list[FieldPatch] = Field(default_factory=list, max_length=100,
        description="仅显式顶层schema字段set/clear/preserve；未声明字段不作措辞改写。已有报告含不适合消费者的内容时，可在保留来源审计的同时显式提交择取后的对应字段；工具不自动清洗或删事实，最终由同一Voice消费者policy校验。")
    field_dispositions: list[FieldDisposition] = Field(default_factory=list, max_length=100)
    identity_decision_ref: ArtifactRef | RecordRef | None = None
    selling_points_from_research: SellingPointsHandoff | None = Field(default=None,
        description="本场准备中只有明确授权的新商品可用。引用同一商品资料、已验收且无冲突的顾客研究；在首次商品保存前生成和复核空白卖点，不重做研究。")

    @field_validator("accepted_report_ref", "product_ref", "identity_decision_ref", mode="before")
    @classmethod
    def exact_kinds(cls, value, info):
        if value is None and info.field_name != "accepted_report_ref":
            return value
        normalized = _ref(value, {"accepted_report_ref": "analysis", "product_ref": "product",
                                 "identity_decision_ref": "human_decision"}[info.field_name], input_value=True)
        # Preserve the supplied reference representation. Returning a dict to
        # this union would let ArtifactRef add its default schema_version to a
        # four-field RecordRef, changing the original business reader contract.
        return (ArtifactRef if "schema_version" in normalized else RecordRef).model_validate(
            normalized, strict=True)


def _reject(code="FACTS_SAVE_LINEAGE_INVALID", message="正式事实报告、采纳回执或来源链未能精确核对"):
    raise ContractError(code, message, status=403)


def _rebind(value, mapping):
    """Only the already receipted source-ref transformation, never prose."""
    if isinstance(value, dict):
        if (_REF_KEYS <= value.keys() and not value.keys() - (_REF_KEYS | {"schema_version"})
                and value.get("kind") == "source_manifest"):
            found = mapping.get(_identity(value))
            if found and ("schema_version" not in value or value["schema_version"] == found["old_ref"].get("schema_version")):
                return deepcopy(found["new_ref"])
        return {key: _rebind(part, mapping) for key, part in value.items()}
    if isinstance(value, list):
        return [_rebind(part, mapping) for part in value]
    return deepcopy(value)


def _accepted_proof(engine, run, branch, reference, formal, *, expected_role="product_facts"):
    """Check both formal immutable body and current-run local commit evidence."""
    if expected_role not in {"product_facts", "audience_scenes", "session_config"}:
        _reject()
    body = formal.get("body") or {}
    if (_identity(_ref(formal.get("ref"), "analysis")) != _identity(reference)
            or canonical_hash(body) != reference["content_hash"]
            or body.get("accepted") is not True or body.get("role") != expected_role
            or body.get("preparation_id") != run["scope"].get("preparation_id")):
        _reject()
    prepared, report = branch.get("report"), body.get("report")
    lineage = body.get("lineage") or {}
    if (not isinstance(prepared, dict) or not isinstance(report, dict)
            or canonical_hash(prepared) != branch.get("report_hash")
            or branch.get("prepared_report_hash") != branch.get("report_hash")
            or branch.get("accepted_report_hash") != canonical_hash(report)
            or lineage.get("prepared_report_hash") != branch.get("report_hash")
            or lineage.get("accepted_report_hash") != canonical_hash(report)):
        _reject()
    matching = []
    for op in engine.store.list("operation"):
        receipt = op.get("receipt") or {}
        data = receipt.get("data") or {}
        args = op.get("args") or {}
        accepted = data.get("analysis_ref") or data.get("accepted_artifact_ref") or data.get("ref") or {}
        prepared_ref = args.get("prepared_report_ref") or {}
        if (op.get("run_id") == run["id"] and op.get("key") == "accept_analysis_report"
                and op.get("status") == "committed" and op.get("request_hash") == canonical_hash(args)
                and receipt.get("command") == "accept_analysis_report" and receipt.get("operation_id") == op.get("id")
                and receipt.get("status") in {"committed", "accepted", "replayed"}
                and _identity(accepted) == _identity(reference)
                and args.get("role") == expected_role and args.get("report") == prepared
                and args.get("report_hash") == branch["report_hash"]
                and args.get("preparation_id") == run["scope"].get("preparation_id")
                and prepared_ref.get("kind") == "prepared_analysis" and prepared_ref.get("id") == branch["id"]
                and prepared_ref.get("content_hash") == branch["report_hash"]
                and prepared_ref == lineage.get("prepared_report_ref")
                and data.get("prepared_report_hash") == branch["report_hash"]
                and data.get("accepted_report_hash") == canonical_hash(report)):
            matching.append(op)
    if not matching:
        _reject()
    op = matching[0]
    allowed_originals = {_identity(_ref(ref, "source_manifest")) for ref in run["input"].get("material_refs", [])}
    task_originals = {_identity(_ref(ref, "source_manifest")) for ref in branch.get("task", {}).get("source_refs", [])}
    if not task_originals <= allowed_originals:
        _reject()
    imported = {_identity(_ref(row.get("source_ref"), "source_manifest")): row
                for row in op["args"].get("source_manifests", [])}
    if set(imported) != task_originals:
        _reject()
    mapping = {}
    replacements = body.get("source_ref_replacements") or []
    if replacements != op["receipt"]["data"].get("source_ref_replacements", []):
        _reject()
    for item in replacements:
        old, new = _ref(item.get("old_ref"), "source_manifest"), _ref(item.get("new_ref"), "source_manifest")
        key = _identity(old)
        if key in mapping or key not in imported or canonical_hash(imported[key].get("manifest")) != old["content_hash"]:
            _reject()
        mapping[key] = {"old_ref": old, "new_ref": new}
    if replacements:
        if (lineage.get("transformation") != "verified_source_ref_rebinding"
                or lineage.get("original_prepared_report") != prepared or set(mapping) != task_originals
                or _rebind(prepared, mapping) != report):
            _reject()
    elif lineage.get("transformation") != "none" or prepared != report or task_originals:
        _reject()
    prepared_sources = {_identity(_ref(ref, "source_manifest")) for ref in prepared.get("source_refs", [])}
    if not prepared_sources <= task_originals:
        _reject()
    return body, report, [_ref(ref, "source_manifest") for ref in report.get("source_refs", [])]


async def _draft_researched_points(engine, scope, run, proof, facts_ref, fact_sources, fields, sku):
    from .models import RunScope
    from .selling_point_evidence import build_evidence_catalog
    from .selling_point_extract import extract_selling_points, review_selling_points
    from .selling_points_fast import _assert_unsent

    text = run["input"]["text"]
    # Deliberately narrow write authorization. A loose match on words like
    # “卖点” or a research report's instruction does not grant an extra write.
    directive = r"(?:如果|如)?(?:商品)?(?:缺少|没有)核心卖点(?:时)?[，,、 ]*(?:请)?(?:先)?(?:根据|依据|结合)顾客(?:研究|分析)(?:结果)?生成并保存(?:核心)?卖点"
    clauses = {part.strip() for part in re.split(r"[。；;！!？?\n]", text)}
    if (run.get("goal", {}).get("objective") not in {"prepare_session", "materials_to_script"}
            or run.get("goal", {}).get("original_input_hash") != canonical_hash(run["input"])
            or proof.user_quote not in clauses or not re.fullmatch(directive, proof.user_quote)):
        raise uncommitted_input_error("FACTS_SAVE_SELLING_POINTS_AUTH_REQUIRED",
            "如要同时生成并保存缺失的核心卖点，请明确告诉我；商品事实仍可独立保存。", status=422)
    ref = _ref(proof.accepted_report_ref, "analysis", input_value=True)
    branch = engine.store.get("branch", proof.branch_task_id, required=False)
    if (not branch or branch["run_id"] != run["id"] or branch["role"] != "audience_scenes"
            or branch["status"] != "ACCEPTED" or _identity(branch["accepted_ref"]) != _identity(ref)):
        _reject("FACTS_SAVE_RESEARCH_DENIED", "只可交接本任务已验收的顾客研究")
    formal = await engine.business_query(scope, "read_context", {"ref": ref})
    body, research, research_sources = _accepted_proof(engine, run, branch, ref, formal, expected_role="audience_scenes")
    facts_sources = {_identity(s) for s in fact_sources}
    if (not facts_sources or {_identity(s) for s in research_sources} != facts_sources
            or body.get("product_refs") or research.get("product_ids")
            or any(dep.get("kind") == "product" for dep in research.get("dependencies", []))
            or research.get("conflicts") or research.get("missing_fields")
            or not any((research.get("fields") or {}).get(key) for key in ("segments", "scenes", "concerns", "benefit_priorities"))
            or (sku is not None and research.get("fields", {}).get("sku_id") != sku)
            or engine._binding_explicit_fact_conflicts(research.get("fields") or {}, fields)):
        raise uncommitted_input_error("FACTS_SAVE_RESEARCH_NOT_COMPATIBLE",
            "顾客研究与当前商品资料或款式无法自动核对，请先核对对应关系。", status=422)
    evidence = build_evidence_catalog(facts_ref, fields)
    stage_identity = "sp_facts_" + canonical_hash([scope.run_id, facts_ref, ref, fields])[7:39]
    stage = engine.store.get("selling_points_facts", stage_identity, required=False) or {
        "id": stage_identity, "run_id": scope.run_id, "facts_ref": facts_ref, "research_ref": ref}
    for name in ("draft", "review"):
        if name in stage:
            continue
        stage_scope = RunScope(scope.run_id, stage_identity + ":" + name, scope.phase, account_id=scope.account_id)
        await engine.assert_scope(stage_scope)
        _assert_unsent(engine, stage_scope)
        kwargs = {"product": fields, "evidence_catalog": evidence, "research": research}
        try:
            items = (await extract_selling_points(engine, stage_scope, **kwargs) if name == "draft" else
                     await review_selling_points(engine, stage_scope, **kwargs, items=stage["draft"]))
        except ContractError as error:
            if error.code not in {"SELLING_POINTS_EVIDENCE_INVALID", "SELLING_POINTS_OUTPUT_INVALID", "SELLING_POINTS_UNSUPPORTED"}:
                raise
            raise uncommitted_input_error("FACTS_SAVE_SELLING_POINTS_NEEDS_REVIEW",
                "卖点未通过结构或事实复核，商品尚未保存；请核对现有依据或询问用户，无需重做顾客研究。",
                status=422, details={"stage": name, "research_ref": ref, "reason_code": error.code}) from None
        stage = engine.store.put("selling_points_facts", {**stage, name: items})
    engine._emit(run["id"], "selling_points.research_handoff", {"title": "顾客研究已用于生成并复核核心卖点",
        "status": "SUCCEEDED", "research_ref": ref, "facts_ref": facts_ref, "item_count": len(stage["review"])}, scope)
    return [item["selling_point"] for item in stage["review"]]


async def save_accepted_product(engine, scope, args: SaveAcceptedProductInput | dict[str, Any], *, tool_call_id: str):
    """Copy an exact accepted candidate and call the unchanged save command.

    Return the normal receipt/data shape. No automatic selection, binding,
    analysis acceptance, script generation or approval takes place here.
    """
    data = args if isinstance(args, SaveAcceptedProductInput) else SaveAcceptedProductInput.model_validate(args)
    run = await engine.assert_scope(scope)
    if (scope.role != "main" or scope.phase != "execute" or scope.branch_task_id
            or "save_product" not in run.get("allowed_effects", [])):
        raise ContractError("GOAL_EFFECT_DENIED", "当前角色和目标未授予商品保存", status=403)
    if run.get("pending_unknown_operation"):
        raise ContractError("RECONCILIATION_REQUIRED", "原操作结果待核实，不能继续保存")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise ContractError("FACTS_SAVE_CALL_ID_REQUIRED", "保存候选需要稳定逻辑调用身份", status=422)
    reference = _ref(data.accepted_report_ref, "analysis")
    branch = engine.store.get("branch", data.branch_task_id, required=False)
    if (not branch or branch.get("run_id") != run["id"] or branch.get("role") != "product_facts"
            or branch.get("status") != "ACCEPTED" or branch.get("task", {}).get("role") != "product_facts"
            or _identity(_ref(branch.get("accepted_ref"), "analysis")) != _identity(reference)):
        _reject("FACTS_SAVE_REPORT_DENIED", "只可使用当前运行已接受的精确商品事实报告")
    formal = await engine.business_query(scope, "read_context", {"ref": reference})
    body, report, sources = _accepted_proof(engine, run, branch, reference, formal)
    schema = await engine.business_query(scope, "get_business_schema", {})
    product_schema = (schema.get("schema") or {}).get("product") or {}
    names = set(product_schema.get("properties") or {})
    if not names or schema.get("schema_hash") != canonical_hash(schema.get("schema")):
        raise ContractError("FACTS_SAVE_SCHEMA_INVALID", "商品schema及哈希无法核对", status=502)
    report_fields = report.get("fields")
    if isinstance(report_fields, dict) and set(report_fields) == {"products"}:
        candidates = report_fields["products"]
        if not isinstance(candidates, list) or any(not isinstance(row, dict) or not isinstance(row.get("sku_id"), str)
                or not row["sku_id"].strip() or set(row) - names for row in candidates):
            raise uncommitted_input_error("FACTS_SAVE_FIELDS_INVALID", "多规格事实报告的商品字段或规格编号不完整", status=422)
        skus = [row["sku_id"] for row in candidates]
        if len({sku.casefold() for sku in skus}) != len(skus) or data.candidate_sku_id not in skus:
            raise uncommitted_input_error("FACTS_SAVE_FIELDS_INVALID", "请选择正式报告中唯一、明确的规格编号", status=422,
                details={"parameter": "candidate_sku_id", "candidate_sku_ids": skus,
                         "correction": "按本次已确认方案选择一个规格；不得按位置猜测"})
        report_fields = candidates[skus.index(data.candidate_sku_id)]
    if (not isinstance(report_fields, dict) or not report_fields or set(report_fields) - names
            or data.candidate_sku_id is not None and report_fields.get("sku_id") != data.candidate_sku_id):
        raise uncommitted_input_error("FACTS_SAVE_FIELDS_INVALID", "事实候选需为顶层商品schema字段，不自动展开或丢弃其他字段", status=422,
            details={"unknown_field_count": len(set(report_fields) - names) if isinstance(report_fields, dict) else 0,
                     "schema_hash": schema["schema_hash"],
                     "correction": "核对正式候选及商品schema；可显式选择正确报告，或经原save_product提交已核查字段，不自动删除未知字段"})
    target = _ref(data.product_ref, "product") if data.product_ref is not None else None
    previous = {}
    if target:
        if target["id"] not in run.get("target_product_ids", []) + run.get("created_product_ids", []):
            raise ContractError("TARGET_DENIED", "商品不在当前明确目标内", status=403)
        old = await engine.business_query(scope, "read_product", {"ref": target})
        if (_identity(_ref(old.get("ref"), "product")) != _identity(target)
                or canonical_hash(old.get("body")) != target["content_hash"]):
            _reject("FACTS_SAVE_PRODUCT_INVALID", "更新商品精确引用与正文不一致")
        previous = {key: deepcopy(value) for key, value in old["body"].items() if key in names}
    fields = {**deepcopy(previous), **deepcopy(report_fields)}
    seen, cleared = set(), []
    for patch in data.overrides:
        if (not re.fullmatch(r"/[A-Za-z_]+", patch.path) or patch.path[1:] not in names or patch.path in seen
                or patch.op in {"clear", "preserve"} and patch.value is not None):
            raise uncommitted_input_error("FACTS_SAVE_OVERRIDE_INVALID", "覆盖仅支持唯一的真实顶层字段及合法set/clear/preserve", status=422,
                details={"parameter": "overrides", "schema_hash": schema["schema_hash"],
                         "correction": "按商品schema使用唯一顶层路径；clear/preserve不带value，不能隐式改其他字段"})
        seen.add(patch.path)
        key = patch.path[1:]
        if patch.op == "set":
            fields[key] = deepcopy(patch.value)
        elif patch.op == "clear":
            if key in product_schema.get("required", []) or key in {"name", "price", "currency"}:
                raise uncommitted_input_error("FIELD_CANNOT_CLEAR", "必填商品身份和价格字段不能清空", status=422,
                    details={"parameter": "overrides", "schema_hash": schema["schema_hash"],
                             "correction": "保留必填值，或显式设置有依据的有效值；缺事实时向用户确认"})
            fields.pop(key, None); cleared.append(key)
        elif target and key in previous:
            fields[key] = deepcopy(previous[key])
    if cleared:
        validated = await engine.business_query(scope, "validate_product_assembly", {"schema_hash": schema["schema_hash"], "fields": fields})
        if validated.get("valid") is not True or not isinstance(validated.get("normalized_fields"), dict):
            raise uncommitted_input_error("FACTS_SAVE_CLEAR_DEFAULT_UNAVAILABLE", "无法由原商品校验取得明确清空默认值，请修正候选", status=422,
                details={"schema_hash": schema["schema_hash"], "correction_tool": "validate_product_assembly",
                         "correction": "核对候选与原商品组装校验；明确修正overrides或经原save_product提交有效字段，不猜清空默认值"})
        for key in cleared:
            fields[key] = deepcopy(validated["normalized_fields"][key])
    for disposition in data.field_dispositions:
        if not re.fullmatch(r"/[A-Za-z_]+", disposition.path) or disposition.path[1:] not in names:
            raise uncommitted_input_error("FACTS_SAVE_OVERRIDE_INVALID", "字段处置必须使用真实商品schema路径", status=422,
                details={"parameter": "field_dispositions", "schema_hash": schema["schema_hash"],
                         "correction": "按商品schema提供真实顶层字段处置，不自动丢弃或改写字段"})
    research_handoff = None
    if data.selling_points_from_research:
        if target or body.get("product_refs") or seen.intersection({"/core_selling_points", "/selling_points"}):
            raise uncommitted_input_error("FACTS_SAVE_SELLING_POINTS_SCOPE_INVALID",
                "同次补齐核心卖点只用于新商品，不能覆盖现有商品或手工填写的卖点。", status=422)
        from .selling_points import _has_selling_points
        if not _has_selling_points(fields):
            validated = await engine.business_query(scope, "validate_product_assembly",
                {"schema_hash": schema["schema_hash"], "fields": fields})
            if validated.get("valid") is not True:
                raise uncommitted_input_error("FACTS_SAVE_FIELDS_INVALID", "商品事实尚未通过校验，未请求模型写卖点。",
                    status=422, details={"errors": validated.get("errors", [])})
            points = await _draft_researched_points(engine, scope, run, data.selling_points_from_research,
                reference, sources, fields, data.candidate_sku_id)
            fields.update(core_selling_points=points, selling_points="\n".join(points))
            research_handoff = {"research_ref": _ref(data.selling_points_from_research.accepted_report_ref, "analysis"),
                                "item_count": len(points)}
    patches = [patch.model_dump(mode="json") for patch in data.overrides]
    if target:
        explicit = {patch.path: patch.model_dump(mode="json") for patch in data.overrides}
        patches = [explicit.get("/" + key, {"path": "/" + key, "op": "set", "value": deepcopy(fields[key])})
            for key in sorted(fields) if previous.get(key) != fields[key] or "/" + key in explicit]
    dispositions = [value.model_dump(mode="json") for value in data.field_dispositions]
    if target and not dispositions:
        # read_product already supplied this exact old snapshot. Retention is
        # evidence of its stored value, not verification of its original source
        # or historical knowledge labels. Do not attribute every retained field
        # (or an explicit override) to this report's new material. Creation and
        # caller-supplied dispositions keep their original business semantics.
        actions = {patch["path"]: patch["op"] for patch in patches}
        for key in sorted(names):
            path = "/" + key
            override = explicit.get(path, {}).get("op")
            if override in {"set", "clear"}:
                refs, reason = [], "本次显式字段操作；未声明该覆盖值的资料来源"
            elif override == "preserve" or key not in report_fields:
                refs = [target] if key in previous else []
                reason = ("保留旧商品精确版本原值；未重新核验原始来源或历史处置标签" if refs
                          else "旧商品与本次报告均未提供该字段；不推断资料来源")
            else:
                refs, reason = sources, "原样采用本次已采纳事实报告的字段及其来源链"
            dispositions.append(FieldDisposition(path=path, action=actions.get(path, "preserve"),
                knowledge="unknown" if fields.get(key) in (None, "", []) else "known",
                source_refs=deepcopy(refs), reason=reason).model_dump(mode="json"))
    command = {"schema_hash": schema["schema_hash"], "fields": fields, "patches": patches,
        "field_dispositions": dispositions, "source_manifest_refs": sources}
    if target:
        command.update(product_id=target["id"], expected_revision=target["revision"])
    if data.identity_decision_ref is not None:
        command["identity_decision_ref"] = _ref(data.identity_decision_ref, "human_decision")
    # The record binds this convenience input to the actual command. M5 may
    # check it against the locally attested failed operation; it is not a new
    # grant, an acceptance or a model-visible substitute for the original args.
    command = COMMAND_ARGS["save_product"].model_validate(command).model_dump(mode="json", exclude_none=True)
    resolution = {"id": tool_call_id, "run_id": run["id"], "scope_hash": canonical_hash(run["scope"]),
        "input_hash": canonical_hash(data.model_dump(mode="json", exclude_none=True)),
        "command_args": deepcopy(command), "command_hash": canonical_hash(command)}
    update_proof = {"accepted_report_ref": reference, "accepted_report_hash": canonical_hash(report),
        "base_product_ref": target} if target else None
    with engine.store.tx() as db:
        old = db.execute("SELECT body FROM objects WHERE kind=? AND id=?", ("product_candidate_save_resolution", tool_call_id)).fetchone()
        if old:
            import json
            previous_resolution = json.loads(old[0])
            if (any(previous_resolution.get(key) != value for key, value in resolution.items())
                    or previous_resolution.get("update_proof") not in (None, update_proof)):
                raise ContractError("FACTS_SAVE_RESOLUTION_CONFLICT", "同一候选保存身份不能改换输入或目标", status=403)
        if update_proof:
            resolution["update_proof"] = update_proof
        if not old or update_proof and not previous_resolution.get("update_proof"):
            engine.store._put(db, "product_candidate_save_resolution", resolution)
    receipt = await engine.business_command(scope, "save_product", command, tool_call_id)
    return {**receipt, "facts_transfer": {"branch_task_id": branch["id"], "accepted_report_ref": reference,
        "accepted_report_hash": canonical_hash(report), "candidate_fields_hash": canonical_hash(report["fields"]),
        "submitted_fields_hash": canonical_hash(fields), "override_paths": sorted(seen), "overrides_applied": len(seen),
        **({"selling_points_handoff": research_handoff} if research_handoff else {}),
        "automatic_selection": False, "automatic_research_binding": False, "approval_implied": False}}


async def verify_receipted_facts_update(engine, run, analysis, target_refs, receipts):
    """Accept a base report for the exact revision its controlled save produced."""
    from shengbo_contracts.common import WorkScope

    if run["goal"]["objective"] != "catalog_update" or analysis.get("body", {}).get("role") != "product_facts":
        return None
    report = analysis["body"]
    if canonical_hash(report) != analysis["ref"]["content_hash"]:
        return None
    product_inputs = {_identity(ref) for ref in report.get("product_refs", [])}
    if len(product_inputs) != 1:
        return None
    target_keys = {_identity(ref) for ref in target_refs}
    operations = {op["id"]: op for op in receipts if op["key"] == "save_product"}
    for row in engine.store.list("product_candidate_save_resolution"):
        if row.get("run_id") != run["id"] or row.get("scope_hash") != canonical_hash(run["scope"]):
            continue
        proof = row.get("update_proof") or {}
        base = proof.get("base_product_ref") or {}
        if (_identity(base) not in product_inputs or _identity(proof.get("accepted_report_ref") or {}) != _identity(analysis["ref"])
                or proof.get("accepted_report_hash") != canonical_hash(report.get("report"))):
            continue
        op_id = "op_" + canonical_hash([run["id"], row["id"], "save_product"])[7:39]
        op = operations.get(op_id) or {}
        args, receipt = op.get("args", {}), op.get("receipt", {})
        saved = receipt.get("data", {}).get("product_ref") or {}
        if (op.get("status") != "committed" or args != row.get("command_args")
                or op.get("request_hash") != canonical_hash(args) or row.get("command_hash") != op.get("request_hash")
                or args.get("product_id") != base.get("id") or args.get("expected_revision") != base.get("revision")
                or saved.get("id") != base.get("id") or _identity(saved) not in target_keys
                or receipt.get("command") != "save_product" or receipt.get("operation_id") != op_id):
            continue
        try:
            observed = await engine.client.query("read_operation_receipt", {"operation_id": op_id},
                scope=run["scope"], run_id=run["id"])
        except (ContractError, ValidationError):
            continue
        scope = WorkScope.model_validate(run["scope"]).model_dump(mode="json", exclude_none=True)
        expected_hash = canonical_hash({"command": "save_product", "args": args, "scope": scope})
        if (observed.get("state") == "committed" and observed.get("receipt") == receipt
                and observed.get("request_hash") == expected_hash):
            return {"product_ref": saved, "base_product_ref": base, "operation_id": op_id,
                "verification": "accepted_facts_and_receipted_product_update"}
    return None
