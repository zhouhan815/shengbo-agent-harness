"""Real temporary business store: queue gaps, local repair and approval binding."""
import unittest

import test_prelive_service as fixtures
from app.prelive import BusinessError


class OpeningServiceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BusinessTests.asyncSetUp
    asyncTearDown = fixtures.BusinessTests.asyncTearDown
    cmd = fixtures.BusinessTests.cmd
    product = fixtures.BusinessTests.product
    prep = fixtures.BusinessTests.prep
    state = fixtures.BusinessTests.state

    async def test_reorder_repairs_only_missing_openings_retains_body_and_reapproves(self):
        product = await self.product()
        room, prep = await self.prep()
        selected = await self.cmd("update_preparation_products", {"preparation_id": prep["id"], "expected_revision": prep["revision"], "selected_product_refs": [product]})
        draft = await self.cmd("save_session_draft", {"preparation_id": prep["id"], "expected_revision": selected["data"]["preparation_ref"]["revision"], "draft": {"theme": "本场介绍", "commerce": {}}})
        context = await self.cmd("save_context_bundle", {"preparation_id": prep["id"], "expected_revision": draft["data"]["preparation_ref"]["revision"], "product_refs": [product], "draft_ref": draft["data"]["draft_ref"]})
        items = []
        for number, opening in ((1, "opening_long"), (2, "opening_short")):
            items.append({"product_ref": product, "variant_id": "v" + str(number), "version_number": number,
                "modules": [{"key": key, "action": "generate", "instruction": "本版本重点" + str(number)}
                    for key in (opening, "product_intro", "selling_points", "cta")]})
        async def execute(items):
            state = await self.state(prep)
            plan = await self.cmd("save_script_plan", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "bundle_ref": context["data"]["context_bundle_ref"], "items": items})
            job = await self.cmd("submit_script_change", {"preparation_id": prep["id"], "expected_revision": plan["data"]["preparation_ref"]["revision"], "plan_ref": plan["data"]["script_plan_ref"], "context_bundle_ref": context["data"]["context_bundle_ref"], "expected_generation_epoch": plan["data"]["generation_epoch"]})
            await self.service.drain_jobs()
            result = await self.service.query("read_job", {"job_id": job["data"]["job_id"]})
            self.assertEqual(result["body"]["status"], "succeeded", result)
        await execute(items)
        state = await self.state(prep)
        original_refs = state["body"]["script_refs"]
        scripts = [await self.service.query("read_script", {"ref": ref}) for ref in original_refs]
        self.assertEqual([set(s["body"]["module_refs"]) for s in scripts], [set(["opening_long", "product_intro", "selling_points", "cta"]), set(["opening_short", "product_intro", "selling_points", "cta"])])
        queue = [{"occurrence_id": "o" + str(i), "product_id": product["id"], "script_revision_ref": ref} for i, ref in enumerate(original_refs)]
        async def save(items):
            state = await self.state(prep)
            return await self.cmd("save_playlist", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "items": items})
        async def compile_current():
            state = await self.state(prep)
            return await self.cmd("compile_playback_plan", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "playlist_ref": state["body"]["playlist_ref"]})
        await save(queue)
        await compile_current()
        old_review = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        self.assertEqual(old_review["playback_statistics"]["entry_count"], 2)
        self.assertEqual(old_review["playback_statistics"]["sentence_count"], 8)
        state = await self.state(prep)
        approval = await self.cmd("approve_text_for_audio", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "review_hash": old_review["review_hash"]})
        state = await self.state(prep)
        await self.cmd("prepare_required_audio", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "approval_ref": approval["data"]["approval_ref"], "review_hash": old_review["review_hash"], "expected_generation_epoch": state["body"]["generation_epoch"]})
        await self.service.drain_jobs()
        # Generating/attaching WAV metadata must not change approved text hash.
        self.assertEqual((await self.service.query("create_text_review", {"preparation_id": prep["id"]}))["review_hash"], old_review["review_hash"])
        before_audio = len(self.adapter.rendered)
        await save(list(reversed(queue)))
        with self.assertRaises(BusinessError) as caught:
            await compile_current()
        gaps = caught.exception.details["missing_modules"]
        self.assertEqual({g["module_key"] for g in gaps}, {"opening_long", "opening_short"})
        self.assertEqual(caught.exception.details["suggested_next_step"]["scope"], "missing_modules_only")
        count_before = len(self.adapter.generated)
        repaired_items = []
        for script in scripts:
            needed = next(g for g in gaps if g["script_revision_ref"] == script["ref"])
            actions = [{"key": key, "action": "reuse", "base_module_ref": ref} for key, ref in script["body"]["module_refs"].items() if ref]
            actions.append({"key": needed["module_key"], "action": "generate", "instruction": "只补齐本版在重排后所需的缺失开场。"})
            repaired_items.append({"product_ref": product, "variant_id": script["body"]["variant_id"], "version_number": script["body"]["version_number"], "base_script_ref": script["ref"], "modules": actions})
        await execute(repaired_items)
        self.assertEqual(len(self.adapter.generated) - count_before, 2)
        state = await self.state(prep)
        updated = [await self.service.query("read_script", {"ref": ref}) for ref in state["body"]["script_refs"]]
        for old, new in zip(scripts, updated):
            for key in ("product_intro", "selling_points", "cta"):
                self.assertEqual(old["body"]["module_refs"][key], new["body"]["module_refs"][key])
        await compile_current()
        new_review = await self.service.query("create_text_review", {"preparation_id": prep["id"]})
        self.assertNotEqual(old_review["review_hash"], new_review["review_hash"])
        with self.assertRaises(BusinessError):
            state = await self.state(prep)
            await self.cmd("prepare_required_audio", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "approval_ref": approval["data"]["approval_ref"], "review_hash": old_review["review_hash"], "expected_generation_epoch": state["body"]["generation_epoch"]})
        self.assertEqual(before_audio, len(self.adapter.rendered))
        state = await self.state(prep)
        new_approval = await self.cmd("approve_text_for_audio", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "review_hash": new_review["review_hash"]})
        state = await self.state(prep)
        await self.cmd("prepare_required_audio", {"preparation_id": prep["id"], "expected_revision": state["ref"]["revision"], "approval_ref": new_approval["data"]["approval_ref"], "review_hash": new_review["review_hash"], "expected_generation_epoch": state["body"]["generation_epoch"]})
        await self.service.drain_jobs()
        # The fake outputs the same fact-preserving opening text; all matching
        # prior WAV files remain usable. No body is synthesized a second time.
        self.assertEqual(before_audio, len(self.adapter.rendered))
        with self.service.store.transaction() as connection:
            # Exact historical scripts still exist unchanged after repair.
            self.assertEqual([self.service.store.exact(connection, ref)["body"] for ref in original_refs], [s["body"] for s in scripts])


if __name__ == "__main__":
    unittest.main()
