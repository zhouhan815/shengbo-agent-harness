"""Read-only edit coordinates match the existing patch precondition exactly."""
from copy import deepcopy

import pytest

from app.prelive import BusinessError
from app.prelive.storage import digest
from shengbo_contracts.common import MutationContext, Principal, WorkScope
from test_script_read_sections_rc13 import database_state, prepared


@pytest.mark.asyncio
@pytest.mark.parametrize("sections,expected", [([], ["opening_long", "product_intro", "selling_points", "cta"]),
    (["cta"], ["cta"]), (["selling_points", "cta"], ["selling_points", "cta"]),
    (["opening_short", "campaign"], []), (["body"], []), (["module_refs"], [])])
async def test_edit_targets_only_describe_actually_returned_nonempty_modules(prepared, sections, expected):
    h, _, ref, full = prepared
    before = database_state(h.service)
    view = await h.service.query("read_script", {"ref": ref, "sections": sections})
    assert set(view.get("module_edit_targets", {})) == set(expected)
    if sections:
        assert list(view.get("module_edit_targets", {})) == expected
    for key, target in view.get("module_edit_targets", {}).items():
        assert target == {"script_ref": ref, "module_key": key,
            "module_ref": full["body"]["module_refs"][key], "sentence_ids": [],
            "selected_text_hash": digest(full["modules"][key]["sentences"])}
        assert target["selected_text_hash"] != target["module_ref"]["content_hash"]
    assert view["ref"] == ref
    assert digest(full["body"]) == ref["content_hash"]
    assert database_state(h.service) == before


@pytest.mark.asyncio
async def test_reader_hash_is_usable_by_real_patch_and_stale_script_remains_rejected(prepared):
    h, prep, ref, original = prepared
    view = await h.service.query("read_script", {"ref": ref, "sections": ["cta"]})
    target = view["module_edit_targets"]["cta"]
    state = await h.service.query("read_preparation", {"preparation_id": prep["id"]})
    args = {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
        **{key: deepcopy(target[key]) for key in ("script_ref", "module_key", "selected_text_hash", "sentence_ids")},
        "sentences": ["想找适合自己练习的器材，可以结合实际需求选择。"]}
    result = await h.cmd("patch_script", args)
    saved = await h.service.query("read_script", {"ref": result["script_ref"]})
    assert saved["modules"]["cta"]["sentences"][0]["text"] == args["sentences"][0]
    assert saved["body"]["version_number"] == original["body"]["version_number"]
    assert saved["body"]["variant_id"] == original["body"]["variant_id"]
    for key in ("opening_long", "product_intro", "selling_points"):
        assert saved["body"]["module_refs"][key] == original["body"]["module_refs"][key]
    args["expected_revision"] = result["preparation_ref"]["revision"]
    with pytest.raises(BusinessError) as failure:
        await h.cmd("patch_script", args)
    assert failure.value.code == "SCRIPT_NOT_IN_PREPARATION"


@pytest.mark.asyncio
async def test_read_coordinates_do_not_grant_agent_patch_permission(prepared):
    h, prep, ref, _ = prepared
    scope = WorkScope(kind="preparation", preparation_id=prep["id"])
    principal = Principal(kind="agent", run_id="read-only-run", actor_id="reader")
    view = await h.service.query("read_script", {"ref": ref, "sections": ["cta"]}, principal=principal, scope=scope)
    target = view["module_edit_targets"]["cta"]
    state = await h.service.query("read_preparation", {"preparation_id": prep["id"]})
    args = {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
        **{key: deepcopy(target[key]) for key in ("script_ref", "module_key", "selected_text_hash", "sentence_ids")},
        "sentences": ["没有写权限时不能提交这句话。"]}
    with pytest.raises(BusinessError) as failure:
        await h.service.execute_command("patch_script", args,
            MutationContext(operation_id="not-a-write-grant"), principal, scope=scope)
    assert failure.value.status == 403
