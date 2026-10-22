"""Shared proxy transport ownership, without credentials or a live provider."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx

from app.prelive_bridge import install


class WaitingBody(httpx.AsyncByteStream):
    def __init__(self, owner):
        self.owner = owner

    async def __aiter__(self):
        await self.owner.release.wait()
        if self.owner.closed:
            raise httpx.ReadError("shared transport was closed during a response")
        yield b'{"events":[{"type":"tool.started"}],"next_cursor":1}'


class OwnedTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.close_calls = 0

    async def handle_async_request(self, request):
        if request.url.path.endswith("/runs/run-test/events"):
            self.started.set()
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=WaitingBody(self))
        if request.url.path.endswith("/runs/run-test"):
            await self.started.wait()
            return httpx.Response(200, json={"run_id": "run-test", "status": "RUNNING"})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b': ready\n\nevent: tool.started\ndata: {}\n\n')
        return httpx.Response(404, json={"error": "fixture path"})

    async def aclose(self):
        self.close_calls += 1
        self.closed = True
        self.release.set()


class ProxyTransportOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prelive-proxy-test-")
        self.app = FastAPI()
        server = SimpleNamespace(RUNTIME=Path(self.temp.name), POOL=None)
        with patch("app.prelive_bridge.service_tokens", return_value={
            "voice_to_agent": "fixture-voice-to-agent", "agent_to_voice": "fixture-agent-to-voice",
            "controller_to_voice": "fixture-controller-to-voice",
        }):
            self.service = install(self.app, server)
        self.transport = OwnedTransport()
        self.app.state.prelive_proxy_transport = self.transport
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:18865")
        await self.client.get("/api/assistant/v2/session")

    async def asyncTearDown(self):
        self.transport.release.set()
        await self.client.aclose()
        await self.service.close()
        self.temp.cleanup()

    async def test_fast_run_poll_does_not_close_concurrent_event_response(self):
        slow = asyncio.create_task(self.client.get("/api/assistant/v2/runs/run-test/events?after=0"))
        try:
            fast = await self.client.get("/api/assistant/v2/runs/run-test")
            self.assertEqual(fast.status_code, 200)
            self.assertEqual(self.transport.close_calls, 0)
        finally:
            self.transport.release.set()
        events = await slow
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events.json()["events"][0]["type"], "tool.started")
        self.assertEqual(self.transport.close_calls, 0)

    async def test_stream_subscriber_closing_does_not_close_app_transport(self):
        async with self.client.stream("GET", "/api/assistant/v2/events") as response:
            self.assertEqual(response.status_code, 200)
            body = await response.aread()
            self.assertIn(b"tool.started", body)
        self.assertEqual(self.transport.close_calls, 0)
        self.assertFalse(self.transport.closed)

    async def test_app_shutdown_closes_shared_transport_once(self):
        async with self.app.router.lifespan_context(self.app):
            self.assertEqual(self.transport.close_calls, 0)
        self.assertEqual(self.transport.close_calls, 1)
        self.assertTrue(self.transport.closed)


if __name__ == "__main__":
    unittest.main()
