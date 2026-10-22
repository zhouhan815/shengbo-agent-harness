"""PRELIVE-2.3 business wire contracts shared by voice and agent services.

Requests reject unknown fields. Principal and execution scope are injected by
the transport, never accepted from the model's business arguments.
"""
from __future__ import annotations

import re
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin
from pydantic_core import ErrorType
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model, JsonValue, field_validator, model_serializer
from .common import RecordRef, ArtifactRef, MutationContext

CONTRACT_VERSION = "PRELIVE-2.3"


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


Ref = RecordRef | ArtifactRef


# Diagnostic paths are schema names, never arbitrary supplied keys or values.
_PRODUCT_DIAGNOSTIC_NAMES = frozenset({
    "sku_id", "sale_status", "sales_platform", "specifications", "material", "ingredients", "target_audience",
    "storage_conditions", "shelf_life_value", "shelf_life_unit", "shipping_origin", "shipping_time",
    "return_policy", "core_selling_points", "usage_instructions", "faq", "hidden_optional_fields",
    "name", "currency", "price", "promo_price", "selling_points", "promotion", "knowledge",
    "knowledge_documents", "image", "value", "id", "title", "content", "unknown_field",
})

# Reuse the shared schema coordinate vocabulary, excluding nested-document
# members and the diagnostic sentinel. Runtime additionally checks the current
# Voice Product schema before granting a requested field scope.
PRODUCT_FIELD_PATHS = tuple("/" + name for name in sorted(
    _PRODUCT_DIAGNOSTIC_NAMES - {"value", "id", "title", "content", "unknown_field"}))


class ProductAssemblyInvalidIssue(DTO):
    """Allowlisted Pydantic code and structural path, with no input/msg/ctx."""
    model_config = ConfigDict(extra="forbid", strict=True)
    path: list[str | int] = Field(max_length=20)
    type: ErrorType

    @field_validator("path")
    @classmethod
    def diagnostic_path(cls, value):
        if any((type(part) is int and part < 0) or
               (type(part) is str and part not in _PRODUCT_DIAGNOSTIC_NAMES) or
               type(part) not in {str, int} for part in value):
            raise ValueError("diagnostic path must contain only known schema names or indexes")
        return value


class ProductAssemblyInvalidDetails(DTO):
    """Exact no-commit product validation rejection; grants no write authority."""
    model_config = ConfigDict(extra="forbid", strict=True)
    validation_stage: Literal["assembled", "supplied"]
    commit_state: Literal["not_committed"]
    issues: list[ProductAssemblyInvalidIssue] = Field(min_length=1, max_length=1000)
    product_ref: Ref | None
    schema_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    required_fields: list[str] = Field(max_length=100)

    @field_validator("product_ref")
    @classmethod
    def product_kind(cls, value):
        if value is not None and value.kind != "product":
            raise ValueError("validation reference must be product")
        return value

    @field_validator("required_fields")
    @classmethod
    def required_schema_names(cls, value):
        if any(name not in _PRODUCT_DIAGNOSTIC_NAMES for name in value):
            raise ValueError("required fields must be schema names")
        return value


class ProductConsumerContentIssue(DTO):
    """Consumer-catalog rejection coordinates; never echo supplier content."""
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=180)
    reason: Literal["supply_channel", "purchase_cost", "supplier_identity", "supplier_metrics",
                    "task_metadata", "specification_placement", "knowledge_placement"]
    action: Literal["remove_from_product", "move_to_knowledge", "move_to_faq", "omit_unknown"]

    @field_validator("path")
    @classmethod
    def schema_coordinate(cls, value):
        pieces = value.removeprefix("/").split("/")
        if (not value.startswith("/") or any(not piece or not (piece in _PRODUCT_DIAGNOSTIC_NAMES
                or re.fullmatch(r"0|[1-9][0-9]{0,5}", piece)) for piece in pieces)):
            raise ValueError("consumer issue must contain only schema coordinates")
        return value


class ProductConsumerContentInvalidDetails(DTO):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy_version: Literal["consumer-catalog-v1"]
    commit_state: Literal["not_committed"]
    issues: list[ProductConsumerContentIssue] = Field(min_length=1, max_length=60)
    retryable_after_correction: Literal[True]


class FieldPatch(DTO):
    path: str
    op: Literal["set", "clear", "preserve"]
    value: Any = None


