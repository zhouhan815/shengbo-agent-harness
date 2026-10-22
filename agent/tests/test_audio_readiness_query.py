"""Real voice service contract tests. Synthetic PCM files are not live TTS."""
from copy import deepcopy
import hashlib
from pathlib import Path
import wave

import pytest
from pydantic import ValidationError

from shengbo_contracts.business import QUERY_ARGS
from shengbo_contracts.common import Principal, WorkScope
from shengbo_prelive.models import RunScope
from shengbo_prelive.policy import READ_TOOLS
from shengbo_prelive.tools import QUERY_MAP, build_tools
from test_runtime_business import env, FakeAdapter, BusinessError, human_command, new_run, prepared_text


class PCMAdapter(FakeAdapter):
    def __init__(self, root):
        self.root = root
        self.rendered = 0

    async def generate_module(self, context, module_key, product, previous):
        # One saved occurrence now uses one opening. Keep the intended 64-file
        # coverage/paging stress case without generating an unused second one.
        count = 8 if module_key in {"opening_long", "opening_short"} else 24 if module_key == "cta" else 16
        return [f"{product['name']}，{module_key}的第{index + 1}句隔离测试口播。" for index in range(count)]

    async def render_audio(self, text, room_id, voice):
        self.rendered += 1
        identity = hashlib.sha256(text.encode()).hexdigest()
        path = self.root / "room_audio" / room_id / (identity + ".wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\x01\x00\xff\xff" * 800)
        return {"path": str(path), "audio_id": identity, "url": "/test/" + identity + ".wav", "duration": .1}


@pytest.fixture
async def ready_audio(env):
    engine, service = env
    adapter = PCMAdapter(service.runtime)
    service.adapter = adapter
    room, prep, _ = await prepared_text(service)
    review = await service.query("create_text_review", {"preparation_id": prep["id"]})
    assert len(review["texts"]) == 64
    approval = await human_command(service, "approve_text_for_audio", {"preparation_id": prep["id"],
        "expected_revision": prep["revision"], "review_hash": review["review_hash"]})
    await human_command(service, "prepare_required_audio", {"preparation_id": prep["id"],
        "expected_revision": approval["data"]["preparation_ref"]["revision"],
        "approval_ref": approval["data"]["approval_ref"], "review_hash": review["review_hash"],
        "expected_generation_epoch": approval["data"]["generation_epoch"]})
    await service.drain_jobs()
    prep = await service.query("read_preparation", {"preparation_id": prep["id"]})
    assert prep["body"]["audio_manifest_ref"] and adapter.rendered == 64
    return engine, service, adapter, room, prep


