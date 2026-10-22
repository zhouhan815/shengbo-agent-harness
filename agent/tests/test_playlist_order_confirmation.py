"""Playlist writes require the exact proposal confirmed through the public API."""
from collections import Counter
from copy import deepcopy
from unittest.mock import patch

import pytest

from shengbo_contracts.business import COMMAND_ARGS
from shengbo_contracts.common import ArtifactRef, ContractError, canonical_hash, canonical_json
from shengbo_prelive.models import AnswerRequest
from test_provider_output_convergence import (
    assert_no_context_churn, call, convergence, final, invoke, ref, tool_outputs,
)


def exact(reference):
    return ArtifactRef.model_validate(reference).model_dump(mode="json")


def identity_items(items):
    return [{**item, "script_revision_ref": {key: item["script_revision_ref"][key]
        for key in ("kind", "id", "revision", "content_hash")}} for item in items]


@pytest.fixture
async def playlist_case(convergence):
    case = convergence
    prep_id = case.run["scope"]["preparation_id"]
    product_body = {"id": "product-playlist", "name": "公开测试通勤杯", "price": 99, "currency": "CNY"}
    case.product = {"ref": exact(ref("product", product_body["id"], product_body)), "body": product_body}
    case.scripts = []
    for position, variant in enumerate(("通勤自用版", "礼赠版")):
        body = {"preparation_id": prep_id, "product_id": product_body["id"], "product_ref": case.product["ref"],
                "variant_id": variant, "version_number": position + 1, "module_refs": {}}
        case.scripts.append({"ref": exact(ref("script_revision", "script-playlist-" + str(position + 1), body)), "body": body})
    prep_body = {"id": prep_id, "selected_product_refs": [case.product["ref"]],
        "script_refs": [script["ref"] for script in case.scripts], "playlist_ref": None, "generation_epoch": 3}
    case.client.preparation = {"ref": exact(ref("preparation", prep_id, prep_body, revision=7)), "body": prep_body}
    # This derived fixture uses a different product from convergence's plan;
    # explicitly freeze that intended write target instead of inheriting it.
    goal = {**case.run["goal"], "target_product_ids": [product_body["id"]],
        "action_target_refs": [case.product["ref"]], "context_product_refs": [case.product["ref"]]}
    case.run = case.engine.store.update("run", case.run["id"], goal=goal, frozen_goal_hash=canonical_hash(goal),
        target_product_ids=goal["target_product_ids"], action_target_refs=goal["action_target_refs"],
        context_product_refs=goal["context_product_refs"])
    case.playlist_args = {"preparation_id": prep_id, "expected_revision": 7, "loop_mode": "infinite",
        "product_group_order": [product_body["id"]], "items": [
            {"occurrence_id": f"exact-occurrence-{position + 1}", "product_id": product_body["id"],
             "script_revision_ref": script["ref"]} for position, script in enumerate(case.scripts)]}
    original_query, original_command = case.client.query, case.client.command

    async def query(key, args=None, **kwargs):
        if key not in {"read_script", "read_product"}:
            return await original_query(key, args, **kwargs)
        case.client.queries.append((key, deepcopy(args), deepcopy(kwargs)))
        assert kwargs["run_id"] == case.run["id"] and kwargs["scope"] == case.run["scope"]
        if key == "read_product":
            assert exact(args["ref"]) == case.product["ref"]
            return deepcopy(case.product)
        return deepcopy(next(script for script in case.scripts if exact(args["ref"]) == script["ref"]))

    async def command(key, args, mutation, **kwargs):
        if key != "save_playlist":
            return await original_command(key, args, mutation, **kwargs)
        assert COMMAND_ARGS[key].model_validate(args).model_dump(mode="json", exclude_none=True) == args
        assert mutation.grant_id == "grant-convergence" and mutation.expected_write_generation == 7
        assert kwargs["run_id"] == case.run["id"] and kwargs["scope"] == case.run["scope"]
        receipt = {"command": key, "operation_id": mutation.operation_id, "status": "committed", "data": {
            "playlist_ref": exact(ref("playlist", "playlist-public-confirmed", args)),
            "preparation_ref": exact(ref("preparation", prep_id, {"playlist": args}, revision=8))}}
        case.client.commands.append({"key": key, "args": deepcopy(args), "mutation": mutation,
            "receipt": deepcopy(receipt), "kwargs": deepcopy(kwargs)})
        return receipt

    case.client.query, case.client.command = query, command
    return case


