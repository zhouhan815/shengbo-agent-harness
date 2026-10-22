"""Hash coordinates preserve the original sentence objects before projection."""
from copy import deepcopy

import pytest

from shengbo_contracts.common import canonical_hash
from shengbo_prelive.script_review_workset import script_review_workset
from test_script_review_workset_v8 import case


def edit_targets(record):
    return {key: {"script_ref": deepcopy(record["ref"]), "module_key": key,
        "module_ref": deepcopy(record["body"]["module_refs"][key]), "sentence_ids": [],
        "selected_text_hash": canonical_hash(module["sentences"])}
        for key, module in record["modules"].items() if module is not None}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_metadata", [False, True])
async def test_workset_provides_original_hash_with_legacy_or_new_reader(backend_metadata):
    c = case()
    if backend_metadata:
        for record in c.documents.values(): record["module_edit_targets"] = edit_targets(record)
    before = deepcopy(c.documents)
    result = await script_review_workset(c.f, c.run, c.prep, c.observation)
    assert result["review_content_complete"] and result["status"] == "COMPLETED"
    for row in result["items"]:
        assert row["module_edit_targets"] == edit_targets(c.documents[row["script_ref"]["id"]])
        assert "campaign" not in row["module_edit_targets"]
        for module in row["modules"]:
            target = row["module_edit_targets"][module["module_key"]]
            assert target["selected_text_hash"] != canonical_hash(module["sentences"])
            assert module["sentences"][0]["spoken_text_equals_text"] is True
    assert c.documents == before


@pytest.mark.asyncio
async def test_wrong_supplied_hash_never_becomes_an_edit_coordinate():
    c = case()
    record = c.documents["script-0"]
    record["module_edit_targets"] = edit_targets(record)
    record["module_edit_targets"]["selling_points"]["selected_text_hash"] = "sha256:" + "0" * 64
    result = await script_review_workset(c.f, c.run, c.prep, c.observation)
    first = result["items"][0]
    assert not result["review_content_complete"] and not first["content_complete"]
    assert "module_edit_targets" not in first and "modules" not in first


@pytest.mark.asyncio
async def test_reader_canonical_reference_metadata_is_used_without_rejecting_same_exact_identity():
    c = case()
    for record in c.documents.values():
        record["ref"]["schema_version"] = "PRELIVE-2.3"
        record["module_edit_targets"] = edit_targets(record)
    result = await script_review_workset(c.f, c.run, c.prep, c.observation)
    assert result["review_content_complete"]
    assert all(target["script_ref"]["schema_version"] == "PRELIVE-2.3"
        for row in result["items"] for target in row["module_edit_targets"].values())
