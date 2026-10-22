import asyncio
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import production, server
from tools.script_prompt_lab import metrics
from app.copywriting_quality import content_issues


class CopywritingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(server, 'RUNTIME', Path(self.temp.name)), patch.object(production, 'TASKS', {})]
        for p in self.patches: p.start()
        self.product = server.put('product', server.ProductInput(name='测试杯', price=49, selling_points='陶瓷材质，350毫升。', knowledge='蓝鹭专享九块九包邮').record('p'))
        self.room_a = await server.create_room({'name': 'A', 'product_ids': ['p'], 'plan_mode': 'all'})
        self.room_b = await server.create_room({'name': 'B', 'product_ids': ['p'], 'plan_mode': 'all'})

    async def asyncTearDown(self):
        tasks = list(production.TASKS.values())
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def script(self, product=None):
        return server.structure_script(product or self.product, {'blocks': [{'stage': 'benefit', 'sentences': [
            {'display_text': '这是陶瓷材质的日常杯子。', 'fact_ids': ['SELLING']}]}]}, 'mock', '1')

    async def test_generation_excludes_knowledge_but_legacy_facts_remain(self):
        self.assertTrue(any(f['id'].startswith('KNOWLEDGE_') for f in server.product_facts(self.product)))
        self.assertFalse(any(f['id'].startswith('KNOWLEDGE_') for f in server.generation_facts(self.product)))
        value = json.dumps(server.generation_facts(self.product), ensure_ascii=False)
        self.assertNotIn('蓝鹭', value)
        self.assertNotIn('None', value)
        self.assertIn('没有另设活动价', value)

    async def test_real_request_contract_excludes_knowledge_and_keeps_prompt_provenance(self):
        raw = {'versions': [{'blocks': [{'stage': 'benefit', 'sentences': [{'display_text': '陶瓷材质。', 'fact_ids': ['SELLING']}]}]}]}
        response = json.dumps({'choices': [{'message': {'content': json.dumps(raw)}}]}).encode()
        with patch.dict(server.os.environ, {'DEEPSEEK_API_KEY': 'unit-test-only'}), patch.object(server.urllib.request, 'urlopen', return_value=io.BytesIO(response)) as request:
            result = server.generate_remote(self.product, 1)
        payload = json.loads(request.call_args.args[0].data)
        data = json.loads(payload['messages'][1]['content'])
        self.assertNotIn('蓝鹭', payload['messages'][1]['content'])
        self.assertEqual(data['must_mention_each_version'], ['陶瓷材质'])
        self.assertEqual({f['id'] for f in data['facts']}, {'NAME', 'PRICE', 'SELLING', 'PROMOTION'})
        self.assertEqual(len(result[0]['prompt_revision']), 12)
        self.assertEqual({f['id'] for f in result[0]['facts']}, {'NAME', 'PRICE', 'SELLING', 'PROMOTION'})

    async def test_prior_context_does_not_feed_previous_full_prose_back_to_model(self):
        raw = {'versions':[{'blocks':[{'sentences':[{'display_text':'陶瓷杯。','fact_ids':['NAME','SELLING']}]}]}]}
        response=json.dumps({'choices':[{'message':{'content':json.dumps(raw)}}]}).encode()
        with patch.dict(server.os.environ, {'DEEPSEEK_API_KEY':'unit-test-only'}), patch.object(server.urllib.request,'urlopen',return_value=io.BytesIO(response)) as request:
            server.generate_remote(self.product,1,[{'opening':['旧开场'], 'outline':['材料讲解摘要'], 'sentences_to_avoid':['这是一整段不应该再次作为全文参考输入的旧话术。']}])
        value=json.loads(request.call_args.args[0].data)['messages'][1]['content']
        self.assertNotIn('这是一整段',value)
        self.assertIn('材料讲解摘要',value)

    async def test_qna_update_does_not_invalidate_new_script_but_price_update_does(self):
        script=self.script(); script['facts']=server.generation_facts(self.product); script['status']='approved'
        server.put('script',script)
        config=server.save_room_config(self.room_a['id'],{'script_order':[script['id']]})
        changed=deepcopy(self.product); changed['knowledge']='新问答资料';server.put('product',changed)
        production.validate_scripts(config,[script['id']])
        changed['price']=59;server.put('product',changed)
        with self.assertRaises(server.HTTPException): production.validate_scripts(config,[script['id']])

    async def test_legacy_script_using_knowledge_still_invalidates_on_qna_update(self):
        script=self.script();server.put('script',script)
        config=server.save_room_config(self.room_a['id'],{'script_order':[script['id']]})
        changed=deepcopy(self.product);changed['knowledge']='新知识';server.put('product',changed)
        with self.assertRaises(server.HTTPException): production.validate_scripts(config,[script['id']])

    async def test_room_versions_do_not_import_other_room_prior_scripts(self):
        foreign = self.script(); foreign.update(id='foreign', room_id=self.room_b['id'], version_number=99)
        server.put('script', foreign)
        seen = []
        def generate(product, count, prior=None):
            seen.append(deepcopy(prior)); return [self.script(product)]
        with patch.object(server, 'generate_remote', side_effect=generate):
            job = await production.create_job(self.room_a['id'], {'kind': 'scripts', 'version_count': 2})
            await production.TASKS[job['id']]
        result = server.get('job', job['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(seen[0], [])
        self.assertEqual(len(seen[1]), 1)
        self.assertIn('sentences_to_avoid', seen[1][0])
        self.assertEqual([i['version_number'] for i in result['items']], [1, 2])
        self.assertTrue(all(server.get('script', sid)['room_id'] == self.room_a['id'] for sid in result['result_script_ids']))
        self.assertEqual(len(production.room_scripts(self.room_b['id'])), 1)

    async def test_standalone_generation_requires_room_and_attaches_to_that_room(self):
        with self.assertRaises(server.HTTPException): await server.generate('p', {'version_count': 1})
        with patch.object(server, 'generate_remote', return_value=[self.script()]):
            result = await server.generate('p', {'version_count': 1, 'room_id': self.room_a['id']})
        self.assertEqual(result['scripts'][0]['room_id'], self.room_a['id'])
        self.assertEqual(production.plan_ids(server.settings(self.room_a['id'])), [result['scripts'][0]['id']])

    async def test_similar_candidate_rewrites_once_and_marks_actual_attempt(self):
        old=self.script();old['sentences'][0]['text']='晚上在家收拾桌面的时候，你可以按自己习惯拿起这只陶瓷杯子喝水。'
        new=deepcopy(old);new['sentences'][0]['text']='早上坐到办公室的座位旁，先看看今天想给自己的桌面放一只什么颜色的陶瓷杯子。'
        with patch.object(server,'_generate_remote_once',side_effect=[[old],[new]]) as generate:
            result=server.generate_remote(self.product,1,[{'sentences_to_avoid':[old['sentences'][0]['text']]}])
        self.assertEqual(generate.call_count,2)
        self.assertEqual(result[0]['quality_rewrite_attempts'],1)

    async def test_retry_still_similar_is_rejected_and_never_stored(self):
        old=self.script();old['sentences'][0]['text']='晚上在家收拾桌面的时候，你可以按自己习惯拿起这只陶瓷杯子喝水。'
        before=len(server.all_of('script'))
        with patch.object(server,'_generate_remote_once',return_value=[old]) as generate:
            with self.assertRaises(RuntimeError): server.generate_remote(self.product,1,[{'sentences_to_avoid':[old['sentences'][0]['text']]}])
        self.assertEqual(generate.call_count,2)
        self.assertEqual(len(server.all_of('script')),before)

    async def test_missing_explicit_material_triggers_bounded_rewrite(self):
        bad=self.script();bad['sentences'][0]['text']='欢迎来到直播间。'
        with patch.object(server,'_generate_remote_once',side_effect=[[bad],[self.script()]]) as generate:
            result=server.generate_remote(self.product,1)
        self.assertEqual(generate.call_count,2)
        self.assertEqual(result[0]['quality_rewrite_attempts'],1)


class EvaluationTests(unittest.TestCase):
    def test_original_traditional_features_are_checked_as_simplified(self):
        product={'selling_points':'靜音、輕便、快乾。','promotion':'限時優惠。'}
        self.assertTrue(content_issues(product,[{'sentences':[{'text':'这款商品静音。'}]}]))
        self.assertEqual(content_issues(product,[{'sentences':[{'text':'静音、轻便、快干，限时优惠。'}]}]),[])

    def test_unprovided_comparison_is_rejected(self):
        product={'selling_points':'鞋底有防滑特性。'}
        self.assertTrue(content_issues(product,[{'sentences':[{'text':'比普通平底多一层抓地的感觉。'}]}]))
        self.assertEqual(content_issues(product,[{'sentences':[{'text':'鞋底有防滑特性，湿地面仍需慢慢走。'}]}]),[])

    def test_unknown_heat_performance_is_not_a_positive_fact(self):
        product={'selling_points':'陶瓷，带把手。未提供保温和耐温资料。'}
        self.assertTrue(content_issues(product,[{'sentences':[{'text':'陶瓷杯壁不烫手。'}]}]))
        self.assertEqual(content_issues(product,[{'sentences':[{'text':'陶瓷杯子有把手，容量三百五十毫升。'}]}]),[])

    def test_one_piece_does_not_imply_anti_chafing_or_live_checkout(self):
        product={'selling_points':'一体成型。'}
        issues=content_issues(product,[{'sentences':[{'text':'一体成型，穿起来不磨脚，直播间就能拍。'}]}])
        self.assertEqual(len(issues),2)

    def test_unrelated_example_and_unprovided_time_limit_are_rejected(self):
        product={'name':'托特包','selling_points':'涤纶，购物通勤','promotion':'活动价79元'}
        script={'sentences':[{'text':'限时活动，浴室走动还是要留意地面。'}]}
        issues=content_issues(product,[script])
        self.assertTrue(any('浴室' in issue for issue in issues))
        self.assertTrue(any('限时' in issue for issue in issues))

    def test_explicit_non_stackable_promotion_cannot_be_dropped(self):
        product={'selling_points':'', 'promotion':'买3件9折，不与其他优惠叠加。'}
        script={'sentences':[{'text':'买三件九折。'}]}
        self.assertTrue(content_issues(product,[script]))
        script['sentences'][0]['text']='买三件九折，不能与其他优惠叠加。'
        self.assertEqual(content_issues(product,[script]),[])

    def test_legacy_stage_names_receive_same_flow_score(self):
        def sample(stages):
            return {'blocks': [{'stage': stage} for stage in stages], 'facts': [], 'issues': [], 'sentences': [
                {'text': '欢迎来到直播间看一看。', 'stage': 'welcome', 'kind': 'engagement', 'fact_ids': []}]}
        a = metrics([sample(['welcome', 'identity', 'benefit', 'promotion', 'cta'])], {'a': ['欢迎']}, [])
        b = metrics([sample(['welcome', 'product_identity', 'selling_point', 'promotion', 'purchase_guide'])], {'a': ['欢迎']}, [])
        self.assertEqual(a['versions'][0]['program_scores']['flow'], b['versions'][0]['program_scores']['flow'])

    def test_cross_version_copy_metric_exempts_price_but_catches_shared_scenario(self):
        sentences = [
            {'text': '如果你晚上起来喝水，家里有人已经休息，这时候脚步轻一些会少些打扰。', 'stage': 'scene', 'kind': 'engagement', 'fact_ids': []},
            {'text': '这款商品的准确活动价格是一百零七新台币，请按需要选择。', 'stage': 'promotion', 'kind': 'engagement', 'fact_ids': []}]
        s = {'blocks': [{'stage': 'scene'}], 'facts': [], 'issues': [], 'sentences': sentences}
        result = metrics([s, deepcopy(s)], {'a': ['喝水']}, [])
        self.assertEqual(result['pair'][0]['shared_narrative_sentences'], [sentences[0]['text']])


if __name__ == '__main__': unittest.main()