class DecisionOpened(Exception):
    pass


async def open_question(case, tool_call_id="open-bound-playlist"):
    # The SDK integration below proves the real interrupt/resume. Other cases
    # stop precisely at interrupt after the real controller persisted its bound
    # question, then use the public answer method rather than forging receipts.
    previous = {question["id"] for question in case.engine.store.list("question")}
    with patch("shengbo_prelive.runtime.interrupt", side_effect=DecisionOpened):
        with pytest.raises(DecisionOpened):
            await case.engine.request_decision(case.scope, {"kind": "playlist_order", "prompt": "请核对顺序。",
                "context": {"playlist": deepcopy(case.playlist_args)}}, tool_call_id)
    return next(question for question in case.engine.store.list("question") if question["id"] not in previous)


async def answer(case, question, value="confirmed"):
    await case.engine.answer(question["id"], AnswerRequest(question_revision=question["revision"], answer=value,
        client_request_id="public-playlist-answer-" + value))
    return case.engine.store.get("question", question["id"])


def assert_public_preview(case, question):
    binding = question["playlist_order_binding"]
    assert question["playlist_order_binding_hash"] == canonical_hash(binding)
    assert binding["items"] == identity_items(case.playlist_args["items"])
    assert binding["loop_mode"] == "infinite"
    assert {item["value"] for item in question["options"]} == {"confirmed", "revise", "reject"}
    assert question["allow_text"] is False
    preview = question["preview"]
    assert preview["loop_mode"] == "infinite" and "循环" in preview["loop_label"]
    assert [item["position"] for item in preview["items"]] == [1, 2]
    assert [item["product_name"] for item in preview["items"]] == [case.product["body"]["name"]] * 2
    assert [item["variant_id"] for item in preview["items"]] == [script["body"]["variant_id"] for script in case.scripts]
    assert case.product["body"]["name"] in question["prompt"] and "循环" in question["prompt"]
    for script in case.scripts:
        assert script["body"]["variant_id"] in question["prompt"]


