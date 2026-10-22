"""512 real compiler requirements -> job projection -> exact reader/budget.

No graph is constructed or started, no model/credentials/TTS/network is used,
and every persistent object is in pytest's temporary runtime. This exercises
the hand-off scale contract, not synthesis throughput or 512 WAV validity.
"""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import patch

from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
import pytest

from shengbo_contracts.common import ContractError, WorkScope, canonical_hash, canonical_json
from shengbo_prelive.middleware import ContextProjection, ToolPolicy
from shengbo_prelive.models import RunScope
from shengbo_prelive.policy import EFFECTS
from shengbo_prelive.providers.chatgpt import preview_request_payload
from shengbo_prelive.runtime import RuntimeEngine, SYSTEM_MAIN
from shengbo_prelive.tools import COMMAND_MAP, build_tools

VOICE_ROOT = Path(__file__).resolve().parents[2] / "ecommerce-voice-agent"
if str(VOICE_ROOT) not in sys.path:
    sys.path.insert(0, str(VOICE_ROOT))
from app.prelive.playback_compiler import compile_items, opening_for_position


def ref(kind, identity, body):
    return {"kind": kind, "id": identity, "revision": 1, "content_hash": canonical_hash(body)}


def actual_compiler_requirements():
    """Eight saved variants, 64 unique sentences each, one actual opening."""
    product_ref = ref("product", "product-scale", {"name": "规模隔离商品", "price": 99})
    scripts, modules, playlist = {}, {}, []
    for position in range(1, 9):
        module_refs = {}
        for key in (opening_for_position(position), "product_intro", "selling_points", "campaign", "cta"):
            count = 60 if key == "selling_points" else 1
            sentences = [{"text": f"第{position}版{key}第{number}句：这是本地规模交接的独立测试口播，只作为合成需求样本。",
                "spoken_text": f"第{position}版{key}第{number}句：这是本地规模交接的独立测试口播，只作为合成需求样本。"}
                for number in range(count)]
            body = {"module_key": key, "sentences": sentences}
            module_ref = ref("module", f"module-{position}-{key}", body)
            modules[module_ref["id"]] = body
            module_refs[key] = module_ref
        script = {"product_id": product_ref["id"], "product_ref": product_ref,
            "variant_id": f"variant-{position}", "version_number": position, "module_refs": module_refs}
        script_ref = ref("script_revision", f"script-{position}", script)
        scripts[script_ref["id"]] = script
        playlist.append({"occurrence_id": f"occurrence-{position}", "product_id": product_ref["id"],
            "script_revision_ref": script_ref})
    compiled = compile_items(playlist, "infinite", lambda r: scripts[r["id"]], lambda r: modules[r["id"]])
    assert len(compiled["requirements"]) == 512
    assert [len(entry["sentences"]) for entry in compiled["entries"]] == [64] * 8
    return compiled


class ScaleClient:
    def __init__(self, job, preparation):
        self.job, self.preparation = job, preparation
        self.queries, self.commands = [], []

    async def query(self, key, args=None, **kwargs):
        self.queries.append((key, deepcopy(args)))
        assert kwargs["run_id"] == self.job["body"]["run_id"]
        if key == "read_job":
            assert args["job_id"] == self.job["ref"]["id"]
            return deepcopy(self.job)
        if key == "read_completion_observation":
            return {"preparation": deepcopy(self.preparation), "jobs": [deepcopy(self.job)],
                "pending_job_ids": [self.job["ref"]["id"]], "unknown_job_ids": [self.job["ref"]["id"]],
                "candidate_ready": False}
        raise AssertionError("scale fixture forbids unexpected queries")

    async def command(self, *args, **kwargs):
        self.commands.append(args)
        raise AssertionError("scale fixture forbids all business commands")

    async def close(self):
        pass


