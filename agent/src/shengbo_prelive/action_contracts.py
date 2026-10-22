"""Canonical action matching and transport-independent recoverable errors.

Matching aliases only projects reports; it never adds granted capabilities and
never rewrites an accepted report/hash. Unknown prose is an exact identifier.
"""
ACTION_FAMILIES = (
    frozenset({"save_product", "save_from_facts", "save_accepted_product", "catalog_update",
               "wf_product_materials.save_from_facts"}),
    frozenset({"save_script_plan", "save_script_change_plan", "submit_script_change",
               "request_script_generation", "execute_script_plan", "generate_script",
               "generate_scripts", "script_generation", "script_change", "patch_script", "import_script"}),
    frozenset({"accept_analysis_report", "accept_analysis_reports", "accept_reports"}),
)


def action_aliases(action):
    return next((family for family in ACTION_FAMILIES if action in family), frozenset({action}))


def actions_match(declared, requested):
    return bool(action_aliases(declared) & action_aliases(requested))


def matching_actions(declared, requested):
    if requested is None:
        return list(declared)
    return [action for action in declared if any(actions_match(action, target) for target in requested)]


def recoverable_input_error(error, *, pending_unknown=False):
    """Only known input errors; never mask authority, integrity or sent/unknown."""
    if error.status in {401, 403} or pending_unknown or error.code not in CORRECTABLE_INPUT_ERRORS:
        return False
    if error.code in PREWRITE_CORRECTABLE_ERRORS:
        return error.details.get("commit_state") == "not_committed"
    return True


def uncommitted_input_error(code, message, *, status=409, details=None):
    """Call only at a proved pre-write validation branch, never after a send."""
    from shengbo_contracts.common import ContractError
    return ContractError(code, message, status=status, details={**(details or {}), "commit_state": "not_committed"})


def capability_manifest():
    """Mechanical reachability ledger; no automatic exposure or permission."""
    from .policy import EFFECTS
    from .tools import COMMAND_MAP
    from .workflows import WORKFLOW_EFFECTS
    internal = {"import_blob", "accept_analysis_report"}
    human = {"refresh_product_reference"}
    effects = set().union(*EFFECTS.values())
    return [{"command": command, "public_tools": sorted(name for name, key in COMMAND_MAP.items() if key == command),
             "workflows": sorted(name for name, keys in WORKFLOW_EFFECTS.items() if command in keys),
             "internal_only": command in internal, "human_only": command in human}
            for command in sorted(effects)]


REPORT_CORRECTABLE_ERRORS = frozenset({
    "MATERIAL_ASSESSMENT_INVALID", "MATERIAL_SECTION_NOT_FOUND", "MATERIAL_EVIDENCE_MISSING",
    "MATERIAL_VISUAL_EVIDENCE_MISMATCH", "MATERIAL_VISUAL_READ_NOT_OBSERVED", "MATERIAL_VISUAL_REUSE_BASIS_REQUIRED",
})

PREWRITE_CORRECTABLE_ERRORS = frozenset({
    "PATCH_SCOPE_COLLAPSE_REQUIRES_EXPLICIT_INTENT",
    "INTENT_RESEARCH_REUSE_INVALID", "REQUIRED_ANALYSIS_BINDING_MISSING", "ANALYSIS_BINDING_INVALID",
    "MATERIAL_VISUAL_PLAN_SCOPE_UNVERIFIED", "MATERIAL_CRITICAL_VISUAL_UNREAD", "TARGET_SCOPE_UNVERIFIED",
    "BINDING_REVIEW_REQUIRED", "BINDING_REPORT_REQUIRED", "BINDING_REQUIRES_REPLAN",
    "FACTS_SAVE_REF_INVALID", "FACTS_SAVE_OVERRIDE_INVALID", "FACTS_SAVE_FIELDS_INVALID",
    "FACTS_SAVE_CLEAR_DEFAULT_UNAVAILABLE", "FIELD_CANNOT_CLEAR", "DUPLICATE_PRODUCT_SELECTION",
    "WAIT_DEPENDENCIES_INVALID", "REPORT_MODE_INVALID", "IDENTITY_NAME_REQUIRED",
    "IDENTITY_CANDIDATES_EMPTY", "DECISION_INPUT_INVALID", "INTENT_CATALOG_SCOPE_INVALID", "INTENT_PRODUCT_FIELD_SCOPE_INVALID",
    "SELLING_POINTS_INPUT_INVALID", "SELLING_POINTS_PRODUCT_STALE", "SELLING_POINTS_PRODUCT_UNAVAILABLE",
    "SELLING_POINTS_EVIDENCE_INVALID", "SELLING_POINTS_RESEARCH_INVALID", "SELLING_POINTS_PROPOSAL_CLOSED",
    "SELLING_POINTS_GENERATION_CHOICE_REQUIRED", "SELLING_POINTS_CONFIRMATION_REQUIRED", "PRODUCT_CONSUMER_CONTENT_INVALID",
})

