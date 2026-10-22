"""Role prompt changes preserve strict checks and precise cache identities.

The transport is scripted; these contracts do not prove live wording quality.
"""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

import httpx

from app.prelive import module_generation as generation
from app.prelive.storage import BusinessError, digest


def product(promotion="先领满180减20券，每单一张，可与本场价叠加，不与其他券或折扣叠加。"):
    return {"id": "role-test-product", "name": "测试日用商品", "price": 129, "currency": "CNY",
            "promo_price": None, "selling_points": "便携。", "promotion": promotion,
            "knowledge": "按实际需要选择。", "specifications": [], "sale_status": "available"}


def output(key, texts, *, facts=None):
    return {"module_key": key, "sentences": [{"display_text": text, "spoken_text": text,
        "kind": "engagement" if facts is None else "product_claim", "fact_ids": facts or []}
        for text in texts]}


class ModuleRolePromptsRC6Tests(unittest.IsolatedAsyncioTestCase):
    def test_neutral_cta_needs_no_coupon_recital_but_real_coupon_action_stays_rejected(self):
        neutral = output("cta", ["先按自己的实际需要选择，确认合适再购买。"])
        self.assertEqual(generation.validate_output(neutral, product(), "cta"),
                         ["先按自己的实际需要选择，确认合适再购买。"])
        previous_cta = output("cta", ["有需要再按规则领券下单，按需购买，不建议为凑券多买用不上的。"], facts=["PROMOTION"])
        with self.assertRaises(BusinessError) as caught:
            generation.validate_output(previous_cta, product(), "cta")
        self.assertEqual(caught.exception.code, "GENERATED_MODULE_NEEDS_REVIEW")
        self.assertTrue(any("叠加" in issue for issue in caught.exception.details["issues"]))

    def test_offer_quotes_thresholds_discounts_and_coupon_actions_remain_strict(self):
        for promotion, claim in [
            ("本场39元，不与其他优惠叠加。", "本场活动价39元。"),
            ("八折，不与其他券叠加。", "现在享受八折优惠。"),
            ("满300减60，不与其他优惠叠加。", "本场有满300减60的优惠。"),
            ("先领券，每单一张，不与其他优惠叠加。", "按规则领券下单。"),
        ]:
            with self.subTest(claim=claim), self.assertRaises(BusinessError) as caught:
                generation.validate_output(output("campaign", [claim], facts=["PROMOTION"]), product(promotion), "campaign")
            self.assertEqual(caught.exception.code, "GENERATED_MODULE_NEEDS_REVIEW")
            complete = claim[:-1] + "，不能与其他券或折扣叠加。"
            self.assertEqual(generation.validate_output(output("campaign", [complete], facts=["PROMOTION"]), product(promotion), "campaign"), [complete])

    def test_unchanged_purposes_keep_legacy_cache_identity(self):
        legacy = hashlib.sha256(generation.SYSTEM.encode()).hexdigest()[:16]
        for key in ["transition"]:
            with self.subTest(key=key):
                self.assertEqual(generation.module_prompt_revision(key), legacy)
        for key in ["opening_long", "opening_short", "product_intro", "selling_points", "campaign", "cta"]:
            with self.subTest(key=key):
                self.assertNotEqual(generation.module_prompt_revision(key), legacy)

    def test_live_purpose_edit_changes_only_its_identity_and_not_immutable_baseline(self):
        before = {key: generation.module_prompt_revision(key) for key in generation.MODULE_KEYS}
        baseline = dict(generation._LEGACY_MODULE_PURPOSE_DIGESTS)
        with patch.dict(generation._MODULE_PURPOSES, {"opening_short": generation._MODULE_PURPOSES["opening_short"] + "新的合法引入要求。"}):
            self.assertNotEqual(generation.module_prompt_revision("opening_short"), before["opening_short"])
            for key in generation.MODULE_KEYS - {"opening_short"}:
                self.assertEqual(generation.module_prompt_revision(key), before[key])
            self.assertEqual(dict(generation._LEGACY_MODULE_PURPOSE_DIGESTS), baseline)
        with self.assertRaises(TypeError):
            generation._LEGACY_MODULE_PURPOSE_DIGESTS["opening_short"] = "mutable"

    def test_real_system_edit_changes_all_module_revisions(self):
        before = {key: generation.module_prompt_revision(key) for key in generation.MODULE_KEYS}
        with patch.object(generation, "SYSTEM", generation.SYSTEM + "\n新的通用输出要求。"):
            self.assertTrue(all(generation.module_prompt_revision(key) != before[key] for key in generation.MODULE_KEYS))

    async def test_receipt_revision_and_actual_request_digest_use_current_module_prompt(self):
        requests = []
        async def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            key = json.loads(body["messages"][1]["content"])["requested_module_key"]
            return httpx.Response(200, json={"id": "offline-role-receipt", "choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(output(key, ["先按实际需要选择，合适再购买。"]), ensure_ascii=False),
                "reasoning_content": "must-not-leave-transport"}}], "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})
        context = {"module_key": "cta", "generation_instruction": "按需选择。"}
        first = await generation.generate_one_module(product(), context, "cta", [], api_key="offline-key",
                                                    transport=httpx.MockTransport(handler))
        first_revision = generation.module_prompt_revision("cta")
        with patch.dict(generation._MODULE_PURPOSES, {"cta": generation._MODULE_PURPOSES["cta"] + "语气简洁。"}):
            second = await generation.generate_one_module(product(), deepcopy(context), "cta", [], api_key="offline-key",
                                                         transport=httpx.MockTransport(handler))
            self.assertEqual(second["prompt_revision"], generation.module_prompt_revision("cta"))
            self.assertNotEqual(second["prompt_revision"], first_revision)
        self.assertEqual(len(requests), 2)
        self.assertEqual(first["prompt_revision"], first_revision)
        self.assertEqual(first["input_hash"], digest(requests[0]))
        self.assertEqual(second["input_hash"], digest(requests[1]))
        self.assertNotEqual(first["input_hash"], second["input_hash"])
        self.assertEqual(requests[0]["messages"][0], requests[1]["messages"][0])
        self.assertNotIn("must-not-leave-transport", json.dumps([first, second]))


if __name__ == "__main__":
    unittest.main()
