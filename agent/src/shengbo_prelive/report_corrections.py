"""Retain a rejected, uncommitted report so the model can fix exact fields.

This is not automatic normalization or formal acceptance. Every correction is
an explicit tool action and re-enters the same complete report validation.
"""
from copy import deepcopy
from typing import Literal
from pydantic import Field, JsonValue, model_validator
from shengbo_contracts.common import ArtifactRef, ContractError, StrictModel, canonical_hash
from .action_contracts import REPORT_CORRECTABLE_ERRORS


class ReportPatch(StrictModel):
    path: str = Field(min_length=2, max_length=500)
    value: JsonValue


class CorrectReportInput(StrictModel):
    draft_ref: ArtifactRef
    action: Literal["read", "patch"] = "patch"
    patches: list[ReportPatch] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def exact_action(self):
        if (self.action == "patch" and not self.patches) or (self.action == "read" and self.patches):
            raise ValueError("patch needs explicit patches; read must not carry patches")
        return self


def retain_rejected_report(engine, scope, report, error):
    if error.code not in REPORT_CORRECTABLE_ERRORS:
        return
    identity = "report_retry_" + canonical_hash([scope.run_id, scope.branch_task_id, report])[7:39]
    ref = ArtifactRef(kind="report_retry_draft", id=identity, revision=1, content_hash=canonical_hash(report)).model_dump(mode="json")
    saved = engine.store.get("report_retry_draft", identity, required=False)
    if saved and (saved.get("ref") != ref or saved.get("report") != report):
        raise ContractError("REPORT_DRAFT_INTEGRITY_INVALID", "已保留草案与内容引用不一致", status=409)
    if not saved:
        engine.store.put("report_retry_draft", {"id": identity, "run_id": scope.run_id, "branch_task_id": scope.branch_task_id,
            "role": scope.role, "ref": ref, "report": report, "rejected_code": error.code, "is_formal": False})
    error.details = {**error.details, "commit_state": "not_committed", "report_correction": {
        "tool": "correct_analysis_report", "draft_ref": ref,
        "usage": "action=read可回读本分支原草案；默认action=patch显式指定JSONpointer和value仅修必要字段。程序重新完整校验，亦可原工具重交完整报告。",
        "formal_report_saved": False}}


def correct_report(engine, scope, draft_ref, patches, action="patch"):
    if scope.role == "main" or not scope.branch_task_id:
        raise ContractError("CHILD_REQUIRED", "仅当前分析角色可修正自己的未提交报告", status=403)
    ref = ArtifactRef.model_validate(draft_ref).model_dump(mode="json")
    saved = engine.store.get("report_retry_draft", ref["id"], required=False)
    branch = engine.store.get("branch", scope.branch_task_id)
    if (not saved or saved.get("run_id") != scope.run_id or saved.get("branch_task_id") != scope.branch_task_id
            or saved.get("role") != scope.role or saved.get("ref") != ref
            or canonical_hash(saved["report"]) != ref["content_hash"]
            or branch.get("run_id") != scope.run_id or branch.get("role") != scope.role
            or branch.get("status") not in {"RUNNING", "RECOVERING"}):
        raise ContractError("REPORT_CORRECTION_SCOPE_DENIED", "草案不属于当前分析分支或已失效", status=403)
    report = deepcopy(saved["report"])
    request = CorrectReportInput(draft_ref=ref, patches=patches, action=action)
    if action == "read":
        engine._emit(scope.run_id, "branch.report_draft_read", {"title": "回读尚未提交的报告草案", "draft_ref": ref,
            "formal_report_saved": False}, scope)
        return {"draft_ref": ref, "report": report, "formal_report_saved": False}
    paths = set()
    for patch in request.patches:
        patch = ReportPatch.model_validate(patch)
        tokens = patch.path.split("/")[1:]
        if not patch.path.startswith("/") or patch.path in paths:
            raise ContractError("MATERIAL_ASSESSMENT_INVALID", "修正路径必须唯一且指向现有字段", status=422)
        paths.add(patch.path)
        current = report
        try:
            for index, raw in enumerate(tokens):
                token = raw.replace("~1", "/").replace("~0", "~")
                key = int(token) if isinstance(current, list) and token.isdecimal() else token
                if index == len(tokens) - 1:
                    current[key]  # existing coordinates only
                    current[key] = patch.value
                else:
                    current = current[key]
        except (KeyError, IndexError, TypeError, ValueError):
            raise ContractError("MATERIAL_ASSESSMENT_INVALID", "修正路径不是草案已有位置", status=422) from None
    result = engine.submit_report(scope, report)
    engine._emit(scope.run_id, "branch.report_corrected", {"title": "分析报告按明确字段修正", "draft_ref": ref,
        "patched_paths": sorted(paths), "full_report_reemitted": False, "formal_acceptance": False}, scope)
    return result
