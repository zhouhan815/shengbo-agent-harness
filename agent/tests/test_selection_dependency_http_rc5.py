"""RC5 dependency failures cross the real HTTP boundary without side effects."""
from copy import deepcopy

from fastapi import FastAPI
import httpx
import pytest

from test_runtime_business import env, human_command, product
from app.prelive import create_router
from app.prelive.service import Product
from shengbo_contracts.common import ContractError, MutationContext, Principal
from shengbo_prelive.client import ShengboClient, RemoteOutcomeUnknown
from shengbo_prelive.models import AnswerRequest, IntentAssessment, MessageRequest, RunScope


@pytest.fixture
async def http_env(env):
    engine, service = env
    identity = {"principal": Principal(kind="human"), "scope": None}
    app = FastAPI()
    app.include_router(create_router(service, lambda request: identity))
    client = ShengboClient("http://isolated-voice", "fixture-agent", "fixture-controller",
                          transport=httpx.ASGITransport(app=app))
    original = engine.client
    try:
        yield engine, service, client, identity
    finally:
        engine.client = original
        await client.close()


async def prepare(engine, service):
    room = (await human_command(service, "create_room", {"name": "RC5隔离本场"}))["data"]["room_ref"]
    conversation = engine.create_conversation({"room_id": room["id"], "scope": {"kind": "catalog"}})
    upload = engine.accept_material(conversation["id"], "rc5-product.txt", "text/plain", "全新商品：RC5口红；常规价格178元。".encode())
    text = "准备整场直播，按上传资料新建商品并配置活动，先不生成语音"
    accepted = await engine.submit_message(conversation["id"], MessageRequest(text=text, intent_mode="new_task",
        material_refs=[upload["material_ref"]]))
    run = engine.store.get("run", accepted["run"]["id"])
    run = engine.store.update("run", run["id"], intent_assessment=IntentAssessment(
        objective="prepare_session", summary=text, authorization_quote=text).model_dump())
    await engine._freeze_goal(run)
    question = engine.get_run(run["id"])["questions"][0]
    await engine.answer(question["id"], AnswerRequest(question_revision=question["revision"],
        answer="confirm_new_products", client_request_id="rc5-import-confirm"))
    await engine._freeze_goal(engine.store.get("run", run["id"]))
    run = engine.store.get("run", run["id"])
    scope = RunScope(run["id"], "rc5-http", "execute")
    prep = (await service.query("read_preparation", {"preparation_id": run["scope"]["preparation_id"]}))["ref"]
    created = await engine.business_command(scope, "save_product", {"schema_hash": service.schema()["schema_hash"],
        "fields": Product(name="RC5口红", price=178).model_dump(), "patches": []}, "new-product-not-selected")
    p = created["data"]["product_ref"]
    draft = {"theme": "情人节", "tts_selection": {"provider": "system", "voice": "Huihui"},
        "commerce": {"coupon": {"mode": "coupon", "amount": 20, "minimum_spend": 180,
                                  "scope": "selected", "product_ids": [p["id"]]},
                     "product_overrides": {p["id"]: {"mode": "price", "price": 99, "exclusive": False}}}}
    return p, room, prep, draft, run, scope


@pytest.mark.asyncio
async def test_http_missing_selection_is_known_no_commit_then_explicit_select_succeeds(http_env):
    engine, service, client, identity = http_env
    p, room, prep, draft, run, scope = await prepare(engine, service)
    identity.update(principal=Principal(kind="agent", actor_id="agent-runtime", run_id=run["id"]), scope=run["scope"])
    engine.client = client
    args = {"preparation_id": prep["id"], "expected_revision": prep["revision"], "draft": draft}
    with service.store.transaction() as connection:
        artifact_count = connection.execute("SELECT COUNT(*) FROM records WHERE kind='draft'").fetchone()[0]
    with pytest.raises(ContractError) as caught:
        await engine.business_command(scope, "save_session_draft", args, "missing-selection")
    error = caught.value
    assert error.code == "PREPARATION_PRODUCTS_NOT_SELECTED"
    assert error.status == 409
    assert error.details["preparation_ref"] == prep
    assert error.details["missing_product_ids"] == [p["id"]]
    assert error.details["current_selected_product_refs"] == []
    assert error.details["commit_state"] == "not_committed"
    assert error.details["required_action"] == {"command": "update_preparation_products",
        "tool": "wf_product_materials", "action": "select", "requires_explicit_selection": True}
    assert not isinstance(error, RemoteOutcomeUnknown)
    assert engine.store.get("run", run["id"]).get("pending_unknown_operation") is None
    failed = [o for o in engine.store.list("operation") if o.get("run_id") == run["id"] and o.get("key") == "save_session_draft"]
    assert len(failed) == 1 and failed[0]["status"] == "known_failed"
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    assert current["ref"] == prep
    assert current["body"]["selected_product_refs"] == [] and current["body"]["draft_ref"] is None
    receipt = await service.query("read_operation_receipt", {"operation_id": failed[0]["id"]},
                                  principal=identity["principal"], scope=run["scope"])
    assert receipt["state"] != "committed"
    with service.store.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM records WHERE kind='draft'").fetchone()[0] == artifact_count
    selected = await engine.business_command(scope, "update_preparation_products", {
        "preparation_id": prep["id"], "expected_revision": prep["revision"], "selected_product_refs": [p]}, "explicit-select")
    args["expected_revision"] = selected["data"]["preparation_ref"]["revision"]
    result = await engine.business_command(scope, "save_session_draft", args, "save-after-select")
    assert result["status"] == "committed"
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    assert current["body"]["selected_product_refs"] == [p]
    value = await service.query("read_context", {"ref": result["data"]["draft_ref"], "sections": ["draft"]})
    assert value["body"]["draft"]["commerce"]["coupon"]["product_ids"] == [p["id"]]
    assert value["body"]["draft"]["commerce"]["product_overrides"][p["id"]]["price"] == 99


