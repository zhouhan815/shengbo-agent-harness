"""Per-viewer burst backlog uses the original natural-boundary Room player."""
import asyncio
from copy import deepcopy
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from app import server, live_source
from test_live_interactions_v1 import BoundaryRoom, sentence


class LikePlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.product = {"id": "p1", "name": "单人网球训练套装", "price": 23.58, "currency": "CNY"}
        self.config = {"id": "default", "product_ids": ["p1"]}
        self.patches = [patch.object(server, "settings", return_value=self.config),
            patch.object(server, "all_of", return_value=[]), patch.object(server, "get", return_value=self.product),
            patch.object(server, "put", side_effect=lambda kind, item: item)]
        for item in self.patches:
            item.start()
        self.room = BoundaryRoom()
        self.room.owner = "owner"
        self.room.state.update(queue=[sentence(1), sentence(2), sentence(3)], index=0)
        self.room.set_current(sentence(1))
        await asyncio.sleep(0)
        self.room.state["status"] = "playing"
        self.clock = [10.0]
        self.room.likes.clock = lambda: self.clock[0]

    async def asyncTearDown(self):
        try:
            await self.room.action({"action": "stop"})
            if self.room.like_worker:
                await self.room.like_worker
            await asyncio.gather(*list(self.room.interaction_tasks), return_exceptions=True)
        finally:
            for item in reversed(self.patches):
                item.stop()

    def event(self, event_id, viewer="A", count=1, **extra):
        return {"id": event_id, "type": "like", "username": viewer + "123✨", "user_id": viewer,
            "count": count, "total": 9999, "source": "simulated", "room_id": "default",
            "session_id": self.room.state["session_id"], **extra}

    async def ready(self):
        for _ in range(250):
            if (self.room.state.get("insertion") or {}).get("pending", {}).get("prepared_audio"):
                return self.room.state["insertion"]["pending"]
            await asyncio.sleep(.005)
        self.fail("like did not become ready")

    async def ack(self, event):
        await self.room.acknowledge("owner", {"event": event, "epoch": self.room.state["epoch"],
            "attempt_id": self.room.state["current"]["attempt_id"]})
        await asyncio.sleep(0)

    @staticmethod
    def audio(*args):
        return {"audio_id": "like-test", "url": "/audio/like-test.wav", "duration": .1}

    async def test_same_viewer_merges_different_viewer_waits_and_both_resume_script(self):
        with patch.object(server, "render", side_effect=self.audio) as render:
            self.assertEqual(self.room.like_entry(self.event("a1", count=3))["status"], "collecting")
            self.room.like_entry(self.event("a2", count=7))
            self.room.like_entry(self.event("b1", "B", 2))
            self.clock[0] += 3.01
            pending = await self.ready()
            self.assertEqual(pending["like_count"], 10)
            self.assertIn("A送来的10个赞", pending["text"])
            self.assertNotIn("9999", pending["text"])
            self.assertEqual(self.room.state["current"]["id"], "s1")
            # No early interrupt: the original sentence must end first.
            await self.ack("ended")
            self.assertTrue(self.room.state["current"]["is_like"])
            await self.ack("playing")
            await self.ack("ended")
            self.assertEqual(self.room.state["current"]["id"], "s2")
            pending = await self.ready()
            self.assertEqual(pending["like_count"], 2)
            await self.ack("playing")
            await self.ack("ended")
            self.assertTrue(self.room.state["current"]["is_like"])
            await self.ack("playing")
            await self.ack("ended")
            self.assertEqual(self.room.state["current"]["id"], "s3")
            self.assertEqual(render.call_count, 2)

    async def test_busy_welcome_does_not_drop_likes_and_old_event_is_not_counted_twice(self):
        self.room.state["insertion"] = {"kind": "welcome"}
        event = self.event("a1", count=5)
        self.assertEqual(self.room.like_entry(event)["status"], "collecting")
        self.assertEqual(self.room.like_entry(event)["reason"], "duplicate_event")
        self.clock[0] += 4
        with patch.object(server, "render", side_effect=self.audio):
            await asyncio.sleep(.01)
            self.assertEqual(len(self.room.likes.pending), 1)
            self.room.state["insertion"] = None
            self.assertEqual((await self.ready())["like_count"], 5)

    async def test_failed_one_person_does_not_repeat_paid_call_or_block_next_person(self):
        self.room.like_entry(self.event("a1"))
        self.room.like_entry(self.event("b1", "B"))
        self.clock[0] += 4
        with patch.object(server, "render", side_effect=[RuntimeError("private"), self.audio()]) as render:
            pending = await self.ready()
            self.assertIn("感谢B", pending["text"])
            self.assertEqual(render.call_count, 2)
            self.assertTrue(any(log["event"] == "like_thanks_diagnostic" and
                log["detail"]["reason"] == "failed_unconfirmed" for log in self.room.state["logs"]))
            self.assertNotIn("private", str(self.room.state["logs"]))

    async def test_price_change_rebuilds_only_unplayed_batch(self):
        with patch.object(server, "render", side_effect=self.audio):
            self.room.like_entry(self.event("a1"))
            self.clock[0] += 4
            old = deepcopy(await self.ready())
            # Let the now-empty collector exit before invalidating its last inflight item.
            await self.room.like_worker
            self.product["price"] = 25
            await self.ack("ended")
            self.assertFalse(self.room.state["current"].get("is_like"))
            pending = await self.ready()
            self.assertEqual(pending["like_batch_id"], old["like_batch_id"])
            self.assertIn("售价25元", pending["text"])

    async def test_new_session_gets_its_own_worker_before_old_worker_exits(self):
        self.room.like_entry(self.event("old"))
        old_worker = self.room.like_worker
        self.room.likes.clear(reason="stopped")
        self.room.state["session_id"] = "next-session"
        with patch.object(server, "render", side_effect=self.audio):
            self.room.like_entry(self.event("new"))
            self.assertIsNot(self.room.like_worker, old_worker)
            self.clock[0] += 4
            pending = await self.ready()
            self.assertEqual(pending["like_session_id"], "next-session")
        await old_worker

    async def test_stop_clears_waiting_people_without_tts(self):
        self.room.like_entry(self.event("a1"))
        await self.room.action({"action": "stop"})
        self.assertFalse(self.room.likes.pending)
        self.assertFalse(self.room.likes.inflight)

    async def test_started_like_pause_resume_continues_same_audio_once(self):
        with patch.object(server, "render", side_effect=self.audio) as render:
            self.room.like_entry(self.event("a1"))
            self.clock[0] += 4
            await self.ready()
            await self.ack("ended")
            await self.ack("playing")
            attempt = self.room.state["current"]["attempt_id"]
            await self.room.action({"action": "pause"})
            await self.room.action({"action": "resume"})
            self.assertEqual(self.room.state["current"]["attempt_id"], attempt)
            self.assertTrue(self.room.state["current"]["is_like"])
            await self.ack("playing")
            await self.ack("ended")
            self.assertEqual(self.room.state["current"]["id"], "s2")
            render.assert_called_once()

    async def test_test_api_preserves_current_count_and_rejects_invalid_values(self):
        with patch.object(server, "room_instance", return_value=self.room):
            for value in (0, -1, True, 1.5, "5"):
                with self.assertRaises(HTTPException):
                    await server.test_event("default", {"type": "like", "username": "A", "like_count": value})
            result = await server.test_event("default", {"type": "like", "username": "A", "like_count": 8, "total_count": 9999})
            self.assertEqual(result["event"]["count"], 8)
            self.assertNotIn("total", result["event"])
            self.assertEqual(result["like_thanks"]["status"], "collecting")

    async def test_real_plugin_like_is_routed_to_collection_and_keeps_count_separate(self):
        config = {"id": "default", "external_room_id": "12345678901", "purpose": "own_room", "platform": "douyin"}
        state = {"config": config, "capture_id": "capture", "counts": {"like": 0}}
        raw = {"method": "WebcastLikeMessage", "common": {"msgId": "1"},
            "user": {"id": "2", "nickname": "A"}, "count": "12", "total": "9999"}
        with patch.object(server, "room_instance", return_value=self.room), \
                patch.object(live_source, "_persist", side_effect=lambda event, key: event), \
                patch.object(self.room, "like_entry", wraps=self.room.like_entry) as collect:
            await live_source._publish("default", state, raw)
        collect.assert_called_once()
        event = collect.call_args.args[0]
        self.assertEqual(event["count"], 12)
        self.assertEqual(event["total"], 9999)
        self.assertEqual(len(self.room.likes.pending), 1)
