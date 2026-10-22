"""Pre-write helper input correction through native M5; formal proofs stay hard."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import patch

from langchain_core.messages import AIMessage
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.action_contracts import recoverable_input_error, uncommitted_input_error
from shengbo_prelive.middleware import ActionBoundary
from shengbo_prelive.product_materials import SaveAcceptedProductInput, _ref, save_accepted_product
from test_middleware import FakeEngine, tool_request
from test_product_materials_exact_candidate import case as accepted_candidate, stats
from test_runtime_business import env
from test_selection_dependency_recovery_v1 import ScriptedModel


CODES = ("FACTS_SAVE_REF_INVALID", "FACTS_SAVE_OVERRIDE_INVALID", "FACTS_SAVE_FIELDS_INVALID",
         "FACTS_SAVE_CLEAR_DEFAULT_UNAVAILABLE", "FIELD_CANNOT_CLEAR")


@pytest.mark.asyncio
@pytest.mark.parametrize("code", CODES)
async def test_native_m5_corrects_only_explicit_uncommitted_helper_input(env, code):
    engine, service = env
    fields = {"name": "M5输入纠错商品", "price": 129, "material": "EVA"}
    if code == "FACTS_SAVE_FIELDS_INVALID":
        fields["unrecognized_candidate_field"] = "不能由helper静默丢弃的备注"
    if code == "FACTS_SAVE_CLEAR_DEFAULT_UNAVAILABLE":
        fields["price"] = [129]
    run, scope, _, branch, reference = await accepted_candidate(engine, service, fields)
    branch_before = deepcopy(engine.store.get("branch", branch))
    goal_before = deepcopy(run["goal"])
    effects_before = deepcopy(run["allowed_effects"])
    before = stats(service)
    helper = {"branch_task_id": branch, "accepted_report_ref": reference}
    bad = deepcopy(helper)
    good = deepcopy(helper)
    if code == "FACTS_SAVE_REF_INVALID":
        bad["accepted_report_ref"] = {**reference, "schema_version": "WRONG"}
    elif code == "FACTS_SAVE_OVERRIDE_INVALID":
        bad["overrides"] = [{"path": "/faq", "op": "set", "value": "一"},
                            {"path": "/faq", "op": "set", "value": "二"}]
        good["overrides"] = [{"path": "/faq", "op": "set", "value": "主Agent明确核查的新问答。"}]
    elif code == "FIELD_CANNOT_CLEAR":
        bad["overrides"] = [{"path": "/price", "op": "clear"}]
        good["overrides"] = [{"path": "/price", "op": "set", "value": 129}]
    elif code == "FACTS_SAVE_CLEAR_DEFAULT_UNAVAILABLE":
        bad["overrides"] = [{"path": "/material", "op": "clear"}]
        good["overrides"] = [{"path": "/material", "op": "clear"}, {"path": "/price", "op": "set", "value": 129}]
    await engine.start()
    engine.scheduler.cancel()
    await asyncio.gather(engine.scheduler, return_exceptions=True)
    state = {"stage": 0}

    def call(name, args):
        return AIMessage(content="", tool_calls=[{"id": f"safe-helper-{state['stage']}", "name": name, "args": args}])

    async def logic(messages):
        stage = state["stage"]
        state["stage"] += 1
        if stage == 0:
            return call("wf_product_materials", {"action": "save_from_facts", "parameters": bad})
        result = json.loads(messages[-1].content)
        if stage == 1:
            assert messages[-1].status == "error", result
            assert result["error"]["code"] == code
            assert result["error"]["details"]["commit_state"] == "not_committed"
            assert result["correction_count"] == 1
            assert stats(service) == before, "Helper parameter rejection must not save or generate anything"
            assert engine.store.get("branch", branch) == branch_before
            assert not engine.store.list("product_candidate_save_resolution")
            assert not [op for op in engine.store.list("operation") if op["key"] == "save_product"]
            assert not engine.store.get("run", run["id"]).get("pending_unknown_operation")
            if code == "FACTS_SAVE_FIELDS_INVALID":
                # An explicit main-Agent choice of the original save command,
                # not the helper silently removing an unrecognized field.
                formal = (await service.query("read_analysis_report", {"ref": reference}))["body"]
                return call("wf_product_materials", {"action": "save", "parameters": {
                    "schema_hash": service.schema()["schema_hash"],
                    "fields": {"name": fields["name"], "price": 129, "material": "EVA"}, "patches": [],
                    "source_manifest_refs": formal["report"]["source_refs"]}})
            return call("wf_product_materials", {"action": "save_from_facts", "parameters": good})
        assert stage == 2
        assert messages[-1].status != "error", result
        data = result["result"]["data"]
        state["saved_ref"] = data["product_ref"]
        return call("finish_turn", {"outcome": "BLOCKED", "summary": "离线输入纠错保存检查点；未批准语音、未生成话术或音频。"})

    engine.model = ScriptedModel(logic)
    engine.main = engine._build_graph("main")
    await engine._invoke(run)
    current = engine.store.get("run", run["id"])
    assert state["stage"] == 3, {"state": state, "error": current.get("error"), "last_error": current.get("last_error")}
    assert current["correctable_tool_errors"] == {code: 1}
    assert current["goal"] == goal_before and current["allowed_effects"] == effects_before
    assert engine.store.get("branch", branch) == branch_before
    saved = (await service.query("read_product", {"ref": state["saved_ref"]}))["body"]
    assert saved["name"] == fields["name"] and saved["price"] == 129
    assert saved["material"] == ("" if code == "FACTS_SAVE_CLEAR_DEFAULT_UNAVAILABLE" else "EVA")
    operations = [op for op in engine.store.list("operation") if op["key"] == "save_product"]
    assert len(operations) == 1 and operations[0]["status"] == "committed"
    if code == "FACTS_SAVE_FIELDS_INVALID":
        assert "unrecognized_candidate_field" in branch_before["report"]["fields"]
        assert not engine.store.list("product_candidate_save_resolution")
    prep = (await service.query("read_preparation", {"preparation_id": run["scope"]["preparation_id"]}))["body"]
    assert prep["selected_product_refs"] == [] and prep["script_refs"] == []
    assert prep["approval_ref"] is None and not engine.store.list("question")


def test_reference_input_marker_is_not_added_to_stored_proof_parsing():
    bad = {"kind": "analysis", "id": "bad", "revision": "1", "content_hash": "sha256:" + "1" * 64}
    with pytest.raises(ContractError) as input_error:
        SaveAcceptedProductInput.model_validate({"branch_task_id": "facts", "accepted_report_ref": bad})
    assert input_error.value.code == "FACTS_SAVE_REF_INVALID"
    assert input_error.value.details["commit_state"] == "not_committed"
    assert recoverable_input_error(input_error.value)
    with pytest.raises(ContractError) as proof_error:
        _ref(bad, "analysis")
    assert proof_error.value.code == "FACTS_SAVE_REF_INVALID"
    assert "commit_state" not in proof_error.value.details
    assert not recoverable_input_error(proof_error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize("boundary", ["no_local_commit_proof", "permission", "pending_unknown"])
async def test_m5_never_softens_same_code_without_prewrite_proof_or_hard_fence(code, boundary):
    engine = FakeEngine()
    request = tool_request("wf_product_materials", args={"action": "save_from_facts", "parameters": {}})
    error = (ContractError(code, "unattested", status=422) if boundary == "no_local_commit_proof" else
             uncommitted_input_error(code, "prewrite", status=403 if boundary == "permission" else 422))
    if boundary == "pending_unknown":
        engine.store.data[("run", "r1")]["pending_unknown_operation"] = "unknown"
    async def fail(_):
        raise error
    with pytest.raises(ContractError):
        await ActionBoundary(engine).awrap_tool_call(request, fail)
    assert not engine.store.get("run", "r1").get("correctable_tool_errors")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["formal_reference", "schema_hash"])
async def test_actual_formal_proof_and_remote_schema_failures_remain_hard(env, bad):
    engine, service = env
    _, scope, _, branch, reference = await accepted_candidate(engine, service)
    before = stats(service)
    original = engine.business_query
    async def corrupt(current_scope, key, args):
        value = await original(current_scope, key, args)
        if bad == "formal_reference" and key == "read_context":
            return {**value, "ref": {**value["ref"], "revision": "1"}}
        if bad == "schema_hash" and key == "get_business_schema":
            return {**value, "schema_hash": "sha256:" + "0" * 64}
        return value
    with patch.object(engine, "business_query", side_effect=corrupt):
        with pytest.raises(ContractError) as error:
            await save_accepted_product(engine, scope, {"branch_task_id": branch, "accepted_report_ref": reference},
                tool_call_id="hard-proof-rejection")
    assert error.value.code == ("FACTS_SAVE_REF_INVALID" if bad == "formal_reference" else "FACTS_SAVE_SCHEMA_INVALID")
    assert not recoverable_input_error(error.value)
    assert "commit_state" not in error.value.details
    assert stats(service) == before and not engine.store.list("product_candidate_save_resolution")
