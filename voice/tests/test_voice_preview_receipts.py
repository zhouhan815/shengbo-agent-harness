"""Isolated SQLite/fake-render proof of no-repeat paid audition semantics."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.prelive.human_workbench import VoicePreviewInput
from app.prelive.storage import BusinessError, Store, canonical, digest, now
from app.prelive.voice_preview import VoicePreviewService, read_preview_receipt


class VoicePreviewReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.store = Store(self.runtime)
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = False
        self.hold = False
        self.invalid = False

        def render(text, directory, voice, provider, model, style):
            self.calls += 1
            self.entered.set()
            if self.hold:
                if not self.release.wait(timeout=5):
                    raise RuntimeError("fixture timed out")
            if self.fail:
                raise RuntimeError("fake provider outcome uncertain: DO_NOT_LOG_SECRET")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "fixture.wav"
            path.write_bytes(b"fake audio; never a real provider")
            return {"url": "/audio/fixture.wav", "duration": 1.5, "provider": provider} if not self.invalid else {"url": "https://external.invalid/token", "duration": 1}

        self.server = SimpleNamespace(voice_options=lambda *_: None, sentence_style=lambda *_: {"speed": 1}, render=render, RUNTIME=self.runtime, POOL=None)
        self.previews = VoicePreviewService(self.store, self.server)

    async def asyncTearDown(self):
        self.release.set()
        self.temp.cleanup()

    def value(self, operation_id="audition-1", **changes):
        return VoicePreviewInput.model_validate({"operation_id": operation_id, "tts_selection": {"provider": "minimax", "voice": "Wise_Woman", "model": "speech-2.8-turbo"}, **changes})

    def receipt(self, operation_id="audition-1", actor_id="human-test"):
        with self.store.transaction() as c:
            return read_preview_receipt(c, actor_id, operation_id)

    async def test_success_same_id_and_new_id_reuse_available_audio(self):
        first = await self.previews.preview(self.value(), "human-test")
        replay = await self.previews.preview(self.value(), "human-test")
        self.assertEqual(first, replay)
        self.assertEqual(self.calls, 1)
        cached = await self.previews.preview(self.value("audition-2"), "human-test")
        self.assertEqual(cached["url"], first["url"])
        self.assertEqual(cached["operation_id"], "audition-2")
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.receipt()["state"], "committed")
        with self.store.transaction() as c:
            saved = c.execute("SELECT command,response_json FROM prelive_operations WHERE principal_id=? AND operation_id=?", ("human-test", "audition-1")).fetchone()
            self.assertEqual(saved["command"], "human.voice-preview")
            self.assertNotIn("DO_NOT_LOG_SECRET", saved["response_json"])

    async def test_same_id_different_content_is_conflict_before_call(self):
        await self.previews.preview(self.value(), "human-test")
        with self.assertRaises(BusinessError) as caught:
            await self.previews.preview(self.value(text="另一句试听内容"), "human-test")
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.calls, 1)

    async def test_concurrent_request_has_one_provider_call_and_pending_receipt(self):
        self.hold = True
        task = asyncio.create_task(self.previews.preview(self.value(), "human-test"))
        self.assertTrue(await asyncio.to_thread(self.entered.wait, 2))
        self.assertEqual(self.receipt()["state"], "pending")
        with self.assertRaises(BusinessError) as caught:
            await self.previews.preview(self.value(), "human-test")
        self.assertEqual(caught.exception.code, "PREVIEW_IN_PROGRESS")
        self.assertEqual(self.calls, 1)
        self.release.set()
        self.assertEqual((await task)["preview_state"], "succeeded")
        self.assertEqual(self.receipt()["state"], "committed")

    async def test_http_caller_cancellation_does_not_discard_result(self):
        self.hold = True
        caller = asyncio.create_task(self.previews.preview(self.value(), "human-test"))
        self.assertTrue(await asyncio.to_thread(self.entered.wait, 2))
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertEqual(self.receipt()["state"], "pending")
        self.release.set()
        for _ in range(100):
            if self.receipt()["state"] == "committed":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.receipt()["state"], "committed")
        self.assertEqual(self.calls, 1)

    async def test_concurrent_same_content_different_ids_has_one_call(self):
        self.hold = True
        first = asyncio.create_task(self.previews.preview(self.value("original-id"), "human-test"))
        try:
            self.assertTrue(await asyncio.to_thread(self.entered.wait, 2))
            with self.assertRaises(BusinessError) as caught:
                await self.previews.preview(self.value("another-id"), "human-test")
            self.assertEqual(caught.exception.code, "PREVIEW_IN_PROGRESS")
            self.assertEqual(caught.exception.details["original_operation_id"], "original-id")
            self.assertEqual(caught.exception.details["operation_id"], "original-id")
            self.assertEqual(caught.exception.details["requested_operation_id"], "another-id")
            self.assertEqual(self.calls, 1)
        finally:
            self.release.set()
            await first
        reused = await self.previews.preview(self.value("another-id"), "human-test")
        self.assertEqual(reused["operation_id"], "another-id")
        self.assertEqual(self.calls, 1)

    async def test_unknown_same_content_new_id_cannot_bypass_original_attempt(self):
        self.fail = True
        with self.assertRaises(BusinessError):
            await self.previews.preview(self.value("original-unknown"), "human-test")
        self.fail = False
        restarted = VoicePreviewService(self.store, self.server)
        with self.assertRaises(BusinessError) as caught:
            await restarted.preview(self.value("new-after-restart"), "human-test")
        self.assertEqual(caught.exception.code, "PREVIEW_OUTCOME_UNKNOWN")
        self.assertEqual(caught.exception.details["original_operation_id"], "original-unknown")
        self.assertEqual(self.calls, 1)
        self.assertIsNone(self.receipt("new-after-restart"), "拒绝的新编号未冒充一个已发出的调用")
        self.assertEqual(self.receipt("original-unknown")["state"], "outcome_unknown")

    async def test_failure_is_unknown_replay_does_not_pay_again_or_leak_error(self):
        self.fail = True
        for _ in range(2):
            with self.assertRaises(BusinessError) as caught:
                await self.previews.preview(self.value(), "human-test")
            self.assertEqual(caught.exception.code, "PREVIEW_OUTCOME_UNKNOWN")
            self.assertNotIn("DO_NOT_LOG_SECRET", canonical(caught.exception.payload()))
        self.assertEqual(self.receipt()["state"], "outcome_unknown")
        self.assertEqual(self.calls, 1)
        # Reinitializing the helper (a restart) cannot erase the unknown attempt.
        restarted = VoicePreviewService(self.store, self.server)
        with self.assertRaises(BusinessError) as caught:
            await restarted.preview(self.value(), "human-test")
        self.assertEqual(caught.exception.code, "PREVIEW_OUTCOME_UNKNOWN")
        self.assertEqual(self.calls, 1)

    async def test_orphan_started_attempt_after_restart_is_unknown_without_call(self):
        value = self.value()
        request_hash = digest({"command": "human.voice-preview", "voice": value.tts_selection.model_dump(), "text": value.text})
        with self.store.transaction(write=True) as c:
            c.execute("INSERT INTO prelive_voice_preview_attempts VALUES(?,?,?,?,?,?,?,?,?)", ("human-test", value.operation_id, "orphan-from-old-process", request_hash, "started", None, None, now(), now()))
        self.assertEqual(self.receipt()["state"], "outcome_unknown")
        with self.assertRaises(BusinessError) as caught:
            await self.previews.preview(value, "human-test")
        self.assertEqual(caught.exception.code, "PREVIEW_OUTCOME_UNKNOWN")
        self.assertEqual(self.calls, 0)

    async def test_deleted_audio_same_id_does_not_render_again(self):
        await self.previews.preview(self.value(), "human-test")
        (self.runtime / "audio" / "fixture.wav").unlink()
        self.assertEqual(self.receipt()["state"], "audio_unavailable")
        with self.assertRaises(BusinessError) as caught:
            await self.previews.preview(self.value(), "human-test")
        self.assertEqual(caught.exception.code, "PREVIEW_AUDIO_UNAVAILABLE")
        self.assertEqual(self.calls, 1)

    async def test_unsafe_or_missing_result_is_unknown_and_not_retried(self):
        self.invalid = True
        with self.assertRaises(BusinessError) as caught:
            await self.previews.preview(self.value(), "human-test")
        self.assertEqual(caught.exception.code, "PREVIEW_OUTCOME_UNKNOWN")
        self.assertEqual(self.receipt()["state"], "outcome_unknown")

    async def test_actor_isolation_and_other_operation_namespace(self):
        await self.previews.preview(self.value(), "human-test")
        self.assertIsNone(self.receipt(actor_id="another-human"))
        with self.store.transaction(write=True) as c:
            c.execute("INSERT INTO prelive_operations VALUES(?,?,?,?,?,?)", ("human-test", "used-by-room", "human.room-settings", "other-hash", "{}", now()))
        with self.assertRaises(BusinessError) as caught:
            await self.previews.preview(self.value("used-by-room"), "human-test")
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.calls, 1)


if __name__ == "__main__":
    unittest.main()
