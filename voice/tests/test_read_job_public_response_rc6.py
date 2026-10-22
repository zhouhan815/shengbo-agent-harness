"""Authorized query-only access to public, received module responses."""
from copy import deepcopy
import json
import tempfile
import unittest

from fastapi import FastAPI
import httpx

from app.prelive import PreliveService, create_router
from app.prelive.service import Product
from shengbo_contracts.common import Principal, WorkScope


PRIVATE = "private-envelope-or-validation-product-must-not-escape"
CONTENT = json.dumps({"module_key": "cta", "sentences": [{
    "display_text": "有需要再按规则领券下单，按需购买。",
    "spoken_text": "有需要再按规则领券下单，按需购买。",
    "kind": "product_claim", "fact_ids": ["PROMOTION"],
}]}, ensure_ascii=False)


class ReadJobPublicResponseRC6Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="read-job-public-rc6-")
        self.service = PreliveService(self.temp.name)
        self.principal = Principal(kind="agent", actor_id="agent-a", run_id="run-a")
        self.scope = WorkScope(kind="preparation", preparation_id="prep-a")
        app = FastAPI()
        app.include_router(create_router(self.service, lambda request: (self.principal, self.scope)))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                       base_url="http://test.local", trust_env=False)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.close()
        self.temp.cleanup()

    def seed(self, *, state="response_received", response=True, archive_patch=None,
             receipt_patch=None, reference_patch=None, archive_kind="provider_response"):
        receipt = {"content": CONTENT, "module_key": "cta", "usage": {
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "private": PRIVATE, "prompt_tokens_details": {"cached_tokens": 10, "private": PRIVATE}},
            "usage_state": "known", "validation_product": {"private": PRIVATE},
            "reasoning_content": PRIVATE, "raw_envelope": {"secret": PRIVATE},
            **(receipt_patch or {})}
        body = {"job_id": "job-a", "item_id": "item-a", "attempt_id": "attempt-a",
                "received_at": "2026-10-17T15:00:00Z", "response": receipt,
                **(archive_patch or {})}
        with self.service.store.transaction(write=True) as c:
            ref = self.service.store.artifact(c, archive_kind, body)
            attempt = {"attempt_id": "attempt-a", "state": state,
                "purpose": "copywriting.cta", "input": {"module_key": "cta"},
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                "usage_state": "known", "validation_state": "rejected",
                "validation_error": {"code": "GENERATED_MODULE_NEEDS_REVIEW",
                                     "details": {"issues": ["优惠条件不完整"]}}}
            if response:
                attempt["provider_response_ref"] = {**ref, **(reference_patch or {})}
            job = {"id": "job-a", "preparation_id": "prep-a", "run_id": "run-a",
                "kind": "scripts", "status": "failed", "principal": {"private": PRIVATE},
                "mutation": {"private": PRIVATE}, "scope": self.scope.model_dump(),
                "items": [{"item_id": "item-a", "status": "failed",
                           "call_state": "result_received", "provider_attempts": [attempt]}]}
            prior = c.execute("SELECT revision FROM records WHERE kind='prelive_job' AND id='job-a'").fetchone()
            job_ref = self.service.store.put(c, "prelive_job", "job-a", job,
                expected=prior["revision"] if prior else None, create=prior is None)
        return ref, job_ref

    async def read(self):
        return await self.client.post("/api/prelive/v2/queries",
                                      json={"query": "read_job", "args": {"job_id": "job-a"}})

    def snapshot(self):
        with self.service.store.transaction() as c:
            return {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("records", "record_revisions", "prelive_events", "prelive_operations")}

    async def test_rejected_public_content_and_usage_are_available_over_http_without_writes(self):
        receipt_ref, job_ref = self.seed()
        before = self.snapshot()
        response = await self.read()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["ref"], job_ref)
        attempt = result["body"]["items"][0]["provider_attempts"][0]
        public = attempt["public_response"]
        self.assertEqual(public["content"], CONTENT)
        self.assertEqual(public["module_key"], "cta")
        self.assertEqual(public["expected_module_key"], "cta")
        self.assertEqual(public["receipt_ref"], receipt_ref)
        self.assertEqual(public["validation_state"], "rejected")
        self.assertEqual(public["usage"], {"prompt_tokens": 100, "completion_tokens": 20,
                         "total_tokens": 120, "prompt_tokens_details": {"cached_tokens": 10}})
        self.assertEqual(attempt["usage"]["total_tokens"], 120)
        self.assertFalse(public["is_formal_module"])
        self.assertFalse(public["reusable"])
        self.assertFalse(public["approvable"])
        self.assertTrue(result["body"]["public_response_contract"]["projection_only"])
        self.assertNotIn(PRIVATE, response.text)
        self.assertNotIn("validation_product", response.text)
        self.assertNotIn("raw_envelope", response.text)
        self.assertEqual(self.snapshot(), before)
        again = await self.read()
        self.assertEqual(again.json(), result)
        self.assertEqual(self.snapshot(), before)

    async def test_other_run_or_preparation_cannot_read_archive_content(self):
        self.seed()
        self.principal = Principal(kind="agent", actor_id="agent-b", run_id="run-b")
        response = await self.read()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "JOB_RUN_MISMATCH")
        self.assertNotIn(CONTENT, response.text)
        self.principal = Principal(kind="agent", actor_id="agent-a", run_id="run-a")
        self.scope = WorkScope(kind="preparation", preparation_id="prep-other")
        response = await self.read()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "SCOPE_CONFLICT")
        self.assertNotIn("public_response", response.text)

    async def test_wrong_archive_associations_fail_closed_without_writes(self):
        for patch in ({"job_id": "foreign-job"}, {"item_id": "foreign-item"},
                      {"attempt_id": "foreign-attempt"}):
            with self.subTest(patch=patch):
                self.seed(archive_patch=patch)
                before = self.snapshot()
                response = await self.read()
                self.assertEqual(response.status_code, 200)
                attempt = response.json()["body"]["items"][0]["provider_attempts"][0]
                self.assertNotIn("public_response", attempt)
                self.assertEqual(attempt["public_response_error"]["code"], "PROVIDER_RESPONSE_LINK_MISMATCH")
                self.assertNotIn("display_text", response.text)
                self.assertEqual(self.snapshot(), before)

    async def test_wrong_reference_hash_revision_or_kind_never_exposes_content(self):
        for kwargs, code in (({"reference_patch": {"content_hash": "sha256:" + "0" * 64}}, "PROVIDER_RESPONSE_REFERENCE_INVALID"),
                             ({"reference_patch": {"revision": 999}}, "PROVIDER_RESPONSE_REFERENCE_INVALID"),
                             ({"archive_kind": "context"}, "PROVIDER_RESPONSE_LINK_MISMATCH")):
            with self.subTest(kwargs=kwargs):
                self.seed(**kwargs)
                before = self.snapshot()
                response = await self.read()
                self.assertEqual(response.status_code, 200)
                attempt = response.json()["body"]["items"][0]["provider_attempts"][0]
                self.assertNotIn("public_response", attempt)
                self.assertEqual(attempt["public_response_error"]["code"], code)
                self.assertNotIn("display_text", response.text)
                self.assertEqual(self.snapshot(), before)

    async def test_unknown_or_not_received_attempt_does_not_claim_response_received(self):
        for kwargs in ({"state": "sent", "response": False},
                       {"state": "timeout_unknown", "response": False},
                       {"state": "outcome_unknown"}, {"state": "response_received", "response": False}):
            with self.subTest(kwargs=kwargs):
                self.seed(**kwargs)
                before = self.snapshot()
                response = await self.read()
                self.assertEqual(response.status_code, 200)
                attempt = response.json()["body"]["items"][0]["provider_attempts"][0]
                self.assertEqual(attempt["state"], kwargs["state"])
                self.assertNotIn("public_response", attempt)
                self.assertNotIn("display_text", response.text)
                self.assertEqual(self.snapshot(), before)

    async def test_long_content_is_complete_and_partial_usage_does_not_gain_missing_fields(self):
        content = "公开内容" * 2500
        self.seed(state="result_received", receipt_patch={"content": content,
                  "usage": {"prompt_tokens": 44, "private": PRIVATE}, "usage_state": "partial"})
        response = await self.read()
        self.assertEqual(response.status_code, 200)
        public = response.json()["body"]["items"][0]["provider_attempts"][0]["public_response"]
        self.assertEqual(public["content"], content)
        self.assertEqual(public["usage"], {"prompt_tokens": 44})
        self.assertEqual(public["usage_state"], "partial")
        self.assertNotIn("completion_tokens", public["usage"])
        self.assertNotIn(PRIVATE, response.text)

    async def test_incomplete_dispositions_report_only_paths_without_committing_then_preserve_custom_semantics(self):
        self.principal, self.scope = Principal(kind="human"), None
        fields = Product(name="不应在错误中回显的商品名", price=99).model_dump()
        custom = [{"path": "/name", "action": "set", "knowledge": "known"},
                  {"path": "/material", "knowledge": "not_applicable", "reason": "本品无此项"}]
        command = {"command": "save_product", "args": {
            "schema_hash": self.service.schema()["schema_hash"], "fields": fields,
            "patches": [], "field_dispositions": custom},
            "mutation": {"operation_id": "incomplete-field-dispositions"}}
        before = self.snapshot()
        rejected = await self.client.post("/api/prelive/v2/commands", json=command)
        self.assertEqual(rejected.status_code, 409)
        error = rejected.json()["error"]
        self.assertEqual(error["code"], "INCOMPLETE_FIELD_DISPOSITIONS")
        details = error["details"]
        required = {"/" + key for key in Product.model_fields}
        self.assertEqual(set(details["required_paths"]), required)
        self.assertEqual(set(details["missing_paths"]), required - {"/name", "/material"})
        self.assertEqual(details["commit_state"], "not_committed")
        self.assertNotIn(fields["name"], rejected.text)
        self.assertEqual(self.snapshot(), before)
        command["args"]["field_dispositions"] = custom + [
            {"path": path, "knowledge": "unknown"} for path in details["missing_paths"]]
        saved = await self.client.post("/api/prelive/v2/commands", json=command)
        self.assertEqual(saved.status_code, 200, saved.text)
        with self.service.store.transaction() as c:
            audit = self.service.store.exact(c, saved.json()["data"]["assembly_ref"])["body"]
        material = next(d for d in audit["field_dispositions"] if d["path"] == "/material")
        self.assertEqual(material["knowledge"], "not_applicable")
        self.assertEqual(material["reason"], "本品无此项")
