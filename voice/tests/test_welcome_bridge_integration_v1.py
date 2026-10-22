"""Persisted preparation -> real bridge -> real Room, entirely offline.

Only provider HTTP and WAV rendering are fixtures. Business commands, immutable
storage, compilation, publication, source normalization and player acks are real.
"""
import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import httpx
from fastapi import HTTPException
from shengbo_contracts.common import MutationContext, Principal

from app.prelive.service import PreliveService, Product
from app.speech import audio_key
from app.prosody import sentence_style
from app.welcome import build_product_templates, validate_templates


MODULES = ("opening_long", "product_intro", "selling_points", "cta")


class WelcomeBridgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="welcome-fullchain-")
        self.environment = patch.dict("os.environ", {"SHENGBO_RUNTIME": self.temp.name,
            "SHENGBO_SKIP_DOTENV": "1", "SHENGBO_DISABLE_AGENT_BRIDGE": "1",
            "DEEPSEEK_API_KEY": "offline-fixture-only"})
        self.environment.start()
        from app import live_source, server
        from app.prelive_bridge import ExistingVoiceAdapter
        self.server, self.source = server, live_source
        self.provider_requests, self.render_calls = [], []
        def provider(request):
            task = json.loads(json.loads(request.content)["messages"][1]["content"])
            self.provider_requests.append(task)
            texts = {"opening_long": "欢迎了解这款商品。", "product_intro": "这是一款测试口红。",
                     "selling_points": "这款口红采用红色。", "cta": "有需要可以了解一下。"}
            modules = [{"module_key": key, "sentences": [{"text": texts[key],
                "kind": "product_claim" if key in {"product_intro", "selling_points"} else "engagement",
                "fact_ids": ["NAME" if key == "product_intro" else "SELLING"] if key in {"product_intro", "selling_points"} else []}]}
                for key in task["requested_module_keys"]]
            return httpx.Response(200, json={"id": "offline-receipt", "choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps({"modules": modules}, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 40, "total_tokens": 60}})
        self.adapter = ExistingVoiceAdapter(server, module_transport=httpx.MockTransport(provider))
        self.service = PreliveService(self.temp.name, self.adapter)
        self.adapter.service = self.service
        self.service.wake_worker = lambda: None
        self.sequence = 0
        self.patches = [patch.object(server, "RUNTIME", Path(self.temp.name)), patch.object(server, "rooms", {}),
            patch.object(server, "start_lock", asyncio.Lock()),
            patch.object(server.app.state, "prelive_service", self.service, create=True),
            patch.object(server, "render", side_effect=self.render_fixture)]
        for current in self.patches:
            current.start()

    async def asyncTearDown(self):
        for room in list(self.server.rooms.values()):
            if room.state["status"] not in {"stopped", "completed"}:
                await room.action({"action": "stop"})
        await asyncio.sleep(.02)
        await self.service.close()
        for current in reversed(self.patches):
            current.stop()
        self.environment.stop()
        self.temp.cleanup()

    @staticmethod
    def write_wav(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(22050)
            output.writeframes(b"\0\0" * 2205)

    def render_fixture(self, text, directory, voice, provider, model, *style):
        self.render_calls.append({"text": text, "directory": str(directory), "voice": voice, "provider": provider})
        identity = hashlib.sha256((text + voice + str(directory)).encode()).hexdigest()
        self.write_wav(Path(directory) / (identity + ".wav"))
        return {"audio_id": identity, "url": "/audio/" + identity + ".wav", "duration": .1}

    async def command(self, name, args):
        self.sequence += 1
        return (await self.service.execute_command(name, args,
            MutationContext(operation_id="welcome-fullchain-" + str(self.sequence)), Principal(kind="human")))["data"]

    async def preparation(self, *, imported=False):
        product = (await self.command("save_product", {"schema_hash": self.service.schema()["schema_hash"],
            "fields": Product(name="测试口红", price=99, selling_points="红色。").model_dump(), "patches": []}))["product_ref"]
        room = (await self.command("create_room", {"name": "欢迎贯通离线测试"}))["room_ref"]
        prep = (await self.command("create_preparation", {"room_id": room["id"], "room_revision": room["revision"]}))["preparation_ref"]
        selected = await self.command("update_preparation_products", {"preparation_id": prep["id"],
            "expected_revision": prep["revision"], "selected_product_refs": [product]})
        draft = await self.command("save_session_draft", {"preparation_id": prep["id"],
            "expected_revision": selected["preparation_ref"]["revision"], "draft": {"theme": "日常介绍", "commerce": {}}})
        if imported:
            text = "欢迎了解测试口红。\n这款口红采用红色。"
            with self.service.store.transaction(write=True) as connection:
                source = self.service.store.artifact(connection, "source_manifest", {"manifest": {"text": text, "locators": []}})
            imported_script = await self.command("import_script", {"preparation_id": prep["id"],
                "expected_revision": draft["preparation_ref"]["revision"], "product_ref": product,
                "variant_id": "imported-welcome", "source_ref": source, "locator": "full", "text": text})
            self.assertTrue(imported_script["lossless"])
            self.assertEqual(imported_script["provider_calls"], 0)
            script_ref = imported_script["script_ref"]
        else:
            context = await self.command("save_context_bundle", {"preparation_id": prep["id"],
                "expected_revision": draft["preparation_ref"]["revision"], "product_refs": [product], "draft_ref": draft["draft_ref"]})
            plan = await self.command("save_script_plan", {"preparation_id": prep["id"],
                "expected_revision": context["preparation_ref"]["revision"], "bundle_ref": context["context_bundle_ref"],
                "items": [{"product_ref": product, "variant_id": "generated-welcome",
                    "modules": [{"key": key, "action": "generate"} for key in MODULES]}]})
            job = await self.command("submit_script_change", {"preparation_id": prep["id"],
                "expected_revision": plan["preparation_ref"]["revision"], "plan_ref": plan["script_plan_ref"],
                "context_bundle_ref": context["context_bundle_ref"], "expected_generation_epoch": plan["generation_epoch"]})
            await self.service.drain_jobs()
            job_body = (await self.service.query("read_job", {"job_id": job["job_id"]}))["body"]
            self.assertEqual(job_body["status"], "succeeded", job_body)
            script_ref = job_body["items"][0]["output"]["script_ref"]
        script = await self.service.query("read_script", {"ref": script_ref})
        self.assertEqual(validate_templates(script["body"]["welcome_templates"]), build_product_templates({"name": "测试口红"}))
        return product, room, prep, script_ref

    async def enter(self, room_id, upstream="100"):
        state = {"config": {"id": room_id, "purpose": "own_room", "external_room_id": "12345678901"},
                 "capture_id": "offline-capture", "counts": Counter(), "unknown_messages": 0}
        await self.source._publish(room_id, state, {"method": "WebcastMemberMessage",
            "common": {"msgId": upstream}, "user": {"nickname": "小明123＋✨"}})

    async def ready(self, target):
        for _ in range(200):
            if target.state["status"] in {"ready", "error"}:
                break
            await asyncio.sleep(.005)
        self.assertEqual(target.state["status"], "ready", target.snapshot())

    async def ack(self, target, event):
        await target.acknowledge("offline-owner", {"event": event, "epoch": target.state["epoch"],
            "attempt_id": target.state["current"]["attempt_id"]})

    async def publish_and_start(self, product, room, prep, script):
        state = await self.service.query("read_preparation", {"preparation_id": prep["id"]})
        playlist = await self.command("save_playlist", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
            "items": [{"occurrence_id": "welcome-occurrence", "product_id": product["id"], "script_revision_ref": script}], "loop_mode": "infinite"})
        compiled = await self.command("compile_playback_plan", {"preparation_id": prep["id"],
            "expected_revision": playlist["preparation_ref"]["revision"], "playlist_ref": playlist["playlist_ref"]})
        review = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        approved = await self.command("approve_text_for_audio", {"preparation_id": prep["id"],
            "expected_revision": compiled["preparation_ref"]["revision"], "review_hash": review["review_hash"]})
        job = await self.command("prepare_required_audio", {"preparation_id": prep["id"],
            "expected_revision": approved["preparation_ref"]["revision"], "approval_ref": approved["approval_ref"],
            "review_hash": review["review_hash"], "expected_generation_epoch": review["generation_epoch"]})
        await self.service.drain_jobs()
        self.assertEqual((await self.service.query("read_job", {"job_id": job["job_id"]}))["body"]["status"], "succeeded")
        state = await self.service.query("read_preparation", {"preparation_id": prep["id"]})
        publication = await self.command("publish_preparation", {"preparation_id": prep["id"],
            "expected_revision": state["ref"]["revision"], "approval_ref": approved["approval_ref"]})
        pub = publication["publication_ref"]
        check = await self.service.query("create_review", {"ref": pub})
        adopted = await self.command("adopt_snapshot", {"publication_ref": pub,
            "review_hash": check["review_hash"], "expected_room_revision": room["revision"]})
        await self.command("start_prepared_room", {"publication_ref": pub, "review_hash": adopted["review_hash"],
            "expected_room_revision": adopted["room_ref"]["revision"], "action_id": "offline-start"})
        target = self.server.room_instance(room["id"])
        target.owner = "offline-owner"
        await self.ready(target)
        return target

    async def welcome_roundtrip(self, target, *, upstream="101"):
        initial_tts, initial_llm = len(self.render_calls), len(self.provider_requests)
        await self.ack(target, "playing")
        await self.enter(target.state["room_id"], upstream)
        self.assertEqual(target.state["insertion"]["kind"], "welcome")
        pending = target.state["insertion"]["pending"]
        self.assertIn("小明", pending["text"])
        self.assertIn("测试口红", pending["text"])
        self.assertNotIn("123", pending["text"])
        self.assertNotIn("{username}", pending["text"])
        for _ in range(200):
            if target.state["insertion"]["pending"].get("prepared_audio"):
                break
            await asyncio.sleep(.005)
        self.assertTrue(target.state["insertion"]["pending"].get("prepared_audio"))
        self.assertFalse(target.state["current"].get("is_welcome"), "preparation must not cut the current script")
        self.assertEqual(len(self.render_calls), initial_tts + 1, "short welcome warms while script keeps playing")
        await self.ack(target, "ended")
        await self.ready(target)
        self.assertTrue(target.state["current"]["is_welcome"])
        self.assertEqual(len(self.render_calls), initial_tts + 1)
        self.assertEqual(self.render_calls[-1]["provider"], target.state["tts"]["provider"])
        await self.ack(target, "playing")
        self.assertEqual(sum(log["event"] == "welcome_play_started" for log in target.state["logs"]), 1)
        await self.ack(target, "ended")
        await self.ready(target)
        self.assertFalse(target.state["current"].get("is_welcome"))
        self.assertIsNone(target.state["insertion"])
        self.assertEqual(len(self.render_calls), initial_tts + 1, "main queue resumes precompiled audio")
        self.assertEqual(len(self.provider_requests), initial_llm, "entry/tts/play ack never regenerate templates or main script")
        # Durable replay is rejected before another welcome reaches Room.
        await self.enter(target.state["room_id"], upstream)
        self.assertIsNone(target.state["insertion"])

    async def test_generated_saved_templates_bridge_queue_normalized_entry_tts_and_play_ack(self):
        product, room, prep, script = await self.preparation()
        self.assertEqual(len(self.provider_requests), 1)
        target = self.server.room_instance(room["id"])
        self.assertEqual(target.state["queue"], [])
        await self.enter(room["id"])
        self.assertEqual(target.state["status"], "stopped")
        self.assertIsNone(target.state["insertion"], "main script with no audio and no queue cannot start welcome")
        self.assertEqual(self.render_calls, [])
        target = await self.publish_and_start(product, room, prep, script)
        stored = (await self.service.query("read_script", {"ref": script}))["body"]["welcome_templates"]
        self.assertTrue(all(entry["welcome_templates"] == stored for entry in target.state["queue"]))
        await self.welcome_roundtrip(target)

    async def test_imported_script_templates_persist_and_complete_bridge_room_roundtrip(self):
        product, room, prep, script = await self.preparation(imported=True)
        stored = (await self.service.query("read_script", {"ref": script}))["body"]
        self.assertEqual(stored["raw_text"], "欢迎了解测试口红。\n这款口红采用红色。")
        self.assertEqual(self.provider_requests, [])
        target = await self.publish_and_start(product, room, prep, script)
        self.assertTrue(all(entry["welcome_templates"] == stored["welcome_templates"] for entry in target.state["queue"]))
        await self.welcome_roundtrip(target)

    async def test_legacy_revision_prepares_templates_in_projection_and_bridge_without_rewrite(self):
        product, room, prep, script = await self.preparation(imported=True)
        with self.service.store.transaction(write=True) as connection:
            old = deepcopy(self.service.store.exact(connection, script)["body"])
            old.pop("welcome_templates")
            legacy = self.service.store.artifact(connection, "script_revision", old)
            state = self.service.store.get(connection, "preparation", prep["id"])
            body = deepcopy(state["body"])
            body["script_refs"] = [legacy]
            self.service.store.put(connection, "preparation", prep["id"], body, expected=state["ref"]["revision"])
        projected = self.server.get("script", legacy["id"])
        self.assertEqual(projected["welcome_templates"], build_product_templates({"name": "测试口红"}))
        target = await self.publish_and_start(product, room, prep, legacy)
        self.assertTrue(all(entry["welcome_templates"] == projected["welcome_templates"] for entry in target.state["queue"]))
        await self.welcome_roundtrip(target)
        self.assertEqual((await self.service.query("read_script", {"ref": legacy}))["body"], old)

    async def test_ordinary_saved_script_requires_main_audio_and_uses_same_welcome_runtime(self):
        server = self.server
        product = server.put("product", server.ProductInput(name="测试口红", price=99, selling_points="红色。").record("ordinary-welcome"))
        script = server.structure_script(product, {"blocks": [{"stage": "identity", "sentences": [
            {"display_text": "这是一款测试口红。", "kind": "product_claim", "fact_ids": ["NAME"]},
            {"display_text": "欢迎来了解。", "kind": "engagement"}]}]}, "offline", "ordinary")
        server.put("script", script)
        room = await server.create_room({"name": "普通欢迎贯通测试", "product_ids": [product["id"]], "selected_script_ids": [script["id"]]})
        with self.assertRaises(HTTPException) as error:
            await server.room_control(room["id"], {"action": "start"})
        self.assertEqual(error.exception.status_code, 409)
        await self.enter(room["id"])
        target = server.room_instance(room["id"])
        self.assertEqual(target.state["queue"], [])
        self.assertIsNone(target.state["insertion"])
        self.assertEqual(self.render_calls, [])
        directory = server.production.room_directory(room["id"])
        for sentence in script["sentences"]:
            identity = audio_key(sentence["spoken_text"], "Huihui", "system", style=sentence_style(sentence, "system"))
            self.write_wav(directory / (identity + ".wav"))
        await server.room_control(room["id"], {"action": "start"})
        target.owner = "offline-owner"
        await self.ready(target)
        self.assertTrue(all(entry["welcome_templates"] == script["welcome_templates"] for entry in target.state["queue"]))
        await self.welcome_roundtrip(target)


if __name__ == "__main__":
    unittest.main()
