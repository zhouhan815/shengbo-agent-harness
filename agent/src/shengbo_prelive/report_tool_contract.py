"""Provider-facing report schemas that leave validation to the runtime."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import ConfigDict, Field, PlainValidator

from shengbo_contracts.business import AnalysisReport
from shengbo_contracts.common import ArtifactRef, RecordRef, StrictModel

from .material_understanding import MaterialAssessment


class ProductFactsEvidence(StrictModel):
    model_config = ConfigDict(extra="allow", json_schema_extra={
        "anyOf": [
            {"required": ["id"], "properties": {"id": {"type": "string", "minLength": 1}}},
            {"required": ["evidence_id"], "properties": {"evidence_id": {"type": "string", "minLength": 1}}},
        ],
        "if": {"properties": {"source_ref": {"properties": {"kind": {"const": "source_manifest"}}}}},
        "then": {"required": ["locator", "quote"], "properties": {
            "locator": {"type": "string", "minLength": 1}, "quote": {"type": "string", "minLength": 1},
        }},
    })
    id: str | None = Field(default=None, description="本报告唯一证据编号，供 evidence_ids 引用。")
    evidence_id: str | None = Field(default=None, description="旧报告的编号别名，新证据优先使用 id。")
    source_ref: ArtifactRef | RecordRef = Field(
        description="此条证据对应的单个获准精确来源。上传材料复制相应 read_source 返回的 source_material_ref，不从报告总来源中猜测。")
    locator: str | None = Field(default=None,
        description="上传材料必填。使用同一份资料真实返回的 paragraph:N、text:1、image:N 等定位。")
    quote: str | None = Field(default=None,
        description="上传材料必填。有正文时逐字摘录同一来源、同一定位的相关原文。无正文的图片或扫描页记录直接看到的内容，并用 visual_reviews 记录阅读回执。")


class ProductFactsToolReport(AnalysisReport):
    evidence: list[ProductFactsEvidence] = Field(default_factory=list,
        description="逐条绑定真实来源。不同附件的原文分别引用各自的 source_ref 和 locator。")
    material_assessment: MaterialAssessment | None = Field(default=None,
        description="可选的结构化资料理解。缺项的 level、status 和适用目的使用声明的枚举。")


def _raw_report(value):
    # Advertise the authoring schema while retaining the untouched draft for
    # the runtime's scoped validation and repair path.
    return value


class ReportInput(StrictModel):
    report: Annotated[Any, PlainValidator(_raw_report, json_schema_input_type=AnalysisReport)]


class ProductFactsReportInput(StrictModel):
    report: Annotated[Any, PlainValidator(_raw_report, json_schema_input_type=ProductFactsToolReport)]
