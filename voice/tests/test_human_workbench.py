"""Real helper routes and SQLite, with no credentials, providers or formal data."""
from copy import deepcopy
import asyncio
import tempfile
import threading
import unittest
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException
from shengbo_contracts.common import Principal, MutationContext
from app.prelive import PreliveService, create_router
from app.prelive.storage import BusinessError
from app.prelive.human_workbench import install_human_workbench


class HumanWorkbenchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = PreliveService(self.temp.name)
        self.actor = Principal(kind="human", actor_id="ui-test")
        self.render_calls = []
        def voice_options(value):
            if value.get("tts_provider") not in {"system", "minimax"} or value.get("tts_voice") not in {"Huihui", "Wise_Woman"}:
                raise HTTPException(400, "不支持的声音")
        def render(*args):
            self.render_calls.append(args)
            directory = args[1]
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "fixture.wav").write_bytes(b"fixture-audio")
            return {"url": "/audio/fixture.wav", "duration": 1.0}
        from pathlib import Path
        self.server = SimpleNamespace(voice_options=voice_options, sentence_style=lambda *_: {}, render=render, RUNTIME=Path(self.temp.name), POOL=None)
        self.app = FastAPI()
        def identity(request, write=False):
            if write and request.headers.get("x-csrf-token") != "fixture":
                raise BusinessError("CSRF_REJECTED", status=403)
            return self.actor, None
        install_human_workbench(self.app, self.server, self.service, identity)
        self.app.include_router(create_router(self.service, lambda _: self.actor))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://local.test", trust_env=False, headers={"X-CSRF-Token": "fixture"})
        self.op = 0
        result = await self.cmd("create_room", {"name": "原直播间"})
        self.room = result["data"]["room_ref"]
        with self.service.store.transaction(write=True) as c:
            self.product = self.service.store.put(c, "product", "product-test", {"id": "product-test", "name": "测试拖鞋", "price": 178, "currency": "TWD", "selling_points": "轻便", "sale_status": "available", "archived": False}, create=True)
        prep = await self.cmd("create_preparation", {"room_id": self.room["id"], "room_revision": self.room["revision"]})
        self.prep = prep["data"]["preparation_ref"]
        prep = await self.cmd("update_preparation_products", {"preparation_id": self.prep["id"], "expected_revision": self.prep["revision"], "selected_product_refs": [self.product]})
        self.prep = prep["data"]["preparation_ref"]

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.close()
        self.temp.cleanup()

    async def cmd(self, key, args):
        self.op += 1
        return await self.service.execute_command(key, args, MutationContext(operation_id="seed-" + str(self.op)), self.actor)

    async def post(self, route, body):
        return await self.client.post("/api/prelive/v2/human/" + route, json=body)

    async def test_room_defaults_cas_idempotency_and_readable_receipt(self):
        original = await self.service.query("read_room", {"room_id": self.room["id"]})
        body = {"room_id": self.room["id"], "expected_revision": self.room["revision"], "operation_id": "room-edit", "name": "新的名称", "tts_selection": {"provider": "system", "voice": "Huihui", "model": "speech-2.8-turbo"}, "version_count": 6, "loop_mode": "once"}
        first = await self.post("room-settings", body)
        again = await self.post("room-settings", body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), again.json())
        changed = await self.service.query("read_room", {"room_id": self.room["id"]})
        self.assertEqual(changed["body"]["version_count"], 6)
        self.assertTrue(changed["body"]["loop"], "旧 once 请求也必须兼容为整队列循环")
        self.assertEqual(changed["body"]["commerce"], original["body"]["commerce"])
        self.assertEqual(changed["body"]["product_ids"], original["body"]["product_ids"])
        stale = await self.post("room-settings", {**body, "operation_id": "stale"})
        self.assertEqual(stale.status_code, 409)
        receipt = await self.service.query("read_operation_receipt", {"operation_id": "room-edit"}, principal=self.actor)
        self.assertEqual(receipt["state"], "committed")
        self.assertEqual(receipt["receipt"], first.json())

    async def test_price_preview_is_readonly_and_uses_pinned_product(self):
        with self.service.store.transaction(write=True) as c:
            old = self.service.store.get(c, "product", self.product["id"])
            self.service.store.put(c, "product", self.product["id"], {**old["body"], "price": 200}, expected=old["ref"]["revision"])
        response = await self.post("price-preview", {"preparation_id": self.prep["id"], "expected_revision": self.prep["revision"], "commerce": {"product_overrides": {self.product["id"]: {"mode": "price", "price": 107}}, "coupon": {"mode": "none", "currency": "TWD"}}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["product_ref"], self.product)
        self.assertEqual(response.json()["items"][0]["quote"]["final_price"], 107)
        current = await self.service.query("read_preparation", {"preparation_id": self.prep["id"]})
        self.assertEqual(current["ref"], self.prep)

    async def test_archive_exact_version_and_blocks_preparation(self):
        response = await self.post("product-archive", {"product_ref": self.product, "operation_id": "archive"})
        self.assertEqual(response.status_code, 200, response.text)
        repeat = await self.post("product-archive", {"product_ref": self.product, "operation_id": "archive"})
        self.assertEqual(response.json(), repeat.json())
        with self.service.store.transaction() as c:
            record = self.service.store.get(c, "product", self.product["id"])
            self.assertTrue(record["body"]["archived"])
            prep = self.service.store.get(c, "preparation", self.prep["id"])
            self.assertTrue(self.service._currentness(c, prep["body"])["blockers"])

    async def test_voice_preview_only_explicit_human_and_no_implicit_calls(self):
        self.assertEqual(self.render_calls, [])
        body = {"operation_id": "voice-test", "tts_selection": {"provider": "system", "voice": "Huihui", "model": "speech-2.8-turbo"}}
        self.actor = Principal(kind="agent", run_id="test-run")
        response = await self.post("voice-preview", body)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.render_calls, [])
        self.actor = Principal(kind="human", actor_id="ui-test")
        response = await self.post("voice-preview", body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.render_calls), 1)
        response = await self.client.post("/api/prelive/v2/human/voice-preview", json=body, headers={"X-CSRF-Token": "wrong"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.render_calls), 1)

    async def test_voice_preview_receipts_are_queryable_and_missing_audio_is_not_committed(self):
        body = {"operation_id": "queryable-preview", "tts_selection": {"provider": "minimax", "voice": "Wise_Woman", "model": "speech-2.8-turbo"}}
        response = await self.post("voice-preview", body)
        self.assertEqual(response.status_code, 200, response.text)
        receipt = await self.service.query("read_operation_receipt", {"operation_id": body["operation_id"]}, principal=self.actor)
        self.assertEqual(receipt["state"], "committed")
        self.assertEqual(receipt["receipt"], response.json())
        (self.server.RUNTIME / "audio" / "fixture.wav").unlink()
        receipt = await self.service.query("read_operation_receipt", {"operation_id": body["operation_id"]}, principal=self.actor)
        self.assertEqual(receipt["state"], "audio_unavailable")
        repeat = await self.post("voice-preview", body)
        self.assertEqual(repeat.status_code, 409)
        self.assertEqual(repeat.json()["error"]["code"], "PREVIEW_AUDIO_UNAVAILABLE")
        self.assertEqual(len(self.render_calls), 1)

    async def test_pending_preview_blocks_human_mutation_using_same_operation_id(self):
        entered, release = threading.Event(), threading.Event()
        original = self.server.render
        def render(*args):
            entered.set()
            release.wait(timeout=5)
            return original(*args)
        self.server.render = render
        body = {"operation_id": "shared-pending-id", "tts_selection": {"provider": "system", "voice": "Huihui", "model": "speech-2.8-turbo"}}
        call = asyncio.create_task(self.post("voice-preview", body))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            pending = await self.service.query("read_operation_receipt", {"operation_id": body["operation_id"]}, principal=self.actor)
            self.assertEqual(pending["state"], "pending")
            rejected = await self.post("product-archive", {"product_ref": self.product, "operation_id": body["operation_id"]})
            self.assertEqual(rejected.status_code, 409)
            self.assertEqual(rejected.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
            # The formal product and eventual audition are both unaffected.
            with self.service.store.transaction() as c:
                self.assertFalse(self.service.store.get(c, "product", self.product["id"])["body"]["archived"])
        finally:
            release.set()
            result = await call
        self.assertEqual(result.status_code, 200, result.text)


if __name__ == "__main__":
    unittest.main()
