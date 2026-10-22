import json
import pytest

from test_script_read_sections_rc13 import prepared, database_state


@pytest.mark.asyncio
async def test_spoken_projection_is_exact_read_only_and_preserves_edit_hashes(prepared):
    h, _, ref, full = prepared
    before = database_state(h.service)
    result = await h.service.query("read_script", {"ref": ref, "sections": ["spoken_text", "content_diagnostics"]})
    assert result["ref"] == full["ref"]
    assert result["projection"] == "exact_spoken_text_v1"
    assert result["coverage"] == "full"
    assert "modules" not in result and "sentences" not in result["body"]
    for key, module in full["modules"].items():
        expected = "".join(s.get("spoken_text") or s["text"] for s in module["sentences"]) if module else ""
        assert result["module_texts"][key]["spoken_text"] == expected
        assert result["module_texts"][key]["source_module_ref"] == full["body"]["module_refs"][key]
    assert result["module_edit_targets"] == full["module_edit_targets"]
    assert result["module_order"] == ["opening_long", "product_intro", "selling_points", "cta"]
    assert result["content_diagnostics"] == full["body"]["content_diagnostics"]
    assert database_state(h.service) == before


@pytest.mark.asyncio
async def test_spoken_partial_and_diagnostics_do_not_claim_full_read(prepared):
    h, _, ref, full = prepared
    result = await h.service.query("read_script", {"ref": ref, "sections": ["spoken_text", "cta"]})
    assert result["coverage"] == "selected_modules"
    assert set(result["module_texts"]) == {"cta"}
    assert set(result["module_edit_targets"]) == {"cta"}
    diag = await h.service.query("read_script", {"ref": ref, "sections": ["content_diagnostics"]})
    assert diag["coverage"] == "diagnostics_only" and "module_texts" not in diag
    assert diag["content_diagnostics"] == full["body"]["content_diagnostics"]


@pytest.mark.asyncio
async def test_large_text_payload_is_smaller_without_any_speech_loss(prepared):
    h, _, ref, full = prepared
    view = await h.service.query("read_script", {"ref": ref, "sections": ["spoken_text"]})
    assert len(json.dumps(view, ensure_ascii=False)) < len(json.dumps(full, ensure_ascii=False))
