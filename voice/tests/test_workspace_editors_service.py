"""Real PRELIVE-2.3 routes and schema in an isolated temporary DB + Chromium.

Only the model/TTS adapter is fake; the browser has no mocked business API.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import wave

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.prelive.service import PreliveService, Product
from app.prelive.routes import create_router
from shengbo_contracts.common import MutationContext, Principal


class FixtureAdapter:
    def __init__(self, root):
        self.root = Path(root)
        self.text_calls = 0
        self.audio_calls = 0

    async def generate_module(self, context, module_key, product, previous):
        self.text_calls += 1
        return [product["name"] + "的" + module_key + "测试话术。", "这是一条隔离测试句子。"]

    async def render_audio(self, text, room_id, voice):
        self.audio_calls += 1
        identity = hashlib.sha256((text + str(voice)).encode()).hexdigest()
        target = self.root / "room_audio" / room_id / (identity + ".wav")
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(22050)
            output.writeframes(b"\0\0" * 2205)
        return {"path": str(target), "audio_id": identity, "duration": .1,
                "url": "/room-audio/" + room_id + "/" + identity + ".wav"}

    def voice_options(self):
        return {"providers": ["system", "minimax"], "voices": {"system": ["Huihui"], "minimax": ["Wise_Woman"]},
                "models": {"system": ["speech-2.8-turbo"], "minimax": ["speech-2.8-turbo"]}}


class BrowserServiceContractTest(unittest.TestCase):
    def test_real_schema_manual_workflow(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="shengbo-workbench-ui-") as directory:
            adapter = FixtureAdapter(directory)
            service = PreliveService(directory, adapter)
            human = Principal(kind="human", actor_id="browser-fixture")

            async def seed():
                async def command(key, args):
                    return (await service.execute_command(key, args, MutationContext(operation_id="seed-" + key), human))["data"]
                product = await command("save_product", {"schema_hash": service.schema()["schema_hash"], "patches": [],
                    "fields": Product(name="测试足弓拖鞋", price=178, currency="TWD", material="EVA", selling_points="足弓支撑，便于清洗").model_dump()})
                room = await command("create_room", {"name": "隔离浏览器测试直播间"})
                prep = await command("create_preparation", {"room_id": room["room_ref"]["id"], "room_revision": room["room_ref"]["revision"]})
                return {"product_id": product["product_ref"]["id"], "room_id": room["room_ref"]["id"], "preparation_id": prep["preparation_ref"]["id"]}

            seeded = asyncio.run(seed())

            @asynccontextmanager
            async def lifespan(_app):
                yield
                await service.close()

            app = FastAPI(lifespan=lifespan)
            app.include_router(create_router(service, lambda _request: human))
            app.mount("/web", StaticFiles(directory=str(project / "web")), name="test-web")

            @app.get("/")
            async def index():
                return HTMLResponse('<!doctype html><meta charset="utf-8"><main id="editor"></main><script>window.seed=' + json.dumps(seeded) + ';</script><script src="/web/workspace-editors.js"></script>')

            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
            thread.start()
            try:
                for _ in range(100):
                    if server.started:
                        break
                    time.sleep(.03)
                self.assertTrue(server.started)
                environment = dict(os.environ)
                environment["NODE_PATH"] = str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules")
                result = subprocess.run([shutil.which("node"), str(project / "web/workspace-editors.service.test.cjs"), f"http://127.0.0.1:{port}"],
                    cwd=project, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=100)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout.strip().splitlines()[-1])
                if environment.get("SHENGBO_EDITOR_TEST_REPORT"):
                    destination = Path(environment["SHENGBO_EDITOR_TEST_REPORT"])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    report.update(text_adapter_calls=adapter.text_calls, audio_adapter_calls=adapter.audio_calls,
                                  model_adapter="isolated deterministic fake", tts_adapter="isolated silent WAV fake",
                                  credentials_read=False, formal_database_written=False)
                    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                self.assertEqual(report["status"], "passed")
                self.assertGreaterEqual(report["checks"], 17)
                self.assertEqual(adapter.text_calls, 5)
                self.assertGreater(adapter.audio_calls, 0)
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()


if __name__ == "__main__":
    unittest.main()
