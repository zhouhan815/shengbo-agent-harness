"""Wire integration against a fresh ASGI app and isolated SQLite database."""
import tempfile
import unittest
try:
    import httpx
except ImportError:
    httpx = None
from fastapi import FastAPI
from shengbo_contracts.common import Principal
from app.prelive import PreliveService, create_router


@unittest.skipIf(httpx is None, "HTTP integration tests require the agent environment's httpx")
class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = PreliveService(self.temp.name)
        self.principal = Principal(kind="human")
        self.app = FastAPI()
        self.app.include_router(create_router(self.service, lambda request: self.principal))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test.local", trust_env=False)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.close()
        self.temp.cleanup()

    async def test_query_and_mutation_contracts_and_idempotency(self):
        capabilities = await self.client.post("/api/prelive/v2/queries", json={"query": "read_capabilities", "args": {}})
        self.assertEqual(capabilities.status_code, 200)
        self.assertEqual(len(capabilities.json()["commands"]), 31)
        body = {"command": "create_room", "args": {"name": "路由测试"}, "mutation": {"operation_id": "room-once"}}
        first = await self.client.post("/api/prelive/v2/commands", json=body)
        second = await self.client.post("/api/prelive/v2/commands", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["data"]["room_ref"]["kind"], "config")

    async def test_no_actor_in_body_and_unknown_keys_not_echoed(self):
        body = {"command": "create_room", "args": {"name": "x"}, "mutation": {"operation_id": "bad"}, "principal": {"kind": "human", "token": "not-a-secret-test-marker"}}
        response = await self.client.post("/api/prelive/v2/commands", json=body)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("not-a-secret-test-marker", response.text)
        query = await self.client.post("/api/prelive/v2/queries", json={"query": "read_capabilities", "args": {"unexpected": 123}})
        self.assertEqual(query.status_code, 422)

    async def test_agent_cannot_reach_human_only_actions(self):
        self.principal = Principal(kind="agent", actor_id="agent", run_id="test-run")
        response = await self.client.post("/api/prelive/v2/commands", json={"command": "approve_text_for_audio", "args": {"preparation_id": "missing", "review_hash": "hash"}, "mutation": {"operation_id": "cannot-approve"}})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "HUMAN_REQUIRED")


if __name__ == "__main__":
    unittest.main()
