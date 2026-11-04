from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from shengbo_contracts.business import AnalysisReport
from shengbo_contracts.common import ContractError
from shengbo_prelive.models import RunScope
from shengbo_prelive.selling_point_evidence import build_evidence_catalog
from shengbo_prelive.selling_point_research_extract import extract_audience_research


@pytest.fixture
def case(monkeypatch):
    ref = {"kind": "product", "id": "lamp-a", "revision": 3, "content_hash": "lamp-a-r3"}
    product = {"name": "露营灯标准版", "sku_id": "LAMP-A", "specifications": [
        {"name": "电源适用条件", "value": "仅适用 5 V USB 电源，不支持边充边用"}]}
    evidence_catalog = build_evidence_catalog(ref, product)
    evidence_id = next(iter(evidence_catalog))
    web_results = {"status": "ok", "results": [{"url": "https://example.com/camping",
        "title": "营地用电讨论", "snippet": "露营顾客讨论可用的充电设备。"}]}
    draft = {"opportunities": [{"audience": "带移动电源的露营者", "scene": "整理营地装备",
        "concern": "手头电源能否使用", "benefit": "出发前按明确的电源适用条件准备装备",
        "emphasis": "说清仅适用 5 V USB 电源，不能边充边用", "evidence_ids": [evidence_id]}],
        "hypotheses": [{"hypothesis": "露营顾客可能希望先确认电源条件。", "web_ids": ["W1"]}]}
    invoke = AsyncMock(return_value=draft)
    monkeypatch.setattr("shengbo_prelive.selling_point_extract._structured_request", invoke)
    return {"engine": object(), "scope": RunScope("run", "invocation", "execute", "audience_scenes", "branch"),
            "product": product, "product_ref": ref, "evidence_catalog": evidence_catalog,
            "web_results": web_results}, invoke


@pytest.mark.asyncio
async def test_research_binds_exact_product_facts_and_keeps_public_snippet_as_hypothesis(case):
    inputs, invoke = case
    report = await extract_audience_research(**inputs)
    parsed = AnalysisReport.model_validate(report)
    ref = inputs["product_ref"]
    fact, web = parsed.evidence

    invoke.assert_awaited_once()
    assert invoke.await_args.args == (inputs["engine"], inputs["scope"])
    assert parsed.product_ids == [ref["id"]]
    assert parsed.dependencies[0].model_dump(exclude_unset=True) == ref
    assert not parsed.source_refs
    assert fact["source_ref"] == ref
    assert fact["quote"] == "仅适用 5 V USB 电源，不支持边充边用"
    assert web["url"] == "https://example.com/camping"
    assert web["quote"] == "露营顾客讨论可用的充电设备。"
    assert web["status"] == "unverified_search_clue"
    assert parsed.fields["benefit_priorities"][0]["evidence_ids"] == [fact["id"]]
    assert parsed.fields["benefit_priorities"][0]["classification"] == "commercial_hypothesis"
    assert "W1" in parsed.hypotheses[0] and "未核验网页" in parsed.hypotheses[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("section", "field", "key", "reason"), [
    ("opportunities", "evidence_ids", "W1", "unknown_product_fact"),
    ("hypotheses", "web_ids", "W42", "unknown_web_source"),
])
async def test_model_cannot_invent_source_ids_or_use_a_web_clue_as_a_product_fact(case, section, field, key, reason):
    inputs, invoke = case
    draft = deepcopy(invoke.return_value)
    draft[section][0][field] = [key]
    invoke.return_value = draft

    with pytest.raises(ContractError) as error:
        await extract_audience_research(**inputs)

    invoke.assert_awaited_once()
    assert error.value.code == "SELLING_POINTS_RESEARCH_OUTPUT_INVALID"
    assert error.value.details == {"section": section, "index": 0, "reason": reason}


@pytest.mark.asyncio
@pytest.mark.parametrize(("web_results", "status"), [
    ({"status": "ok", "results": []}, "no_results"),
    ({"status": "error", "error": {"code": "public_search_unavailable"}}, "unavailable"),
])
async def test_empty_or_failed_search_stays_explicit_without_fake_research_evidence(case, web_results, status):
    inputs, invoke = case
    inputs["web_results"] = web_results
    invoke.return_value["hypotheses"][0]["web_ids"] = []

    report = await extract_audience_research(**inputs)

    assert report["fields"]["context"]["public_research_status"] == status
    assert [item["kind"] for item in report["evidence"]] == ["product_fact"]


@pytest.mark.asyncio
async def test_absent_product_facts_does_not_spend_a_model_attempt(case):
    inputs, invoke = case
    inputs["evidence_catalog"] = {}

    with pytest.raises(ContractError) as error:
        await extract_audience_research(**inputs)

    invoke.assert_not_awaited()
    assert error.value.code == "SELLING_POINTS_RESEARCH_NO_FACTS"


@pytest.mark.asyncio
async def test_research_rejects_model_supplied_citation_fields_without_exposing_their_values(case):
    inputs, invoke = case
    invoke.return_value["hypotheses"][0]["url"] = "https://unseen.example/unsupported"

    with pytest.raises(ContractError) as error:
        await extract_audience_research(**inputs)

    assert error.value.code == "SELLING_POINTS_RESEARCH_OUTPUT_INVALID"
    assert error.value.details == {"issues": [["hypotheses", "0"]]}
