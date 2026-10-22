"""F05 exposes minimal real HITL arguments without doing the next action."""
from copy import deepcopy

import pytest
from langgraph.errors import GraphInterrupt

from shengbo_contracts.common import ContractError, MutationContext, Principal
from shengbo_prelive.models import AnswerRequest, RunScope
from shengbo_prelive.workflows import review_inspection, run_workflow, text_audio_approval_option
from test_optional_workflows import authorize
from test_playback_inspection_rc9 import preparation, ref
from test_runtime_business import env, new_run, prepared_text, stop_at_interrupt


def test_minimal_approval_option_does_not_invent_preview_hash_or_answer():
    option = text_audio_approval_option()
    assert option["tool"] == "request_user_decision"
    assert set(option["arguments"]) == {"kind", "prompt"}
    assert option["arguments"]["kind"] == "text_audio_approval"
    assert option["arguments"]["prompt"].strip()
    assert option["approval_granted"] is False
    assert not {"preview", "review_hash", "target_refs", "answer", "human_receipt", "context"}.intersection(option)
    changed = text_audio_approval_option()
    changed["arguments"]["kind"] = "changed"
    assert text_audio_approval_option() == option


@pytest.mark.parametrize("missing", ["script_refs", "playlist_ref", "compiled_plan_ref", "requirements_ref"])
def test_missing_compile_dependencies_never_offers_approval_as_if_ready(missing):
    p = preparation(script_refs=[ref("script_revision")], playlist_ref=ref("playlist"),
        compiled_plan_ref=ref("compiled_plan"), requirements_ref=ref("requirements"))
    p["body"][missing] = [] if missing == "script_refs" else None
    view = review_inspection(p, [])
    assert not any(a.get("arguments", {}).get("kind") == "text_audio_approval" for a in view["optional_next_actions"])
    assert view["preparation_readiness"] == "not_evaluated"


@pytest.mark.parametrize("approval", [None, ref("approval")])
def test_compiled_inspection_offers_optional_minimal_confirmation_never_certifies_approval(approval):
    p = preparation(script_refs=[ref("script_revision")], playlist_ref=ref("playlist"),
        compiled_plan_ref=ref("compiled_plan"), requirements_ref=ref("requirements"), approval_ref=approval)
    original = deepcopy(p)
    view = review_inspection(p, [])
    assert p == original and view["observed_dependencies"] == p["body"]
    assert view["optional_next_actions"][-1] == text_audio_approval_option()
    assert view["text_review"]["status"] == "not_verified"
    assert view["approval_observation"]["status"] == ("present_not_verified" if approval else "not_observed")
    assert not any(a["tool"] in {"prepare_required_audio", "publish_preparation"} for a in view["optional_next_actions"])


@pytest.mark.asyncio
async def test_real_compile_preserves_exact_receipt_and_adds_no_query_or_hidden_decision(env, monkeypatch):
    engine, service = env
    room, prep, _ = await prepared_text(service)
    run = await new_run(engine, "只编译本场，之后由人检查", room_id=room["id"], preparation_id=prep["id"])
    await authorize(engine, run, ["compile_playback_plan"])
    queries, commands, receipts = [], [], []
    original_query, original_command = engine.client.query, engine.business_command

    async def query(key, *args, **kwargs):
        queries.append(key)
        return await original_query(key, *args, **kwargs)

    async def command(scope, key, *args, **kwargs):
        commands.append(key)
        value = await original_command(scope, key, *args, **kwargs)
        receipts.append(deepcopy(value))
        return value

    monkeypatch.setattr(engine.client, "query", query)
    monkeypatch.setattr(engine, "business_command", command)
    result = await run_workflow(engine, RunScope(run["id"], "compile-entry", "execute"),
        "wf_playback_review", {"action": "compile"})
    assert queries == ["read_preparation"]
    assert commands == ["compile_playback_plan"]
    assert result["status"] == "COMPLETED"
    returned = result["result"]
    assert all(returned[key] == value for key, value in receipts[0].items())
    assert returned["optional_next_actions"] == [text_audio_approval_option()]
    assert returned["approval_status"] == "not_verified"
    assert returned["preparation_readiness"] == "not_evaluated"
    assert not engine.store.list("question")
    current = await service.query("read_preparation", {"preparation_id": prep["id"]})
    assert current["body"]["approval_ref"] is current["body"]["audio_manifest_ref"] is None
    assert current["body"]["candidate_publication_ref"] is None


@pytest.mark.asyncio
async def test_minimal_entry_uses_backend_current_preview_and_rejects_forged_approval(env, monkeypatch):
    engine, service = env
    room, prep, _ = await prepared_text(service)
    run = await new_run(engine, "准备语音", room_id=room["id"], preparation_id=prep["id"])
    engine.store.update("run", run["id"], phase="execute", status="RUNNING")
    scope = RunScope(run["id"], "minimal-approval-entry", "execute")
    monkeypatch.setattr("shengbo_prelive.runtime.interrupt", stop_at_interrupt)
    with pytest.raises(GraphInterrupt):
        await engine.request_decision(scope, text_audio_approval_option()["arguments"], "minimal-approval-call")
    question, = engine.get_run(run["id"])["questions"]
    current = await service.query("create_text_review", {"preparation_id": prep["id"]})
    assert question["preview"] == current
    assert question["approval_binding"]["review_hash"] == current["review_hash"]
    assert question["status"] == "OPEN"
    forged = AnswerRequest(question_revision=question["revision"], answer="approve", client_request_id="fake-answer",
        human_receipt={"operation_id": question["approval_binding"]["operation_id"], "status": "approved"})
    with pytest.raises(ContractError, match="TEXT_APPROVAL_MISSING"):
        await engine.answer(question["id"], forged)
    assert engine.store.get("question", question["id"])["status"] == "OPEN"
    assert not engine.store.list("operation")
