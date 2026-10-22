import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import httpx
from fastapi import FastAPI

VOICE_ROOT = Path("D:/Codex project/ecommerce-voice-agent")
if str(VOICE_ROOT) not in sys.path:
    sys.path.insert(0, str(VOICE_ROOT))
from app import prelive_bridge as bridge
from app.prelive.storage import BusinessError, digest
from shengbo_contracts.common import canonical_json

FAKE_TOKENS = {"agent_to_voice": "a" * 48, "controller_to_voice": "c" * 48, "voice_to_agent": "v" * 48}


class FakeRoom:
    def __init__(self, identity):
        self.state = {"room_id": identity, "status": "stopped", "epoch": 0, "queue": []}
        self.lock, self.actions = asyncio.Lock(), {}
        self.events, self.current_calls = [], []
    def set_current(self, item):
        self.current_calls.append(item)
        self.state.update(current=deepcopy(item), epoch=self.state["epoch"] + 1, status="preparing")
    def log(self, kind, value):
        self.events.append((kind, value))
    async def broadcast(self):
        pass


def fake_server(root):
    def effective(product, config):
        return {**product, "_room_commerce": config["commerce"]}
    server = SimpleNamespace(RUNTIME=Path(root), start_lock=asyncio.Lock(), rooms={},
                             os=SimpleNamespace(environ={"DEEPSEEK_API_KEY": "unit-test-module-only"}),
                             commerce=SimpleNamespace(effective_product=effective, parse_room=lambda value: value), SIMPLIFY=lambda text: text,
                             settings=lambda identity: {"tts_provider": "system", "tts_voice": "Huihui", "tts_model": "speech-2.8-turbo", "loop": False},
                             voice_options=lambda data: {"provider": data.get("tts_provider", "system"), "voice": data.get("tts_voice", "Huihui"), "model": data.get("tts_model", "speech-2.8-turbo")},
                             uid=lambda prefix: prefix + "_test")
    server.room_instance = lambda identity: server.rooms.setdefault(identity, FakeRoom(identity))
    server.get = lambda kind, identity: {"id": identity, "name": "最新普通编辑", "sale_status": "available"}
    return server


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = fake_server(self.temporary.name)
        self.app = FastAPI()
        with patch.object(bridge, "service_tokens", return_value=FAKE_TOKENS):
            self.service = bridge.install(self.app, self.server)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:18180")
    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.close()
        self.temporary.cleanup()
    async def bootstrap(self):
        result = await self.client.get("/api/assistant/v2/session")
        self.assertEqual(result.status_code, 200)
        self.assertIn("HttpOnly", result.headers["set-cookie"])
        self.assertIn("SameSite=strict", result.headers["set-cookie"])
        return {"Origin": "http://127.0.0.1:18180", "X-CSRF-Token": result.json()["csrf_token"]}
    async def test_bootstrap_and_csrf_origin_gate(self):
        headers = await self.bootstrap()
        body = {"query": "read_capabilities", "args": {}}
        ok = await self.client.post("/api/prelive/v2/queries", json=body, headers=headers)
        self.assertEqual(ok.status_code, 200)
        bad = await self.client.post("/api/prelive/v2/queries", json=body, headers={"Origin": "http://evil.example"})
        self.assertEqual(bad.status_code, 403)
        no_origin = await self.client.post("/api/prelive/v2/queries", json=body, headers={"X-CSRF-Token": headers["X-CSRF-Token"]})
        self.assertEqual(no_origin.status_code, 403)
        no_csrf = await self.client.post("/api/prelive/v2/queries", json=body, headers={"Origin": headers["Origin"]})
        self.assertEqual(no_csrf.status_code, 403)
    async def test_service_roles_scope_trust_and_browser_cannot_select_actor(self):
        recorded = []
        async def query(key, args, *, principal=None, scope=None):
            recorded.append((principal, scope))
            return {"ok": True}
        self.service.query = query
        body = {"query": "read_capabilities", "args": {}}
        scope = base64.b64encode(canonical_json({"kind": "preparation", "account_id": "local", "preparation_id": "prep1"}).encode()).decode()
        response = await self.client.post("/api/prelive/v2/queries", json=body,
            headers={"X-Shengbo-Service-Token": FAKE_TOKENS["agent_to_voice"], "X-Shengbo-Run-Id": "run1", "X-Shengbo-Scope": scope})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(recorded[-1][0].kind, "agent")
        self.assertEqual(recorded[-1][0].actor_id, "agent-runtime")
        self.assertEqual(recorded[-1][1].preparation_id, "prep1")
        headers = await self.bootstrap()
        headers.update({"X-Shengbo-Actor-Id": "controller-runtime", "X-Shengbo-Run-Id": "run1", "X-Shengbo-Scope": scope})
        await self.client.post("/api/prelive/v2/queries", json=body, headers=headers)
        self.assertEqual(recorded[-1][0].kind, "human")
        self.assertEqual(recorded[-1][0].actor_id, "local-user")
        self.assertIsNone(recorded[-1][1])
        rejected = await self.client.post("/api/prelive/v2/queries", json=body, headers={"X-Shengbo-Service-Token": "fake-invalid"})
        self.assertEqual(rejected.status_code, 403)
    async def test_proxy_strips_client_auth_and_does_not_return_service_secret(self):
        captured = []
        def upstream(request):
            captured.append(request)
            return httpx.Response(200, json={"ok": True})
        self.app.state.prelive_proxy_transport = httpx.MockTransport(upstream)
        headers = await self.bootstrap()
        headers.update({"Authorization": "Bearer fake-browser", "X-Shengbo-Actor-Id": "forged", "X-Shengbo-Scope": "forged", "X-Shengbo-Service-Token": "forged"})
        result = await self.client.post("/api/assistant/v2/conversations", headers=headers, json={"title": "test"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(captured[0].headers["X-Shengbo-Service-Token"], FAKE_TOKENS["voice_to_agent"])
        for key in ["authorization", "cookie", "x-shengbo-actor-id", "x-shengbo-scope"]:
            self.assertNotIn(key, captured[0].headers)
        self.assertNotIn(FAKE_TOKENS["voice_to_agent"], result.text)
    async def test_sse_proxy_is_subscriber_only_no_run_cancel(self):
        calls = []
        def upstream(request):
            calls.append(request.url.path)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text="data: {\"type\":\"run.progress\"}\n\n")
        self.app.state.prelive_proxy_transport = httpx.MockTransport(upstream)
        await self.bootstrap()
        response = await self.client.get("/api/assistant/v2/runs/run1/events")
        self.assertEqual(response.status_code, 200)
        self.assertIn("run.progress", response.text)
        self.assertEqual(calls, ["/api/assistant/v2/runs/run1/events"])
    async def test_install_idempotent_and_lifespan_only_wakes_worker(self):
        self.assertIsNone(self.service._background)
        self.assertIs(bridge.install(self.app, self.server), self.service)
        async with self.app.router.lifespan_context(self.app):
            self.assertIsNotNone(self.service._background)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = fake_server(self.temporary.name)
        self.adapter = bridge.ExistingVoiceAdapter(self.server)
    async def asyncTearDown(self):
        self.temporary.cleanup()
    def audio(self, text="测试"):
        identity = hashlib.sha256(text.encode()).hexdigest()
        path = self.server.RUNTIME / "room_audio" / "room1" / (identity + ".wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 1600)
        return {"audio_id": identity, "path": str(path), "duration": 0.1, "sha256_bytes": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
    async def test_render_reuses_engine_and_validates_room_audio_wav(self):
        def render(text, directory, voice, provider, model):
            asset = self.audio(text)
            return {"audio_id": asset["audio_id"], "duration": asset["duration"]}
        self.server.render = render
        result = await self.adapter.render_audio("测试", "room1", {"provider": "system", "voice": "Huihui", "model": "speech-2.8-turbo"})
        self.assertTrue(Path(result["path"]).is_file())
        self.assertTrue(result["url"].startswith("/room-audio/room1/"))
    async def test_generate_module_uses_immutable_context_with_strict_one_module_http(self):
        captured = []
        def generated(request):
            captured.append(json.loads(json.loads(request.content)["messages"][1]["content"]))
            result = {"module_key": "selling_points", "sentences": [{"display_text": "这款商品轻便。", "spoken_text": "这款商品轻便。", "kind": "product_claim", "fact_ids": ["SELLING"]}]}
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}], "usage": {"total_tokens": 100}})
        self.server._generate_remote_once = lambda *a: self.fail("must not request or extract a whole script")
        self.adapter.module_transport = httpx.MockTransport(generated)
        draft_ref = {"kind": "draft", "id": "draft1", "revision": 1}
        context = {"draft_ref": draft_ref, "records": [{"ref": draft_ref, "body": {"draft": {"commerce": {"scene": "当前活动"}}}}], "generation_instruction": "突出用途"}
        result = await self.adapter.generate_module(context, "selling_points", {"id": "p1", "name": "测试", "price": 10, "currency": "CNY", "selling_points": "轻便"}, [])
        self.assertEqual(result["sentences"], ["这款商品轻便。"])
        self.assertEqual(result["usage"], {"total_tokens": 100})
        self.assertEqual(captured[0]["effective_product_snapshot"]["_room_commerce"], {"scene": "当前活动"})
        self.assertEqual(captured[0]["context_bundle"], context)
        self.assertEqual(captured[0]["generation_instruction"], "突出用途")
        self.assertNotIn("must_mention_each_version", captured[0])

    async def test_generate_module_rejects_whole_script_instead_of_stage_filtering(self):
        def whole_script(_):
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"versions": [{"blocks": []}]})}}]})
        self.adapter.module_transport = httpx.MockTransport(whole_script)
        context = {"records": [{"body": {"draft": {"commerce": {}}}}]}
        with self.assertRaises(BusinessError) as error:
            await self.adapter.generate_module(context, "selling_points", {"id": "p1", "name": "测试", "price": 10, "currency": "CNY"}, [])
        self.assertEqual(error.exception.code, "MODULE_RESPONSE_INVALID")
    async def test_precompiled_prepare_does_not_depend_on_old_script_or_ordinary_head_change(self):
        target = FakeRoom("room1")
        target.state.update(epoch=1, status="preparing")
        item = {"id": "s1", "product_id": "p1", "pinned_product_ref": {"revision": 1}, "prelive_audio": self.audio()}
        await bridge.prepare_precompiled(target, 1, item, self.server)
        self.assertEqual(target.state["status"], "ready")
        self.assertIn("audio", target.state)
        self.server.get = lambda kind, identity: {"sale_status": "unavailable"}
        target.state["status"] = "preparing"
        await bridge.prepare_precompiled(target, 1, item, self.server)
        self.assertEqual(target.state["status"], "error")
    async def test_start_publication_preserves_exact_compiled_entry_order_without_legacy_compile(self):
        from app.prelive.service import PreliveService
        service = PreliveService(self.server.RUNTIME, self.adapter)
        self.adapter.service = service
        with service.store.transaction(write=True) as connection:
            product_ref = service.store.put(connection, "product", "p1", {"id": "p1", "name": "采用时商品", "price": 20, "currency": "CNY"}, create=True)
            script_ref = service.store.artifact(connection, "script_revision", {"product_id": "p1", "version_number": 3, "script_revision": 2, "variant_id": "variant3"})
            draft_ref = service.store.artifact(connection, "draft", {"preparation_id": "prep1", "draft": {"tts_selection": {"provider": "system", "voice": "Huihui", "model": "speech-2.8-turbo"}}})
            playlist_ref = service.store.artifact(connection, "playlist", {"preparation_id": "prep1", "loop_mode": "infinite"})
        publication = {"preparation_id": "prep1", "draft_ref": draft_ref, "playlist_ref": playlist_ref, "product_refs": [product_ref], "publication_ref": {"kind": "publication", "id": "pub1", "revision": 1, "content_hash": "test"}, "action_id": "start1"}
        entry = {"compiled_entry_id": "e1", "original_position": 6, "occurrence_id": "o1", "product_id": "p1",
                 "script_revision_ref": script_ref, "opening_mode": "opening_long",
                 "sentences": [{"id": "s1", "text": "采用时稿件。", "spoken_text": "采用时稿件。", "audio": self.audio()}]}
        result = await self.adapter.start_publication("room1", publication, [entry])
        target = self.server.rooms["room1"]
        queue = target.state["queue"]
        self.assertEqual(queue[0]["original_position"], 6)
        self.assertEqual(queue[0]["product_name"], "采用时商品")
        self.assertEqual(queue[0]["text"], "采用时稿件。")
        self.assertEqual(queue[0]["opening_mode"], "opening_long")
        self.assertEqual(queue[0]["version_number"], 3)
        self.assertEqual(queue[0]["revision"], 2)
        self.assertEqual(queue[0]["script_revision_ref"], script_ref)
        self.assertTrue(target.state["loop"])
        self.assertEqual(result["playback_url"], "/live/room1")
        self.assertEqual(len(target.current_calls), 1)
        self.assertEqual((await self.adapter.start_publication("room1", publication, [entry]))["session_id"], result["session_id"])
        self.assertEqual(len(target.current_calls), 1)


if __name__ == "__main__":
    unittest.main()
