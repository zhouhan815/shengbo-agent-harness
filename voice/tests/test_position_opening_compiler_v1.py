"""Offline contract checks for saved-position openings and actual spoken scope."""
from copy import deepcopy
import unittest

from app.prelive.playback_compiler import compile_items, opening_for_position, playback_statistics, reorder
from app.prelive.storage import BusinessError, digest
from shengbo_contracts.business import ACTIVE_MODULE_KEYS, LEGACY_MODULE_KEYS, MODULE_KEYS, PLAYBACK_STRUCTURE_VERSION


class PositionOpeningCompilerTests(unittest.TestCase):
    def fixture(self, names, *, omit=()):
        scripts, modules, items = {}, {}, []
        for name in dict.fromkeys(names):
            refs = {}
            for key in MODULE_KEYS:
                if (name, key) in omit:
                    continue
                ref = {"id": name + ":" + key}
                refs[key] = ref
                modules[ref["id"]] = {"module_key": key, "sentences": [{
                    "id": name + ":sentence:" + key, "text": name + "-" + key,
                    "spoken_text": name + "-" + key, "fact_ids": ["fact-" + name]}]}
            scripts[name] = {"product_id": name[0], "variant_id": name, "module_refs": refs}
        for index, name in enumerate(names):
            items.append({"occurrence_id": "occ-" + str(index), "product_id": name[0], "script_revision_ref": {"id": name}})
        return items, scripts, modules

    def compile(self, items, scripts, modules, loop_mode="infinite"):
        return compile_items(items, loop_mode, lambda r: scripts[r["id"]], lambda r: modules[r["id"]])

    def test_seven_positions_and_one_iteration_no_lcm(self):
        items, scripts, modules = self.fixture(["A1", "B1", "A2", "C1", "A3", "B2", "D1"])
        plan = self.compile(items, scripts, modules)
        self.assertEqual(plan["compiler_version"], PLAYBACK_STRUCTURE_VERSION)
        self.assertEqual(plan["cycle_length"], 7)
        self.assertEqual(plan["supercycle_length"], 7)
        self.assertEqual([e["opening_mode"] for e in plan["entries"]], ["opening_long", "opening_short", "opening_short", "opening_short", "opening_short", "opening_long", "opening_short"])
        # Repeat and jump use these entries, rather than a global play counter.
        self.assertEqual((plan["entries"] * 2)[7]["original_position"], 1)
        self.assertEqual(plan["entries"][5]["opening_mode"], "opening_long")

    def test_one_item_needs_long_only(self):
        items, scripts, modules = self.fixture(["A1"], omit=[("A1", "opening_short")])
        plan = self.compile(items, scripts, modules)
        self.assertEqual(len(plan["entries"]), 1)
        self.assertEqual(len(plan["requirements"]), 5)
        self.assertEqual({s["module_key"] for s in plan["entries"][0]["sentences"]}, {"opening_long", "product_intro", "selling_points", "campaign", "cta"})

    def test_two_items_do_not_require_unused_opening(self):
        items, scripts, modules = self.fixture(["A1", "B1"], omit=[("A1", "opening_short"), ("B1", "opening_long")])
        plan = self.compile(items, scripts, modules)
        self.assertEqual(len(plan["entries"]), 2)
        self.assertEqual(len(plan["requirements"]), 10)
        self.assertEqual([e["opening_mode"] for e in plan["entries"]], ["opening_long", "opening_short"])

    def test_legacy_transition_is_readable_but_not_selected_or_generated(self):
        self.assertIn("transition", LEGACY_MODULE_KEYS)
        self.assertNotIn("transition", ACTIVE_MODULE_KEYS)
        items, scripts, modules = self.fixture(["A1"])
        old_hash = digest({"items": items, "scripts": scripts, "modules": modules})
        plan = self.compile(items, scripts, modules)
        self.assertNotIn("transition", {s["module_key"] for e in plan["entries"] for s in e["sentences"]})
        self.assertEqual(old_hash, digest({"items": items, "scripts": scripts, "modules": modules}))
        plan["base_items"][0]["product_id"] = "must-not-mutate-input"
        self.assertEqual(items[0]["product_id"], "A")

    def test_campaign_optional_and_required_body_remains_required(self):
        items, scripts, modules = self.fixture(["A1"], omit=[("A1", "campaign")])
        self.assertEqual(len(self.compile(items, scripts, modules)["requirements"]), 4)
        del scripts["A1"]["module_refs"]["selling_points"]
        with self.assertRaises(BusinessError) as caught:
            self.compile(items, scripts, modules)
        self.assertEqual(caught.exception.code, "MISSING_MODULE")
        self.assertEqual(caught.exception.details["missing_modules"][0]["module_key"], "selling_points")

    def test_reorder_returns_exact_gap_and_preserves_body_audio_keys(self):
        items, scripts, modules = self.fixture(["A1", "B1"], omit=[("A1", "opening_short"), ("B1", "opening_long")])
        before = self.compile(items, scripts, modules)
        before_body = {s["module_ref"]["id"]: s["requirement_id"] for e in before["entries"] for s in e["sentences"] if s["module_key"] not in {"opening_long", "opening_short"}}
        swapped = list(reversed(items))
        with self.assertRaises(BusinessError) as caught:
            self.compile(swapped, scripts, modules)
        details = caught.exception.details
        gaps = details["missing_modules"]
        self.assertEqual([(g["script_revision_ref"]["id"], g["module_key"], g["positions"]) for g in gaps], [("B1", "opening_long", [1]), ("A1", "opening_short", [2])])
        self.assertTrue(details["suggested_next_step"]["preserve_existing_body"])
        for gap in gaps:
            self.assertIn("product_intro", gap["reusable_module_refs"])
            name, key = gap["script_revision_ref"]["id"], gap["module_key"]
            ref = {"id": name + ":" + key}
            scripts[name]["module_refs"][key] = ref
            modules[ref["id"]] = {"module_key": key, "sentences": [{"id": ref["id"], "text": "新增对应开场" + name}]}
        after = self.compile(swapped, scripts, modules)
        after_body = {s["module_ref"]["id"]: s["requirement_id"] for e in after["entries"] for s in e["sentences"] if s["module_key"] not in {"opening_long", "opening_short"}}
        self.assertEqual(before_body, after_body)

    def test_same_version_in_two_position_types_gets_both_only_if_needed(self):
        items, scripts, modules = self.fixture(["A1", "A1", "A1", "A1", "A1", "A1"])
        plan = self.compile(items, scripts, modules)
        self.assertEqual(plan["cycle_length"], 6)
        self.assertEqual(len(plan["requirements"]), 6)  # two openings + four body parts
        self.assertEqual(plan["playback_statistics"]["sentence_count"], 30)
        self.assertEqual(plan["playback_statistics"]["unique_synthesis_sentences"], 6)

    def test_identical_gap_groups_only_same_script_type_positions(self):
        items, scripts, modules = self.fixture(["A1"] * 7, omit=[("A1", "opening_short")])
        with self.assertRaises(BusinessError) as caught:
            self.compile(items, scripts, modules)
        gaps = caught.exception.details["missing_modules"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["positions"], [2, 3, 4, 5, 7])
        self.assertEqual(len(gaps[0]["occurrence_ids"]), 5)

    def test_actual_statistics_exclude_unselected_opening_transition_and_repeat_audio(self):
        items, scripts, modules = self.fixture(["A1", "A1"])
        plan = self.compile(items, scripts, modules)
        assets = [{"requirement_id": r["requirement_id"], "duration": 1.25} for r in plan["requirements"]]
        stats = playback_statistics(plan["entries"], assets)
        selected = [s for e in plan["entries"] for s in e["sentences"]]
        self.assertEqual(stats["spoken_characters"], sum(len(s["spoken_text"]) for s in selected))
        self.assertEqual(stats["sentence_count"], 10)
        self.assertEqual(stats["audio_duration_seconds"], 12.5)
        self.assertEqual(stats["unique_synthesis_sentences"], 6)
        self.assertGreater(stats["spoken_characters"], stats["unique_synthesis_characters"])
        self.assertIsNone(plan["playback_statistics"]["audio_duration_seconds"])
        assets[0]["duration"] = float("nan")
        self.assertIsNone(playback_statistics(plan["entries"], assets)["audio_duration_seconds"])

    def test_read_only_approved_legacy_plan_is_not_migrated_in_place(self):
        items, scripts, modules = self.fixture(["A1", "B1"])
        approved_old = {"compiler_version": "prelive-supercycle-v1", "base_items": deepcopy(items), "entries": [], "approval_hash": "approved-immutable"}
        stored = deepcopy(approved_old)
        new_plan = self.compile(approved_old["base_items"], scripts, modules)
        self.assertEqual(stored, approved_old)
        self.assertNotEqual(new_plan["compiler_version"], approved_old["compiler_version"])

    def test_reorder_then_compile_follows_new_saved_positions(self):
        items, scripts, modules = self.fixture(["A1", "A2", "B1", "B2", "C1", "C2", "A3"])
        out = reorder(items, {"mode": "interleave", "product_group_order": ["A", "B", "C"], "within_product": "keep", "seed": "a" * 32})
        plan = self.compile(out["proposed_items"], scripts, modules)
        self.assertEqual([e["occurrence_id"] for e in plan["entries"]], [i["occurrence_id"] for i in out["proposed_items"]])
        self.assertEqual([e["original_position"] for e in plan["entries"] if e["opening_mode"] == "opening_long"], [1, 6])

    def test_invalid_queue_and_position_rejected(self):
        for position in (0, -1, True, 1.0):
            with self.assertRaises(BusinessError):
                opening_for_position(position)
        items, scripts, modules = self.fixture(["A1", "A1"])
        items[1]["occurrence_id"] = items[0]["occurrence_id"]
        with self.assertRaises(BusinessError) as caught:
            self.compile(items, scripts, modules)
        self.assertEqual(caught.exception.code, "DUPLICATE_OCCURRENCE_ID")

    def test_exact_revision_reader_reuse_and_output_isolation(self):
        items, scripts, modules = self.fixture(["A1"] * 100)
        reads = {"scripts": 0, "modules": 0}
        def script_reader(ref):
            reads["scripts"] += 1
            return scripts[ref["id"]]
        def module_reader(ref):
            reads["modules"] += 1
            return modules[ref["id"]]
        plan = compile_items(items, "infinite", script_reader, module_reader)
        self.assertEqual(reads, {"scripts": 1, "modules": 6})
        old_hash = digest({"items": items, "scripts": scripts, "modules": modules})
        plan["entries"][0]["sentences"][0]["fact_ids"].append("do-not-mutate-source")
        plan["entries"][0]["script_revision_ref"]["id"] = "do-not-mutate-source"
        self.assertEqual(old_hash, digest({"items": items, "scripts": scripts, "modules": modules}))


if __name__ == "__main__":
    unittest.main()
