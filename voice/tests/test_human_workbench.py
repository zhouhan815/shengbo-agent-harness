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

    async def test_script_delete_checks_exact_revision_live_slot_and_replays_once(self):
        with self.service.store.transaction(write=True) as db:
            prep = self.service._prep(db, self.prep["id"])
            base = {"preparation_id": self.prep["id"], "product_id": self.product["id"],
                    "product_ref": self.product, "module_refs": {}, "script_revision": 1}
            removed = self.service.store.artifact(db, "script_revision", {**base, "variant_id": "remove", "version_number": 2})
            kept = self.service.store.artifact(db, "script_revision", {**base, "variant_id": "keep", "version_number": 1})
            items = [{"occurrence_id": key, "product_id": self.product["id"], "script_revision_ref": ref}
                     for key, ref in [("remove-first", removed), ("kept", kept), ("remove-last", removed)]]
            playlist = self.service.store.artifact(db, "playlist", {"preparation_id": self.prep["id"], "items": items,
                "loop_mode": "infinite", "product_group_order": [self.product["id"]]})
            publication = self.service.store.artifact(db, "publication", {"preparation_id": self.prep["id"], "playlist_ref": playlist})
            room = self.service.store.get(db, "config", self.room["id"])
            self.service.store.put(db, "config", room["ref"]["id"], {**room["body"], "adopted_publication_ref": publication},
                                   expected=room["ref"]["revision"])
            prior = self.service._save_prep(db, prep, {**prep["body"], "script_refs": [removed, kept], "playlist_ref": playlist,
                "status": "ready", "candidate_publication_ref": publication, "approval_ref": {"fixture": True},
                "compiled_plan_ref": {"fixture": True}, "requirements_ref": {"fixture": True}, "audio_manifest_ref": {"fixture": True}},
                invalidate=False)
            db.execute("INSERT INTO prelive_live_slot VALUES(1,?,?,?,?)", (self.room["id"], publication["id"], "live", "fixture-start"))
        request = {"preparation_id": self.prep["id"], "expected_revision": prior["preparation_ref"]["revision"],
                   "script_ref": removed, "operation_id": "delete-version"}
        reply = await self.post("script-delete", {**request, "expected_revision": self.prep["revision"]})
        self.assertEqual(reply.json()["error"]["code"], "REVISION_CONFLICT", reply.text)
        reply = await self.post("script-delete", request)
        self.assertEqual(reply.json()["error"]["code"], "ROOM_IS_LIVE", reply.text)
        self.service.release_live_slot(self.room["id"])
        reply = await self.post("script-delete", request)
        self.assertEqual(reply.status_code, 200, reply.text)
        result = reply.json()
        self.assertEqual(result["deleted_script_ref"], removed)
        self.assertEqual(result["removed_occurrence_count"], 2)
        self.assertEqual(result["generation_epoch"], prior["generation_epoch"] + 1)
        self.assertEqual((await self.post("script-delete", request)).json(), result)
        with self.service.store.transaction() as db:
            prep = self.service._prep(db, self.prep["id"])
            self.assertEqual(prep["body"]["script_refs"], [kept])
            self.assertEqual(self.service._artifact(db, result["playlist_ref"])["body"]["items"], [items[1]])
            for key in ("bundle_ref", "plan_ref", "compiled_plan_ref", "requirements_ref", "audio_manifest_ref", "approval_ref", "candidate_publication_ref"):
                self.assertIsNone(prep["body"][key], key)
            self.assertEqual(self.service._script_version_numbers(db, prep["body"])[self.product["id"]], {1, 2})
            self.assertEqual(self.service._artifact(db, removed)["ref"], removed)
            self.assertEqual(self.service.store.get(db, "config", self.room["id"])["body"]["adopted_publication_ref"], publication)
            self.assertEqual(self.service._artifact(db, publication)["body"]["playlist_ref"], playlist)
            with self.assertRaises(BusinessError) as error:
                self.service._publication_review(db, publication)
            self.assertEqual(error.exception.code, "PUBLICATION_SUPERSEDED")
        reply = await self.post("script-delete", {**request, "expected_revision": result["preparation_ref"]["revision"],
                                                "operation_id": "delete-removed-again"})
        self.assertEqual(reply.json()["error"]["code"], "SCRIPT_NOT_IN_PREPARATION", reply.text)
        reply = await self.post("script-delete", {**request, "expected_revision": result["preparation_ref"]["revision"],
                                                "script_ref": kept, "operation_id": "delete-last"})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertIsNone(reply.json()["playlist_ref"])

    async def test_room_defaults_inherit_for_future_drafts(self):
        voice = {"provider": "minimax", "voice": "Wise_Woman", "model": "speech-2.8-turbo"}
        reply = await self.post("room-settings", {
            "room_id": self.room["id"], "expected_revision": self.room["revision"], "operation_id": "future-room-defaults",
            "name": "默认声音房间", "tts_selection": voice, "version_count": 3,
        })
        self.assertEqual(reply.status_code, 200, reply.text)
        room = await self.service.query("read_room", {"room_id": self.room["id"]})
        self.assertEqual(room["body"]["default_tts_selection"], voice)
        saved = await self.cmd("save_session_draft", {"preparation_id": self.prep["id"],
            "expected_revision": self.prep["revision"], "draft": {"theme": "沿用房间默认值", "commerce": {}}})
        snapshot = await self.service.query("get_workspace_snapshot", {"preparation_id": self.prep["id"]})
        self.assertEqual(snapshot["preparation_details"]["draft"]["body"]["draft"]["tts_selection"], voice)
        replacement = {"provider": "system", "voice": "Huihui", "model": "speech-2.8-turbo"}
        updated_room = await self.post("room-settings", {
            "room_id": self.room["id"], "expected_revision": room["ref"]["revision"], "operation_id": "later-room-defaults",
            "name": "已更新默认音色", "tts_selection": replacement, "version_count": 3,
        })
        self.assertEqual(updated_room.status_code, 200, updated_room.text)
        saved = await self.cmd("save_session_draft", {"preparation_id": self.prep["id"],
            "expected_revision": saved["data"]["preparation_ref"]["revision"], "draft": {"theme": "本场仍用之前声音",
                "commerce": {"product_overrides": {self.product["id"]: {"mode": "price", "price": 99,
                    "gifts": "商品专享赠品", "shipping_override": True, "shipping_time": "本场两天内发货"}}}}})
        self.assertEqual(saved["data"]["changed_product_override_ids"], [self.product["id"]])
        snapshot = await self.service.query("get_workspace_snapshot", {"preparation_id": self.prep["id"]})
        draft = snapshot["preparation_details"]["draft"]["body"]["draft"]
        self.assertEqual(draft["tts_selection"], voice)
        self.assertEqual(draft["commerce"]["product_overrides"][self.product["id"]]["price"], 99)
        self.assertEqual(draft["commerce"]["product_overrides"][self.product["id"]]["shipping_time"], "本场两天内发货")

    async def test_catalog_rows_expose_bounded_readable_product_details(self):
        with self.service.store.transaction(write=True) as db:
            head = self.service.store.get(db, "product", self.product["id"])
            body = {**head["body"], "sales_platform": "douyin",
                    "core_selling_points": ["耐磨 · " + "多场景" * 60, "轻量\n好携带", "方便收纳", "第四条不显示"],
                    "knowledge": "有使用说明", "promotion": "老客专享\n组合优惠", "shipping_time": "下单后 2 个工作日内发货",
                    "image": "data:image/svg+xml;base64,AAA="}
            self.service.store.put(db, "product", self.product["id"], body, expected=self.product["revision"])
        response = await self.client.post("/api/prelive/v2/queries", json={"query": "search_products", "args": {}})
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()["items"][0]
        self.assertEqual(row["sales_platform"], "douyin")
        self.assertTrue(row["has_knowledge"])
        self.assertLessEqual(len(row["selling_point_preview"]), 101)
        self.assertEqual(len(row["selling_points_preview"]), 3)
        self.assertEqual(len(row["selling_points_preview"][0]), 64)
        self.assertTrue(row["selling_points_preview"][0].endswith("…"))
        self.assertEqual(row["selling_points_preview"][1:], ["轻量 好携带", "方便收纳"])
        self.assertEqual(row["promotion_preview"], "老客专享 组合优惠")
        self.assertEqual(row["shipping_time_preview"], "下单后 2 个工作日内发货")
        self.assertNotIn("thumbnail_url", row)
        snapshot = await self.service.query("get_workspace_snapshot", {"preparation_id": self.prep["id"]}, principal=self.actor)
        self.assertEqual(snapshot["products"]["items"][0], row)
        agent_page = await self.service.query("search_products", {}, principal=Principal(kind="controller"))
        for key in ("selling_point_preview", "selling_points_preview", "promotion_preview", "shipping_time_preview"):
            self.assertNotIn(key, agent_page["items"][0])

    async def test_catalog_previews_support_legacy_points_without_inferred_promo_or_hidden_shipping(self):
        with self.service.store.transaction(write=True) as db:
            head = self.service.store.get(db, "product", self.product["id"])
            body = {**head["body"], "core_selling_points": [], "selling_points": "旧卖点一\n\n旧卖点二",
                    "promo_price": 1, "shipping_time": "旧的隐藏发货时效", "hidden_optional_fields": ["shipping_time"],
                    "return_policy": "有售后说明"}
            body.pop("promotion", None)
            self.service.store.put(db, "product", self.product["id"], body, expected=self.product["revision"])
        page = await self.service.query("search_products", {}, principal=self.actor)
        row = page["items"][0]
        self.assertEqual(row["selling_point_preview"], "旧卖点一")
        self.assertEqual(row["selling_points_preview"], ["旧卖点一", "旧卖点二"])
        self.assertEqual(row["promotion_preview"], "")
        self.assertEqual(row["shipping_time_preview"], "")

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