def data_snapshot(service):
    with service.store.transaction() as connection:
        return {table: [tuple(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            for table in ("records", "record_revisions", "prelive_operations", "prelive_events")}


@pytest.mark.asyncio
async def test_real_voice_readiness_reports_all_64_files_with_paging_and_no_writes(ready_audio):
    engine, service, adapter, room, prep = ready_audio
    before = data_snapshot(service)
    scope = WorkScope(kind="preparation", preparation_id=prep["ref"]["id"])
    first = await service.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"], "limit": 2},
        principal=Principal(kind="agent", run_id="readonly-audio-reader"), scope=scope)
    assert first["ready_for_publish"] is True and first["blockers"] == []
    assert first["approval"]["status"] == "current"
    for key in ("approval_ref", "requirements_ref", "audio_manifest_ref"):
        assert first[key] == prep["body"][key]
    assert first["preparation_ref"] == prep["ref"] and first["generation_epoch"] == prep["body"]["generation_epoch"]
    assert first["counts"] == {"required": 64, "manifest_assets": 64, "covered": 64, "file_valid": 64, "hash_present": 64,
        "voice_matches": 64, "style_matches": 64, "spoken_text_matches": 64, "duplicate_requirement_ids": 0,
        "duplicate_asset_ids": 0, "unexpected_assets": 0, "unknown_job_items": 0, "pending_jobs": 0}
    assert first["total"] == 64 and len(first["items"]) == 2 and first["next_cursor"] == 2
    last = await service.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"], "cursor": 62, "limit": 20}, scope=scope)
    assert len(last["items"]) == 2 and last["next_cursor"] is None
    assert first["counts"] == last["counts"]
    assert before == data_snapshot(service) and adapter.rendered == 64
    # The context reader remains closed; the new query is the legitimate path.
    with pytest.raises(BusinessError, match="WRONG_ARTIFACT_KIND"):
        await service.query("read_context", {"ref": first["audio_manifest_ref"]}, scope=scope)
    assert "read_audio_readiness" in READ_TOOLS and QUERY_MAP["read_audio_readiness"] == "read_audio_readiness"
    assert "read_audio_readiness" in {tool.name for tool in build_tools(engine)}
    run = await new_run(engine, "只读核对本场语音就绪", room_id=room["id"], preparation_id=prep["ref"]["id"])
    engine.store.update("run", run["id"], status="RUNNING", phase="execute")
    result = await engine.business_query(RunScope(run["id"], "real-readiness-query", "execute"), "read_audio_readiness",
        {"preparation_id": prep["ref"]["id"], "limit": 2})
    assert result["ready_for_publish"] and result["counts"]["file_valid"] == 64
    assert engine.store.list("query_evidence")[-1]["result"] == result
    assert before == data_snapshot(service) and adapter.rendered == 64


def add_unknown_job(service, prep, *, kind="scripts", later=False, target="current", purpose="copywriting.whole_script"):
    """An isolated historical loss, not a real request or a retry permission."""
    with service.store.transaction(write=True) as connection:
        current = service._artifact(connection, prep["body"]["script_refs"][0])["body"]
        source = service.store.get(connection, "prelive_job", current["generation_provenance"]["job_id"])["body"]
        spec = deepcopy(source["items"][0]["input"])
        moment = "2100-01-01T00:00:00+00:00" if later else "2000-01-01T00:00:00+00:00"
        if kind in {"audio", "preview"}:
            requirements = service._artifact(connection, prep["body"]["requirements_ref"])["body"]
            spec = deepcopy(requirements["items"][0])
            if target == "other":
                spec["requirement_id"] = "historical-unselected-audio"
        elif target == "other":
            spec["variant_id"] = "historical-unselected-variant"
        body = {"id": "historical-unknown-job", "kind": kind, "preparation_id": prep["ref"]["id"],
            "status": "interrupted", "created_at": moment,
            "requirements_ref": {"kind": "requirements", "id": "old-requirements"},
            "items": [{"item_id": "historical-item", "status": "interrupted", "call_state": "timeout_unknown",
                "input": spec, "sent_at": moment, "output": None,
                "provider_attempts": [{"attempt_id": "historical-attempt", "state": "timeout_unknown",
                    "purpose": purpose if kind == "scripts" else "tts", "sent_at": moment}]}]}
        service.store.put(connection, "prelive_job", body["id"], body, create=True)
        return body


@pytest.mark.asyncio
async def test_later_exact_committed_script_does_not_turn_old_writing_loss_into_audio_blocker(ready_audio):
    _, service, adapter, _, prep = ready_audio
    add_unknown_job(service, prep)
    before = data_snapshot(service)
    result = await service.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"], "limit": 1})
    assert result["ready_for_publish"] and result["counts"]["unknown_job_items"] == 0
    history = result["job_outcomes"]["historical_unknown_script_items"]
    assert len(history) == 1 and history[0]["provider_outcome"] == "still_unknown"
    assert history[0]["unknown_attempt_ids"] == ["historical-attempt"] and not history[0]["retry_authorized"]
    assert history[0]["selected_script_ref"] == prep["body"]["script_refs"][0]
    assert before == data_snapshot(service) and adapter.rendered == 64
    # The command shares the classification; no new provider call is needed to
    # prove that the valid candidate can be published.
    published = await human_command(service, "publish_preparation", {"preparation_id": prep["ref"]["id"],
        "expected_revision": prep["ref"]["revision"], "approval_ref": prep["body"]["approval_ref"]})
    assert published["data"]["publication_ref"]
    with service.store.transaction() as connection:
        review = service._publication_review(connection, published["data"]["publication_ref"])
    assert review["currentness"]["blockers"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["no_later_commit", "wrong_output_ref", "unrelated_module", "unknown_replacement",
    "unknown_tts", "missing_provenance", "no_attempt_scope"])
async def test_incomplete_or_current_unknowns_remain_blocking_at_query_and_commands(ready_audio, fault):
    _, service, adapter, _, prep = ready_audio
    add_unknown_job(service, prep, kind="audio" if fault == "unknown_tts" else "scripts",
        later=fault == "no_later_commit")
    with service.store.transaction(write=True) as connection:
        current = service._artifact(connection, prep["body"]["script_refs"][0])["body"]
        job = service.store.get(connection, "prelive_job", current["generation_provenance"]["job_id"])
        changed = deepcopy(job["body"])
        item = changed["items"][0]
        if fault == "wrong_output_ref":
            item["output"]["script_ref"] = {"kind": "script_revision", "id": "unselected-result"}
        elif fault == "unrelated_module":
            for action in item["input"]["modules"]:
                action["action"] = "generate" if action["key"] == "opening_short" else "reuse"
        elif fault == "unknown_replacement":
            item.setdefault("provider_attempts", []).append({"state": "outcome_unknown"})
        elif fault in {"missing_provenance", "no_attempt_scope"}:
            old = service.store.get(connection, "prelive_job", "historical-unknown-job")
            source = deepcopy(old["body"])
            if fault == "missing_provenance":
                source["items"][0]["input"].pop("product_ref")
            else:
                source["items"][0]["provider_attempts"][0].pop("purpose")
            service.store.put(connection, "prelive_job", old["ref"]["id"], source, expected=old["ref"]["revision"])
        if changed != job["body"]:
            service.store.put(connection, "prelive_job", job["ref"]["id"], changed, expected=job["ref"]["revision"])
    before = data_snapshot(service)
    ready = await service.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"]})
    assert not ready["ready_for_publish"] and ready["counts"]["unknown_job_items"] >= 1
    for command, extra in (("publish_preparation", {}), ("prepare_required_audio", {
            "expected_generation_epoch": prep["body"]["generation_epoch"],
            "review_hash": ready["approval"]["review_hash"]})):
        with pytest.raises(BusinessError) as error:
            await human_command(service, command, {"preparation_id": prep["ref"]["id"],
                "expected_revision": prep["ref"]["revision"], "approval_ref": prep["body"]["approval_ref"], **extra})
        assert error.value.code == "EXTERNAL_OUTCOME_UNKNOWN"
    assert before == data_snapshot(service) and adapter.rendered == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["scripts", "preview"])