CORRECTABLE_INPUT_ERRORS = {
    "INVALID_EFFECT_ENUM",
    "INTENT_EVIDENCE_INVALID", "INTENT_MODULE_SCOPE_REQUIRED", "REVISION_CONFLICT", "SCHEMA_CHANGED", "EXACT_REF_REQUIRED", "NOT_FOUND",
    "WRONG_ARTIFACT_KIND", "GENERATION_EPOCH_CHANGED", "GENERATION_INPUTS_CHANGED", "CONTEXT_INPUTS_CHANGED",
    "CONTEXT_BUNDLE_CHANGED", "REPORT_NOT_READY", "REPORT_MISSING", "REPORT_DEPENDENCY_NOT_READY",
    "REPORT_REQUIRES_CURRENT_RUN_REVALIDATION",
    "HISTORY_SNAPSHOT_PATH_INVALID", "HISTORY_SNAPSHOT_SECTION_INVALID", "HISTORY_SNAPSHOT_SELECTION_INVALID",
    "HISTORY_SNAPSHOT_INPUT_INVALID", "HISTORY_SECTION_INVALID", "HISTORY_PAGE_INVALID",
    "HISTORY_BUSINESS_REF_REQUIRES_READER", "HISTORY_REF_KIND_MISMATCH", "CONTEXT_SECTION_INVALID",
    "ANALYSIS_REFERENCE_UNRESOLVED",
    "PLAYLIST_REQUIRED", "PLAYLIST_CHANGED", "PLAYLIST_DECISION_REQUIRED", "PLAYLIST_PROPOSAL_REQUIRED",
    "PLAYLIST_PROPOSAL_INPUT_INVALID", "PLAYLIST_PROPOSAL_STALE", "REORDER_PREVIEW_STALE", "TEXT_SELECTION_STALE",
    "PLAN_REQUIRED", "PREPARATION_REQUIRED", "PRODUCT_REQUIRED", "PRODUCT_ASSEMBLY_INVALID", "PREPARATION_PRODUCTS_NOT_SELECTED", "BASE_MODULE_REQUIRED", "BASE_SCRIPT_TARGET_MISMATCH", "BASE_MODULE_TARGET_MISMATCH",
    "EMPTY_SCRIPT_PLAN", "DUPLICATE_MODULE_ACTION", "INCOMPLETE_MODULE_PLAN", "REQUIRED_MODULE_DISABLED",
    "ASSEMBLY_PATCH_MISMATCH", "CANONICAL_FIELD_CONFLICT", "INVALID_FIELD_PATH", "INCOMPLETE_FIELD_DISPOSITIONS",
    "PRODUCT_IDENTITY_CONFIRMATION_REQUIRED", "VARIANT_ALREADY_EXISTS", "SCRIPT_FACTS_STALE",
    "GENERATOR_REVISION_CHANGED",
    "MATERIAL_VISUAL_PLAN_SCOPE_UNVERIFIED", "MATERIAL_CRITICAL_VISUAL_UNREAD", "TARGET_SCOPE_UNVERIFIED",
    "SCRIPT_MODE_CONFLICT", "SCRIPT_SOURCE_RANGE_INVALID", "SCRIPT_SOURCE_TEXT_MISMATCH",
    "ORIGINAL_SCRIPT_TARGET_MISMATCH", "ARCHIVED_RESPONSE_INCOMPLETE", "ARCHIVED_RESPONSE_NOT_AVAILABLE",
} | REPORT_CORRECTABLE_ERRORS | PREWRITE_CORRECTABLE_ERRORS
