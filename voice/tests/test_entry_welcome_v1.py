import asyncio
from copy import deepcopy
import threading
import unittest
from unittest.mock import AsyncMock, patch

from app import live_source, server, welcome


TEMPLATES = [f"欢迎，{{username}}！来看看本品的第{index}个选购角度。" for index in range(5)]


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def item(number, product="p1"):
    return {"id": f"s{number}", "queue_id": f"q{number}", "product_id": product,
            "script_id": f"script-{product}", "text": "商品讲解", "spoken_text": "商品讲解",
            "welcome_templates": deepcopy(TEMPLATES)}


class QuietRoom(server.Room):
    auto_ready_welcome = True

    def log(self, event, detail):
        self.state["logs"].append({"event": event, "detail": detail})

    async def broadcast(self):
        pass

    async def prepare(self, epoch, entry):
        pass

    def queue_interaction(self, kind, entry):
        result = super().queue_interaction(kind, entry)
        # These existing tests exercise queue/ACK policy with explicit fake
        # already-ready audio. Dedicated warm-up tests call the production
        # prepare_pending_insertion with a controlled fake render instead.
        pending = (self.state.get("insertion") or {}).get("pending")
        if kind == "welcome" and pending and self.auto_ready_welcome:
            pending["prepared_audio"] = {"audio_id": "ready-test-welcome", "url": "/test-welcome.wav", "duration": 2}
        return result

    async def prepare_pending_insertion(self, session_id, entry):
        pass  # Never call real TTS from the pure state-machine fixture.


class TemplateTests(unittest.TestCase):
    def test_builder_generates_five_product_bound_templates_without_claims(self):
        templates = welcome.build_product_templates({"name": "棉拖鞋", "selling_points": "不可引入的资料", "price": 123})
        self.assertEqual(len(set(templates)), 5)
        self.assertTrue(all("棉拖鞋" in template and template.count("{username}") == 1 for template in templates))
        self.assertNotIn("不可引入", str(templates))
        self.assertNotIn("123", str(templates))
        self.assertEqual(len(welcome.build_product_templates({"name": "<script>{username}<#0.5#>"})), 5)

    def test_only_letters_and_attached_language_marks_are_read(self):
        for nickname, expected in [("张三123💖＋-×÷", "张三"), ("Alice １２3_+-", "Alice"),
                                   ("José العربية हिन्दी 한국어 Русский", "José العربية हिन्दी 한국어 Русский"),
                                   ("123１２３𝟟＋÷✨", ""), ("\u200b李<三>{两}", "李三两"),
                                   ("A\ufe0f\u20e3B", "AB")]:
            with self.subTest(nickname=nickname):
                self.assertEqual(welcome.readable_username(nickname), expected)

    def test_contract_rejects_bad_count_duplicate_token_or_tts_control(self):
        self.assertEqual(welcome.validate_templates(TEMPLATES), TEMPLATES)
        for invalid in [TEMPLATES[:4], TEMPLATES[:4] + TEMPLATES[:1],
                        ["欢迎{username}{username}"] + TEMPLATES[1:],
                        ["欢迎{username}<#0.5#>"] + TEMPLATES[1:],
                        ["欢迎{username}[happy]"] + TEMPLATES[1:],
                        ["欢迎{username}(laughs)"] + TEMPLATES[1:],
                        ["欢迎{username}\u200b"] + TEMPLATES[1:]]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                welcome.validate_templates(invalid)


class EntryWelcomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.object(server, "settings", return_value={}), patch.object(server, "all_of", return_value=[]):
            self.r = QuietRoom()
        self.clock = Clock()
        self.r.welcomes = welcome.WelcomeRuntime(clock=self.clock, choose=lambda values: values[0])
        self.r.owner = "owner"
        self.r.state.update(queue=[item(1), item(2), item(3, "p2")], index=0)
        self.r.set_current(item(1))
        self.r.state.update(status="playing", audio={"duration": 3})

    def event(self, number, **extra):
        return {"id": f"event{number}", "type": "enter", "source": "simulated", "username": "小明123＋✨",
                "session_id": self.r.state["session_id"], **extra}

    async def enter(self, number, **extra):
        async with self.r.lock:
            return self.r.welcome_entry(self.event(number, **extra))

    def ack(self, name):
        return {"event": name, "epoch": self.r.state["epoch"], "attempt_id": self.r.state["current"]["attempt_id"]}

    def clear_insertion(self):
        self.r.state.update(insertion=None, status="playing")
        self.r.set_current(self.r.current_item())
        self.r.state["status"] = "playing"

    async def start_welcome(self):
        await self.r.acknowledge("owner", self.ack("ended"))
        self.r.state["status"] = "ready"
        await self.r.acknowledge("owner", self.ack("playing"))

    async def test_sliding_boundary_at_zero_59999_and_60(self):
        self.assertEqual((await self.enter(1))["status"], "queued")
        self.clear_insertion()
        self.clock.value = 59.999
        self.assertEqual((await self.enter(2))["reason"], "cooldown")
        self.clock.value = 60
        self.assertEqual((await self.enter(3))["status"], "queued")
        self.assertEqual(self.r.state["insertion"]["pending"]["welcome_event_id"], "event3")

    async def test_new_script_generation_and_legacy_start_prepare_templates_once(self):
        product = {"id": "p1", "name": "棉拖鞋"}
        script = server.structure_script(product, {"blocks": [{"stage": "welcome", "sentences": [{"display_text": "欢迎来了解。", "kind": "engagement"}]}]}, "test", "test", [])
        self.assertEqual(script["welcome_templates"], welcome.build_product_templates(product))
        del script["welcome_templates"]
        self.r.state["status"] = "stopped"
        with patch.object(server, "get", side_effect=lambda kind, identity: script if kind == "script" else product), patch.object(server, "settings", return_value={}):
            await self.r.action({"action": "start", "script_ids": [script["id"]]})
        self.assertEqual(self.r.state["current"]["welcome_templates"], welcome.build_product_templates(product))
        self.assertNotIn("welcome_templates", script, "old immutable script content is not rewritten")

    async def test_concurrent_boundary_admits_only_one(self):
        await self.enter(0)
        self.clear_insertion()
        self.clock.value = 60
        results = await asyncio.gather(*(self.enter(number) for number in range(1, 40)))
        self.assertEqual(sum(result["status"] == "queued" for result in results), 1)
        self.assertFalse(any(result.get("pending") for result in results))

    async def test_duplicate_enter_never_replays_after_cooldown(self):
        await self.enter(1)
        self.clear_insertion()
        self.clock.value = 60
        self.assertEqual((await self.enter(1))["reason"], "duplicate_event")
        self.assertEqual((await self.enter(2))["status"], "queued")

    async def test_upstream_reconnect_replay_ignores_local_event_id(self):
        values = {"source": "douyin", "purpose": "own_room", "external_room_id": "12345", "raw_method": "WebcastMemberMessage", "upstream_event_id": "90000000000001"}
        await self.enter(1, **values)
        self.clear_insertion()
        self.clock.value = 60
        self.assertEqual((await self.enter(2, **values))["reason"], "duplicate_event")

    async def test_current_product_templates_and_rotation(self):
        await self.enter(1)
        first = self.r.state["insertion"]["pending"]["text"]
        self.assertNotIn("123", first)
        self.assertIn("小明", first)
        self.clear_insertion()
        self.clock.value = 60
        await self.enter(2)
        self.assertNotEqual(first, self.r.state["insertion"]["pending"]["text"])
        self.clear_insertion()
        self.clock.value = 120
        self.r.state["index"] = 2
        other = item(3, "p2")
        other["welcome_templates"] = [text.replace("本品", "第二款") for text in TEMPLATES]
        self.r.set_current(other)
        self.r.state["status"] = "playing"
        await self.enter(3)
        self.assertIn("第二款", self.r.state["insertion"]["pending"]["text"])

    async def test_empty_filtered_username_uses_fallback_and_consumes_interval(self):
        await self.enter(1, username="123＋÷✨")
        self.assertEqual(self.r.state["insertion"]["text"], "欢迎新来的朋友")
        self.clear_insertion()
        self.clock.value = 1
        self.assertEqual((await self.enter(2))["reason"], "cooldown")

    async def test_product_switch_keeps_room_cooldown(self):
        await self.enter(1)
        self.clear_insertion()
        self.clock.value = 20
        self.r.state["index"] = 2
        self.r.set_current(self.r.current_item())
        self.r.state["status"] = "playing"
        self.assertEqual((await self.enter(2))["reason"], "cooldown")

    async def test_source_test_and_inactive_states_remain_silent(self):
        self.assertEqual((await self.enter(1, source="douyin", purpose="external_test"))["reason"], "external_test_read_only")
        for status in ("stopped", "paused", "completed", "error", "preparing"):
            self.r.state["status"] = status
            self.assertEqual((await self.enter(status))["reason"], "not_playing")
        self.assertIsNone(self.r.state["insertion"])

    async def test_manual_insert_is_unchanged_and_busy_does_not_accumulate(self):
        await self.r.action({"action": "insert", "text": "人工通知", "mode": "after_sentence"})
        self.assertEqual((await self.enter(1))["reason"], "insertion_busy")
        await self.r.acknowledge("owner", self.ack("ended"))
        self.assertEqual(self.r.state["current"]["text"], "人工通知")
        self.assertNotIn("is_welcome", self.r.state["current"])

    async def test_manual_insert_supersedes_pending_welcome(self):
        await self.enter(1)
        await self.r.action({"action": "insert", "text": "人工通知", "mode": "after_sentence"})
        self.assertEqual(self.r.state["insertion"]["pending"]["text"], "人工通知")

    async def test_expired_pending_welcome_drops_without_tts_or_backlog(self):
        await self.enter(1)
        self.clock.value = 30
        await self.r.acknowledge("owner", self.ack("ended"))
        self.assertEqual(self.r.state["current"]["id"], "s2")
        self.assertIsNone(self.r.state["insertion"])
        self.assertEqual(self.r.state["logs"][-2]["detail"].get("reason"), "expired")

    async def test_boundary_product_change_drops_old_product_welcome(self):
        self.r.state["index"] = 1
        self.r.set_current(self.r.current_item())
        self.r.state["status"] = "playing"
        await self.enter(1)
        await self.r.acknowledge("owner", self.ack("ended"))
        self.assertEqual(self.r.state["current"]["product_id"], "p2")
        self.assertIsNone(self.r.state["insertion"])
        self.assertTrue(any(log["detail"].get("reason") == "product_changed" for log in self.r.state["logs"] if isinstance(log["detail"], dict)))

    async def test_actual_play_time_gate_prevents_congested_welcomes(self):
        self.r.state["queue"][2] = item(3)
        await self.enter(1)
        self.clock.value = 20
        await self.start_welcome()
        self.clock.value = 21
        await self.r.acknowledge("owner", self.ack("ended"))
        self.r.state["status"] = "playing"
        self.clock.value = 60
        self.assertEqual((await self.enter(2))["reason"], "cooldown")
        self.assertIsNone(self.r.state["insertion"])
        self.assertEqual(self.r.welcomes.last_admitted, 0)
        self.clock.value = 80
        self.assertEqual((await self.enter(3))["status"], "queued")

    async def test_stop_new_session_resets_interval_but_not_replay_dedup(self):
        await self.enter(1)
        await self.r.action({"action": "stop"})
        self.assertIsNone(self.r.state["insertion"])
        self.r.state.update(session_id="new-session", index=0)
        self.r.set_current(self.r.current_item())
        self.r.state["status"] = "playing"
        self.clock.value = 5
        self.assertEqual((await self.enter(1))["reason"], "duplicate_event")
        self.assertEqual((await self.enter(2))["status"], "queued")

    async def test_tts_failure_returns_to_normal_queue_without_auto_retry(self):
        self.r.auto_ready_welcome = False
        await self.enter(1)
        pending = deepcopy(self.r.state["insertion"]["pending"])
        with patch.object(server, "render", side_effect=RuntimeError("secret-provider-response")) as render:
            await server.Room.prepare_pending_insertion(self.r, self.r.state["session_id"], pending)
        render.assert_called_once()
        self.assertEqual(self.r.state["current"]["id"], "s1")
        self.assertEqual(self.r.state["status"], "playing")
        self.assertIsNone(self.r.state["insertion"])
        self.assertNotIn("secret-provider-response", str(self.r.state["logs"]))

    async def test_blocked_welcome_is_dropped_and_does_not_wait_for_later_unlock(self):
        await self.enter(1)
        await self.r.acknowledge("owner", self.ack("ended"))
        self.r.state["status"] = "ready"
        await self.r.acknowledge("owner", self.ack("blocked"))
        self.assertEqual(self.r.state["current"]["id"], "s2")
        self.assertIsNone(self.r.state["insertion"])

    async def test_expired_paused_welcome_resume_returns_to_normal_queue(self):
        await self.enter(1)
        await self.r.acknowledge("owner", self.ack("ended"))
        await self.r.action({"action": "pause"})
        self.clock.value = 31
        await self.r.action({"action": "resume"})
        self.assertEqual(self.r.state["current"]["id"], "s2")
        self.assertIsNone(self.r.state["insertion"])

    async def test_old_synthesis_completion_after_stop_cannot_change_new_session(self):
        self.r.auto_ready_welcome = False
        await self.enter(1)
        pending, session = deepcopy(self.r.state["insertion"]["pending"]), self.r.state["session_id"]
        await self.r.action({"action": "stop"})
        self.r.state.update(session_id="new-session", index=2)
        self.r.set_current(self.r.current_item())
        new_epoch = self.r.state["epoch"]
        with patch.object(server, "render", return_value={"audio_id": "old-stub", "url": "/old-stub.wav", "duration": 2}):
            await server.Room.prepare_pending_insertion(self.r, session, pending)
        self.assertEqual(self.r.state["epoch"], new_epoch)
        self.assertEqual(self.r.state["current"]["product_id"], "p2")
        self.assertIsNone(self.r.state["audio"])

    async def test_tts_finishes_after_expiry_is_not_ready_for_playback(self):
        self.r.auto_ready_welcome = False
        await self.enter(1)
        pending = deepcopy(self.r.state["insertion"]["pending"])
        original_audio = deepcopy(self.r.state["audio"])
        def render(*args):
            self.clock.value = 31
            return {"audio_id": "stub", "url": "/stub.wav", "duration": 2}
        with patch.object(server, "render", side_effect=render):
            await server.Room.prepare_pending_insertion(self.r, self.r.state["session_id"], pending)
        self.assertFalse(self.r.state["current"].get("is_welcome"))
        self.assertEqual(self.r.state["audio"], original_audio)
        self.assertIsNone(self.r.state["insertion"])

    async def test_tts_uses_existing_insertion_channel_and_voice(self):
        self.r.auto_ready_welcome = False
        await self.enter(1)
        pending = deepcopy(self.r.state["insertion"]["pending"])
        with patch.object(server, "render", return_value={"audio_id": "stub", "url": "/stub.wav", "duration": 2}) as render:
            await server.Room.prepare_pending_insertion(self.r, self.r.state["session_id"], pending)
            self.assertEqual(self.r.state["current"]["id"], "s1", "prepared welcome cannot interrupt mid-sentence")
            await self.r.acknowledge("owner", self.ack("ended"))
        render.assert_called_once()
        self.assertEqual(self.r.state["status"], "ready")
        self.assertEqual(render.call_args.args[0], pending["spoken_text"])
        self.assertEqual(render.call_args.args[2:5], ("Huihui", "system", "speech-2.8-turbo"))
        self.assertEqual(render.call_args.args[5]["profile_id"], "welcome")

    async def test_slow_tts_keeps_script_playing_then_selects_only_at_a_later_boundary(self):
        self.r.auto_ready_welcome = False
        self.r.state["queue"] = [item(1), item(2), item(3), item(4)]
        started, release = threading.Event(), threading.Event()
        def render(*args):
            started.set()
            if not release.wait(3):
                raise RuntimeError("TESTONLY controlled render was not released")
            return {"audio_id": "slow-stub", "url": "/slow-stub.wav", "duration": 2}
        with patch.object(server, "render", side_effect=render) as rendered:
            await self.enter(1)
            pending = deepcopy(self.r.state["insertion"]["pending"])
            task = asyncio.create_task(server.Room.prepare_pending_insertion(self.r, self.r.state["session_id"], pending))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.assertEqual(self.r.state["current"]["id"], "s1")
                await self.r.acknowledge("owner", self.ack("ended"))
                self.assertEqual(self.r.state["current"]["id"], "s2")
                self.assertFalse(self.r.state["current"].get("is_welcome"))
                self.assertIsNotNone(self.r.state["insertion"]["pending"])
                self.r.state.update(status="playing", audio={"audio_id": "normal-s2", "duration": 3})
                self.clock.value = 10
                self.assertEqual((await self.enter(2))["reason"], "insertion_busy")
            finally:
                release.set()
                await task
            self.assertEqual(self.r.state["current"]["id"], "s2", "audio ready must wait for a sentence boundary")
            await self.r.acknowledge("owner", self.ack("ended"))
            self.assertTrue(self.r.state["current"]["is_welcome"])
            self.assertEqual(self.r.state["audio"]["audio_id"], "slow-stub")
            rendered.assert_called_once()
            await self.r.acknowledge("owner", self.ack("playing"))
            await self.r.acknowledge("owner", self.ack("ended"))
            self.assertEqual(self.r.state["current"]["id"], "s3")
            self.assertIsNone(self.r.state["insertion"])

    async def test_publish_invokes_welcome_only_after_durable_dedup(self):
        config = {"id": "default", "external_room_id": "12345", "purpose": "own_room"}
        state = {"config": config, "capture_id": "capture", "unknown_messages": 0, "counts": {"enter": 0}}
        raw = {"method": "WebcastMemberMessage", "common": {"msgId": "100"}, "user": {"nickname": "小明"}}
        with patch.object(server, "room_instance", return_value=self.r), patch.object(live_source, "_persist", side_effect=lambda event, key: event):
            await live_source._publish("default", state, raw)
        self.assertEqual(self.r.state["insertion"]["kind"], "welcome")
        self.clear_insertion()
        self.clock.value = 60
        with patch.object(server, "room_instance", return_value=self.r), patch.object(live_source, "_persist", return_value=None):
            await live_source._publish("default", state, raw)
        self.assertIsNone(self.r.state["insertion"])


if __name__ == "__main__":
    unittest.main()
