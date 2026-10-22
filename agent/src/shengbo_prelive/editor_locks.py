"""Read-only projection of runtime write ownership for human editor controls.

This is a UI interlock, not a replacement for exact references, write grants or
business CAS. No model text or private context is exposed by this projection.
"""
from __future__ import annotations

import json

from .store import TERMINAL


def project_editor_locks(store, execution_status=None):
    owners = {item["run_id"] for item in (execution_status or {}).get("owners", [])}
    with store.tx() as db:
        runs = [json.loads(row[0]) for row in db.execute("SELECT body FROM objects WHERE kind='run'")]
        unknown = {json.loads(row[0]).get("run_id") for row in db.execute(
            "SELECT body FROM objects WHERE kind='operation' AND json_extract(body,'$.status') IN ('sent','unknown')")}
    locks = []
    for run in runs:
        status = run.get("status", "QUEUED")
        pending_write = run.get("pending_unknown_operation") or run["id"] in unknown
        unsettled = pending_write or run["id"] in owners
        if status in TERMINAL and not unsettled:
            continue
        goal = run.get("goal") or {}
        if goal.get("objective") == "read_explain" and not pending_write:
            continue
        scope = run.get("scope") or {}
        if scope.get("account_id", "local") != "local":
            continue
        prep = scope.get("preparation_id")
        selection = run.get("selection_binding") or {}
        raw = (run.get("input") or {}).get("selection") or {}
        target = raw.get("target") or {}
        ref = selection.get("current_ref") or target.get("object_ref") or {}
        selected_kind = selection.get("kind") or ref.get("kind")
        field = selection.get("field_path") or target.get("field_path")
        module = selection.get("module_key") or target.get("module_key")
        product_ids = list(dict.fromkeys([*(run.get("target_product_ids") or goal.get("target_product_ids") or []),
                                         *run.get("created_product_ids", [])]))
        selected_product = selection.get("product_id") or (ref.get("id") if selected_kind == "product" else None)
        if selected_product:
            product_ids = [selected_product]
        selected_kinds = {selected_kind} if selected_kind and selected_kind != "selection_set" else set()
        multi_items = selection.get("items") if selection.get("kind") == "selection_set" else raw.get("items")
        if isinstance(multi_items, list):
            for item in multi_items:
                item_ref = item.get("current_ref") or item.get("target", {}).get("object_ref") or {}
                item_kind = item.get("kind") or item_ref.get("kind")
                if item_kind:
                    selected_kinds.add(item_kind)
                if item_kind == "product" and item_ref.get("id") not in product_ids:
                    product_ids.append(item_ref["id"])
        effects = run.get("allowed_effects", goal.get("allowed_effects"))
        targets = []

        def add(kind, identity=None, **details):
            if identity:
                item = {"kind": kind, "id": identity, **details}
                if item not in targets:
                    targets.append(item)

        # A submitted explicit selection locks only that field/module even while
        # the main Agent is assessing intent. Navigation is never a lock source.
        if selected_kind == "product":
            if effects is None or "save_product" in effects or unsettled:
                add("product", ref.get("id"), field_paths=[field] if field else run.get("allowed_product_paths") or None)
        elif selected_kind == "preparation":
            if effects is None or "save_session_draft" in effects or unsettled:
                add("session", prep or ref.get("id"), field_paths=[field] if field else None)
        elif selected_kind == "script_revision":
            if effects is None or set(effects) & {"patch_script", "submit_script_change"} or unsettled:
                add("scripts", prep, script_id=ref.get("id"), product_ids=product_ids or None,
                    module_keys=[module] if module else run.get("module_allowlist") or None)
                original = selection.get("base_ref") or target.get("object_ref") or {}
                if original.get("id") and original.get("id") != ref.get("id"):
                    add("scripts", prep, script_id=original.get("id"), product_ids=product_ids or None,
                        module_keys=[module] if module else run.get("module_allowlist") or None)
        elif selected_kind == "analysis":
            add("insights", prep or "catalog", analysis_id=ref.get("id"), product_ids=product_ids or None)
        if isinstance(multi_items, list):
            for item in multi_items:
                item_target = item.get("target") or {}
                item_ref = item.get("current_ref") or item_target.get("object_ref") or {}
                item_kind = item.get("kind") or item_ref.get("kind")
                item_field = item.get("field_path") or item_target.get("field_path")
                item_module = item.get("module_key") or item_target.get("module_key")
                if item_kind == "product" and (effects is None or "save_product" in effects or unsettled):
                    add("product", item_ref.get("id"), field_paths=[item_field] if item_field else None)
                elif item_kind == "preparation" and (effects is None or "save_session_draft" in effects or unsettled):
                    add("session", prep or item_ref.get("id"), field_paths=[item_field] if item_field else None)
                elif item_kind == "script_revision" and (effects is None or set(effects) & {"patch_script", "submit_script_change"} or unsettled):
                    add("scripts", prep, script_id=item_ref.get("id"), product_ids=[item["product_id"]] if item.get("product_id") else None,
                        module_keys=[item_module] if item_module else None)
                    original = item.get("base_ref") or item_target.get("object_ref") or {}
                    if original.get("id") != item_ref.get("id"):
                        add("scripts", prep, script_id=original.get("id"), module_keys=[item_module] if item_module else None)
                elif item_kind == "analysis":
                    add("insights", prep or "catalog", analysis_id=item_ref.get("id"))

        if effects is None:
            if not selected_kinds:
                if prep:
                    for kind in ("session", "scripts", "playlist", "insights", "assets", "product_selection"):
                        add(kind, prep)
                elif product_ids:
                    for identity in product_ids:
                        add("product", identity, field_paths=None)
                else:
                    add("product_create", "catalog")
        else:
            effects = set(effects)
            if "save_product" in effects and "product" not in selected_kinds:
                for identity in product_ids:
                    add("product", identity, field_paths=run.get("allowed_product_paths") or None)
                if not product_ids:
                    add("product_create", "catalog")
            if "save_session_draft" in effects and "preparation" not in selected_kinds:
                add("session", prep, field_paths=None)
            if effects & {"patch_script", "submit_script_change", "save_script_plan"} and "script_revision" not in selected_kinds:
                add("scripts", prep, product_ids=product_ids or None, module_keys=run.get("module_allowlist") or None)
            if effects & {"save_playlist", "compile_playback_plan", "prepare_required_audio", "publish_preparation"}:
                add("playlist", prep)
            if effects & {"update_preparation_products", "refresh_product_reference"}:
                add("product_selection", prep)
            if "accept_analysis_report" in effects:
                add("insights", prep or "catalog", product_ids=product_ids or None)
            if "save_asset_checklist" in effects:
                add("assets", prep)
        if targets:
            locks.append({"run_id": run["id"], "status": status, "revision": run.get("revision"),
                          "reason": "正在核实写入结果或等待执行退出" if unsettled else "Agent 正在处理此区域",
                          "targets": targets})
    return {"schema_version": "PRELIVE-2.3", "locks": locks}
