"""No-provider acceptance cases for the merchant-material interpretation contract."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from shengbo_prelive.material_understanding import (
    MaterialAssessment,
    assess_material_readiness,
    find_exact_scope_conflicts,
    validate_material_assessment,
)


def assessment():
    return {
        "product_candidates": [
            {"candidate_id": "power-bank-10k", "name": "小米1万移动电源", "identity_status": "new",
             "sku_keys": ["10000mAh"], "evidence_ids": ["e-name"]},
            {"candidate_id": "power-bank-20k", "name": "小米2万移动电源", "identity_status": "new",
             "sku_keys": ["20000mAh"], "evidence_ids": ["e-name-20k"]},
        ],
        "content_groups": [
            {"group_id": "cn", "role": "existing_script", "product_candidate_ids": ["power-bank-10k"],
             "language": "zh-CN", "script_variant_label": "方案A", "evidence_ids": ["e-script-cn"]},
            {"group_id": "ms", "role": "existing_script", "product_candidate_ids": ["power-bank-10k"],
             "language": "ms", "script_variant_label": "方案A译稿", "evidence_ids": ["e-script-ms"]},
            {"group_id": "ops", "role": "presentation_instruction", "evidence_ids": ["e-ops"]},
        ],
        "field_observations": [
            {"observation_id": "wireless", "product_candidate_id": "power-bank-10k", "sku_key": "10000mAh",
             "field_path": "power", "value": 15, "unit": "W", "parameter_role": "wireless_output",
             "scope": "catalog", "claim_kind": "fact", "evidence_ids": ["e-wireless"], "comparison_eligible": True},
            {"observation_id": "input", "product_candidate_id": "power-bank-10k", "sku_key": "10000mAh",
             "field_path": "power", "value": 22.5, "unit": "W", "parameter_role": "input",
             "scope": "catalog", "claim_kind": "fact", "evidence_ids": ["e-input"], "comparison_eligible": True},
        ],
    }


def observation(**changes):
    value = deepcopy(assessment()["field_observations"][0])
    value.update(changes)
    return value


def missing(**changes):
    item = {"item_id": "price", "product_candidate_id": "power-bank-10k", "field_path": "currency",
            "level": "conditional", "why": "报具体价需要明确币种", "question": "本次报价使用什么币种？",
            "applicable_purposes": ["generate_script"], "blocked_actions": ["include_price"],
            "condition": "本次口播包含具体报价", "condition_active": True, "can_skip": True,
            "alternatives": ["本版暂不报价"], "evidence_ids": ["e-price"]}
    item.update(changes)
    return item


def test_preserves_product_sku_language_and_region_roles_separately():
    result = MaterialAssessment.model_validate(assessment())
    assert len(result.product_candidates) == 2
    assert result.content_groups[0].product_candidate_ids == result.content_groups[1].product_candidate_ids
    assert result.content_groups[0].language != result.content_groups[1].language
    assert result.content_groups[2].role == "presentation_instruction"


def test_input_and_output_power_are_not_a_numeric_conflict():
    assert find_exact_scope_conflicts(assessment()["field_observations"]) == []


@pytest.mark.parametrize("different_dimension", [
    {"product_candidate_id": "power-bank-20k"},
    {"sku_key": "20000mAh"},
    {"field_path": "rated_power"},
    {"unit": "mW"},
    {"measurement_basis": "rated"},
    {"parameter_role": "input"},
    {"scope": "session", "scope_id": "valentine"},
    {"scope_id": "another-market"},
])
def test_numeric_comparison_never_crosses_scope_dimensions(different_dimension):
    original = observation()
    other = observation(observation_id="other", value=22.5, **different_dimension)
    assert find_exact_scope_conflicts([original, other]) == []


def test_cell_and_rated_capacities_are_not_the_same_field_basis():
    cell = observation(field_path="capacity", value=10000, unit="mAh", parameter_role=None,
                       measurement_basis="cell")
    rated = observation(observation_id="rated", field_path="capacity", value=5500, unit="mAh",
                        parameter_role=None, measurement_basis="rated")
    assert find_exact_scope_conflicts([cell, rated]) == []


def test_same_scope_conflict_is_a_candidate_not_an_automatic_blocker():
    found = find_exact_scope_conflicts([observation(), observation(observation_id="second", value=12)])
    assert len(found) == 1
    assert found[0]["values"] == [15, 12]
    assert found[0]["status"] == "candidate_requires_semantic_review"
    assert found[0]["automatically_blocks"] is False


def test_numeric_format_difference_does_not_create_a_conflict():
    assert find_exact_scope_conflicts([observation(value=15), observation(observation_id="two", value="15.0")]) == []


def test_script_claim_hypothesis_and_unclassified_comparison_are_not_promoted():
    assert find_exact_scope_conflicts([
        observation(), observation(observation_id="script", value=12, claim_kind="script_claim"),
        observation(observation_id="hypothesis", value=20, claim_kind="research_hypothesis"),
        observation(observation_id="unclassified", value=19, comparison_eligible=False),
    ]) == []


def test_conditional_price_only_blocks_the_requested_pricing_action():
    value = assessment()
    value["missing_info"] = [missing()]
    included = assess_material_readiness(value, purpose="generate_script", requested_actions=["include_price"])
    excluded = assess_material_readiness(value, purpose="generate_script", requested_actions=["generate_neutral_script"])
    assert included["ready"] is False
    assert included["blocked_actions"] == ["include_price"]
    assert excluded["ready"] is True
    assert excluded["questions_by_product"][0]["items"][0]["blocks_current_task"] is False


def test_inactive_or_omitted_conditions_and_unrelated_purposes_do_not_block():
    for item in [missing(condition_active=False), missing(status="omitted_from_task"), missing(status="answered")]:
        value = assessment()
        value["missing_info"] = [item]
        assert assess_material_readiness(value, purpose="generate_script", requested_actions=["include_price"])["ready"]
    value["missing_info"] = [missing()]
    assert assess_material_readiness(value, purpose="facts_analysis", requested_actions=["include_price"])["ready"]


def test_recommended_and_optional_items_never_become_task_gates():
    value = assessment()
    value["missing_info"] = [missing(item_id="brand", field_path="brand_story", level="recommended"),
                             missing(item_id="video", field_path="video", level="optional")]
    result = assess_material_readiness(value, purpose="generate_script", requested_actions=["include_price"])
    assert result["ready"] is True
    assert len(result["questions_by_product"]) == 1
    assert len(result["questions_by_product"][0]["items"]) == 2
    assert result["quality_counts"]["missing_by_level"]["recommended"] == 1


def test_required_identity_and_price_conflict_share_one_product_question_group():
    value = assessment()
    value["missing_info"] = [missing(item_id="identity", field_path="sku", level="block_required",
                                     blocked_actions=["save_product"], condition=None)]
    value["conflicts"] = [{"conflict_id": "two-prices", "product_candidate_id": "power-bank-10k",
                           "field_path": "price", "explanation": "同SKU的口播与参数报价不同，需核实",
                           "blocked_actions": ["include_price"], "question": "哪一组报价有效？"}]
    result = assess_material_readiness(value, purpose="generate_script", requested_actions=["save_product", "include_price"])
    assert result["blocked_actions"] == ["include_price", "save_product"]
    assert len(result["questions_by_product"]) == 1
    assert len(result["questions_by_product"][0]["items"]) == 2


def test_empty_action_list_is_read_only_and_does_not_invent_authority():
    value = assessment()
    value["missing_info"] = [missing()]
    result = assess_material_readiness(value, purpose="generate_script", requested_actions=[])
    assert result["ready"] is True
    assert result["interpretation"] == "declared_assessment_projection_not_semantic_verification"


def test_legacy_missing_strings_are_preserved_without_automatic_severity():
    value = assessment()
    value["missing_info"] = [missing()]
    result = assess_material_readiness(value, purpose="facts_analysis", legacy_missing_fields=["currency", "brand_story", "brand_story"])
    assert result["legacy_unclassified_fields"] == ["brand_story"]
    assert result["ready"] is True
    assert validate_material_assessment(None, legacy_missing_fields=["price"]).product_candidates == []


def test_unknown_price_and_currency_remain_unknown_not_zero_or_cny():
    value = assessment()
    value["field_observations"] = [observation(value=None, knowledge="unknown", unit=None,
                                             claim_kind="merchant_claim", evidence_ids=[])]
    result = MaterialAssessment.model_validate(value)
    saved = result.model_dump(mode="json")["field_observations"][0]
    assert saved["value"] is None
    assert saved["unit"] is None
    with pytest.raises(ValidationError, match="must not fabricate"):
        MaterialAssessment.model_validate({**value, "field_observations": [observation(value=0, knowledge="unknown")]})


def test_need_to_evidence_to_benefit_to_talking_focus_mapping():
    value = assessment()
    value["benefit_mappings"] = [{"mapping_id": "travel", "product_candidate_id": "power-bank-10k",
                                  "audience_need": "通勤途中补电", "use_scene": "地铁通勤",
                                  "supported_observation_ids": ["wireless"], "selling_point": "支持15W无线输出",
                                  "talking_focus": "先确认设备兼容，再讲通勤补电便利", "research_status": "provided"}]
    assert MaterialAssessment.model_validate(value).benefit_mappings[0].supported_observation_ids == ["wireless"]
    for change in [{"product_candidate_id": "power-bank-20k"}, {"supported_observation_ids": ["invented-fast-charge"]}]:
        wrong = deepcopy(value)
        wrong["benefit_mappings"][0].update(change)
        with pytest.raises(ValidationError):
            MaterialAssessment.model_validate(wrong)
    wrong = deepcopy(value)
    wrong["field_observations"][0]["claim_kind"] = "research_hypothesis"
    with pytest.raises(ValidationError, match="known product facts"):
        MaterialAssessment.model_validate(wrong)


def test_duplicate_or_foreign_ids_do_not_pass_structural_validation():
    value = assessment()
    value["field_observations"].append(deepcopy(value["field_observations"][0]))
    with pytest.raises(ValidationError, match="duplicate"):
        MaterialAssessment.model_validate(value)
    value = assessment()
    value["content_groups"][0]["product_candidate_ids"] = ["invented"]
    with pytest.raises(ValidationError, match="declared candidates"):
        MaterialAssessment.model_validate(value)


def test_known_fact_needs_provenance_and_optional_cannot_be_required():
    with pytest.raises(ValidationError, match="evidence_ids"):
        MaterialAssessment.model_validate({**assessment(), "field_observations": [observation(evidence_ids=[])]})
    with pytest.raises(ValidationError, match="skippable"):
        MaterialAssessment.model_validate({**assessment(), "missing_info": [missing(level="optional", can_skip=False)]})
    with pytest.raises(ValidationError, match="require a value"):
        MaterialAssessment.model_validate({**assessment(), "field_observations": [observation(value=None)]})


def test_readiness_purpose_aliases_cannot_silently_bypass_declared_blocker():
    value = assessment()
    value["missing_info"] = [missing()]
    for purpose in ["generate_script", "script_generation", "materials_to_script"]:
        result = assess_material_readiness(value, purpose=purpose, requested_actions=["include_price"])
        assert result["purpose"] == "script_generation"
        assert result["ready"] is False
        assert result["blocked_actions"] == ["include_price"]
    assert MaterialAssessment.model_validate(value).missing_info[0].applicable_purposes == ["script_generation"]


def test_unknown_readiness_purpose_and_declared_purpose_are_rejected():
    with pytest.raises(ValueError, match="unknown material purpose"):
        assess_material_readiness(assessment(), purpose="script_genration_typo")
    with pytest.raises(ValidationError, match="unknown material purpose"):
        MaterialAssessment.model_validate({**assessment(), "missing_info": [missing(applicable_purposes=["anything"])]})
    with pytest.raises(ValidationError, match="unknown material purpose"):
        MaterialAssessment.model_validate({**assessment(), "conflicts": [{"conflict_id": "unknown-purpose",
            "field_path": "price", "explanation": "需核实", "applicable_purposes": ["anything"]}]})
    with pytest.raises(ValidationError, match="must be a list"):
        MaterialAssessment.model_validate({**assessment(), "missing_info": [missing(applicable_purposes=None)]})


def test_progressive_skill_protocol_matches_runtime_schema_and_example_is_valid():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "skills/product_facts/prelive/references"
    assert json.loads((root / "material-assessment-schema.json").read_text(encoding="utf-8")) == MaterialAssessment.model_json_schema()
    value = MaterialAssessment.model_validate(json.loads((root / "material-assessment-example.json").read_text(encoding="utf-8")))
    result = assess_material_readiness(value, purpose="script_generation", requested_actions=["include_price"])
    assert result["ready"] is True  # the example explicitly has no active quotation condition
    assert value.field_observations[0].evidence_ids == ["e-material"]
