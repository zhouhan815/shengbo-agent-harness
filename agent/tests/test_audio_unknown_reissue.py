"""Explicit one-time audio reissue against temporary DBs and synthetic PCM only.

The first fake provider call deliberately loses its result. All subsequent
successes write a tiny local WAV; no SDK, credentials or live service is used.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
from pathlib import Path
import wave

import pytest
from pydantic import ValidationError

from shengbo_contracts.common import MutationContext, Principal, WorkScope
from test_runtime_business import (
    BusinessError, FakeAdapter, PreliveService, human_command, prepared_text,
)


class ReissuePCMAdapter(FakeAdapter):
    """Count real invocation boundaries without calling a real provider."""

    def __init__(self, root):
        self.root = Path(root)
        self.calls = Counter()
        self.fail_next = 1

    async def render_audio(self, text, room_id, voice, *, style=None):
        self.calls[text] += 1
        if self.fail_next:
            self.fail_next -= 1
            raise TimeoutError("isolated fake outcome is unknown")
        identity = hashlib.sha256(text.encode()).hexdigest()
        path = self.root / "fake_audio" / room_id / (identity + ".wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b"\x01\x00\xff\xff" * 800)
        return {"path": str(path), "audio_id": identity,
            "url": "/isolated-test/" + identity + ".wav", "duration": .1}


def stored(service, kind, identity):
    with service.store.transaction() as db:
        return deepcopy(service.store.get(db, kind, identity))


def db_snapshot(service):
    with service.store.transaction() as db:
        return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            for table in ("records", "record_revisions", "prelive_operations", "prelive_events")}


async def inspect(service, case):
    return await service.query("inspect_missing_audio_reissue", {
        "preparation_id": case["preparation_id"], "job_id": case["source_job_id"]})


def approval_arguments(view):
    return {"preparation_id": view["preparation_ref"]["id"],
        "expected_revision": view["preparation_ref"]["revision"],
        **{key: deepcopy(view[key]) for key in ("source_job_ref", "approval_ref", "requirements_ref",
            "review_hash", "expected_generation_epoch", "unknown_items", "binding_hash")},
        "authorization_quote": "我知道旧请求可能已经计费，同意只对这些缺失句每句补合成一次。"}


@pytest.fixture
async def unknown_audio(tmp_path):
    adapter = ReissuePCMAdapter(tmp_path / "voice")
    service = PreliveService(tmp_path / "voice", adapter, minimax_audio_concurrency=1)
    try:
        room, prep, _ = await prepared_text(service)
        review = await service.query("create_text_review", {"preparation_id": prep["id"]})
        approval = await human_command(service, "approve_text_for_audio", {
            "preparation_id": prep["id"], "expected_revision": prep["revision"],
            "review_hash": review["review_hash"]})
        submitted = await human_command(service, "prepare_required_audio", {
            "preparation_id": prep["id"],
            "expected_revision": approval["data"]["preparation_ref"]["revision"],
            "approval_ref": approval["data"]["approval_ref"], "review_hash": review["review_hash"],
            "expected_generation_epoch": approval["data"]["generation_epoch"]})
        await service.drain_jobs()
        case = {"service": service, "adapter": adapter, "room": room,
            "preparation_id": prep["id"], "source_job_id": submitted["data"]["job_id"]}
        source = stored(service, "prelive_job", case["source_job_id"])
        unknowns = [item for item in source["body"]["items"] if item["call_state"] == "timeout_unknown"]
        assert len(unknowns) == 1
        assert all(item["status"] not in {"queued", "running"} for item in source["body"]["items"])
        case.update(source_before=source, unknown_before=deepcopy(unknowns[0]),
            calls_before=deepcopy(adapter.calls))
        yield case
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_inspection_is_read_only_and_binds_exact_final_missing_sentence(unknown_audio):
    case = unknown_audio
    service = case["service"]
    before = db_snapshot(service)
    view = await inspect(service, case)
    assert view["status"] == "ready_for_explicit_approval" and view["blockers"] == []
    assert view["missing_count"] == len(view["unknown_items"]) == 1
    assert view["source_job_ref"] == case["source_before"]["ref"]
    unknown = case["unknown_before"]
    assert view["unknown_items"] == [{"item_id": unknown["item_id"],
        "requirement_id": unknown["input"]["requirement_id"],
        "unknown_attempt_ids": [a["attempt_id"] for a in unknown["provider_attempts"]
            if a["state"] in {"sent", "timeout_unknown", "outcome_unknown"}]}]
    assert view["binding_hash"]
    assert before == db_snapshot(service) and case["adapter"].calls == case["calls_before"]


@pytest.mark.asyncio
@pytest.mark.parametrize("principal_kind", ["agent", "controller"])
async def test_only_a_human_can_authorize_unknown_audio_reissue(unknown_audio, principal_kind):
    case = unknown_audio
    service = case["service"]
    args = approval_arguments(await inspect(service, case))
    before = db_snapshot(service)
    with pytest.raises(BusinessError) as error:
        await service.execute_command("approve_missing_audio_reissue", args,
            MutationContext(operation_id="forbidden-reissue"),
            Principal(kind=principal_kind, run_id="not-a-human"),
            scope=WorkScope(kind="preparation", preparation_id=case["preparation_id"]))
    assert error.value.code == "HUMAN_REQUIRED" and error.value.status == 403
    assert before == db_snapshot(service) and case["adapter"].calls == case["calls_before"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["item", "requirement", "attempt", "requirements_ref", "source_ref", "review", "epoch", "binding", "empty", "too_many", "quote"])
async def test_unknown_reissue_rejects_wrong_or_unapproved_input_without_any_provider_call(unknown_audio, fault):
    case = unknown_audio
    service = case["service"]
    args = approval_arguments(await inspect(service, case))
    if fault == "item":
        args["unknown_items"][0]["item_id"] = "foreign-item"
    elif fault == "requirement":
        args["unknown_items"][0]["requirement_id"] = "foreign-requirement"
    elif fault == "attempt":
        args["unknown_items"][0]["unknown_attempt_ids"] = ["foreign-attempt"]
    elif fault == "requirements_ref":
        args["requirements_ref"]["content_hash"] = "sha256:" + "0" * 64
    elif fault == "source_ref":
        args["source_job_ref"]["content_hash"] = "sha256:" + "0" * 64
    elif fault == "review":
        args["review_hash"] = "sha256:" + "0" * 64
    elif fault == "epoch":
        args["expected_generation_epoch"] += 1
    elif fault == "binding":
        args["binding_hash"] = "sha256:" + "0" * 64
    elif fault == "empty":
        args["unknown_items"] = []
    elif fault == "too_many":
        args["unknown_items"] = [{**deepcopy(args["unknown_items"][0]), "item_id": "item-" + str(n)} for n in range(11)]
    else:
        args["authorization_quote"] = ""
    before = db_snapshot(service)
    with pytest.raises((BusinessError, ValidationError)):
        await human_command(service, "approve_missing_audio_reissue", args, operation="wrong-input-" + fault)
    assert before == db_snapshot(service) and case["adapter"].calls == case["calls_before"]


@pytest.mark.asyncio
async def test_inspection_rejects_foreign_scope_and_source_job_that_has_not_finished(unknown_audio):
    case = unknown_audio
    service = case["service"]
    args = {"preparation_id": case["preparation_id"], "job_id": case["source_job_id"]}
    before = db_snapshot(service)
    with pytest.raises(BusinessError) as denied:
        await service.query("inspect_missing_audio_reissue", args,
            principal=Principal(kind="agent", run_id="foreign-reader"),
            scope=WorkScope(kind="preparation", preparation_id="another-preparation"))
    assert denied.value.status == 403 and denied.value.code == "SCOPE_CONFLICT"
    assert before == db_snapshot(service)
    # Synthetic mutation only: simulate the very short period while the source
    # worker still has unfinished items. A human cannot approve missing results
    # while their original requests are still in flight.
    with service.store.transaction(write=True) as db:
        row = service.store.get(db, "prelive_job", case["source_job_id"])
        changed = deepcopy(row["body"])
        changed["status"] = "running"
        service.store.put(db, "prelive_job", row["ref"]["id"], changed, expected=row["ref"]["revision"])
    before = db_snapshot(service)
    view = await inspect(service, case)
    assert view["status"] == "blocked" and view["blockers"]
    assert before == db_snapshot(service) and case["adapter"].calls == case["calls_before"]


@pytest.mark.asyncio
async def test_an_unrelated_missing_cached_file_prevents_partial_reissue_approval(unknown_audio):
    case = unknown_audio
    service = case["service"]
    old_view = await inspect(service, case)
    original = case["source_before"]["body"]
    completed = next(item for item in original["items"] if item["status"] == "succeeded")
    Path(completed["output"]["asset"]["path"]).unlink()
    before = db_snapshot(service)
    view = await inspect(service, case)
    assert view["status"] == "blocked" and view["missing_count"] == 2 and view["blockers"]
    with pytest.raises(BusinessError):
        await human_command(service, "approve_missing_audio_reissue", approval_arguments(old_view),
            operation="cannot-approve-only-one-of-two-missing")
    assert before == db_snapshot(service) and case["adapter"].calls == case["calls_before"]


@pytest.mark.asyncio
async def test_success_only_reissues_missing_audio_merges_manifest_and_keeps_old_unknown_ledger(unknown_audio):
    case = unknown_audio
    service, adapter = case["service"], case["adapter"]
    view = await inspect(service, case)
    old_attempt = case["unknown_before"]["provider_attempts"][-1]
    old_index = stored(service, "prelive_external_request", old_attempt["request_key"])
    result = await human_command(service, "approve_missing_audio_reissue", approval_arguments(view), operation="approve-once")
    assert result["data"]["item_count"] == 1
    assert result["data"]["reissue_approval_ref"] and result["data"]["binding_hash"] == view["binding_hash"]
    await service.drain_jobs()
    child = stored(service, "prelive_job", result["data"]["job_id"])["body"]
    assert child["kind"] == "audio" and child["status"] == "succeeded" and len(child["items"]) == 1
    assert child["audio_reissue_approval_ref"] == result["data"]["reissue_approval_ref"]
    new_attempt = child["items"][0]["provider_attempts"][-1]
    assert new_attempt["request_key"] != old_attempt["request_key"]
    assert new_attempt["state"] == "result_received"
    expected_calls = case["calls_before"].copy()
    expected_calls[case["unknown_before"]["input"]["spoken_text"]] += 1
    assert adapter.calls == expected_calls
    assert stored(service, "prelive_job", case["source_job_id"]) == case["source_before"]
    assert stored(service, "prelive_external_request", old_attempt["request_key"]) == old_index
    ready = await service.query("read_audio_readiness", {"preparation_id": case["preparation_id"]})
    assert ready["ready_for_publish"] and ready["blockers"] == []
    assert ready["counts"]["covered"] == ready["counts"]["required"] == sum(case["calls_before"].values())
    assert ready["counts"]["file_valid"] == ready["counts"]["required"]
    assert ready["counts"]["unknown_job_items"] == 0
    # The successful replacement satisfies current requirements; it must not
    # rewrite the original provider attempt's unknown state or unknown usage.
    assert old_attempt["state"] == "timeout_unknown" and old_attempt["usage_state"] == "unknown"


@pytest.mark.asyncio
async def test_double_click_and_service_restart_never_send_the_same_reissue_twice(unknown_audio):
    case = unknown_audio
    service, adapter = case["service"], case["adapter"]
    args = approval_arguments(await inspect(service, case))
    clicks = await asyncio.gather(*[human_command(service, "approve_missing_audio_reissue", args,
        operation="independent-click-" + str(n)) for n in range(2)], return_exceptions=True)
    successes = [result for result in clicks if isinstance(result, dict)]
    assert len(successes) == 1 and isinstance(next(result for result in clicks if not isinstance(result, dict)), BusinessError)
    accepted_index = clicks.index(successes[0])
    await service.drain_jobs()
    calls_after = adapter.calls.copy()
    assert sum(calls_after.values()) == sum(case["calls_before"].values()) + 1
    await service.close()
    restarted = PreliveService(service.runtime, adapter, minimax_audio_concurrency=1)
    try:
        replay = await human_command(restarted, "approve_missing_audio_reissue", args,
            operation="independent-click-" + str(accepted_index))
        assert replay == successes[0]
        with pytest.raises(BusinessError):
            await human_command(restarted, "approve_missing_audio_reissue", args, operation="click-after-restart")
        await restarted.drain_jobs()
        assert adapter.calls == calls_after
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_a_second_unknown_stays_blocking_and_cannot_be_automatically_reissued(unknown_audio):
    case = unknown_audio
    service, adapter = case["service"], case["adapter"]
    adapter.fail_next = 1
    result = await human_command(service, "approve_missing_audio_reissue",
        approval_arguments(await inspect(service, case)), operation="authorized-but-lost")
    await service.drain_jobs()
    calls_after = adapter.calls.copy()
    child = stored(service, "prelive_job", result["data"]["job_id"])["body"]
    assert child["items"][0]["call_state"] == "timeout_unknown"
    ready = await service.query("read_audio_readiness", {"preparation_id": case["preparation_id"]})
    assert not ready["ready_for_publish"] and ready["counts"]["unknown_job_items"] >= 1
    for job_id in (case["source_job_id"], result["data"]["job_id"]):
        with pytest.raises(BusinessError) as error:
            await human_command(service, "retry_failed_items", {"job_id": job_id}, operation="ordinary-retry-" + job_id)
        assert error.value.code == ("EXTERNAL_OUTCOME_UNKNOWN" if job_id == case["source_job_id"]
            else "AUDIO_REISSUE_ONCE_ONLY")
    await service.drain_jobs()
    assert adapter.calls == calls_after
    again = await inspect(service, case)
    assert again["status"] == "blocked" and again["blockers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["approval", "voice"])
async def test_changed_approval_or_voice_invalidates_the_old_reissue_binding(unknown_audio, change):
    case = unknown_audio
    service = case["service"]
    args = approval_arguments(await inspect(service, case))
    prep = await service.query("read_preparation", {"preparation_id": case["preparation_id"]})
    if change == "approval":
        await human_command(service, "approve_text_for_audio", {"preparation_id": case["preparation_id"],
            "expected_revision": prep["ref"]["revision"], "review_hash": args["review_hash"]}, operation="replace-text-approval")
    else:
        with service.store.transaction() as db:
            draft = deepcopy(service._artifact(db, prep["body"]["draft_ref"])["body"]["draft"])
        draft["tts_selection"] = {"provider": "minimax", "voice": "a-different-isolated-voice"}
        await human_command(service, "save_session_draft", {"preparation_id": case["preparation_id"],
            "expected_revision": prep["ref"]["revision"], "draft": draft}, operation="change-voice")
    # Pass the new revision so rejection must be about the frozen approval /
    # requirements rather than merely an optimistic-lock mismatch.
    args["expected_revision"] = (await service.query("read_preparation", {
        "preparation_id": case["preparation_id"]}))["ref"]["revision"]
    before = db_snapshot(service)
    with pytest.raises(BusinessError):
        await human_command(service, "approve_missing_audio_reissue", args, operation="stale-binding-" + change)
    assert before == db_snapshot(service) and case["adapter"].calls == case["calls_before"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tampered_field", ["asset_ref", "approval_ref"])
async def test_tampered_resolution_cannot_hide_the_original_unknown(unknown_audio, tampered_field):
    case = unknown_audio
    service = case["service"]
    await human_command(service, "approve_missing_audio_reissue",
        approval_arguments(await inspect(service, case)), operation="success-before-tamper")
    await service.drain_jobs()
    ready = await service.query("read_audio_readiness", {"preparation_id": case["preparation_id"]})
    assert ready["ready_for_publish"]
    with service.store.transaction(write=True) as db:
        resolution = service.store.all(db, "audio_reissue_resolution")[0]
        altered = deepcopy(resolution["body"])
        if tampered_field == "asset_ref":
            # A real but different successful sentence is not replacement
            # evidence for the exact missing requirement.
            altered["asset_ref"] = next(item["output"]["asset_ref"]
                for item in case["source_before"]["body"]["items"] if item["status"] == "succeeded")
        else:
            # A real text approval is not the human-only paid-reissue receipt.
            prep = service._prep(db, case["preparation_id"])
            altered["approval_ref"] = prep["body"]["approval_ref"]
        service.store.put(db, "audio_reissue_resolution", resolution["ref"]["id"], altered,
            expected=resolution["ref"]["revision"])
    before = db_snapshot(service)
    not_ready = await service.query("read_audio_readiness", {"preparation_id": case["preparation_id"]})
    assert not not_ready["ready_for_publish"] and not_ready["counts"]["unknown_job_items"] >= 1
    assert before == db_snapshot(service)
    assert stored(service, "prelive_job", case["source_job_id"]) == case["source_before"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fake_resolution", [False, True])
async def test_reserved_slot_or_forged_resolution_cannot_claim_a_successful_replacement(unknown_audio, fake_resolution):
    case = unknown_audio
    service = case["service"]
    # Keep the newly queued child genuinely unexecuted. Disabling this instance's
    # wake hook never touches production or worker implementation.
    service.wake_worker = lambda: None
    accepted = await human_command(service, "approve_missing_audio_reissue",
        approval_arguments(await inspect(service, case)), operation="reserved-not-completed")
    child = stored(service, "prelive_job", accepted["data"]["job_id"])["body"]
    assert child["status"] == "queued"
    with service.store.transaction(write=True) as db:
        slot = service.store.all(db, "audio_reissue_slot")[0]
        assert slot["body"]["state"] == "reserved" and slot["body"]["consumed_attempts"] == 0
        if fake_resolution:
            other_asset = next(item["output"]["asset_ref"]
                for item in case["source_before"]["body"]["items"] if item["status"] == "succeeded")
            service.store.put(db, "audio_reissue_resolution", slot["ref"]["id"], {
                "preparation_id": case["preparation_id"],
                "approval_ref": accepted["data"]["reissue_approval_ref"],
                "job_id": child["id"], "item_id": child["items"][0]["item_id"],
                "attempt_id": "fabricated-nonexistent-attempt", "asset_ref": other_asset,
                "requirement_id": case["unknown_before"]["input"]["requirement_id"],
                "source_job_ref": case["source_before"]["ref"],
                "source_item_id": case["unknown_before"]["item_id"],
                "source_unknown_attempt_ids": [a["attempt_id"] for a in case["unknown_before"]["provider_attempts"]],
                "committed_at": "2000-01-01T00:00:00+00:00"}, create=True)
    before = db_snapshot(service)
    ready = await service.query("read_audio_readiness", {"preparation_id": case["preparation_id"]})
    assert not ready["ready_for_publish"] and ready["counts"]["unknown_job_items"] >= 1
    assert before == db_snapshot(service) and case["adapter"].calls == case["calls_before"]


class PeakPCMAdapter(ReissuePCMAdapter):
    def __init__(self, root):
        super().__init__(root)
        self.fail_next = 2
        self.active = 0
        self.peak = 0

    async def render_audio(self, text, room_id, voice, *, style=None):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(.03)
            return await super().render_audio(text, room_id, voice, style=style)
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_authorized_child_is_serial_while_normal_minimax_keeps_three_way_concurrency(tmp_path):
    adapter = PeakPCMAdapter(tmp_path / "voice")
    service = PreliveService(tmp_path / "voice", adapter, minimax_audio_concurrency=3)
    try:
        _, prep, _ = await prepared_text(service)
        current = await service.query("read_preparation", {"preparation_id": prep["id"]})
        with service.store.transaction() as db:
            draft = deepcopy(service._artifact(db, current["body"]["draft_ref"])["body"]["draft"])
        draft["tts_selection"] = {"provider": "minimax", "voice": "isolated-fake-voice", "model": "speech-2.8-turbo"}
        saved = await human_command(service, "save_session_draft", {"preparation_id": prep["id"],
            "expected_revision": current["ref"]["revision"], "draft": draft}, operation="select-fake-minimax")
        compiled = await human_command(service, "compile_playback_plan", {"preparation_id": prep["id"],
            "expected_revision": saved["data"]["preparation_ref"]["revision"],
            "playlist_ref": current["body"]["playlist_ref"]}, operation="compile-fake-minimax")
        review = await service.query("create_text_review", {"preparation_id": prep["id"]})
        approval = await human_command(service, "approve_text_for_audio", {"preparation_id": prep["id"],
            "expected_revision": compiled["data"]["preparation_ref"]["revision"],
            "review_hash": review["review_hash"]}, operation="approve-fake-minimax-text")
        original = await human_command(service, "prepare_required_audio", {"preparation_id": prep["id"],
            "expected_revision": approval["data"]["preparation_ref"]["revision"],
            "approval_ref": approval["data"]["approval_ref"], "review_hash": review["review_hash"],
            "expected_generation_epoch": approval["data"]["generation_epoch"]}, operation="normal-concurrent-audio")
        await service.drain_jobs()
        assert adapter.peak == 3
        case = {"preparation_id": prep["id"], "source_job_id": original["data"]["job_id"]}
        view = await inspect(service, case)
        assert view["status"] == "ready_for_explicit_approval" and view["missing_count"] == 2
        adapter.peak = 0
        calls_before = sum(adapter.calls.values())
        child = await human_command(service, "approve_missing_audio_reissue", approval_arguments(view), operation="serial-two-item-reissue")
        await service.drain_jobs()
        assert adapter.peak == 1 and service.minimax_audio_concurrency == 3
        assert sum(adapter.calls.values()) == calls_before + 2
        assert stored(service, "prelive_job", child["data"]["job_id"])["body"]["status"] == "succeeded"
        ready = await service.query("read_audio_readiness", {"preparation_id": prep["id"]})
        assert ready["ready_for_publish"] and ready["counts"]["unknown_job_items"] == 0
    finally:
        await service.close()
