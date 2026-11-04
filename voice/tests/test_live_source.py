import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from app import live_history, live_source, server


class FakeSocket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def recv(self):
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, str) else json.dumps(item)


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class LiveSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(server, "RUNTIME", Path(self.temp.name)),
                        patch.object(server, "rooms", {}),
                        patch.object(live_source, "SESSIONS", {}),
                        patch.object(live_source, "CONTROL_LOCKS", {}),
                        patch.object(live_source, "SERVICE_LOCK", asyncio.Lock()),
                        patch.object(live_source, "COLLECTOR", {"process": None, "port": None, "key": None})]
        for item in self.patches:
            item.start()
        self.room = server.save_room_config("voice_a", {"name": "调试A"}, creating=True)
        self.other = server.save_room_config("voice_b", {"name": "调试B"}, creating=True)
        self.config = live_source._validated("voice_a", {"room": "12345678901"})
        server.put("live_source_config", self.config)

    async def asyncTearDown(self):
        await live_source.shutdown()
        for item in self.patches[::-1]:
            item.stop()
        self.temp.cleanup()

    async def spin(self, condition):
        for _ in range(100):
            if condition():
                return
            await asyncio.sleep(.002)
        self.fail("后台任务未在预期内推进")

    def message(self, method="WebcastChatMessage", event_id="18446744073709551615", **values):
        return {"method": method, "common": {"msgId": event_id, "createTime": "1780000000"},
                "user": {"id": "18446744073709551614", "nickname": "公开昵称"},
                "content": "这个多少钱？", **values}

    def persist(self, config=None, capture_id="capture_test", **kwargs):
        event, key = live_source.normalize(self.message(**kwargs), config or self.config, capture_id)
        return live_source._persist(event, key)

    async def test_input_url_and_number_normalize_and_reject_non_rooms(self):
        self.assertEqual(live_source.parse_room("https://live.douyin.com/12345678901/?from=share"), "12345678901")
        self.assertEqual(live_source.parse_room("12345678901"), "12345678901")
        for value in ("https://evil.test/123456", "https://live.douyin.com.evil.test/123456",
                      "https://live.douyin.com:443/123456", "https://name@live.douyin.com/123456",
                      "https://v.douyin.com/ABC", "abc123", "123", 123456, "file:///123456"):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                live_source.parse_room(value)

    async def test_room_id_namespace_missing_room_and_validation(self):
        with self.assertRaises(HTTPException) as caught:
            live_source.get_view("not_a_shengbo_room")
        self.assertEqual(caught.exception.status_code, 404)
        for values in ({"purpose": "other"}, {"platform": "tiktok"}, {"enabled": "false"}):
            with self.subTest(values=values), self.assertRaises(HTTPException):
                live_source.save_config("voice_a", values)
        self.assertEqual(live_source.get_view("voice_a")["config"]["external_room_id"], "12345678901")

    async def test_config_persists_enabled_without_automatic_connection(self):
        view = live_source.save_config("voice_a", {"url": "https://live.douyin.com/23456789012", "enabled": True})
        self.assertEqual(view["status"]["state"], "reconnect_required")
        self.assertFalse(view["status"]["connected"])
        self.assertFalse(live_source.SESSIONS)
        self.assertEqual(view["config"]["url"], "https://live.douyin.com/23456789012")

    async def test_normalize_big_ids_public_nickname_and_no_unproven_social_mapping(self):
        event, _ = live_source.normalize(self.message(), self.config, "capture")
        self.assertEqual(event["upstream_event_id"], "18446744073709551615")
        self.assertEqual(event["nickname"], "公开昵称")
        self.assertNotIn("18446744073709551614", json.dumps(event))
        social, _ = live_source.normalize(self.message("WebcastSocialMessage", action="3"), self.config, "capture")
        self.assertEqual(social["type"], "social")
        self.assertEqual(social["social_action_raw"], "3")
        self.assertIsNone(live_source.normalize({"method": "UnexpectedMessage"}, self.config, "capture"))

    async def test_message_kinds_preserve_semantics(self):
        cases = [("WebcastChatMessage", "comment"), ("WebcastMemberMessage", "enter"),
                 ("WebcastLikeMessage", "like"), ("WebcastGiftMessage", "gift"),
                 ("WebcastRoomUserSeqMessage", "audience_snapshot"), ("WebcastRoomStatsMessage", "stats_display")]
        for method, expected in cases:
            with self.subTest(method=method):
                event, _ = live_source.normalize(self.message(method), self.config, "c")
                self.assertEqual(event["type"], expected)
                self.assertEqual(event["source"], "douyin")
        gift, _ = live_source.normalize(self.message("WebcastGiftMessage", gift={"name": "玫瑰"}, repeatCount="4", groupId="9223372036854775807"), self.config, "c")
        self.assertEqual(gift["count"], 4)
        self.assertEqual(gift["gift_name"], "玫瑰")

    async def test_durable_dedup_scoped_to_each_shengbo_and_external_room(self):
        self.assertIsNotNone(self.persist())
        self.assertIsNone(self.persist(capture_id="a_reconnect"))
        config = {**self.config, "id": "voice_b"}
        self.assertIsNotNone(self.persist(config=config))
        self.assertEqual(len(server.all_of("viewer_event")), 2)
        live_source.SESSIONS.clear()
        self.assertIsNone(self.persist())
        self.assertEqual(live_source.get_view("voice_a")["status"]["history_counts"], {"comment": 1})
        self.assertEqual(live_source.get_view("voice_a")["status"]["counts"], {})

    async def test_missing_ids_do_not_collapse_unrelated_identical_comments(self):
        self.assertIsNotNone(self.persist(event_id="0"))
        self.assertIsNotNone(self.persist(event_id="0"))
        self.assertEqual(len(server.all_of("viewer_event")), 2)

    async def test_incremental_totals_migrate_existing_durable_events_once(self):
        self.persist()
        with server.db() as connection:
            connection.execute("DROP TABLE live_source_totals")
        self.assertEqual(live_source.get_view("voice_a")["status"]["history_counts"], {"comment": 1})
        self.assertEqual(live_source.get_view("voice_a")["status"]["history_counts"], {"comment": 1})
        self.persist(event_id="200")
        self.assertEqual(live_source.get_view("voice_a")["status"]["history_counts"], {"comment": 2})

    async def test_cursor_recent_and_incremental_bounded_pagination(self):
        for number in range(255):
            self.persist(event_id=str(number + 1))
        view = live_source.get_view("voice_a")
        self.assertEqual(len(view["events"]), 200)
        self.assertEqual(view["events"][0]["cursor"], 56)
        self.assertEqual(view["next_cursor"], 255)
        incremental = live_source.get_view("voice_a", 1)
        self.assertEqual([event["cursor"] for event in incremental["events"]], list(range(2, 202)))
        final = live_source.get_view("voice_a", incremental["next_cursor"])
        self.assertEqual(final["next_cursor"], 255)
        self.assertEqual(live_source.get_view("voice_a", 255)["events"], [])
        for after in (-1, "1", True):
            with self.assertRaises(HTTPException):
                live_source.get_view("voice_a", after)

    async def test_metrics_never_infer_online_from_enters_and_restart_is_stale(self):
        self.persist(method="WebcastMemberMessage")
        self.assertIsNone(live_source.get_view("voice_a")["status"]["metrics"]["online"])
        self.persist(method="WebcastRoomUserSeqMessage", total="4201", totalUser="28001", totalPvForAnchor="34002")
        view = live_source.get_view("voice_a")
        metrics = view["status"]["metrics"]
        self.assertEqual(metrics["online"]["value"], 4201)
        self.assertEqual(metrics["total_visits"]["value"], 34002)
        self.assertFalse(metrics["online"]["verified"])
        self.assertTrue(metrics["online"]["stale"])
        self.assertEqual(len(server.all_of("viewer_event")), 1)
        self.assertEqual(len(server.all_of("live_metric")), 1)

    async def test_switch_saved_source_does_not_display_previous_room_events(self):
        self.persist()
        view = live_source.save_config("voice_a", {"room": "23456789012"})
        self.assertEqual(view["events"], [])
        self.assertEqual(view["status"]["history_counts"], {})
        old = live_source.save_config("voice_a", {"room": "12345678901"})
        self.assertEqual(len(old["events"]), 1)

    async def test_socket_handshake_not_connected_then_online_events_broadcast_disconnect(self):
        socket = FakeSocket()
        target = server.room_instance("voice_a")
        with patch.object(live_source, "_ensure_collector", AsyncMock(return_value=(17899, "not-for-the-ui"))), \
             patch.object(live_source, "connect", return_value=socket) as connector, \
             patch.object(target, "broadcast", AsyncMock()) as broadcast:
            await live_source.connect_source("voice_a")
            await self.spin(lambda: live_source.get_view("voice_a")["status"]["local_socket_connected"])
            self.assertFalse(live_source.get_view("voice_a")["status"]["connected"])
            socket.queue.put_nowait("pong")
            socket.queue.put_nowait({"type": "system", "status": "online", "live": True})
            await self.spin(lambda: live_source.get_view("voice_a")["status"]["connected"])
            socket.queue.put_nowait(self.message())
            await self.spin(lambda: len(live_source.get_view("voice_a")["events"]) == 1)
            self.assertEqual(target.state["audience_events"][0]["source"], "douyin")
            self.assertEqual(broadcast.await_count, 1)
            self.assertEqual(connector.call_args.kwargs["additional_headers"], {"Authorization": "Bearer not-for-the-ui"})
            view = await live_source.disconnect_source("voice_a")
            self.assertFalse(view["status"]["connected"])
            self.assertFalse(view["config"]["enabled"])
            self.assertEqual(len(view["events"]), 1)
            self.assertTrue(socket.closed)
            self.assertNotIn("not-for-the-ui", json.dumps(view))
            self.assertEqual(target.state["status"], "stopped")
            self.assertFalse(server.all_of("reply_turn"))

    async def test_switch_connect_cancels_old_task_and_isolates_counts(self):
        sockets = [FakeSocket(), FakeSocket()]
        with patch.object(live_source, "_ensure_collector", AsyncMock(return_value=(17899, "key"))), \
             patch.object(live_source, "connect", side_effect=sockets):
            await live_source.connect_source("voice_a")
            await self.spin(lambda: live_source.get_view("voice_a")["status"]["local_socket_connected"])
            first = live_source.SESSIONS["voice_a"]["task"]
            with self.assertRaises(HTTPException):
                live_source.save_config("voice_a", {"room": "23456789012"})
            await live_source.connect_source("voice_a", {"room": "23456789012", "purpose": "own_room"})
            await self.spin(lambda: live_source.get_view("voice_a")["status"]["local_socket_connected"])
            self.assertTrue(first.done())
            self.assertTrue(sockets[0].closed)
            self.assertEqual(live_source.get_view("voice_a")["config"]["external_room_id"], "23456789012")

    async def test_offline_unknown_ended_remain_waiting_not_connected(self):
        socket = FakeSocket()
        with patch.object(live_source, "_ensure_collector", AsyncMock(return_value=(17899, "key"))), \
             patch.object(live_source, "connect", return_value=socket):
            await live_source.connect_source("voice_a")
            for status in ("offline", "unknown", "ended"):
                socket.queue.put_nowait({"type": "system", "status": status, "live": False})
                await asyncio.sleep(.01)
                self.assertFalse(live_source.get_view("voice_a")["status"]["connected"])
                self.assertFalse(live_source.SESSIONS["voice_a"]["task"].done())
            self.assertNotIn("已下播", live_source.get_view("voice_a")["status"]["message"])

    async def test_not_found_finishes_without_fake_connected(self):
        socket = FakeSocket()
        socket.queue.put_nowait({"type": "system", "status": "not_found", "live": False})
        with patch.object(live_source, "_ensure_collector", AsyncMock(return_value=(17899, "key"))), \
             patch.object(live_source, "connect", return_value=socket):
            await live_source.connect_source("voice_a")
            await self.spin(lambda: live_source.SESSIONS["voice_a"]["task"].done())
            self.assertEqual(live_source.get_view("voice_a")["status"]["state"], "not_found")
            self.assertFalse(live_source.get_view("voice_a")["status"]["connected"])

    async def test_retries_are_bounded_and_errors_do_not_leak_headers(self):
        with patch.object(live_source, "_ensure_collector", AsyncMock(side_effect=OSError("Authorization: Bearer secret"))), \
             patch.object(live_source, "RETRY_BASE", 0):
            await live_source.connect_source("voice_a")
            await self.spin(lambda: live_source.SESSIONS["voice_a"]["task"].done())
            view = live_source.get_view("voice_a")
            self.assertEqual(view["status"]["state"], "error")
            self.assertEqual(view["status"]["retry_count"], 5)
            self.assertNotIn("secret", json.dumps(view))

    async def test_missing_collector_visible_without_launching(self):
        with patch.object(live_source, "executable", return_value=Path(self.temp.name) / "missing.exe"):
            view = await live_source.connect_source("voice_a")
            await self.spin(lambda: live_source.SESSIONS["voice_a"]["task"].done())
            view = live_source.get_view("voice_a")
            self.assertEqual(view["status"]["state"], "error")
            self.assertFalse(view["status"]["collector_ready"])
            self.assertIn("未安装", view["status"]["message"])

    async def test_collector_environment_and_shutdown_cleanup(self):
        with patch.dict("os.environ", {"APP_PORT": "80", "APP_SIGN_PROVIDER": "remote", "APP_API_KEY": "old"}), \
             patch.object(live_source.urllib.request, "getproxies", return_value={"https": "http://proxy.test:88"}):
            env = live_source.collector_environment("new-key")
        self.assertNotIn("APP_PORT", env)
        self.assertNotIn("APP_SIGN_PROVIDER", env)
        self.assertEqual(env["APP_API_KEY"], "new-key")
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.test:88")
        self.assertIn("127.0.0.1", env["NO_PROXY"])
        process = FakeProcess()
        live_source.COLLECTOR.update(process=process, port=1, key="private")
        live_source.save_config("voice_a", {"enabled": True})
        await live_source.shutdown()
        self.assertTrue(process.terminated)
        self.assertIsNone(live_source.COLLECTOR["key"])
        self.assertEqual(live_source.get_view("voice_a")["status"]["state"], "reconnect_required")

    async def test_process_started_locally_with_authenticated_health_and_private_config(self):
        path = Path(self.temp.name) / "collector.exe"
        path.touch()
        process = FakeProcess()
        with patch.object(live_source, "executable", return_value=path), \
             patch.object(live_source.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as launch, \
             patch.object(live_source, "_health", return_value=True) as health:
            port, key = await live_source._ensure_collector()
            self.assertGreater(port, 1024)
            self.assertGreater(len(key), 24)
            self.assertEqual(health.call_args.args, (port, key))
            self.assertEqual(launch.call_args.kwargs["env"]["APP_API_KEY"], key)
            content = (Path(self.temp.name) / "live_source" / "collector.yaml").read_text(encoding="utf-8")
            self.assertNotIn(key, content)
            self.assertIn("use_stored: false", content)
        await live_source.shutdown()
        self.assertTrue(process.terminated)

    async def test_health_uses_authenticated_api_without_proxy(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Opener:
            def open(self, request, timeout):
                self.request, self.timeout = request, timeout
                return Response()
        opener = Opener()
        with patch.object(live_source.urllib.request, "build_opener", return_value=opener) as build:
            self.assertTrue(live_source._health(17899, "private-token"))
        self.assertEqual(opener.request.full_url, "http://127.0.0.1:17899/api/v1/health")
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer private-token")
        self.assertEqual(build.call_args.args[0].proxies, {})

    async def test_failed_authenticated_readiness_terminates_process(self):
        path = Path(self.temp.name) / "collector.exe"
        path.touch()
        process = FakeProcess()
        calls = 0
        def denied(port, key):
            nonlocal calls
            calls += 1
            # End the fake process on the second readiness check to avoid a real delay.
            process.returncode = 1
            raise OSError("unauthorized")
        with patch.object(live_source, "executable", return_value=path), \
             patch.object(live_source.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), \
             patch.object(live_source, "_health", side_effect=denied):
            with self.assertRaises(RuntimeError):
                await live_source._ensure_collector()
        self.assertGreater(calls, 0)
        self.assertIsNone(live_source.COLLECTOR["process"])

    async def test_shutdown_cancels_tasks_but_preserves_reconnect_intent(self):
        socket = FakeSocket()
        with patch.object(live_source, "_ensure_collector", AsyncMock(return_value=(17899, "key"))), \
             patch.object(live_source, "connect", return_value=socket):
            await live_source.connect_source("voice_a")
            await self.spin(lambda: live_source.get_view("voice_a")["status"]["local_socket_connected"])
            task = live_source.SESSIONS["voice_a"]["task"]
            await live_source.shutdown()
            self.assertTrue(task.done())
            self.assertTrue(socket.closed)
            view = live_source.get_view("voice_a")
            self.assertTrue(view["config"]["enabled"])
            self.assertFalse(view["status"]["connected"])
            self.assertEqual(view["status"]["state"], "reconnect_required")

    async def test_malformed_wire_messages_do_not_create_fake_events(self):
        socket = FakeSocket()
        with patch.object(live_source, "_ensure_collector", AsyncMock(return_value=(17899, "key"))), \
             patch.object(live_source, "connect", return_value=socket):
            await live_source.connect_source("voice_a")
            for message in ("not json", "[]", {"method": "UNKNOWN"}):
                socket.queue.put_nowait(message)
            await self.spin(lambda: live_source.get_view("voice_a")["status"]["unknown_messages"] == 3)
            self.assertEqual(live_source.get_view("voice_a")["events"], [])

    async def test_playback_history_uses_real_starts_and_exact_source_scope(self):
        def log(identity, session, when, event, detail):
            server.put("room_log", {"id": server.uid("room_log"), "room_id": identity, "session_id": session,
                                    "time": when, "event": event, "detail": detail})

        server.put("prelive_session", {"id": "early", "room_id": "voice_a", "state": "stopped",
                    "started_at": "2026-11-03T02:00:00.500+00:00", "stopped_at": "2026-11-03T03:00:00.450+00:00"})
        log("voice_a", "early", "2026-11-03T10:00:00+08:00", "prelive_publication_started", {})
        log("voice_a", "late", "2026-11-03T21:00:00+08:00", "control_accepted", "start")
        log("voice_a", "late", "2026-11-03T20:59:00+08:00", "control_accepted", "stop")
        log("voice_a", "late", "2026-11-03T22:00:00+08:00", "playback_completed", {})
        log("voice_a", "late", "2026-11-03T22:01:00+08:00", "control_accepted", "stop")
        log("voice_a", "idle", "2026-11-03T23:00:00+08:00", "control_accepted", "stop")
        log("voice_b", "other", "2026-11-03T09:00:00+08:00", "control_accepted", "start")

        def record(session, event_id, when, *, config=None, mark=None, method="WebcastChatMessage", **extra):
            event, key = live_source.normalize(self.message(method,event_id,**extra),config or self.config,"capture_test")
            event.update(session_id=session,time=when)
            if mark is not None:
                event["during_playback"] = mark
            live_source._persist(event,key)

        record("early","1","2026-11-03T10:00:00+08:00",method="WebcastRoomUserSeqMessage",total="66")
        record("early","2","2026-11-03T10:10:00+08:00")
        record("early","3","2026-11-03T11:00:00+08:00")  # ambiguous historical stop second
        record("early","4","2026-11-03T11:10:00+08:00")  # old collector retained the stopped session ID
        record("early","5","2026-11-03T09:59:00+08:00")
        record("idle","6","2026-11-03T10:10:00+08:00")
        record("late","7","2026-11-03T21:30:00+08:00",mark=True)
        own = live_source._validated("voice_a", {"room":"99999999999","purpose":"own_room"})
        record("late","8","2026-11-03T21:40:00+08:00",config=own,mark=True)
        record("late","9","2026-11-03T22:00:00+08:00",mark=True)
        other = live_source._validated("voice_b", {"room":"99999999999"})
        record("late","10","2026-11-03T21:40:00+08:00",config=other,mark=True)
        record("early","11","2026-11-03T11:00:00.400+08:00")
        record("early","12","2026-11-03T11:00:00.550+08:00")
        self.assertEqual([item["id"] for item in live_history.list_sessions("voice_a")["items"]],["late","early"])
        with self.assertRaises(HTTPException):
            live_history.read_session("voice_a","other")
        with self.assertRaises(HTTPException):
            live_history.read_session("voice_a","idle")
        early = live_history.read_session("voice_a","early")
        self.assertEqual(len(early["events"]),3)
        self.assertEqual(early["sources"][0]["metrics"]["online"]["value"],66)
        self.assertEqual(early["sources"][0]["counts"]["comment"],2)
        with patch.object(live_history,"EVENT_PAGE",2):
            late = live_history.read_session("voice_a","late")
            self.assertEqual(len(late["events"]),2)
            next_page = live_history.read_session("voice_a","late",late["next_before"])
            self.assertEqual(len(next_page["events"]),1)
            self.assertNotIn(next_page["events"][0]["id"],[event["id"] for event in late["events"]])
            self.assertEqual(late["session"]["state"],"ended")
            self.assertEqual(late["session"]["ended_at"],"2026-11-03T22:00:00+08:00")
            self.assertEqual({item["purpose"] for item in late["sources"]},{"own_room","external_test"})
            self.assertEqual(sum(item["counts"]["comment"] for item in late["sources"]),3)

    async def test_capture_tags_only_active_playback_and_setup_can_read_status_only(self):
        target = server.room_instance("voice_a")
        source = {"capture_id":"capture_live","config":self.config,"counts":live_source.Counter(),"unknown_messages":0}
        await live_source._publish("voice_a",source,self.message(event_id="1"))
        target.state.update(status="paused", session_id="active")
        await live_source._publish("voice_a",source,self.message(event_id="2"))
        events = live_source.get_view("voice_a")["events"]
        self.assertEqual([(event["session_id"],event["during_playback"]) for event in events],[(None,False),("active",True)])
        with patch.object(live_source,"_schema",side_effect=AssertionError("status-only read scanned events")):
            summary = live_source.get_view("voice_a",summary_only=True)
        self.assertEqual(set(summary),{"config","status"})
        self.assertEqual(summary["status"]["state"],"disabled")


if __name__ == "__main__":
    unittest.main()
