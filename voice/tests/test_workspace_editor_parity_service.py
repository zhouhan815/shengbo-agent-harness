"""New left-side controls against real v2 services; isolated DB and fake audio."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.prelive.human_workbench import install_human_workbench
from app.prelive.routes import create_router
from app.prelive.service import PreliveService, Product
from shengbo_contracts.common import MutationContext, Principal
from tests.test_workspace_editors_service import FixtureAdapter


class LeftEditorParityIntegration(unittest.TestCase):
    def test_room_history_copy_and_full_commerce(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="shengbo-left-parity-") as directory:
            adapter = FixtureAdapter(directory)
            service = PreliveService(directory, adapter)
            human = Principal(kind="human", actor_id="left-parity-fixture")

            async def seed():
                index = 0
                async def cmd(name, args):
                    nonlocal index
                    index += 1
                    return (await service.execute_command(name, args, MutationContext(operation_id="seed-" + str(index)), human))["data"]
                product = await cmd("save_product", {"schema_hash": service.schema()["schema_hash"], "patches": [], "fields": Product(name="隔离测试拖鞋", price=178, currency="TWD", selling_points="足弓支撑").model_dump()})
                room = await cmd("create_room", {"name": "旧房间名称"})
                prep = await cmd("create_preparation", {"room_id": room["room_ref"]["id"], "room_revision": room["room_ref"]["revision"], "goal": "上一场中秋活动"})
                prep = await cmd("update_preparation_products", {"preparation_id": prep["preparation_ref"]["id"], "expected_revision": prep["preparation_ref"]["revision"], "selected_product_refs": [product["product_ref"]]})
                prep = await cmd("save_session_draft", {"preparation_id": prep["preparation_ref"]["id"], "expected_revision": prep["preparation_ref"]["revision"], "draft": {"theme": "中秋活动", "campaign_ends_at": "2020-09-25T23:00:00+08:00", "commerce": {"promotion": "中秋限定赠品", "knowledge_documents": [{"id": "longterm", "title": "长期售后", "content": "尺寸不合适请联系售后"}, {"id": "dated", "title": "2020年中秋活动", "content": "中秋赠品"}], "product_overrides": {product["product_ref"]["id"]: {"mode": "price", "price": 107, "gifts": "限时赠品"}}}}})
                return {"product_id": product["product_ref"]["id"], "room_id": room["room_ref"]["id"], "preparation_id": prep["preparation_ref"]["id"]}

            seeded = asyncio.run(seed())
            def voice_options(value):
                return {"provider": value.get("tts_provider", "system"), "voice": value.get("tts_voice", "Huihui"), "model": value.get("tts_model", "speech-2.8-turbo")}
            def render(text, _directory, voice, provider, model, _style):
                result = asyncio.run(adapter.render_audio(text, "preview", {"provider": provider, "voice": voice, "model": model}))
                target = Path(_directory) / Path(result["path"]).name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(result["path"], target)
                return {**result, "path": str(target), "url": "/audio/" + target.name}
            fake_server = SimpleNamespace(voice_options=voice_options, sentence_style=lambda *_: {}, render=render, RUNTIME=Path(directory), POOL=None)
            @asynccontextmanager
            async def lifespan(_):
                yield
                await service.close()
            app = FastAPI(lifespan=lifespan)
            app.include_router(create_router(service, lambda _: human))
            install_human_workbench(app, fake_server, service, lambda _request, **_: (human, None))
            app.mount("/web", StaticFiles(directory=str(project / "web")))
            @app.get("/")
            async def index():
                return HTMLResponse('<!doctype html><meta charset="utf-8"><main id="editor"></main><script>window.seed=' + json.dumps(seeded) + ';</script><script src="/web/workspace-editors.js"></script>')
            sock=socket.socket();sock.bind(("127.0.0.1", 0));port=sock.getsockname()[1]
            server=uvicorn.Server(uvicorn.Config(app, log_level="error"));thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
            try:
                for _ in range(100):
                    if server.started: break
                    time.sleep(.03)
                self.assertTrue(server.started)
                env=dict(os.environ);env["NODE_PATH"]=str(Path.home()/".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules")
                result=subprocess.run([shutil.which("node"),str(project/"web/workspace-editors.parity.service.test.cjs"),f"http://127.0.0.1:{port}"],cwd=project,env=env,capture_output=True,text=True,encoding="utf-8",timeout=100)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                report=json.loads(result.stdout.strip().splitlines()[-1]);self.assertEqual(report["status"],"passed");self.assertGreaterEqual(report["checks"],25)
                self.assertEqual(adapter.text_calls,0);self.assertEqual(adapter.audio_calls,1)
            finally:
                server.should_exit=True;thread.join(timeout=5);sock.close()


if __name__ == "__main__":
    unittest.main()