async def test_off_dependency_historical_unknown_is_visible_without_blocking_current_package(ready_audio, kind):
    _, service, adapter, _, prep = ready_audio
    add_unknown_job(service, prep, kind=kind, target="other")
    before = data_snapshot(service)
    ready = await service.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"]})
    assert ready["ready_for_publish"] and ready["counts"]["unknown_job_items"] == 0
    assert len(ready["job_outcomes"]["unrelated_historical_unknown_items"]) == 1
    assert before == data_snapshot(service) and adapter.rendered == 64


@pytest.mark.asyncio
async def test_active_job_still_blocks_and_unknown_after_publication_blocks_review(ready_audio):
    _, service, adapter, _, prep = ready_audio
    published = await human_command(service, "publish_preparation", {"preparation_id": prep["ref"]["id"],
        "expected_revision": prep["ref"]["revision"], "approval_ref": prep["body"]["approval_ref"]})
    prep = await service.query("read_preparation", {"preparation_id": prep["ref"]["id"]})
    add_unknown_job(service, prep, kind="audio")
    with service.store.transaction() as connection:
        with pytest.raises(BusinessError) as error:
            service._publication_review(connection, published["data"]["publication_ref"])
        assert error.value.code == "EXTERNAL_OUTCOME_UNKNOWN"
    with service.store.transaction(write=True) as connection:
        old = service.store.get(connection, "prelive_job", "historical-unknown-job")
        body = deepcopy(old["body"])
        body["status"] = "running"
        body["items"][0].update(call_state="not_sent", provider_attempts=[])
        service.store.put(connection, "prelive_job", old["ref"]["id"], body, expected=old["ref"]["revision"])
    before = data_snapshot(service)
    ready = await service.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"]})
    assert not ready["ready_for_publish"] and ready["counts"]["pending_jobs"] == 1
    assert ready["counts"]["unknown_job_items"] == 0
    with pytest.raises(BusinessError) as error:
        await human_command(service, "publish_preparation", {"preparation_id": prep["ref"]["id"],
            "expected_revision": prep["ref"]["revision"], "approval_ref": prep["body"]["approval_ref"]})
    assert error.value.code == "BUSINESS_JOB_PENDING"
    assert before == data_snapshot(service) and adapter.rendered == 64


@pytest.mark.asyncio
async def test_late_corrupt_file_is_not_hidden_by_the_first_detail_page(ready_audio):
    _, service, adapter, _, prep = ready_audio
    with service.store.transaction() as connection:
        manifest = service._artifact(connection, prep["body"]["audio_manifest_ref"])["body"]
    Path(manifest["assets"][-1]["path"]).write_bytes(b"corrupt-not-wave")
    result = await service.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"], "limit": 1})
    assert not result["ready_for_publish"]
    assert result["counts"]["covered"] == 64 and result["counts"]["file_valid"] == 63
    assert result["items"][0]["file_valid"] is True
    assert any(blocker["code"] == "AUDIO_COVERAGE_OR_FILE_INVALID" for blocker in result["blockers"])
    assert adapter.rendered == 64