class FieldDisposition(DTO):
    path: str
    action: Literal["set", "clear", "preserve"] = "preserve"
    knowledge: Literal["known", "unknown", "not_applicable"] = "known"
    source_refs: list[Ref] = Field(default_factory=list)
    reason: str = ""


class ImportBlobArgs(DTO):
    content_base64: str
    mime: str = "text/plain"
    expected_sha256: str
    filename: str = "material.txt"


class PreparationArgs(DTO):
    preparation_id: str
    expected_revision: int | None = None


class ImportRecapArgs(DTO):
    raw_blob_ref: Ref
    document: dict[str, Any]
    synthetic: bool = False


class AssetChecklistItem(DTO):
    item_id: str = Field(min_length=1, max_length=120)
    product_ref: Ref | None = None
    category: Literal["product_overview", "on_body", "detail", "use_scene", "specification", "comparison", "other"] = "other"
    purpose: str = Field(min_length=1, max_length=2000)
    recommended_content: str = Field(min_length=1, max_length=4000)
    status: Literal["missing", "available", "not_needed"] = "missing"
    blob_ref: ArtifactRef | None = None
    source_refs: list[Ref] = Field(default_factory=list)


class AssetChecklist(DTO):
    items: list[AssetChecklistItem]
    notes: list[str] = Field(default_factory=list)


class SaveAssetChecklistArgs(PreparationArgs):
    checklist: AssetChecklist


class LinkRecapArgs(PreparationArgs):
    recap_refs: list[Ref]


class ApproveTextArgs(PreparationArgs):
    review_hash: str


class RevokeApprovalArgs(DTO):
    approval_ref: Ref
    reason: str = "用户撤销批准"


class CreateRoomArgs(DTO):
    name: str = Field(min_length=1, max_length=120)
    initial_config: dict[str, Any] = Field(default_factory=dict)


class CreatePreparationArgs(DTO):
    room_id: str
    room_revision: int
    goal: str = "准备本场直播"
    source_refs: list[Ref] = Field(default_factory=list)


class SaveProductArgs(DTO):
    product_id: str | None = None
    expected_revision: int | None = None
    schema_hash: str
    patches: list[FieldPatch]
    fields: dict[str, Any]
    field_dispositions: list[FieldDisposition] = Field(default_factory=list)
    source_manifest_refs: list[Ref] = Field(default_factory=list)
    identity_decision_ref: Ref | None = None


class UpdateProductsArgs(PreparationArgs):
    selected_product_refs: list[Ref]


class RefreshProductArgs(PreparationArgs):
    expected_old_product_ref: Ref
    new_product_ref: Ref


class RevisionChoiceArgs(PreparationArgs):
    pinned_product_ref: Ref
    observed_head_ref: Ref
    diff_hash: str
    choice: Literal["keep_pinned", "refresh"]


class SaveDraftArgs(PreparationArgs):
    draft: dict[str, Any]


class VoiceSelection(DTO):
    provider: Literal["system", "minimax"]
    voice: str = Field(min_length=1, max_length=120)
    model: str = "speech-2.8-turbo"


class AnalysisReport(DTO):
    summary: str = Field(min_length=1, max_length=4000)
    product_ids: list[str] = Field(default_factory=list)
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    insights: list[dict[str, JsonValue]] = Field(default_factory=list)
    evidence: list[dict[str, JsonValue]] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    conflicts: list[dict[str, JsonValue]] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    asset_suggestions: list[dict[str, JsonValue]] = Field(default_factory=list)
    source_refs: list[ArtifactRef] = Field(default_factory=list)
    dependencies: list[Ref] = Field(default_factory=list)
    material_assessment: dict[str, JsonValue] | None = Field(default=None,
        description="可选资料理解结果：商品/SKU与语言归属、字段来源/计量基准、材料用途及分级缺项。不是自动核实商品事实的证明。")

    @model_serializer(mode="wrap")
    def preserve_legacy_report_hash(self, handler):
        value = handler(self)
        if self.material_assessment is None:
            value.pop("material_assessment", None)
        return value


class SourceManifestImport(DTO):
    source_ref: ArtifactRef
    manifest: dict[str, JsonValue]
    original_blob_ref: ArtifactRef


class AcceptAnalysisArgs(DTO):
    role: Literal["product_facts", "session_config", "audience_scenes"]
    report: AnalysisReport
    report_hash: str
    preparation_id: str | None = None
    expected_revision: int | None = None
    product_refs: list[Ref] = Field(default_factory=list)
    readset: list[Ref] = Field(default_factory=list)
    source_manifests: list[SourceManifestImport] = Field(default_factory=list)
    prepared_report_ref: ArtifactRef | None = None


