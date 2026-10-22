"""Typed missing-base rejection is pre-write; reuse and recovery gates remain."""
from copy import deepcopy
import json
from unittest.mock import patch

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.prelive import BusinessError, create_router
from shengbo_contracts.business import ScriptPlanModuleBaseInvalidDetails
from shengbo_contracts.common import MutationContext, Principal, WorkScope
import test_prelive_module_recovery_v1 as recovery_helpers


def database_state(service):
    with service.store.transaction() as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: sorted(tuple(row) for row in connection.execute(
            'SELECT * FROM "' + name.replace('"', '""') + '"')) for name in tables}


@pytest_asyncio.fixture
async def partial(tmp_path):
    h = recovery_helpers.ModuleRecoveryV1Tests(methodName="runTest")
    await h.asyncSetUp()
    h.fail_module = "cta"
    prep, submitted = await h.setup_plan()
    await h.service.drain_jobs()
    job = await h.job(submitted)
    assert job["status"] == "failed"
    assert set(job["items"][0]["module_results"]) == {"opening_long", "product_intro", "selling_points"}
    current = await h.service.query("read_preparation", {"preparation_id": prep["id"]})
    with h.service.store.transaction() as connection:
        original = h.service.store.exact(connection, current["body"]["plan_ref"])["body"]
    yield h, prep, submitted, job, original
    await h.asyncTearDown()


async def explicit_partial_reuse(partial):
    h, prep, _, job, original = partial
    current = await h.service.query("read_preparation", {"preparation_id": prep["id"]})
    item = deepcopy(original["items"][0])
    for action in item["modules"]:
        if action["key"] in job["items"][0]["module_results"]:
            action.update(action="reuse", base_module_ref=job["items"][0]["module_results"][action["key"]]["module_ref"])
    return {"preparation_id": prep["id"], "expected_revision": current["ref"]["revision"],
            "bundle_ref": current["body"]["bundle_ref"], "items": [item]}


async def assert_rejected_without_dml(h, args, *, code, status, principal=None, scope=None):
    before, sql, calls = database_state(h.service), [], list(h.calls)
    original_connect = h.service.store.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(sql.append)
        return connection

    with patch.object(h.service.store, "connect", side_effect=traced_connect):
        with pytest.raises(BusinessError) as caught:
            await h.service.execute_command("save_script_plan", args,
                MutationContext(operation_id="rejected-rc12"), principal or Principal(kind="human"), scope=scope)
    assert (caught.value.code, caught.value.status) == (code, status)
    assert database_state(h.service) == before
    assert h.calls == calls
    assert not [query for query in sql if query.split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}]
    return caught.value


@pytest.mark.asyncio
async def test_real_partial_module_reuse_has_exact_closed_no_commit_error(partial):
    h, _, _, _, _ = partial
    args = await explicit_partial_reuse(partial)
    error = await assert_rejected_without_dml(h, args, code="BASE_MODULE_TARGET_MISMATCH", status=422)
    details = ScriptPlanModuleBaseInvalidDetails.model_validate(error.details).model_dump(mode="json")
    assert details == error.details
    assert set(details) == {"command", "commit_state", "preparation_ref", "bundle_ref", "item_index", "module_key",
        "requested_product_ref", "requested_variant_id", "requested_version_number", "requested_base_module_ref",
        "requested_base_script_ref", "reason"}
    assert details["command"] == "save_script_plan" and details["commit_state"] == "not_committed"
    assert details["preparation_ref"]["revision"] == args["expected_revision"]
    assert details["bundle_ref"] == args["bundle_ref"]
    assert details["item_index"] == 0 and details["module_key"] == "opening_long"
    assert details["requested_product_ref"] == args["items"][0]["product_ref"]
    assert details["requested_variant_id"] == args["items"][0]["variant_id"]
    assert details["requested_version_number"] == args["items"][0]["version_number"]
    for key in ("kind", "id", "revision", "content_hash"):
        assert details["requested_base_module_ref"][key] == args["items"][0]["modules"][0]["base_module_ref"][key]
    assert details["requested_base_script_ref"] is None
    assert details["reason"] == "BASE_SCRIPT_REQUIRED_FOR_EXPLICIT_MODULE_REUSE"
    assert error.payload()["retryable"] is False
    assert "job_id" not in details and "actual_owner" not in details
    assert not {"input", "msg", "ctx"}.intersection(details)


