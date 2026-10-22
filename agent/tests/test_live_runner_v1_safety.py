"""Offline checks for constrained real-API tester policy. No service starts."""
import importlib.util
from contextlib import closing
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from pathlib import Path
import sqlite3
import json
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("live_runner_v1", Path(__file__).resolve().parents[1] / "verification/run_efficiency_live_v1.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerSafetyTests(unittest.TestCase):
    def test_unknown_decision_is_never_guessed(self):
        self.assertIsNone(runner.operator_answer({"kind": "change_price", "options": [{"value": "approve"}]}))
        self.assertIsNone(runner.operator_answer({"kind": "task_scope", "options": [{"value": "reject"}]}))
        self.assertEqual(runner.operator_answer({"kind": "playlist_order", "options": [{"value": "confirmed"}]}), "confirmed")

    def question(self, provider="system", voice="Huihui"):
        return {"preview": {"review_hash": "hash-1", "voice": {"provider": provider, "voice": voice},
                            "texts": [{"spoken_text": "虚构商品技术测试"}]},
            "human_action": {"command": {"command": "approve_text_for_audio", "args": {"review_hash": "hash-1"}}}}

    def test_only_actual_local_voice_and_bound_review_approved(self):
        self.assertEqual(runner.verify_audio_approval(self.question())["command"], "approve_text_for_audio")
        for provider, voice in (("minimax", "Huihui"), ("system", "other")):
            with self.assertRaises(RuntimeError):
                runner.verify_audio_approval(self.question(provider, voice))
        missing = self.question(); del missing["preview"]["voice"]
        with self.assertRaises(RuntimeError):
            runner.verify_audio_approval(missing)
        wrong_hash = self.question(); wrong_hash["preview"]["review_hash"] = "changed"
        with self.assertRaises(RuntimeError):
            runner.verify_audio_approval(wrong_hash)
        wrong_command = self.question(); wrong_command["human_action"]["command"]["command"] = "start_prepared_room"
        with self.assertRaises(RuntimeError):
            runner.verify_audio_approval(wrong_command)

    def test_actual_adoption_state_checked_not_just_declared(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / "voice").mkdir()
            database = root / "voice/library.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE records(kind TEXT,id TEXT,body TEXT)")
                connection.execute("INSERT INTO records VALUES(?,?,?)", ("config", "room", json.dumps({"id": "room"})))
                connection.commit()
            state = runner.actual_live_state(root, "room")
            self.assertFalse(state["adopted"]); self.assertFalse(state["started_live"])
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE records SET body=? WHERE kind='config'", (json.dumps({"adopted_publication_ref": {"id": "p"}}),))
                connection.execute("INSERT INTO records VALUES(?,?,?)", ("prelive_session", "s", json.dumps({"room_id": "room"})))
                connection.commit()
            state = runner.actual_live_state(root, "room")
            self.assertTrue(state["adopted"]); self.assertTrue(state["started_live"])

    def test_timeout_cleanup_uses_one_real_api_cancel_then_terminal_observation(self):
        async def run():
            calls, gets = [], 0
            def handler(request):
                nonlocal gets
                calls.append((request.method, request.url.path))
                if request.method == "POST":
                    self.assertTrue(request.url.path.endswith("/cancel"))
                    self.assertEqual(request.headers["X-CSRF-Token"], "fake-csrf")
                    return httpx.Response(202, json={"status": "CANCELLING"})
                gets += 1
                return httpx.Response(200, json={"id": "run", "status": "RUNNING" if gets == 1 else "CANCELLED"})
            async with httpx.AsyncClient(base_url="http://127.0.0.1:1", transport=httpx.MockTransport(handler)) as client:
                result = await runner.cancel_and_observe(client, "run", {"X-CSRF-Token": "fake-csrf"})
            self.assertTrue(result["requested"]); self.assertTrue(result["confirmed_cancelled"])
            self.assertEqual(sum(method == "POST" for method, _ in calls), 1)
            self.assertFalse(any("messages" in path or "commands" in path for _, path in calls))
        asyncio.run(run())

    def test_lost_cancel_response_is_queried_not_reposted(self):
        async def run():
            calls, gets = [], 0
            def handler(request):
                nonlocal gets
                calls.append(request.method)
                if request.method == "POST":
                    raise httpx.ReadTimeout("fake response lost", request=request)
                gets += 1
                return httpx.Response(200, json={"status": "RUNNING" if gets == 1 else "CANCELLED"})
            async with httpx.AsyncClient(base_url="http://127.0.0.1:1", transport=httpx.MockTransport(handler)) as client:
                result = await runner.cancel_and_observe(client, "run", {})
            self.assertTrue(result["response_unknown"]); self.assertTrue(result["confirmed_cancelled"])
            self.assertEqual(calls.count("POST"), 1)
        asyncio.run(run())

    def test_unavailable_service_still_writes_explicit_incomplete_result(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "ids.json").write_text(json.dumps({"run_id": "run", "room_id": "room"}), encoding="utf-8")
                (root / "deadline.json").write_text(json.dumps({"at": runner.now()}), encoding="utf-8")
                def handler(request):
                    raise httpx.ConnectError("fake service unavailable", request=request)
                args = SimpleNamespace(root=root, voice_port=18965, simulate_operator=True)
                result = await runner.record_incomplete(args, RuntimeError("LIVE_ACCEPTANCE_DEADLINE"), transport=httpx.MockTransport(handler))
                saved = json.loads((root / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(result["status"], "INCOMPLETE_TIMEOUT")
                self.assertFalse(saved["benchmark_eligible"]); self.assertFalse(saved["completed"])
                self.assertFalse(saved["cancellation"]["confirmed_cancelled"])
                self.assertTrue(saved["cancellation"]["observation_unavailable"])
        asyncio.run(run())

    def test_driver_exception_always_calls_incomplete_cleanup_and_rethrows(self):
        async def run():
            cleanup = AsyncMock(return_value={"status": "INCOMPLETE_ERROR"})
            with patch.object(runner, "_drive_round", AsyncMock(side_effect=RuntimeError("test_error"))), patch.object(runner, "record_incomplete", cleanup):
                with self.assertRaisesRegex(RuntimeError, "test_error"):
                    await runner.drive(SimpleNamespace())
            self.assertEqual(cleanup.await_count, 1)
        asyncio.run(run())

    def test_reused_round_rejection_never_cancels_an_existing_run(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "ids.json").write_text(json.dumps({"run_id": "existing-owner"}), encoding="utf-8")
                cleanup, inner = AsyncMock(), AsyncMock()
                with patch.object(runner, "record_incomplete", cleanup), patch.object(runner, "_drive_round", inner):
                    with self.assertRaisesRegex(RuntimeError, "NEW_ROUND_ROOT_REQUIRED"):
                        await runner.drive(SimpleNamespace(root=root))
                self.assertEqual(cleanup.await_count, 0)
                self.assertEqual(inner.await_count, 0)
        asyncio.run(run())

    def test_atomic_json_replace_retries_transient_share_failure_then_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "run-latest.json"
            target.write_text('{"previous":true}', encoding="utf-8")
            original, calls, temporary_names = Path.replace, [], []
            def replace(source, destination):
                calls.append(1); temporary_names.append(source.name)
                if len(calls) <= 2:
                    error = PermissionError("fake-sensitive-source-is-never-logged")
                    error.winerror = 32
                    raise error
                return original(source, destination)
            with patch.object(Path, "replace", autospec=True, side_effect=replace), patch.object(runner.time, "sleep") as delay:
                runner.save(target, {"new": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"new": True})
            self.assertEqual(len(calls), 3); self.assertEqual(delay.call_count, 2)
            self.assertTrue(all(name != "run-latest.json.tmp" for name in temporary_names))
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])

    def test_atomic_json_persistent_share_failure_is_bounded_and_preserves_old_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "run-latest.json"
            previous = b'{"previous":"last-known-complete-json"}'
            target.write_bytes(previous)
            error = PermissionError("fake-sensitive-error-filename")
            error.winerror = 5
            with patch.object(Path, "replace", side_effect=error) as replace, patch.object(runner.time, "sleep") as delay:
                with self.assertRaises(PermissionError) as caught:
                    runner.save(target, {"new": True})
            self.assertEqual(target.read_bytes(), previous)
            self.assertEqual(replace.call_count, 8); self.assertEqual(delay.call_count, 7)
            self.assertLessEqual(sum(call.args[0] for call in delay.call_args_list), 2)
            self.assertEqual(runner.safe_failure_metadata(caught.exception), {
                "error_type": "PermissionError", "safe_winerror": 5, "operation": "atomic_replace"})
            self.assertNotIn("fake-sensitive", json.dumps(runner.safe_failure_metadata(caught.exception)))
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