class SaveContextArgs(PreparationArgs):
    product_refs: list[Ref]
    analysis_refs: list[Ref] = Field(default_factory=list)
    draft_ref: Ref
    recap_refs: list[Ref] = Field(default_factory=list)
    source_refs: list[Ref] = Field(default_factory=list)
    prior_script_refs: list[Ref] = Field(default_factory=list)
    user_decisions: list[dict[str, Any]] = Field(default_factory=list)


ModuleKey = Literal["opening_long", "opening_short", "product_intro", "selling_points", "campaign", "cta", "transition"]


class PublicProviderResponseView(DTO):
    """Read-only, allowlisted receipt projection; never a formal module.

    The Voice service must verify the exact response's job/item/attempt owner
    before constructing this view. A valid DTO alone grants no read authority.
    """
    content: str | None
    module_key: ModuleKey | None
    expected_module_key: ModuleKey | None
    receipt_ref: Ref
    usage: dict[str, JsonValue] = Field(default_factory=dict)
    usage_state: Literal["known", "partial", "pending", "unknown"]
    validation_state: Literal["pending", "accepted", "rejected", "unknown"]
    state: Literal["response_received"] = "response_received"
    is_formal_module: Literal[False] = False
    reusable: Literal[False] = False
    approvable: Literal[False] = False

    @field_validator("is_formal_module", "reusable", "approvable", mode="before")
    @classmethod
    def no_formal_authority(cls, value):
        if value is not False:
            raise ValueError("public response cannot grant formal module authority")
        return value

    @field_validator("receipt_ref")
    @classmethod
    def response_kind(cls, value):
        if value.kind != "provider_response":
            raise ValueError("public receipt must reference provider_response")
        return value

    @field_validator("usage", mode="before")
    @classmethod
    def public_usage_only(cls, value):
        flat = {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
        nested = {"prompt_tokens_details": {"cached_tokens"}, "completion_tokens_details": {"reasoning_tokens"}}
        valid_int = lambda number: type(number) is int and number >= 0
        if not isinstance(value, dict) or set(value) - flat - set(nested):
            raise ValueError("usage must contain only allowlisted public token counts")
        for key, number in value.items():
            if key in flat and not valid_int(number):
                raise ValueError("public token counts must be nonnegative integers")
            if key in nested and (not isinstance(number, dict) or set(number) - nested[key]
                    or any(not valid_int(count) for count in number.values())):
                raise ValueError("public usage detail is not an allowlisted count")
        return value
# Historical plans and scripts may contain transition, so retain the wire
# union and the legacy iteration order for read-only projections. New plans
# use ACTIVE_MODULE_KEYS and prepare only the opening required by the queue.
MODULE_KEYS = ("opening_long", "opening_short", "product_intro", "selling_points", "campaign", "cta", "transition")
LEGACY_MODULE_KEYS = MODULE_KEYS
OPENING_MODULE_KEYS = ("opening_long", "opening_short")
REQUIRED_BODY_MODULE_KEYS = ("product_intro", "selling_points", "cta")
ACTIVE_MODULE_KEYS = ("opening_long", "opening_short", "product_intro", "selling_points", "campaign", "cta")
PLAYBACK_STRUCTURE_VERSION = "prelive-position-v2"


class ModuleAction(DTO):
    key: ModuleKey
    action: Literal["generate", "patch", "reuse", "disable"]
    instruction: str = ""
    base_module_ref: Ref | None = None


class ScriptPlanItem(DTO):
    product_ref: Ref
    variant_id: str
    version_number: int = Field(default=1, ge=1)
    base_script_ref: Ref | None = None
    original_script_ref: Ref | None = Field(default=None,
        description="扩版所依据的精确原稿；仅作为创作依据，保留原稿且新方案ID必须不同。不是base_script_ref编辑。")
    generation_mode: Literal["auto", "whole_script", "module"] = Field(default="auto",
        description="auto新建整版generate默认一次生成全部所需模块；局部patch/reuse及旧job保留模块路径。不是强制总Agent工作流。")
    modules: list[ModuleAction] = Field(description=(
        "新计划仅配置实际需要的模块。product_intro、selling_points、cta必需；"
        "opening_long/opening_short至少一种有效，按保存队列第1、6、11项长开场，其余短开场。"
        "另一种开场可不提供，重排出现真实缺口时定向补齐；campaign可选。"
        "transition仅用于兼容历史读取/禁用/复用，不生成新衔接。"))


class ScriptPlanModuleBaseInvalidDetails(DTO):
    """One precise pre-write rejection; not a grant or provenance certificate."""
    model_config = ConfigDict(extra="forbid", strict=True)
    command: Literal["save_script_plan"]
    commit_state: Literal["not_committed"]
    preparation_ref: Ref
    bundle_ref: Ref
    item_index: int = Field(ge=0)
    module_key: ModuleKey
    requested_product_ref: Ref
    requested_variant_id: str
    requested_version_number: int = Field(ge=1)
    requested_base_module_ref: Ref
    requested_base_script_ref: None
    reason: Literal["BASE_SCRIPT_REQUIRED_FOR_EXPLICIT_MODULE_REUSE"]

    @field_validator("preparation_ref", "bundle_ref", "requested_product_ref", "requested_base_module_ref", mode="before")
    @classmethod
    def exact_requested_reference(cls, value, info):
        if not isinstance(value, (RecordRef, ArtifactRef)):
            if not isinstance(value, dict):
                raise ValueError("diagnostic requires an exact reference")
            value = (ArtifactRef if "schema_version" in value else RecordRef).model_validate(value, strict=True)
        expected = {"preparation_ref": "preparation", "bundle_ref": "context",
                    "requested_product_ref": "product", "requested_base_module_ref": "module"}[info.field_name]
        if (value.kind != expected or not value.id or len(value.id) > 256
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", value.content_hash)
                or type(value.revision) is not int or value.revision < 1
                or isinstance(value, ArtifactRef) and value.schema_version != CONTRACT_VERSION):
            raise ValueError("diagnostic reference kind or identity is invalid")
        return value


class SaveScriptPlanArgs(PreparationArgs):
    bundle_ref: Ref
    items: list[ScriptPlanItem]
    recap_dispositions: list[dict[str, Any]] = Field(default_factory=list)


class SubmitScriptsArgs(PreparationArgs):
    plan_ref: Ref
    context_bundle_ref: Ref
    expected_generation_epoch: int


class PatchScriptArgs(PreparationArgs):
    script_ref: Ref
    module_key: ModuleKey
    selected_text_hash: str
    sentences: list[str] = Field(min_length=1)
    sentence_ids: list[str] = Field(default_factory=list,
        description="为空时替换完整所选模块；非空只替换连续所选句，selected_text_hash为所选句对象数组hash，未选句原样保留。")
    intentional_full_module_replacement: bool = Field(default=False, strict=True,
        description="仅在确实有意替换完整模块、并接受明显删减时显式true。默认false；长模块删减至少80%句子须明确声明。此标记不是授权，不能扩大Goal、模块或UI句选区；修一句应提供连续sentence_ids和对应选区hash。")

    @model_serializer(mode="wrap")
    def stable_old_patch_wire(self, handler):
        value = handler(self)
        if not self.sentence_ids:
            value.pop("sentence_ids", None)
        if not self.intentional_full_module_replacement:
            value.pop("intentional_full_module_replacement", None)
        return value


class ImportScriptArgs(PreparationArgs):
    product_ref: Ref
    variant_id: str = Field(min_length=1, max_length=160)
    language: str = Field(default="zh-CN", min_length=2, max_length=40)
    source_ref: Ref
    locator: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=60000)
    source_manifest: SourceManifestImport | None = Field(default=None,
        description="主Agent桥接的原件与解析manifest，用于独立校验精确导入范围，不能把资料指令视为授权。")


