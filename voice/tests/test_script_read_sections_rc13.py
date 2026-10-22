"""Exact script reads support explicit projections without altering artifacts."""
from copy import deepcopy
import json
from unittest.mock import patch

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio

from app.prelive import BusinessError, create_router
from app.prelive.service import wire
from shengbo_contracts.common import Principal, WorkScope
import test_prelive_module_recovery_v1 as helpers


def database_state(service):
    with service.store.transaction() as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: sorted(tuple(row) for row in connection.execute(
            'SELECT * FROM "' + name.replace('"', '""') + '"')) for name in tables}


@pytest_asyncio.fixture
async def prepared():
    h = helpers.ModuleRecoveryV1Tests(methodName="runTest")
    await h.asyncSetUp()
    prep, job = await h.setup_plan()
    await h.service.drain_jobs()
    assert (await h.job(job))["status"] == "succeeded"
    state = await h.service.query("read_preparation", {"preparation_id": prep["id"]})
    ref = state["body"]["script_refs"][0]
    full = await h.service.query("read_script", {"ref": ref})
    yield h, prep, ref, full
    await h.asyncTearDown()


@pytest.mark.asyncio
async def test_empty_sections_preserve_artifacts_with_additive_edit_coordinates(prepared):
    h, _, ref, original = prepared
    before, calls = database_state(h.service), list(h.calls)
    explicit_empty = await h.service.query("read_script", {"ref": ref, "sections": []})
    four_fields = {k: ref[k] for k in ("kind", "id", "revision", "content_hash")}
    record_ref_read = await h.service.query("read_script", {"ref": four_fields})
    assert explicit_empty == record_ref_read == original
    assert set(original) == {"ref", "body", "modules", "module_edit_targets"}
    assert "coverage" not in original
    assert set(original["modules"]) == {"opening_long", "product_intro", "selling_points", "cta"}
    assert database_state(h.service) == before and h.calls == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("sections, expected", [
    (["cta"], ["cta"]),
    (["selling_points", "cta"], ["selling_points", "cta"]),
    (["body", "cta"], ["cta"]),
    (["modules", "cta"], ["cta"]),
    (["modules", "selling_points", "cta"], ["selling_points", "cta"]),
    (["module_refs", "cta"], ["cta"]),
    (["cta", "cta"], ["cta"]),
])
async def test_explicit_module_keys_expand_only_requested_modules(prepared, sections, expected):
    h, _, ref, full = prepared
    before = database_state(h.service)
    with patch.object(h.service, "_artifact", wraps=h.service._artifact) as reads:
        result = await h.service.query("read_script", {"ref": ref, "sections": sections})
    expected_body = {"module_refs": full["body"]["module_refs"]} if "module_refs" in sections else full["body"]
    assert result["ref"] == full["ref"] and result["body"] == expected_body
    assert list(result["modules"]) == expected
    assert result["modules"] == {key: full["modules"][key] for key in expected}
    assert result["coverage"] == "selected_modules"
    assert result["selected_sections"] == list(dict.fromkeys(sections))
    assert {"body", "modules", "cta", "campaign", "transition"} <= set(result["available_sections"])
    module_ids = {r["id"] for r in full["body"]["module_refs"].values() if r}
    expanded_ids = [wire(call.args[1])["id"] for call in reads.call_args_list
                    if wire(call.args[1])["id"] in module_ids]
    assert expanded_ids == [full["body"]["module_refs"][key]["id"] for key in expected]
    assert database_state(h.service) == before


@pytest.mark.asyncio
async def test_body_only_does_not_read_or_expand_any_module(prepared):
    h, _, ref, full = prepared
    with patch.object(h.service, "_artifact", wraps=h.service._artifact) as reads:
        result = await h.service.query("read_script", {"ref": ref, "sections": ["body"]})
    assert result["ref"] == full["ref"] and result["body"] == full["body"]
    assert result["coverage"] == "metadata_only" and "modules" not in result
    assert len(reads.call_args_list) == 1
    assert wire(reads.call_args_list[0].args[1]) == ref


