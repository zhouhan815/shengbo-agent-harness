"""Merge model-selected session field updates against the current draft."""
from copy import deepcopy

from shengbo_contracts.common import ContractError, canonical_hash


def update_draft_fields(draft, updates):
    paths = [item["path"] for item in updates]
    if (not paths or len(set(paths)) != len(paths)
            or any(other.startswith(path + "/") for path in paths for other in paths)):
        return None
    updated = deepcopy(draft)
    for item in updates:
        *parents, leaf = item["path"].lstrip("/").split("/")
        node = updated
        for parent in parents:
            node = node.setdefault(parent, {})
            if not isinstance(node, dict):
                return None
        value = item.get("value")
        node[leaf] = value
        root = parents[0] if parents else leaf
        if not valid_root_value(root, updated[root]):
            return None
    return updated


def valid_root_value(field, value):
    if field == "scheduled_at":
        return isinstance(value, str) or isinstance(value, dict) and all(
            key in {"start", "end"} and isinstance(item, str) for key, item in value.items())
    if field in {"theme", "objective", "timezone", "language", "loop_mode"}:
        return isinstance(value, str)
    if field in {"campaign_starts_at", "campaign_ends_at"}:
        return value is None or isinstance(value, str)
    if field in {"commerce", "tts_selection", "requested_variant_counts"}:
        return isinstance(value, dict)
    if field in {"inheritance_decisions", "unknowns"}:
        return isinstance(value, list)
    return False


async def apply_session_settings(flow, updates, observed_preparation):
    engine, scope = flow.engine, flow.scope
    run = await engine.assert_scope(scope)
    if "save_session_draft" not in run.get("allowed_effects", []):
        raise ContractError("GOAL_EFFECT_DENIED", "本次没有修改本场设置的授权。", status=403)
    op_id = "op_" + canonical_hash([scope.run_id, flow.id, "save_session_draft"])[7:39]
    prior = engine.store.get("operation", op_id, required=False)
    if prior and prior["status"] == "committed":
        params = prior["args"]
    else:
        prep = await flow.prep()
        prep_ref = prep["ref"]
        if observed_preparation and observed_preparation != prep_ref:
            return flow.result("NEEDS_REPLAN", result={"preparation_ref": prep_ref},
                blockers=["本场草案已变化，请核对当前配置后再提交。"])
        draft_ref = prep["body"].get("draft_ref")
        draft = (await flow.query("read_context", {"ref": draft_ref, "sections": ["draft"]}))["body"]["draft"] if draft_ref else {}
        merged = update_draft_fields(draft, updates)
        if merged is None:
            return flow.result("NEEDS_REPLAN", blockers=["请分别提交需要更新的字段，使用正确的数据类型，避免重复或同时提交整组字段及其子字段。"])
        params = {"preparation_id": prep_ref["id"], "expected_revision": prep_ref["revision"], "draft": merged}
    standalone = run["goal"]["objective"] == "session_config"
    if standalone and not run.get("task_assessment"):
        engine.save_assessment(scope, {"summary": "更新明确指定的本场设置，保留其他配置",
            "capabilities": ["wf_context_bundle"], "steps": ["核对当前草案", "保存指定设置"]})
    try:
        receipt = await flow.step("save_session_draft", lambda: engine.business_command(scope, "save_session_draft", params, flow.id))
    except ContractError as error:
        if error.code != "REVISION_CONFLICT":
            raise
        latest = await flow.prep()
        return flow.result("NEEDS_REPLAN", result={"preparation_ref": latest["ref"]},
            blockers=["本场配置刚刚变化。请读取当前草案，核对变更后带上新的session_preparation_ref再提交。"])
    if standalone:
        completion = await engine.finish(scope, "COMPLETED", f"已更新本场设置，共提交 {len(updates)} 项。")
        if not completion.get("accepted"):
            return flow.result("NEEDS_REPLAN", result=receipt, blockers=completion.get("missing", []))
    return flow.result(result=receipt)
