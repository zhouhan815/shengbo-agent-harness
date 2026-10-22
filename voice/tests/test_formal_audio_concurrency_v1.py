"""Offline single-coordinator MiniMax batches: real SQLite/WAV, no paid calls."""
import asyncio
from copy import deepcopy
from pathlib import Path
import threading
from time import perf_counter
import unittest
from unittest.mock import patch

from app.prelive import BusinessError, PreliveService
import test_prelive_service as fixture


VOICE = {"provider": "minimax", "voice": "Friendly_Person", "model": "speech-2.8-turbo"}


class TrackingAdapter(fixture.FakeAdapter):
    def __init__(self, root):
        super().__init__(root)
        self.active = 0
        self.maximum = 0
        self.attempted = []
        self.delay = .06
        self.fail_text = None
        self.block_texts = set()
        self.release = asyncio.Event()

    async def render_audio(self, text, room_id, voice, *, style=None):
        self.attempted.append(text)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            if text == self.fail_text:
                raise BusinessError("TTS_RENDER_FAILED", "offline explicit failure")
            if text in self.block_texts:
                await self.release.wait()
            await asyncio.sleep(self.delay)
            return await super().render_audio(text, room_id, voice)
        finally:
            self.active -= 1


class FormalAudioConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    cmd = fixture.BusinessTests.cmd
    product = fixture.BusinessTests.product
    prep = fixture.BusinessTests.prep
    state = fixture.BusinessTests.state
    prepared_text = fixture.BusinessTests.prepared_text
    asyncTearDown = fixture.BusinessTests.asyncTearDown

    async def asyncSetUp(self):
        await fixture.BusinessTests.asyncSetUp(self)
        self.adapter = TrackingAdapter(self.temp.name)
        self.service.adapter = self.adapter
        self.service.wake_worker = lambda: None

    async def compiled(self, count=9, *, provider="minimax", prefix="并发", repeat=False):
        async def generate(context, key, product, previous):
            if key in {"opening_long", "opening_short"}:
                return [prefix + "欢迎了解本商品。"]
            n = count - 3 if key == "selling_points" else 1
            return [prefix + key + "第" + str(i) + "条演示内容。" for i in range(n)]
        self.adapter.generate_module = generate
        _, _, prep, script = await self.prepared_text()
        state = await self.state(prep)
        saved = await self.cmd("save_session_draft", {"preparation_id": prep["id"],
            "expected_revision": state["ref"]["revision"], "draft": {"theme": "日常介绍", "commerce": {},
            "tts_selection": VOICE if provider == "minimax" else {"provider": "system", "voice": "Huihui", "model": "speech-2.8-turbo"}}})
        prep = saved["data"]["preparation_ref"]
        state = await self.state(prep)
        if repeat:
            with self.service.store.transaction() as c:
                playlist = self.service.store.exact(c, state["body"]["playlist_ref"])["body"]
            duplicate = {**deepcopy(playlist["items"][0]), "occurrence_id": "repeated-occurrence"}
            saved = await self.cmd("save_playlist", {"preparation_id": prep["id"],
                "expected_revision": state["ref"]["revision"], "items": playlist["items"] + [duplicate], "loop_mode": "infinite"})
            prep = saved["data"]["preparation_ref"]
            state = await self.state(prep)
        saved = await self.cmd("compile_playback_plan", {"preparation_id": prep["id"],
            "expected_revision": state["ref"]["revision"], "playlist_ref": state["body"]["playlist_ref"]})
        prep = saved["data"]["preparation_ref"]
        review = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        self.assertEqual(len(review["texts"]), count)
        state = await self.state(prep)
        saved = await self.cmd("approve_text_for_audio", {"preparation_id": prep["id"],
            "expected_revision": state["ref"]["revision"], "review_hash": review["review_hash"]})
        return prep, review, saved["data"]["approval_ref"]

    async def enqueue(self, prep, review, approval, *, preview=False, index=0):
        state = await self.state(prep)
        args = {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
            "approval_ref": approval, "review_hash": review["review_hash"],
            "expected_generation_epoch": review["generation_epoch"]}
        if preview:
            args["requirement_id"] = review["texts"][index]["requirement_id"]
        result = await self.cmd("preview_audio" if preview else "prepare_required_audio", args)
        return result["data"]["job_id"]

    async def job(self, identity):
        return (await self.service.query("read_job", {"job_id": identity}))["body"]

    async def fresh_store(self, suffix):
        # The shared fixture intentionally uses one stable variant ID. Separate
        # comparison scenarios get isolated stores instead of bypassing its
        # real product-identity and variant-identity guards.
        concurrency = self.service.minimax_audio_concurrency
        await self.service.close()
        self.adapter.root = Path(self.temp.name) / suffix
        self.service = PreliveService(self.adapter.root, self.adapter,
            minimax_audio_concurrency=concurrency)
        self.service.wake_worker = lambda: None

    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), 5)

    async def test_bound_three_order_cache_and_serial_benchmark(self):
        prep, review, approval = await self.compiled()
        identity = await self.enqueue(prep, review, approval)
        started = perf_counter()
        await self.service.drain_jobs()
        parallel = perf_counter() - started
        body = await self.job(identity)
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(self.adapter.maximum, 3)
        self.assertEqual(len(self.adapter.attempted), 9)
        self.assertTrue(all(len(i["provider_attempts"]) == 1 for i in body["items"]))
        state = await self.state(prep)
        with self.service.store.transaction() as c:
            manifest = self.service.store.exact(c, state["body"]["audio_manifest_ref"])["body"]
        self.assertEqual([a["requirement_id"] for a in manifest["assets"]],
                         [r["requirement_id"] for r in review["texts"]])
        cached = await self.enqueue(prep, review, approval)
        await self.service.drain_jobs()
        self.assertEqual(len(self.adapter.attempted), 9)
        self.assertTrue(all(i["output"]["asset"]["reused"] for i in (await self.job(cached))["items"]))
        self.service.minimax_audio_concurrency = 1
        self.adapter.maximum = 0
        await self.fresh_store("serial-baseline")
        prep, review, approval = await self.compiled(prefix="串行基准")
        serial_job = await self.enqueue(prep, review, approval)
        started = perf_counter()
        await self.service.drain_jobs()
        serial = perf_counter() - started
        self.assertEqual(self.adapter.maximum, 1)
        self.assertEqual((await self.job(serial_job))["status"], "succeeded")
        print(f"OFFLINE_TTS_BENCHMARK sentences=9 delay_seconds={self.adapter.delay} parallel3={parallel:.3f}s serial1={serial:.3f}s ratio={serial/parallel:.2f}")

    async def test_final_batch_failure_waits_for_running_siblings(self):
        prep, review, approval = await self.compiled(6)
        self.adapter.fail_text = review["texts"][3]["spoken_text"]
        self.adapter.block_texts = {r["spoken_text"] for r in review["texts"][4:]}
        identity = await self.enqueue(prep, review, approval)
        self.service._background = asyncio.create_task(self.service.drain_jobs())
        await self.wait_until(lambda: len(self.adapter.attempted) == 6)
        # The deliberate failure may still be committing its short transaction.
        for _ in range(100):
            body = await self.job(identity)
            if body["items"][3]["status"] == "failed":
                break
            await asyncio.sleep(.005)
        self.assertEqual(body["status"], "running")
        self.assertEqual(sum(i["status"] == "running" for i in body["items"]), 2)
        self.assertIsNone((await self.state(prep))["body"]["audio_manifest_ref"])
        self.adapter.release.set()
        await self.service._background
        body = await self.job(identity)
        self.assertEqual(body["status"], "partially_failed")
        self.assertEqual(sum(i["status"] == "succeeded" for i in body["items"]), 5)
        await self.service.drain_jobs()
        self.assertEqual(len(self.adapter.attempted), 6)

    async def test_close_settles_children_unknown_is_not_reissued(self):
        prep, review, approval = await self.compiled(6)
        self.adapter.block_texts = {r["spoken_text"] for r in review["texts"][:3]}
        identity = await self.enqueue(prep, review, approval)
        self.service._background = asyncio.create_task(self.service.drain_jobs())
        await self.wait_until(lambda: self.adapter.active == 3)
        await self.service.close()
        self.assertEqual(self.adapter.active, 0)
        self.assertFalse([t for t in asyncio.all_tasks() if t.get_name().startswith("prelive-item-") and not t.done()])
        body = await self.job(identity)
        self.assertEqual(sum(i["call_state"] == "timeout_unknown" for i in body["items"]), 3)
        self.assertEqual(sum(i["status"] == "queued" for i in body["items"]), 3)
        with self.service.store.transaction() as c:
            external = [r for r in self.service.store.all(c, "prelive_external_request")
                        if r["body"]["job_id"] == identity]
        self.assertEqual(len(external), 3)
        self.assertTrue(all(r["body"]["state"] == "timeout_unknown" for r in external))
        self.adapter.block_texts.clear()
        await self.service.drain_jobs()
        self.assertEqual(len(self.adapter.attempted), 6)
        self.assertEqual(len(set(self.adapter.attempted)), 6)
        self.assertEqual((await self.job(identity))["status"], "partially_failed")

    async def test_local_audio_and_previews_remain_serial(self):
        prep, review, approval = await self.compiled(6, provider="system")
        await self.enqueue(prep, review, approval)
        await self.service.drain_jobs()
        self.assertEqual(self.adapter.maximum, 1)
        self.adapter.maximum = 0
        await self.fresh_store("preview-scenario")
        prep, review, approval = await self.compiled(6, prefix="试听")
        first = await self.enqueue(prep, review, approval, preview=True, index=0)
        second = await self.enqueue(prep, review, approval, preview=True, index=1)
        await self.service.drain_jobs()
        self.assertEqual(self.adapter.maximum, 1)
        self.assertEqual((await self.job(first))["status"], "succeeded")
        self.assertEqual((await self.job(second))["status"], "succeeded")

    async def test_repeated_occurrence_deduplicates_before_parallel_work(self):
        prep, review, approval = await self.compiled(6, repeat=True)
        self.assertEqual(review["playback_statistics"]["sentence_count"], 12)
        await self.enqueue(prep, review, approval)
        await self.service.drain_jobs()
        self.assertEqual(len(self.adapter.attempted), 6)
        self.assertEqual(len(set(self.adapter.attempted)), 6)
        self.assertEqual(self.adapter.maximum, 3)

    async def test_cancel_during_commit_preserves_already_committed_success(self):
        prep, review, approval = await self.compiled(6)
        identity = await self.enqueue(prep, review, approval, preview=True)
        committed, release = threading.Event(), threading.Event()
        original = self.service._commit_job_item
        def paused_return(*args):
            original(*args)
            committed.set()
            release.wait(5)
        try:
            with patch.object(self.service, "_commit_job_item", side_effect=paused_return):
                self.service._background = asyncio.create_task(self.service.drain_jobs())
                self.assertTrue(await asyncio.to_thread(committed.wait, 5))
                await self.service.close()
                body = await self.job(identity)
                self.assertEqual(body["status"], "succeeded")
                self.assertEqual(body["items"][0]["status"], "succeeded")
                self.assertEqual(body["items"][0]["provider_attempts"][0]["state"], "result_received")
        finally:
            release.set()

    def test_configuration_only_allows_one_to_three(self):
        for value in ("0", "4", "bad", True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PreliveService(self.temp.name, minimax_audio_concurrency=value)
        with patch.dict("app.prelive.service.os.environ", {"PRELIVE_MINIMAX_AUDIO_CONCURRENCY": "2"}):
            self.assertEqual(PreliveService(self.temp.name).minimax_audio_concurrency, 2)


if __name__ == "__main__":
    unittest.main()
