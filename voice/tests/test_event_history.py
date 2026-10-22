"""Latest-first business history pagination preserves scoped event consumers."""
import tempfile
import unittest

from app.prelive.service import PreliveService
from app.prelive.storage import BusinessError
from shengbo_contracts.common import WorkScope


class EventHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = PreliveService(self.temp.name)
        with self.service.store.transaction(write=True) as c:
            for i in range(450):
                self.service.store.event(c, ("prep1", "prep2", "catalog")[i % 3], "fixture", {"number": i})

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def test_latest_page_after_200_events_and_complete_pagination(self):
        query = {"scope_id": "prep1", "include_catalog": True, "order": "desc", "limit": 47}
        values, cursor = [], 0
        while True:
            page = await self.service.query("read_events", {**query, "cursor": cursor})
            self.assertTrue(all(item["stream_id"] in {"prep1", "catalog"} for item in page["items"]))
            values.extend(item["seq"] for item in page["items"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        self.assertEqual(values[0], 450)
        self.assertEqual(len(values), 300)
        self.assertEqual(values, sorted(set(values), reverse=True))

    async def test_old_ascending_cursor_contract_and_security_scope(self):
        first = await self.service.query("read_events", {"limit": 200})
        self.assertEqual([v["seq"] for v in first["items"]], list(range(1, 201)))
        second = await self.service.query("read_events", {"cursor": first["next_cursor"], "limit": 200})
        self.assertEqual(second["items"][0]["seq"], 201)
        scoped = await self.service.query("read_events", {"order": "desc", "include_catalog": True}, scope=WorkScope(kind="preparation", preparation_id="prep1"))
        self.assertEqual({item["stream_id"] for item in scoped["items"]}, {"prep1"})
        with self.assertRaises(BusinessError) as caught:
            await self.service.query("read_events", {"scope_id": "prep2"}, scope=WorkScope(kind="preparation", preparation_id="prep1"))
        self.assertEqual(caught.exception.code, "SCOPE_CONFLICT")

    async def test_new_events_do_not_duplicate_older_pages(self):
        first = await self.service.query("read_events", {"order": "desc", "limit": 2})
        with self.service.store.transaction(write=True) as c:
            self.service.store.event(c, "catalog", "new", {})
        older = await self.service.query("read_events", {"order": "desc", "cursor": first["next_cursor"], "limit": 2})
        self.assertEqual([item["seq"] for item in first["items"]], [450, 449])
        self.assertEqual([item["seq"] for item in older["items"]], [448, 447])


if __name__ == "__main__":
    unittest.main()
