"""Full previous copy reaches the provider without live-head retry drift."""
from copy import deepcopy
import json
import unittest

import test_whole_script_generation_v1 as whole
from app.prelive.script_version_context import script_version_context, _full_rewrite


class VersionReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = whole.WholeServiceTests()
        await self.fixture.asyncSetUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_existing_copy_and_batch_briefs_are_exact_and_retry_stable(self):
        f = self.fixture
        prep, initial_job = await f.setup_job(2)
        await f.service.drain_jobs()
        state = await f.service.query("read_preparation", {"preparation_id": prep["id"]})
        prior = deepcopy(state["body"]["script_refs"])
        context = await f.cmd("save_context_bundle", {
            "preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
            "product_refs": state["body"]["selected_product_refs"], "draft_ref": state["body"]["draft_ref"],
            "prior_script_refs": prior})
        product = state["body"]["selected_product_refs"][0]
        # Reuse the existing plan schema: a brief is declared once in an
        # existing module instruction, then frozen alongside the other tasks.
        briefs = ["版本简报（作者用）：主问题独练约不到搭档；场景新手自主练；先单人用途再成套；问答到手组成；收口选颜色。",
                  "版本简报（作者用）：主问题课外想多练；场景培训学员；先课外用途再三件组成；问答颜色差异；收口安排课外练习。"]
        items = [{"product_ref": product, "variant_id": "new-scene-" + str(i + 1), "modules": [
            {**action, "instruction": (briefs[i] if index == 0 else "") + "讲清当前模块的有据用途。"}
            for index, action in enumerate(whole.actions())]} for i in (0, 1)]
        plan = await f.cmd("save_script_plan", {"preparation_id": prep["id"],
            "expected_revision": context["preparation_ref"]["revision"], "bundle_ref": context["context_bundle_ref"], "items": items})
        job = await f.cmd("submit_script_change", {"preparation_id": prep["id"],
            "expected_revision": plan["preparation_ref"]["revision"], "plan_ref": plan["script_plan_ref"],
            "context_bundle_ref": context["context_bundle_ref"], "expected_generation_epoch": plan["generation_epoch"]})
        snapshot = await f.read_job(job)
        self.assertTrue(all(item["input"]["version_context_contract"] == "existing-script-text-v3"
                            for item in snapshot["items"]))
        with f.service.store.transaction() as c:
            before = script_version_context(f.service, c, snapshot, snapshot["items"][0]["input"])
        await f.service.drain_jobs()
        result = await f.read_job(job)
        self.assertEqual(result["status"], "succeeded", result)
        task = f.sent[-1]
        refs_context = task["context_bundle"]["same_product_existing_scripts"]
        self.assertEqual([s["ref"] for s in refs_context], prior)
        self.assertEqual(refs_context[0]["modules"]["cta"][0]["spoken_text"], "有需要可以了解一下。")
        self.assertTrue(all(row["reference_role"] == "difference_comparison_only" for row in refs_context))
        self.assertEqual(task["context_bundle"]["writing_operation"]["kind"], "generate_new_variant")
        self.assertEqual(len(task["context_bundle"]["script_variant_briefs"]), 2)
        for actual, planned in zip(task["context_bundle"]["script_variant_briefs"], items):
            self.assertEqual(actual["module_instructions"],
                {action["key"]: action["instruction"] for action in planned["modules"] if action["action"] != "disable"})
        contract = task["version_writing_contract"]
        self.assertEqual(contract["default_minimum_changed_content_axes"], 2)
        self.assertEqual(len(contract["vary"]), 5)
        self.assertIn("不是硬拒绝门禁", contract["user_override"])
        with f.service.store.transaction() as c:
            after = script_version_context(f.service, c, result, result["items"][0]["input"])
        self.assertEqual(before, after, "Other completed items must not change the frozen dependency")
        self.assertEqual(len(f.sent), 4)

    async def test_v2_full_batch_rewrite_removes_old_prose_and_old_directives_but_keeps_identities(self):
        f = self.fixture
        prep, _ = await f.setup_job(2)
        await f.service.drain_jobs()
        state = await f.service.query("read_preparation", {"preparation_id": prep["id"]})
        prior = deepcopy(state["body"]["script_refs"])
        context = await f.cmd("save_context_bundle", {
            "preparation_id": prep["id"], "expected_revision": state["ref"]["revision"],
            "product_refs": state["body"]["selected_product_refs"], "draft_ref": state["body"]["draft_ref"],
            "prior_script_refs": prior, "user_decisions": [{"writing_direction": "主播当面销售，完整重新写作。"}]})
        product = state["body"]["selected_product_refs"][0]
        items = [{"product_ref": product, "variant_id": "whole-v" + str(i + 1),
                  "base_script_ref": ref, "modules": whole.actions()} for i, ref in enumerate(prior)]
        plan = await f.cmd("save_script_plan", {"preparation_id": prep["id"],
            "expected_revision": context["preparation_ref"]["revision"], "bundle_ref": context["context_bundle_ref"],
            "items": items})
        job = await f.cmd("submit_script_change", {"preparation_id": prep["id"],
            "expected_revision": plan["preparation_ref"]["revision"], "plan_ref": plan["script_plan_ref"],
            "context_bundle_ref": context["context_bundle_ref"], "expected_generation_epoch": plan["generation_epoch"]})
        snapshot = await f.read_job(job)
        frozen = deepcopy(snapshot)
        with f.service.store.transaction() as c:
            projections = [script_version_context(f.service, c, snapshot, item["input"]) for item in snapshot["items"]]
        for i, view in enumerate(projections):
            self.assertEqual(view["writing_operation"]["kind"], "rewrite_existing_version")
            self.assertEqual(view["writing_operation"]["target_script_ref"], prior[i])
            self.assertFalse(view["writing_operation"]["preserve_prior_wording"])
            self.assertEqual([row["ref"] for row in view["same_product_existing_scripts"]], prior)
            for row in view["same_product_existing_scripts"]:
                self.assertEqual(row["reference_role"], "superseded_rewrite_target")
                self.assertFalse(row["prior_text_provided"])
                self.assertNotIn("modules", row)
                self.assertEqual(set(row["module_keys"]), set(whole.KEYS))
                self.assertEqual(set(row["module_hashes"]), set(whole.KEYS))
            self.assertNotIn("有需要可以了解一下。", json.dumps(view, ensure_ascii=False))
            for old, projected in zip(snapshot["context"]["records"], view["records"]):
                self.assertEqual(old["ref"], projected["ref"])
                if old["ref"]["kind"] == "script_revision":
                    self.assertNotIn("content_diagnostics", projected["body"])
                    self.assertNotIn("generation_provenance", projected["body"])
                    self.assertEqual(old["body"]["module_refs"], projected["body"]["module_refs"])
                else:
                    self.assertEqual(old, projected)
        self.assertEqual(snapshot, frozen, "Writing projections must not mutate frozen job context")
        await f.service.drain_jobs()
        result = await f.read_job(job)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(f.sent), 4, "Two rewritten versions remain one provider request each")
        for task in f.sent[-2:]:
            wire = task["context_bundle"]
            self.assertEqual(wire["writing_operation"]["kind"], "rewrite_existing_version")
            self.assertEqual([entry["value"] for entry in wire["user_business_decisions"]],
                             snapshot["context"]["user_decisions"])
            self.assertNotIn("有需要可以了解一下。", json.dumps(wire, ensure_ascii=False))
        with f.service.store.transaction() as c:
            after = [script_version_context(f.service, c, result, item["input"]) for item in result["items"]]
        self.assertEqual(after, projections, "Completion of another item cannot shift rewrite inputs")

    async def test_v1_frozen_inputs_keep_old_full_body_shape_even_for_full_rewrite(self):
        f = self.fixture
        prep, job = await f.setup_job(1)
        await f.service.drain_jobs()
        state = await f.service.query("read_preparation", {"preparation_id": prep["id"]})
        ref = state["body"]["script_refs"][0]
        snapshot = await f.read_job(job)
        snapshot["context"]["prior_script_refs"] = [deepcopy(ref), deepcopy(ref)]
        spec = {**snapshot["items"][0]["input"], "base_script_ref": ref,
                "version_context_contract": "existing-script-text-v1"}
        frozen = deepcopy(snapshot)
        with f.service.store.transaction() as c:
            view = script_version_context(f.service, c, snapshot, spec)
            record = f.service._artifact(c, ref, "script_revision", prep["id"])
            expected = {"ref": record["ref"], "variant_id": record["body"]["variant_id"],
                "version_number": record["body"]["version_number"],
                "modules": {key: f.service._artifact(c, module_ref, "module")["body"]["sentences"]
                            for key, module_ref in record["body"]["module_refs"].items() if module_ref}}
        self.assertEqual(view["same_product_existing_scripts"], [expected])
        self.assertEqual(set(view), {"same_product_existing_scripts", "script_variant_briefs", "version_reference_policy"})
        self.assertEqual(snapshot, frozen)

    def test_partial_module_plans_are_not_misclassified_as_full_rewrites(self):
        base = {"id": "existing", "revision": 1, "content_hash": "hash"}
        self.assertFalse(_full_rewrite({"generation_mode": "module", "base_script_ref": base,
            "modules": [{"key": "cta", "action": "patch"}]}))
        self.assertFalse(_full_rewrite({"generation_mode": "whole_script", "base_script_ref": base,
            "modules": [{"key": "cta", "action": "generate"}, {"key": "selling_points", "action": "reuse"}]}))

    def test_legacy_archived_jobs_do_not_gain_new_dependency_fields(self):
        self.assertEqual(script_version_context(None, None, {}, {}), {})


if __name__ == "__main__":
    unittest.main()
