"""Native Deep Agents correction over real isolated ASGI Voice contracts.

The model and copywriter are scripted; no network, paid provider, research or
TTS calls occur. Four modules are really committed before the CTA is rejected.
"""
import asyncio
from copy import deepcopy
import json

from fastapi import FastAPI
import httpx
import pytest
from langchain_core.messages import AIMessage

from test_runtime_business import env, human_command, new_run, product
from app.prelive import create_router
from app.prelive.module_generation import finish_received_module
from shengbo_contracts.business import ScriptPlanModuleBaseInvalidDetails
from shengbo_contracts.common import Principal
from shengbo_prelive.client import ShengboClient
from shengbo_prelive.models import RunScope
from shengbo_prelive.tools import COMMAND_MAP
from test_optional_workflows import authorize
from test_selection_dependency_recovery_v1 import ScriptedModel


KEYS = ("opening_long", "product_intro", "selling_points", "campaign", "cta")
INSTRUCTIONS = {key: "离线合法测试，仅按本商品已知名称陈述。" for key in KEYS}
CODE = "BASE_MODULE_TARGET_MISMATCH"


class OneRejectedCTA:
    """Public-response callback and real validation, without an HTTP provider."""
    def __init__(self):
        self.calls = []
        self.cta_count = 0
        self.audio_calls = 0

    def voice_options(self):
        return {"providers": ["system"], "voices": ["Huihui"]}

    async def generate_module(self, context, module_key, product, previous, *, response_observer=None):
        self.calls.append(module_key)
        if module_key == "cta":
            self.cta_count += 1
        bad_cta = module_key == "cta" and self.cta_count == 1
        text = product["name"] + "。" if module_key != "cta" else "请按实际需要决定。"
        raw = {"module_key": module_key, "sentences": [{"display_text": text, "spoken_text": text,
            "kind": "product_claim" if module_key != "cta" or bad_cta else "engagement",
            "fact_ids": ["UNKNOWN"] if bad_cta else ["NAME"] if module_key != "cta" else []}]}
        receipt = {"provider": "offline-fixture", "model": "scripted-copywriter", "module_key": module_key,
            "content": json.dumps(raw, ensure_ascii=False), "finish_reason": "stop", "unsupported_response": False,
            "usage": {}, "usage_state": "unknown", "validation_product": deepcopy(product)}
        if response_observer is not None:
            await response_observer(deepcopy(receipt))
        return finish_received_module(receipt, product, module_key)

    async def render_audio(self, *args, **kwargs):
        self.audio_calls += 1
        raise AssertionError("This regression must not call TTS")


def business_stats(service):
    with service.store.transaction() as db:
        return {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in ("records", "record_revisions", "prelive_operations", "prelive_events", "prelive_sequences")}