@pytest.mark.asyncio
async def test_http_422_preserves_exact_no_commit_diagnostic_and_no_side_effect(partial):
    h, _, _, _, _ = partial
    args = await explicit_partial_reuse(partial)
    app = FastAPI()
    app.include_router(create_router(h.service, lambda request: Principal(kind="human")))
    before, calls = database_state(h.service), list(h.calls)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://rc12.invalid", trust_env=False) as client:
        response = await client.post("/api/prelive/v2/commands", json={"command": "save_script_plan", "args": args,
            "mutation": {"operation_id": "http-rc12-missing-base"}})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "BASE_MODULE_TARGET_MISMATCH" and error["retryable"] is False
    ScriptPlanModuleBaseInvalidDetails.model_validate(error["details"])
    assert database_state(h.service) == before and h.calls == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["cas", "no_grant", "foreign_preparation", "wrong_module_key"])
async def test_other_cas_permission_scope_and_key_errors_are_not_reclassified(partial, case):
    h, prep, _, job, _ = partial
    args = await explicit_partial_reuse(partial)
    principal, scope = Principal(kind="human"), None
    if case == "cas":
        args["expected_revision"] -= 1
        code, status = "REVISION_CONFLICT", 409
    elif case == "no_grant":
        principal = Principal(kind="agent", actor_id="agent-no-grant", run_id="rc12-no-grant")
        scope = WorkScope(kind="preparation", preparation_id=prep["id"])
        code, status = "GRANT_REQUIRED", 403
    elif case == "foreign_preparation":
        principal = Principal(kind="agent", actor_id="agent-no-grant", run_id="rc12-no-grant")
        scope = WorkScope(kind="preparation", preparation_id="different-preparation")
        code, status = "SCOPE_CONFLICT", 403
    else:
        args["items"][0]["modules"][0]["base_module_ref"] = job["items"][0]["module_results"]["product_intro"]["module_ref"]
        code, status = "BASE_MODULE_KEY_MISMATCH", 409
    error = await assert_rejected_without_dml(h, args, code=code, status=status, principal=principal, scope=scope)
    with pytest.raises(ValidationError):
        ScriptPlanModuleBaseInvalidDetails.model_validate(error.details)


@pytest.mark.asyncio
async def test_full_base_with_nonmember_module_stays_hard_target_rejection(partial):
    h, prep, submitted, _, _ = partial
    await h.cmd("retry_failed_items", {"job_id": submitted["job_id"]})
    await h.service.drain_jobs()
    args = await explicit_partial_reuse(partial)
    current = await h.service.query("read_preparation", {"preparation_id": prep["id"]})
    args["items"][0]["base_script_ref"] = current["body"]["script_refs"][0]
    with h.service.store.transaction(write=True) as connection:
        other = h.service.store.artifact(connection, "module", h.service._module_body("opening_long", ["这是另一个模块。 "]))
    args["items"][0]["modules"][0]["base_module_ref"] = other
    error = await assert_rejected_without_dml(h, args, code="BASE_MODULE_TARGET_MISMATCH", status=409)
    assert error.details == {}


@pytest.mark.asyncio
async def test_legitimate_current_full_base_reuse_still_succeeds(partial):
    h, prep, submitted, _, _ = partial
    await h.cmd("retry_failed_items", {"job_id": submitted["job_id"]})
    await h.service.drain_jobs()
    args = await explicit_partial_reuse(partial)
    current = await h.service.query("read_preparation", {"preparation_id": prep["id"]})
    args["items"][0]["base_script_ref"] = current["body"]["script_refs"][0]
    result = await h.cmd("save_script_plan", args)
    assert result["script_plan_ref"]["kind"] == "script_plan"


