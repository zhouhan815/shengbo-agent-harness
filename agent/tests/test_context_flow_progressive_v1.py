"""F03 delivery views preserve formal inputs, readback ACL and optional routing."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import RunScope
from shengbo_prelive.workflows import ContextFlowInput, Flow, _context_inputs_view, run_workflow
from test_optional_workflows import authorize
from test_runtime_business import env, new_run, prepared_text


def ref(kind, identity, revision=1):
    return {"kind": kind, "id": identity, "revision": revision, "content_hash": canonical_hash([kind, identity, revision])}


def exact_inputs():
    product = ref("product", "cosmetic")
    source = ref("source_material", "source")
    report = {"summary": "已验收的场景建议", "fields": {"price": 129, "optional": None,
        "segments": [{"customer": "敏感肌", "status": "尚需验证"}]},
        "insights": [{"statement": "先讲补水", "evidence": "嵌套证据仍保留"}],
        "hypotheses": [{"needs_test": True}], "conflicts": [{"left": 129, "right": 119}],
        "missing_fields": ["未知库存"], "asset_suggestions": [{"purpose": "上脸展示", "status": "missing"}],
        "evidence": [{"text": "完整来源证据" * 2000, "source_ref": source}],
        "dependencies": [source], "source_refs": [source], "product_ids": []}
    research = []
    for i in range(2):
        bound = deepcopy(report)
        if i:
            bound["product_ids"] = [product["id"]]
            bound["dependencies"] = [source, product]
        research.append({"ref": ref("analysis", "research-" + str(i)), "body": {"accepted": True,
            "role": "audience_scenes", "product_refs": [product] if i else [], "readset": [product] if i else [],
            "report": bound, "lineage": {"full_original": deepcopy(report)},
            "source_ref_replacements": [{"old": source, "new": product}]}})
    return {"context_inputs": {"product_refs": [product], "analysis_refs": [r["ref"] for r in research]},
        "products": [{"ref": product, "body": {"name": "保湿霜", "price": 129, "knowledge": "完整知识", "unknown": None}}],
        "draft": {"ref": ref("draft", "valentine"), "body": {"draft": {"theme": "情人节", "coupon": "满100减10",
            "tts_selection": {"provider": "system", "voice": "普通话"}}}},
        "research": research, "recaps": [], "prior_script_refs": [], "note": "完整原文"}


def test_default_is_index_and_explicit_modes_are_valid():
    assert ContextFlowInput().detail == "index"
    for detail in ("index", "plan", "full"):
        assert ContextFlowInput(detail=detail).detail == detail
    with pytest.raises(ValidationError):
        ContextFlowInput(detail="summary")


def test_index_has_complete_product_draft_refs_and_exact_section_paths():
    original = exact_inputs()
    before = deepcopy(original)
    full_ref = ref("runtime_context", "archive")
    view = _context_inputs_view(original, "index", full_ref)
    assert original == before
    for key in ("products", "draft", "context_inputs", "recaps", "prior_script_refs"):
        assert view[key] == original[key]
    assert all(r in Flow._refs(view) for r in Flow._refs(original))
    for i, record in enumerate(view["research"]):
        raw = original["research"][i]
        assert record["ref"] == raw["ref"]
        assert record["body"]["report"]["summary"] == raw["body"]["report"]["summary"]
        assert record["body"]["report"]["dependencies"] == raw["body"]["report"]["dependencies"]
        assert record["body"]["product_refs"] == raw["body"]["product_refs"]
        assert record["available_report_sections"] == list(raw["body"]["report"])
        assert record["full_ref"] == full_ref and record["report_path"] == f"/inputs/research/{i}/body/report"
        assert "lineage" not in record["body"] and "evidence" not in record["body"]["report"]


def test_plan_shares_only_exact_equal_content_and_preserves_every_binding():
    original = exact_inputs()
    view = _context_inputs_view(original, "plan", ref("runtime_context", "archive"))
    first, second = view["research"]
    expected = original["research"][0]["body"]["report"]
    for key in expected.keys() - {"evidence"}:
        assert first["body"]["report"][key] == expected[key]
    assert second["body"]["report"]["shared_content"]["research_index"] == 0
    assert second["body"]["report"]["dependencies"] == original["research"][1]["body"]["report"]["dependencies"]
    assert second["body"]["report"]["product_ids"] == ["cosmetic"]
    assert second["ref"] != first["ref"]
    changed = deepcopy(original)
    changed["research"][1]["body"]["report"]["fields"]["price"] = 119
    different = _context_inputs_view(changed, "plan", ref("runtime_context", "archive"))
    assert "shared_content" not in different["research"][1]["body"]["report"]
    assert different["research"][1]["body"]["report"]["fields"]["price"] == 119


@pytest.mark.asyncio
async def test_full_snapshot_readback_is_exact_hash_checked_and_scoped(env):
    engine, _ = env
    run = await new_run(engine, "只读核对完整上下文")
    scope = RunScope(run["id"], "f03-index", "execute")
    original = exact_inputs()
    full_ref = engine._snapshot_context(run, {"kind": "workflow_context_inputs", "inputs": original, "branch": {"id": None}})
    view = _context_inputs_view(original, "index", full_ref)
    archive = engine.run_history(scope, {"ref": view["full_ref"], "full": True})
    assert archive["document"]["inputs"] == original
    record = view["research"][1]
    fields = engine.run_history(scope, {"ref": record["full_ref"], "path": record["report_path"] + "/fields"})
    assert fields["document"] == original["research"][1]["body"]["report"]["fields"]
    other = await new_run(engine, "另一会话")
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        engine.run_history(RunScope(other["id"], "other", "execute"), {"ref": full_ref, "full": True})
    with pytest.raises(ContractError, match="BRANCH_SCOPE_DENIED"):
        engine.run_history(RunScope(run["id"], "child", "execute", "audience_scenes", "unrelated-child"), {"ref": full_ref})
    altered = {**full_ref, "content_hash": canonical_hash("wrong")}
    with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
        engine.run_history(scope, {"ref": altered})


@pytest.mark.asyncio
async def test_full_behavior_and_save_bundle_do_not_depend_on_display_mode(env):
    engine, service = env
    room, prep, _ = await prepared_text(service)
    run = await new_run(engine, "汇合完整上下文", room_id=room["id"], preparation_id=prep["id"])
    await authorize(engine, run, ["save_context_bundle"])
    scope = RunScope(run["id"], "f03-full-save", "execute")
    before = await service.query("read_preparation", {"preparation_id": prep["id"]})
    full = await run_workflow(engine, scope, "wf_context_bundle", {"detail": "full"})
    indexed = await run_workflow(engine, scope, "wf_context_bundle", {})
    assert "full_ref" not in full["result"] and "detail" not in full["result"]
    assert indexed["result"]["products"] == full["result"]["products"]
    assert indexed["result"]["draft"] == full["result"]["draft"]
    readback = engine.run_history(scope, {"ref": indexed["result"]["full_ref"], "full": True})
    assert readback["document"]["inputs"] == full["result"]
    saved = await run_workflow(engine, scope, "wf_context_bundle", {"action": "save", "detail": "index"})
    assert saved["status"] == "COMPLETED"
    after = await service.query("read_preparation", {"preparation_id": prep["id"]})
    assert after["body"]["selected_product_refs"] == before["body"]["selected_product_refs"]
    bundle = await service.query("read_context", {"ref": after["body"]["bundle_ref"]})
    assert bundle["body"]["product_refs"] == before["body"]["selected_product_refs"]