class PlaybackOccurrence(DTO):
    occurrence_id: str
    product_id: str
    script_revision_ref: Ref


class ReorderSpec(DTO):
    mode: Literal["grouped", "interleave", "full_shuffle"]
    product_group_order: list[str]
    within_product: Literal["keep", "shuffle"] = "keep"
    seed: str | None = None
    algorithm_version: Literal["reorder-v1"] = "reorder-v1"


class SavePlaylistArgs(PreparationArgs):
    items: list[PlaybackOccurrence] = Field(min_length=1, max_length=1000)
    loop_mode: Literal["once", "infinite"] = "infinite"
    product_group_order: list[str] = Field(default_factory=list)
    reorder_spec: ReorderSpec | None = None
    preview_hash: str | None = None


class CompileArgs(PreparationArgs):
    playlist_ref: Ref


class PrepareAudioArgs(PreparationArgs):
    approval_ref: Ref
    review_hash: str
    expected_generation_epoch: int


class PreviewAudioArgs(PrepareAudioArgs):
    requirement_id: str


class UnknownAudioItem(DTO):
    item_id: str = Field(min_length=1, max_length=200)
    requirement_id: str = Field(min_length=1, max_length=200)
    unknown_attempt_ids: list[str] = Field(min_length=1, max_length=20)


