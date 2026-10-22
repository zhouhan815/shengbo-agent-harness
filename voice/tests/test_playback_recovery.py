"""Playback owner recovery uses isolated SQLite and no model/audio providers."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app import server
from app.prelive import PreliveService, BusinessError


class PlaybackRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="playback-recovery-")
        self.service = PreliveService(self.temp.name)
        self.patches = [patch.object(server, "RUNTIME", Path(self.temp.name)),
                        patch.object(server, "rooms", {}),
                        patch.object(server.app.state, "prelive_service", self.service, create=True)]
        for item in self.patches:
            item.start()
        self.config = await server.create_room({"name": "恢复测试"})

    async def asyncTearDown(self):
        await self.service.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def seed_slot(self, state, *, owner=None, room_match=True):
        with self.service.store.transaction(write=True) as c:
            c.execute("INSERT OR REPLACE INTO prelive_live_slot VALUES(1,?,?,?,?)",
                      (self.config["id"], "publication-test", state, "start-test"))
            if owner:
                self.service.store.put(c, "playback_start", "start-test",
                                       {"owner_id": owner, "room_id": self.config["id"] if room_match else "other-room",
                                        "publication_id": "publication-test"}, create=True)
            self.service.store.put(c, "prelive_session", "session-test",
                                   {"room_id": self.config["id"], "state": "live", "start_operation_id": "start-test"}, create=True)

    def slot(self):
        with self.service.store.transaction() as c:
            return dict(c.execute("SELECT * FROM prelive_live_slot WHERE slot=1").fetchone())

    async def test_matching_exited_owner_is_recovered_without_replaying(self):
        for state in ("starting", "live", "unknown"):
            with self.subTest(state=state):
                # Each subtest has a fresh generation and exact persisted ownership.
                if state != "starting":
                    with self.service.store.transaction(write=True) as c:
                        c.execute("DELETE FROM records WHERE kind IN ('playback_start','prelive_session')")
                        c.execute("DELETE FROM record_revisions WHERE kind IN ('playback_start','prelive_session')")
                self.service.begin_playback_owner("old-" + state, exclusive_lease=True)
                self.seed_slot(state, owner="old-" + state)
                result = self.service.begin_playback_owner("new-" + state, exclusive_lease=True)
                self.assertIsNone(result)
                self.assertEqual(self.slot()["state"], "stopped")
                with self.service.store.transaction() as c:
                    session = self.service.store.get(c, "prelive_session", "session-test")["body"]
                    self.assertEqual(session["state"], "interrupted")
                self.assertEqual(server.rooms, {})  # no automatic replay

    async def test_unproven_legacy_slot_requires_visible_human_stop(self):
        self.seed_slot("unknown")
        async with server.app_lifespan(server.app):
            self.assertEqual(self.slot()["state"], "unknown")
            target = server.room_instance(self.config["id"])
            self.assertTrue(target.state["recovery_required"])
            self.assertEqual(target.state["status"], "error")
            self.assertIn("OBS", target.state["error"])
            await target.action({"action": "stop"})
            self.assertEqual(self.slot()["state"], "stopped")
            self.assertFalse(target.state["recovery_required"])

    async def test_shutdown_does_not_silently_clear_unproven_slot(self):
        self.seed_slot("unknown")
        async with server.app_lifespan(server.app):
            pass
        self.assertEqual(self.slot()["state"], "unknown")

    async def test_mismatched_owner_evidence_is_not_released(self):
        self.service.begin_playback_owner("old", exclusive_lease=True)
        self.seed_slot("live", owner="old", room_match=False)
        result = self.service.begin_playback_owner("new", exclusive_lease=True)
        self.assertEqual(result["state"], "live")
        self.assertEqual(self.slot()["state"], "live")

    async def test_owner_reconciliation_requires_exclusive_lease(self):
        self.seed_slot("live")
        with self.assertRaises(BusinessError) as error:
            self.service.begin_playback_owner("untrusted")
        self.assertEqual(error.exception.code, "PLAYBACK_OWNER_LEASE_REQUIRED")
        self.assertEqual(self.slot()["state"], "live")

    async def test_second_live_lease_cannot_take_over(self):
        with server.playback_owner_lease():
            with self.assertRaises(RuntimeError):
                with server.playback_owner_lease():
                    self.fail("a second owner acquired a live runtime")
        with server.playback_owner_lease() as new_owner:
            self.assertTrue(new_owner)

    async def test_os_releases_owner_after_process_crash(self):
        # Spawn a genuine owner, then terminate only that test-created process.
        # No service ports, credentials, models or TTS are used by the child.
        code = """
import time
from app import server
from app.prelive import PreliveService
service = PreliveService(server.RUNTIME)
with server.playback_owner_lease() as owner:
    service.begin_playback_owner(owner, exclusive_lease=True)
    with service.store.transaction(write=True) as c:
        c.execute('INSERT INTO prelive_live_slot VALUES(1,?,?,?,?)', ('test-room', 'test-publication', 'live', 'crashed-start'))
        service.store.put(c, 'playback_start', 'crashed-start', {'owner_id': owner, 'room_id': 'test-room', 'publication_id': 'test-publication'}, create=True)
    print('READY', flush=True)
    time.sleep(30)
"""
        environment = {**os.environ, "SHENGBO_RUNTIME": self.temp.name,
                       "SHENGBO_SKIP_DOTENV": "1", "SHENGBO_DISABLE_AGENT_BRIDGE": "1"}
        child = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code],
                                 cwd=server.ROOT, env=environment,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8")
        try:
            ready = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), timeout=10)
            self.assertEqual(ready.strip(), "READY")
            with self.assertRaises(RuntimeError):
                with server.playback_owner_lease():
                    self.fail("running child owner was taken over")
            child.terminate()
            await asyncio.to_thread(child.wait, 10)
            with server.playback_owner_lease() as owner:
                self.assertIsNone(self.service.begin_playback_owner(owner, exclusive_lease=True))
                self.assertEqual(self.slot()["state"], "stopped")
        finally:
            if child.poll() is None:
                child.kill()
                await asyncio.to_thread(child.wait, 10)
            child.stdout.close()
            child.stderr.close()


if __name__ == "__main__":
    unittest.main()
