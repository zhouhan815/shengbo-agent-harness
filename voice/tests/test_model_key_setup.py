"""First-time key setup reads locally and saves only after explicit verification."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
import httpx

from app import model_key_setup
from app.prelive.storage import BusinessError
from shengbo_prelive.providers.deepseek_key import read_api_key


class ModelKeySetupTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / ".env"
        self.enterContext(patch.dict(os.environ, {"SHENGBO_DEEPSEEK_ENV_FILE": str(self.path)}))
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self.verifier = model_key_setup._verify_key
        self.verify = self.enterContext(patch.object(model_key_setup, "_verify_key", new_callable=AsyncMock))
        self.key = "sk-" + uuid4().hex
        app = FastAPI()

        def authenticate(request, *, write=False):
            if request.headers.get("x-test-session") != "1":
                raise BusinessError("LOCAL_SESSION_REQUIRED", status=401)
            if write and request.headers.get("x-csrf-token") != "test":
                raise BusinessError("CSRF_REJECTED", status=403)

        model_key_setup.install_model_key_setup(app, SimpleNamespace(ROOT=self.path.parent), authenticate)
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"x-test-session": "1"}))

    async def get_status(self):
        response = await self.client.get("/api/assistant/v2/model-key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()["status"]

    async def connect(self, value, **kwargs):
        return await self.client.post("/api/assistant/v2/model-key/connect", json={"api_key": value},
                                      headers={"x-csrf-token": "test", **kwargs})

    async def test_startup_only_reads_local_and_identifies_key_shaped_sample(self):
        self.assertEqual(await self.get_status(), "missing")
        self.path.write_text("DEEPSEEK_API_KEY=sk-" "your-deepseek-api-key-here\n", encoding="utf-8")
        self.assertEqual(await self.get_status(), "placeholder")
        self.path.write_text(f"DEEPSEEK_API_KEY={self.key}\n", encoding="utf-8")
        self.assertEqual(await self.get_status(), "configured")
        self.verify.assert_not_awaited()

    async def test_failed_connection_preserves_existing_file_and_does_not_echo_key(self):
        old = b"# keep\r\nDEEPSEEK_API_KEY=\r\nDEEPSEEK_MODEL=deepseek-flash\r\n"
        self.path.write_bytes(old)
        self.verify.return_value = False
        response = await self.connect(self.key)
        self.assertEqual(response.json()["error"]["code"], "MODEL_KEY_CONNECTION_FAILED")
        self.assertNotIn(self.key, response.text)
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(await self.get_status(), "missing")
        self.verify.assert_awaited_once_with(self.key)

    async def test_verified_key_is_visible_to_the_agent_and_other_env_entries_are_preserved(self):
        old = b"# preserve\r\nDEEPSEEK_API_KEY=\r\nDEEPSEEK_MODEL=deepseek-flash\r\n"
        self.path.write_bytes(old)
        os.environ["DEEPSEEK_API_KEY"] = "sk-" "your-deepseek-api-key-here"
        self.verify.return_value = True
        response = await self.connect(self.key)
        self.assertEqual(response.json(), {"status": "configured"})
        self.assertNotIn(self.key, response.text)
        self.assertEqual(self.path.read_bytes(), old.replace(b"DEEPSEEK_API_KEY=\r\n", f"DEEPSEEK_API_KEY={self.key}\r\n".encode()))
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], self.key)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-" "your-deepseek-api-key-here"}):
            self.assertEqual(read_api_key(), self.key)
        self.assertEqual(await self.get_status(), "configured")

    async def test_csrf_and_sample_inputs_do_not_start_verification(self):
        response = await self.connect(self.key, **{"x-csrf-token": "wrong"})
        self.assertEqual(response.status_code, 403)
        response = await self.connect("sk-" "your-deepseek-api-key-here")
        self.assertEqual(response.json()["error"]["code"], "MODEL_KEY_INPUT_INVALID")
        self.verify.assert_not_awaited()
        self.assertFalse(self.path.exists())

    async def test_provider_verification_is_a_read_only_authenticated_request(self):
        key = "sk-" + uuid4().hex

        def reply(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(str(request.url), "https://api.deepseek.com/user/balance")
            self.assertEqual(request.headers.get("authorization"), f"Bearer {key}")
            return httpx.Response(200, json={"is_available": False})

        client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        with patch.object(model_key_setup.httpx, "AsyncClient", return_value=client):
            self.assertTrue(await self.verifier(key))

    async def test_existing_key_is_not_replaced_or_reverified(self):
        self.path.write_text(f"DEEPSEEK_API_KEY={self.key}\n", encoding="utf-8")
        reply = await self.connect("sk-" + uuid4().hex)
        self.assertEqual(reply.json(), {"status": "configured", "already_configured": True})
        self.verify.assert_not_awaited()
        self.assertEqual(read_api_key(), self.key)

    async def test_save_failure_keeps_original_and_removes_temporary_file(self):
        original = b"# keep\nDEEPSEEK_API_KEY=\n"
        self.path.write_bytes(original)
        self.verify.return_value = True
        with patch.object(model_key_setup.os, "replace", side_effect=OSError("offline save failure")):
            reply = await self.connect(self.key)
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(reply.json()["error"]["code"], "MODEL_KEY_SAVE_FAILED")
        self.assertNotIn(self.key, reply.text)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
        self.assertEqual(list(Path(self.directory).glob(".deepseek-key-*.tmp")), [])

    async def test_session_limit_and_removed_management_routes(self):
        denied = await self.client.get("/api/assistant/v2/model-key", headers={"x-test-session": "0"})
        self.assertEqual(denied.status_code, 401)
        oversized = await self.connect(self.key * 100)
        self.assertEqual(oversized.status_code, 422)
        self.assertNotIn(self.key, oversized.text)
        self.verify.assert_not_awaited()
        self.assertEqual((await self.client.get("/api/assistant/v2/model-config")).status_code, 404)
        self.assertEqual((await self.client.post("/api/assistant/v2/model-config/save", json={})).status_code, 404)

    def test_append_preserves_existing_bom_and_newline_style(self):
        self.path.write_bytes(b"\xef\xbb\xbf# preserve\r\nDEEPSEEK_MODEL=deepseek-flash")
        model_key_setup._save_verified_key(self.path, self.key)
        self.assertEqual(self.path.read_bytes(), b"\xef\xbb\xbf# preserve\r\nDEEPSEEK_MODEL=deepseek-flash\r\n" + f"DEEPSEEK_API_KEY={self.key}\r\n".encode())
