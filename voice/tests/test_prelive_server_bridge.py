"""Legacy/server adapter integration, always with isolated runtime and no .env."""
import asyncio
from copy import deepcopy
import hashlib
import importlib.util
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import wave

from fastapi import HTTPException
from shengbo_contracts.common import Principal, MutationContext
from app.prelive import PreliveService
from app.prelive.storage import digest


@unittest.skipIf(importlib.util.find_spec("httpx") is None, "Bridge dependencies are not installed")
class ServerBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prelive-server-test-")
        self.environment = patch.dict("os.environ", {"SHENGBO_RUNTIME": self.temp.name, "SHENGBO_SKIP_DOTENV": "1", "SHENGBO_DISABLE_AGENT_BRIDGE": "1"})
        self.environment.start()
        from app import server
        from app.prelive_bridge import ExistingVoiceAdapter
        self.server = server
        self.adapter = ExistingVoiceAdapter(server)
        self.service = PreliveService(self.temp.name, self.adapter)
        self.adapter.service = self.service
        self.patches = [patch.object(server, "RUNTIME", Path(self.temp.name)), patch.object(server, "rooms", {}), patch.object(server, "start_lock", asyncio.Lock()), patch.object(server.app.state, "prelive_service", self.service, create=True)]
        for item in self.patches:
            item.start()
        self.product = server.put("product", server.ProductInput(name="桥接测试商品", price=19, selling_points="轻便").record("bridge-product"))

    async def asyncTearDown(self):
        await self.service.close()
        for item in reversed(self.patches):
            item.stop()
        self.environment.stop()
        self.temp.cleanup()

    async def test_legacy_write_cas_history_and_sku(self):
        first = self.server.get("product", self.product["id"])
        stale = deepcopy(first)
        changed = deepcopy(first)
        changed["price"] = 29
        self.server.put("product", changed)
        stale["price"] = 39
        with self.assertRaises(HTTPException) as error:
            self.server.put("product", stale)
        self.assertEqual(error.exception.status_code, 409)
        old = await self.service.query("read_product", {"ref": {"kind": "product", "id": first["id"], "revision": first["_record_revision"], "content_hash": digest({k: v for k, v in first.items() if not k.startswith("_record_")})}})
        self.assertEqual(old["body"]["price"], 19)
        other = self.server.ProductInput(name="另一个", sku_id=self.product["sku_id"], price=2, selling_points="棉质").model_dump()
        other["id"] = "different-product"
        with self.assertRaises(HTTPException):
            self.server.put("product", other)

    async def test_precompiled_queue_unique_sentence_ids_and_no_live_tts(self):
        server = self.server
        config = await server.create_room({"name": "预编译测试", "product_ids": [self.product["id"]]})
        with self.service.store.transaction() as connection:
            room_ref = self.service.store.get(connection, "config", config["id"])["ref"]
            product_ref = self.service.store.get(connection, "product", self.product["id"])["ref"]
        prep_receipt = await self.service.execute_command("create_preparation", {"room_id": config["id"], "room_revision": room_ref["revision"]}, MutationContext(operation_id="prepare"), Principal(kind="human"))
        prep_ref = prep_receipt["data"]["preparation_ref"]
        with self.service.store.transaction(write=True) as connection:
            draft_ref = self.service.store.artifact(connection, "draft", {"preparation_id": prep_ref["id"], "draft": {"tts_selection": {"provider": "system", "voice": "Huihui", "model": "speech-2.8-turbo"}}})
            playlist_ref = self.service.store.artifact(connection, "playlist", {"preparation_id": prep_ref["id"], "items": [], "loop_mode": "once"})
            module_ref = self.service.store.artifact(connection, "module", self.service._module_body("opening_long", ["欢迎了解测试商品。"]))
            module = self.service.store.exact(connection, module_ref)["body"]
            context_ref = self.service.store.artifact(connection, "context", {"preparation_id": prep_ref["id"], "draft_ref": draft_ref})
            script_ref = self.service.store.artifact(connection, "script_revision", {"preparation_id": prep_ref["id"], "product_id": self.product["id"], "product_ref": product_ref, "variant_id": "variant-bridge", "version_number": 1, "script_revision": 1, "module_refs": {"opening_long": module_ref}, "generation_provenance": {"context_ref": context_ref}, "created_at": "2026-10-16T00:00:00+00:00"})
        audio_id = "a" * 64
        path = Path(self.temp.name) / "room_audio" / config["id"] / (audio_id + ".wav")
        path.parent.mkdir(parents=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(22050); output.writeframes(b"\0\0" * 2205)
        asset = {"path": str(path), "audio_id": audio_id, "duration": .1, "url": "/room-audio/" + config["id"] + "/" + audio_id + ".wav", "sha256_bytes": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
        source = module["sentences"][0]
        entries = [{"compiled_entry_id": "entry-" + str(position), "original_position": position, "occurrence_id": "occurrence-" + str(position), "product_id": self.product["id"], "script_revision_ref": script_ref, "opening_mode": "opening_long", "sentences": [{**source, "module_key": "opening_long", "module_ref": module_ref, "requirement_id": "req", "audio": asset}]} for position in (1, 6)]
        publication = {"preparation_id": prep_ref["id"], "draft_ref": draft_ref, "playlist_ref": playlist_ref, "product_refs": [product_ref], "publication_ref": {"kind": "publication", "id": "test-publication", "revision": 1, "content_hash": digest("test"), "schema_version": "PRELIVE-2.3"}, "action_id": "start-test"}
        # Defaults edited after review must not leak into playback, including
        # the insertion voice. The precompiled sentence files stay unchanged.
        changed = server.settings(config["id"])
        changed.update(loop=False, tts_provider="minimax", tts_voice="Wise_Woman")
        server.put("config", changed)
        with patch.object(server, "render", side_effect=AssertionError("precompiled playback must not render")):
            result = await self.adapter.start_publication(config["id"], publication, entries)
            await asyncio.sleep(.08)
        target = server.room_instance(config["id"])
        self.assertEqual(target.state["status"], "ready")
        self.assertTrue(target.state["loop"])
        self.assertEqual(target.state["tts"]["provider"], "system")
        self.assertEqual(result["playback_url"], "/live/" + config["id"])
        self.assertEqual(len({entry["id"] for entry in target.state["queue"]}), 2)
        self.assertEqual({entry["source_sentence_id"] for entry in target.state["queue"]}, {source["id"]})
        self.assertEqual(target.state["queue"][0]["stage"], "opening_long")
        self.assertEqual(target.state["queue"][0]["block_ordinal"], 1)
        target.state["index"] = len(target.state["queue"]) - 1
        await target.advance()
        await asyncio.sleep(.05)
        self.assertEqual(target.state["index"], 0)
        self.assertEqual(target.state["status"], "ready")
        projection = server.get("script", script_ref["id"])
        self.assertTrue(projection["prelive_managed"])
        self.assertTrue(projection["commerce_signature"])
        self.assertFalse(server.commerce.script_is_stale(
            projection, server.get("product", self.product["id"]), server.settings(config["id"])))
        different_config = deepcopy(server.settings(config["id"]))
        different_config["commerce"]["scene"] = "新活动场景"
        self.assertTrue(server.commerce.script_is_stale(
            projection, server.get("product", self.product["id"]), different_config))
        with self.assertRaises(HTTPException):
            await server.edit_script(script_ref["id"], {"lines": ["不可通过旧接口修改。"]})
        await target.action({"action": "stop"})
        self.assertEqual(target.state["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
