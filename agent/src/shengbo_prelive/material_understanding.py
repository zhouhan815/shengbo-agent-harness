"""Typed, source-aware assessments produced by the existing analysis Agents.

This module does not classify merchant prose, infer product identity, or call an
LLM.  It validates the Agent's explicit interpretation, compares only explicitly
comparable observations, and groups task-specific missing information.  Formal
source authorization and evidence readback remain the runtime's responsibility.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from shengbo_contracts.common import ArtifactRef, RecordRef, StrictModel


MATERIAL_PURPOSES = frozenset({"catalog_update", "facts_analysis", "audience_research", "session_config",
                              "script_generation", "script_change", "prepare_session", "read_explain",
                              "audio_prepare", "playlist_reorder"})
MATERIAL_PURPOSE_ALIASES = {"generate_script": "script_generation", "materials_to_script": "script_generation",
                            "product_update": "catalog_update", "save_product": "catalog_update"}
MaterialPurpose = Literal[tuple(sorted(MATERIAL_PURPOSES))]


def normalize_material_purpose(value: str) -> str:
    """Canonicalize only known task names; an unknown purpose cannot imply ready."""
    if not isinstance(value, str):
        raise ValueError("material purpose must be a known string")
    purpose = MATERIAL_PURPOSE_ALIASES.get(value, value)
    if purpose not in MATERIAL_PURPOSES:
        raise ValueError("unknown material purpose")
    return purpose


class AssessmentDTO(StrictModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class ProductCandidate(AssessmentDTO):
    candidate_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=500)
    product_id: str | None = None
    product_ref: ArtifactRef | RecordRef | None = None
    identity_status: Literal["new", "existing", "ambiguous"] = "ambiguous"
    sku_keys: list[str] = Field(default_factory=list, max_length=200)
    evidence_ids: list[str] = Field(default_factory=list, max_length=200)
    identity_note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def existing_identity(self):
        if self.identity_status == "existing" and not (self.product_id or self.product_ref):
            raise ValueError("existing product identity requires product_id or product_ref")
        if self.product_ref is not None and self.product_ref.kind != "product":
            raise ValueError("product_ref must reference a product")
        if self.product_ref and self.product_id and self.product_ref.id != self.product_id:
            raise ValueError("product_id and product_ref must identify the same product")
        return self


from .material_roles import CONTENT_ROLES


class ContentGroup(AssessmentDTO):
    """A classified material region, not an authorized script-import command."""
    group_id: str = Field(min_length=1, max_length=160)
    role: Literal[CONTENT_ROLES]
    product_candidate_ids: list[str] = Field(default_factory=list, max_length=200)
    language: str | None = Field(default=None, max_length=100)
    script_variant_label: str | None = Field(default=None, max_length=200)
    evidence_ids: list[str] = Field(default_factory=list, max_length=200)
    note: str = Field(default="", max_length=2000)


class FieldObservation(AssessmentDTO):
    observation_id: str = Field(min_length=1, max_length=160)
    product_candidate_id: str = Field(min_length=1, max_length=160)
    sku_key: str | None = Field(default=None, max_length=200)
    field_path: str = Field(min_length=1, max_length=300)
    value: JsonValue = None
    unit: str | None = Field(default=None, max_length=100)
    measurement_basis: str | None = Field(default=None, max_length=300)
    parameter_role: str | None = Field(default=None, max_length=200)
    scope: Literal["catalog", "session", "unknown"] = "unknown"
    scope_id: str | None = Field(default=None, max_length=200)
    claim_kind: Literal["fact", "merchant_claim", "script_claim", "research_hypothesis"] = "merchant_claim"
    knowledge: Literal["known", "unknown", "not_applicable"] = "known"
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    # A semantic decision, never inferred merely from a common field name.
    comparison_eligible: bool = False

    @model_validator(mode="after")
    def sourced_known_fact(self):
        if self.knowledge == "known" and self.claim_kind == "fact" and not self.evidence_ids:
            raise ValueError("known facts require evidence_ids")
        if self.knowledge == "known" and self.claim_kind == "fact" and self.value is None:
            raise ValueError("known facts require a value; mark unavailable values unknown")
        if self.knowledge != "known" and self.value is not None:
            raise ValueError("unknown or not_applicable observation must not fabricate a value")
        return self


class MissingInfoItem(AssessmentDTO):
    item_id: str = Field(min_length=1, max_length=160)
    product_candidate_id: str | None = Field(default=None, max_length=160)
    field_path: str = Field(min_length=1, max_length=300)
    level: Literal["block_required", "conditional", "recommended", "optional"]
    why: str = Field(min_length=1, max_length=3000)
    question: str = Field(min_length=1, max_length=3000)
    applicable_purposes: list[MaterialPurpose] = Field(default_factory=list, max_length=30)
    blocked_actions: list[str] = Field(default_factory=list, max_length=30)
    condition: str | None = Field(default=None, max_length=2000)
    condition_active: bool = False
    can_skip: bool = True
    alternatives: list[str] = Field(default_factory=list, max_length=20)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    status: Literal["unresolved", "answered", "omitted_from_task", "not_applicable"] = "unresolved"

    @field_validator("applicable_purposes", mode="before")
    @classmethod
    def canonical_purposes(cls, values):
        if not isinstance(values, list):
            raise ValueError("applicable_purposes must be a list")
        return list(dict.fromkeys(normalize_material_purpose(value) for value in values))

    @model_validator(mode="after")
    def explicit_blocking_conditions(self):
        if self.level == "conditional" and not self.condition:
            raise ValueError("conditional missing information requires an explicit condition")
        if self.level in {"block_required", "conditional"} and not self.blocked_actions:
            raise ValueError("blocking missing information must name affected actions")
        if self.level in {"recommended", "optional"} and not self.can_skip:
            raise ValueError("recommended and optional information must remain skippable")
        return self


class MaterialConflict(AssessmentDTO):
    conflict_id: str = Field(min_length=1, max_length=160)
    product_candidate_id: str | None = Field(default=None, max_length=160)
    field_path: str = Field(min_length=1, max_length=300)
    observation_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    explanation: str = Field(min_length=1, max_length=3000)
    status: Literal["unresolved", "resolved", "different_scope", "nonblocking"] = "unresolved"
    blocked_actions: list[str] = Field(default_factory=list, max_length=30)
    applicable_purposes: list[MaterialPurpose] = Field(default_factory=list, max_length=30)
    resolution: str | None = Field(default=None, max_length=3000)
    question: str | None = Field(default=None, max_length=3000)

    @field_validator("applicable_purposes", mode="before")
    @classmethod
    def canonical_purposes(cls, values):
        if not isinstance(values, list):
            raise ValueError("applicable_purposes must be a list")
        return list(dict.fromkeys(normalize_material_purpose(value) for value in values))

    @model_validator(mode="after")
    def resolved_explanation(self):
        if self.status == "resolved" and not self.resolution:
            raise ValueError("resolved conflict requires a resolution")
        return self


class BenefitMapping(AssessmentDTO):
    mapping_id: str = Field(min_length=1, max_length=160)
    product_candidate_id: str = Field(min_length=1, max_length=160)
    audience_need: str = Field(min_length=1, max_length=2000)
    use_scene: str = Field(min_length=1, max_length=2000)
    supported_observation_ids: list[str] = Field(min_length=1, max_length=100)
    selling_point: str = Field(min_length=1, max_length=2000)
    talking_focus: str = Field(min_length=1, max_length=3000)
    priority: int = Field(default=1, ge=1, le=100)
    research_status: Literal["provided", "reused", "researched", "hypothesis"] = "hypothesis"
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class VisualReview(AssessmentDTO):
    """An Agent judgement about one real visual region, separate from delivery.

    Receipt/locator validation proves access and coordinates, never the meaning
    of the image or the truth of the Agent's summary.
    """
    review_id: str = Field(min_length=1, max_length=160)
    source_ref: ArtifactRef
    locator: str = Field(pattern=r"^(image|page):[1-9][0-9]*$", max_length=100)
    product_candidate_ids: list[str] = Field(default_factory=list, max_length=200)
    status: Literal["reviewed", "unread", "unreadable"]
    relevance: Literal["relevant", "irrelevant", "unknown"] = "unknown"
    summary: str = Field(min_length=1, max_length=3000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    read_receipt_ids: list[str] = Field(default_factory=list, max_length=30)
    reuse_note: str = Field(default="", max_length=2000)
    critical_for_actions: list[str] = Field(default_factory=list, max_length=30)
    missing_info_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("source_ref", mode="before")
    @classmethod
    def strict_source_reference(cls, value):
        return ArtifactRef.model_validate(value, strict=True)

    @model_validator(mode="after")
    def explicit_visual_state(self):
        if self.source_ref.kind != "source_manifest":
            raise ValueError("visual review requires an exact uploaded source_manifest")
        if self.status == "reviewed" and (not self.evidence_ids or not self.read_receipt_ids):
            raise ValueError("reviewed visual requires evidence_ids and successful read_receipt_ids")
        if self.status != "reviewed" and self.relevance == "irrelevant":
            # Exclusion can be a scoped judgement from an index, but it cannot
            # masquerade as an inspection of the unseen pixels.
            if self.critical_for_actions:
                raise ValueError("an irrelevant unread visual cannot also be critical for current actions")
        if self.status != "reviewed" and self.critical_for_actions and not self.missing_info_ids:
            raise ValueError("unread critical visual requires linked missing_info_ids")
        return self


class MaterialAssessment(AssessmentDTO):
    schema_version: Literal["material-assessment.v1"] = "material-assessment.v1"
    product_candidates: list[ProductCandidate] = Field(default_factory=list, max_length=200)
    content_groups: list[ContentGroup] = Field(default_factory=list, max_length=1000)
    field_observations: list[FieldObservation] = Field(default_factory=list, max_length=3000)
    missing_info: list[MissingInfoItem] = Field(default_factory=list, max_length=500)
    conflicts: list[MaterialConflict] = Field(default_factory=list, max_length=500)
    benefit_mappings: list[BenefitMapping] = Field(default_factory=list, max_length=1000)
    reused_research_refs: list[ArtifactRef | RecordRef] = Field(default_factory=list, max_length=100)
    notes: list[str] = Field(default_factory=list, max_length=100)
    visual_reviews: list[VisualReview] | None = Field(default=None, max_length=3000,
        exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def internally_consistent(self):
        def unique(items, key):
            identifiers = [getattr(item, key) for item in items]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"duplicate {key}")
            return set(identifiers)

        candidates = unique(self.product_candidates, "candidate_id")
        observations = unique(self.field_observations, "observation_id")
        unique(self.content_groups, "group_id")
        unique(self.missing_info, "item_id")
        unique(self.conflicts, "conflict_id")
        unique(self.benefit_mappings, "mapping_id")
        unique(self.visual_reviews or [], "review_id")
        by_id = {item.observation_id: item for item in self.field_observations}
        missing = {item.item_id: item for item in self.missing_info}
        visual_keys = set()
        for review in self.visual_reviews or []:
            if set(review.product_candidate_ids) - candidates:
                raise ValueError("visual review must reference declared candidates")
            key = (review.source_ref.model_dump_json(), review.locator,
                   tuple(sorted(review.product_candidate_ids)))
            if key in visual_keys:
                raise ValueError("duplicate visual source/locator/product scope")
            visual_keys.add(key)
            if set(review.missing_info_ids) - set(missing):
                raise ValueError("visual review must reference declared missing information")
            if review.status != "reviewed" and review.critical_for_actions:
                covered = set()
                for identity in review.missing_info_ids:
                    gap = missing[identity]
                    if gap.product_candidate_id and gap.product_candidate_id not in review.product_candidate_ids:
                        raise ValueError("critical visual cannot borrow another product's missing information")
                    if gap.status == "unresolved" and (gap.level == "block_required" or
                            (gap.level == "conditional" and gap.condition_active)):
                        covered.update(gap.blocked_actions)
                from .action_contracts import matching_actions
                if len(matching_actions(review.critical_for_actions, list(covered))) != len(review.critical_for_actions):
                    raise ValueError("unread critical visual requires active required gaps for affected actions")
        for item in [*self.field_observations, *self.missing_info, *self.conflicts, *self.benefit_mappings]:
            if item.product_candidate_id is not None and item.product_candidate_id not in candidates:
                raise ValueError("product_candidate_id must reference a declared candidate")
        for group in self.content_groups:
            if set(group.product_candidate_ids) - candidates:
                raise ValueError("content group must reference declared candidates")
        for conflict in self.conflicts:
            if set(conflict.observation_ids) - observations:
                raise ValueError("conflict must reference declared observations")
            if any(by_id[key].product_candidate_id != conflict.product_candidate_id
                   for key in conflict.observation_ids) and conflict.product_candidate_id is not None:
                raise ValueError("conflict cannot silently combine different products")
        for mapping in self.benefit_mappings:
            if set(mapping.supported_observation_ids) - observations:
                raise ValueError("benefit mapping must reference declared observations")
            for key in mapping.supported_observation_ids:
                observation = by_id[key]
                if observation.product_candidate_id != mapping.product_candidate_id:
                    raise ValueError("benefit mapping cannot borrow another product's feature")
                if observation.claim_kind != "fact" or observation.knowledge != "known":
                    raise ValueError("benefit mapping requires known product facts, not script claims or hypotheses")
        return self


def validate_material_assessment(value: MaterialAssessment | dict | None, *,
                                 legacy_missing_fields: list[str] | None = None) -> MaterialAssessment:
    """Validate an explicit assessment without treating legacy strings as gates.

    legacy_missing_fields is accepted for migration; it is only surfaced by the
    readiness projection and never guessed into a severity or rewritten as facts.
    """
    return value if isinstance(value, MaterialAssessment) else MaterialAssessment.model_validate(value or {})


def _applies(purposes: list[str], purpose: str) -> bool:
    return not purposes or purpose in {normalize_material_purpose(value) for value in purposes}


def assess_material_readiness(value: MaterialAssessment | dict | None, *, purpose: str,
                              requested_actions: list[str] | None = None,
                              legacy_missing_fields: list[str] | None = None) -> dict:
    """Project task-specific blockers and one grouped supplement list.

    ``ready`` only means no declared active blocker for these actions; it is not
    proof of semantic correctness, approval, product persistence, or script safety.
    Supplying [] actions is different from omitting them: [] is a read-only view.
    """
    requested_purpose = purpose
    purpose = normalize_material_purpose(purpose)
    assessment = validate_material_assessment(value)
    from .action_contracts import matching_actions
    action_filter = requested_actions
    groups: dict[str | None, list[dict]] = defaultdict(list)
    blockers: list[dict] = []
    counts = {level: 0 for level in ("block_required", "conditional", "recommended", "optional")}
    for item in assessment.missing_info:
        if item.status != "unresolved" or not _applies(item.applicable_purposes, purpose):
            continue
        row = item.model_dump(mode="json")
        affected = matching_actions(item.blocked_actions, action_filter)
        active = bool(affected) and (item.level == "block_required" or
                                    (item.level == "conditional" and item.condition_active))
        row.update({"blocks_current_task": active, "active_blocked_actions": affected if active else []})
        counts[item.level] += 1
        groups[item.product_candidate_id].append(row)
        if active:
            blockers.append({"kind": "missing_info", "item_id": item.item_id,
                             "product_candidate_id": item.product_candidate_id,
                             "field_path": item.field_path, "blocked_actions": affected})
    for item in assessment.conflicts:
        if item.status != "unresolved" or not _applies(item.applicable_purposes, purpose):
            continue
        affected = matching_actions(item.blocked_actions, action_filter)
        row = item.model_dump(mode="json")
        row.update({"kind": "conflict", "blocks_current_task": bool(affected), "active_blocked_actions": affected})
        groups[item.product_candidate_id].append(row)
        if affected:
            blockers.append({"kind": "conflict", "item_id": item.conflict_id,
                             "product_candidate_id": item.product_candidate_id,
                             "field_path": item.field_path, "blocked_actions": affected})
    # A declared critical unseen source cannot be made ready merely by placing
    # its linked gap under an unrelated purpose. Actions remain task-scoped;
    # no visual importance is inferred by the program.
    for review in assessment.visual_reviews or []:
        if review.status == "reviewed":
            continue
        affected = matching_actions(review.critical_for_actions, action_filter)
        if affected:
            blockers.append({"kind": "visual_unread", "item_id": review.review_id,
                             "product_candidate_ids": review.product_candidate_ids,
                             "source_ref": review.source_ref.model_dump(mode="json"),
                             "locator": review.locator, "missing_info_ids": review.missing_info_ids,
                             "blocked_actions": affected})
    typed_fields = {item.field_path for item in assessment.missing_info}
    legacy = list(dict.fromkeys(field for field in legacy_missing_fields or [] if field not in typed_fields))
    return {
        "schema_version": assessment.schema_version, "purpose": purpose, "requested_purpose": requested_purpose,
        "requested_actions": requested_actions,
        "ready": not blockers,
        "blocked_actions": sorted({action for blocker in blockers for action in blocker["blocked_actions"]}),
        "blockers": blockers,
        "questions_by_product": [{"product_candidate_id": key, "items": items}
                                 for key, items in groups.items()],
        "quality_counts": {"products": len(assessment.product_candidates),
                           "content_groups": len(assessment.content_groups),
                           "observations": len(assessment.field_observations),
                           "benefit_mappings": len(assessment.benefit_mappings),
                           "ambiguous_product_candidates": sum(item.identity_status == "ambiguous"
                                                               for item in assessment.product_candidates),
                           "unknown_observations": sum(item.knowledge == "unknown"
                                                       for item in assessment.field_observations),
                           "script_claims": sum(item.claim_kind == "script_claim"
                                                for item in assessment.field_observations),
                           "unresolved_conflicts": sum(item.status == "unresolved"
                                                       for item in assessment.conflicts),
                           "missing_by_level": counts},
        "visual_review_coverage": {
            "state": "declared_subset" if assessment.visual_reviews else "unknown",
            "reviewed": sum(item.status == "reviewed" for item in assessment.visual_reviews or []),
            "unread": sum(item.status == "unread" for item in assessment.visual_reviews or []),
            "unreadable": sum(item.status == "unreadable" for item in assessment.visual_reviews or []),
            "complete_source_coverage_verified": False,
            "semantic_support_verified": False,
        },
        "legacy_unclassified_fields": legacy,
        "interpretation": "declared_assessment_projection_not_semantic_verification",
    }


def _comparable_value(value):
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float, str)):
        try:
            number = Decimal(str(value).strip())
            if number.is_finite():
                return ("number", number)
        except (InvalidOperation, ValueError):
            pass
    # Text spelling differences and structured values need semantic resolution;
    # do not turn case, wording or display formatting into new numeric conflicts.
    return None


def find_exact_scope_conflicts(observations: list[FieldObservation | dict]) -> list[dict]:
    """Find differing numeric/bool candidates only after explicit scope matching.

    This is a hint for targeted review, not an automatic blocking/precedence rule.
    Script claims, hypotheses, unknown scope, and unresolved product attribution
    must be classified by the Agent before invoking this comparison.
    """
    groups: dict[tuple, list[tuple[FieldObservation, tuple]]] = defaultdict(list)
    for raw in observations:
        item = raw if isinstance(raw, FieldObservation) else FieldObservation.model_validate(raw)
        if (not item.comparison_eligible or item.knowledge != "known" or item.scope == "unknown"
                or item.claim_kind != "fact"):
            continue
        value = _comparable_value(item.value)
        if value is None:
            continue
        key = (item.product_candidate_id, item.sku_key, item.field_path,
               item.unit, item.measurement_basis, item.parameter_role, item.scope, item.scope_id)
        groups[key].append((item, value))
    conflicts = []
    for key, entries in groups.items():
        if len({value for _, value in entries}) < 2:
            continue
        conflicts.append({
            "product_candidate_id": key[0], "sku_key": key[1], "field_path": key[2],
            "unit": key[3], "measurement_basis": key[4], "parameter_role": key[5],
            "scope": key[6], "scope_id": key[7],
            "observation_ids": [item.observation_id for item, _ in entries],
            "values": [item.value for item, _ in entries],
            "evidence_ids": list(dict.fromkeys(evidence for item, _ in entries for evidence in item.evidence_ids)),
            "status": "candidate_requires_semantic_review", "automatically_blocks": False,
        })
    return conflicts