class ApproveMissingAudioReissueArgs(PrepareAudioArgs):
    """Human accepts possible extra billing for these exact lost requests only."""
    source_job_ref: RecordRef
    requirements_ref: Ref
    unknown_items: list[UnknownAudioItem] = Field(min_length=1, max_length=10)
    binding_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    authorization_quote: str = Field(min_length=1, max_length=6000)


class ApproveScriptAudioPreviewArgs(PreparationArgs):
    """Independent human consent for one continuous, immutable script excerpt."""
    run_id: str = Field(min_length=1, max_length=200)
    script_ref: ArtifactRef
    module_keys: list[ModuleKey] = Field(min_length=1, max_length=6)
    module_refs: list[ArtifactRef] = Field(min_length=1, max_length=6)
    ordered_text_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tts_selection: VoiceSelection
    style: dict[str, JsonValue]
    authorization_quote: str = Field(min_length=1, max_length=6000)
    output_mode: Literal["continuous"] = "continuous"


class PreviewScriptAudioArgs(PreparationArgs):
    preview_approval_ref: ArtifactRef
    binding_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class JobArgs(DTO):
    job_id: str


class RetryJobArgs(JobArgs):
    recovery_mode: Literal["retry", "revalidate_archived"] = "retry"

    @model_serializer(mode="wrap")
    def stable_old_retry_wire(self, handler):
        value = handler(self)
        if self.recovery_mode == "retry":
            value.pop("recovery_mode", None)
        return value


def compatible_command_args(command, args):
    """Known newly added defaults only; all other values remain significant."""
    value = dict(args)
    if command == "patch_script" and value.get("sentence_ids") == []:
        value.pop("sentence_ids")
    if command == "patch_script" and value.get("intentional_full_module_replacement") is False:
        value.pop("intentional_full_module_replacement")
    if command == "retry_failed_items" and value.get("recovery_mode") == "retry":
        value.pop("recovery_mode")
    return value


def command_wire_candidates(command, args):
    canonical = compatible_command_args(command, args)
    values = [canonical]
    if command == "patch_script" and "sentence_ids" not in canonical:
        values.append({**canonical, "sentence_ids": []})
    if command == "patch_script" and "intentional_full_module_replacement" not in canonical:
        values += [{**value, "intentional_full_module_replacement": False} for value in values[:]]
    if command == "retry_failed_items" and "recovery_mode" not in canonical:
        values.append({**canonical, "recovery_mode": "retry"})
    return values


class PublishArgs(PreparationArgs):
    approval_ref: Ref


class AdoptArgs(DTO):
    publication_ref: Ref
    review_hash: str
    expected_room_revision: int


class StartArgs(AdoptArgs):
    action_id: str


class UndoArgs(DTO):
    change_id: str
    expected_revision: int


class IssueGrantArgs(DTO):
    run_id: str
    scope: dict[str, Any]
    capabilities: list[str]
    target_constraints: list[dict[str, Any]] = Field(default_factory=list)


class RevokeGrantArgs(DTO):
    run_id: str
    grant_id: str | None = None
    expected_write_generation: int | None = None
    reason: str = "任务已结束"


