"""Model-selected tool disclosure; registration and authorization stay separate."""
from __future__ import annotations

import json

from shengbo_contracts.common import canonical_hash, now_iso

VERSION = "tool-presentation-v1"
CONTROL_TOOLS = {"tool_catalog", "activate_tools"}
CORE_TOOLS = {
    "ls", "read_file", "glob", "grep", "read_capabilities", "read_source", "read_run_history",
    "save_task_assessment", "dispatch_analysis_batch", "read_analysis_batch", "accept_analysis_reports",
    "request_user_decision", "finish_turn", "read_business_schema", "read_preparation", "read_text_review",
    "read_audio_readiness", "save_session_draft", "save_script_change_plan", "read_product", "read_context",
    "read_script", "read_job", "import_script", "patch_script", "wf_product_materials", "wf_analysis_coordination", "wf_context_bundle",
    "wf_script_execution", "wf_playback_review", "wf_required_audio", "wf_finalize_preparation", "wf_selling_points",
} | CONTROL_TOOLS
GROUPS = {
    "product": ("商品检索查找、资料导入校验保存、完整字段和顾客研究", {"search_catalog", "read_product", "validate_product_assembly", "save_product", "find_audience_research"}),
    "session": ("本场、直播间、主题活动优惠、声音音色和选品", {"read_preparation", "read_room", "read_voice_options", "read_session_history", "save_session_draft", "update_preparation_products"}),
    "context": ("完整上下文整理、schema和复盘导入关联", {"read_business_schema", "read_context", "build_context_bundle", "read_recap", "import_recap", "link_recap_to_preparation"}),
    "script": ("话术读取、原稿导入扩版、生成结果原文、版本使用、影响分析和局部改稿生成", {"read_script", "read_generation_output", "read_script_usage", "inspect_change_impact", "save_script_change_plan", "execute_script_plan", "import_script", "patch_script"}),
    "playback": ("顺序排序重排、开场编排、素材和文字核对", {"preview_playlist_reorder", "read_text_review", "read_asset_checklist", "save_playlist", "compile_playback_plan", "save_asset_checklist"}),
    "audio": ("任务状态、局部连续试听、已批准语音合成和未采用候选发布", {"read_job", "read_audio_readiness", "read_script_audio_preview", "list_script_audio_previews", "preview_script_audio", "prepare_required_audio", "publish_preparation", "retry_failed_items"}),
}

_RECEIPT_PHASES = {
    "save_product": "setup", "save_session_draft": "setup", "update_preparation_products": "setup",
    "refresh_product_reference": "setup", "import_recap": "setup", "link_recap_to_preparation": "setup",
    "save_context_bundle": "script", "save_script_plan": "script", "submit_script_change": "script",
    "import_script": "script", "patch_script": "script", "save_playlist": "delivery",
    "compile_playback_plan": "delivery", "prepare_required_audio": "delivery", "publish_preparation": "delivery",
}
_PHASE_GROUPS = {
    "script": ("context", "script", "playback"),
    "script_only": ("context", "script"),
    "delivery": ("script", "playback", "audio"),
}
_SCRIPT_ONLY_OBJECTIVES = {"materials_to_script", "script_change"}


def _disclosure(row, scope):
    if not _matches(row, scope):
        return set(), set()
    # Old records cannot distinguish automatic pins from explicit expansion.
    # Keep their entire previously visible set available on resume.
    manual = row.get("manual_names", row.get("expanded_names", []))
    pinned = row.get("pinned_names", [])
    return ({name for name in manual if isinstance(name, str)} if isinstance(manual, list) else set(),
            {name for name in pinned if isinstance(name, str)} if isinstance(pinned, list) else set())


def _receipt_phase(engine, scope):
    run = engine.store.get("run", scope.run_id)
    objective = (run.get("goal") or {}).get("objective")
    if (objective not in _SCRIPT_ONLY_OBJECTIVES | {"prepare_session"}
            or run.get("status") != "RUNNING" or run.get("pending_unknown_operation")):
        return None
    phase = next((_RECEIPT_PHASES[entry["command"]] for entry in reversed(run.get("receipts", []))
                  if entry.get("command") in _RECEIPT_PHASES), None)
    if phase not in _PHASE_GROUPS:
        return None
    # Open decisions and unaccepted analyses can require earlier tools even
    # after a later business receipt. Scope the check to this run.
    with engine.store.tx() as db:
        unsettled = db.execute("""
            SELECT 1 FROM objects
            WHERE kind='question' AND json_extract(body,'$.run_id')=? AND json_extract(body,'$.status')='OPEN'
            UNION ALL
            SELECT 1 FROM objects
            WHERE kind='branch' AND json_extract(body,'$.run_id')=?
                AND json_extract(body,'$.status') IN ('QUEUED','RUNNING','RECOVERING','PREPARED','FAILED','BLOCKED','CANCELLED')
            LIMIT 1
        """, (scope.run_id, scope.run_id)).fetchone()
    if unsettled:
        return None
    return "script_only" if objective in _SCRIPT_ONLY_OBJECTIVES else phase


