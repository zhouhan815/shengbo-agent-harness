"""RC2: pure promotion previews must not force a full offer in an opening."""
import unittest

from app.copywriting_quality import content_issues
from app.prelive.module_generation import validate_output


class PromotionPreviewGuardTests(unittest.TestCase):
    product = {"name": "唇膏", "selling_points": "", "price": 129, "currency": "CNY",
        "promotion": "本场99元，满180减20，先领券，每单一张，不与其他券或折扣叠加。"}

    def issues(self, text, coverage=False):
        return content_issues(self.product, [{"sentences": [{"text": text}]}], require_coverage=coverage)

    def test_clear_topic_previews_are_not_offer_assertions(self):
        for text in [
            "关于价格和优惠怎么算，放到后面的活动环节集中讲清楚，先不在这里报数。",
            "优惠怎么用、有哪些条件，稍后统一说明。",
            "折扣和优惠券的具体规则，我们后面会集中介绍。",
            "本场活动的优惠规则后面再说明，现在先看商品。",
            "想了解优惠细则的朋友，我会在后面的活动环节说明。",
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.issues(text), [])

    def test_actual_offer_claims_still_need_nonstackable_condition(self):
        for text in [
            "满180减20，详细优惠后面再讲。",
            "关于优惠后面会说明，现在先领券下单。",
            "优惠规则后面再讲，本场99元。",
            "今天给你八折优惠，后面再讲细则。",
            "现在有优惠，喜欢就选。",
            "领券就能便宜，优惠细则后面再讲。",
            "优惠券稍后再讲，这张券能和所有折扣一起叠加。",
            "先领券，每单一张。",
        ]:
            with self.subTest(text=text):
                self.assertTrue(any("叠加" in issue for issue in self.issues(text)))

    def test_other_sentences_cannot_hide_offer_behind_a_safe_preview(self):
        self.assertTrue(self.issues("优惠后面再讲。现在买两支只要178元。"))
        self.assertTrue(self.issues("本场99元。优惠规则后面再讲。"))

    def test_complete_offer_and_legacy_whole_script_contract_unchanged(self):
        self.assertEqual(self.issues("本场99元，满180减20须先领券，每单一张，不能与其他优惠叠加。"), [])
        self.assertTrue(any("叠加" in issue for issue in self.issues("优惠细节稍后介绍。", coverage=True)))

    def test_module_validation_accepts_actual_failed_preview(self):
        line = "关于价格和优惠怎么算，放到后面的活动环节集中讲清楚，先不在这里报数。"
        output = {"module_key": "opening_long", "sentences": [{"display_text": line, "spoken_text": line, "kind": "engagement", "fact_ids": []}]}
        self.assertEqual(validate_output(output, self.product, "opening_long"), [line])


if __name__ == "__main__":
    unittest.main()