COMMAND_ARGS = {
    "import_blob": ImportBlobArgs, "import_recap": ImportRecapArgs,
    "save_asset_checklist": SaveAssetChecklistArgs, "link_recap_to_preparation": LinkRecapArgs,
    "approve_text_for_audio": ApproveTextArgs, "revoke_text_approval": RevokeApprovalArgs,
    "create_room": CreateRoomArgs, "create_preparation": CreatePreparationArgs,
    "save_product": SaveProductArgs, "update_preparation_products": UpdateProductsArgs,
    "refresh_product_reference": RefreshProductArgs, "record_product_revision_choice": RevisionChoiceArgs,
    "save_session_draft": SaveDraftArgs, "accept_analysis_report": AcceptAnalysisArgs,
    "save_context_bundle": SaveContextArgs, "save_script_plan": SaveScriptPlanArgs,
    "submit_script_change": SubmitScriptsArgs, "patch_script": PatchScriptArgs, "import_script": ImportScriptArgs,
    "save_playlist": SavePlaylistArgs, "compile_playback_plan": CompileArgs,
    "prepare_required_audio": PrepareAudioArgs, "preview_audio": PreviewAudioArgs,
    "approve_missing_audio_reissue": ApproveMissingAudioReissueArgs,
    "approve_script_audio_preview": ApproveScriptAudioPreviewArgs,
    "preview_script_audio": PreviewScriptAudioArgs,
    "retry_failed_items": RetryJobArgs, "cancel_job": JobArgs,
    "publish_preparation": PublishArgs, "adopt_snapshot": AdoptArgs,
    "start_prepared_room": StartArgs, "undo_mutation": UndoArgs,
    "issue_grant": IssueGrantArgs, "revoke_grant": RevokeGrantArgs,
}


class QueryArgs(DTO):
    preparation_id: str | None = None
    room_id: str | None = None
    product_id: str | None = None
    ref: Ref | None = None
    q: str = ""
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)
    scope_id: str | None = Field(default=None, min_length=1, max_length=200)
    include_catalog: bool = False
    order: Literal["asc", "desc"] = "asc"
    job_id: str | None = None
    operation_id: str | None = None
    principal_id: str | None = None
    sections: list[str] = Field(default_factory=list)
    module_key: ModuleKey | None = Field(default=None,
        description="read_script句级选区：与非空sentence_ids一起提供，读取该精确模块的连续原句与可用于patch_script的选区hash；不授权修改。")
    sentence_ids: list[str] | None = Field(default=None, max_length=1000,
        description="read_script句级选区的原句ID；须属于同一module_key且连续。返回selection中服务端计算的selected_text_hash，不自行计算或猜测。省略时保持原查询。")
    spec: ReorderSpec | None = None
    fields: dict[str, Any] | None = None
    schema_hash: str | None = None
    provider: str | None = None
    byte_start: int = Field(default=0, ge=0)
    byte_end: int | None = Field(default=None, ge=0)
    context: dict[str, Any] = Field(default_factory=dict)


QUERY_NAMES = (
    "read_capabilities", "get_business_schema", "search_products", "read_product",
    "read_preparation", "read_room", "read_analysis_report", "preview_playlist_reorder",
    "find_audience_research", "read_session_history", "inspect_change_impact", "read_asset_checklist",
    "read_recap", "create_text_review", "read_text_approval", "read_audio_readiness", "read_context", "read_script",
    "inspect_missing_audio_reissue",
    "read_script_audio_preview",
    "list_script_audio_previews",
    "read_script_usage", "voice_options", "validate_product_assembly", "get_workspace_snapshot",
    "read_completion_observation", "read_job", "create_review", "read_operation_receipt",
    "read_events", "read_blob",
)