@pytest.mark.asyncio
async def test_revoked_approval_blocks_readiness_without_reapproving_or_rendering(ready_audio):
    _, service, adapter, _, prep = ready_audio
    await human_command(service, "revoke_text_approval", {"approval_ref": prep["body"]["approval_ref"], "reason": "隔离测试撤销"})
    before = data_snapshot(service)
    result = await service.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"]})
    assert not result["ready_for_publish"] and result["approval"]["status"] == "revoked"
    assert result["counts"]["file_valid"] == 64
    assert before == data_snapshot(service) and adapter.rendered == 64


@pytest.mark.asyncio
async def test_readiness_scope_and_input_contract_are_closed(ready_audio):
    _, service, _, _, prep = ready_audio
    for scope in (WorkScope(kind="catalog"), WorkScope(kind="preparation", preparation_id="another-preparation")):
        with pytest.raises(BusinessError) as caught:
            await service.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"]},
                principal=Principal(kind="agent", run_id="foreign-reader"), scope=scope)
        assert caught.value.status == 403 and caught.value.code == "SCOPE_CONFLICT"
    for args in ({}, {"preparation_id": prep["ref"]["id"], "ref": prep["body"]["approval_ref"]},
        {"preparation_id": prep["ref"]["id"], "cursor": -1}):
        with pytest.raises(ValidationError):
            QUERY_ARGS["read_audio_readiness"].model_validate(args)


@pytest.mark.asyncio
async def test_new_preparation_readiness_lists_missing_evidence_without_side_effects(env):
    _, service = env
    room = (await human_command(service, "create_room", {"name": "尚未准备"}))["data"]["room_ref"]
    prep = (await human_command(service, "create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["data"]["preparation_ref"]
    before = data_snapshot(service)
    result = await service.query("read_audio_readiness", {"preparation_id": prep["id"]})
    assert not result["ready_for_publish"] and result["counts"]["required"] == 0
    assert {blocker["code"] for blocker in result["blockers"]} >= {"TEXT_APPROVAL_NOT_CURRENT", "AUDIO_REQUIREMENTS_MISSING", "AUDIO_MANIFEST_MISSING"}
    assert before == data_snapshot(service)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "duplicate", "voice", "spoken_hash", "missing_file_hash", "binding", "foreign_scope", "unknown"])
async def test_manifest_identity_coverage_and_hidden_unknown_faults_are_not_ready(ready_audio, fault):
    _, service, adapter, _, prep = ready_audio
    with service.store.transaction(write=True) as connection:
        if fault == "unknown":
            service.store.put(connection, "prelive_job", "late-unknown-job", {"preparation_id": prep["ref"]["id"],
                "status": "interrupted", "items": [{"call_state": "result_received", "provider_attempts": [
                    {"state": "result_received"} for _ in range(30)] + [{"state": "outcome_unknown"}]}]}, create=True)
        else:
            manifest = deepcopy(service._artifact(connection, prep["body"]["audio_manifest_ref"])["body"])
            if fault == "missing":
                manifest["assets"].pop()
            elif fault == "duplicate":
                manifest["assets"].append(deepcopy(manifest["assets"][-1]))
            elif fault == "voice":
                manifest["assets"][-1]["voice"] = {"provider": "another", "voice": "wrong"}
            elif fault == "spoken_hash":
                manifest["assets"][-1]["spoken_text_hash"] = "sha256:" + "0" * 64
            elif fault == "missing_file_hash":
                manifest["assets"][-1].pop("sha256_bytes")
            elif fault == "binding":
                manifest["approval_ref"] = None
            else:
                manifest["preparation_id"] = "foreign-preparation"
            manifest_ref = service.store.artifact(connection, "audio_manifest", manifest)
            current = service._prep(connection, prep["ref"]["id"])
            body = deepcopy(current["body"])
            body["audio_manifest_ref"] = manifest_ref
            service._save_prep(connection, current, body, invalidate=False)
    before = data_snapshot(service)
    if fault == "foreign_scope":
        with pytest.raises(BusinessError) as caught:
            await service.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"], "limit": 1})
        assert caught.value.status == 403
    else:
        result = await service.query("read_audio_readiness", {"preparation_id": prep["ref"]["id"], "limit": 1})
        assert not result["ready_for_publish"] and result["blockers"]
        if fault == "unknown":
            assert result["counts"]["unknown_job_items"] == 1
        if fault == "missing_file_hash":
            assert result["counts"]["hash_present"] == result["counts"]["file_valid"] == 63
    assert before == data_snapshot(service) and adapter.rendered == 64
