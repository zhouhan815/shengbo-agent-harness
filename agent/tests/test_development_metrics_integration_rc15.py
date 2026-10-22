"""Developer-only measurements do not change native model requests or control."""
import json
from copy import deepcopy
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from shengbo_prelive.models import RunScope
from shengbo_prelive.runtime import CURRENT_SCOPE
from test_context_window_budget import setup


@pytest.mark.asyncio
async def test_actual_native_request_hook_records_separate_development_metric(setup):
    engine, graph, run, sent, credentials = setup
    operations = deepcopy(engine.store.list("operation"))
    questions = deepcopy(engine.store.list("question"))
    pending = engine.store.get("run", run["id"]).get("pending_unknown_operation")
    scope = RunScope(run["id"], "developer-observation-native", "execute")
    token = CURRENT_SCOPE.set(scope)
    try:
        await graph.ainvoke({"messages": [HumanMessage("只读解释当前任务，不执行业务操作。")], "files": {}},
            {"configurable": {"thread_id": "developer-observation-native"}, "recursion_limit": 15}, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)
    assert len(sent) == credentials.calls == 1
    metrics = list((engine.root / "development_diagnostics" / "requests").glob("*.json"))
    assert len(metrics) == 1
    measured = json.loads(metrics[0].read_text(encoding="utf-8"))
    assert measured["run_id"] == run["id"] and measured["role"] == "main"
    assert measured["development_only"] is True and measured["not_a_provider_usage_ledger"] is True
    assert engine.store.list("operation") == operations and engine.store.list("question") == questions
    events = engine.trace.events(run["id"])["events"]
    assert len([event for event in events if event["type"] == "model.request"]) == 1
    assert not any(event["type"].startswith("development.") for event in events)
    assert engine.store.get("run", run["id"]).get("pending_unknown_operation") == pending


@pytest.mark.asyncio
async def test_development_writer_failure_neither_retries_nor_hides_original_trace(setup):
    engine, graph, run, sent, credentials = setup
    operations = deepcopy(engine.store.list("operation"))
    questions = deepcopy(engine.store.list("question"))
    scope = RunScope(run["id"], "developer-observation-degraded", "execute")
    token = CURRENT_SCOPE.set(scope)
    try:
        with patch("shengbo_prelive.development_metrics.record_request_metrics", side_effect=OSError("private test details")):
            await graph.ainvoke({"messages": [HumanMessage("只读继续。")], "files": {}},
                {"configurable": {"thread_id": "developer-observation-degraded"}, "recursion_limit": 15}, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)
    assert len(sent) == credentials.calls == 1
    warning = engine.store.get("development_observation_status", run["id"])
    assert warning["status"] == "degraded" and warning["code"] == "DEVELOPMENT_MEASUREMENT_UNAVAILABLE"
    assert "private test details" not in json.dumps(warning)
    assert engine.store.list("operation") == operations and engine.store.list("question") == questions
    events = engine.trace.events(run["id"])["events"]
    assert len([event for event in events if event["type"] == "model.request"]) == 1
    assert not any(event["type"].startswith("development.") for event in events)