@pytest.fixture
async def scale512(tmp_path):
    compiled = actual_compiler_requirements()
    requirements = {"preparation_id": "prep-scale-512", "items": compiled["requirements"]}
    requirements_ref = ref("requirements", "requirements-scale-512", requirements)
    run_id = "run-scale-512"
    items = []
    for position, requirement in enumerate(requirements["items"]):
        output = {"asset": {"requirement_id": requirement["requirement_id"],
            "spoken_text": requirement["spoken_text"], "path": f"synthetic/{position}.wav",
            "sha256": canonical_hash(["synthetic-metadata-not-a-real-WAV", position])}}
        state, status = "result_received", "succeeded"
        if position == 509:
            state, status = "known_failed", "failed"
        elif position == 510:
            state, status = "sent", "running"
        elif position == 511:
            state, status = "outcome_unknown", "interrupted"
        attempt = {"attempt_id": f"attempt-{position}", "purpose": "tts", "state": state,
            "input_hash": canonical_hash(requirement), "usage_state": "known" if status == "succeeded" else "unknown"}
        if status == "succeeded":
            attempt.update(output=deepcopy(output), output_hash=canonical_hash(output),
                usage={"characters": len(requirement["spoken_text"])})
        items.append({"item_id": f"item-{position}", "input": deepcopy(requirement),
            "status": status, "call_state": state, "attempt": 1,
            "error": {"code": "SYNTHETIC_EXPLICIT_FAILURE"} if position == 509 else None,
            "output": output if status == "succeeded" else None, "provider_attempts": [attempt]})
    job_body = {"id": "job-scale-512", "kind": "audio", "run_id": run_id,
        "preparation_id": requirements["preparation_id"], "status": "running", "generation_epoch": 3,
        "requirements_ref": requirements_ref, "items": items, "cancel_requested": False}
    job = {"ref": ref("prelive_job", job_body["id"], job_body), "body": job_body}
    prep_body = {"id": requirements["preparation_id"], "generation_epoch": 3,
        "requirements_ref": requirements_ref, "selected_product_refs": [], "analysis_refs": []}
    prep = {"ref": ref("preparation", prep_body["id"], prep_body), "body": prep_body}
    client = ScaleClient(job, prep)
    engine = RuntimeEngine(tmp_path, client, context_input_token_limit=48000)
    conversation = engine.create_conversation({"scope": {"kind": "preparation", "preparation_id": prep_body["id"]}})
    work_scope = WorkScope(kind="preparation", preparation_id=prep_body["id"]).model_dump()
    effects = sorted(EFFECTS["prepare_session"])
    goal = {"objective": "prepare_session", "scope": work_scope, "allowed_effects": effects}
    run = engine.store.put("run", {"id": run_id, "conversation_id": conversation["id"], "scope": work_scope,
        "phase": "execute", "status": "RUNNING", "input": {"text": "只读核对已批准音频任务的512项交接。", "material_refs": []},
        "goal": goal, "frozen_goal_hash": canonical_hash(goal), "allowed_effects": effects,
        "allowed_tools": [name for name, command in COMMAND_MAP.items() if command in effects]})
    scope = RunScope(run_id, "scale-projection-only", "execute")
    case = SimpleNamespace(engine=engine, client=client, run=run, scope=scope,
        job=job, compiled=compiled, requirements=requirements, original=deepcopy(job))
    try:
        yield case
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_512_distinct_compiler_requirements_and_tail_states_survive_projection(scale512):
    c = scale512
    requirements = c.requirements["items"]
    assert len({r["requirement_id"] for r in requirements}) == 512
    assert len({r["spoken_text"] for r in requirements}) == 512
    assert [item["input"] for item in c.job["body"]["items"]] == requirements
    view = await c.engine.business_query(c.scope, "read_job", {"job_id": c.job["ref"]["id"]})
    counts = view["items_projection"]
    assert counts["total"] == counts["provider_attempt_count"] == 512
    assert counts["status_counts"] == {"succeeded": 509, "failed": 1, "running": 1, "interrupted": 1}
    assert counts["unknown_item_count"] == counts["unknown_provider_attempt_count"] == 1
    assert counts["in_flight_provider_attempt_count"] == 1
    assert len(view["body"]["items"]) <= 4
    assert counts["next_cursor"] == len(view["body"]["items"])
    assert {row["item_id"] for row in view["attention_items"]} >= {"item-509", "item-511"}
    for position in (509, 510, 511):
        exact = c.engine.run_history(c.scope, {"ref": view["full_ref"],
            "path": f"/job/body/items/{position}", "max_chars": 12000})
        assert exact["document"] == c.original["body"]["items"][position]
    # Full exact recovery is deliberately inspected locally, never sent to a
    # model: all 512 original objects, not only the first model-visible page.
    original = c.engine.run_history(c.scope, {"ref": view["full_ref"], "full": True})["document"]["job"]
    assert original == c.original and len(original["body"]["items"]) == 512
    page = c.engine.run_history(c.scope, {"ref": counts["full_ref"], "path": counts["path"],
        "cursor": 400, "limit": 2, "max_chars": 24000})
    assert page["document"] == c.original["body"]["items"][400:402]
    assert page["selection"]["next_cursor"] == 402
    assert c.client.job == c.original and not c.client.commands
    assert not c.engine.started and not hasattr(c.engine, "main")


