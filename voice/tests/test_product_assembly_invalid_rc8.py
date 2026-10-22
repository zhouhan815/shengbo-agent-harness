"""Product-shape errors are correctable, safe and strictly pre-write.

Only temporary SQLite stores and in-process ASGI are used. No provider,
credentials, production data, Agent graph or audio is involved.
"""
from copy import deepcopy
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx

from app.prelive import PreliveService, BusinessError, create_router
from app.prelive.service import Product
from shengbo_contracts.common import MutationContext, Principal


class ProductAssemblyInvalidTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = PreliveService(self.temp.name)
        self.principal = Principal(kind="human")
        self.counter = 0
        app = FastAPI()
        app.include_router(create_router(self.service, lambda request: self.principal))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="http://assembly.invalid", trust_env=False)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.close()
        self.temp.cleanup()

    async def cmd(self, args, *, principal=None):
        self.counter += 1
        return await self.service.execute_command("save_product", args,
            MutationContext(operation_id=f"assembly-{self.counter}"), principal or self.principal)

    def args(self, fields, *, product=None, patches=None):
        result = {"schema_hash": self.service.schema()["schema_hash"],
            "fields": fields, "patches": patches or []}
        if product:
            result.update(product_id=product["product_ref"]["id"],
                expected_revision=product["product_ref"]["revision"])
        return result

    async def saved(self):
        fields = Product(name="离线商品", price=129,
            specifications=[{"name": "净含量", "value": "3.2克"}]).model_dump()
        return (await self.cmd(self.args(fields)))["data"]

    def current_fields(self, product):
        return {key: deepcopy(product["product"][key]) for key in Product.model_fields}

    def state(self):
        # Snapshot all persisted tables, including sqlite_sequence: a rejected
        # assembly must not consume product display numbers or event cursors.
        with self.service.store.transaction() as connection:
            names = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: sorted(tuple(row) for row in connection.execute(
                'SELECT * FROM "' + name.replace('"', '""') + '"')) for name in names}

    async def assert_invalid(self, args, *, stage, product_ref=None):
        before, sql = self.state(), []
        connect = self.service.store.connect

        def traced_connect():
            connection = connect()
            connection.set_trace_callback(sql.append)
            return connection

        with patch.object(self.service.store, "connect", side_effect=traced_connect):
            with self.assertRaises(BusinessError) as caught:
                await self.cmd(args)
        error = caught.exception
        self.assertEqual((error.code, error.status), ("PRODUCT_ASSEMBLY_INVALID", 422))
        details = error.payload()["details"]
        self.assertEqual(details["validation_stage"], stage)
        self.assertEqual(details["commit_state"], "not_committed")
        self.assertEqual(details["product_ref"], product_ref)
        self.assertEqual(details["schema_hash"], self.service.schema()["schema_hash"])
        self.assertEqual(set(details["required_fields"]), {"name", "price"})
        self.assertTrue(details["issues"])
        self.assertTrue(all(set(issue) == {"path", "type"} for issue in details["issues"]))
        self.assertFalse(error.payload()["retryable"])
        self.assertEqual(self.state(), before)
        # A rollback alone could hide attempted writes. SQL tracing additionally
        # proves these validation paths reach no DML before raising.
        self.assertFalse([statement.split(None, 1)[0] for statement in sql
            if statement.split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}])
        return details, error.payload()

    async def test_empty_new_fields_is_assembled_error_before_any_write(self):
        details, _ = await self.assert_invalid(self.args({}), stage="assembled")
        self.assertEqual({tuple(issue["path"]) for issue in details["issues"]},
            {("name",), ("price",)})
        self.assertTrue(all(issue["type"] == "missing" for issue in details["issues"]))

    async def test_new_legal_specification_patches_with_empty_fields_is_supplied_error(self):
        # Exact RC7 failure category: specification list was legitimate; the
        # required full fields assembly, not the patch schema, was missing.
        await self.assert_invalid(self.args({}, patches=[
            {"path": "/name", "op": "set", "value": "新商品"},
            {"path": "/price", "op": "set", "value": 129},
            {"path": "/specifications", "op": "set", "value": [{"name": "净含量", "value": "3.2克"}]}]),
            stage="supplied")

    async def test_model_validator_root_issue_does_not_copy_error_context(self):
        details, payload = await self.assert_invalid(self.args({"name": "离线商品", "price": 129,
            "core_selling_points": [""]}), stage="assembled")
        self.assertEqual(details["issues"], [{"path": [], "type": "value_error"}])
        self.assertNotIn("ctx", json.dumps(payload))
        self.assertNotIn("每条核心卖点", json.dumps(payload, ensure_ascii=False))

    async def test_bad_new_fields_do_not_echo_rejected_input(self):
        marker = "PRIVATE_REJECTED_PRICE_NEVER_ECHO"
        details, payload = await self.assert_invalid(
            self.args({"name": "离线商品", "price": marker}), stage="assembled")
        self.assertEqual(details["issues"], [{"path": ["price"], "type": "float_parsing"}])
        self.assertNotIn(marker, json.dumps(payload))
        self.assertNotIn("input", json.dumps(payload))

    async def test_extra_field_name_is_not_echoed_as_a_diagnostic_path(self):
        marker = "PRIVATE_UNKNOWN_FIELD_KEY_NEVER_ECHO"
        details, payload = await self.assert_invalid(
            self.args({"name": "离线商品", "price": 129, marker: "not-public"}), stage="assembled")
        self.assertEqual(details["issues"], [{"path": ["unknown_field"], "type": "extra_forbidden"}])
        self.assertNotIn(marker, json.dumps(payload))
        self.assertNotIn("not-public", json.dumps(payload))

    async def test_update_empty_complete_fields_is_supplied_error_with_exact_current_ref(self):
        product = await self.saved()
        await self.assert_invalid(self.args({}, product=product,
            patches=[{"path": "/name", "op": "set", "value": "改名"}]),
            stage="supplied", product_ref=product["product_ref"])

    async def test_update_bad_nested_complete_fields_only_exposes_path_and_type(self):
        product = await self.saved()
        fields = self.current_fields(product)
        marker = "PRIVATE_REJECTED_SPEC_NEVER_ECHO"
        fields["specifications"] = [{"name": "净含量", "value": {"secret": marker}}]
        details, payload = await self.assert_invalid(self.args(fields, product=product),
            stage="supplied", product_ref=product["product_ref"])
        self.assertIn({"path": ["specifications", 0, "value"], "type": "string_type"}, details["issues"])
        self.assertNotIn(marker, json.dumps(payload))
        self.assertNotIn("secret", json.dumps(payload))

    async def test_invalid_patch_assembled_stage_preserves_existing_revision(self):
        product = await self.saved()
        fields = self.current_fields(product)
        await self.assert_invalid(self.args(fields, product=product,
            patches=[{"path": "/name", "op": "set", "value": ""}]),
            stage="assembled", product_ref=product["product_ref"])

    async def test_legal_specifications_full_create_and_patch_succeed(self):
        product = await self.saved()
        self.assertEqual(product["product"]["specifications"], [{"name": "净含量", "value": "3.2克"}])
        fields = self.current_fields(product)
        fields["name"] = "离线商品新版"
        fields["specifications"] = [{"name": "净含量", "value": "3.5克"}]
        args = self.args(fields, product=product, patches=[
            {"path": "/name", "op": "set", "value": fields["name"]},
            {"path": "/specifications", "op": "set", "value": fields["specifications"]}])
        changed = (await self.cmd(args))["data"]
        self.assertEqual(changed["product_ref"]["revision"], product["product_ref"]["revision"] + 1)
        self.assertEqual(changed["product"]["specifications"], fields["specifications"])
        self.assertEqual(set(changed["changed_paths"]), {"/name", "/specifications"})

    async def test_http_422_uses_product_error_and_safe_details(self):
        before = self.state()
        response = await self.client.post("/api/prelive/v2/commands", json={
            "command": "save_product", "args": self.args({}),
            "mutation": {"operation_id": "http-empty-assembly"}})
        self.assertEqual(response.status_code, 422, response.text)
        body = response.json()
        self.assertEqual(body["error"]["code"], "PRODUCT_ASSEMBLY_INVALID")
        self.assertEqual(body["error"]["details"]["validation_stage"], "assembled")
        self.assertEqual(self.state(), before)

    async def test_permissions_schema_and_cas_errors_are_not_reclassified(self):
        product = await self.saved()
        cases = [
            (self.args({}), Principal(kind="agent", actor_id="no-grant", run_id="run-other"), "GRANT_REQUIRED", 403),
            ({**self.args({}), "schema_hash": "stale-schema"}, self.principal, "SCHEMA_CHANGED", 409),
            ({**self.args({}, product=product), "expected_revision": 0}, self.principal, "REVISION_CONFLICT", 409),
        ]
        for args, principal, code, status in cases:
            with self.subTest(code=code):
                before = self.state()
                with self.assertRaises(BusinessError) as caught:
                    await self.cmd(args, principal=principal)
                self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
                self.assertEqual(self.state(), before)

    async def test_invalid_save_preserves_unrelated_unknown_job_record(self):
        with self.service.store.transaction(write=True) as connection:
            self.service.store.put(connection, "prelive_job", "unknown-job", {
                "kind": "scripts", "run_id": "unrelated-run", "status": "blocked",
                "items": [{"item_id": "item-x", "state": "outcome_unknown",
                    "provider_attempts": [{"attempt_id": "attempt-x", "state": "sent"}]}]}, create=True)
        # This tests non-interference with the persisted unknown record, not
        # provider reconciliation or an end-to-end unknown recovery claim.
        await self.assert_invalid(self.args({}), stage="assembled")


if __name__ == "__main__":
    unittest.main()
