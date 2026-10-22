"""Actual bridge snapshot transformation + offline archive-only recovery."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from app import commerce_config
from app.prelive import module_generation, whole_script_generation
from app.prelive.archive_validation_product import verified_archived_validation_product
from app.prelive.storage import BusinessError, digest
from app.prelive_bridge import ExistingVoiceAdapter
import test_archived_revalidation_r12 as archived_cases


class WholeBridgeSnapshotRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await archived_cases.ArchivedRevalidationTests.asyncSetUp(self)
        self.sent = []
        self.commerce_override = None

        def respond(request):
            self.sent.append(request)
            task = json.loads(json.loads(request.content)["messages"][1]["content"])
            content = ({"modules": [archived_cases.module(key) for key in task["requested_module_keys"]]}
                if "requested_module_keys" in task else archived_cases.module(task["requested_module_key"]))
            if "requested_module_keys" in task:
                for item in content["modules"]:
                    item["sentences"] = [{"text": row["spoken_text"], "kind": row["kind"], "fact_ids": row["fact_ids"]}
                                         for row in item["sentences"]]
            return httpx.Response(200, json={"id": "testonly-bridge-archive", "choices": [{
                "finish_reason": "stop", "message": {"content": json.dumps(content, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})

        self.server = SimpleNamespace(commerce=commerce_config,
            os=SimpleNamespace(environ={"DEEPSEEK_API_KEY": "testonly-archive-no-real-key", "DEEPSEEK_MODEL": "testonly-fixed"}))
        self.adapter = ExistingVoiceAdapter(self.server, module_transport=httpx.MockTransport(respond))
        self.adapter.service = self.service
        self.service.adapter = self.adapter

    asyncTearDown = archived_cases.ArchivedRevalidationTests.asyncTearDown
    setup_job = archived_cases.ArchivedRevalidationTests.setup_job
    job = archived_cases.ArchivedRevalidationTests.job
    forbidden_calls = archived_cases.ArchivedRevalidationTests.forbidden_calls
    recover = archived_cases.ArchivedRevalidationTests.recover

    async def cmd(self, name, args):
        if name == "save_session_draft" and self.commerce_override is not None:
            args = deepcopy(args)
            args["draft"]["commerce"] = self.commerce_override
        return await archived_cases.ArchivedRevalidationTests.cmd(self, name, args)

    async def seed_rejected(self, mode="whole_script"):
        # The real bridge and MockTransport create the actual effective-product
        # receipt. Only the old validator rejection is injected, after archive.
        original_module_finish = module_generation.finish_received_module
        def reject_final(receipt, product, key):
            if key == "cta":
                raise BusinessError("GENERATED_MODULE_NEEDS_REVIEW", "TESTONLY旧验证器拒绝")
            return original_module_finish(receipt, product, key)
        target = (patch.object(whole_script_generation, "finish_received_script",
            side_effect=BusinessError("GENERATED_MODULE_NEEDS_REVIEW", "TESTONLY旧验证器拒绝"))
            if mode == "whole_script" else patch.object(module_generation, "finish_received_module", side_effect=reject_final))
        with target:
            prep, job = await self.setup_job(mode)
        before = await self.job(job)
        self.assertEqual(before["status"], "failed")
        self.assertEqual(len(self.sent), 1 if mode == "whole_script" else len(archived_cases.KEYS))
        attempt = next(row for row in before["items"][0]["provider_attempts"] if row["validation_state"] == "rejected")
        with self.service.store.transaction() as db:
            archive = self.service.store.exact(db, attempt["provider_response_ref"])
            product = self.service.store.exact(db, before["items"][0]["input"]["product_ref"])["body"]
        raw = commerce_config.active_product_fields(product)
        self.assertNotEqual(archive["body"]["response"]["validation_product"], raw)
        return prep, job, before, archive

    async def test_real_bridge_empty_room_revalidates_effective_snapshot_without_provider(self):
        prep, job, before, archive = await self.seed_rejected()
        _, final = await self.recover(job)
        self.assertEqual(final["status"], "succeeded", final)
        check = final["items"][0]["archive_revalidations"][-1]["checks"][0]
        self.assertEqual(check["validation_details"]["validation_product_contract"], "frozen_room_effective_snapshot")
        self.assertEqual(check["validation_details"]["validation_product_hash"], digest(archive["body"]["response"]["validation_product"]))
        self.assertEqual(final["items"][0]["provider_attempts"], before["items"][0]["provider_attempts"])
        self.assertEqual(len(self.sent), 1)
        with self.service.store.transaction() as db:
            self.assertEqual(self.service.store.exact(db, archive["ref"]), archive)
            self.assertEqual(len(self.service.store.all(db, "provider_response")), 1)
        self.assertTrue((await self.service.query("read_preparation", {"preparation_id": prep["id"]}))["body"]["script_refs"])

    async def test_frozen_room_promotion_and_scene_are_validated_as_same_effective_input(self):
        self.commerce_override = {"scene": "TESTONLY本场场景", "promotion": "TESTONLY商家本场活动说明"}
        _, job, _, archive = await self.seed_rejected()
        effective = archive["body"]["response"]["validation_product"]
        self.assertEqual(effective["_room_scene"], "TESTONLY本场场景")
        self.assertEqual(effective["promotion"], "TESTONLY商家本场活动说明")
        _, final = await self.recover(job)
        self.assertEqual(final["status"], "succeeded", final)
        self.assertEqual(len(self.sent), 1)

    async def test_provider_configuration_change_still_rejects_before_revalidation(self):
        _, job, _, _ = await self.seed_rejected()
        self.server.os.environ["DEEPSEEK_MODEL"] = "testonly-changed-model"
        _, final = await self.recover(job)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["items"][0]["error"]["code"], "ARCHIVED_RESPONSE_INPUT_MISMATCH")
        self.assertEqual(len(self.sent), 1)

    async def test_real_bridge_module_reuses_siblings_and_revalidates_same_effective_snapshot(self):
        self.commerce_override = {"scene": "TESTONLY模块本场场景", "promotion": "TESTONLY模块活动"}
        _, job, before, archive = await self.seed_rejected("module")
        siblings = deepcopy(before["items"][0]["module_results"])
        _, final = await self.recover(job)
        self.assertEqual(final["status"], "succeeded", final)
        item = final["items"][0]
        self.assertEqual(item["provider_attempts"], before["items"][0]["provider_attempts"])
        for key, result in siblings.items():
            self.assertEqual(item["module_results"][key]["module_ref"], result["module_ref"])
        check = item["archive_revalidations"][-1]["checks"][0]
        self.assertEqual(check["target"], "cta")
        self.assertEqual(check["validation_details"]["validation_product_contract"], "frozen_room_effective_snapshot")
        self.assertEqual(check["validation_details"]["validation_product_hash"], digest(archive["body"]["response"]["validation_product"]))
        self.assertEqual(len(self.sent), len(archived_cases.KEYS))
        with self.service.store.transaction() as db:
            self.assertEqual(self.service.store.exact(db, archive["ref"]), archive)

    async def test_module_configuration_change_still_rejects_without_provider(self):
        _, job, _, _ = await self.seed_rejected("module")
        self.server.os.environ["DEEPSEEK_MODEL"] = "testonly-changed-model"
        _, final = await self.recover(job)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["items"][0]["error"]["code"], "ARCHIVED_RESPONSE_INPUT_MISMATCH")
        self.assertEqual(len(self.sent), len(archived_cases.KEYS))

    def test_only_two_complete_frozen_snapshots_are_accepted_not_field_stripping(self):
        product = {"id": "p-offline", "name": "TESTONLY收纳袋", "price": 29, "currency": "CNY"}
        draft_ref = {"kind": "draft", "id": "draft-offline", "revision": 1, "content_hash": digest("draft")}
        context = {"draft_ref": draft_ref, "records": [{"ref": draft_ref, "body": {"draft": {"commerce": {
            "scene": "TESTONLY收纳场景", "promotion": "TESTONLY活动"}}}}]}
        effective = commerce_config.active_product_fields(commerce_config.effective_product(product, {
            "commerce": commerce_config.parse_room(context["records"][0]["body"]["draft"]["commerce"])}))
        self.assertEqual(verified_archived_validation_product(product, context, product)["contract"], "catalog_snapshot")
        self.assertEqual(verified_archived_validation_product(product, context, effective)["contract"], "frozen_room_effective_snapshot")
        for invalid in ({**effective, "price": 999}, {key: value for key, value in effective.items() if key != "_gifts"},
                        {**effective, "arbitrary_extra": "cannot ignore"}):
            self.assertIsNone(verified_archived_validation_product(product, context, invalid))
        foreign = {**context, "draft_ref": {**draft_ref, "id": "not-the-frozen-draft"}}
        self.assertIsNone(verified_archived_validation_product(product, foreign, effective))
