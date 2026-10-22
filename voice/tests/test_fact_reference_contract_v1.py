import json
import unittest

from app.prelive import module_generation as local, whole_script_generation as whole
from app.prelive.fact_reference_contract import request_contract
from app.prelive.storage import BusinessError


class FactReferenceContractTests(unittest.TestCase):
    def setUp(self):
        self.product = {"id": "p1", "name": "回弹训练套件", "price": 23.58,
                        "currency": "CNY", "specifications": [{"name": "颜色", "value": "红色"}],
                        "knowledge": "适合网球新手练习。"}

    def test_both_writer_requests_have_exact_dynamic_enum(self):
        requests = [local.build_request(self.product, {}, "product_intro", [], model="test"),
                    whole.build_request(self.product, {}, [{"key": "product_intro", "action": "generate"}], model="test")[0]]
        for request in requests:
            task = json.loads(request["messages"][1]["content"])
            with self.subTest(task=task["task"]):
                ids = [row["id"] for row in task["facts"]]
                self.assertEqual(task["allowed_fact_ids"], ids)
                self.assertEqual((task["response_schema"]["$defs"].get("ModuleSentence") or task["response_schema"]["$defs"]["ScriptUtterance"])["properties"]["fact_ids"]["items"]["enum"], ids)
                self.assertNotIn("SELLING", ids)
                self.assertNotIn("PROMOTION", ids)
                self.assertNotIn("target_audience", ids)

    def test_schema_is_not_mutated_and_fact_ids_follow_current_product(self):
        schema = local.ModuleOutput.model_json_schema()
        result = request_contract(schema, [{"id": "NAME"}])
        self.assertNotIn("enum", schema["$defs"]["ModuleSentence"]["properties"]["fact_ids"]["items"])
        self.assertEqual(result["allowed_fact_ids"], ["NAME"])
        task = json.loads(local.build_request({**self.product, "selling_points": "一个人也能练。"}, {}, "cta", [], model="test")["messages"][1]["content"])
        self.assertIn("SELLING", task["allowed_fact_ids"])

    def test_unknown_source_is_rejected_not_silently_remapped(self):
        raw = {"module_key": "product_intro", "sentences": [{"display_text": "适合网球新手练习。", "spoken_text": "适合网球新手练习。", "kind": "product_claim", "fact_ids": ["target_audience"]}]}
        with self.assertRaises(BusinessError) as caught:
            local.validate_output(raw, self.product, "product_intro")
        self.assertEqual(caught.exception.code, "GENERATED_MODULE_NEEDS_REVIEW")
        self.assertEqual(caught.exception.details["reference_issues"][0]["invalid_id_count"], 1)
        self.assertEqual(raw["sentences"][0]["fact_ids"], ["target_audience"])

    def test_valid_identifier_does_not_bypass_content_guard(self):
        raw = {"module_key": "product_intro", "sentences": [{"display_text": "绝对不会受伤。", "spoken_text": "绝对不会受伤。", "kind": "product_claim", "fact_ids": ["NAME"]}]}
        with self.assertRaises(BusinessError):
            local.validate_output(raw, self.product, "product_intro")


if __name__ == "__main__":
    unittest.main()
