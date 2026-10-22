"""Reviewed offline platform defaults: no provider or production database."""
from pathlib import Path
import tempfile
import unittest

from app.platform_rules import platform_policy, platform_policy_catalog, RULES_PATH
from app.prelive import PreliveService
from app.prelive.storage import digest


class PlatformRuleTests(unittest.TestCase):
    def test_reviewed_defaults_have_official_sources_and_clear_applicability(self):
        policy = platform_policy("douyin")
        self.assertEqual(set(policy["defaults"]), {"shipping_time", "return_policy"})
        self.assertEqual(policy["checked_at"], "2026-10-20")
        self.assertTrue(policy["scope_label"])
        self.assertTrue(policy["notes"])
        self.assertIn("七天", policy["defaults"]["return_policy"])
        self.assertTrue(all(source["url"].startswith("https://school.jinritemai.com/") for source in policy["sources"]))
        self.assertTrue(RULES_PATH.is_file())

    def test_callers_cannot_mutate_registry_and_other_platforms_are_not_invented(self):
        original = platform_policy("douyin")["defaults"]["shipping_time"]
        value = platform_policy_catalog()
        value["douyin"]["defaults"]["shipping_time"] = "wrong"
        self.assertEqual(platform_policy("douyin")["defaults"]["shipping_time"], original)
        self.assertEqual(set(platform_policy_catalog()), {"douyin"})
        self.assertIsNone(platform_policy("tiktok"))
        self.assertIsNone(platform_policy("unspecified"))

    def test_schema_exposes_registry_without_changing_product_schema_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            service = PreliveService(directory)
            schema = service.schema()
            self.assertEqual(schema["platform_policies"], platform_policy_catalog())
            self.assertEqual(schema["schema_hash"], digest(schema["schema"]))
            self.assertNotIn("platform_policies", schema["schema"]["product"]["properties"])
            with service.store.transaction() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)

    def test_legacy_neutral_templates_are_explicitly_replaceable_not_arbitrary_text(self):
        policy = platform_policy("douyin")
        replaceable = policy["replaceable_defaults"]
        self.assertEqual(set(replaceable), {"shipping_time", "return_policy"})
        self.assertIn("按商品详情页及订单页显示的承诺发货。", replaceable["shipping_time"])
        self.assertNotIn("本店今天16点前订单当日发货", replaceable["shipping_time"])


if __name__ == "__main__":
    unittest.main()
