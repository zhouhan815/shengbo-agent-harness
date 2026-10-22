import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from app import server, production, reply_lab
from app import commerce_config as commerce


class CommerceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(server, 'RUNTIME', Path(self.temp.name)),
                        patch.object(server, 'rooms', {}), patch.object(production, 'TASKS', {})]
        for p in self.patches:
            p.start()
        self.p = server.put('product', server.ProductInput(name='测试商品', sku_id='SKU-01', price=149,
            selling_points='EVA材质，轻便。', shipping_time='付款后72小时发货',
            knowledge='商品默认72小时发货。', specifications=[{'name':'尺码','value':'36-37'}]).record('p1'))
        self.other = server.put('product', server.ProductInput(name='另一商品', price=30,
            selling_points='棉质').record('p2'))
        self.a = server.save_room_config('a', {'name':'A','product_ids':['p1']}, creating=True)
        self.b = server.save_room_config('b', {'name':'B','product_ids':['p1']}, creating=True)

    async def asyncTearDown(self):
        tasks = list(production.TASKS.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def config(self, coupon=None, override=None, **kwargs):
        value = commerce.RoomCommerce().model_dump()
        if coupon is not None:
            value['coupon'] = {**value['coupon'], **coupon}
        if override is not None:
            value['product_overrides']['p1'] = {**commerce.ProductOverride().model_dump(), **override}
        value.update(kwargs)
        return server.save_room_config('a', {'commerce':value})

    def test_prd_six_price_cases(self):
        coupon = {'mode':'coupon','amount':10,'minimum_spend':100,'condition':'满100减10'}
        for name, room_coupon, single, exclusive, expected in [
            ('case1',None,False,True,149), ('case2',coupon,False,True,139),
            ('case3',None,True,True,129), ('case4',None,True,False,129),
            ('case5',coupon,True,True,129), ('case6',coupon,True,False,119)]:
            with self.subTest(name=name):
                c=self.config(room_coupon, {'mode':'price','price':129,'exclusive':exclusive} if single else None)
                q=commerce.quote(self.p,c)
                self.assertEqual(q['final_price'],expected)
                self.assertTrue(q['confirmed'])

    def test_discount_decimal_rounding(self):
        c=self.config({'mode':'discount','discount':0.9}, {'mode':'discount','discount':0.85,'exclusive':False})
        self.assertEqual(commerce.quote(self.p,c)['final_price'],113.99)

    def test_threshold_and_scope(self):
        c=self.config({'mode':'coupon','amount':10,'minimum_spend':200})
        self.assertEqual(commerce.quote(self.p,c)['final_price'],149)
        server.save_room_config('a', {'product_ids':['p1','p2']})
        c=self.config({'mode':'coupon','amount':10,'scope':'selected','product_ids':['p2']})
        self.assertEqual(commerce.quote(self.p,c)['final_price'],149)

    def test_manual_or_conflicting_conditions_never_invent_final_price(self):
        for condition, threshold in [('仅限新会员',None),('满100减10',200),('满100减20',100)]:
            with self.subTest(condition=condition):
                c=self.config({'mode':'coupon','amount':10,'minimum_spend':threshold,'condition':condition})
                q=commerce.quote(self.p,c)
                self.assertFalse(q['confirmed'])
                self.assertIsNone(q['final_price'])
                facts=server.generation_facts(commerce.effective_product(self.p,c))
                self.assertIn('尚未确认',next(f['text'] for f in facts if f['id']=='PRICE'))

    def test_currency_is_not_converted_or_subtracted(self):
        c=self.config({'mode':'coupon','amount':10,'currency':'TWD'})
        self.assertEqual(commerce.quote(self.p,c)['final_price'],149)

    def test_invalid_configs_rejected(self):
        for value in [
            {'coupon':{'mode':'discount','discount':1}},
            {'coupon':{'mode':'discount','discount':0}},
            {'coupon':{'mode':'coupon','amount':-1}},
            {'coupon':{'mode':'discount','discount':float('nan')}},
            {'coupon':{'mode':'coupon','amount':10,'scope':'selected','product_ids':[]}},
            {'product_overrides':{'p1':{'mode':'price','price':150}}},
            {'product_overrides':{'p1':{'shipping_override':True,'shipping_time':''}}},
            {'product_overrides':{'p2':{'mode':'price','price':10}}},
            {'product_overrides':None}, {'product_overrides':{'p1':{'unknown':True}}},
            {'knowledge_documents':[{'id':'x','title':'','content':'x'}]}, {'unknown':'x'},
        ]:
            with self.subTest(value=value):
                with self.assertRaises(HTTPException):
                    server.save_room_config('a',{'commerce':value})

    def test_room_isolation_reference_and_persistence(self):
        self.config(override={'mode':'price','price':129,'gifts':'送收纳袋1个','shipping_override':True,'shipping_time':'12小时发货'},scene='周年庆')
        server.rooms.clear()
        a,b=server.settings('a'),server.settings('b')
        self.assertEqual(commerce.quote(self.p,a)['final_price'],129)
        self.assertEqual(commerce.quote(self.p,b)['final_price'],149)
        self.assertNotIn('name',a['commerce']['product_overrides']['p1'])
        self.assertEqual(server.get('product','p1')['price'],149)
        self.assertEqual(server.get('product','p1')['shipping_time'],'付款后72小时发货')

    def test_unrelated_room_save_preserves_business_fields(self):
        old=self.config(override={'mode':'price','price':129},scene='开学专场')
        new=server.save_room_config('a',{'name':'新名称','tts_provider':'system','tts_voice':'Huihui'})
        self.assertEqual(new['commerce'],old['commerce'])
        new=server.save_room_config('a',{'commerce':{'scene':'暑期专场'}})
        self.assertEqual(new['commerce']['product_overrides'],old['commerce']['product_overrides'])

    def test_removing_reference_cleans_only_its_overrides(self):
        self.config({'mode':'coupon','amount':10,'scope':'selected','product_ids':['p1']}, {'mode':'price','price':129})
        c=server.save_room_config('a',{'product_ids':[]})
        self.assertEqual(c['commerce']['product_overrides'],{})
        self.assertEqual(c['commerce']['coupon']['mode'],'none')
        self.assertEqual(server.get('product','p1')['price'],149)

    async def test_product_fields_round_trip_and_legacy_client_preserves_them(self):
        body=server.ProductInput(name='  结构化商品  ',price=20,sku_id='NEW',sale_status='available',
            core_selling_points=['  卖点一  ','卖点二'], material='棉',ingredients='100%棉',
            specifications=[{'name':'颜色','value':'蓝'}],target_audience='成人',
            storage_conditions='阴凉干燥处',shelf_life_value=2,shelf_life_unit='year',
            shipping_origin='杭州',shipping_time='48小时发货',return_policy='未拆封可联系售后',
            usage_instructions='冷水手洗',faq='问：什么颜色？\n答：蓝色。')
        p=await server.add_product(body)
        self.assertEqual(p['name'],'结构化商品')
        self.assertEqual(p['selling_points'],'卖点一\n卖点二')
        revised=await server.update_product(p['id'],server.ProductInput(name='新名称',price=21))
        for k in body.model_fields_set-{'name','price'}:
            self.assertEqual(revised[k],p[k])
        self.assertEqual(revised['sku_id'],'NEW')

    async def test_product_required_fields_and_unique_sku(self):
        with self.assertRaises(HTTPException):
            await server.add_product(server.ProductInput(name='没有卖点',price=1))
        with self.assertRaises(HTTPException):
            await server.add_product(server.ProductInput(name='重复SKU',price=1,sku_id='SKU-01',selling_points='卖点'))
        for body in [{'name':' '*5,'price':1},{'name':'a'*61,'price':1},{'name':'x','price':1,'sale_status':'maybe'}]:
            with self.assertRaises(ValidationError):
                server.ProductInput(**body)

    async def test_unavailable_product_cannot_be_added_generated_recommended_or_played(self):
        p=deepcopy(self.p);p['sale_status']='unavailable';server.put('product',p)
        with self.assertRaises(HTTPException):
            server.save_room_config('new',{'name':'新房间','product_ids':['p1']},creating=True)
        with self.assertRaises(HTTPException):
            await production.create_job('a',{'kind':'scripts'})
        self.assertEqual(reply_lab.room_products('a')[1],[])
        script=self.script(self.p);server.put('script',script)
        with self.assertRaises(HTTPException):
            server.validate_plan(server.settings('a'),[script['id']])

    def script(self, product):
        return server.structure_script(product, {'blocks':[{'stage':'identity','sentences':[
            {'display_text':'这是测试商品。','fact_ids':['NAME']}]}]},'test','1',server.generation_facts(product))

    def test_new_scripts_snapshot_effective_settings_and_invalidate_on_change(self):
        c=self.config(override={'mode':'price','price':129},scene='轻松介绍')
        script=self.script(commerce.effective_product(self.p,c));server.put('script',script)
        production.validate_scripts(c,[script['id']])
        c=self.config(override={'mode':'price','price':119},scene='轻松介绍')
        with self.assertRaises(HTTPException):
            production.validate_scripts(c,[script['id']])

    async def test_next_audio_read_stops_after_business_facts_change(self):
        c=self.config(override={'mode':'price','price':129})
        script=self.script(commerce.effective_product(self.p,c));server.put('script',script)
        room=server.Room('a')
        self.config(override={'mode':'price','price':119})
        item={**script['sentences'][0],'product_id':'p1','script_id':script['id']}
        with patch.object(production,'prerecorded') as audio, patch.object(room,'broadcast',new=AsyncMock()):
            await room.prepare(room.state['epoch'],item)
        audio.assert_not_called()
        self.assertEqual(room.state['status'],'error')
        self.assertIn('停止继续播放旧话术',room.state['error'])

    async def test_generation_reads_effective_product_not_a_global_copy(self):
        self.config(override={'mode':'price','price':129},scene='周年庆')
        seen=[]
        def generate(p,count,prior):
            seen.append(deepcopy(p))
            return [self.script(p)]
        with patch.object(server,'generate_remote',side_effect=generate):
            job=await production.create_job('a',{'kind':'scripts','version_count':1})
            await production.TASKS[job['id']]
        self.assertEqual(server.get('job',job['id'])['status'],'completed')
        self.assertEqual(seen[0]['promo_price'],129)
        self.assertEqual(seen[0]['_room_scene'],'周年庆')
        self.assertIsNone(server.get('product','p1')['promo_price'])

    def test_rag_shipping_override_and_fallback_and_room_documents(self):
        a=self.config(override={'shipping_override':True,'shipping_time':'本场12小时发货'},
            knowledge_documents=[{'id':'r1','title':'A专属活动','content':'本场主题为清凉夏日。'}])
        a_result=reply_lab.retrieve([self.p],'什么时候发货？',config=a)
        text='\n'.join(c['text'] for c in a_result['chunks'])
        self.assertIn('12小时',text)
        self.assertNotIn('72小时',text)
        self.assertTrue(any(c.get('source_level')=='P1' for c in a_result['chunks']))
        b_result=reply_lab.retrieve([self.p],'什么时候发货？',config=server.settings('b'))
        self.assertIn('72小时','\n'.join(c['text'] for c in b_result['chunks']))
        self.assertNotIn('清凉夏日',str(reply_lab.knowledge_view('b')))
        cleared=self.config(override={'shipping_override':False})
        self.assertIn('72小时',str(reply_lab.retrieve([self.p],'发货',config=cleared)))
        # The shared persisted index still contains product defaults, not A's override.
        self.assertNotIn('12小时',str(reply_lab.read_index([self.p])))

    def test_rag_price_uses_backend_quote(self):
        c=self.config({'mode':'coupon','amount':10,'minimum_spend':100}, {'mode':'price','price':129,'exclusive':False})
        result=reply_lab.retrieve([self.p],'现在到手多少钱？',config=c)
        price=next(x for x in result['chunks'] if x['kind']=='price')
        self.assertIn('119.0',price['text'])
        self.assertEqual(price['scope'],'room')

    async def test_price_preview_does_not_save_unsaved_configuration(self):
        value=commerce.RoomCommerce(product_overrides={'p1':{'mode':'price','price':129}}).model_dump()
        q=await server.price_preview('a',{'product_id':'p1','commerce':value})
        self.assertEqual(q['final_price'],129)
        self.assertEqual(server.settings('a')['commerce']['product_overrides'],{})
        with self.assertRaises(HTTPException):
            await server.price_preview('a',{'product_id':'p2'})

    def test_legacy_room_migrates_offer_without_changing_product(self):
        product=deepcopy(self.p);product.update(promo_price=107,promotion='活动价107元');server.put('product',product)
        server.put('config',{'id':'old','name':'旧直播间','product_ids':['p1']})
        result=server.settings('old')
        self.assertEqual(commerce.quote(product,result)['final_price'],107)
        self.assertEqual(result,server.settings('old'))
        self.assertEqual(server.get('product','p1')['promo_price'],107)

    def test_lowering_global_price_requires_fixing_invalid_room_offer(self):
        c=self.config(override={'mode':'price','price':129})
        changed={**self.p,'price':99}
        with self.assertRaises(HTTPException):
            commerce.quote(changed,c)

    async def test_removing_optional_product_fields_clears_persisted_facts_and_rag(self):
        filled=await server.update_product('p1',server.ProductInput(
            name=self.p['name'],price=149,material='可移除材质标记',
            storage_conditions='可移除储存标记',shelf_life_value=2,shelf_life_unit='year',
            specifications=[{'name':'可移除规格','value':'蓝色'}],knowledge='可移除知识标记',
            knowledge_documents=[{'id':'optional-doc','title':'可移除文档','content':'可移除文档内容'}]))
        before_docs,_=reply_lab.read_index([filled])
        self.assertIn('可移除文档内容',str(before_docs))
        hidden=['material','storage_conditions','shelf_life','specifications','knowledge','knowledge_documents']
        await server.update_product('p1',server.ProductInput(
            name=self.p['name'],price=149,hidden_optional_fields=hidden))

        # Reload from SQLite rather than retaining the response returned to the editor.
        reopened=server.get('product','p1')
        self.assertEqual(reopened['hidden_optional_fields'],hidden)
        for field in ['material','storage_conditions','knowledge']:
            self.assertEqual(reopened[field],'')
        for field in ['specifications','knowledge_documents']:
            self.assertEqual(reopened[field],[])
        self.assertIsNone(reopened['shelf_life_value'])
        self.assertEqual(reopened['shelf_life_unit'],'month')
        self.assertEqual(reopened['selling_points'],self.p['selling_points'])
        after_docs,after_chunks=reply_lab.read_index([reopened])
        for marker in ['可移除材质标记','可移除储存标记','可移除规格','可移除知识标记','可移除文档内容']:
            self.assertNotIn(marker,str(after_docs)+str(after_chunks))

    async def test_restoring_optional_product_fields_saves_new_values(self):
        hidden=['material','storage_conditions','shelf_life','specifications','knowledge','knowledge_documents']
        await server.update_product('p1',server.ProductInput(
            name=self.p['name'],price=149,hidden_optional_fields=hidden))
        await server.update_product('p1',server.ProductInput(
            name=self.p['name'],price=149,hidden_optional_fields=[],material='新材质',
            storage_conditions='冷藏',shelf_life_value=7,shelf_life_unit='day',
            specifications=[{'name':'容量','value':'200毫升'}],knowledge='新知识',
            knowledge_documents=[{'id':'new-doc','title':'新文档','content':'新文档内容'}]))
        reopened=server.get('product','p1')
        self.assertEqual(reopened['hidden_optional_fields'],[])
        for field,expected in [('material','新材质'),('storage_conditions','冷藏'),
                ('shelf_life_value',7),('shelf_life_unit','day'),('knowledge','新知识')]:
            self.assertEqual(reopened[field],expected)
        self.assertEqual(reopened['specifications'],[{'name':'容量','value':'200毫升'}])
        self.assertEqual(reopened['knowledge_documents'],[{'id':'new-doc','title':'新文档','content':'新文档内容'}])
        docs,_=reply_lab.read_index([reopened])
        self.assertIn('新文档内容',str(docs))

    async def test_legacy_product_updates_preserve_removed_optional_fields(self):
        await server.update_product('p1',server.ProductInput(
            name=self.p['name'],price=149,hidden_optional_fields=['material','knowledge']))
        await server.update_product('p1',server.ProductInput(
            name='旧客户端改名称',price=148,material='旧客户端残留材质',knowledge='旧客户端残留知识'))
        reopened=server.get('product','p1')
        self.assertEqual(reopened['name'],'旧客户端改名称')
        self.assertEqual(reopened['price'],148)
        self.assertEqual(reopened['hidden_optional_fields'],['material','knowledge'])
        self.assertEqual(reopened['material'],'')
        self.assertEqual(reopened['knowledge'],'')

    def test_removing_room_sections_clears_rules_and_preserves_other_room(self):
        active=self.config({'mode':'coupon','amount':10,'minimum_spend':100},
            scene='可移除场景',promotion='可移除活动说明',
            knowledge_documents=[{'id':'room-doc','title':'本场活动','content':'活动专属暗号甲'}])
        other=server.save_room_config('b',{'commerce':deepcopy(active['commerce'])})
        self.assertEqual(commerce.quote(self.p,active)['final_price'],139)
        self.assertIn('活动专属暗号甲',str(reply_lab.knowledge_view('a')['documents']))
        hidden=['coupon','promotion','scene','knowledge_documents']
        server.save_room_config('a',{'commerce':{'hidden_sections':hidden}})

        server.rooms.clear()
        reopened=server.settings('a')
        self.assertEqual(reopened['commerce']['hidden_sections'],hidden)
        self.assertEqual(reopened['commerce']['coupon']['mode'],'none')
        self.assertEqual(reopened['commerce']['promotion'],'')
        self.assertEqual(reopened['commerce']['promotion_scope'],'all')
        self.assertEqual(reopened['commerce']['promotion_product_ids'],[])
        self.assertEqual(reopened['commerce']['scene'],'')
        self.assertEqual(reopened['commerce']['knowledge_documents'],[])
        self.assertEqual(commerce.quote(self.p,reopened)['final_price'],149)
        effective=commerce.effective_product(self.p,reopened)
        self.assertEqual(effective['_room_scene'],'')
        self.assertEqual(effective['promotion'],'')
        self.assertNotIn('活动专属暗号甲',str(reply_lab.knowledge_view('a')['documents']))
        self.assertEqual(server.settings('b')['commerce'],other['commerce'])
        self.assertEqual(commerce.quote(self.p,server.settings('b'))['final_price'],139)
        self.assertIn('活动专属暗号甲',str(reply_lab.knowledge_view('b')['documents']))

    def test_restoring_room_sections_accepts_new_configuration(self):
        self.config(hidden_sections=['coupon','promotion','scene','knowledge_documents'])
        restored=server.save_room_config('a',{'commerce':{
            'hidden_sections':[], 'scene':'新场景','promotion':'新促销说明',
            'coupon':{'mode':'coupon','amount':20,'minimum_spend':100},
            'knowledge_documents':[{'id':'restored-doc','title':'新活动规则','content':'恢复后活动内容'}]}})
        reopened=server.settings('a')
        self.assertEqual(reopened['commerce'],restored['commerce'])
        self.assertEqual(reopened['commerce']['hidden_sections'],[])
        self.assertEqual(commerce.quote(self.p,reopened)['final_price'],129)
        self.assertEqual(commerce.effective_product(self.p,reopened)['_room_scene'],'新场景')
        self.assertIn('新促销说明',commerce.effective_product(self.p,reopened)['promotion'])
        self.assertIn('恢复后活动内容',str(reply_lab.knowledge_view('a')['documents']))
        self.assertEqual(commerce.quote(self.p,server.settings('b'))['final_price'],149)

    def test_legacy_room_update_preserves_removed_sections(self):
        self.config(hidden_sections=['coupon','scene'])
        server.save_room_config('a',{'name':'改房间名','tts_provider':'system','tts_voice':'Huihui'})
        reopened=server.settings('a')
        self.assertEqual(reopened['commerce']['hidden_sections'],['coupon','scene'])
        self.assertEqual(reopened['commerce']['coupon']['mode'],'none')

    def test_unknown_optional_fields_and_room_sections_are_rejected(self):
        with self.assertRaises(ValidationError):
            server.ProductInput(name='商品',price=1,hidden_optional_fields=['price'])
        before=server.settings('a')
        with self.assertRaises(HTTPException) as error:
            server.save_room_config('a',{'commerce':{'hidden_sections':['product_overrides']}})
        self.assertEqual(error.exception.status_code,400)
        self.assertEqual(server.settings('a'),before)
