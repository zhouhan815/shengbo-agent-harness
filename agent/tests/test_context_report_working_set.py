"""Keep a fitting report working set through real SDK/provider tool rounds."""
from collections import Counter
import json
from unittest.mock import patch

import httpx
import pytest
import tiktoken
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from shengbo_contracts.common import ContractError, WorkScope, canonical_hash, canonical_json
from shengbo_prelive.middleware import Audit, ContextProjection, ToolPolicy
from shengbo_prelive.models import RunScope
from shengbo_prelive.policy import EFFECTS
from shengbo_prelive.providers.chatgpt import build_chatgpt_model
from shengbo_prelive.runtime import CURRENT_SCOPE, RuntimeEngine
from shengbo_prelive.tools import COMMAND_MAP


ENCODING = tiktoken.get_encoding("o200k_base")
# Relative sizes, not a promise that today's evolving schemas leave a fixed
# number of free tokens. The fixture measures the complete actual SDK wire
# before credentials, then allocates synthetic filler within the same 48k
# policy. Overflow suites separately exercise the exact hard fence.
REPORT_SIZES = {"product_facts": 8400, "session_config": 4900, "audience_scenes": 6600}


def token_count(value):
    return len(ENCODING.encode(canonical_json(value)))


def sized_document(document, target):
    """Pad public test data to a measured size, without a task-specific prompt."""
    filler = "独立来源条目；未知事实保持未知。"
    low, high = 0, target
    while low < high:
        middle = (low + high) // 2
        document["body"] = filler * middle
        if token_count(document) < target:
            low = middle + 1
        else:
            high = middle
    document["body"] = filler * low
    assert target <= token_count(document) < target + 20
    return document


def function_call(name, call_id, args):
    return {"type": "function_call", "namespace": "functions", "name": name,
            "call_id": call_id, "arguments": json.dumps(args, ensure_ascii=False)}


class OfflineBusiness:
    def __init__(self, schema):
        self.schema = schema
        self.queries = []

    async def query(self, key, args=None, **kwargs):
        self.queries.append((key, args))
        assert key == "get_business_schema", "this test must not contact a business service"
        return self.schema

    async def close(self):
        pass


class OfflineCredentials:
    calls = 0

    def get_access_token(self):
        self.calls += 1
        return "fake-working-set-test-credential"


