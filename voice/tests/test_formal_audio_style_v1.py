"""Small offline checks for frozen formal-audio style; no real provider calls."""
from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import speech
from app.prelive import PreliveService, BusinessError
from app.prelive_bridge import ExistingVoiceAdapter
from app.prelive.playback_compiler import playback_statistics
from app.prelive.storage import digest
from app.prosody import sentence_style
import test_prelive_service as business_fixture
from test_speech_style import wav_bytes


VOICE = {"provider": "minimax", "voice": "Wise_Woman", "model": "speech-2.8-turbo"}
TEXT = "宝宝，先了解这款商品。"


class FormalAudioStyleTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the existing isolated fixture, not a second business harness.
    asyncSetUp = business_fixture.BusinessTests.asyncSetUp
    asyncTearDown = business_fixture.BusinessTests.asyncTearDown
    cmd = business_fixture.BusinessTests.cmd
    product = business_fixture.BusinessTests.product
    prep = business_fixture.BusinessTests.prep
    state = business_fixture.BusinessTests.state
    prepared_text = business_fixture.BusinessTests.prepared_text

    async def compiled(self):
        async def same_text(*args):
            return [TEXT]
        self.adapter.generate_module = same_text
        product, room, prep, script = await self.prepared_text()
        state = await self.state(prep)
        saved = await self.cmd("save_session_draft", {"preparation_id": prep["id"],
            "expected_revision": state["ref"]["revision"], "draft": {"theme": "日常介绍", "commerce": {}, "tts_selection": VOICE}})
        prep = saved["data"]["preparation_ref"]
        return room, await self.recompile(prep)

    async def recompile(self, prep):
        state = await self.state(prep)
        result = await self.cmd("compile_playback_plan", {"preparation_id": prep["id"],
            "expected_revision": state["ref"]["revision"], "playlist_ref": state["body"]["playlist_ref"]})
        return result["data"]["preparation_ref"]

    async def approve(self, prep):
        state = await self.state(prep)
        review = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        approved = await self.cmd("approve_text_for_audio", {"preparation_id": prep["id"],
            "expected_revision": state["ref"]["revision"], "review_hash": review["review_hash"]})
        return review, approved["data"]["approval_ref"]

    async def render(self, prep, review, approval):
        state = await self.state(prep)
        result = await self.cmd("prepare_required_audio", {"preparation_id": prep["id"],
            "expected_revision": state["ref"]["revision"], "approval_ref": approval,
            "review_hash": review["review_hash"], "expected_generation_epoch": review["generation_epoch"]})
        await self.service.drain_jobs()
        job = await self.service.query("read_job", {"job_id": result["data"]["job_id"]})
        self.assertEqual(job["body"]["status"], "succeeded", job)
        return job["body"]

    async def test_compile_same_text_keeps_distinct_module_styles(self):
        room, prep = await self.compiled()
        review, approval = await self.approve(prep)
        self.assertEqual(len(review["texts"]), 4)
        self.assertEqual({r["style"]["profile_id"] for r in review["texts"]}, {"welcome", "identity", "benefit", "cta"})
        self.assertEqual(len({r["requirement_id"] for r in review["texts"]}), 4)
        for row in review["texts"]:
            self.assertEqual(row["spoken_text"], TEXT)
            self.assertEqual(row["requirement_id"], digest({"spoken_text": TEXT, "voice": VOICE, "style": row["style"]}))
        self.assertEqual((await self.service.query("read_text_approval", {"preparation_id": prep["id"]}))["status"], "current")

    async def test_formal_adapter_actual_mock_http_and_cache_use_frozen_style(self):
        room, prep = await self.compiled()
        review, approval = await self.approve(prep)
        server = SimpleNamespace(RUNTIME=Path(self.temp.name), render=speech.render,
            settings=lambda room_id: {}, voice_options=lambda config: deepcopy(VOICE))
        self.service.adapter = ExistingVoiceAdapter(server)
        sent = []
        def respond(request, timeout):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"data": {"audio": wav_bytes().hex()}, "base_resp": {"status_code": 0}}).encode())
        with patch.dict(speech.os.environ, {"MINIMAX_API_KEY": "unit-test-placeholder"}), patch.object(speech.urllib.request, "urlopen", side_effect=respond), patch("app.prelive.service.sentence_style", side_effect=AssertionError("frozen render must not re-resolve style")):
            job = await self.render(prep, review, approval)
            await self.render(prep, review, approval)
        self.assertEqual(len(sent), 4)
        self.assertEqual({r["voice_setting"]["speed"] for r in sent}, {0.98, 1.0, 1.02})
        self.assertEqual(sum(r["voice_setting"].get("emotion") == "happy" for r in sent), 2)
        for request in sent:
            self.assertEqual(request["text"], TEXT)
            self.assertNotIn("prompt", request)
            self.assertNotIn("direction", request)
            self.assertNotIn("label", request)
        for item in job["items"]:
            self.assertEqual(item["input"]["style"], item["output"]["asset"]["style"])
            self.assertEqual(item["provider_attempts"][0]["input"]["style"], item["input"]["style"])

    async def test_new_style_revision_changes_review_and_requirement_identity(self):
        room, prep = await self.compiled()
        before, approval = await self.approve(prep)
        def updated(sentence, provider):
            return {**sentence_style(sentence, provider), "style_revision": "isolated-next", "speed": 0.91}
        with patch("app.prelive.service.sentence_style", side_effect=updated):
            prep = await self.recompile(prep)
        after = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        self.assertNotEqual(before["review_hash"], after["review_hash"])
        self.assertFalse({r["requirement_id"] for r in before["texts"]} & {r["requirement_id"] for r in after["texts"]})
        with self.assertRaises(BusinessError):
            await self.render(prep, before, approval)
        self.assertFalse(self.adapter.rendered)

    async def test_legacy_minimax_approval_restart_keeps_no_style_three_args(self):
        room, prep = await self.compiled()
        # A historical frozen fixture: old requirements used text+voice only.
        with self.service.store.transaction(write=True) as c:
            state = self.service._prep(c, prep["id"])
            body = deepcopy(state["body"])
            plan = deepcopy(self.service._artifact(c, body["compiled_plan_ref"])["body"])
            rid = digest({"spoken_text": TEXT, "voice": VOICE})
            refs = []
            for entry in plan["entries"]:
                for sentence in entry["sentences"]:
                    sentence.pop("style", None)
                    sentence["requirement_id"] = rid
                    if sentence["module_ref"] not in refs:
                        refs.append(sentence["module_ref"])
            plan["playback_statistics"] = playback_statistics(plan["entries"])
            body["compiled_plan_ref"] = self.service.store.artifact(c, "compiled_plan", plan)
            body["requirements_ref"] = self.service.store.artifact(c, "requirements", {"preparation_id": prep["id"],
                "compiled_plan_ref": body["compiled_plan_ref"], "voice": VOICE,
                "items": [{"requirement_id": rid, "spoken_text": TEXT, "voice": VOICE, "sentence_refs": refs}]})
            self.service._save_prep(c, state, body, invalidate=True)
        review, approval = await self.approve(prep)
        with patch("app.prelive.service.sentence_style", side_effect=AssertionError("legacy must not resolve style")):
            await self.render(prep, review, approval)  # Existing FakeAdapter accepts only three args.
            await self.service.close()
            self.service = PreliveService(self.temp.name, self.adapter)
            current = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
            self.assertEqual(current["review_hash"], review["review_hash"])
            await self.render(prep, review, approval)
        self.assertEqual(self.adapter.rendered, [TEXT])
        self.assertNotIn("style", current["texts"][0])

    async def test_received_styled_wav_recovers_without_render_and_style_mismatch_is_rejected(self):
        room, prep = await self.compiled()
        review, approval = await self.approve(prep)
        req = review["texts"][0]
        path = Path(self.temp.name) / "received.wav"
        path.write_bytes(wav_bytes())
        asset = {"path": str(path), "audio_id": "a" * 64}
        item = {"item_id": "received-item", "input": req, "provider_attempts": [{"purpose": "tts", "state": "result_received", "input": deepcopy(req), "output": asset}]}
        with self.service.store.transaction(write=True) as c:
            self.service.store.put(c, "prelive_job", "received-job", {"items": [item]}, create=True)
        with patch.object(self.adapter, "render_audio", side_effect=AssertionError("must recover archived WAV")):
            result = await self.service._produce_audio({"id": "received-job", "room_id": room["id"]}, item)
        self.assertTrue(result["asset"]["reused"])
        self.assertEqual(result["asset"]["style"], req["style"])
        self.assertFalse(self.service._audio_style_matches({**result["asset"], "style": {"speed": 2}}, req))
        self.assertTrue(self.service._audio_style_matches(asset, {"spoken_text": TEXT}))
