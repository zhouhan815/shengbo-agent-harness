"""Only the read-only approval-preview dependency is a normal not-ready view."""
from types import SimpleNamespace

import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.tools import build_tools


def query_tool(response=None, error=None, tool_name="read_text_review"):
    calls = []
    async def query(scope, key, args):
        calls.append((scope, key, args))
        if error is not None:
            raise error
        return response
    engine = SimpleNamespace(business_query=query)
    tool = next(tool for tool in build_tools(engine) if tool.name == tool_name)
    return tool, calls


@pytest.mark.asyncio
async def test_expected_preview_dependency_returns_not_ready_without_approval_artifacts():
    tool, calls = query_tool(error=ContractError("DEPENDENCY_NOT_READY", "尚未编排", status=409,
        details={"missing": "compiled_plan_ref"}))
    scope = object()
    result = await tool.coroutine(runtime=SimpleNamespace(context=scope), preparation_id="prep")
    assert result["status"] == "NOT_READY" and result["missing"] == ["compiled_plan_ref"]
    assert result["review_available"] is result["approval_granted"] is result["automatic_execution"] is False
    assert result["read_only"] is True
    assert not {"review_hash", "review_ref", "approval_ref", "request_id"} & set(result)
    assert result["next_options"][0]["tool"] == "read_script"
    assert calls == [(scope, "create_text_review", {"preparation_id": "prep"})]


@pytest.mark.asyncio
async def test_ready_preview_returns_exact_original_response():
    response = {"review_hash": "sha256:valid", "texts": ["当前正式文本"], "voice": {"provider": "system"}}
    tool, calls = query_tool(response=response)
    assert await tool.coroutine(runtime=SimpleNamespace(context=object()), preparation_id="prep") is response
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [("DEPENDENCY_NOT_READY", 403), ("SCOPE_CONFLICT", 403),
    ("CURRENTNESS_BLOCKED", 409), ("COMPILED_PLAN_STALE", 409), ("DEPENDENCY_NOT_READY", 503)])
async def test_permission_integrity_and_transport_errors_keep_the_original_hard_boundary(code, status):
    error = ContractError(code, "保持原异常", status=status)
    tool, _ = query_tool(error=error)
    with pytest.raises(ContractError) as caught:
        await tool.coroutine(runtime=SimpleNamespace(context=object()), preparation_id="prep")
    assert caught.value is error


@pytest.mark.asyncio
async def test_same_dependency_code_from_another_reader_is_not_swallowed():
    error = ContractError("DEPENDENCY_NOT_READY", "其他读取器未就绪", status=409, details={"missing": "draft_ref"})
    tool, _ = query_tool(error=error, tool_name="read_script")
    with pytest.raises(ContractError) as caught:
        await tool.coroutine(runtime=SimpleNamespace(context=object()), ref={})
    assert caught.value is error