@pytest.fixture
async def working_set(tmp_path):
    schema = sized_document({"contract_version": "PRELIVE-2.3",
        "schema": {"public_schema_marker": "SCHEMA_FIELD_VISIBLE"}}, 2700)
    schema["schema_hash"] = canonical_hash(schema["schema"])
    client = OfflineBusiness(schema)
    engine = RuntimeEngine(tmp_path, client, context_input_token_limit=48000)
    assert engine.context_summary_trigger_tokens == 12000
    assert engine.context_summary_keep_tokens == 3000
    conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    work_scope = WorkScope(kind="preparation", preparation_id="prep-working-set").model_dump()
    effects = sorted(EFFECTS["prepare_session"])
    run = engine.store.put("run", {"id": "run-working-set", "conversation_id": conversation["id"],
        "scope": work_scope, "status": "RUNNING", "phase": "execute",
        "input": {"text": "继续当前任务。", "material_refs": []},
        "goal": {"objective": "prepare_session", "scope": work_scope,
                 "required_analyses": list(REPORT_SIZES), "allowed_effects": effects},
        "allowed_effects": effects,
        "allowed_tools": [name for name, command in COMMAND_MAP.items() if command in effects]})
    reports = {}
    for role in REPORT_SIZES:
        report = {"summary": role + "公开候选报告", "product_ids": [],
            "fields": {"public_field_marker": role.upper() + "_FIELD_VISIBLE"},
            "source_refs": [], "dependencies": []}
        reports[role] = report
        engine.store.put("branch", {"id": "branch-" + role, "run_id": run["id"], "role": role,
            "status": "PREPARED", "report": report, "report_hash": canonical_hash(report),
            "task": {"source_refs": [], "input_refs": [], "product_ids": []}})
    scope = RunScope(run["id"], "working-set-invoke", "execute")
    rounds = []
    sent, credentials = [], OfflineCredentials()

    def respond(request):
        payload = json.loads(request.content)
        assert engine._estimate_payload(payload) <= 48000, "check the complete serialized wire on every send"
        sent.append(payload)
        assert payload.get("tools"), "a fitting working set must not cause a separate summary model call"
        output = rounds.pop(0) if rounds else [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "测试完成"}]}]
        event = {"type": "response.completed", "response": {"status": "completed", "output": output,
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
            text="data: " + json.dumps(event) + "\n\n")

    engine.model = build_chatgpt_model(credentials=credentials, transport=httpx.MockTransport(respond),
        verify_catalog=False, before_send=engine._before_send, observer=engine._model_event,
        on_request=engine._model_request, on_response=engine._model_response)
    engine.saver = InMemorySaver()
    # Keep actual tools, Skills, native summarization, checkpointing, M3 and wire
    # serialization. This read-only budget test does not run completion guards.
    with patch("shengbo_prelive.runtime.main_middleware",
               lambda e: [ContextProjection(e), ToolPolicy(e), Audit(e)]):
        graph = engine._build_graph("main")
    try:
        class WireShapeReady(Exception):
            pass

        fixed_probe = []

        def capture_fixed_wire(payload):
            fixed_probe.append(payload)
            raise WireShapeReady()

        with patch.object(engine.model, "_before_send", capture_fixed_wire):
            with pytest.raises(WireShapeReady):
                await graph.ainvoke({"messages": [HumanMessage("继续。")],
                    "files": engine._initial_files("main")},
                    {"configurable": {"thread_id": "report-fixed-shape-probe"}}, context=scope)
        assert len(fixed_probe) == 1 and not sent and credentials.calls == 0
        with engine.store.tx() as db:
            assert db.execute("SELECT COUNT(*) FROM attempts WHERE run_id=?", (run["id"],)).fetchone()[0] == 0
        fixed_tokens = engine._estimate_payload(fixed_probe[0])
        # Reserve the exact schema document, 3k for reader envelopes/M3 read
        # receipts, and 2k for the soft-pressure margin/UUID variation. Only
        # public synthetic filler scales; reports, markers, refs and policy do
        # not. The final test still checks every complete sent wire.
        filler_budget = 48000 - fixed_tokens - token_count(schema) - 3000 - 2048
        assert 12000 <= filler_budget <= 36000, "unexpected fixed-schema growth must remain visible"
        for role, weight in REPORT_SIZES.items():
            report = sized_document(reports[role], int(filler_budget * weight / sum(REPORT_SIZES.values())))
            engine.store.update("branch", "branch-" + role, report=report, report_hash=canonical_hash(report))
        projection = await engine.project_context(scope)
        refs = {branch["role"]: branch["report_ref"] for branch in projection["branches"]["items"]}
        rounds.extend([
            [function_call("read_run_history", "read-facts-once", {"ref": refs["product_facts"], "full": True}),
             function_call("read_business_schema", "read-schema-once", {})],
            [function_call("read_run_history", "read-session-once", {"ref": refs["session_config"], "full": True})],
            [function_call("read_run_history", "read-audience-once", {"ref": refs["audience_scenes"], "full": True})],
        ])
        yield engine, graph, run, scope, reports, schema, sent, credentials
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_three_reports_and_schema_survive_separate_tool_rounds_when_complete_wire_fits(working_set):
    engine, graph, run, scope, reports, schema, sent, credentials = working_set
    config = {"configurable": {"thread_id": "three-report-working-set"}, "recursion_limit": 30}
    token = CURRENT_SCOPE.set(scope)
    try:
        result = await graph.ainvoke({"messages": [HumanMessage("继续。")],
            "files": engine._initial_files("main")}, config, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)

    assert result["messages"][-1].content == "测试完成"
    assert len(sent) == credentials.calls == 4, "three tool batches and their final response need four main calls"
    estimates = [engine._estimate_payload(payload) for payload in sent]
    assert estimates == sorted(estimates)
    assert 35000 < estimates[-1] < 48000 - 1024, "exercise the large but fitting working set, below soft pressure"
    assert token_count(sent[-1]["tools"]) > 10000, "include real visible tool schemas rather than a tiny stub"

    expected = {"read-facts-once": "product_facts", "read-session-once": "session_config",
                "read-audience-once": "audience_scenes"}
    for payload, available in zip(sent[1:], [set(expected) - {"read-session-once", "read-audience-once"},
                                           set(expected) - {"read-audience-once"}, set(expected)]):
        outputs = {item["call_id"]: item["output"] for item in payload["input"]
                   if item.get("type") == "function_call_output"}
        assert set(outputs) == available | {"read-schema-once"}
        assert json.loads(outputs["read-schema-once"]) == schema
        for call_id in available:
            role = expected[call_id]
            branch = json.loads(outputs[call_id])["document"]["branch"]
            assert branch["role"] == role and branch["status"] == "PREPARED"
            assert branch["report"] == reports[role], "earlier full report fields must remain beside later reports"
            assert not branch.get("accepted_ref"), "reading a report does not imply formal acceptance"

    events = engine.events(run["id"])["events"]
    assert not any(event["type"] in {"context.summarized", "context.tools_offloaded", "model.window_rejected"}
                   for event in events)
    starts = [event["data"] for event in events if event["type"] == "tool.started"]
    assert Counter(item["tool_name"] for item in starts) == {"read_run_history": 3, "read_business_schema": 1}
    assert Counter(item["tool_call_id"] for item in starts) == Counter([*expected, "read-schema-once"])
    assert len([message for message in result["messages"] if isinstance(message, ToolMessage)]) == 4

    read_records = (await engine.project_context(scope))["retrieved_documents"]["items"]
    assert len(read_records) == 4
    assert {record["role"] for record in read_records if record["kind"] == "branch_report"} == set(REPORT_SIZES)
    assert all(record["read_count"] == 1 and record["status"] == "full_result_returned_by_tool"
               and record["acceptance_implied"] is False for record in read_records)


async def report_refs(engine, scope):
    projection = await engine.project_context(scope)
    return {item["role"]: item["report_ref"] for item in projection["branches"]["items"]}


async def retrieved(engine, scope):
    return (await engine.project_context(scope))["retrieved_documents"]["items"]


@pytest.mark.asyncio
async def test_document_read_ledger_counts_exact_successful_branch_reads_without_implying_acceptance(working_set):
    engine, _, run, scope, reports, _, sent, credentials = working_set
    refs = await report_refs(engine, scope)
    assert await retrieved(engine, scope) == []

    engine.run_history(scope, {"sections": ["analysis_reports"]})
    assert await retrieved(engine, scope) == [], "a history listing is not an exact full-report read"
    engine.store.put("question", {"id": "preview-only", "run_id": run["id"], "kind": "question",
        "status": "OPEN", "prompt": "测试预览", "preview": {"text": "公开预览"}})
    preview_ref = (await engine.project_context(scope))["pending_questions"][0]["preview"]["full_ref"]
    assert engine.run_history(scope, {"full": True, "ref": preview_ref})["document"]["kind"] == "question_preview"
    assert await retrieved(engine, scope) == [], "a successful non-report snapshot read must not count as a report"

    first = engine.run_history(scope, {"full": True, "ref": refs["product_facts"]})
    assert first["document"]["branch"]["report"] == reports["product_facts"]
    records = await retrieved(engine, scope)
    assert len(records) == 1
    record = records[0]
    assert record["ref"] == refs["product_facts"]
    assert record["kind"] == "branch_report" and record["role"] == "product_facts"
    assert record["owner_run_id"] == run["id"]
    assert record["document_hash"] == canonical_hash(reports["product_facts"])
    assert record["status"] == "full_result_returned_by_tool"
    assert record["read_count"] == 1 and record["acceptance_implied"] is False

    next_invocation = RunScope(run["id"], "another-read-invocation", "execute")
    assert engine.run_history(next_invocation, {"full": True, "ref": refs["product_facts"]}) == first
    repeated = await retrieved(engine, scope)
    assert len(repeated) == 1 and repeated[0] == {**record, "read_count": 2}
    assert engine.store.get("branch", "branch-product_facts")["status"] == "PREPARED"
    assert not engine.store.get("branch", "branch-product_facts").get("accepted_ref")
    assert not sent and credentials.calls == 0


@pytest.mark.asyncio
async def test_document_read_ledger_rejects_foreign_or_tampered_refs_without_recording_reads(working_set):
    engine, _, run, scope, reports, _, sent, credentials = working_set
    refs = await report_refs(engine, scope)
    engine.run_history(scope, {"full": True, "ref": refs["product_facts"]})
    baseline = engine.store.list("document_read")

    foreign_conversation = engine.create_conversation({"scope": {"kind": "catalog"}})
    foreign_run = engine.store.put("run", {**run, "id": "foreign-conversation-run",
        "conversation_id": foreign_conversation["id"]})
    engine.store.put("branch", {"id": "foreign-branch", "run_id": foreign_run["id"],
        "role": "product_facts", "status": "PREPARED", "report": reports["product_facts"],
        "report_hash": canonical_hash(reports["product_facts"]),
        "task": {"source_refs": [], "input_refs": [], "product_ids": []}})
    foreign_scope = RunScope(foreign_run["id"], "foreign-invoke", "execute")
    foreign_ref = (await report_refs(engine, foreign_scope))["product_facts"]
    denied_account = RunScope(run["id"], "wrong-account", "execute", account_id="foreign-account")
    attempts = [
        (scope, foreign_ref, "RUN_SCOPE_DENIED"),
        (scope, {**refs["product_facts"], "content_hash": canonical_hash("tampered")}, "HISTORY_REF_STALE"),
        (scope, {**refs["product_facts"], "revision": 2}, "HISTORY_REF_INVALID"),
        (scope, {**refs["product_facts"], "kind": "source_manifest"}, "HISTORY_REF_INVALID"),
        (denied_account, refs["product_facts"], "RUN_SCOPE_DENIED"),
    ]
    for reader, ref, reason in attempts:
        with pytest.raises(ContractError, match=reason):
            engine.run_history(reader, {"full": True, "ref": ref})
        assert engine.store.list("document_read") == baseline, "a denied read must neither add nor increment evidence"
    assert await retrieved(engine, foreign_scope) == []
    assert not sent and credentials.calls == 0


@pytest.mark.asyncio
async def test_document_read_ledger_isolates_main_sibling_branches_and_reader_runs(working_set):
    engine, _, run, scope, _, _, sent, credentials = working_set
    refs = await report_refs(engine, scope)
    facts_scope = RunScope(run["id"], "facts-reader", "execute", "product_facts", "branch-product_facts")
    session_scope = RunScope(run["id"], "session-reader", "execute", "session_config", "branch-session_config")

    engine.run_history(scope, {"full": True, "ref": refs["product_facts"]})
    assert await retrieved(engine, facts_scope) == []
    engine.run_history(facts_scope, {"full": True, "ref": refs["product_facts"]})
    main_records = await retrieved(engine, scope)
    facts_records = await retrieved(engine, facts_scope)
    assert len(main_records) == len(facts_records) == 1
    assert main_records[0]["read_count"] == facts_records[0]["read_count"] == 1
    assert await retrieved(engine, session_scope) == []

    baseline = engine.store.list("document_read")
    with pytest.raises(ContractError, match="BRANCH_SCOPE_DENIED"):
        engine.run_history(session_scope, {"full": True, "ref": refs["product_facts"]})
    assert engine.store.list("document_read") == baseline
    engine.run_history(session_scope, {"full": True, "ref": refs["session_config"]})
    assert {item["role"] for item in await retrieved(engine, session_scope)} == {"session_config"}
    assert await retrieved(engine, scope) == main_records
    assert await retrieved(engine, facts_scope) == facts_records

    # Same-conversation historical evidence is readable, but its receipt belongs
    # to the new reader run and cannot migrate an earlier run's read count.
    next_work_scope = WorkScope(kind="preparation", preparation_id="another-preparation").model_dump()
    next_run = engine.store.put("run", {**run, "id": "next-reader-run", "scope": next_work_scope,
        "goal": {**run["goal"], "scope": next_work_scope, "allowed_effects": []}, "allowed_effects": []})
    next_scope = RunScope(next_run["id"], "next-reader-invoke", "execute")
    assert await retrieved(engine, next_scope) == []
    engine.run_history(next_scope, {"full": True, "ref": refs["product_facts"]})
    next_records = await retrieved(engine, next_scope)
    assert len(next_records) == 1 and next_records[0]["read_count"] == 1
    assert next_records[0]["owner_run_id"] == run["id"], "record the original owner separately from the reader"
    assert next_records[0]["ref"] == refs["product_facts"]
    assert await retrieved(engine, scope) == main_records
    assert await retrieved(engine, facts_scope) == facts_records
    assert engine.store.get("run", next_run["id"])["allowed_effects"] == []
    records = engine.store.list("document_read")
    assert len(records) == 4
    assert all(item["acceptance_implied"] is False for item in records)
    assert not sent and credentials.calls == 0
