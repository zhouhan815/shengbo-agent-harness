"""Narrow, durable room identity choices; never a general intent approval.

The controller owns questions/proofs. The caller still performs ordinary intent,
effect, product, field, module and preparation-scope checks. These helpers neither
grant permissions nor read/write business state.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy

from shengbo_contracts.common import ArtifactRef, canonical_hash


_KIND = "room_selection"
_PROOF_VERSION = "room-resolution-v1"
# Presentation / clarification progress is not an action boundary. All other
# intent fields, including unknown future fields, remain in the fingerprint.
_NON_SCOPE = {"room_id", "summary", "clarification", "needs_clarification"}
_SET_FIELDS = {"target_product_ids", "requested_modules", "requested_product_paths",
               "proposed_effects", "required_analyses", "required_reuse_analysis_refs"}
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_ROOM_ID = re.compile(r"(?<![A-Za-z0-9_-])room_[A-Za-z0-9_-]{1,190}(?![A-Za-z0-9_-])")
_NEGATIVE_OR_AMBIGUOUS = re.compile(
    r"不|别|勿|否|拒绝|排除|除外|或者|还是|可能|考虑|建议|例如|比如|假设|如果|待定|[？?]"
    r"|\b(?:not|no|don't|except|maybe|either|or|consider|example|if)\b", re.I)
_AFFIRMATIVE = re.compile(r"使用|选用|选择|采用|指定|绑定|进入|用|\b(?:use|select|choose)\b", re.I)


def _wire(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict):
        raise ValueError("intent must be a validated intent dictionary")
    return deepcopy(value)


def intent_scope_fingerprint(intent) -> str:
    """Bind every semantic intent field except the room being clarified."""
    value = {k: v for k, v in _wire(intent).items() if k not in _NON_SCOPE}
    # Neutral defaults introduced after a pending room question do not change
    # its already approved task. Nonempty exact reuse obligations still bind.
    for key in ("required_reuse_analysis_refs", "reuse_analysis_quote"):
        if not value.get(key):
            value.pop(key, None)
    for key in _SET_FIELDS:
        if isinstance(value.get(key), list):
            value[key] = sorted(value[key], key=canonical_hash)
    if isinstance(value.get("requested_script_variants"), list):
        value["requested_script_variants"] = sorted(value["requested_script_variants"], key=canonical_hash)
    return canonical_hash(value)


def room_selection_context(run: dict, rooms=()) -> dict:
    """Create trusted task_scope context before asking for a room.

    Optional rooms are server-discovered {id, name} choices, never model-proposed
    approvals. Every selected identity must still be read from the business API.
    """
    if not run.get("id") or not isinstance(run.get("input"), dict) or not run.get("intent_assessment"):
        raise ValueError("room selection needs a run, original input and intent")
    options = []
    for room in rooms:
        identity, name = room.get("id"), room.get("name")
        if (not isinstance(identity, str) or not _OPAQUE_ID.fullmatch(identity)
                or not isinstance(name, str) or not name.strip()):
            raise ValueError("invalid server room option")
        option = {"id": identity, "name": name.strip()}
        if option not in options:
            options.append(option)
    return {"resolution_kind": _KIND, "run_id": run["id"],
        "original_input_hash": canonical_hash(run["input"]),
        "intent_scope_hash": intent_scope_fingerprint(run["intent_assessment"]),
        "room_options": options}


def _context_matches(run, context, intent=None):
    try:
        return (isinstance(context, dict) and context.get("resolution_kind") == _KIND
            and context.get("run_id") == run.get("id")
            and context.get("original_input_hash") == canonical_hash(run["input"])
            and context.get("intent_scope_hash") == intent_scope_fingerprint(
                intent if intent is not None else run["intent_assessment"]))
    except (KeyError, TypeError, ValueError):
        return False


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate selection key")
        result[key] = value
    return result


def parse_room_selection_answer(answer, context: dict) -> str | None:
    """Recognize an explicit choice, never an LLM-inferred authorization.

    Supports JSON {room_id}, room:<id>, a full room_ ID, or a unique exact server
    option name/ID. Conservative natural text needs one full ID, an affirmative
    choice and no negation/ambiguity anywhere. None means ask, not authorize.
    """
    if not isinstance(context, dict) or context.get("resolution_kind") != _KIND:
        return None
    if isinstance(answer, dict) and set(answer) == {"text"}:
        if not isinstance(answer["text"], str):
            return None
        answer = answer["text"]
    if isinstance(answer, dict):
        value = answer.get("room_id") if set(answer) == {"room_id"} else None
        return value if isinstance(value, str) and _OPAQUE_ID.fullmatch(value) else None
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 6000:
        return None
    text = answer.strip()
    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text, object_pairs_hook=_unique_object)
        except (TypeError, ValueError):
            return None
        return parse_room_selection_answer(parsed, context) if isinstance(parsed, dict) else None
    if text.startswith("room:"):
        identity = text[5:]
        return identity if _OPAQUE_ID.fullmatch(identity) else None
    if _ROOM_ID.fullmatch(text):
        return text
    options = context.get("room_options") or []
    exact = {r["id"] for r in options if isinstance(r, dict)
             and isinstance(r.get("id"), str) and text in {r.get("id"), r.get("name")}}
    if len(exact) == 1:
        return next(iter(exact))
    if exact or _NEGATIVE_OR_AMBIGUOUS.search(text):
        return None
    identities = list(_ROOM_ID.finditer(text))
    if len(identities) != 1 or not _AFFIRMATIVE.search(text[:identities[0].start()]):
        return None
    return identities[0].group()


def _room_ref(room):
    try:
        ref = ArtifactRef.model_validate(room["ref"], strict=True).model_dump(mode="json")
        body = room["body"]
        if (ref["kind"] != "config" or not isinstance(body, dict)
                or body.get("id") != ref["id"] or canonical_hash(body) != ref["content_hash"]):
            return None
        return ref
    except (KeyError, TypeError, ValueError):
        return None


def make_room_resolution(run: dict, question: dict, *, resolved_room: dict) -> dict | None:
    """Build proof only after the real answer was committed and room was read.

    Do not use this for OPEN questions, guesses, legacy untyped scope questions,
    already-frozen Goals, or general confirm buttons.
    """
    context = question.get("context")
    if (run.get("goal") or run.get("phase") != "intent"
            or question.get("kind") != "task_scope" or question.get("controller_question") is not True
            or question.get("run_id") != run.get("id") or question.get("status") != "ANSWERED"
            or not question.get("id") or not question.get("answer_event_ref")
            or not question.get("answer_request_id") or not _context_matches(run, context)):
        return None
    selected = parse_room_selection_answer(question.get("answer"), context)
    ref = _room_ref(resolved_room)
    if not selected or not ref or selected != ref["id"]:
        return None
    return {"version": _PROOF_VERSION, "resolution_kind": _KIND,
        "run_id": run["id"], "room_id": selected, "room_ref": ref,
        "question_id": question["id"], "answer_event_ref": question["answer_event_ref"],
        "question_context_hash": canonical_hash(context),
        "original_input_hash": context["original_input_hash"],
        "intent_scope_hash": context["intent_scope_hash"],
        "scope_approval_granted": False}


def room_resolution_matches(run: dict, proposal, room: dict) -> bool:
    """Only skip identity re-confirmation; all other authorization gates remain."""
    proof, ref = run.get("room_resolution"), _room_ref(room)
    try:
        candidate = _wire(proposal)
        return bool(run.get("phase") == "intent" and not run.get("goal")
            and isinstance(proof, dict) and ref
            and proof.get("version") == _PROOF_VERSION and proof.get("resolution_kind") == _KIND
            and proof.get("scope_approval_granted") is False
            and proof.get("run_id") == run.get("id")
            and proof.get("question_id") and proof.get("answer_event_ref")
            and proof.get("question_context_hash")
            and proof.get("room_id") == candidate.get("room_id") == ref["id"]
            and proof.get("original_input_hash") == canonical_hash(run["input"])
            and proof.get("intent_scope_hash") == intent_scope_fingerprint(candidate))
    except (KeyError, TypeError, ValueError):
        return False
