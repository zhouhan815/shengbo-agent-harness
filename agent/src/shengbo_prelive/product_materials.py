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


class SaveAcceptedProductInput(StrictModel):
    branch_task_id: str = Field(min_length=1, max_length=160)
    accepted_report_ref: ArtifactRef | RecordRef = Field(
        description="当前run已正式接受的精确事实报告。fields须为消费者商品结构；供货渠道/供应商/进价/供货库存评价仅留Agent原件与分析审计，不能借knowledge或internal_only进入商品库。")
    product_ref: ArtifactRef | RecordRef | None = Field(default=None,
        description="明确更新时提供精确product引用；null表示新建，不根据报告product_ids猜更新目标。")
    overrides: list[FieldPatch] = Field(default_factory=list, max_length=100,
        description="仅显式顶层schema字段set/clear/preserve；未声明字段不作措辞改写。已有报告含不适合消费者的内容时，可在保留来源审计的同时显式提交择取后的对应字段；工具不自动清洗或删事实，最终由同一Voice消费者policy校验。")
    field_dispositions: list[FieldDisposition] = Field(default_factory=list, max_length=100)
    identity_decision_ref: ArtifactRef | RecordRef | None = None

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
    fields = report.get("fields")
    if not isinstance(fields, dict) or not fields or set(fields) - names:
        raise uncommitted_input_error("FACTS_SAVE_FIELDS_INVALID", "事实候选需为顶层商品schema字段，不自动展开或丢弃其他字段", status=422,
            details={"unknown_field_count": len(set(fields) - names) if isinstance(fields, dict) else 0,
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
    fields = {**deepcopy(previous), **deepcopy(fields)}
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
            elif override == "preserve" or key not in report["fields"]:
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
    with engine.store.tx() as db:
        old = db.execute("SELECT body FROM objects WHERE kind=? AND id=?", ("product_candidate_save_resolution", tool_call_id)).fetchone()
        if old:
            import json
            previous_resolution = json.loads(old[0])
            if any(previous_resolution.get(key) != value for key, value in resolution.items()):
                raise ContractError("FACTS_SAVE_RESOLUTION_CONFLICT", "同一候选保存身份不能改换输入或目标", status=403)
        else:
            engine.store._put(db, "product_candidate_save_resolution", resolution)
    receipt = await engine.business_command(scope, "save_product", command, tool_call_id)
    return {**receipt, "facts_transfer": {"branch_task_id": branch["id"], "accepted_report_ref": reference,
        "accepted_report_hash": canonical_hash(report), "candidate_fields_hash": canonical_hash(report["fields"]),
        "submitted_fields_hash": canonical_hash(fields), "override_paths": sorted(seen), "overrides_applied": len(seen),
        "automatic_selection": False, "automatic_research_binding": False, "approval_implied": False}}