@pytest.mark.asyncio
async def test_module_refs_index_is_exact_metadata_without_module_body_reads(prepared):
    h, _, ref, full = prepared
    before = database_state(h.service)
    with patch.object(h.service, "_artifact", wraps=h.service._artifact) as reads:
        result = await h.service.query("read_script", {"ref": ref, "sections": ["module_refs"]})
    assert result["ref"] == full["ref"]
    assert result["body"] == {"module_refs": full["body"]["module_refs"]}
    assert result["coverage"] == "metadata_only" and "modules" not in result
    assert result["selected_sections"] == ["module_refs"]
    assert len(reads.call_args_list) == 1
    assert wire(reads.call_args_list[0].args[1]) == ref
    assert database_state(h.service) == before


@pytest.mark.asyncio
async def test_module_refs_mixed_with_all_modules_preserves_metadata_projection(prepared):
    h, _, ref, full = prepared
    result = await h.service.query("read_script", {"ref": ref, "sections": ["module_refs", "modules"]})
    assert result["body"] == {"module_refs": full["body"]["module_refs"]}
    assert result["modules"] == full["modules"]
    assert result["coverage"] == "selected_modules" and result["ref"] == full["ref"]
    metadata = await h.service.query("read_script", {"ref": ref, "sections": ["module_refs", "body"]})
    assert metadata["body"] == full["body"] and "modules" not in metadata
    assert metadata["coverage"] == "metadata_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("sections", [["modules"], ["body", "modules"]])
async def test_explicit_all_modules_alias_returns_all_with_coverage(prepared, sections):
    h, _, ref, full = prepared
    result = await h.service.query("read_script", {"ref": ref, "sections": sections})
    assert result["ref"] == full["ref"] and result["body"] == full["body"]
    assert result["modules"] == full["modules"] and result["coverage"] == "full"


@pytest.mark.asyncio
async def test_missing_optional_module_is_null_without_full_fallback(prepared):
    h, _, ref, full = prepared
    assert "opening_short" not in full["body"]["module_refs"]
    result = await h.service.query("read_script", {"ref": ref, "sections": ["opening_short", "campaign"]})
    assert result["modules"] == {"opening_short": None, "campaign": None}
    assert result["coverage"] == "selected_modules" and result["ref"] == ref


@pytest.mark.asyncio
async def test_legacy_transition_and_disabled_module_remain_exact_readable(prepared):
    h, _, _, full = prepared
    with h.service.store.transaction(write=True) as connection:
        transition = h.service.store.artifact(connection, "module",
            h.service._module_body("transition", ["历史衔接正文。 "]))
        body = deepcopy(full["body"])
        body["module_refs"].update(transition=transition, campaign=None)
        legacy = h.service.store.artifact(connection, "script_revision", body)
    before = database_state(h.service)
    result = await h.service.query("read_script", {"ref": legacy, "sections": ["transition", "campaign"]})
    assert result["ref"] == legacy
    assert result["modules"]["transition"]["module_key"] == "transition"
    assert result["modules"]["campaign"] is None
    assert set(result["modules"]) == {"transition", "campaign"}
    assert database_state(h.service) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("sections", [["sentences"], ["not-a-public-section"], ["cta", "unknown/private"], ["X" * 10000]])
async def test_unknown_sections_return_safe_known_rejection_without_writes(prepared, sections):
    h, _, ref, _ = prepared
    before = database_state(h.service)
    with pytest.raises(BusinessError) as caught:
        await h.service.query("read_script", {"ref": ref, "sections": sections})
    error = caught.value
    assert (error.code, error.status) == ("CONTEXT_SECTION_INVALID", 422)
    assert set(error.details) == {"available_sections", "ref_kind", "reader"}
    assert error.details["reader"] == "read_script" and error.details["ref_kind"] == "script_revision"
    assert set(error.details["available_sections"]) == {"body", "module_refs", "modules", "opening_long", "opening_short",
        "product_intro", "selling_points", "campaign", "cta", "transition", "spoken_text", "content_diagnostics"}
    assert sections[-1] not in json.dumps(error.payload())
    assert database_state(h.service) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["foreign_scope", "wrong_hash", "wrong_revision", "wrong_kind"])
