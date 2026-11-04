"""Provider-visible report shapes and lossless handoff to scoped validation."""
from copy import deepcopy
from types import SimpleNamespace

from langchain.tools import ToolRuntime
from langchain_core.utils.function_calling import convert_to_openai_tool
import pytest

from shengbo_prelive.models import RunScope
from shengbo_prelive.tools import build_tools


def report_tool(role, engine=None):
    return next(tool for tool in build_tools(engine, role) if tool.name == f"submit_{role}_report")


def wire_report(role):
    parameters = convert_to_openai_tool(report_tool(role))["function"]["parameters"]
    assert set(parameters["properties"]) == {"report"}
    return parameters["properties"]["report"]["properties"]


def test_product_worker_advertises_nested_enums_and_upload_evidence_schema():
    report = wire_report("product_facts")
    material = report["material_assessment"]["anyOf"][0]["properties"]
    missing = material["missing_info"]["items"]["properties"]
    assert missing["level"]["enum"] == ["block_required", "conditional", "recommended", "optional"]
    assert missing["status"]["enum"] == ["unresolved", "answered", "omitted_from_task", "not_applicable"]
    assert "why" in missing and "question" in missing
    evidence = report["evidence"]["items"]
    assert "source_ref" in evidence["required"]
    assert evidence["if"]["properties"]["source_ref"]["properties"]["kind"]["const"] == "source_manifest"
    assert set(evidence["then"]["required"]) == {"locator", "quote"}
    refs = evidence["properties"]["source_ref"]["anyOf"]
    assert all({"kind", "id", "revision", "content_hash"} <= set(ref["required"]) for ref in refs)


@pytest.mark.parametrize("role", ["session_config", "audience_scenes"])
def test_other_workers_keep_heterogeneous_evidence(role):
    evidence = wire_report(role)["evidence"]["items"]
    assert evidence["type"] == "object"
    assert "source_ref" not in evidence.get("required", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [
    {"summary": 7, "unexpected_field": {"must_keep": True}, "evidence": [{"quote": "缺来源的原文"}],
     "material_assessment": {"missing_info": [{"level": "critical", "status": "needs_confirmation"}]}},
    ["report must be an object, but keep this draft for diagnostics"],
])
async def test_tool_passes_invalid_report_to_runtime_without_normalizing(draft):
    calls = []

    def submit(scope, raw, *, strict_evidence=False):
        calls.append((scope, deepcopy(raw), strict_evidence))
        return {"status": "received"}

    role = "product_facts"
    scope = RunScope("run-fixture", "invocation-fixture", "execute", role, "branch-fixture")
    runtime = ToolRuntime(state={}, context=scope, config={}, stream_writer=lambda _: None,
                          tool_call_id="submit-fixture", store=None)
    tool = report_tool(role, SimpleNamespace(submit_report=submit))
    assert await tool.ainvoke({"report": deepcopy(draft), "runtime": runtime}) == {"status": "received"}
    assert calls == [(scope, draft, True)]
