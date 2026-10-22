"""Known no-commit selection recovery, through the real native Agent harness.

All business state is isolated and the model is scripted. No provider, TTS,
network research or simulated user approval is performed by this suite.
"""
from copy import deepcopy
import asyncio
import json

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from shengbo_contracts.common import ContractError, MutationContext
from shengbo_prelive.middleware import ActionBoundary
from shengbo_prelive.models import RunScope
from shengbo_prelive.tools import COMMAND_MAP, build_tools
from shengbo_prelive.workflows import AnalysisFlowInput, run_workflow
from test_middleware import FakeEngine, tool_request
from test_runtime_business import env, human_command, new_run, product, Product
from test_optional_workflows import authorize


def dependency_details():
    return {"commit_state": "not_committed", "preparation_ref": {"id": "prep1", "revision": 2},
        "current_selected_product_refs": [], "missing_product_ids": ["product1"],
        "refresh_query": {"query": "read_preparation", "args": {"preparation_id": "prep1"}},
        "required_action": {"command": "update_preparation_products", "tool": "wf_product_materials",
                            "action": "select", "requires_explicit_selection": True}}


@pytest.mark.asyncio
async def test_selection_dependency_is_bounded_known_correction_not_automatic_action():
    engine = FakeEngine()
    boundary = ActionBoundary(engine)
    async def reject(request):
        raise ContractError("PREPARATION_PRODUCTS_NOT_SELECTED", "missing selection", status=409,
                            details=dependency_details())
    for count in range(1, 4):
        message = await boundary.awrap_tool_call(tool_request("save_session_draft"), reject)
        payload = json.loads(message.content)
        assert message.status == "error"
        assert payload["correction_count"] == count
        assert payload["recovery"]["automatic_action_taken"] is False
        assert payload["recovery"]["next_actions"][1]["action"] == "select"
        assert "最新" in payload["corrective_hint"]
    with pytest.raises(ContractError, match="PREPARATION_PRODUCTS_NOT_SELECTED"):
        await boundary.awrap_tool_call(tool_request("save_session_draft"), reject)
    # FakeEngine has no business command method: no recovery action was hidden
    # inside the middleware's model-readable correction.


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status,details,pending,tool,role", [
    ("VOICE_REJECTED", 409, dependency_details(), False, "save_session_draft", "main"),
    ("REMOTE_OUTCOME_UNKNOWN", 503, dependency_details(), False, "save_session_draft", "main"),
    ("PREPARATION_PRODUCTS_NOT_SELECTED", 503, dependency_details(), False, "save_session_draft", "main"),
    ("PREPARATION_PRODUCTS_NOT_SELECTED", 403, dependency_details(), False, "save_session_draft", "main"),
    ("PREPARATION_PRODUCTS_NOT_SELECTED", 401, dependency_details(), False, "save_session_draft", "main"),
    ("PREPARATION_PRODUCTS_NOT_SELECTED", 409, {}, False, "save_session_draft", "main"),
    ("PREPARATION_PRODUCTS_NOT_SELECTED", 409, {"commit_state": "unknown"}, False, "save_session_draft", "main"),
    ("PREPARATION_PRODUCTS_NOT_SELECTED", 409, dependency_details(), True, "save_session_draft", "main"),
    ("PREPARATION_PRODUCTS_NOT_SELECTED", 409, dependency_details(), False, "save_product", "main"),
    ("PREPARATION_PRODUCTS_NOT_SELECTED", 409, dependency_details(), False, "save_session_draft", "product_facts"),
    ("REVISION_CONFLICT", 409, {}, True, "save_session_draft", "main"),
])
async def test_dependency_label_never_downgrades_permission_unknown_or_wrong_contract(code, status, details, pending, tool, role):
    engine = FakeEngine()
    if pending:
        engine.store.update("run", "r1", pending_unknown_operation="unknown-op")
    async def reject(request):
        raise ContractError(code, "hard boundary", status=status, details=details)
    with pytest.raises(ContractError, match=code):
        await ActionBoundary(engine).awrap_tool_call(tool_request(tool, role=role), reject)
    assert not engine.store.get("run", "r1").get("correctable_tool_errors")


