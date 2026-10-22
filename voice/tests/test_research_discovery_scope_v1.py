"""Research discovery exposes only bodies readable in the same work scope."""
import tempfile
import unittest

from app.prelive.service import PreliveService, Product
from app.prelive.storage import BusinessError, digest
from shengbo_contracts.business import AnalysisReport
from shengbo_contracts.common import MutationContext, Principal, WorkScope


class ResearchDiscoveryScopeV1Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="research-discovery-scope-")
        self.service = PreliveService(self.temp.name)
        self.sequence = 0
        self.human = Principal(kind="human")
        self.agent = Principal(kind="agent", run_id="isolated-read-run")
        fields = Product(name="相同商品", price=23.58).model_dump(mode="json")
        self.product = (await self.cmd("save_product", {"fields": fields,
            "schema_hash": self.service.schema()["schema_hash"], "patches": []}))["product_ref"]
        room = (await self.cmd("create_room", {"name": "研究隔离直播间"}))["room_ref"]
        self.preparations = []
        for _ in range(2):
            result = await self.cmd("create_preparation", {"room_id": room["id"], "room_revision": room["revision"]})
            self.preparations.append(result["preparation_ref"])
        self.catalog = WorkScope(kind="catalog")
        self.scope_a = WorkScope(kind="preparation", preparation_id=self.preparations[0]["id"])
        self.scope_b = WorkScope(kind="preparation", preparation_id=self.preparations[1]["id"])

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def cmd(self, key, args):
        self.sequence += 1
        result = await self.service.execute_command(key, args,
            MutationContext(operation_id="research-scope-" + str(self.sequence)), self.human)
        return result["data"]

    async def accept(self, label, preparation_id=None):
        report = AnalysisReport(summary=label, product_ids=[self.product["id"]],
            dependencies=[self.product], fields={"scenes": [label]}).model_dump(mode="json")
        args = {"role": "audience_scenes", "report": report, "report_hash": digest(report),
            "product_refs": [self.product], "readset": [self.product]}
        if preparation_id:
            prep = await self.service.query("read_preparation", {"preparation_id": preparation_id})
            args.update(preparation_id=preparation_id, expected_revision=prep["ref"]["revision"])
        return (await self.cmd("accept_analysis_report", args))["analysis_ref"]

    async def find(self, scope, *, cursor=0, limit=20):
        return await self.service.query("find_audience_research", {
            "ref": self.product, "product_id": self.product["id"], "cursor": cursor, "limit": limit},
            principal=self.agent, scope=scope)

    async def test_catalog_scope_excludes_same_product_other_preparation_bodies(self):
        catalog = await self.accept("商品库正式顾客研究")
        await self.accept("A场次私有研究", self.scope_a.preparation_id)
        await self.accept("B场次私有研究", self.scope_b.preparation_id)
        result = await self.find(self.catalog)
        self.assertEqual([item["research_ref"] for item in result["items"]], [catalog])
        self.assertNotIn("私有研究", str(result))
        self.assertIsNone(result["next_cursor"])

    async def test_preparation_scope_includes_its_report_and_catalog_but_not_foreign(self):
        catalog = await self.accept("商品库研究")
        own = await self.accept("A本场研究", self.scope_a.preparation_id)
        foreign = await self.accept("B本场研究", self.scope_b.preparation_id)
        result = await self.find(self.scope_a)
        self.assertEqual([item["research_ref"] for item in result["items"]], [own, catalog])
        self.assertTrue(all(item["compatibility"] == "exact" for item in result["items"]))
        self.assertNotIn(foreign["id"], str(result))

    async def test_scope_filtering_precedes_cursor_and_limit(self):
        catalog_old = await self.accept("旧商品库研究")
        await self.accept("不能占用分页A", self.scope_b.preparation_id)
        own_old = await self.accept("旧本场研究", self.scope_a.preparation_id)
        await self.accept("不能占用分页B", self.scope_b.preparation_id)
        catalog_new = await self.accept("新商品库研究")
        own_new = await self.accept("新本场研究", self.scope_a.preparation_id)
        await self.accept("最新其他场次也不能造成空页", self.scope_b.preparation_id)
        expected = [own_new, catalog_new, own_old, catalog_old]
        for cursor, reference in enumerate(expected):
            result = await self.find(self.scope_a, cursor=cursor, limit=1)
            self.assertEqual([item["research_ref"] for item in result["items"]], [reference])
            self.assertEqual(result["next_cursor"], cursor + 1 if cursor < 3 else None)
        for cursor, reference in enumerate([catalog_new, catalog_old]):
            result = await self.find(self.catalog, cursor=cursor, limit=1)
            self.assertEqual([item["research_ref"] for item in result["items"]], [reference])
            self.assertEqual(result["next_cursor"], 1 if cursor == 0 else None)

    async def test_unscoped_human_query_preserves_original_history_visibility(self):
        catalog = await self.accept("商品库")
        own = await self.accept("本场A", self.scope_a.preparation_id)
        foreign = await self.accept("本场B", self.scope_b.preparation_id)
        result = await self.service.query("find_audience_research", {"ref": self.product}, principal=self.human)
        self.assertEqual([item["research_ref"] for item in result["items"]], [foreign, own, catalog])

    async def test_exact_reader_foreign_preparation_fence_remains_unchanged(self):
        foreign = await self.accept("另一场研究", self.scope_b.preparation_id)
        for scope in [self.scope_a, self.catalog]:
            with self.assertRaises(BusinessError) as rejected:
                await self.service.query("read_analysis_report", {"ref": foreign}, principal=self.agent, scope=scope)
            self.assertEqual(rejected.exception.code, "ARTIFACT_SCOPE_CONFLICT")
            self.assertEqual(rejected.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
