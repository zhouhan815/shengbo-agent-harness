from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shengbo_contracts.common import EventEnvelope, SCHEMA_VERSION
from shengbo_prelive.observability import Observability, ObservabilityUnavailable, sanitize


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Observability(self.root)
    def tearDown(self):
        self.temporary.cleanup()
    def test_event_uses_shared_strict_contract(self):
        event = self.store.emit("run1", "tool.started", {"name": "lookup", "args": {"product_id": "p1"}}, invocation_id="i1")
        EventEnvelope.model_validate(event)
        self.assertEqual(event["schema_version"], SCHEMA_VERSION)
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(event["type"], "tool.started")
        self.assertIn("timestamp", event)
    def test_concurrent_sequence_persisted_after_reopen_and_cursor_page(self):
        def write(index):
            target = self.store if index % 2 else Observability(self.root)
            return target.emit("run1", "step.progress", {"index": index})
        with ThreadPoolExecutor(max_workers=8) as executor:
            values = list(executor.map(write, range(40)))
        self.assertEqual(sorted(v["sequence"] for v in values), list(range(1, 41)))
        reopened = Observability(self.root)
        page = reopened.events("run1", after=10, limit=5)
        self.assertEqual([e["sequence"] for e in page["events"]], [11, 12, 13, 14, 15])
        self.assertEqual(page["next_cursor"], 15)
        self.assertEqual(reopened.emit("run1", "step.done", {})["sequence"], 41)
        self.assertEqual(reopened.emit("run2", "step.done", {})["sequence"], 1)
    def test_credentials_removed_business_token_and_xss_text_preserved(self):
        unsafe = {"Authorization": "Bearer fake-auth-value", "Cookie": "fake-cookie-value", "api_key": "fake-key-value",
                  "nested": {"access_token": "fake-access-value", "refresh_token": "fake-refresh-value", "id_token": "fake-id-value"},
                  "description": "see sk-fake01234567890123456789 and Bearer fake-bearer-value",
                  "token_count": 5, "token": "业务优惠券标记", "html": "<script>alert('x')</script>",
                  "output": [{"type": "reasoning", "summary": [{"text": "fake-private-thought"}]}]}
        ref = self.store.payload("run1", unsafe)
        recovered = self.store.read_payload("run1", ref)
        text = json.dumps(recovered, ensure_ascii=False)
        for secret in ["fake-auth-value", "fake-cookie-value", "fake-key-value", "fake-access-value", "fake-refresh-value", "fake-id-value", "sk-fake01234567890123456789", "fake-bearer-value", "fake-private-thought"]:
            self.assertNotIn(secret, text)
        self.assertEqual(recovered["token"], "业务优惠券标记")
        self.assertEqual(recovered["token_count"], 5)
        self.assertEqual(recovered["html"], "<script>alert('x')</script>")
    def test_exact_ref_run_binding_and_integrity(self):
        ref = self.store.payload("run1", {"ok": True})
        with self.assertRaises(KeyError):
            self.store.read_payload("run2", ref)
        for bad in ["../outside", "C:/private.txt", "payload:../../file"]:
            with self.assertRaises(ValueError):
                self.store.read_payload("run1", bad)
        file = self.store.payload_root / (ref[8:] + ".json")
        file.write_text('{"tampered":true}')
        with self.assertRaisesRegex(ObservabilityUnavailable, "integrity_failed"):
            self.store.read_payload("run1", ref)
    def test_tail_failure_spools_and_does_not_raise_but_next_action_blocked(self):
        with patch.object(self.store, "_connect", side_effect=OSError("fake-secret")):
            self.assertIsNone(self.store.try_emit("run1", "tool.completed", {"result": "saved", "access_token": "fake-secret"}))
        self.assertFalse(self.store.health()["healthy"])
        self.assertEqual(self.store.health()["spooled_events"], 1)
        self.assertNotIn("fake-secret", self.store.spool_path.read_text())
        with self.assertRaises(ObservabilityUnavailable):
            self.store.assert_ready_for_side_effect()
    def test_startup_and_emit_fail_closed(self):
        invalid = self.root / "not-directory"
        invalid.write_text("file")
        with self.assertRaises(ObservabilityUnavailable):
            Observability(invalid)
        with patch.object(self.store, "_connect", side_effect=OSError("fake-secret")):
            with self.assertRaisesRegex(ObservabilityUnavailable, "event_write_failed"):
                self.store.emit("run1", "tool.started", {})
    def test_spool_degradation_persists_and_recovery_is_idempotent(self):
        with patch.object(self.store, "_connect", side_effect=OSError("unavailable")):
            self.store.try_emit("run1", "tool.completed", {"saved": True})
        reopened = Observability(self.root)
        self.assertFalse(reopened.health()["healthy"])
        self.assertEqual(reopened.recover_spool(), {"recovered": 1, "healthy": True})
        self.assertTrue(reopened.health()["healthy"])
        self.assertEqual(len(reopened.events("run1")["events"]), 1)
        self.assertEqual(reopened.recover_spool(), {"recovered": 0, "healthy": True})
        self.assertEqual(len(reopened.events("run1")["events"]), 1)
    def test_embedded_credentials_cycles_and_non_json_error_safe(self):
        value = {"text": 'Authorization: Bearer fake-secret\nhttps://example.test/?access_token=secret123&x=2', "error": RuntimeError("Bearer fake-private-error")}
        value["cycle"] = value
        result = json.dumps(sanitize(value))
        for secret in ["fake-secret", "secret123", "fake-private-error"]:
            self.assertNotIn(secret, result)
    def test_independent_service_credentials_and_installation_identifier_redacted(self):
        secret = "svc_" + "a" * 64
        value = {"agent_to_voice": "opaque-service-value", "controller_to_voice": "opaque-controller-value",
                 "voice_to_agent": "opaque-voice-value", "ext_agent_host_id": "fake-installation-id",
                 "text": "accidental " + secret, "X-Shengbo-Service-Token": "header-service-value"}
        result = json.dumps(sanitize(value))
        for text in ["opaque-service-value", "opaque-controller-value", "opaque-voice-value", "fake-installation-id", secret, "header-service-value"]:
            self.assertNotIn(text, result)


if __name__ == "__main__":
    unittest.main()
