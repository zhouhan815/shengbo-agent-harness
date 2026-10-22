"""Real Runtime -> temporary HTTP -> exact read -> original patch, no providers."""
from copy import deepcopy

from fastapi import FastAPI
import httpx
import pytest
from pydantic import ValidationError

from shengbo_contracts.business import QUERY_ARGS
from shengbo_contracts.common import ContractError, Principal, canonical_hash
from shengbo_prelive.client import ShengboClient
from shengbo_prelive.tools import build_tools
from test_patch_scope_collapse_recovery_v1 import long_case
from test_runtime_business import env
from app.prelive import create_router


@pytest.fixture
async def selection_case(env):
    engine, service = env
    run, scope, script, args = await long_case(engine, service, selected_index=7)
    principal = Principal(kind="agent", actor_id="fixture-selection-reader", run_id=run["id"])
    app = FastAPI()
    app.include_router(create_router(service, lambda request: {"principal": principal, "scope": run["scope"]}))
    client = ShengboClient("http://isolated-sentence-selection", "fake-agent", "fake-controller",
        transport=httpx.ASGITransport(app=app))
    original = engine.client
    engine.client = client
    try:
        yield engine, service, run, scope, script, args
    finally:
        engine.client = original
        await client.close()


def selected_args(script, rows, module="selling_points"):
    return {"ref": script["ref"], "module_key": module, "sentence_ids": [row["id"] for row in rows]}


def test_additive_selection_schema_keeps_legacy_wire_and_original_tools():
    ref = {"kind": "script_revision", "id": "s", "revision": 1, "content_hash": canonical_hash("s")}
    parsed = QUERY_ARGS["read_script"].model_validate({"ref": ref}).model_dump(mode="json", exclude_none=True)
    assert parsed == {"ref": ref, "sections": []}
    schema = QUERY_ARGS["read_script"].model_json_schema()
    assert {"module_key", "sentence_ids"} <= set(schema["properties"])
    with pytest.raises(ValidationError):
        QUERY_ARGS["read_context"].model_validate({"ref": ref, "sentence_ids": ["x"]})


@pytest.mark.asyncio
async def test_runtime_http_selection_then_original_patch_preserves_unselected_text(selection_case):
    engine, service, run, scope, before, args = selection_case
    rows = before["modules"]["selling_points"]["sentences"]
    old_operations = len(engine.store.list("operation"))
    default = await engine.business_query(scope, "read_script", {"ref": before["ref"]})
    assert default == before and "selection" not in default
    result = await engine.business_query(scope, "read_script", selected_args(before, rows[7:8]))
    target = result["selection"]
    assert target["sentences"] == rows[7:8]
    assert target["selected_text_hash"] == canonical_hash(rows[7:8])
    assert result["ref"] == before["ref"] and result["coverage"] == "selected_sentences"
    assert len(engine.store.list("operation")) == old_operations
    doc = next(row for row in engine.store.list("document_read") if row.get("query") == "read_script"
        and row.get("coverage") == "section_view")
    assert doc["approval_implied"] is False
    request = {**args, **{key: deepcopy(target[key]) for key in (
        "script_ref", "module_key", "sentence_ids", "selected_text_hash")}, "sentences": ["TESTONLY这句话明确修订。"]}
    receipt = await engine.business_command(scope, "patch_script", request, "selection-reader-exact-patch")
    after = await service.query("read_script", {"ref": receipt["data"]["script_ref"]})
    changed = after["modules"]["selling_points"]["sentences"]
    assert changed[:7] == rows[:7] and changed[8:] == rows[8:]
    assert changed[7]["text"] == request["sentences"][0]
    assert {key: ref for key, ref in after["body"]["module_refs"].items() if key != "selling_points"} == {
        key: ref for key, ref in before["body"]["module_refs"].items() if key != "selling_points"}
    with pytest.raises(ContractError, match="SCRIPT_NOT_IN_PREPARATION"):
        await engine.business_query(scope, "read_script", selected_args(before, rows[7:8]))
    assert not engine.store.list("model_attempt")


@pytest.mark.asyncio
async def test_contiguous_multiple_sentence_hash_matches_patch_but_cannot_expand_ui_grant(selection_case):
    engine, service, run, scope, before, args = selection_case
    rows = before["modules"]["selling_points"]["sentences"]
    result = await engine.business_query(scope, "read_script", selected_args(before, rows[7:9]))
    target = result["selection"]
    assert target["selected_text_hash"] == canonical_hash(rows[7:9])
    # Read access is not authority to expand the user's exact one-sentence selection.
    with pytest.raises(ContractError) as failure:
        await engine.business_command(scope, "patch_script", {**args, **{key: target[key] for key in (
            "script_ref", "module_key", "sentence_ids", "selected_text_hash")}}, "deny-selection-expansion")
    assert failure.value.status == 403
    assert (await service.query("read_script", {"ref": before["ref"]})) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["duplicate", "gap", "unknown", "wrong_module", "wrong_hash", "missing_pair"])
async def test_selection_reader_rejects_invalid_coordinates_before_any_write(selection_case, invalid):
    engine, service, run, scope, before, _ = selection_case
    rows = before["modules"]["selling_points"]["sentences"]
    request = selected_args(before, rows[7:8])
    if invalid == "duplicate": request["sentence_ids"] *= 2
    if invalid == "gap": request["sentence_ids"] = [rows[7]["id"], rows[9]["id"]]
    if invalid == "unknown": request["sentence_ids"] = ["not-a-real-sentence"]
    if invalid == "wrong_module": request["module_key"] = "cta"
    if invalid == "wrong_hash": request["ref"] = {**request["ref"], "content_hash": "sha256:" + "0" * 64}
    if invalid == "missing_pair": request.pop("module_key")
    operations = len(engine.store.list("operation"))
    with pytest.raises(ContractError):
        await engine.business_query(scope, "read_script", request)
    assert len(engine.store.list("operation")) == operations
    assert (await service.query("read_script", {"ref": before["ref"]})) == before


@pytest.mark.asyncio
async def test_read_selection_no_write_grant_does_not_authorize_original_patch(selection_case):
    engine, service, run, scope, before, args = selection_case
    rows = before["modules"]["selling_points"]["sentences"]
    # Direct backend call without the Runtime's write grant remains rejected.
    target = (await engine.business_query(scope, "read_script", selected_args(before, rows[7:8])))["selection"]
    from shengbo_contracts.common import MutationContext
    with pytest.raises(ContractError) as failure:
        await engine.client.command("patch_script", {**args, **{key: target[key] for key in (
            "script_ref", "module_key", "sentence_ids", "selected_text_hash")}},
            MutationContext(operation_id="reader-does-not-authorize-write"), scope=run["scope"], run_id=run["id"])
    assert failure.value.status == 403