@pytest.mark.asyncio
async def test_512_resume_has_global_unknown_and_exact_reader_is_scope_bound(scale512):
    c = scale512
    observation = await c.engine._completion_observation(c.run)
    wake = c.engine._resume_event_view(c.run, {"kind": "job_progress", "jobs": observation})
    counts = wake["jobs"]["jobs_projection"]
    assert counts["total"] == 1 and counts["provider_attempt_count"] == 512
    assert counts["unknown_item_count"] == counts["unknown_provider_attempt_count"] == 1
    assert counts["in_flight_provider_attempt_count"] == 1
    assert wake["jobs"]["unknown_job_ids"] == [c.job["ref"]["id"]]
    assert len(canonical_json(wake)) < 30000
    c.engine.store.update("run", c.run["id"], finish={"outcome": "READY_FOR_REVIEW", "summary": "故意尝试错误声明完成"})
    acceptance = await c.engine.verify_finish(c.scope)
    assert not acceptance["ok"] and "业务任务尚未成功结束或结果未知" in acceptance["missing"]
    view = c.engine._job_view(c.run, c.job, include_provider_outputs=False)
    foreign_conversation = c.engine.create_conversation({"scope": {"kind": "catalog"}})
    foreign = c.engine.store.put("run", {**c.run, "id": "foreign-scale-512", "conversation_id": foreign_conversation["id"]})
    with pytest.raises(ContractError, match="RUN_SCOPE_DENIED"):
        c.engine.run_history(RunScope(foreign["id"], "foreign", "execute"),
            {"ref": view["full_ref"], "path": "/job/body/items/511"})
    with pytest.raises(ContractError, match="HISTORY_REF_STALE"):
        c.engine.run_history(c.scope, {"ref": {**view["full_ref"], "content_hash": canonical_hash("tampered")},
            "path": "/job/body/items/511"})
    assert not c.client.commands and c.client.job == c.original


@pytest.mark.asyncio
async def test_512_real_public_schemas_m3_skill_and_wake_fit_unsent_wire(scale512, record_property):
    c = scale512
    view = await c.engine.business_query(c.scope, "read_job", {"job_id": c.job["ref"]["id"]})
    observation = await c.engine._completion_observation(c.run)
    wake = c.engine._resume_event_view(c.run, {"kind": "job_progress", "jobs": observation})
    fs = FilesystemMiddleware(backend=StateBackend(), tools=["ls", "read_file", "glob", "grep"])
    tools = [*build_tools(c.engine), *fs.tools, *TodoListMiddleware().tools]
    # Main manuals are split since 2026-10-22; keep the conservative full set.
    skill = "\n\n".join(path.read_text(encoding="utf-8") for path in
        sorted(Path(__file__).resolve().parents[1].joinpath("skills/main").glob("prelive*/SKILL.md")))
    messages = [HumanMessage(c.run["input"]["text"]),
        AIMessage(content="", tool_calls=[{"id": "read-512", "name": "read_job", "args": {"job_id": c.job["ref"]["id"]}}]),
        ToolMessage(content=canonical_json(view), tool_call_id="read-512"), HumanMessage(canonical_json(wake))]
    # Preserve the current full main Skill conservatively, rather than replacing
    # it with a tiny fake prompt. Actual M3 and tool filtering wrappers run below.
    request = ModelRequest(model=object(), tools=tools, messages=messages,
        system_message=SystemMessage(SYSTEM_MAIN + "\n\n" + skill), state={"messages": messages},
        runtime=SimpleNamespace(context=c.scope))
    captured = {}

    async def capture(modified):
        captured["payload"] = preview_request_payload([modified.system_message, *modified.messages], modified.tools)
        return captured["payload"]

    async def through_policy(modified):
        return await ToolPolicy(c.engine).awrap_model_call(modified, capture)

    try:
        with patch("shengbo_prelive.providers.chatgpt.CredentialManager", side_effect=AssertionError("offline preview must never read credentials")):
            await ContextProjection(c.engine).awrap_model_call(request, through_policy)
    finally:
        fs._glob_executor.shutdown(wait=False)
    payload = captured["payload"]
    schemas = payload["tools"][0]["tools"]
    names = {tool["name"] for tool in schemas}
    assert {"read_job", "read_run_history", "wf_required_audio", "wf_finalize_preparation", "read_file"} <= names
    # deepagents 0.7.25 has no todo tool; the stale policy name was removed.
    assert {"execute", "task", "write_file", "edit_file", "delete", "write_todos"}.isdisjoint(names)
    assert len(schemas) >= 49 and all(tool.get("parameters") for tool in schemas)
    wire = canonical_json(payload)
    assert "shengbo_trusted_context" in wire and "unknown_side_effects" in wire
    assert c.run["goal"]["objective"] in wire and "outcome_unknown" in wire and "item-511" in wire
    full_tokens = c.engine._estimate_payload(payload)
    raw_tokens = c.engine._estimate_payload({"input": [{"role": "tool", "content": canonical_json(c.original)}]})
    assert full_tokens < 48000, "the whole tested public wire includes actual schemas/M3/Skill/wake, not only the job index"
    assert raw_tokens > 48000, "all 512 original inputs/outputs must remain archived, not reinjected"
    record_property("distinct_compiler_requirements", 512)
    record_property("public_schema_count", len(schemas))
    record_property("complete_public_preview_tokens", full_tokens)
    record_property("raw_job_tokens", raw_tokens)
    with c.engine.store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert not c.engine.store.list("operation") and not c.client.commands
    assert not c.engine.started and not hasattr(c.engine, "main")