def _identity(scope):
    return "presentation_" + canonical_hash([scope.run_id, scope.branch_task_id, scope.role, scope.phase, VERSION])[7:]


def _matches(row, scope):
    return (row.get("version") == VERSION and row.get("run_id") == scope.run_id
        and row.get("branch_task_id") == scope.branch_task_id and row.get("role") == scope.role
        and row.get("phase") == scope.phase)


def expanded_names(engine, scope):
    manual, pinned = _disclosure(engine.store.get("tool_presentation", _identity(scope), required=False) or {}, scope)
    return manual | pinned


def _visible_names_and_phase(engine, scope, allowed):
    if not engine.tool_presentation_enabled or scope.role != "main" or scope.phase != "execute":
        return set(allowed), None
    manual, pinned = _disclosure(engine.store.get("tool_presentation", _identity(scope), required=False) or {}, scope)
    phase = _receipt_phase(engine, scope) if pinned else None
    extra = set()
    if phase:
        groups = _PHASE_GROUPS[phase]
        pinned &= set().union(*(GROUPS[group][1] for group in groups))
        # Script and voice jobs share retry_failed_items. It must be visible
        # before audio, alongside the exact-generation output reader.
        extra = {"retry_failed_items", "read_generation_output"}
        if phase == "delivery":
            extra.update({"compile_playback_plan", "prepare_required_audio", "publish_preparation"})
    return set(allowed) & (CORE_TOOLS | manual | pinned | extra), phase


def visible_names(engine, scope, allowed):
    """The same function must drive native budget preview and final binding."""
    return _visible_names_and_phase(engine, scope, allowed)[0]


def presentation_view(engine, scope, allowed):
    visible, phase = _visible_names_and_phase(engine, scope, allowed)
    deferred = set(allowed) - visible
    return {"version": VERSION, "visible_count": len(visible), "authorized_count": len(allowed),
        **({"workflow_phase": phase} if phase else {}),
        "deferred_groups": {group: sorted(names & deferred) for group, (_, names) in GROUPS.items() if names & deferred},
        "other_deferred": sorted(deferred - set().union(*(names for _, names in GROUPS.values()))),
        "note": "未展开仅指当前未呈现schema，不是能力或阶段禁令。随时用activate_tools的names/groups或all=true自主展开；原工具执行仍受原Goal、权限及人工确认约束。"}


def catalog(engine, scope, *, query="", group=None, cursor=0, limit=30):
    allowed = engine.allowed_tool_names(scope)
    visible = visible_names(engine, scope, allowed)
    rows = []
    for name in sorted(allowed):
        category, purpose = next(((key, text) for key, (text, names) in GROUPS.items() if name in names),
            ("coordination", "统筹、工具流或受控资料读取"))
        if group and group != category:
            continue
        if query and query.casefold() not in (name + category + purpose).casefold():
            continue
        rows.append({"name": name, "group": category, "purpose": purpose, "expanded": name in visible})
    selected = rows[cursor:cursor+limit]
    return {"version": VERSION, "items": selected, "total": len(rows),
        "next_cursor": cursor+len(selected) if cursor+len(selected) < len(rows) else None,
        "permission_changed": False, "business_operation_started": False}


def activate(engine, scope, *, names=None, groups=None, all=False, pinned=False):
    """Only persist disclosure choices; never call a business tool/provider."""
    allowed = engine.allowed_tool_names(scope)
    requested = set(names or [])
    unknown_groups = sorted(set(groups or []) - set(GROUPS))
    for group in groups or []:
        if group in GROUPS:
            requested.update(GROUPS[group][1])
    if all:
        requested.update(allowed)
    accepted, unavailable = requested & allowed, requested - allowed
    identity = _identity(scope)
    with engine.store.tx() as db:
        old = db.execute("SELECT body FROM objects WHERE kind=? AND id=?", ("tool_presentation", identity)).fetchone()
        prior = json.loads(old[0]) if old else {}
        manual, automatic = _disclosure(prior, scope)
        (automatic if pinned else manual).update(accepted)
        record = {"id": identity, "version": VERSION, "run_id": scope.run_id,
            "branch_task_id": scope.branch_task_id, "role": scope.role, "phase": scope.phase,
            "expanded_names": sorted(manual | automatic), "manual_names": sorted(manual),
            "pinned_names": sorted(automatic), "updated_at": now_iso()}
        engine.store._put(db, "tool_presentation", record)
    result = {"version": VERSION, "expanded": sorted(accepted), "unavailable": sorted(unavailable),
        "unknown_groups": unknown_groups, "permission_changed": False, "business_operation_started": False,
        "note": "下一次模型请求提供所选工具的原完整schema；执行器一直保留全部注册工具，未增加激活前置权限。"}
    engine._emit(scope.run_id, "tools.presentation.updated", result, scope)
    return result