_QUERY_FIELDS = {
    "read_capabilities": ((), ()), "get_business_schema": ((), ()),
    "search_products": ((), ("q", "cursor", "limit")),
    "read_product": ((), ("product_id", "ref")),
    "read_preparation": (("preparation_id",), ("sections",)), "read_room": (("room_id",), ()),
    "read_analysis_report": (("ref",), ()),
    "preview_playlist_reorder": (("preparation_id", "spec"), ("ref",)),
    "find_audience_research": ((), ("product_id", "ref", "context", "cursor", "limit")),
    "read_session_history": ((), ("preparation_id", "room_id", "cursor", "limit")),
    "inspect_change_impact": ((), ("product_id", "ref", "fields")),
    "read_asset_checklist": ((), ("preparation_id", "ref")),
    "read_recap": (("ref",), ("cursor", "limit")),
    "create_text_review": (("preparation_id",), ()),
    "read_text_approval": (("preparation_id",), ("ref",)),
    "read_script_audio_preview": ((), ("preparation_id", "ref", "operation_id")),
    "list_script_audio_previews": (("preparation_id",), ("ref", "cursor", "limit")),
    "read_audio_readiness": (("preparation_id",), ("cursor", "limit")),
    "inspect_missing_audio_reissue": (("preparation_id", "job_id"), ()),
    "read_context": (("ref",), ("sections",)), "read_script": (("ref",), ("sections", "module_key", "sentence_ids")),
    "read_script_usage": (("ref",), ()), "voice_options": ((), ("provider",)),
    "validate_product_assembly": (("fields", "schema_hash"), ()),
    "get_workspace_snapshot": ((), ("preparation_id", "cursor", "limit")),
    "read_completion_observation": ((), ("preparation_id",)),
    "read_job": (("job_id",), ()), "create_review": ((), ("preparation_id", "ref")),
    "read_operation_receipt": (("operation_id",), ("principal_id",)),
    "read_events": ((), ("cursor", "limit", "scope_id", "include_catalog", "order")),
    "read_blob": (("ref",), ("byte_start", "byte_end")),
}
QUERY_ARGS = {}
for _query, (_required, _optional) in _QUERY_FIELDS.items():
    _fields = {}
    for _field in (*_required, *_optional):
        _info = QueryArgs.model_fields[_field]
        _default = ... if _field in _required else Field(default_factory=_info.default_factory) if _info.default_factory else _info.default
        if _query == "read_audio_readiness":
            _default = {"preparation_id": Field(..., min_length=1), "cursor": Field(default=0, ge=0),
                "limit": Field(default=20, ge=1, le=20)}[_field]
        _annotation = _info.annotation
        if _query == "read_preparation" and _field == "sections":
            _annotation, _default = list[Literal["script_plan"]], Field(default_factory=list, max_length=1)
        if _field in _required and get_origin(_annotation) in (Union, UnionType):
            # QueryArgs is a permissive shared carrier. A concrete query's
            # required input must not inherit its nullable carrier annotation:
            # otherwise ref:null passes the tool and exclude_none silently
            # removes it before backend validation, aborting the whole branch.
            _members = tuple(member for member in get_args(_annotation) if member is not type(None))
            _annotation = _members[0] if len(_members) == 1 else Union[_members]
        _fields[_field] = (_annotation, _default)
    _name = "".join(part.title() for part in _query.split("_")) + "QueryArgs"
    _model = create_model(_name, __base__=DTO, **_fields)
    globals()[_name] = _model
    QUERY_ARGS[_query] = _model


# Each command is a closed discriminated model, rather than a free-form
# executable function name. The registry above remains the single source.
COMMAND_MODELS = {}
for _command, _args in COMMAND_ARGS.items():
    _name = "".join(part.title() for part in _command.split("_")) + "Command"
    _model = create_model(_name, __base__=DTO,
                          contract_version=(Literal["PRELIVE-2.3"], "PRELIVE-2.3"),
                          command=(Literal[_command], ...),
                          args=(_args, ...), mutation=(MutationContext, ...))
    globals()[_name] = _model
    COMMAND_MODELS[_command] = _model
VoiceCommand = Annotated[Union[tuple(COMMAND_MODELS.values())], Field(discriminator="command")]
VOICE_COMMAND_ADAPTER = TypeAdapter(VoiceCommand)


def parse_args(command: str, value: dict[str, Any]) -> DTO:
    if command not in COMMAND_ARGS:
        raise ValueError("UNKNOWN_COMMAND")
    return COMMAND_ARGS[command].model_validate(value)


def parse_query_args(query: str, value: dict[str, Any]) -> QueryArgs:
    if query not in QUERY_ARGS:
        raise ValueError("UNKNOWN_QUERY")
    parsed = QUERY_ARGS[query].model_validate(value)
    return QueryArgs.model_validate(parsed.model_dump(exclude_none=True))


def export_schema() -> dict[str, Any]:
    return {"contract_version": CONTRACT_VERSION,
            "script_structure": {"compiler_version": PLAYBACK_STRUCTURE_VERSION,
                "active_module_keys": list(ACTIVE_MODULE_KEYS), "legacy_module_keys": list(LEGACY_MODULE_KEYS),
                "required_body_module_keys": list(REQUIRED_BODY_MODULE_KEYS),
                "opening_module_keys": list(OPENING_MODULE_KEYS),
                "opening_policy": "saved_position_every_five", "independent_transition": False},
            "commands": {k: v.model_json_schema() for k, v in COMMAND_ARGS.items()},
            "command_envelope": VOICE_COMMAND_ADAPTER.json_schema(),
            "queries": {k: v.model_json_schema() for k, v in QUERY_ARGS.items()}}
