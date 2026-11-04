"""An accepted citation does not prove everything asserted in the selling point."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage

from shengbo_contracts.common import ContractError
from shengbo_prelive import selling_point_extract as extraction
from shengbo_prelive.models import RunScope
from shengbo_prelive.providers.chatgpt import ModelProviderError


CATALOG = {
    "lamp-material": {"label": "灯罩材质", "fact": "磨砂 PC"},
    "lamp-duration": {"label": "低亮续航", "fact": "充满电后使用低亮模式，参考时长约 8 小时"},
}


def _point(text, evidence):
    return {
        "selling_point": text,
        "audience_need": "选购营地照明",
        "use_scene": "露营",
        "evidence": [{"evidence_id": evidence}],
        "limitations": "",
    }


@pytest.mark.asyncio
async def test_semantic_review_can_remove_unsupported_claim_and_keep_conditional_duration(monkeypatch):
    draft = [
        _point("磨砂灯罩不刺眼：磨砂 PC 灯罩让光线更均匀。", "lamp-material"),
        _point("低亮续航：充满电后使用低亮模式，参考时长约 8 小时。", "lamp-duration"),
    ]

    async def provider(*_, inputs, **__):
        assert inputs["candidates"][0]["facts"] == [CATALOG["lamp-material"]]
        return {"items": [
            {"index": 0, "verdict": "drop"},
            {"index": 1, "verdict": "keep"},
        ]}

    monkeypatch.setattr(extraction, "_structured_request", provider)
    verified = await extraction.review_selling_points(None, None, product={"name": "营地灯"},
        evidence_catalog=CATALOG, research={}, items=draft)
    assert verified == draft[1:]


@pytest.mark.asyncio
async def test_reviewer_cannot_add_unsupported_duration(monkeypatch):
    async def provider(*_, **__):
        return {"items": [
            {"index": 0, "verdict": "rewrite", "unsupported_claims": ["约 8 小时"],
             "selling_point": "续航更长：充满后可用 16 小时。"},
        ]}

    monkeypatch.setattr(extraction, "_structured_request", provider)
    with pytest.raises(ContractError) as error:
        await extraction.review_selling_points(None, None, product={"name": "营地灯"},
            evidence_catalog=CATALOG, research={}, items=[_point("低亮续航：约 8 小时。", "lamp-duration")])
    assert error.value.code == "SELLING_POINTS_UNSUPPORTED"


@pytest.mark.asyncio
async def test_echoed_keep_is_safe_but_echoed_rewrite_is_not(monkeypatch):
    draft = [
        _point("磨砂灯罩：不刺眼。", "lamp-material"),
        _point("低亮续航：充满电后，低亮模式参考时长约 8 小时。", "lamp-duration"),
    ]

    async def provider(*_, **__):
        return {"items": [
            {"index": 0, "verdict": "rewrite", "unsupported_claims": ["不刺眼"],
             "selling_point": draft[0]["selling_point"]},
            {"index": 1, "verdict": "keep", "selling_point": draft[1]["selling_point"]},
        ]}

    monkeypatch.setattr(extraction, "_structured_request", provider)
    verified = await extraction.review_selling_points(None, None, product={"name": "营地灯"},
        evidence_catalog=CATALOG, research={}, items=draft)
    assert verified == draft[1:]


@pytest.mark.asyncio
async def test_body_only_rewrite_keeps_unflagged_title_without_a_second_review(monkeypatch):
    draft = [_point("低亮续航：充满电后低亮模式参考约 8 小时，整晚无忧。", "lamp-duration")]
    calls = []

    async def provider(*_, inputs, **__):
        point, = inputs["candidates"]
        calls.append(point["point_id"])
        return {"items": [{"point_id": point["point_id"], "verdict": "rewrite",
            "unsupported_claims": ["整晚无忧"], "selling_point": "充满电后，低亮模式参考时长约 8 小时。"}]}

    monkeypatch.setattr(extraction, "_structured_request", provider)
    verified, = await extraction.review_selling_points(None, None, product={"name": "营地灯"},
        evidence_catalog=CATALOG, research={}, items=draft)
    assert verified["selling_point"] == "低亮续航：充满电后，低亮模式参考时长约 8 小时。"
    assert verified["evidence"] == draft[0]["evidence"]
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("style", [
    {"title": "随便装进包里", "copy": [], "fact_verified": True},
    {"title": "整晚不用充电", "copy": "能用 16 小时。", "fact_verified": False},
    {"title": "长续航", "copy": "能用 16 小时。", "fact_verified": True},
])
async def test_malformed_or_unsupported_style_keeps_independently_passed_original(monkeypatch, style):
    original = _point("准备开练：充满电后使用低亮模式参考约 8 小时，夜里练习时可以照明。", "lamp-duration")
    calls = []

    async def provider(*_, inputs, **__):
        calls.append(inputs["candidates"])
        return {"items": [{"point_id": inputs["candidates"][0]["point_id"], "fact_verdict": "pass",
                           "style_rewrite": style}]}

    monkeypatch.setattr(extraction, "_structured_request", provider)
    verified = await extraction.review_selling_points(None, None, product={"name": "营地灯"},
        evidence_catalog=CATALOG, research={}, items=[original], with_diagnostics=True)

    assert verified["items"] == [original] and len(calls) == 1
    decision, = verified["decisions"]
    assert (decision["outcome"], decision["reason"], decision["rechecked"]) == ("saved", "style_unusable", False)


@pytest.mark.asyncio
async def test_verified_optional_body_polish_can_reuse_fact_approved_title(monkeypatch):
    original = _point("低亮续航：充满电后低亮模式参考时长约 8 小时。", "lamp-duration")
    provider = AsyncMock(return_value={"items": [{"index": 0, "fact_verdict": "pass", "style_rewrite": {
        "copy": "夜里收拾装备时可以用低亮模式。充满电后，该模式参考时长约 8 小时。", "fact_verified": True}}]})
    monkeypatch.setattr(extraction, "_structured_request", provider)
    report = await extraction.review_selling_points(None, None, product={"name": "营地灯"},
        evidence_catalog=CATALOG, research={}, items=[original], with_diagnostics=True)

    assert report["items"][0]["selling_point"] == "低亮续航：夜里收拾装备时可以用低亮模式。充满电后，该模式参考时长约 8 小时。"
    assert report["decisions"][0]["reason"] == "style_applied"
    assert provider.await_count == 1


@pytest.mark.asyncio
async def test_fact_fix_that_cannot_be_verified_never_falls_back_to_original(monkeypatch):
    original = _point("防眩光：磨砂 PC 灯罩完全不刺眼。", "lamp-material")
    monkeypatch.setattr(extraction, "_structured_request", AsyncMock(return_value={"items": [
        {"index": 0, "fact_verdict": "fix", "unsupported_claims": ["防眩光", "完全不刺眼"],
         "copy": "露营准备照明时可选磨砂 PC 灯罩。", "replacement_fact_verified": False}]}))

    with pytest.raises(ContractError) as error:
        await extraction.review_selling_points(None, None, product={"name": "营地灯"},
            evidence_catalog=CATALOG, research={}, items=[original], with_diagnostics=True)
    assert error.value.code == "SELLING_POINTS_UNSUPPORTED"
    assert error.value.details["issues"][0]["reason"] == "invalid_rewrite"


@pytest.mark.asyncio
async def test_targeted_retry_cannot_reapprove_a_previously_identified_unsupported_claim(monkeypatch):
    draft = [_point("防眩光：磨砂 PC 灯罩完全不刺眼。", "lamp-material"),
             _point("低亮续航：充满后低亮模式参考约 8 小时。", "lamp-duration")]
    monkeypatch.setattr(extraction, "_structured_request", AsyncMock(return_value={"items": [
        {"index": 0, "fact_verdict": "fix", "unsupported_claims": ["完全不刺眼"],
         "replacement_fact_verified": False}, {"index": 1, "fact_verdict": "pass"}]}))
    targeted = AsyncMock(return_value=[extraction._ReviewItem(index=0, fact_verdict="pass")])
    monkeypatch.setattr(extraction, "_review_subset", targeted)
    scope = SimpleNamespace(run_id="test")
    report = await extraction.review_selling_points(SimpleNamespace(_emit=Mock()), scope,
        product={"name": "营地灯"}, evidence_catalog=CATALOG, research={}, items=draft, with_diagnostics=True)

    assert report["items"] == draft[1:]
    assert report["decisions"][0]["reason"] == "prior_fact_conflict"
    assert targeted.await_args.args[3] == {0: {"retry_reason": "invalid_rewrite",
                                                "previous_unsupported_claims": ["完全不刺眼"]}}


@pytest.mark.asyncio
async def test_factual_pass_can_repair_local_title_punctuation_without_changing_body(monkeypatch):
    original = _point("马上开练！：在已有场地和球具条件下，可以自己安排练习。", "lamp-material")
    monkeypatch.setattr(extraction, "_structured_request", AsyncMock(return_value={"items": [
        {"index": 0, "fact_verdict": "pass"}]}))
    report = await extraction.review_selling_points(None, None, product={"name": "单人网球训练器"},
        evidence_catalog={"lamp-material": {"fact": "支持单人练习"}}, research={}, items=[original],
        with_diagnostics=True)

    assert report["items"][0]["selling_point"] == "马上开练：在已有场地和球具条件下，可以自己安排练习。"
    assert report["decisions"][0]["reason"] == "title_repaired"


@pytest.mark.asyncio
async def test_point_id_cannot_be_used_with_another_candidates_index(monkeypatch):
    draft = [_point("灯罩材质：采用磨砂 PC。", "lamp-material"),
             _point("低亮续航：充满电后，低亮模式参考时长约 8 小时。", "lamp-duration")]

    async def provider(*_, inputs, **__):
        first, second = inputs["candidates"]
        return {"items": [{"point_id": first["point_id"], "index": 1, "verdict": "keep"},
                          {"point_id": second["point_id"], "index": 0, "verdict": "drop"}]}

    monkeypatch.setattr(extraction, "_structured_request", provider)
    with pytest.raises(extraction.SellingPointsOutputError, match="SELLING_POINTS_OUTPUT_INVALID"):
        await extraction.review_selling_points(None, None, product={"name": "营地灯"},
            evidence_catalog=CATALOG, research={}, items=draft)


@pytest.mark.asyncio
async def test_program_formats_title_and_body_and_uses_research_only_as_customer_context(monkeypatch):
    async def provider(*_, inputs, **__):
        assert inputs["customer_context"] == {
            "opportunities": [{"audience": "露营者", "concern": "夜间换电"}],
        }
        return {"items": [{
            "title": "低亮续航",
            "copy": "充满电后低亮模式参考时长约 8 小时。",
            "audience_need": "夜间照明",
            "use_scene": "露营",
            "evidence": [{"evidence_id": "lamp-duration"}],
        }]}

    monkeypatch.setattr(extraction, "_structured_request", provider)
    verified = await extraction.extract_selling_points(None, None, product={"name": "营地灯"},
        evidence_catalog=CATALOG, research={"fields": {"benefit_priorities": [
            {"audience": "露营者", "concern": "夜间换电", "benefit": "整晚无忧"},
        ]}})
    assert verified[0]["selling_point"] == "低亮续航：充满电后低亮模式参考时长约 8 小时。"


@pytest.mark.asyncio
async def test_same_cited_quantity_is_saved_only_once_even_if_review_keeps_both(monkeypatch):
    draft = [
        _point("低亮续航：充满电后，低亮模式参考时长约 8 小时。", "lamp-duration"),
        _point("户外照明：低亮模式下可用约 8 小时。", "lamp-duration"),
    ]

    async def provider(*_, **__):
        return {"items": [{"index": 0, "verdict": "keep"}, {"index": 1, "verdict": "keep"}]}

    monkeypatch.setattr(extraction, "_structured_request", provider)
    verified = await extraction.review_selling_points(None, None, product={"name": "营地灯"},
        evidence_catalog=CATALOG, research={}, items=draft)
    assert verified == draft[0:1]


@pytest.mark.asyncio
async def test_recovery_feedback_reaches_provider_with_original_product_evidence():
    stage = AsyncMock(return_value=AIMessage(content="", tool_calls=[{"name": "submit", "args": {}, "id": "a"}]))
    engine = SimpleNamespace(assert_scope=AsyncMock(),
                             model=SimpleNamespace(bind_tools=Mock(return_value=SimpleNamespace(ainvoke=stage))))
    scope = RunScope("run", "invocation", "execute", "main")
    feedback = {"code": "SELLING_POINTS_EVIDENCE_INVALID", "details": {"index": 0}}
    await extraction._structured_request(engine, scope, tool_name="submit", system="核对事实", timeout_seconds=1,
                                         inputs={"evidence_catalog": CATALOG}, schema={}, recovery=feedback)
    system, human = stage.await_args.args[0]
    assert "validation_feedback" in system.content
    assert json.loads(human.content) == {"evidence_catalog": CATALOG, "validation_feedback": feedback}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome_unknown", [False, True])
async def test_only_confirmed_invalid_provider_output_is_classified_for_recovery(outcome_unknown):
    stage = AsyncMock(side_effect=ModelProviderError("model_tool_call_invalid", outcome_unknown=outcome_unknown))
    engine = SimpleNamespace(assert_scope=AsyncMock(),
                             model=SimpleNamespace(bind_tools=Mock(return_value=SimpleNamespace(ainvoke=stage))))
    scope = RunScope("run", "invocation", "execute", "main")
    expected = ModelProviderError if outcome_unknown else extraction.SellingPointsOutputError
    with pytest.raises(expected):
        await extraction._structured_request(engine, scope, tool_name="submit", system="核对事实", timeout_seconds=1,
                                             inputs={}, schema={})
