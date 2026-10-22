import unittest
from copy import deepcopy
from unittest.mock import patch
from fastapi import HTTPException
from app import server


def item(n, product='p1', script='s1'):
    return {'id':f'sentence{n}', 'queue_id':f'q{n}', 'product_id':product, 'script_id':script,
            'text':f'第{n}句', 'spoken_text':f'第{n}句', 'ordinal':n, 'block_ordinal':1,
            'product_name':product, 'version':script, 'revision':1, 'total_sentences':3, 'source':'test'}


class FakeRoom(server.Room):
    def log(self, event, detail):
        self.state['logs'].append({'event':event,'detail':detail})
    async def broadcast(self):
        pass
    async def prepare(self, epoch, entry):
        pass


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.r=FakeRoom(); self.r.owner='owner'
        self.r.state.update(queue=[item(1),item(2),item(3,'p2','s2')],index=0)
        self.r.set_current(item(1)); self.r.state.update(status='playing', audio={'duration':3})
    def ack(self,event,**extra):
        return {'event':event,'epoch':self.r.state['epoch'],'attempt_id':self.r.state['current']['attempt_id'],**extra}
    async def test_stale_epoch(self):
        data=self.ack('ended');await self.r.action({'action':'retry'});await self.r.acknowledge('owner',data)
        self.assertEqual(self.r.state['index'],0)
    async def test_stale_attempt(self):
        data=self.ack('ended',attempt_id='old');await self.r.acknowledge('owner',data);self.assertEqual(self.r.state['index'],0)
    async def test_non_owner(self):
        await self.r.acknowledge('observer',self.ack('ended'));self.assertEqual(self.r.state['index'],0)
    async def test_ended_before_playing(self):
        self.r.state['status']='ready';await self.r.acknowledge('owner',self.ack('ended'));self.assertEqual(self.r.state['index'],0)
    async def test_pause_end_ignored(self):
        await self.r.action({'action':'pause'});await self.r.acknowledge('owner',self.ack('ended'));self.assertEqual(self.r.state['index'],0)
    async def test_real_end_advances(self):
        await self.r.acknowledge('owner',self.ack('ended'));self.assertEqual(self.r.state['current']['id'],'sentence2');self.assertEqual(len(self.r.state['played']),1)
    async def test_immediate_insert_resumes_current(self):
        await self.r.action({'action':'insert','text':'提醒','mode':'immediate'});self.assertEqual(self.r.state['current']['text'],'提醒')
        self.r.state['status']='playing';await self.r.acknowledge('owner',self.ack('ended'));self.assertEqual(self.r.state['current']['id'],'sentence1')
    async def test_after_insert_resumes_next(self):
        await self.r.action({'action':'insert','text':'提醒','mode':'after_sentence'});await self.r.acknowledge('owner',self.ack('ended'))
        self.assertEqual(self.r.state['current']['text'],'提醒');self.r.state['status']='playing';await self.r.acknowledge('owner',self.ack('ended'));self.assertEqual(self.r.state['current']['id'],'sentence2')
    async def test_after_last_restarts_the_arranged_queue(self):
        self.r.state['index']=2;self.r.set_current(item(3,'p2','s2'));self.r.state['status']='playing'
        original_order = [entry['queue_id'] for entry in self.r.state['queue']]
        await self.r.acknowledge('owner',self.ack('ended'))
        self.assertEqual(self.r.state['index'],0)
        self.assertEqual(self.r.state['current']['id'],'sentence1')
        self.assertEqual([entry['queue_id'] for entry in self.r.state['queue']],original_order)
        self.assertNotEqual(self.r.state['status'],'completed')
    async def test_duplicate_action(self):
        data={'action':'insert','text':'提醒','action_id':'one'};await self.r.action(data);epoch=self.r.state['epoch'];await self.r.action(data);self.assertEqual(epoch,self.r.state['epoch'])
    async def test_loop_after_last_insert(self):
        self.r.state.update(index=2,loop=True);self.r.set_current(item(3,'p2','s2'));self.r.state['status']='playing'
        await self.r.action({'action':'insert','text':'提醒','mode':'after_sentence'});await self.r.acknowledge('owner',self.ack('ended'));self.r.state['status']='playing';await self.r.acknowledge('owner',self.ack('ended'))
        self.assertEqual(self.r.state['current']['id'],'sentence1')
    async def test_stale_control_guard(self):
        with self.assertRaises(HTTPException):await self.r.action({'action':'next','expected_epoch':-1})
        with self.assertRaises(HTTPException):await self.r.action({'action':'next','expected_session_id':'old'})
    async def test_reorder_preserves_current(self):
        await self.r.action({'action':'reorder','queue_ids':['q3','q2']});self.assertEqual([x['queue_id'] for x in self.r.state['queue']],['q1','q3','q2']);self.assertEqual(self.r.state['current']['id'],'sentence1')
    async def test_bad_reorder_rejected(self):
        with self.assertRaises(HTTPException):await self.r.action({'action':'reorder','queue_ids':['q3','q3']})
    async def test_switch_product(self):
        await self.r.action({'action':'switch','product_id':'p2'});self.assertEqual(self.r.state['current']['id'],'sentence3')
    async def test_cancel_current(self):
        await self.r.action({'action':'cancel_product','product_id':'p1'});self.assertEqual(self.r.state['current']['product_id'],'p2');self.assertEqual(len(self.r.state['queue']),1)
    async def test_replace_pending_only(self):
        script={'id':'new','product_id':'p1','status':'approved','sentences':[item(8)],'product_snapshot':{'name':'p1'},'label':'new','revision':2,'source':'test'}
        with patch.object(server,'get',side_effect=lambda kind,id:script if kind=='script' else {'archived':False}),patch.object(server,'validate_plan'),patch.object(server.production,'require_audio'):
            await self.r.action({'action':'replace_pending','script_id':'new','target_script_id':'s1'})
        self.assertEqual(self.r.state['queue'][0]['id'],'sentence1');self.assertEqual(self.r.state['queue'][1]['id'],'sentence8');self.assertEqual(self.r.state['queue'][2]['id'],'sentence3')
    async def test_stop_invalidates(self):
        data=self.ack('ended');await self.r.action({'action':'stop'});await self.r.acknowledge('owner',data);self.assertEqual(self.r.state['status'],'stopped');self.assertIsNone(self.r.state['current'])
    async def test_edited_script_is_new_snapshot(self):
        original={'id':'s1','revision':1,'status':'approved','sentences':[item(1)],'blocks':[]}
        with patch.object(server,'get',return_value=deepcopy(original)),patch.object(server,'put',side_effect=lambda k,v:v):
            edited=await server.edit_script('s1',{'lines':['第一句。第二句。']})
        self.assertEqual(original['status'],'approved');self.assertNotEqual(edited['id'],original['id']);self.assertEqual(edited['status'],'draft');self.assertEqual(len(edited['sentences']),2)
    async def test_voice_selection_validation(self):
        with self.assertRaises(HTTPException):server.voice_options({'tts_provider':'minimax','tts_voice':'Huihui'})
        self.assertEqual(server.voice_options({'tts_provider':'minimax','tts_voice':'Wise_Woman'})['model'],'speech-2.8-turbo')
    async def test_session_freezes_voice(self):
        self.r.state['status']='stopped'
        script={'id':'s1','product_id':'p1','status':'approved','sentences':[item(1)],'product_snapshot':{'name':'p1'},'label':'test','revision':1,'source':'test'}
        with patch.object(server,'get',side_effect=lambda k,i:script if k=='script' else {'id':'p1'}),patch.object(server,'settings',return_value={'tts_provider':'system','tts_voice':'Huihui'}):
            await self.r.action({'action':'start','script_ids':['s1']})
        with patch.object(server,'settings',return_value={'tts_provider':'minimax','tts_voice':'Wise_Woman'}):
            self.assertEqual(self.r.state['tts']['provider'],'system')
    async def test_generated_text_simplified(self):
        product={'id':'p','name':'商品','price':1,'promo_price':None,'currency':'CNY','selling_points':'','promotion':'','knowledge':''}
        raw={'blocks':[{'stage':'welcome','sentences':[{'display_text':'歡迎瞭解這款涼拖鞋。','kind':'engagement','fact_ids':[]}]}]}
        result=server.structure_script(product,raw,'test','test')
        self.assertEqual(result['sentences'][0]['text'],'欢迎了解这款凉拖鞋。')
    async def test_unverified_claim_cannot_approve(self):
        with patch.object(server,'get',return_value={'sentences':[{'ordinal':1,'text':'零风险试穿。'}]}):
            with self.assertRaises(HTTPException):await server.approve('bad')


if __name__=='__main__':unittest.main()