@pytest.mark.asyncio
async def test_sdk_missing_confirmation_interrupts_public_answer_then_saves_exact_bound_playlist(playlist_case):
    case = playlist_case

    def respond(payload):
        round_number, seen = len(case.sent), tool_outputs(payload)
        if round_number == 1:
            return [call("save_playlist", "playlist-without-confirmation", case.playlist_args)]
        if round_number == 2:
            error = seen["playlist-without-confirmation"]
            assert error["status"] == "error" and error["error"]["code"] == "PLAYLIST_DECISION_REQUIRED"
            assert error["correction_count"] == 1 and error["correction_limit"] == 3
            request = error["error"]["details"]["decision_request"]
            assert request["kind"] == "playlist_order" and request["context"]["playlist"]["items"] == identity_items(case.playlist_args["items"])
            return [call("request_user_decision", "ask-exact-playlist", request)]
        if round_number == 3:
            decision = seen["ask-exact-playlist"]
            assert decision["answer"] == "confirmed"
            assert decision["human_receipt"]["decision"] == "playlist_order_confirmed"
            binding = decision["playlist_order_binding"]
            return [call("save_playlist", "save-public-confirmed-playlist", {**binding, "expected_revision": 7})]
        assert round_number == 4 and seen["save-public-confirmed-playlist"]["status"] == "committed"
        return final()

    case.respond = respond
    first, snapshot, _ = await case.engine._invoke(case.run, invocation_id="playlist-before-human")
    assert first.get("__interrupt__") and any(task.interrupts for task in snapshot.tasks)
    assert len(case.sent) == 2 and not case.client.commands
    assert case.engine.store.get("run", case.run["id"])["status"] == "WAITING_USER"
    question = case.engine.store.list("question")[0]
    assert_public_preview(case, question)
    saved_question = await answer(case, question)
    expected = {"decision": "playlist_order_confirmed", "question_id": question["id"], "run_id": case.run["id"],
        "preparation_id": case.playlist_args["preparation_id"], "binding_hash": canonical_hash(question["playlist_order_binding"]),
        "answer_event_ref": saved_question["answer_event_ref"]}
    assert saved_question["verified_human_receipt"] == expected
    result, final_snapshot, _ = await case.engine._invoke(case.engine.store.get("run", case.run["id"]),
        invocation_id="playlist-after-public-human-answer")
    assert not final_snapshot.next and result["messages"][-1].content == "测试完成"
    assert len(case.sent) == case.credentials.calls == 4
    assert len(case.client.commands) == 1 and case.client.commands[0]["key"] == "save_playlist"
    assert case.client.commands[0]["args"]["items"] == question["playlist_order_binding"]["items"]
    assert case.client.commands[0]["args"]["loop_mode"] == "infinite"
    operation = case.engine.store.list("operation")[0]
    assert operation["status"] == "committed" and operation["request_hash"] == canonical_hash(operation["args"])
    assert operation["receipt"] == case.client.commands[0]["receipt"]
    assert_no_context_churn(case)


@pytest.mark.asyncio
async def test_fourth_missing_playlist_confirmation_stops_at_original_correction_limit(playlist_case):
    case = playlist_case
    case.respond = lambda payload: [call("save_playlist", f"unconfirmed-{len(case.sent)}", case.playlist_args)]
    with pytest.raises(ContractError, match="PLAYLIST_DECISION_REQUIRED"):
        await invoke(case, thread="playlist-missing-correction-cap")
    assert len(case.sent) == case.credentials.calls == 4
    assert case.engine.store.get("run", case.run["id"])["correctable_tool_errors"] == {"PLAYLIST_DECISION_REQUIRED": 3}
    assert not case.client.commands and case.engine.store.list("operation") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["revise", "reject"])
async def test_public_revise_or_reject_is_not_permission_to_save(playlist_case, choice):
    case = playlist_case
    question = await open_question(case)
    saved = await answer(case, question, choice)
    assert saved["status"] == "ANSWERED" and saved["verified_human_receipt"] is None
    with pytest.raises(ContractError, match="PLAYLIST_ORDER_BINDING_MISMATCH") as caught:
        await case.engine.business_command(case.scope, "save_playlist", case.playlist_args, "not-approved-playlist")
    assert caught.value.status == 403 and not case.client.commands


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["swap_order", "occurrence_id", "script_ref", "script_hash", "product_id",
    "group_order", "fewer_items", "reorder_spec", "preview_hash"])
async def test_confirmed_proposal_cannot_authorize_different_items_refs_or_order(playlist_case, change):
    case = playlist_case
    question = await open_question(case)
    await answer(case, question)
    args = deepcopy(case.playlist_args)
    if change == "swap_order":
        args["items"].reverse()
    elif change == "occurrence_id":
        args["items"][0]["occurrence_id"] = "replacement-occurrence"
    elif change == "script_ref":
        args["items"][0]["script_revision_ref"] = case.scripts[1]["ref"]
    elif change == "script_hash":
        args["items"][0]["script_revision_ref"]["content_hash"] = canonical_hash("unobserved-script")
    elif change == "product_id":
        args["items"][0]["product_id"] = "foreign-product"
    elif change == "group_order":
        args["product_group_order"] = ["foreign-product"]
    elif change == "fewer_items":
        args["items"].pop()
    elif change == "reorder_spec":
        args["reorder_spec"] = {"mode": "full_shuffle", "product_group_order": [case.product["ref"]["id"]]}
    elif change == "preview_hash":
        args["preview_hash"] = canonical_hash("different-preview")
    with pytest.raises(ContractError, match="PLAYLIST_ORDER_BINDING_MISMATCH") as caught:
        await case.engine.business_command(case.scope, "save_playlist", args, "changed-bound-playlist")
    assert caught.value.status == 403 and not case.client.commands


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing_receipt", "question_id", "run_id", "preparation_id", "binding_hash",
    "answer_event_ref", "question_binding", "question_binding_hash"])