@pytest.mark.asyncio
async def test_foreign_preparation_bundle_stays_denied_without_disclosing_its_state(partial):
    h, _, _, _, _ = partial
    args = await explicit_partial_reuse(partial)
    room = (await h.cmd("create_room", {"name": "无关直播间"}))["room_ref"]
    prep = (await h.cmd("create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["preparation_ref"]
    product = args["items"][0]["product_ref"]
    selected = await h.cmd("update_preparation_products", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "selected_product_refs": [product]})
    draft = await h.cmd("save_session_draft", {"preparation_id": prep["id"],
        "expected_revision": selected["preparation_ref"]["revision"], "draft": {"theme": "不应暴露的别场主题"}})
    bundle = await h.cmd("save_context_bundle", {"preparation_id": prep["id"],
        "expected_revision": draft["preparation_ref"]["revision"], "product_refs": [product], "draft_ref": draft["draft_ref"]})
    args["bundle_ref"] = bundle["context_bundle_ref"]
    error = await assert_rejected_without_dml(h, args, code="ARTIFACT_SCOPE_CONFLICT", status=403)
    assert "不应暴露的别场主题" not in json.dumps(error.payload(), ensure_ascii=False)
    with pytest.raises(ValidationError):
        ScriptPlanModuleBaseInvalidDetails.model_validate(error.details)


@pytest.mark.asyncio
@pytest.mark.parametrize("change_body", [False, True])
async def test_new_plan_only_reuses_exact_cached_generation_inputs(partial, change_body):
    h, prep, _, first, original = partial
    current = await h.service.query("read_preparation", {"preparation_id": prep["id"]})
    item = deepcopy(original["items"][0])
    for action in item["modules"]:
        assert action["action"] == "generate" and not action.get("base_module_ref")
        if action["key"] == "cta":
            action["instruction"] = "只用中性的按需购买建议，不提优惠。"
        elif change_body and action["key"] == "product_intro":
            action["instruction"] += " 本次还需要改写这个介绍。"
    saved = await h.cmd("save_script_plan", {"preparation_id": prep["id"],
        "expected_revision": current["ref"]["revision"], "bundle_ref": current["body"]["bundle_ref"], "items": [item]})
    assert saved["script_plan_ref"] != current["body"]["plan_ref"]
    created = await h.cmd("submit_script_change", {"preparation_id": prep["id"],
        "expected_revision": saved["preparation_ref"]["revision"], "plan_ref": saved["script_plan_ref"],
        "context_bundle_ref": current["body"]["bundle_ref"], "expected_generation_epoch": saved["generation_epoch"]})
    await h.service.drain_jobs()
    final = await h.job(created)
    assert final["status"] == "succeeded"
    assert h.calls == ["opening_long", "product_intro", "selling_points", "cta"] + (["product_intro"] if change_body else []) + ["cta"]
    # Equal generated output can have the same content ref even after a paid
    # call. Actual adapter call counts, not ref equality alone, prove cache use.
    attempted = [a["purpose"] for a in final["items"][0]["provider_attempts"]]
    assert attempted == (["copywriting.product_intro"] if change_body else []) + ["copywriting.cta"]
    for key in ["opening_long", "selling_points"] + ([] if change_body else ["product_intro"]):
        assert final["items"][0]["module_results"][key]["module_ref"] == first["items"][0]["module_results"][key]["module_ref"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["extra", "wrong_kind", "bad_hash", "bad_schema", "bool_revision", "bool_index", "string_version", "wrong_reason", "base_present", "missing_field"])
async def test_diagnostic_dto_rejects_forged_or_malformed_shapes(partial, case):
    h, _, _, _, _ = partial
    args = await explicit_partial_reuse(partial)
    with pytest.raises(BusinessError) as caught:
        await h.cmd("save_script_plan", args)
    details = deepcopy(caught.value.details)
    if case == "extra": details["actual_owner"] = "not-an-authority"
    elif case == "wrong_kind": details["requested_product_ref"]["kind"] = "module"
    elif case == "bad_hash": details["requested_product_ref"]["content_hash"] = "not-a-hash"
    elif case == "bad_schema": details["bundle_ref"]["schema_version"] = "wrong-schema"
    elif case == "bool_revision": details["preparation_ref"]["revision"] = True
    elif case == "bool_index": details["item_index"] = True
    elif case == "string_version": details["requested_version_number"] = "1"
    elif case == "wrong_reason": details["reason"] = "AUTO_REUSE_ALLOWED"
    elif case == "base_present": details["requested_base_script_ref"] = details["bundle_ref"]
    else: details.pop("requested_base_script_ref")
    with pytest.raises(ValidationError):
        ScriptPlanModuleBaseInvalidDetails.model_validate(details)
