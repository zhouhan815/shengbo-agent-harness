"""Changing one actual module prompt must not reuse its old cached response."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from app.prelive import module_generation
import test_prelive_module_recovery_v1 as recovery_helpers


class ModulePromptCacheRC6Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.harness = recovery_helpers.ModuleRecoveryV1Tests(methodName="runTest")
        await self.harness.asyncSetUp()

    async def asyncTearDown(self):
        await self.harness.asyncTearDown()

    async def test_changed_cta_purpose_invalidates_only_cta_and_same_prompt_reuses(self):
        h = self.harness
        _, submitted = await h.setup_plan()
        with h.service.store.transaction(write=True) as c:
            saved = h.service.store.get(c, "prelive_job", submitted["job_id"])
            body = deepcopy(saved["body"])
            body["status"] = "running"
            body["items"][0]["status"] = "running"
            h.service.store.put(c, "prelive_job", submitted["job_id"], body,
                                expected=saved["ref"]["revision"])
        first = await h.service._produce_script(body, body["items"][0])
        self.assertEqual(h.calls, ["opening_long", "product_intro", "selling_points", "cta"])
        prior = {key: value["reuse_ref"] for key, value in first["modules"].items()}
        revision = {key: module_generation.module_prompt_revision(key) for key in prior}
        changed = module_generation._MODULE_PURPOSES["cta"] + " 本次仅调整CTA职责。"
        with patch.dict(module_generation._MODULE_PURPOSES, {"cta": changed}):
            self.assertNotEqual(module_generation.module_prompt_revision("cta"), revision["cta"])
            self.assertEqual(module_generation.module_prompt_revision("opening_long"), revision["opening_long"])
            second = await h.service._produce_script(body, body["items"][0])
            self.assertEqual(h.calls, ["opening_long", "product_intro", "selling_points", "cta", "cta"])
            for key in prior:
                if key != "cta":
                    self.assertEqual(second["modules"][key]["reuse_ref"], prior[key])
            third = await h.service._produce_script(body, body["items"][0])
            self.assertEqual(len(h.calls), 5)
            self.assertEqual(third["modules"], second["modules"])
            with h.service.store.transaction() as c:
                job = h.service.store.get(c, "prelive_job", submitted["job_id"])["body"]
                responses = h.service.store.all(c, "provider_response")
                self.assertEqual(len(responses), 5)
                self.assertEqual(job["items"][0]["provider_attempts"][-1]["input"]["module_key"], "cta")
                self.assertEqual(responses[-1]["body"]["response"]["prompt_revision"], module_generation.module_prompt_revision("cta"))
