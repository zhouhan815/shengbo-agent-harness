"""The Valentine case keeps a known single price without inventing coupon use."""
from copy import deepcopy
import json
import unittest

from app import commerce_config as commerce
from app.prelive.module_generation import build_request


class CouponSingleUnitPriceTests(unittest.TestCase):
    def setUp(self):
        self.product = {'id': 'valentine-lipstick', 'name': '绯笺柔雾唇膏 02玫瑰豆沙色',
            'sku_id': 'VAL-ROSE02-32G', 'price': 129, 'currency': 'CNY', 'promo_price': None,
            'promotion': '', 'selling_points': '柔雾妆效，支持薄涂与叠涂。', 'knowledge': ''}
        self.condition = ('仅本场选中这款唇膏；先领券，每单限一张；可与本场99元/支单品价叠加，'
            '不与其他券或折扣叠加；优惠截止2027-02-14 23:59 Asia/Shanghai。')

    def config(self, condition, *, threshold=180, amount=20, price=99, product=None):
        product = product or self.product
        return {'commerce': commerce.validate_room({
            'coupon': {'mode': 'coupon', 'amount': amount, 'minimum_spend': threshold,
                'currency': 'CNY', 'condition': condition, 'scope': 'selected',
                'product_ids': [product['id']]},
            'product_overrides': {product['id']: {'mode': 'price', 'price': price, 'exclusive': False,
                'promotion': '本场99元/支；满180减20须先领券，每单一张，不与其他券或折扣叠加。'}},
        }, {product['id']: product})}

    def test_valentine_single_unit_is_confirmed_99_and_retains_all_coupon_conditions(self):
        for condition in (self.condition, '满180减20；先领券，每单一张，不与其他券或折扣叠加。'):
            with self.subTest(condition=condition):
                config = self.config(condition)
                original = deepcopy(config)
                quote = commerce.quote(self.product, config)
                self.assertTrue(quote['confirmed'])
                self.assertEqual((quote['quantity'], quote['base_price'], quote['final_price']), (1, 99, 99))
                self.assertEqual([s['source'] for s in quote['steps']], ['product', 'room_product'])
                self.assertIn('单件未达到', quote['reason'])
                effective = commerce.effective_product(self.product, config)
                self.assertEqual((effective['price'], effective['promo_price']), (129, 99))
                self.assertIn(condition, effective['promotion'])
                self.assertEqual(config, original)
                self.assertIsNone(self.product['promo_price'])

                # Inspect the exact payload builder used by the actual adapter,
                # without invoking a provider or loading any credentials.
                request = build_request(effective, {'module_key': 'campaign'}, 'campaign', [], model='deepseek-flash')
                task = json.loads(request['messages'][1]['content'])
                price = json.loads(next(f['text'] for f in task['facts'] if f['id'] == 'PRICE'))
                self.assertEqual(price['promo_price'], 99)
                self.assertTrue(price['_price_quote']['confirmed'])
                self.assertIn(condition, task['effective_product_snapshot']['promotion'])

    def test_textual_threshold_and_amount_conflicts_still_require_confirmation_below_both_thresholds(self):
        for condition in ('满100减20', '满100减20；先领券，每单一张。',
                          '满180减30；先领券，每单一张。', '无门槛，先领券，每单一张。'):
            with self.subTest(condition=condition):
                quote = commerce.quote(self.product, self.config(condition))
                self.assertFalse(quote['confirmed'])
                self.assertIsNone(quote['final_price'])
                self.assertIn('冲突', quote['reason'])

    def test_eligible_single_unit_does_not_assume_coupon_claim_or_apply_reduction(self):
        product = {**self.product, 'price': 299}
        for price in (180, 198):
            with self.subTest(price=price):
                quote = commerce.quote(product, self.config(self.condition, price=price, product=product))
                self.assertEqual(quote['base_price'], price)
                self.assertFalse(quote['confirmed'])
                self.assertIsNone(quote['final_price'])
                self.assertNotIn('room_coupon', [s['source'] for s in quote['steps']])

    def test_manual_condition_without_numeric_spend_gate_remains_unknown(self):
        quote = commerce.quote(self.product, self.config('仅限新会员，先领券。', threshold=None))
        self.assertFalse(quote['confirmed'])
        self.assertIsNone(quote['final_price'])


if __name__ == '__main__':
    unittest.main()
