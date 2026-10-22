"""Real SDK tool loop: scoped read recovery is feedback, actual denial is not."""
from collections import Counter
from copy import deepcopy

import pytest
from langchain_core.messages import ToolMessage

from shengbo_contracts.common import ContractError
from test_provider_output_convergence import (
    convergence, call, final, invoke, ref, tool_outputs,
)


@pytest.fixture
async def reader_case(convergence):
    case = convergence
    product = case.run["goal"]["action_target_refs"][0]
    report = {"summary": "真实隔离的正式顾客研究", "product_ids": [product["id"]],
        "dependencies": [product], "fields": {"scene": "课外自主练习"}}
    body = {"accepted": True, "preparation_id": None, "product_refs": [product],
        "readset": [product], "report": report}
    case.analysis = {"ref": ref("analysis", "analysis-reader-feedback", body), "body": body}
    prep_body = {"id": case.run["scope"]["preparation_id"], "analysis_refs": []}
    case.client.preparation = {"ref": ref("preparation", prep_body["id"], prep_body), "body": prep_body}
    original_query = case.client.query

    async def query(key, args=None, **kwargs):
        if key not in {"find_audience_research", "read_analysis_report"}:
            return await original_query(key, args, **kwargs)
        case.client.queries.append((key, deepcopy(args), deepcopy(kwargs)))
        if key == "find_audience_research":
            assert args["ref"] == product and args["product_id"] == product["id"]
            return {"items": [{"research_ref": case.analysis["ref"], "compatibility": "exact",
                "product_refs": [product], "report": report}], "next_cursor": None}
        assert args["ref"]["id"] == case.analysis["ref"]["id"]
        if getattr(case, "actual_denial", False):
            raise ContractError("ARTIFACT_SCOPE_CONFLICT", "不是当前本场", status=403)
        return deepcopy(case.analysis)

    case.client.query = query
    return case


@pytest.mark.asyncio
async def test_sdk_reads_existing_report_without_manual_discovery_or_restart(reader_case):
    case = reader_case

    def respond(payload):
        if len(case.sent) == 1:
            return [call("read_context", "read-existing", {"ref": case.analysis["ref"], "sections": ["report"]})]
        assert len(case.sent) == 2
        assert tool_outputs(payload)["read-existing"]["body"]["report"] == case.analysis["body"]["report"]
        return final()

    case.respond = respond
    result = await invoke(case, thread="research-auto-scoped-reader")
    assert result["messages"][-1].content == "测试完成"
    assert not any(isinstance(m, ToolMessage) and m.status == "error" for m in result["messages"])
    assert len(case.sent) == 2 and not case.client.commands
    assert Counter(key for key, _, _ in case.client.queries) == {
        "read_preparation": 1, "find_audience_research": 1, "read_analysis_report": 1}


@pytest.mark.asyncio
async def test_sdk_unresolved_reference_is_feedback_then_model_chooses_correct_ref(reader_case):
    case = reader_case
    wrong = {**case.analysis["ref"], "id": "analysis-unknown"}

    def respond(payload):
        seen = tool_outputs(payload)
        if len(case.sent) == 1:
            return [call("read_context", "read-unresolved", {"ref": wrong})]
        if len(case.sent) == 2:
            error = seen["read-unresolved"]
            assert error["error"]["code"] == "ANALYSIS_REFERENCE_UNRESOLVED"
            assert error["recovery"]["business_write_performed"] is False
            assert case.engine.store.get("run", case.run["id"])["status"] == "RUNNING"
            return [call("read_context", "read-corrected", {"ref": case.analysis["ref"], "sections": ["report"]})]
        assert len(case.sent) == 3
        assert seen["read-corrected"]["body"]["report"] == case.analysis["body"]["report"]
        return final()

    case.respond = respond
    result = await invoke(case, thread="research-input-feedback")
    errors = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.status == "error"]
    assert len(errors) == 1 and errors[0].tool_call_id == "read-unresolved"
    assert len(case.sent) == 3 and not case.client.commands


@pytest.mark.asyncio
async def test_actual_backend_denial_still_stops_sdk(reader_case):
    case = reader_case
    case.actual_denial = True
    case.respond = lambda payload: [call("read_context", "not-your-prep", {"ref": case.analysis["ref"]})]
    with pytest.raises(ContractError, match="ARTIFACT_SCOPE_CONFLICT"):
        await invoke(case, thread="research-true-permission-denial")
    assert len(case.sent) == 1 and not case.client.commands
