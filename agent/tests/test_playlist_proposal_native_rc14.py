"""Optional F05 assembly over the real Deep Agents loop, with fake providers.

The planner chooses the sequence and next tool. Assembly neither asks nor saves;
the existing public question/answer/receipt boundary remains authoritative.
"""
from copy import deepcopy

import pytest

from shengbo_contracts.common import canonical_hash, ContractError
from shengbo_prelive.workflows import run_workflow
from test_playlist_order_confirmation import playlist_case, answer, identity_items
from test_provider_output_convergence import call, final, tool_outputs, convergence


@pytest.mark.asyncio
async def test_workflow_nested_invalid_input_preserves_safe_m5_correction_contract(playlist_case):
    case = playlist_case
    private = "private-malformed-test-input-do-not-display"
    from shengbo_prelive.action_contracts import recoverable_input_error
    with pytest.raises(ContractError, match="PLAYLIST_PROPOSAL_INPUT_INVALID") as caught:
        await run_workflow(case.engine, case.scope, "wf_playback_review", {
            "action": "prepare_order", "parameters": {"ordered_script_refs": [{"kind": private}], "extra": private}})
    assert recoverable_input_error(caught.value)
    assert private not in str(caught.value.as_error())
    assert not case.engine.store.list("question") and not case.client.commands
    saved, = case.engine.store.list("workflow")
    assert saved["status"] == "NEEDS_REPLAN" and saved["completed_steps"] == []


@pytest.mark.asyncio
async def test_native_main_can_select_helper_then_real_human_gate_without_automatic_write(playlist_case):
    case = playlist_case
    selected = [deepcopy(script["ref"]) for script in reversed(case.scripts)]
    seen = {}

    def respond(payload):
        number = len(case.sent)
        seen.update(tool_outputs(payload))
        if number == 1:
            return [call("wf_playback_review", "explicit-order-assembly", {
                "action": "prepare_order", "parameters": {"ordered_script_refs": selected}})]
        if number == 2:
            result = seen["explicit-order-assembly"]["result"]
            assert result["question_created"] is False and result["playlist_saved"] is False
            assert result["human_approved"] is False
            assert not case.engine.store.list("question") and not case.client.commands
            assert [item["script_revision_ref"]["id"] for item in result["playlist_proposal"]["items"]] == [ref["id"] for ref in selected]
            case.proposed = deepcopy(result["playlist_proposal"])
            return [call("request_user_decision", "ask-model-selected-proposal", result["optional_next_action"]["arguments"])]
        if number == 3:
            decision = seen["ask-model-selected-proposal"]
            assert decision["answer"] == "confirmed"
            assert decision["human_receipt"]["decision"] == "playlist_order_confirmed"
            binding = decision["playlist_order_binding"]
            assert binding["items"] == identity_items(case.proposed["items"])
            return [call("save_playlist", "save-true-approved-proposal", {**binding, "expected_revision": 7})]
        assert number == 4
        assert seen["save-true-approved-proposal"]["status"] == "committed"
        return final()

    case.respond = respond
    result, snapshot, _ = await case.engine._invoke(case.run, invocation_id="optional-proposal-before-human")
    assert result.get("__interrupt__") and any(task.interrupts for task in snapshot.tasks)
    assert len(case.sent) == 2 and not case.client.commands
    question, = case.engine.store.list("question")
    assert question["playlist_order_binding_hash"] == canonical_hash(question["playlist_order_binding"])
    assert [row["variant_id"] for row in question["preview"]["items"]] == [script["body"]["variant_id"] for script in reversed(case.scripts)]
    saved = await answer(case, question)
    assert saved["verified_human_receipt"]["answer_event_ref"] == saved["answer_event_ref"]
    result, snapshot, _ = await case.engine._invoke(case.engine.store.get("run", case.run["id"]), invocation_id="optional-proposal-after-human")
    assert not snapshot.next and len(case.sent) == case.credentials.calls == 4
    command, = case.client.commands
    assert command["key"] == "save_playlist"
    assert command["args"]["items"] == question["playlist_order_binding"]["items"]
    assert command["args"]["loop_mode"] == "infinite"
    operation, = case.engine.store.list("operation")
    assert operation["status"] == "committed"
    assert not any(row["key"] in {"prepare_required_audio", "publish_preparation"} for row in case.client.commands)
