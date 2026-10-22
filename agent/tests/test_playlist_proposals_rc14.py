"""Optional proposal over real isolated Runtime/Voice; no model or TTS calls."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from shengbo_contracts.business import COMMAND_ARGS, MODULE_KEYS
from shengbo_contracts.common import ContractError, canonical_hash, canonical_json
from shengbo_prelive.api import create_app
from shengbo_prelive.models import DecisionRequest, RunScope
from shengbo_prelive.playlist_proposals import PlaylistProposalInput, prepare_playlist_proposal
from test_optional_workflows import authorize
from test_runtime_business import env, human_command, new_run, product


class DecisionOpened(Exception):
    pass


def stats(service):
    with service.store.transaction() as db:
        return {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in ("records", "record_revisions", "prelive_operations", "prelive_events", "prelive_sequences")}


@pytest.fixture
async def proposal_case(env):
    engine, service = env
    products = [await product(service, "明确选择商品A"), await product(service, "明确选择商品B")]
    room = (await human_command(service, "create_room", {"name": "只读排序提案隔离房间"}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    prep = (await human_command(service, "update_preparation_products", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "selected_product_refs": products}))["data"]["preparation_ref"]
    draft = (await human_command(service, "save_session_draft", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "draft": {"theme": "离线提案", "commerce": {}}}))["data"]
    bundle = (await human_command(service, "save_context_bundle", {"preparation_id": prep["id"],
        "expected_revision": draft["preparation_ref"]["revision"], "product_refs": products,
        "draft_ref": draft["draft_ref"]}))["data"]
    items = [{"product_ref": products[index], "variant_id": variant, "version_number": position + 1,
        "modules": [{"key": key, "action": "disable" if key in {"campaign", "transition"} else "generate"}
                    for key in MODULE_KEYS]} for position, (index, variant) in enumerate(((0, "A1"), (0, "A2"), (1, "B1")))]
    plan = (await human_command(service, "save_script_plan", {"preparation_id": prep["id"],
        "expected_revision": bundle["preparation_ref"]["revision"], "bundle_ref": bundle["context_bundle_ref"],
        "items": items}))["data"]
    await human_command(service, "submit_script_change", {"preparation_id": prep["id"],
        "expected_revision": plan["preparation_ref"]["revision"], "plan_ref": plan["script_plan_ref"],
        "context_bundle_ref": bundle["context_bundle_ref"], "expected_generation_epoch": plan["generation_epoch"]})
    await service.drain_jobs()
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    scripts = [(await service.query("read_script", {"ref": ref})) for ref in current["body"]["script_refs"]]
    scripts = {script["body"]["variant_id"]: script for script in scripts}
    assert set(scripts) == {"A1", "A2", "B1"}
    run = await new_run(engine, "准备本场并核对播放顺序，先不生成语音", room_id=room["id"], preparation_id=prep["id"])
    run = await authorize(engine, run, ["save_playlist"])
    goal = {**run["goal"], "target_product_ids": [ref["id"] for ref in products],
        "action_target_refs": products, "context_product_refs": products}
    run = engine.store.update("run", run["id"], goal=goal, target_product_ids=goal["target_product_ids"],
        action_target_refs=products, context_product_refs=products)
    return SimpleNamespace(engine=engine, service=service, products=products, room=room,
        prep=current, scripts=scripts, run=run, scope=RunScope(run["id"], "proposal-fixture", "execute"))


def args(case, variants=("B1", "A1", "B1")):
    return {"ordered_script_refs": [deepcopy(case.scripts[variant]["ref"]) for variant in variants]}


def assert_not_executed(case, result):
    assert result["question_created"] is result["human_approved"] is result["playlist_saved"] is False
    assert result["automatic_execution"] is result["content_review_completed"] is False
    assert result["requires_revalidation_on_decision_or_save"] is True
    assert not case.engine.store.list("question") and not case.engine.store.list("operation")


@pytest.mark.asyncio
async def test_exact_explicit_order_repeats_and_read_cost_without_any_business_transition(proposal_case, monkeypatch):
    case = proposal_case
    submitted = args(case)
    original = deepcopy(submitted)
    before = stats(case.service)
    queries = []
    original_query = case.engine.client.query
    async def query(key, arguments=None, **kwargs):
        queries.append((key, deepcopy(arguments)))
        assert kwargs["run_id"] == case.run["id"] and kwargs["scope"] == case.run["scope"]
        return await original_query(key, arguments, **kwargs)
    monkeypatch.setattr(case.engine.client, "query", query)
    result = await prepare_playlist_proposal(case.engine, case.scope, submitted)
    assert submitted == original and stats(case.service) == before
    assert_not_executed(case, result)
    assert result["status"] == "PROPOSAL_READY_FOR_OPTIONAL_DECISION"
    assert [row["variant_id"] for row in result["preview"]] == ["B1", "A1", "B1"]
    assert [row["position"] for row in result["preview"]] == [1, 2, 3]
    proposal = result["playlist_proposal"]
    assert proposal["loop_mode"] == "infinite"
    assert proposal["product_group_order"] == [case.products[1]["id"], case.products[0]["id"]]
    assert len({row["occurrence_id"] for row in proposal["items"]}) == 3
    assert proposal["items"][0]["script_revision_ref"] == proposal["items"][2]["script_revision_ref"]
    assert result["proposal_hash"] == canonical_hash(proposal)
    assert Counter(key for key, _ in queries) == {"read_preparation": 2, "read_script": 4, "read_product": 2}
    assert sum(arguments.get("sections") == ["body"] for key, arguments in queries if key == "read_script") == 2
    # Real current checks are reused, not summarized/cached into a fake grant.
    decision = result["optional_next_action"]
    assert decision["tool"] == "request_user_decision"
    assert DecisionRequest.model_validate(decision["arguments"]).kind == "playlist_order"
    assert decision["arguments"]["context"]["playlist"] == proposal
    COMMAND_ARGS["save_playlist"].model_validate({**proposal, "expected_revision": case.prep["ref"]["revision"]})
    second = await prepare_playlist_proposal(case.engine, case.scope, submitted)
    assert second["playlist_proposal"] == proposal and stats(case.service) == before
    result["playlist_proposal"]["items"][0]["product_id"] = "changed-return-only"
    assert second["playlist_proposal"]["items"][0]["product_id"] == case.products[1]["id"]


@pytest.mark.asyncio
async def test_subset_is_not_completed_or_sorted_and_occurrence_ids_stable_across_explicit_reorder(proposal_case):
    case = proposal_case
    a = await prepare_playlist_proposal(case.engine, case.scope, args(case, ("A2",)))
    assert len(a["playlist_proposal"]["items"]) == 1 and a["preview"][0]["variant_id"] == "A2"
    first = await prepare_playlist_proposal(case.engine, case.scope, args(case, ("B1", "A1", "B1")))
    other = await prepare_playlist_proposal(case.engine, case.scope, args(case, ("A1", "B1", "B1")))
    assert {row["occurrence_id"] for row in first["playlist_proposal"]["items"]} == {
        row["occurrence_id"] for row in other["playlist_proposal"]["items"]}
    assert first["proposal_hash"] != other["proposal_hash"]


@pytest.mark.asyncio
async def test_expected_preparation_is_exact_and_stale_ref_never_silently_refreshed(proposal_case):
    case = proposal_case
    exact = {key: value for key, value in case.prep["ref"].items() if key != "schema_version"}
    good = await prepare_playlist_proposal(case.engine, case.scope, {**args(case), "expected_preparation_ref": exact})
    assert good["base_preparation_ref"] == case.prep["ref"]
    for ref in ({**exact, "revision": exact["revision"] + 1}, {**exact, "content_hash": "sha256:" + "0" * 64}):
        with pytest.raises(ContractError, match="PLAYLIST_PROPOSAL_STALE"):
            await prepare_playlist_proposal(case.engine, case.scope, {**args(case), "expected_preparation_ref": ref})


def ref():
    return {"kind": "script_revision", "id": "script-fixture", "revision": 1, "content_hash": "sha256:" + "1" * 64}


@pytest.mark.parametrize("updates", [
    {}, {"ordered_script_refs": []}, {"ordered_script_refs": [ref()] * 1001},
    {"ordered_script_refs": (ref(),)}, {"ordered_script_refs": [{**ref(), "kind": "module"}]},
    {"ordered_script_refs": [{**ref(), "revision": True}]}, {"ordered_script_refs": [{**ref(), "revision": "1"}]},
    {"ordered_script_refs": [{**ref(), "id": ""}]}, {"ordered_script_refs": [{**ref(), "id": "x" * 257}]},
    {"ordered_script_refs": [{**ref(), "content_hash": "sha256:short"}]},
    {"ordered_script_refs": [{**ref(), "schema_version": "WRONG"}]},
    {"ordered_script_refs": [ref()], "expected_preparation_ref": ref()},
    {"ordered_script_refs": [ref()], "prompt": "   "}, {"ordered_script_refs": [ref()], "prompt": 3},
    {"ordered_script_refs": [ref()], "human_approved": True},
])
def test_closed_strict_input_ref_lengths_and_no_authority_fields(updates):
    with pytest.raises((ValueError, TypeError)):
        PlaylistProposalInput.model_validate(updates)


@pytest.mark.asyncio
async def test_invalid_arguments_have_constant_safe_error_not_pydantic_input(proposal_case):
    case = proposal_case
    secret = "fake-private-diagnostic-that-must-not-appear"
    before = stats(case.service)
    for values in ({**args(case), "unknown_private_key": secret},
                   {"ordered_script_refs": [{**ref(), "revision": secret}]},
                   PlaylistProposalInput.model_construct(ordered_script_refs=[{**ref(), "revision": secret}])):
        with pytest.raises(ContractError) as caught:
            await prepare_playlist_proposal(case.engine, case.scope, values)
        assert caught.value.code == "PLAYLIST_PROPOSAL_INPUT_INVALID"
        assert secret not in str(caught.value) + canonical_json(caught.value.details)
    assert stats(case.service) == before and not case.engine.store.list("question")


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["child", "intent", "no_effect", "unknown", "cancelled", "other_account"])
async def test_role_goal_unknown_and_revocation_guards_do_not_create_proposal_actions(proposal_case, boundary):
    case = proposal_case
    scope = case.scope
    if boundary == "child": scope = replace(scope, role="product_facts", branch_task_id="not-authorized")
    if boundary == "intent": scope = replace(scope, phase="intent")
    if boundary == "other_account": scope = replace(scope, account_id="different")
    if boundary == "no_effect": case.engine.store.update("run", case.run["id"], allowed_effects=[])
    if boundary == "unknown": case.engine.store.update("run", case.run["id"], pending_unknown_operation="op-unknown")
    if boundary == "cancelled": case.engine.store.update("run", case.run["id"], status="CANCELLED")
    before = stats(case.service)
    with pytest.raises(ContractError):
        await prepare_playlist_proposal(case.engine, scope, args(case))
    assert stats(case.service) == before and not case.engine.store.list("question")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["allowed_effects", "pending_unknown_operation", "status"])
async def test_authority_change_during_reads_is_rechecked_before_return(proposal_case, monkeypatch, change):
    case = proposal_case
    original_query = case.engine.client.query
    async def query(key, arguments=None, **kwargs):
        result = await original_query(key, arguments, **kwargs)
        if key == "read_product":
            case.engine.store.update("run", case.run["id"], **{change: {
                "allowed_effects": [], "pending_unknown_operation": "op-unknown", "status": "CANCELLED"}[change]})
        return result
    monkeypatch.setattr(case.engine.client, "query", query)
    before = stats(case.service)
    with pytest.raises(ContractError):
        await prepare_playlist_proposal(case.engine, case.scope, args(case))
    assert stats(case.service) == before and not case.engine.store.list("question")


@pytest.mark.asyncio
async def test_foreign_unselected_or_stale_script_cannot_be_replaced_by_name(proposal_case):
    case = proposal_case
    before = stats(case.service)
    for bad in ({**case.scripts["A1"]["ref"], "id": "another-preparation-script"},
                {**case.scripts["A1"]["ref"], "revision": 2}):
        with pytest.raises(ContractError, match="PLAYLIST_ORDER_TARGET_INVALID"):
            await prepare_playlist_proposal(case.engine, case.scope, {"ordered_script_refs": [bad]})
    assert stats(case.service) == before
    await human_command(case.service, "update_preparation_products", {"preparation_id": case.prep["ref"]["id"],
        "expected_revision": case.prep["ref"]["revision"], "selected_product_refs": [case.products[0]]})
    before = stats(case.service)
    with pytest.raises(ContractError, match="PLAYLIST_ORDER_TARGET_INVALID"):
        await prepare_playlist_proposal(case.engine, case.scope, args(case, ("B1",)))
    assert stats(case.service) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["script_hash", "script_preparation", "product_hash", "product_identity"])
async def test_original_verifier_rejects_exact_body_or_product_ownership_corruption(proposal_case, monkeypatch, kind):
    case = proposal_case
    original_query = case.engine.client.query
    async def query(key, arguments=None, **kwargs):
        result = deepcopy(await original_query(key, arguments, **kwargs))
        if key == "read_script" and kind.startswith("script"):
            result["body"]["preparation_id"] = "foreign" if kind == "script_preparation" else result["body"]["preparation_id"]
            result["body"]["variant_id"] = "changed-without-matching-ref"
        if key == "read_product" and kind.startswith("product"):
            result["body"]["name"] = "wrong product body"
            if kind == "product_identity": result["ref"]["id"] = "unrelated-product"
        return result
    monkeypatch.setattr(case.engine.client, "query", query)
    before = stats(case.service)
    with pytest.raises(ContractError, match="PLAYLIST_ORDER_TARGET_INVALID"):
        await prepare_playlist_proposal(case.engine, case.scope, args(case))
    assert stats(case.service) == before and not case.engine.store.list("question")


async def open_question(case, arguments, monkeypatch, call_id="simulated-playlist-question"):
    def interrupt(_value):
        raise DecisionOpened
    monkeypatch.setattr("shengbo_prelive.runtime.interrupt", interrupt)
    with pytest.raises(DecisionOpened):
        await case.engine.request_decision(case.scope, arguments, call_id)
    return case.engine.store.list("question")[-1]


async def simulated_api_answer(case, question, answer):
    # Real protected API and real Runtime verification; only an isolated dummy
    # service token, never actual credentials or a synthesized approved flag.
    token = "isolated-playlist-fixture-service-key"
    app = create_app(case.engine, token)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated.test") as client:
        response = await client.post("/api/assistant/v2/decisions/" + question["id"] + "/answers",
            headers={"X-Shengbo-Service-Token": token}, json={"question_revision": question["revision"],
                "answer": answer, "client_request_id": "simulated-acceptance-tester-" + answer})
    assert response.status_code == 200, response.json()
    return case.engine.store.get("question", question["id"])


@pytest.mark.asyncio
async def test_helper_does_not_approve_then_real_api_confirmation_binds_exact_save_and_cas(proposal_case, monkeypatch):
    case = proposal_case
    result = await prepare_playlist_proposal(case.engine, case.scope, args(case))
    assert_not_executed(case, result)
    parameters = {**result["playlist_proposal"], "expected_revision": case.prep["ref"]["revision"]}
    with pytest.raises(ContractError, match="PLAYLIST_DECISION_REQUIRED"):
        await case.engine.business_command(case.scope, "save_playlist", parameters, "unconfirmed-save")
    question = await open_question(case, result["optional_next_action"]["arguments"], monkeypatch)
    assert question["playlist_order_binding"] == result["playlist_proposal"] and question["status"] == "OPEN"
    confirmed = await simulated_api_answer(case, question, "confirmed")
    assert confirmed["verified_human_receipt"]["binding_hash"] == result["proposal_hash"]
    assert confirmed["verified_human_receipt"]["answer_event_ref"] == confirmed["answer_event_ref"]
    wrong = deepcopy(parameters)
    wrong["items"].reverse()
    # Use two unequal positions: the original three-item example is a palindrome.
    wrong["items"][0], wrong["items"][1] = wrong["items"][1], wrong["items"][0]
    before = stats(case.service)
    with pytest.raises(ContractError, match="PLAYLIST_ORDER_BINDING_MISMATCH"):
        await case.engine.business_command(case.scope, "save_playlist", wrong, "changed-after-confirm")
    assert stats(case.service) == before
    receipt = await case.engine.business_command(case.scope, "save_playlist", parameters, "confirmed-exact-save")
    assert receipt["status"] == "committed"
    current = await case.service.query("read_preparation", {"preparation_id": case.prep["ref"]["id"]})
    assert current["body"]["playlist_ref"] and current["body"]["approval_ref"] is None
    assert current["body"]["audio_manifest_ref"] is current["body"]["candidate_publication_ref"] is None
    before = stats(case.service)
    with pytest.raises(ContractError, match="REVISION_CONFLICT"):
        await case.engine.business_command(case.scope, "save_playlist", parameters, "stale-cas-save")
    assert stats(case.service) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["revise", "reject"])
async def test_real_api_revise_or_reject_does_not_authorize_save(proposal_case, monkeypatch, answer):
    case = proposal_case
    result = await prepare_playlist_proposal(case.engine, case.scope, args(case))
    question = await open_question(case, result["optional_next_action"]["arguments"], monkeypatch)
    answered = await simulated_api_answer(case, question, answer)
    assert answered["verified_human_receipt"] is None
    before = stats(case.service)
    with pytest.raises(ContractError, match="PLAYLIST_ORDER_BINDING_MISMATCH"):
        await case.engine.business_command(case.scope, "save_playlist", {**result["playlist_proposal"],
            "expected_revision": case.prep["ref"]["revision"]}, "not-confirmed-save")
    assert stats(case.service) == before