async def test_answered_rows_and_tampered_human_bindings_do_not_authorize_writes(playlist_case, change):
    case = playlist_case
    question = await open_question(case)
    saved = await answer(case, question)
    modified = deepcopy(saved)
    if change == "missing_receipt":
        modified["verified_human_receipt"] = None
    elif change == "question_binding":
        modified["playlist_order_binding"]["items"].reverse()
    elif change == "question_binding_hash":
        modified["playlist_order_binding_hash"] = canonical_hash("altered-question-binding")
    else:
        modified["verified_human_receipt"][change] = "foreign-or-altered-" + change
    case.engine.store.put("question", modified)
    with pytest.raises(ContractError, match="PLAYLIST_ORDER_BINDING_MISMATCH") as caught:
        await case.engine.business_command(case.scope, "save_playlist", case.playlist_args, "tampered-proof-playlist")
    assert caught.value.status == 403 and not case.client.commands


@pytest.mark.asyncio
async def test_public_confirmation_rechecks_current_exact_script_product_and_binding(playlist_case):
    case = playlist_case
    question = await open_question(case)
    # The proposal was genuine when displayed. A source-body change under the
    # same advertised immutable ref must not receive a public confirmation proof.
    case.scripts[0]["body"]["variant_id"] = "changed-after-preview"
    with pytest.raises(ContractError, match="PLAYLIST_ORDER_TARGET_INVALID") as caught:
        await answer(case, question)
    assert caught.value.status == 403
    still_open = case.engine.store.get("question", question["id"])
    assert still_open["status"] == "OPEN" and not still_open.get("verified_human_receipt")
    assert not case.client.commands


@pytest.mark.asyncio
async def test_effective_infinite_loop_is_bound_for_legacy_once_without_changing_voice_behavior(playlist_case):
    case = playlist_case
    case.playlist_args["loop_mode"] = "once"
    case.playlist_args["product_group_order"] = []
    question = await open_question(case)
    assert_public_preview(case, question)
    assert question["playlist_order_binding"]["product_group_order"] == [case.product["ref"]["id"]]
    saved = await answer(case, question)
    assert saved["verified_human_receipt"]["binding_hash"] == canonical_hash(question["playlist_order_binding"])
    effective = {**case.playlist_args, "loop_mode": "infinite"}
    receipt = await case.engine.business_command(case.scope, "save_playlist", effective, "save-effective-infinite")
    assert receipt["status"] == "committed" and len(case.client.commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("later_choice", ["reject", "revise"])
async def test_later_public_rejection_or_revision_revokes_reuse_of_earlier_confirmation(playlist_case, later_choice):
    case = playlist_case
    first = await open_question(case)
    confirmed = await answer(case, first)
    assert confirmed["verified_human_receipt"]["decision"] == "playlist_order_confirmed"
    later = await open_question(case, "later-playlist-choice")
    newer = await answer(case, later, later_choice)
    assert newer["id"] != confirmed["id"] and newer["answered_at"] >= confirmed["answered_at"]
    assert newer["verified_human_receipt"] is None
    assert case.engine.store.get("question", confirmed["id"])["verified_human_receipt"] == confirmed["verified_human_receipt"]
    with pytest.raises(ContractError, match="PLAYLIST_ORDER_BINDING_MISMATCH") as caught:
        await case.engine.business_command(case.scope, "save_playlist", case.playlist_args, "do-not-reuse-old-confirmation")
    assert caught.value.status == 403 and not case.client.commands
