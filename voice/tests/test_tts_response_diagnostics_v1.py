"""Safe MiniMax diagnostics and pre-validation public usage, entirely offline."""
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
from copy import deepcopy

from app import speech
from app.prelive.storage import BusinessError
from app.prelive_bridge import ExistingVoiceAdapter
from test_speech_style import wav_bytes
import test_prelive_service as business_fixture
import test_formal_audio_style_v1 as formal_fixture


KEY = "sk-offline-secret-never-record"
VOICE = {"provider": "minimax", "voice": "Friendly_Person", "model": "speech-2.8-turbo"}
TEXT = "欢迎了解这款商品。"


def response(payload):
    return io.BytesIO(json.dumps(payload).encode())


class TTSResponseDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tts-diagnostics-offline-")
        self.root = Path(self.temp.name)
        self.key_patch = patch.dict(speech.os.environ, {"MINIMAX_API_KEY": KEY})
        self.key_patch.start()
        self.server = SimpleNamespace(RUNTIME=self.root, render=speech.render,
            settings=lambda room_id: {}, voice_options=lambda config: dict(VOICE))
        self.adapter = ExistingVoiceAdapter(self.server)

    def tearDown(self):
        self.key_patch.stop()
        self.temp.cleanup()

    def assert_safe(self, error):
        serialized = json.dumps(error.payload() if isinstance(error, BusinessError) else error.diagnostics)
        self.assertNotIn(KEY, serialized + str(error) + repr(error))
        self.assertNotIn("Authorization", serialized)
        self.assertNotIn("headers", serialized)
        self.assertNotIn("status_msg", serialized)
        self.assertNotIn("https://", serialized)
        self.assertNotIn('"audio":', serialized)

    async def test_explicit_provider_rejections_are_known_without_parsing_message(self):
        for code in (1002, 1004, 1008, 1039, 1041, 1045, 2013, 2049, 2062):
            with self.subTest(code=code):
                observed = []
                payload = {"base_resp": {"status_code": code, "status_msg": "超时 Authorization " + KEY},
                    "trace_id": "safe-trace-1", "headers": {"Authorization": KEY}, "untrusted": KEY}
                with patch.object(speech.urllib.request, "urlopen", return_value=response(payload)) as request:
                    with self.assertRaises(BusinessError) as caught:
                        await self.adapter.render_audio(TEXT, "room-test", VOICE, response_observer=observed.append)
                self.assertEqual(request.call_count, 1)
                error = caught.exception
                self.assertEqual(error.code, "TTS_PROVIDER_REJECTED")
                diagnostic = error.details["tts_diagnostics"]
                self.assertEqual(diagnostic["outcome"], "known_rejected")
                self.assertEqual(diagnostic["provider_code"], code)
                self.assertEqual(diagnostic["http_status"], 200)
                self.assertEqual(diagnostic["usage_state"], "unknown")
                self.assertNotIn("usage_characters", diagnostic)
                self.assertEqual(observed, [diagnostic])
                self.assert_safe(error)

    async def test_provider_timeouts_and_internal_errors_keep_outcome_unknown_and_observed_usage(self):
        for code in (1001, 1000, 1024, 1027, 1033, 2064, 2066, 54321):
            with self.subTest(code=code):
                observed = []
                payload = {"base_resp": {"status_code": code, "status_msg": KEY},
                    "extra_info": {"usage_characters": 17, "private": KEY}, "trace_id": "safe-trace-2"}
                with patch.object(speech.urllib.request, "urlopen", return_value=response(payload)) as request:
                    with self.assertRaises(BusinessError) as caught:
                        await self.adapter.render_audio(TEXT, "room-test", VOICE, response_observer=observed.append)
                error = caught.exception
                self.assertEqual(request.call_count, 1)
                self.assertEqual(error.code, "TTS_OUTCOME_UNKNOWN")
                diagnostic = error.details["tts_diagnostics"]
                self.assertEqual(diagnostic["outcome"], "outcome_unknown")
                self.assertEqual(diagnostic["usage_characters"], 17)
                self.assertEqual(diagnostic["usage_state"], "known")
                self.assertEqual(observed, [diagnostic])
                self.assert_safe(error)

    async def test_transport_unknown_keeps_only_exception_type(self):
        for failure in (TimeoutError(KEY), urllib.error.URLError(KEY), ConnectionResetError(KEY)):
            with self.subTest(exception_type=type(failure).__name__):
                observed = []
                with patch.object(speech.urllib.request, "urlopen", side_effect=failure) as request:
                    with self.assertRaises(BusinessError) as caught:
                        await self.adapter.render_audio(TEXT, "room-test", VOICE, response_observer=observed.append)
                self.assertEqual(request.call_count, 1)
                error = caught.exception
                self.assertEqual(error.code, "TTS_OUTCOME_UNKNOWN")
                diagnostic = error.details["tts_diagnostics"]
                self.assertEqual(diagnostic["exception_type"], type(failure).__name__)
                self.assertEqual(diagnostic["stage"], "transport")
                self.assertFalse(diagnostic["response_received"])
                self.assertEqual(observed, [])
                self.assert_safe(error)

    async def test_http_rejection_and_gateway_timeout_are_distinguished_safely(self):
        for status, code, expected in ((401, 1004, "TTS_PROVIDER_REJECTED"),
                (429, 1002, "TTS_PROVIDER_REJECTED"), (504, None, "TTS_OUTCOME_UNKNOWN"),
                (500, None, "TTS_OUTCOME_UNKNOWN"), (429, 1001, "TTS_OUTCOME_UNKNOWN")):
            with self.subTest(http=status, provider_code=code):
                observed = []
                body = {"base_resp": {"status_code": code, "status_msg": KEY}, "trace_id": "http-trace"} if code is not None else {"private": KEY}
                failure = urllib.error.HTTPError("https://untrusted.invalid/" + KEY, status, KEY,
                    {"Authorization": KEY}, response(body))
                with patch.object(speech.urllib.request, "urlopen", side_effect=failure):
                    with self.assertRaises(BusinessError) as caught:
                        await self.adapter.render_audio(TEXT, "room-test", VOICE, response_observer=observed.append)
                error = caught.exception
                self.assertEqual(error.code, expected)
                diagnostic = error.details["tts_diagnostics"]
                self.assertEqual(diagnostic["http_status"], status)
                self.assertEqual(diagnostic["exception_type"], "HTTPError")
                self.assertEqual(diagnostic["stage"], "http_response")
                self.assertEqual(observed, [diagnostic])
                self.assert_safe(error)

    async def test_invalid_json_is_unknown_without_exposing_document(self):
        observed = []
        with patch.object(speech.urllib.request, "urlopen", return_value=io.BytesIO(("Authorization " + KEY).encode())):
            with self.assertRaises(BusinessError) as caught:
                await self.adapter.render_audio(TEXT, "room-test", VOICE, response_observer=observed.append)
        error = caught.exception
        self.assertEqual(error.code, "TTS_OUTCOME_UNKNOWN")
        diagnostic = error.details["tts_diagnostics"]
        self.assertEqual(diagnostic["stage"], "json_decode")
        self.assertEqual(diagnostic["exception_type"], "JSONDecodeError")
        self.assertTrue(diagnostic["response_received"])
        self.assertEqual(observed, [diagnostic])
        self.assert_safe(error)

    async def test_public_usage_is_observed_before_missing_or_corrupt_audio_validation(self):
        for data, stage in ((None, "audio_decode"), ({"audio": "not-hex"}, "audio_decode"),
                ({"audio": b"invalid-wav".hex()}, "audio_validate")):
            with self.subTest(stage=stage, missing=data is None):
                observed = []
                payload = {"base_resp": {"status_code": 0}, "data": data,
                    "extra_info": {"usage_characters": 23, "private": KEY}, "trace_id": "paid-invalid-audio"}
                with patch.object(speech.urllib.request, "urlopen", return_value=response(payload)) as request:
                    with self.assertRaises(BusinessError) as caught:
                        await self.adapter.render_audio(TEXT, "room-test", VOICE, response_observer=observed.append)
                error = caught.exception
                self.assertEqual(request.call_count, 1)
                self.assertEqual(error.code, "TTS_OUTCOME_UNKNOWN")
                self.assertEqual(observed[0]["outcome"], "response_received")
                self.assertEqual(observed[0]["usage_characters"], 23)
                self.assertEqual(observed[0]["usage_state"], "known")
                self.assertEqual(error.details["tts_diagnostics"]["stage"], stage)
                self.assertEqual(error.details["tts_diagnostics"]["usage_characters"], 23)
                self.assertEqual(error.details["tts_diagnostics"]["outcome"], "outcome_unknown")
                self.assertFalse(list(self.root.rglob("*.wav")))
                self.assert_safe(error)

    async def test_valid_audio_observer_precedes_file_write_and_cache_never_rebills_usage(self):
        observed = []
        def observer(diagnostic):
            self.assertFalse(list(self.root.rglob("*.wav")))
            observed.append(diagnostic)
        payload = {"data": {"audio": wav_bytes().hex()}, "base_resp": {"status_code": 0},
            "extra_info": {"usage_characters": 9}, "trace_id": "valid-trace"}
        with patch.object(speech.urllib.request, "urlopen", return_value=response(payload)) as request:
            first = await self.adapter.render_audio(TEXT, "room-test", VOICE, response_observer=observer)
            cached = await self.adapter.render_audio(TEXT, "room-test", VOICE, response_observer=observer)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["usage_characters"], 9)
        self.assertTrue(Path(first["path"]).is_file())
        self.assertEqual(first["response_diagnostics"], observed[0])
        self.assertTrue(cached["cache_hit"])
        self.assertNotIn("usage_characters", cached)
        self.assertNotIn("response_diagnostics", cached)

    async def test_observer_failure_is_unknown_without_saving_or_repeating_audio(self):
        def fail_to_record(diagnostic):
            raise RuntimeError(KEY)
        payload = {"data": {"audio": wav_bytes().hex()}, "base_resp": {"status_code": 0},
            "extra_info": {"usage_characters": 9}, "trace_id": "observed-not-saved"}
        with patch.object(speech.urllib.request, "urlopen", return_value=response(payload)) as request:
            with self.assertRaises(BusinessError) as caught:
                await self.adapter.render_audio(TEXT, "room-test", VOICE, response_observer=fail_to_record)
        error = caught.exception
        self.assertEqual(request.call_count, 1)
        self.assertEqual(error.code, "TTS_OUTCOME_UNKNOWN")
        self.assertEqual(error.details["tts_diagnostics"]["stage"], "observation")
        self.assertEqual(error.details["tts_diagnostics"]["usage_characters"], 9)
        self.assertFalse(list(self.root.rglob("*.wav")))
        self.assert_safe(error)

    async def test_unknown_untyped_adapter_error_does_not_classify_its_prose(self):
        self.server.render = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("限流 鉴权 超时 " + KEY))
        with self.assertRaises(BusinessError) as caught:
            await self.adapter.render_audio(TEXT, "room-test", VOICE)
        error = caught.exception
        self.assertEqual(error.code, "TTS_OUTCOME_UNKNOWN")
        self.assertEqual(error.details["tts_diagnostics"]["stage"], "adapter")
        self.assert_safe(error)

    async def test_secret_or_malformed_trace_is_omitted_and_missing_usage_stays_unknown(self):
        for trace in (KEY, "trace-" + KEY, "Authorization: " + KEY, "line\nbreak"):
            with self.subTest(trace_kind="unsafe-trace"):
                payload = {"base_resp": {"status_code": 1002}, "trace_id": trace,
                    "extra_info": {"usage_characters": True}}
                with patch.object(speech.urllib.request, "urlopen", return_value=response(payload)):
                    with self.assertRaises(BusinessError) as caught:
                        await self.adapter.render_audio(TEXT, "room-test", VOICE)
                diagnostic = caught.exception.details["tts_diagnostics"]
                self.assertNotIn("trace_id", diagnostic)
                self.assertEqual(diagnostic["usage_state"], "unknown")
                self.assertNotIn("usage_characters", diagnostic)
                self.assert_safe(caught.exception)


class TTSLedgerDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    """The service remains the sole writer of exact-attempt usage/state."""
    cmd = business_fixture.BusinessTests.cmd
    product = business_fixture.BusinessTests.product
    prep = business_fixture.BusinessTests.prep
    state = business_fixture.BusinessTests.state
    prepared_text = business_fixture.BusinessTests.prepared_text
    compiled = formal_fixture.FormalAudioStyleTests.compiled
    recompile = formal_fixture.FormalAudioStyleTests.recompile
    approve = formal_fixture.FormalAudioStyleTests.approve
    asyncTearDown = business_fixture.BusinessTests.asyncTearDown

    async def asyncSetUp(self):
        await business_fixture.BusinessTests.asyncSetUp(self)
        self.service.wake_worker = lambda: None

    async def run_response(self, payload, *, validation_checks=None):
        room, prep = await self.compiled()
        review, approval = await self.approve(prep)
        server = SimpleNamespace(RUNTIME=Path(self.temp.name), render=speech.render,
            settings=lambda room_id: {}, voice_options=lambda config: {
                "provider": config["tts_provider"], "voice": config["tts_voice"], "model": config["tts_model"]})
        self.service.adapter = ExistingVoiceAdapter(server)
        state = await self.state(prep)
        result = await self.cmd("prepare_required_audio", {"preparation_id": prep["id"],
            "expected_revision": state["ref"]["revision"], "approval_ref": approval,
            "review_hash": review["review_hash"], "expected_generation_epoch": review["generation_epoch"]})
        identity = result["data"]["job_id"]
        original_open = speech.wave.open
        def validate_after_usage(file, *args, **kwargs):
            if validation_checks is not None and isinstance(file, io.BytesIO):
                with self.service.store.transaction() as c:
                    job = self.service.store.get(c, "prelive_job", identity)["body"]
                # At least the response whose WAV is now validated has already
                # durably recorded usage; no response observation marks audio
                # succeeded or changes its still-sent call state.
                validation_checks.append(any(i["call_state"] == "sent"
                    and any(a.get("usage", {}).get("usage_characters") == 31
                            and a.get("response_diagnostics", {}).get("outcome") == "response_received"
                            for a in i.get("provider_attempts", [])) for i in job["items"]))
            return original_open(file, *args, **kwargs)
        with patch.dict(speech.os.environ, {"MINIMAX_API_KEY": KEY}), \
                patch.object(speech.urllib.request, "urlopen", side_effect=lambda *args, **kwargs: response(payload)) as request, \
                patch.object(speech.wave, "open", side_effect=validate_after_usage):
            await self.service.drain_jobs()
        body = (await self.service.query("read_job", {"job_id": identity}))["body"]
        self.assertEqual(request.call_count, len(review["texts"]))
        with self.service.store.transaction() as c:
            events = [(row["seq"], row["event_type"], json.loads(row["payload"])) for row in c.execute(
                "SELECT seq,event_type,payload FROM prelive_events WHERE json_extract(payload,'$.job_id')=? ORDER BY seq", (identity,))]
            external = [r["body"] for r in self.service.store.all(c, "prelive_external_request") if r["body"]["job_id"] == identity]
        for item in body["items"]:
            observed = [seq for seq, kind, data in events if kind == "provider.audio_observed" and data["item_id"] == item["item_id"]]
            failed = [seq for seq, kind, data in events if kind == "job.item_failed" and data["item_id"] == item["item_id"]]
            self.assertTrue(observed and failed)
            self.assertLess(min(observed), min(failed))
        self.assertNotIn(KEY, json.dumps(body) + json.dumps(events))
        self.assertIsNone((await self.state(prep))["body"]["audio_manifest_ref"])
        return body, external

    async def test_explicit_rejection_is_known_failed_without_fabricated_zero_usage(self):
        body, external = await self.run_response({"base_resp": {"status_code": 1002, "status_msg": KEY}, "trace_id": "rate-rejected"})
        self.assertEqual(body["status"], "failed")
        self.assertTrue(all(row["state"] == "known_failed" for row in external))
        for item in body["items"]:
            self.assertEqual(item["status"], "failed")
            attempt = item["provider_attempts"][0]
            self.assertEqual(attempt["state"], "known_failed")
            self.assertEqual(attempt["usage_state"], "unknown")
            self.assertEqual(attempt["usage"], {})
            self.assertEqual(attempt["response_diagnostics"]["provider_code"], 1002)

    async def test_provider_timeout_keeps_unknown_state_with_known_usage_and_old_history(self):
        historical = {"id": "historical-unknown", "kind": "audio", "status": "interrupted",
            "items": [{"item_id": "old-item", "status": "interrupted", "call_state": "timeout_unknown",
                       "provider_attempts": [{"attempt_id": "old-attempt", "state": "timeout_unknown", "usage": {}, "usage_state": "unknown"}]}]}
        with self.service.store.transaction(write=True) as c:
            self.service.store.put(c, "prelive_job", historical["id"], historical, create=True)
        body, external = await self.run_response({"base_resp": {"status_code": 1001},
            "extra_info": {"usage_characters": 17}, "trace_id": "timeout-observed"})
        self.assertEqual(body["status"], "interrupted")
        self.assertTrue(all(row["state"] == "timeout_unknown" for row in external))
        for item in body["items"]:
            self.assertEqual(item["call_state"], "timeout_unknown")
            attempt = item["provider_attempts"][0]
            self.assertEqual(attempt["usage"], {"usage_characters": 17})
            self.assertEqual(attempt["usage_state"], "known")
            self.assertEqual(attempt["response_diagnostics"]["provider_code"], 1001)
        with self.service.store.transaction() as c:
            self.assertEqual(self.service.store.get(c, "prelive_job", historical["id"])["body"], historical)

    async def test_invalid_paid_audio_records_usage_before_validation_and_stays_unknown(self):
        validation_checks = []
        body, external = await self.run_response({"base_resp": {"status_code": 0},
            "data": {"audio": b"invalid-wave".hex()}, "extra_info": {"usage_characters": 31},
            "trace_id": "invalid-wav-observed"}, validation_checks=validation_checks)
        self.assertTrue(validation_checks and all(validation_checks))
        self.assertEqual(body["status"], "interrupted")
        self.assertTrue(all(row["state"] == "timeout_unknown" for row in external))
        for item in body["items"]:
            attempt = item["provider_attempts"][0]
            self.assertEqual(item["call_state"], "timeout_unknown")
            self.assertEqual(attempt["usage"], {"usage_characters": 31})
            self.assertEqual(attempt["response_diagnostics"]["stage"], "audio_validate")
            self.assertEqual(attempt["response_observations"][0]["stage"], "provider_response")


if __name__ == "__main__":
    unittest.main()
