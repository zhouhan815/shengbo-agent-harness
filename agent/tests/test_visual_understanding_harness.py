"""Offline visual access, scoped judgement and unresolved key-source closure."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.material_understanding import MaterialAssessment, assess_material_readiness
from shengbo_prelive.models import RunScope
from shengbo_prelive.tools import build_tools
from test_merchant_material_readers import image_bytes
from test_priority_eight_runtime import report_branch
from test_runtime_business import env


async def image_branch(engine):
    run, _, scope = await report_branch(engine)
    ref = engine.materials.accept(run["conversation_id"], "示意.png", "image/png", image_bytes())["material_ref"]
    engine.store.update("run", run["id"], input={**run["input"], "material_refs": [ref]})
    branch = engine.store.get("branch", scope.branch_task_id)
    engine.store.update("branch", scope.branch_task_id, task={**branch["task"], "source_refs": [ref]})
    return run, ref, scope


def visual_report(ref, receipt_id="not-observed", **changes):
    review = {"review_id": "red-image", "source_ref": ref, "locator": "image:1",
              "product_candidate_ids": ["sample"], "status": "reviewed", "relevance": "relevant",
              "summary": "红色示意图，只作为外观示意，不包含可验证的价格和规格。",
              "evidence_ids": ["e-visual"], "read_receipt_ids": [receipt_id]}
    review.update(changes)
    return {"summary": "示意图分析，未从图片添加参数", "evidence": [
        {"id": "e-visual", "source_ref": ref, "locator": "image:1", "quote": "红色示意"}],
        "material_assessment": {"product_candidates": [
            {"candidate_id": "sample", "name": "示意商品", "identity_status": "ambiguous"}],
            "visual_reviews": [review]}}


@pytest.mark.asyncio
async def test_index_is_unknown_and_cannot_claim_review_without_successful_delivery(env):
    engine, _ = env
    run, ref, scope = await image_branch(engine)
    indexed = await engine.read_source(scope, ref, "overview")
    assert indexed["visual_review_observation"]["coverage"] == "unknown"
    assert indexed["visual_review_observation"]["unreported_locator_state"] == "unknown"
    with pytest.raises(ContractError) as caught:
        engine.submit_report(scope, visual_report(ref))
    assert caught.value.code == "MATERIAL_VISUAL_READ_NOT_OBSERVED"
    assert engine.store.get("branch", scope.branch_task_id).get("report") is None


@pytest.mark.asyncio
async def test_successful_tool_delivery_and_declared_review_remain_separate_and_scoped(env):
    engine, _ = env
    run, ref, scope = await image_branch(engine)
    tool = next(t for t in build_tools(engine, role="product_facts") if t.name == "read_source")
    delivered = await tool.coroutine(runtime=SimpleNamespace(context=scope), ref=ref, section="image:1")
    assert any(item["type"] == "image_url" for item in delivered)
    receipts = engine.materials.list_visual_receipts(ref, run["conversation_id"])
    assert len(receipts) == 1
    before = await engine.read_source(scope, ref, "overview")
    assert before["visual_review_observation"]["coverage"] == "unknown"
    report = visual_report(ref, receipts[0]["id"])
    saved = engine.submit_report(scope, report)
    stored = engine.store.get("branch", scope.branch_task_id)
    assert stored["report"]["material_assessment"] == report["material_assessment"]
    assert stored["report_hash"] == canonical_hash(stored["report"]) == saved["report_hash"]
    after = await engine.read_source(RunScope(run["id"], "main-review", "execute"), ref, "overview")
    observation = after["visual_review_observation"]
    assert observation["coverage"] == "declared_subset"
    assert observation["selected_scope"]["scope_kind"] == "source_candidates"
    assert observation["reviews"][0]["summary"] == report["material_assessment"]["visual_reviews"][0]["summary"]
    assert observation["semantic_support_verified"] is False
    assert observation["unreported_locator_state"] == "unknown"
    assert saved["material_readiness"]["visual_review_coverage"]["complete_source_coverage_verified"] is False


@pytest.mark.asyncio
async def test_review_cannot_borrow_other_locator_source_or_unassigned_product(env):
    engine, _ = env
    run, ref, scope = await image_branch(engine)
    receipt = engine.materials.record_visual_delivery(ref, run["conversation_id"], locator="image:1",
        run_id=run["id"], branch_task_id=scope.branch_task_id)
    wrong = visual_report(ref, receipt["id"], locator="page:1")
    with pytest.raises(ContractError) as caught:
        engine.submit_report(scope, wrong)
    assert caught.value.code == "MATERIAL_VISUAL_EVIDENCE_MISMATCH"
    wrong = visual_report(ref, receipt["id"], source_ref={**ref, "id": "foreign"})
    with pytest.raises(ContractError) as caught:
        engine.submit_report(scope, wrong)
    assert caught.value.code == "MATERIAL_VISUAL_SCOPE_DENIED"
    branch = engine.store.get("branch", scope.branch_task_id)
    engine.store.update("branch", scope.branch_task_id, task={**branch["task"], "product_ids": ["selected"]})
    wrong = visual_report(ref, receipt["id"])
    wrong["material_assessment"]["product_candidates"][0].update(product_id="another", identity_status="existing")
    with pytest.raises(ContractError) as caught:
        engine.submit_report(scope, wrong)
    assert caught.value.code == "MATERIAL_VISUAL_SCOPE_DENIED"


@pytest.mark.asyncio
async def test_history_receipt_requires_explicit_reuse_basis_and_ocr_is_valid_access(env):
    engine, _ = env
    run, ref, scope = await image_branch(engine)
    receipt = engine.materials.record_visual_delivery(ref, run["conversation_id"], locator="image:1",
        run_id="previous-run", method="ocr")
    with pytest.raises(ContractError) as caught:
        engine.submit_report(scope, visual_report(ref, receipt["id"]))
    assert caught.value.code == "MATERIAL_VISUAL_REUSE_BASIS_REQUIRED"
    saved = engine.submit_report(scope, visual_report(ref, receipt["id"],
        reuse_note="相同不可变来源与定位；沿用此前OCR读取，仅支持已有示意说明，不增加参数。"))
    assert saved["status"] == "PREPARED"
    assert saved["material_readiness"]["visual_review_coverage"]["semantic_support_verified"] is False


@pytest.mark.asyncio
async def test_unread_critical_spec_requires_gap_and_only_blocks_affected_action(env):
    engine, _ = env
    run, ref, scope = await image_branch(engine)
    report = visual_report(ref, status="unreadable", read_receipt_ids=[], relevance="unknown",
        summary="规格截图模糊，额定容量尚不可读。", critical_for_actions=["include_capacity"],
        missing_info_ids=["capacity-image"])
    with pytest.raises(ValidationError):
        MaterialAssessment.model_validate(report["material_assessment"])
    report["material_assessment"]["missing_info"] = [{"item_id": "capacity-image",
        "product_candidate_id": "sample", "field_path": "specifications.capacity",
        "level": "block_required", "why": "唯一规格截图尚不可读，不可猜测额定容量",
        "question": "请补清晰规格截图或原始参数", "blocked_actions": ["include_capacity"],
        "can_skip": False}]
    saved = engine.submit_report(scope, report)
    assert saved["material_readiness"]["blocked_actions"] == ["include_capacity"]
    read_only = assess_material_readiness(report["material_assessment"], purpose="script_generation",
                                         requested_actions=["generate_neutral_script"])
    assert read_only["ready"] is True
    assert read_only["visual_review_coverage"]["unreadable"] == 1
    wrong = deepcopy(report["material_assessment"])
    wrong["missing_info"][0]["level"] = "optional"
    wrong["missing_info"][0]["can_skip"] = True
    with pytest.raises(ValidationError, match="active required gaps"):
        MaterialAssessment.model_validate(wrong)


@pytest.mark.asyncio
async def test_unread_review_still_requires_a_real_source_locator(env):
    engine, _ = env
    _, ref, scope = await image_branch(engine)
    report = visual_report(ref, locator="image:99", status="unread", read_receipt_ids=[],
                           evidence_ids=[], summary="尚未读取此参数图，范围未知。")
    report["evidence"] = []
    with pytest.raises(ContractError) as caught:
        engine.submit_report(scope, report)
    assert caught.value.code == "MATERIAL_SECTION_NOT_FOUND"


def test_legacy_visual_omission_is_hash_stable_and_never_means_complete():
    original = MaterialAssessment(notes=["旧报告未维护视觉工作集"]).model_dump(mode="json")
    assert "visual_reviews" not in original
    digest = canonical_hash(original)
    parsed = MaterialAssessment.model_validate(original)
    assert canonical_hash(parsed.model_dump(mode="json")) == digest
    readiness = assess_material_readiness(parsed, purpose="facts_analysis")
    assert readiness["visual_review_coverage"]["state"] == "unknown"
    assert readiness["visual_review_coverage"]["complete_source_coverage_verified"] is False