async def partial_case(engine, service):
    await engine.start()
    engine.scheduler.cancel()
    await asyncio.gather(engine.scheduler, return_exceptions=True)
    adapter = OneRejectedCTA()
    service.adapter = adapter
    service.wake_worker = lambda: None
    p = await product(service, "RC12真实partial模块纠错商品")
    room = (await human_command(service, "create_room", {"name": "RC12离线纠错"}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"],
        "room_revision": room["revision"]}))["data"]["preparation_ref"]
    prep = (await human_command(service, "update_preparation_products", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "selected_product_refs": [p]}))["data"]["preparation_ref"]
    draft = (await human_command(service, "save_session_draft", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "draft": {"theme": "离线恢复验证", "commerce": {}}}))["data"]
    bundle = (await human_command(service, "save_context_bundle", {"preparation_id": prep["id"],
        "expected_revision": draft["preparation_ref"]["revision"], "product_refs": [p],
        "draft_ref": draft["draft_ref"]}))["data"]
    actions = [{"key": key, "action": "generate", "instruction": INSTRUCTIONS[key]} for key in KEYS]
    item = {"product_ref": p, "variant_id": "native-v1", "version_number": 1, "modules": actions}
    saved = (await human_command(service, "save_script_plan", {"preparation_id": prep["id"],
        "expected_revision": bundle["preparation_ref"]["revision"], "bundle_ref": bundle["context_bundle_ref"],
        "items": [item]}))["data"]
    run = await new_run(engine, "恢复本场失败话术；保留已提交模块，不批准、不生成语音、不开播。",
        room_id=room["id"], preparation_id=prep["id"])
    effects = ["save_script_plan", "submit_script_change", "retry_failed_items"]
    await authorize(engine, run, effects)
    run = engine.store.update("run", run["id"], target_product_ids=[p["id"]], created_product_ids=[],
        allowed_tools=[tool for tool, key in COMMAND_MAP.items() if key in effects],
        module_allowlist=list(KEYS), goal={"objective": "prepare_session", "target_product_ids": [p["id"]],
            "scope": run["scope"], "required_analyses": []})
    receipt = await engine.business_command(RunScope(run["id"], "rc12-initial-submit", "execute"),
        "submit_script_change", {"preparation_id": prep["id"],
            "expected_revision": saved["preparation_ref"]["revision"], "plan_ref": saved["script_plan_ref"],
            "context_bundle_ref": bundle["context_bundle_ref"], "expected_generation_epoch": saved["generation_epoch"]},
        "rc12-original-script-submit")
    job_id = receipt["data"]["job_id"]
    await service.drain_jobs()
    job = await service.query("read_job", {"job_id": job_id})
    assert job["body"]["status"] == "failed"
    failed = job["body"]["items"][0]
    assert failed["error"]["code"] == "GENERATED_MODULE_NEEDS_REVIEW"
    assert failed["provider_attempts"][-1]["validation_state"] == "rejected"
    refs = {key: value["module_ref"] for key, value in failed["module_results"].items()}
    assert set(refs) == set(KEYS) - {"cta"}
    assert adapter.calls == list(KEYS)
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    assert current["body"]["script_refs"] == [] and current["body"]["approval_ref"] is None
    return {"run": engine.store.get("run", run["id"]), "prep": current, "product_ref": p,
        "bundle_ref": bundle["context_bundle_ref"], "original_plan_ref": saved["script_plan_ref"],
        "job_id": job_id, "job": job, "refs": refs, "item": item, "adapter": adapter}


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["original_job_retry", "new_generate_plan_cache"])
async def test_native_model_can_correct_missing_base_without_losing_partial_results_or_approval(env, recovery):
    engine, service = env
    case = await partial_case(engine, service)
    run, prep, adapter = case["run"], case["prep"], case["adapter"]
    original_goal, original_effects = deepcopy(run["goal"]), deepcopy(run["allowed_effects"])
    original_refs = deepcopy(case["refs"])
    with service.store.transaction() as db:
        original_modules = {key: service.store.exact(db, ref) for key, ref in original_refs.items()}
    before = business_stats(service)
    bad_item = deepcopy(case["item"])
    for action in bad_item["modules"]:
        if action["key"] != "cta":
            action.update(action="reuse", base_module_ref={k: v for k, v in original_refs[action["key"]].items()
                if k != "schema_version"})
    bad_item["product_ref"] = {k: v for k, v in bad_item["product_ref"].items() if k != "schema_version"}
    assert "base_script_ref" not in bad_item
    bad_plan = {"preparation_id": prep["ref"]["id"], "expected_revision": prep["ref"]["revision"],
        "bundle_ref": {k: v for k, v in case["bundle_ref"].items() if k != "schema_version"}, "items": [bad_item]}

    # Real wire DTO/error parsing through an in-process ASGI transport. These
    # fixture tokens are not real credentials, and no socket is opened.
    app = FastAPI()
    def context(request):
        token = request.headers.get("X-Shengbo-Service-Token")
        assert token in {"rc12-fixture-agent", "rc12-fixture-controller"}
        kind = "controller" if token == "rc12-fixture-controller" else "agent"
        return {"principal": Principal(kind=kind, actor_id=kind + "-runtime",
            run_id=request.headers.get("X-Shengbo-Run-Id")), "scope": run["scope"]}
    app.include_router(create_router(service, context))
    client = ShengboClient("http://rc12-isolated", "rc12-fixture-agent", "rc12-fixture-controller",
        transport=httpx.ASGITransport(app=app))
    original_client = engine.client
    engine.client = client
    calls, state = [], {"stage": 0}
    def call(name, args):
        calls.append((name, deepcopy(args)))
        return AIMessage(content="", tool_calls=[{"id": "rc12-correction-" + str(len(calls)), "name": name, "args": args}])

    async def logic(messages):
        stage = state["stage"]
        state["stage"] += 1
        if stage == 0:
            return call("save_script_change_plan", bad_plan)
        last = messages[-1]
        value = json.loads(last.content)
        if stage == 1:
            assert last.status == "error" and value["error"]["code"] == CODE
            details = ScriptPlanModuleBaseInvalidDetails.model_validate(value["error"]["details"], strict=True)
            assert details.commit_state == "not_committed"
            assert details.reason == "BASE_SCRIPT_REQUIRED_FOR_EXPLICIT_MODULE_REUSE"
            assert details.requested_base_script_ref is None
            assert details.requested_base_module_ref.model_dump(mode="json") == original_refs["opening_long"]
            assert value["correction_count"] == 1
            assert value["recovery"]["automatic_action_taken"] is False
            assert value["recovery"]["existing_modules_preserved"] is True
            assert value["recovery"]["explicit_model_action_required"] is True
            assert engine.store.get("run", run["id"])["status"] == "RUNNING"
            assert not engine.store.get("run", run["id"]).get("pending_unknown_operation")
            assert (await service.query("read_preparation", {"preparation_id": prep["ref"]["id"]})) == prep
            assert business_stats(service) == before, "Rejected plan must roll back all Voice writes"
            assert adapter.calls == list(KEYS), "A correction hint must not generate/retry anything"
            assert not engine.store.list("question"), "No approval was invented"
            if recovery == "original_job_retry":
                return call("wf_script_execution", {"action": "retry", "job_id": case["job_id"]})
            good = deepcopy(case["item"])
            good["modules"][-1]["instruction"] = "离线修正CTA，仅理性按需购买，不包含新事实。"
            assert "base_script_ref" not in good and all(a["action"] == "generate" for a in good["modules"])
            return call("save_script_change_plan", {**bad_plan, "items": [good]})
        if recovery == "new_generate_plan_cache" and stage == 2:
            assert last.status != "error"
            state["new_plan_ref"] = value["data"]["script_plan_ref"]
            return call("wf_script_execution", {"action": "execute", "plan_ref": state["new_plan_ref"]})
        expected = 2 if recovery == "original_job_retry" else 3
        assert stage == expected and value["status"] == "WAITING"
        state["recovery_job_id"] = value["result"]["data"]["job_id"]
        return call("finish_turn", {"outcome": "WAITING_JOBS",
            "summary": "模型已显式选择合法恢复并等待离线后台任务，未批准、未生成语音，不宣称整场完成。"})

    try:
        engine.model = ScriptedModel(logic)
        engine.main = engine._build_graph("main")
        await engine._drive_main(run["id"])
        current = engine.store.get("run", run["id"])
        assert current["status"] == "WAITING_JOBS", {"status": current["status"], "calls": [c[0] for c in calls],
            "stage": state["stage"], "error": current.get("error"), "last_error": current.get("last_error"),
            "events": [{"type": e["type"], "data": e["data"]} for e in engine.trace.events(run["id"])["events"][-8:]]}
        assert current["correctable_tool_errors"] == {CODE: 1}
        assert current["goal"] == original_goal and current["allowed_effects"] == original_effects
        assert not current.get("pending_unknown_operation")
        operations = [row for row in engine.store.list("operation") if row["run_id"] == run["id"]]
        rejected = [row for row in operations if row.get("error_code") == CODE]
        assert len(rejected) == 1 and rejected[0]["status"] == "known_failed"
        assert rejected[0]["key"] == "save_script_plan"
        assert rejected[0]["args"]["items"][0].get("base_script_ref") is None
        assert not any(row["status"] in {"sent", "unknown"} for row in operations)
        await service.drain_jobs()
        recovered = await service.query("read_job", {"job_id": state["recovery_job_id"]})
        assert recovered["body"]["status"] == "succeeded"
        recovered_refs = {key: value["module_ref"] for key, value in recovered["body"]["items"][0]["module_results"].items()}
        assert all(recovered_refs[key] == ref for key, ref in original_refs.items())
        assert set(recovered_refs) == set(KEYS)
        assert adapter.calls == [*KEYS, "cta"], "Only failed CTA may call the scripted copywriter again"
        with service.store.transaction() as db:
            assert {key: service.store.exact(db, ref) for key, ref in original_refs.items()} == original_modules
            plans = db.execute("SELECT COUNT(*) FROM records WHERE kind='script_plan'").fetchone()[0]
            audio_jobs = [json.loads(row[0]) for row in db.execute("SELECT body FROM records WHERE kind='prelive_job'")
                          if json.loads(row[0]).get("kind") == "audio"]
            assert not audio_jobs
        assert plans == (1 if recovery == "original_job_retry" else 2)
        assert (state["recovery_job_id"] == case["job_id"]) is (recovery == "original_job_retry")
        final_prep = await service.query("read_preparation", {"preparation_id": prep["ref"]["id"]})
        assert len(final_prep["body"]["script_refs"]) == 1
        assert final_prep["body"]["approval_ref"] is None
        assert final_prep["body"]["audio_manifest_ref"] is None and final_prep["body"]["candidate_publication_ref"] is None
        assert not engine.store.list("question") and adapter.audio_calls == 0
        assert [name for name, _ in calls] == (["save_script_change_plan", "wf_script_execution", "finish_turn"]
            if recovery == "original_job_retry" else ["save_script_change_plan", "save_script_change_plan", "wf_script_execution", "finish_turn"])
    finally:
        engine.client = original_client
        await client.close()
