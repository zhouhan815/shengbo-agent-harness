import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from app import server, reply_lab


def grounded_reply(context):
    chunk = next(c for c in context['evidence'] if c['kind'] != 'identity')
    return {'answer':'主播根据本轮资料作答。', 'status':'answered',
            'citations':[{'chunk_id':chunk['id'], 'quote':chunk['text']}], 'missing_information':[]}, {'model':'mock', 'provider':'mock'}


class ReplyLabTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(server, 'RUNTIME', Path(self.temp.name)), patch.object(server, 'rooms', {}),
                        patch.object(reply_lab, 'TASKS', {}), patch.object(reply_lab, 'complete_remote', grounded_reply)]
        for p in self.patches: p.start()
        self.p1 = server.put('product', server.ProductInput(name='足弓凉拖鞋', price=178, promo_price=107, currency='TWD',
             selling_points='材质：EVA。\n颜色：粉色、黑色。\n尺码：36-37、38-39、40-41。',
             knowledge='浴室地面遇水仍须小心行走，不要奔跑。\n标准尺码，脚背宽或脚型偏肉可斟酌大一号。',
             knowledge_documents=[server.KnowledgeDocumentInput(id='shipping',title='物流政策',content='备货2至8个工作日寄出。具体配送日期待确认。'),
                server.KnowledgeDocumentInput(id='returns',title='售后政策',content='质量问题请先提供照片联系客服核实。')]).record('p1'))
        self.p2 = server.put('product', server.ProductInput(name='不锈钢保温杯',price=58,knowledge='保温杯质保两年。').record('p2'))
        self.config = server.save_room_config('room1', {'name':'鞋子直播间','product_ids':['p1']},creating=True)
        self.other = server.save_room_config('room2', {'name':'杯子直播间','product_ids':['p2']},creating=True)

    async def asyncTearDown(self):
        for t in list(reply_lab.TASKS.values()):
            if not t.done(): t.cancel()
        if reply_lab.TASKS: await asyncio.gather(*reply_lab.TASKS.values(),return_exceptions=True)
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    async def turn(self, text='多少钱？', product='p1', session=None, request_id='request-12345'):
        session = session or reply_lab.create_session('room1')
        t = await reply_lab.send_comment(session['id'],{'text':text,'product_id':product,'request_id':request_id})
        task = reply_lab.TASKS.get(t['id'])
        if task: await task
        return server.get('reply_turn',t['id'])

    def test_actual_chunk_retrieval_and_document_titles(self):
        r = reply_lab.retrieve([self.p1],'多久发货？')
        self.assertIn('备货2至8个工作日',r['chunks'][0]['text'])
        self.assertIn('物流政策',r['chunks'][0]['title'])
        self.assertEqual(r['chunks'][0]['document_id'],'p1:doc_shipping')
        self.assertGreater(r['chunks'][0]['score'],0)

    def test_chinese_synonyms_and_size_retrieval(self):
        r = reply_lab.retrieve([self.p1],'我是宽脚，选几码？')
        self.assertTrue(any('大一号' in c['text'] for c in r['chunks']))

    def test_price_currency_remain_factual(self):
        r = reply_lab.retrieve([self.p1],'多少钱？')
        self.assertTrue(any('107 TWD' in c['text'] for c in r['chunks']))

    def test_empty_knowledge_and_unknown_question_have_no_fake_facts(self):
        r = reply_lab.retrieve([self.p1],'保修八十年是真的吗？')
        self.assertFalse(any(c['product_id'] == 'p2' or '八十年' in c['text'] for c in r['chunks']))

    def test_index_refreshes_after_edit_and_removes_deleted_doc(self):
        reply_lab.knowledge_view('room1')
        p = deepcopy(self.p1); p['knowledge_documents'] = []; p['knowledge']='发货需要十个工作日。'; server.put('product',p)
        r = reply_lab.knowledge_view('room1')
        self.assertFalse(any(d['id']=='p1:doc_shipping' for d in r['documents']))
        retrieved = reply_lab.retrieve([p],'多久发货？')
        self.assertFalse(any('2至8' in c['text'] for c in retrieved['chunks']))
        self.assertTrue(any('十个工作日' in c['text'] for c in retrieved['chunks']))

    def test_rooms_do_not_copy_products_or_share_documents(self):
        r = reply_lab.knowledge_view('room1')
        self.assertEqual({p['id'] for p in r['products']},{'p1'})
        self.assertFalse(any('保温杯' in d['content'] for d in r['documents']))
        self.assertNotIn('knowledge',server.settings('room1'))

    async def test_reply_is_persisted_with_evidence_and_never_changes_playback(self):
        t = await self.turn()
        self.assertEqual(t['status'],'completed')
        self.assertEqual(t['model'],'mock')
        self.assertTrue(t['citations'])
        self.assertTrue(t['retrieval']['chunks'])
        self.assertIsNone(server.room_instance('room1').state['current'])
        self.assertIsNone(server.room_instance('room1').state['insertion'])
        self.assertEqual(server.all_of('viewer_event')[0]['source'],'simulated')
        self.assertEqual(server.all_of('viewer_event')[0]['reply_turn_id'],t['id'])

    async def test_close_and_reopen_restores_conversation_comments_and_logs(self):
        t = await self.turn()
        server.rooms.clear(); reply_lab.TASKS.clear()
        new_room = server.room_instance('room1')
        self.assertEqual(new_room.state['audience_events'][0]['text'],'多少钱？')
        self.assertTrue(new_room.state['logs'])
        self.assertEqual(new_room.state['status'],'stopped')
        stored = reply_lab.get_session(t['session_id'])['turns'][0]
        self.assertEqual(stored['answer'],t['answer'])
        self.assertEqual(stored['retrieval'],t['retrieval'])

    async def test_idempotent_request_does_not_double_reply_or_event(self):
        session = reply_lab.create_session('room1')
        a = await self.turn(session=session)
        b = await self.turn(session=session)
        self.assertEqual(a['id'],b['id'])
        self.assertEqual(len(server.all_of('viewer_event')),1)
        self.assertEqual(len(server.all_of('reply_turn')),1)

    async def test_product_from_another_room_rejected_before_save(self):
        session = reply_lab.create_session('room1')
        with self.assertRaises(HTTPException):
            await self.turn(product='p2',session=session)
        self.assertEqual(server.all_of('reply_turn'),[])

    async def test_multiple_products_without_context_asks_instead_of_guessing(self):
        server.save_room_config('room1',{'product_ids':['p1','p2']})
        t = await self.turn('多少钱？',product=None)
        self.assertEqual(t['answer_status'],'needs_confirmation')
        self.assertIn('哪件商品',t['answer'])
        self.assertEqual(t['retrieval']['chunks'],[])

    async def test_followup_reuses_same_conversation_product(self):
        server.save_room_config('room1',{'product_ids':['p1','p2']})
        session = reply_lab.create_session('room1')
        await self.turn('脚背宽怎么选？',session=session)
        t = await self.turn('那怎么选？',product=None,session=session,request_id='request-67890')
        self.assertEqual(t['product_id'],'p1')
        self.assertIn('脚背宽',t['retrieval']['query'])

    async def test_model_failure_keeps_comment_and_retry_reuses_record(self):
        session = reply_lab.create_session('room1')
        with patch.object(reply_lab,'complete_remote',side_effect=RuntimeError('模拟模型服务不可用')):
            t = await self.turn(session=session)
        self.assertEqual(t['status'],'error')
        self.assertTrue(t['retrieval']['chunks'])
        retry = await reply_lab.retry_turn(t['id']); await reply_lab.TASKS[retry['id']]
        t2 = server.get('reply_turn',t['id'])
        self.assertEqual(t2['status'],'completed')
        self.assertEqual(t2['attempts'],2)
        self.assertEqual(len(server.all_of('viewer_event')),1)

    async def test_restart_detects_unfinished_job(self):
        t = await self.turn()
        t['status']='running'; server.put('reply_turn',t); reply_lab.TASKS.clear()
        self.assertEqual(reply_lab.get_session(t['session_id'])['turns'][0]['status'],'interrupted')

    def test_unknown_citation_rejected(self):
        with self.assertRaises(RuntimeError):
            reply_lab.validate_answer({'answer':'包邮','status':'answered','citations':[{'chunk_id':'unknown','quote':'包邮'}]},reply_lab.retrieve([self.p1],'包邮？'))

    def test_quote_must_be_in_source_and_fact_answer_needs_citation(self):
        r = reply_lab.retrieve([self.p1],'价格？'); chunk = r['chunks'][0]
        for citations in [[],[{'chunk_id':chunk['id'],'quote':'免费赠送十双'}]]:
            with self.assertRaises(RuntimeError): reply_lab.validate_answer({'answer':'免费','status':'answered','citations':citations},r)

    def test_chat_label_cannot_bypass_factual_citation_check(self):
        with self.assertRaises(RuntimeError):
            reply_lab.validate_answer({'answer':'本款防滑包邮，107新台币','status':'chat','citations':[]},reply_lab.retrieve([self.p1],'你好'))

    def test_document_title_is_also_searchable(self):
        p = deepcopy(self.p1); p['knowledge_documents'] = [{'id':'returns','title':'退货政策','content':'收到货后七日内，保持原包装且未使用，可申请处理。'}]
        r = reply_lab.retrieve([p],'退货规则是什么？')
        self.assertTrue(any(c['document_id']=='p1:doc_returns' for c in r['chunks']))

    async def test_poll_during_event_broadcast_does_not_interrupt_queued_reply(self):
        session = reply_lab.create_session('room1'); seen = []
        async def event(identity, data):
            seen.append(reply_lab.get_session(session['id'])['turns'][0]['status'])
            return {}
        with patch.object(server,'test_event',event):
            t = await self.turn(session=session)
        self.assertEqual(seen,['queued'])
        self.assertEqual(t['status'],'completed')

    async def test_other_viewer_does_not_inherit_previous_viewer_product(self):
        server.save_room_config('room1',{'product_ids':['p1','p2']})
        session = reply_lab.create_session('room1'); await self.turn('尺码？',session=session)
        turn = await reply_lab.send_comment(session['id'],{'text':'多少钱？','username':'另一个观众','request_id':'different-viewer'})
        await reply_lab.TASKS[turn['id']]
        self.assertEqual(server.get('reply_turn',turn['id'])['answer_status'],'needs_confirmation')

    def test_original_faq_text_not_script_generation_context(self):
        self.assertFalse(any('知识库' in f['id'] or 'KNOWLEDGE' in f['id'] for f in server.generation_facts(self.p1)))

    def test_document_schema_and_duplicate_ids(self):
        with self.assertRaises(HTTPException):
            server.ProductInput(name='x',price=1,knowledge_documents=[{'id':'a','title':'t','content':'x'},{'id':'a','title':'t','content':'y'}]).record()

    async def test_one_pending_turn_per_conversation(self):
        blocker = asyncio.Event()
        async def delayed(turn): await blocker.wait()
        session = reply_lab.create_session('room1')
        with patch.object(reply_lab,'run_turn',delayed):
            t = await reply_lab.send_comment(session['id'],{'text':'价格？','request_id':'pending-first'})
            with self.assertRaises(HTTPException):
                await reply_lab.send_comment(session['id'],{'text':'尺码？','request_id':'pending-second'})
            blocker.set(); await reply_lab.TASKS[t['id']]


if __name__ == '__main__': unittest.main()
