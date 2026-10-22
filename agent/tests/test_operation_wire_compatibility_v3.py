"""Old persisted operations must survive additive DTO defaults.

The before snapshot creates real isolated Voice and Agent operation records.
No operation hash/receipt is forged or rewritten, and no provider is contacted.
These regressions intentionally fail until wire compatibility is implemented.
"""
import importlib.util
from pathlib import Path
import sys

import pytest

from shengbo_contracts.common import ContractError, MutationContext, Principal, canonical_hash
from shengbo_prelive.client import RemoteOutcomeUnknown
from shengbo_prelive.models import IntentAssessment, RunScope
from shengbo_prelive.runtime import RuntimeEngine
from test_runtime_business import (AIMessage, BindableFakeModel, DirectBusinessClient,
    FakeAdapter, MODULE_KEYS, PreliveService, human_command, new_run, prepared_text)


BEFORE = Path(__file__).resolve().parents[1] / "output/20261020-third-update-implementation/before"


def load_snapshot(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def before_modules():
    business = load_snapshot("shengbo_contracts._operation_wire_before_v3",
        BEFORE / "agent/src/shengbo_contracts/business.py")
    voice = load_snapshot("app.prelive._operation_wire_before_v3",
        BEFORE / "voice/app/prelive/service.py")
    runtime = load_snapshot("shengbo_prelive._operation_wire_before_v3",
        BEFORE / "agent/src/shengbo_prelive/runtime.py")
    policy = load_snapshot("shengbo_prelive._operation_wire_policy_before_v3",
        BEFORE / "agent/src/shengbo_prelive/policy.py")
    # The snapshot's import statements otherwise bind this process's current
    # shared module. Bind the actual historical parser/DTO tables explicitly.
    voice.parse_args, voice.parse_query_args = business.parse_args, business.parse_query_args
    voice.COMMAND_ARGS = business.COMMAND_ARGS
    runtime.COMMAND_ARGS, runtime.QUERY_ARGS = business.COMMAND_ARGS, business.QUERY_ARGS
    runtime.EFFECTS, runtime.validate_intent = policy.EFFECTS, policy.validate_intent
    return business, voice, runtime


async def legacy_args(service, command, preparation, script_ref, *, engine=None, scope=None):
    prep_id = preparation["id"]
    current = await service.query("read_preparation", {"preparation_id": prep_id})
    script = await service.query("read_script", {"ref": script_ref})
    if command == "patch_script":
        return {"preparation_id": prep_id, "expected_revision": current["ref"]["revision"],
            "script_ref": script_ref, "module_key": "selling_points",
            "selected_text_hash": canonical_hash(script["modules"]["selling_points"]["sentences"]),
            "sentences": ["旧版本真实保存的隔离正文。"]}

    # An actual known-failed fake generation produces a retryable persisted job.
    from app.prelive.storage import BusinessError
    async def fail_known(*_args, **_kwargs):
        raise BusinessError("MODULE_RESPONSE_INVALID", "隔离fake已知失败，不是外部请求未知")
    service.adapter.generate_module = fail_known
    plan_args = {"preparation_id": prep_id, "expected_revision": current["ref"]["revision"],
        "bundle_ref": current["body"]["bundle_ref"], "items": [{"product_ref": script["body"]["product_ref"],
            "variant_id": "legacy-retry-variant", "modules": [{"key": key,
                "action": "disable" if key in {"campaign", "transition"} else "generate"} for key in MODULE_KEYS]}]}
    if engine:
        saved = await engine.business_command(scope, "save_script_plan", plan_args, "old-plan")
    else:
        saved = await human_command(service, "save_script_plan", plan_args, operation="old-plan")
    current = await service.query("read_preparation", {"preparation_id": prep_id})
    submit_args = {"preparation_id": prep_id, "expected_revision": current["ref"]["revision"],
        "plan_ref": saved["data"]["script_plan_ref"], "context_bundle_ref": current["body"]["bundle_ref"],
        "expected_generation_epoch": current["body"]["generation_epoch"]}
    if engine:
        submitted = await engine.business_command(scope, "submit_script_change", submit_args, "old-submit")
    else:
        submitted = await human_command(service, "submit_script_change", submit_args, operation="old-submit")
    await service.drain_jobs()
    # Agent jobs need the same exact principal as their real originating run.
    job = await service.query("read_job", {"job_id": submitted["data"]["job_id"]},
        principal=Principal(kind="controller"))
    assert job["body"]["status"] == "failed"
    assert all(item["call_state"] != "timeout_unknown" for item in job["body"]["items"])
    return {"job_id": submitted["data"]["job_id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("command,new_field", [("patch_script", "sentence_ids"), ("retry_failed_items", "recovery_mode")])
async def test_voice_replays_actual_before_operation_without_hash_drift(tmp_path, before_modules, command, new_field):
    business, voice, _runtime = before_modules
    path = tmp_path / "voice"
    old = voice.PreliveService(path, FakeAdapter())
    _room, preparation, script_ref = await prepared_text(old)
    args = await legacy_args(old, command, preparation, script_ref)
    old_wire = business.parse_args(command, args).model_dump(mode="json", exclude_none=True)
    assert new_field not in old_wire
    mutation, principal = MutationContext(operation_id="actual-old-" + command), Principal(kind="human")
    receipt = await old.execute_command(command, old_wire, mutation, principal)
    with old.store.transaction() as c:
        persisted = c.execute("SELECT request_hash,response_json FROM prelive_operations WHERE principal_id=? AND operation_id=?",
            (principal.actor_id, mutation.operation_id)).fetchone()
        assert persisted is not None
        assert persisted["request_hash"] == canonical_hash({"command": command, "args": old_wire, "scope": None})
    await old.close()
    current = PreliveService(path, FakeAdapter())
    try:
        # Correct upgrade behavior is exact replay, not a second mutation.
        assert await current.execute_command(command, old_wire, mutation, principal) == receipt
        from app.prelive.storage import BusinessError
        nondefault = {**old_wire, new_field: ["different-sentence-range"] if command == "patch_script" else "revalidate_archived"}
        with pytest.raises(BusinessError) as changed:
            await current.execute_command(command, nondefault, mutation, principal)
        assert changed.value.code == "IDEMPOTENCY_KEY_REUSED", "New nondefault behavior must not alias the historical request"
    finally:
        await current.close()


class LoseCommittedReceipt(DirectBusinessClient):
    lose_command = None
    async def command(self, key, args, mutation, **kwargs):
        receipt = await super().command(key, args, mutation, **kwargs)
        if key == self.lose_command:
            self.lose_command = None
            raise RemoteOutcomeUnknown(mutation.operation_id)
        return receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("command,new_field", [("patch_script", "sentence_ids"), ("retry_failed_items", "recovery_mode")])
@pytest.mark.parametrize("unknown", [False, True], ids=["committed", "unknown-then-reconciled"])
async def test_agent_replays_before_wire_after_restart(tmp_path, before_modules, command, new_field, unknown):
    business, voice, runtime = before_modules
    old_service = voice.PreliveService(tmp_path / "voice", FakeAdapter())
    old_client = LoseCommittedReceipt(old_service)
    old_engine = runtime.RuntimeEngine(tmp_path / "agent", old_client,
        model=BindableFakeModel(responses=[AIMessage(content="unused")]))
    _room, preparation, script_ref = await prepared_text(old_service)
    run = await new_run(old_engine, "修改本场话术", preparation_id=preparation["id"])
    proposal = IntentAssessment(objective="script_change", summary="旧版目标", authorization_quote=run["input"]["text"])
    run = old_engine.store.update("run", run["id"], intent_assessment=proposal.model_dump())
    await old_engine._freeze_goal(run)
    scope = RunScope(run["id"], "old-invocation", "execute")
    args = await legacy_args(old_service, command, preparation, script_ref, engine=old_engine, scope=scope)
    old_wire = business.parse_args(command, args).model_dump(mode="json", exclude_none=True)
    assert new_field not in old_wire
    if unknown:
        old_client.lose_command = command
        with pytest.raises(RemoteOutcomeUnknown):
            await old_engine.business_command(scope, command, old_wire, "old-tool-call")
    else:
        await old_engine.business_command(scope, command, old_wire, "old-tool-call")
    operation = next(operation for operation in old_engine.store.list("operation")
        if operation["key"] == command and operation.get("args") == old_wire)
    assert operation["request_hash"] == canonical_hash(old_wire)
    assert new_field not in operation["args"]
    assert operation["status"] == ("unknown" if unknown else "committed")
    await old_engine.stop()

    current_service = PreliveService(tmp_path / "voice", FakeAdapter())
    current = RuntimeEngine(tmp_path / "agent", DirectBusinessClient(current_service),
        model=BindableFakeModel(responses=[AIMessage(content="unused")]))
    try:
        if unknown:
            assert await current._reconcile_pending_operation(current.store.get("run", run["id"]))
            assert current.store.get("operation", operation["id"])["status"] == "committed"
        before_replay = current.store.get("operation", operation["id"])
        replayed = await current.business_command(RunScope(run["id"], "new-invocation", "execute"), command, old_wire, "old-tool-call")
        assert replayed == before_replay["receipt"]
        assert current.store.get("operation", operation["id"])["request_hash"] == operation["request_hash"]
        nondefault = {**old_wire, new_field: ["different-sentence-range"] if command == "patch_script" else "revalidate_archived"}
        with pytest.raises(ContractError, match="OPERATION_CHANGED"):
            await current.business_command(RunScope(run["id"], "new-invocation", "execute"), command, nondefault, "old-tool-call")
    finally:
        await current.stop()
