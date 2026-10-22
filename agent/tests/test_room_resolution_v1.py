from copy import deepcopy

import pytest

from shengbo_contracts.common import canonical_hash
from shengbo_prelive.room_resolution import (
    intent_scope_fingerprint, make_room_resolution, parse_room_selection_answer,
    room_resolution_matches, room_selection_context,
)


def sample():
    run = {"id": "run_one", "phase": "intent", "scope": {"kind": "catalog"},
        "input": {"text": "只为商品A生成两版话术，不生成语音。", "selection": {"product_id": "product_a"}},
        "intent_assessment": {"objective": "materials_to_script", "summary": "生成两版",
            "room_id": None, "preparation_id": None, "target_product_ids": ["product_a"],
            "requested_product_paths": [], "requested_modules": [],
            "script_mode": "generate", "script_delivery_requirement": "new_generation",
            "requested_script_variants": [{"product_id": "product_a", "count": 2}],
            "proposed_effects": ["save_session_draft", "save_script_plan"],
            "required_analyses": [], "authorization_quote": "只为商品A生成两版话术，不生成语音。",
            "needs_clarification": True, "clarification": "请选择直播间"}}
    body = {"id": "room_a", "name": "普通房间", "commerce": {"mode": "none"}}
    room = {"ref": {"kind": "config", "id": "room_a", "revision": 1,
        "content_hash": canonical_hash(body)}, "body": body}
    context = room_selection_context(run, [{"id": "room_a", "name": "普通房间"}])
    question = {"id": "decision_a", "run_id": run["id"], "kind": "task_scope",
        "controller_question": True, "status": "ANSWERED", "context": context,
        "answer": ' {"room_id": "room_a"} ', "answer_event_ref": "answer_a", "answer_request_id": "request_a"}
    return run, question, room


@pytest.mark.parametrize("answer", [
    '{"room_id":"room_a"}', {"room_id": "room_a"}, {"text": '{"room_id":"room_a"}'},
    "room:room_a", "room_a", "普通房间", "请使用 room_a 进行本次准备。",
])
def test_accepts_only_explicit_room_answers(answer):
    run, question, room = sample()
    assert parse_room_selection_answer(answer, question["context"]) == "room_a"
    question["answer"] = answer
    proof = make_room_resolution(run, question, resolved_room=room)
    assert proof and proof["scope_approval_granted"] is False


@pytest.mark.parametrize("answer", [
    "confirm", "revise", "可以继续", "不要使用 room_a", "请勿使用 room_a", "use room_a or room_b",
    "请使用 room_a，不要生成语音", "可以考虑 room_a", "使用 room_a 还是 room_b", "room_a room_a",
    "room_a 是什么？", "room_a备用", '{"room_id":"room_a","approve":true}',
    '{"room_id":"room_a","room_id":"room_b"}', '{"room_id":1}', '["room_a"]',
    {"text": "room_a", "approved": True}, {"text": {"room_id": "room_a"}}, {"room_id": "../room_a"},
])
def test_ambiguous_negative_or_overbroad_answers_are_not_authorization(answer):
    _run, question, _room = sample()
    # A full, valid room_ token can itself contain non-ASCII-free suffixes only
    # within its documented identifier grammar. Chinese prose is not an ID.
    assert parse_room_selection_answer(answer, question["context"]) is None


def test_duplicate_room_names_require_id():
    _run, q, _room = sample()
    q["context"]["room_options"].append({"id": "room_b", "name": "普通房间"})
    assert parse_room_selection_answer("普通房间", q["context"]) is None
    assert parse_room_selection_answer("room_a", q["context"]) == "room_a"


def test_room_and_presentation_can_change_but_scope_cannot():
    run, q, room = sample()
    original = deepcopy(run)
    run["room_resolution"] = make_room_resolution(run, q, resolved_room=room)
    proposal = deepcopy(run["intent_assessment"])
    proposal.update(room_id="room_a", summary="使用选定房间完成两版", needs_clarification=False, clarification=None)
    assert room_resolution_matches(run, proposal, room)
    assert run["input"] == original["input"]
    assert not run.get("confirmed_intent_hash") and not run.get("goal")


