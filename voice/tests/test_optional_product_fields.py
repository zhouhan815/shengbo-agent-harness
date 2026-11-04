"""Inactive product facts stay inactive at save, old snapshot, and RAG boundaries."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx
from shengbo_contracts.common import Principal
from app import commerce_config as commerce, reply_lab, server
from app.prelive import PreliveService, create_router
from app.prelive.module_generation import build_request, fact_catalog
from app.prelive.service import Product


def product():
    return {"id": "inactive-test", "name": "测试商品", "price": 178, "currency": "CNY",
            "selling_points": "轻便", "material": "停用材质哨兵", "ingredients": "停用成分哨兵",
            "specifications": [{"name": "停用规格", "value": "停用规格哨兵"}],
            "target_audience": "停用人群哨兵", "storage_conditions": "停用储存哨兵",
            "shelf_life_value": 987, "shelf_life_unit": "year", "shipping_origin": "停用产地哨兵",
            "shipping_time": "停用物流哨兵", "return_policy": "停用售后哨兵",
            "usage_instructions": "停用用法哨兵", "faq": "停用问答哨兵", "knowledge": "停用知识哨兵",
            "knowledge_documents": [{"id": "old-doc", "title": "旧知识", "content": "停用文档哨兵"}],
            "hidden_optional_fields": list(commerce.OPTIONAL_PRODUCT_FIELDS)}


class ActiveFactTests(unittest.TestCase):
    def test_projection_clears_all_groups_without_changing_saved_snapshot(self):
        old = product()
        untouched = deepcopy(old)
        current = commerce.active_product_fields(old)
        self.assertEqual(old, untouched)
        self.assertEqual(current["name"], old["name"])
        self.assertEqual(current["price"], 178)
        self.assertIsNone(current["shelf_life_value"])
        self.assertEqual(current["shelf_life_unit"], "month")
        self.assertEqual(current["knowledge_documents"], [])
        self.assertNotIn("停用", json.dumps(current, ensure_ascii=False))
        self.assertNotIn("资料未提供", json.dumps(current, ensure_ascii=False))

    def test_old_snapshot_cannot_leak_through_prompt_facts_or_nested_context(self):
        old = product()
        context = {"records": [{"ref": {"kind": "product", "id": old["id"]}, "body": old}],
                   "generation_instruction": "讲清适用场景", "module_key": "selling_points"}
        body = build_request(old, context, "selling_points", [], model="deepseek-flash")
        task = json.loads(body["messages"][1]["content"])
        self.assertNotIn("停用", body["messages"][1]["content"])
        self.assertNotIn("KNOWLEDGE", {fact["id"] for fact in fact_catalog(old)})
        self.assertEqual(task["effective_product_snapshot"]["material"], "")
        self.assertEqual(context["records"][0]["body"]["material"], "停用材质哨兵")

    def test_rag_omits_old_disabled_facts_and_rebuild_removes_indexed_documents(self):
        old = product()
        enabled = {**old, "hidden_optional_fields": []}
        with tempfile.TemporaryDirectory() as directory, patch.object(server, "RUNTIME", Path(directory)):
            reply_lab.sync_index([enabled])
            with server.db() as connection:
                self.assertGreater(connection.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0], 10)
            reply_lab.sync_index([old])
            with server.db() as connection:
                values = [row[0] for row in connection.execute("SELECT body FROM rag_chunks")]
            self.assertNotIn("停用", " ".join(values))
            self.assertTrue(any("轻便" in value for value in values))
            self.assertEqual({d["kind"] for d in reply_lab.document_specs(old)}, {"identity", "price", "selling"})

    def test_room_shipping_override_is_explicitly_active_without_restoring_global_default(self):
        old = product()
        config = {"commerce": commerce.parse_room({"product_overrides": {
            old["id"]: {"shipping_override": True, "shipping_time": "本场48小时发货"}}})}
        effective = commerce.effective_product(old, config)
        self.assertEqual(effective["shipping_time"], "本场48小时发货")
        self.assertEqual(commerce.active_product_fields(effective)["shipping_time"], "本场48小时发货")
        self.assertEqual(old["shipping_time"], "停用物流哨兵")


class OptionalFieldApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = PreliveService(self.temp.name)
        app = FastAPI()
        app.include_router(create_router(self.service, lambda request: Principal(kind="human")))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False)
        self.operation = 0

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.close()
        self.temp.cleanup()

    async def save(self, fields, previous=None, patches=None):
        self.operation += 1
        args = {"schema_hash": self.service.schema()["schema_hash"], "patches": patches or [], "fields": fields}
        if previous:
            args.update(product_id=previous["id"], expected_revision=previous["revision"])
        response = await self.client.post("/api/prelive/v2/commands", json={"command": "save_product", "args": args,
            "mutation": {"operation_id": "optional-" + str(self.operation)}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    async def test_api_hide_clears_values_and_readd_does_not_resurrect_them(self):
        fields = Product(name="API停用测试", price=100, material="旧材质", shelf_life_value=12,
                         knowledge="旧说明", knowledge_documents=[{"id": "doc1", "title": "旧文档", "content": "旧内容"}]).model_dump()
        created = await self.save(fields)
        fields = {key: created["product"][key] for key in Product.model_fields}
        hidden = {**fields, "hidden_optional_fields": ["material", "shelf_life", "knowledge", "knowledge_documents"]}
        saved = await self.save(hidden, created["product_ref"], [{"path": "/hidden_optional_fields", "op": "set", "value": hidden["hidden_optional_fields"]}])
        self.assertEqual(saved["product"]["material"], "")
        self.assertIsNone(saved["product"]["shelf_life_value"])
        self.assertEqual(saved["product"]["knowledge_documents"], [])
        self.assertIn("/material", saved["changed_paths"])
        with self.service.store.transaction() as connection:
            original = self.service.store.exact(connection, created["product_ref"])
            audit = self.service.store.exact(connection, saved["assembly_ref"])
        self.assertEqual(original["body"]["material"], "旧材质")
        disposition = next(item for item in audit["body"]["field_dispositions"] if item["path"] == "/material")
        self.assertEqual(disposition["knowledge"], "not_applicable")
        self.assertEqual(disposition["action"], "clear")
        values = {key: saved["product"][key] for key in Product.model_fields}
        values["hidden_optional_fields"] = []
        restored = await self.save(values, saved["product_ref"], [{"path": "/hidden_optional_fields", "op": "set", "value": []}])
        self.assertEqual(restored["product"]["material"], "")
        values["material"] = "重新填写材质"
        active = await self.save(values, restored["product_ref"], [{"path": "/material", "op": "set", "value": values["material"]}])
        self.assertEqual(active["product"]["material"], "重新填写材质")

    async def test_saved_knowledge_destination_reports_only_changed_surviving_documents(self):
        original = [
            {"id": "same-title-a", "title": "使用说明", "content": "旧说明"},
            {"id": "same-title-b", "title": "使用说明", "content": "保持此说明"},
            {"id": "removed", "title": "旧补充", "content": "将移除的说明"},
        ]
        fields = Product(name="成果定位测试", price=100, knowledge_documents=original).model_dump()
        created = await self.save(fields)
        self.assertEqual(created["changed_knowledge_document_ids"], [d["id"] for d in original])

        fields = {key: created["product"][key] for key in Product.model_fields}
        updated = [
            original[1],
            {**original[0], "content": "已核对的新说明"},
            {"id": "new", "title": "使用说明", "content": "新增说明"},
        ]
        fields["knowledge_documents"] = updated
        saved = await self.save(fields, created["product_ref"],
                                [{"path": "/knowledge_documents", "op": "set", "value": updated}])
        self.assertEqual(saved["changed_knowledge_document_ids"], ["same-title-a", "new"])

        fields = {key: saved["product"][key] for key in Product.model_fields}
        fields["knowledge_documents"] = updated[:2]
        deleted = await self.save(fields, saved["product_ref"],
                                  [{"path": "/knowledge_documents", "op": "set", "value": updated[:2]}])
        self.assertEqual(deleted["changed_knowledge_document_ids"], [])
        self.assertIn("/knowledge_documents", deleted["changed_paths"])

    async def test_old_hidden_record_is_preserved_on_empty_patch_and_validation_is_only_a_view(self):
        old = Product(name="旧数据", price=100, material="旧材质").model_dump()
        old.update(id="old-record", sku_id="old-record", hidden_optional_fields=["material"])
        with self.service.store.transaction() as connection:
            ref = self.service.store.put(connection, "product", "old-record", old, create=True)
        values = {key: old[key] for key in Product.model_fields}
        response = await self.client.post("/api/prelive/v2/queries", json={"query": "validate_product_assembly", "args": {
            "schema_hash": self.service.schema()["schema_hash"], "fields": values}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["normalized_fields"]["material"], "")
        saved = await self.save(values, ref)
        # Validation/read projections cannot authorize a data migration.
        self.assertEqual(saved["product"]["material"], "旧材质")
        self.assertNotIn("/material", saved["changed_paths"])
        self.assertEqual(commerce.active_product_fields(saved["product"])["material"], "")
        with self.service.store.transaction() as connection:
            audit = self.service.store.exact(connection, saved["assembly_ref"])["body"]
            historic = self.service.store.exact(connection, ref)
        disposition = next(d for d in audit["field_dispositions"] if d["path"] == "/material")
        self.assertEqual(disposition["action"], "preserve")
        self.assertEqual(disposition["knowledge"], "not_applicable")
        self.assertEqual(historic["body"], old)

    async def test_name_patch_cannot_clear_old_hidden_values_or_bypass_cas(self):
        old = Product(name="旧商品", price=100, material="保留材质", knowledge="保留知识").model_dump()
        old.update(id="legacy-name", sku_id="legacy-name", hidden_optional_fields=["material", "knowledge"])
        with self.service.store.transaction() as connection:
            ref = self.service.store.put(connection, "product", "legacy-name", old, create=True)
        values = {key: old[key] for key in Product.model_fields}
        values["name"] = "只更新名称"
        patches = [{"path": "/name", "op": "set", "value": values["name"]}]
        saved = await self.save(values, ref, patches)
        self.assertEqual(saved["changed_paths"], ["/name"])
        self.assertEqual(saved["product"]["material"], "保留材质")
        self.assertEqual(saved["product"]["knowledge"], "保留知识")
        response = await self.client.post("/api/prelive/v2/commands", json={"command": "save_product", "args": {
            "schema_hash": self.service.schema()["schema_hash"], "product_id": ref["id"],
            "expected_revision": ref["revision"], "fields": values, "patches": patches},
            "mutation": {"operation_id": "stale-optional-cas"}})
        self.assertEqual(response.status_code, 409)
        self.assertIn("REVISION_CONFLICT", response.text)

    async def test_explicit_new_hidden_group_clears_only_that_group_not_old_hidden_storage(self):
        old = Product(name="分组停用", price=100, material="旧隐藏材质", knowledge="将明确停用的知识").model_dump()
        old.update(id="legacy-groups", sku_id="legacy-groups", hidden_optional_fields=["material"])
        with self.service.store.transaction() as connection:
            ref = self.service.store.put(connection, "product", "legacy-groups", old, create=True)
        values = {key: old[key] for key in Product.model_fields}
        values["hidden_optional_fields"] = ["material", "knowledge"]
        saved = await self.save(values, ref, [{"path": "/hidden_optional_fields", "op": "set",
            "value": values["hidden_optional_fields"]}])
        self.assertEqual(saved["product"]["material"], "旧隐藏材质")
        self.assertEqual(saved["product"]["knowledge"], "")
        self.assertEqual(set(saved["changed_paths"]), {"/hidden_optional_fields", "/knowledge"})
        with self.service.store.transaction() as connection:
            self.assertEqual(self.service.store.exact(connection, ref)["body"], old)