@pytest.mark.asyncio
async def test_missing_dependency_does_not_authorize_outside_goal_or_bypass_cas(http_env):
    engine, service, client, identity = http_env
    p, room, prep, draft, run, scope = await prepare(engine, service)
    outsider = await product(service, "不属于当前目标")
    identity.update(principal=Principal(kind="agent", actor_id="agent-runtime", run_id=run["id"]), scope=run["scope"])
    engine.client = client
    with pytest.raises(ContractError, match="TARGET_DENIED"):
        await engine.business_command(scope, "update_preparation_products", {"preparation_id": prep["id"],
            "expected_revision": prep["revision"], "selected_product_refs": [outsider]}, "outside-goal")
    assert (await service.query("read_preparation", {"preparation_id": prep["id"]}))["ref"] == prep
    # Authorization is checked before exposing dependency corrections.
    identity["principal"] = Principal(kind="agent", actor_id="agent-runtime", run_id="ungranted-run")
    with pytest.raises(ContractError) as denied:
        await client.command("save_session_draft", {"preparation_id": prep["id"],
            "expected_revision": prep["revision"], "draft": draft}, MutationContext(operation_id="ungranted-save"),
            run_id="ungranted-run", scope=run["scope"])
    assert denied.value.status == 403
    assert denied.value.code != "PREPARATION_PRODUCTS_NOT_SELECTED"
    identity.update(principal=Principal(kind="human"), scope=None)
    with pytest.raises(ContractError) as stale:
        await client.command("save_session_draft", {"preparation_id": prep["id"],
            "expected_revision": prep["revision"] + 1, "draft": draft}, MutationContext(operation_id="stale-save"))
    assert stale.value.code == "REVISION_CONFLICT"


@pytest.mark.asyncio
async def test_http_partial_selection_preserves_current_refs_and_other_validation_stays_distinct(http_env):
    engine, service, client, identity = http_env
    p, room, prep, draft, run, scope = await prepare(engine, service)
    outsider = await product(service, "仍未选入的其他商品")
    identity.update(principal=Principal(kind="agent", actor_id="agent-runtime", run_id=run["id"]), scope=run["scope"])
    engine.client = client
    selected = await engine.business_command(scope, "update_preparation_products", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "selected_product_refs": [p]}, "choose-only-authorized")
    current_ref = selected["data"]["preparation_ref"]
    missing_draft = deepcopy(draft)
    missing_draft["commerce"]["coupon"]["product_ids"].append(outsider["id"])
    args = {"preparation_id": prep["id"], "expected_revision": current_ref["revision"], "draft": missing_draft}
    with pytest.raises(ContractError) as missing:
        await engine.business_command(scope, "save_session_draft", args, "one-still-missing")
    assert missing.value.code == "PREPARATION_PRODUCTS_NOT_SELECTED"
    assert missing.value.details["current_selected_product_refs"] == [p]
    assert missing.value.details["missing_product_ids"] == [outsider["id"]]
    assert set(missing.value.details["requested_product_ids"]) == {p["id"], outsider["id"]}
    assert (await service.query("read_preparation", {"preparation_id": prep["id"]}))["ref"] == current_ref
    invalid_price = deepcopy(draft)
    invalid_price["commerce"]["product_overrides"][p["id"]]["price"] = 999
    args["draft"] = invalid_price
    with pytest.raises(ContractError) as other:
        await engine.business_command(scope, "save_session_draft", args, "invalid-price-not-selection")
    assert other.value.code == "VOICE_REJECTED" and other.value.status == 400
    assert other.value.details == {}
    assert engine.store.get("run", run["id"]).get("pending_unknown_operation") is None


def response(status, value):
    return httpx.Response(status, json=value, request=httpx.Request("POST", "http://isolated-voice"))


def test_client_dependency_correction_is_narrow_and_does_not_echo_arbitrary_detail():
    details = {"missing_product_ids": ["product_needed"], "current_selected_product_refs": [],
        "required_action": {"command": "start_prepared_room", "private": "fixture-private-detail"},
        "exception_text": "fixture-private-detail", "api_token": "fixture-private-detail"}
    with pytest.raises(ContractError) as caught:
        ShengboClient._result(response(409, {"error": {"code": "PREPARATION_PRODUCTS_NOT_SELECTED",
            "message": "fixture-private-detail", "details": details}}))
    assert caught.value.details["required_action"]["command"] == "update_preparation_products"
    assert caught.value.details["missing_product_ids"] == ["product_needed"]
    assert "fixture-private-detail" not in str(caught.value.as_error().model_dump())
    for status in (401, 403, 500, 503):
        with pytest.raises(ContractError) as malformed:
            ShengboClient._result(response(status, {"error": {"code": "PREPARATION_PRODUCTS_NOT_SELECTED", "details": details}}))
        assert malformed.value.code == "VOICE_RESPONSE_INVALID"
        assert malformed.value.status == status
        assert malformed.value.details == {}
    for status in (400, 500):
        with pytest.raises(ContractError) as generic:
            ShengboClient._result(response(status, {"detail": "fixture-private-detail"}))
        assert generic.value.code == "VOICE_REJECTED" and generic.value.status == status
        assert "fixture-private-detail" not in str(generic.value)