@pytest.mark.parametrize("field,value", [
    ("objective", "prepare_session"), ("target_product_ids", ["product_b"]),
    ("preparation_id", "preparation_other"), ("requested_product_paths", ["/price"]),
    ("requested_modules", ["campaign"]), ("script_mode", "edit"),
    ("script_delivery_requirement", "reuse_existing"),
    ("requested_script_variants", [{"product_id": "product_a", "count": 3}]),
    ("proposed_effects", ["publish_preparation"]), ("required_analyses", ["product_facts"]),
    ("authorization_quote", "改商品价格"), ("future_scope_field", "new effect"),
])
def test_every_semantic_scope_change_invalidates_narrow_proof(field, value):
    run, q, room = sample()
    run["room_resolution"] = make_room_resolution(run, q, resolved_room=room)
    proposal = {**run["intent_assessment"], "room_id": "room_a", field: value}
    assert not room_resolution_matches(run, proposal, room)


@pytest.mark.parametrize("mutation", [
    "other_run", "changed_input", "changed_selection", "open", "no_event", "no_request",
    "not_controller", "wrong_kind", "untyped_context", "changed_intent", "frozen_goal", "wrong_phase",
])
def test_proof_requires_same_run_original_input_typed_answer_and_unfrozen_intent(mutation):
    run, q, room = sample()
    if mutation == "other_run": q["run_id"] = "run_other"
    if mutation == "changed_input": run["input"]["text"] += " 额外任务"
    if mutation == "changed_selection": run["input"]["selection"]["product_id"] = "product_b"
    if mutation == "open": q["status"] = "OPEN"
    if mutation == "no_event": q["answer_event_ref"] = None
    if mutation == "no_request": q["answer_request_id"] = None
    if mutation == "not_controller": q["controller_question"] = False
    if mutation == "wrong_kind": q["kind"] = "new_product_import"
    if mutation == "untyped_context": q["context"] = {}
    if mutation == "changed_intent": run["intent_assessment"]["target_product_ids"] = ["product_b"]
    if mutation == "frozen_goal": run["goal"] = {"objective": "materials_to_script"}
    if mutation == "wrong_phase": run["phase"] = "execute"
    assert make_room_resolution(run, q, resolved_room=room) is None


def test_room_must_be_read_and_hash_valid_and_identity_exact():
    run, q, room = sample()
    wrong = deepcopy(room)
    wrong["body"]["id"] = wrong["ref"]["id"] = "room_b"
    wrong["ref"]["content_hash"] = canonical_hash(wrong["body"])
    assert make_room_resolution(run, q, resolved_room=wrong) is None
    stale = deepcopy(room)
    stale["body"]["name"] = "different without new hash"
    assert make_room_resolution(run, q, resolved_room=stale) is None
    run["room_resolution"] = make_room_resolution(run, q, resolved_room=room)
    assert not room_resolution_matches(run, {**run["intent_assessment"], "room_id": "room_b"}, wrong)


def test_order_of_set_like_semantic_fields_does_not_create_another_confirmation():
    run, q, _room = sample()
    before = run["intent_assessment"]
    after = deepcopy(before)
    after["proposed_effects"].reverse()
    assert intent_scope_fingerprint(before) == intent_scope_fingerprint(after)


def test_another_run_or_original_input_cannot_reuse_the_stored_proof():
    run, q, room = sample()
    run["room_resolution"] = make_room_resolution(run, q, resolved_room=room)
    proposal = {**run["intent_assessment"], "room_id": "room_a"}
    other = {**run, "id": "run_other"}
    assert not room_resolution_matches(other, proposal, room)
    changed = deepcopy(run)
    changed["input"]["selection"]["product_id"] = "product_other"
    assert not room_resolution_matches(changed, proposal, room)
