"""Exact references may omit optional wire metadata; identity guards stay strict."""
import unittest

import test_prelive_service as helpers
from app.prelive import BusinessError
from shengbo_contracts.business import MODULE_KEYS


def shorthand(value):
    if isinstance(value, list):
        return [shorthand(v) for v in value]
    if isinstance(value, dict):
        return {k: shorthand(v) for k, v in value.items() if k != "schema_version"}
    return value


class ContextReferenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = helpers.BusinessTests.asyncSetUp
    asyncTearDown = helpers.BusinessTests.asyncTearDown
    cmd = helpers.BusinessTests.cmd
    product = helpers.BusinessTests.product
    prep = helpers.BusinessTests.prep
    state = helpers.BusinessTests.state
    prepared_text = helpers.BusinessTests.prepared_text

    async def inputs(self, name="情人节口红"):
        product = await self.product(name)
        _, prep = await self.prep()
        saved = await self.cmd("update_preparation_products", {"preparation_id": prep["id"],
            "expected_revision": prep["revision"], "selected_product_refs": [product]})
        saved = await self.cmd("save_session_draft", {"preparation_id": prep["id"],
            "expected_revision": saved["data"]["preparation_ref"]["revision"],
            "draft": {"theme": "情人节", "commerce": {}}})
        state = await self.state(prep)
        return prep, state, state["context_inputs"]

    async def test_discoverable_context_inputs_accept_shorthand_and_save_canonical_refs(self):
        prep, state, inputs = await self.inputs()
        self.assertEqual(inputs["expected_revision"], state["ref"]["revision"])
        self.assertEqual(inputs["product_refs"], state["body"]["selected_product_refs"])
        self.assertEqual(inputs["draft_ref"], state["body"]["draft_ref"])
        self.assertEqual(state["context_input_contract"]["command"], "save_context_bundle")
        self.assertNotIn("user_decisions", inputs)
        result = await self.cmd("save_context_bundle", shorthand(inputs))
        context = await self.service.query("read_context", {"ref": result["data"]["context_bundle_ref"]})
        self.assertEqual(context["body"]["draft_ref"], inputs["draft_ref"])
        self.assertEqual(context["body"]["product_refs"], inputs["product_refs"])

    async def test_stale_draft_identifies_changed_field_without_replacing_agent_arguments(self):
        prep, state, inputs = await self.inputs()
        await self.cmd("save_session_draft", {"preparation_id": prep["id"],
            "expected_revision": state["ref"]["revision"], "draft": {"theme": "已确认新主题"}})
        current = await self.state(prep)
        stale = {**shorthand(inputs), "expected_revision": current["ref"]["revision"]}
        with self.assertRaises(BusinessError) as error:
            await self.cmd("save_context_bundle", stale)
        self.assertEqual(error.exception.code, "CONTEXT_INPUTS_CHANGED")
        self.assertEqual(error.exception.details["changed_fields"], ["draft_ref"])
        self.assertEqual(error.exception.details["refresh_query"], {"query": "read_preparation",
            "args": {"preparation_id": prep["id"]}})
        self.assertEqual(error.exception.details["input_section"], "context_inputs")
        self.assertNotIn("expected_inputs", error.exception.details)
        self.assertEqual(await self.state(prep), current)

    async def test_hash_revision_schema_and_foreign_scope_still_fail(self):
        prep, state, inputs = await self.inputs()
        _, foreign_state, _ = await self.inputs("异本场口红")
        cases = [({**inputs["draft_ref"], "content_hash": "sha256:" + "0" * 64}, "REF_HASH_MISMATCH"),
                 ({**inputs["draft_ref"], "revision": 99}, "NOT_FOUND"),
                 ({**inputs["draft_ref"], "schema_version": "forged"}, "REF_SCHEMA_MISMATCH"),
                 (foreign_state["body"]["draft_ref"], "ARTIFACT_SCOPE_CONFLICT")]
        for ref, code in cases:
            with self.subTest(code=code), self.assertRaises(BusinessError) as error:
                await self.cmd("save_context_bundle", {**inputs, "draft_ref": ref})
            self.assertEqual(error.exception.code, code)
            self.assertEqual(await self.state(prep), state)

    async def test_other_valid_product_is_not_silently_selected(self):
        prep, state, inputs = await self.inputs()
        other = await self.product("其他商品")
        with self.assertRaises(BusinessError) as error:
            await self.cmd("save_context_bundle", {**inputs, "product_refs": [other]})
        self.assertEqual(error.exception.code, "CONTEXT_INPUTS_CHANGED")
        self.assertEqual(error.exception.details["changed_fields"], ["product_refs"])
        self.assertEqual(await self.state(prep), state)

    async def test_followup_plan_patch_playlist_and_compile_use_exact_shorthand(self):
        product, _, prep, old_script_ref = await self.prepared_text()
        state = await self.state(prep)
        saved_context = await self.cmd("save_context_bundle", shorthand(state["context_inputs"]))
        context_ref = saved_context["data"]["context_bundle_ref"]
        old_script = await self.service.query("read_script", {"ref": old_script_ref})
        actions = []
        for key in MODULE_KEYS:
            base_ref = old_script["body"]["module_refs"][key]
            actions.append({"key": key, "action": "disable" if not base_ref else "patch" if key == "opening_long" else "reuse",
                **({"base_module_ref": shorthand(base_ref)} if base_ref else {}), "instruction": "只调整开场" if key == "opening_long" else ""})
        args = {"preparation_id": prep["id"], "expected_revision": saved_context["data"]["preparation_ref"]["revision"],
            "bundle_ref": shorthand(context_ref), "items": [{"product_ref": product, "variant_id": "variant-a1", "version_number": 1,
                "base_script_ref": shorthand(old_script_ref), "modules": actions}]}
        plan = await self.cmd("save_script_plan", args)
        queued = await self.cmd("submit_script_change", {"preparation_id": prep["id"],
            "expected_revision": plan["data"]["preparation_ref"]["revision"],
            "plan_ref": shorthand(plan["data"]["script_plan_ref"]), "context_bundle_ref": shorthand(context_ref),
            "expected_generation_epoch": plan["data"]["generation_epoch"]})
        await self.service.drain_jobs()
        job = await self.service.query("read_job", {"job_id": queued["data"]["job_id"]})
        self.assertEqual(job["body"]["status"], "succeeded")
        self.assertEqual(job["body"]["context_ref"], context_ref)
        self.assertEqual(job["body"]["plan_ref"], plan["data"]["script_plan_ref"])
        state = await self.state(prep)
        new_ref = state["body"]["script_refs"][0]
        new_script = await self.service.query("read_script", {"ref": shorthand(new_ref)})
        self.assertEqual(new_script["body"]["parent_ref"], old_script_ref)
        for key in MODULE_KEYS:
            if key != "opening_long":
                self.assertEqual(new_script["body"]["module_refs"][key], old_script["body"]["module_refs"][key])
        playlist = await self.cmd("save_playlist", {"preparation_id": prep["id"],
            "expected_revision": state["ref"]["revision"], "items": [{"occurrence_id": "new-first",
                "product_id": product["id"], "script_revision_ref": shorthand(new_ref)}], "loop_mode": "infinite"})
        compiled = await self.cmd("compile_playback_plan", {"preparation_id": prep["id"],
            "expected_revision": playlist["data"]["preparation_ref"]["revision"],
            "playlist_ref": shorthand(playlist["data"]["playlist_ref"])})
        review = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        self.assertEqual(review["compiled_plan_ref"], compiled["data"]["compiled_plan_ref"])
        self.assertEqual(review["playlist_ref"], playlist["data"]["playlist_ref"])
        self.assertEqual(self.adapter.rendered, [])


if __name__ == "__main__":
    unittest.main()