async def test_exact_reference_and_owner_fences_precede_section_diagnostics(prepared, case):
    h, prep, ref, _ = prepared
    args = {"ref": deepcopy(ref), "sections": ["unknown/private"]}
    scope = None
    if case == "foreign_scope":
        scope = WorkScope(kind="preparation", preparation_id="unrelated-preparation")
        expected = "ARTIFACT_SCOPE_CONFLICT"
    elif case == "wrong_hash":
        args["ref"]["content_hash"] = "sha256:" + "0" * 64
        expected = "REF_HASH_MISMATCH"
    elif case == "wrong_revision":
        args["ref"]["revision"] += 1
        expected = "NOT_FOUND"
    else:
        state = await h.service.query("read_preparation", {"preparation_id": prep["id"]})
        args["ref"] = state["body"]["plan_ref"]
        expected = "WRONG_ARTIFACT_KIND"
    before = database_state(h.service)
    with pytest.raises(BusinessError) as caught:
        await h.service.query("read_script", args, principal=Principal(kind="human"), scope=scope)
    assert caught.value.code == expected
    assert "available_sections" not in caught.value.details
    assert database_state(h.service) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("sections, status, code", [
    (["cta"], 200, None), (["not-a-public-section"], 422, "CONTEXT_SECTION_INVALID"),
    ("cta", 422, "INVALID_REQUEST"), ([1], 422, "INVALID_REQUEST"),
    ([{}], 422, "INVALID_REQUEST"), (None, 422, "INVALID_REQUEST"),
])
async def test_real_http_query_projection_and_invalid_types(prepared, sections, status, code):
    h, _, ref, _ = prepared
    app = FastAPI()
    app.include_router(create_router(h.service, lambda request: Principal(kind="human")))
    before = database_state(h.service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sections.invalid", trust_env=False) as client:
        response = await client.post("/api/prelive/v2/queries", json={"query": "read_script",
            "args": {"ref": ref, "sections": sections}})
    assert response.status_code == status
    if code:
        assert response.json()["error"]["code"] == code
    else:
        assert response.json()["coverage"] == "selected_modules"
        assert list(response.json()["modules"]) == ["cta"]
    assert database_state(h.service) == before


@pytest.mark.asyncio
async def test_all_projections_leave_review_approval_artifacts_and_generation_unchanged(prepared):
    h, prep, ref, full = prepared
    state = await h.service.query("read_preparation", {"preparation_id": prep["id"]})
    await h.cmd("save_playlist", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
        "items": [{"occurrence_id": "original-first", "product_id": full["body"]["product_id"], "script_revision_ref": ref}]})
    state = await h.service.query("read_preparation", {"preparation_id": prep["id"]})
    await h.cmd("compile_playback_plan", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
        "playlist_ref": state["body"]["playlist_ref"]})
    review = await h.service.query("create_text_review", {"preparation_id": prep["id"]})
    state = await h.service.query("read_preparation", {"preparation_id": prep["id"]})
    approved = await h.cmd("approve_text_for_audio", {"preparation_id": prep["id"],
        "expected_revision": state["ref"]["revision"], "review_hash": review["review_hash"]})
    before, calls = database_state(h.service), list(h.calls)
    for sections in ([], ["body"], ["module_refs"], ["modules"], ["cta"], ["modules", "selling_points"], ["transition"]):
        await h.service.query("read_script", {"ref": ref, "sections": sections})
    assert (await h.service.query("create_text_review", {"preparation_id": prep["id"]})) == review
    approval = await h.service.query("read_text_approval", {"preparation_id": prep["id"], "ref": approved["approval_ref"]})
    assert approval["status"] == "current" and approval["approval_ref"] == approved["approval_ref"]
    assert approval["approval"]["review_hash"] == review["review_hash"]
    assert (await h.service.query("read_script", {"ref": ref})) == full
    assert database_state(h.service) == before and h.calls == calls