@pytest.mark.asyncio
async def test_missing_context_dependencies_return_exact_optional_recovery_without_writes(env):
    engine, service = env
    room = (await human_command(service, "create_room", {"name": "缺项入口测试"}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    run = await new_run(engine, "准备本场上下文", room_id=room["id"], preparation_id=prep["id"])
    await authorize(engine, run, ["save_context_bundle", "update_preparation_products"])
    scope = RunScope(run["id"], "context-selection-test", "execute")
    result = await run_workflow(engine, scope, "wf_context_bundle", {"action": "inspect"})
    assert result["status"] == "NEEDS_REPLAN"
    assert {entry["field"] for entry in result["blockers"]} == {"selected_product_refs", "draft_ref"}
    state = result["result"]["preparation_selection"]
    assert state["preparation_ref"] == prep
    assert state["automatic_action_taken"] is False
    assert state["next_actions"][0]["action"] == "select"
    assert state["next_actions"][0]["preparation_ref"] == prep
    assert state["next_actions"][1]["args"] == {"preparation_id": prep["id"], "expected_revision": prep["revision"]}
    assert not engine.store.list("operation")
    after = await service.query("read_preparation", {"preparation_id": prep["id"]})
    assert after["ref"] == prep and after["body"]["selected_product_refs"] == []
    materials = await run_workflow(engine, scope, "wf_product_materials", {"action": "inspect"})
    assert materials["result"]["preparation_selection"]["preparation_ref"] == prep
    assert materials["result"]["preparation_selection"]["next_actions"][0]["action"] == "select"
    assert not engine.store.list("operation"), "An inspect hint must not select goods"


@pytest.mark.asyncio
async def test_optional_selection_recovery_preserves_goal_pinned_refs_and_real_cas(env):
    engine, service = env
    pinned = await product(service, "原已选商品")
    foreign = await product(service, "当前Goal外商品")
    room = (await human_command(service, "create_room", {"name": "选品权限保持测试"}))["data"]["room_ref"]
    created = (await human_command(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    selected = (await human_command(service, "update_preparation_products", {"preparation_id": created["id"],
        "expected_revision": created["revision"], "selected_product_refs": [pinned]}))["data"]["preparation_ref"]
    run = await new_run(engine, "只核对原选商品", room_id=room["id"], preparation_id=created["id"])
    await authorize(engine, run, ["update_preparation_products"])
    scope = RunScope(run["id"], "selection-target-fence", "execute")
    with pytest.raises(ContractError, match="PRODUCT_SELECTION_REMOVAL_DENIED"):
        await run_workflow(engine, scope, "wf_product_materials", {"action": "select",
            "parameters": {"selected_product_refs": [foreign]}})
    with pytest.raises(ContractError, match="TARGET_DENIED"):
        await run_workflow(engine, scope, "wf_product_materials", {"action": "select",
            "parameters": {"selected_product_refs": [pinned, foreign]}})
    # Known CAS failures now retain the same bounded M5 correction contract
    # for native and optional workflow tools, rather than a second error path.
    with pytest.raises(ContractError, match="REVISION_CONFLICT") as stale:
        await run_workflow(engine, scope, "wf_product_materials", {"action": "select",
            "parameters": {"selected_product_refs": [pinned], "expected_revision": created["revision"]}})
    from shengbo_prelive.action_contracts import recoverable_input_error
    assert recoverable_input_error(stale.value)
    after = await service.query("read_preparation", {"preparation_id": created["id"]})
    assert after["ref"] == selected and after["body"]["selected_product_refs"] == [pinned]
    assert not [row for row in engine.store.list("operation") if row["status"] == "committed"]


class ScriptedModel(BaseChatModel):
    _logic = PrivateAttr()
    def __init__(self, logic):
        super().__init__()
        self._logic = logic
    @property
    def _llm_type(self):
        return "selection-recovery-isolated-native-agent"
    def bind_tools(self, tools, **kwargs):
        return self
    def _generate(self, *args, **kwargs):
        raise AssertionError("async only")
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=await self._logic(messages))])


@pytest.mark.asyncio
async def test_native_runtime_model_explicitly_selects_after_rejected_draft_then_saves(env):
    engine, service = env
    # Use the real upload/source bridge and real new-product write.
    # No preexisting target is selected by controller initialization here.
    await engine.start()
    engine.scheduler.cancel()
    await asyncio.gather(engine.scheduler, return_exceptions=True)
    room = (await human_command(service, "create_room", {"name": "新商品显式选入回归"}))["data"]["room_ref"]
    prep_ref = (await human_command(service, "create_preparation", {
        "room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    run = await new_run(engine, "依据上传资料新建商品并准备本场，不生成语音",
        room_id=room["id"], preparation_id=prep_ref["id"])
    sources = [engine.materials.accept(run["conversation_id"], "商品资料.md", "text/markdown",
        "# 商家资料\n新建润色口红回归商品，129元，日常唇妆。".encode())["material_ref"]]
    effects = ["import_blob", "accept_analysis_report", "save_product", "update_preparation_products", "save_session_draft"]
    grant = await engine.client.command("issue_grant", {"run_id": run["id"], "scope": run["scope"],
        "capabilities": effects, "target_constraints": []}, MutationContext(operation_id=run["id"] + "-recovery-grant"),
        run_id=run["id"], controller=True)
    run = engine.store.update("run", run["id"], phase="execute", status="RUNNING", allowed_effects=effects,
        allowed_tools=[tool for tool, key in COMMAND_MAP.items() if key in effects],
        input={**run["input"], "material_refs": sources}, target_product_ids=[], created_product_ids=[], receipts=[],
        task_assessment={"summary": "隔离验证模型显式纠错，不固定整场流程", "capabilities": []},
        goal={"objective": "prepare_session", "scope": run["scope"], "target_product_ids": [], "required_analyses": []},
        grant_id=grant["data"]["grant_id"], write_generation=grant["data"]["write_generation"])
    # Seed an actually accepted facts report as the source mapping required by
    # the normal save-product boundary, rather than bypassing that boundary.
    branch_id = "selection-recovery-facts"
    engine.store.put("branch", {"id": branch_id, "run_id": run["id"], "role": "product_facts", "status": "RUNNING",
        "task": {"role": "product_facts", "instruction": "核验上传商品原料", "product_ids": [], "source_refs": sources, "input_refs": []}})
    engine.submit_report(RunScope(run["id"], "fixture-worker", "execute", "product_facts", branch_id),
        {"summary": "隔离测试已核验上传商品事实", "source_refs": sources})
    await engine.accept_reports(RunScope(run["id"], "fixture-main", "execute"), [branch_id], "fixture-accept-facts")
    original_goal = deepcopy(run["goal"])
    original_effects = deepcopy(run["allowed_effects"])
    prep_id = run["scope"]["preparation_id"]
    fields = Product(name="新建润色口红回归商品", price=129, selling_points="日常唇妆").model_dump()
    product_args = {"schema_hash": service.schema()["schema_hash"], "patches": [], "fields": fields,
        "source_manifest_refs": sources,
        "field_dispositions": [{"path": "/" + key, "action": "set",
            "knowledge": "known" if value not in (None, "", []) else "unknown", "source_refs": sources,
            "reason": "隔离测试商家资料已核验"} for key, value in fields.items()]}
    state = {"stage": 0}
    calls = []
    def call(name, args):
        calls.append((name, deepcopy(args)))
        return AIMessage(content="", tool_calls=[{"id": "selection-recovery-" + str(len(calls)), "name": name, "args": args}])

    async def logic(messages):
        stage = state["stage"]
        state["stage"] += 1
        if stage == 0:
            return call("wf_product_materials", {"action": "save", "parameters": product_args})
        last = messages[-1]
        value = json.loads(last.content)
        if stage == 1:
            assert value["status"] == "COMPLETED", value.get("blockers")
            receipt = value["result"]
            state["product_ref"] = receipt["data"]["product_ref"]
            state["before"] = (await service.query("read_preparation", {"preparation_id": prep_id}))["ref"]
            assert receipt["preparation_selection"]["current_selected_product_refs"] == []
            assert receipt["preparation_selection"]["unselected_candidate_refs"] == [state["product_ref"]]
            assert receipt["preparation_selection"]["next_actions"][0]["action"] == "select"
            state["draft"] = {"theme": "情人节活动", "commerce": {
                "promotion_product_ids": [state["product_ref"]["id"]]}}
            return call("save_session_draft", {"preparation_id": prep_id,
                "expected_revision": state["before"]["revision"], "draft": state["draft"]})
        if stage == 2:
            assert last.status == "error"
            assert value["error"]["code"] == "PREPARATION_PRODUCTS_NOT_SELECTED"
            assert value["error"]["details"]["commit_state"] == "not_committed"
            assert value["recovery"]["automatic_action_taken"] is False
            before = await service.query("read_preparation", {"preparation_id": prep_id})
            assert before["ref"] == state["before"]
            assert before["body"]["selected_product_refs"] == [] and before["body"]["draft_ref"] is None
            # This call is emitted by the model after observing the error,
            # never inserted by the middleware/flow/controller.
            return call("wf_product_materials", {"action": "select",
                "parameters": {"selected_product_refs": [state["product_ref"]]}})
        if stage == 3:
            selected = value["result"]["preparation_selection"]
            assert selected["current_selected_product_refs"] == [state["product_ref"]]
            assert selected["preparation_ref"]["revision"] > state["before"]["revision"]
            assert not any(action.get("action") == "select" for action in selected["next_actions"])
            return call("save_session_draft", {"preparation_id": prep_id,
                "expected_revision": selected["preparation_ref"]["revision"], "draft": state["draft"]})
        assert stage == 4
        assert value["data"]["draft_ref"]["kind"] == "draft"
        return call("finish_turn", {"outcome": "BLOCKED", "summary":
            "隔离回归主动停止于配置已保存检查点，未执行话术、人工批准、语音或候选；不宣称整场完成。"})

    engine.model = ScriptedModel(logic)
    engine.main = engine._build_graph("main")
    result, _, _ = await engine._invoke(run)
    assert state["stage"] == 5
    assert [name for name, _ in calls] == ["wf_product_materials", "save_session_draft", "wf_product_materials", "save_session_draft", "finish_turn"]
    prep = await service.query("read_preparation", {"preparation_id": prep_id})
    draft = await service.query("read_context", {"ref": prep["body"]["draft_ref"], "sections": ["draft"]})
    assert prep["body"]["selected_product_refs"] == [state["product_ref"]]
    assert draft["body"]["draft"]["commerce"]["promotion_product_ids"] == [state["product_ref"]["id"]]
    current = engine.store.get("run", run["id"])
    assert current["goal"] == original_goal and current["allowed_effects"] == original_effects
    assert not current.get("pending_unknown_operation")
    operations = [row for row in engine.store.list("operation") if row["run_id"] == run["id"]]
    rejected = [row for row in operations if row.get("error_code") == "PREPARATION_PRODUCTS_NOT_SELECTED"]
    assert len(rejected) == 1 and rejected[0]["status"] == "known_failed"
    assert len([row for row in operations if row["key"] == "save_session_draft" and row["status"] == "committed"]) == 1
    assert len([row for row in operations if row["key"] == "update_preparation_products" and row["status"] == "committed"]) == 1
    assert not any(row["status"] in {"unknown", "sent"} for row in operations)
    assert not engine.store.list("question"), "No fake approval is needed or generated"
    assert current["correctable_tool_errors"] == {"PREPARATION_PRODUCTS_NOT_SELECTED": 1}


def test_tool_contracts_expose_explicit_selection_and_exact_bind_review_protocol():
    tools = {tool.name: tool for tool in build_tools(FakeEngine())}
    assert "不会自动选入" in tools["save_product"].description
    assert "完整精确清单" in tools["update_preparation_products"].description
    assert "PREPARATION_PRODUCTS_NOT_SELECTED" in tools["save_session_draft"].description
    description = AnalysisFlowInput.model_fields["parameters"].description
    assert all(key in description for key in ("conflict_reviews", "branch_task_id", "report_hash", "issue_index", "nonblocking_boundary_note"))
    assert "session_config不绑定" in tools["wf_analysis_coordination"].description
