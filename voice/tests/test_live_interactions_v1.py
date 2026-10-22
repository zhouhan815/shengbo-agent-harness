"""Current-sentence boundaries with real Room policy, fake providers only."""
import asyncio
from copy import deepcopy
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import server
from app.welcome import build_product_templates


def sentence(number, product='p1'):
    return {'id': f's{number}', 'queue_id': f'q{number}', 'product_id': product, 'script_id': 'script1',
            'text': f'商品讲解{number}', 'spoken_text': f'商品讲解{number}',
            'welcome_templates': build_product_templates({'name': '训练套装'})}


class BoundaryRoom(server.Room):
    def log(self, event, detail):
        self.state['logs'].append({'event': event, 'detail': detail})

    async def broadcast(self):
        pass

    def release_publication_slot(self):
        pass

    async def prepare(self, epoch, item):
        async with self.lock:
            if epoch == self.state['epoch']:
                self.state.update(status='ready', audio={'audio_id': item['id'], 'duration': 1})


class InteractionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.product = {'id': 'p1', 'name': '训练套装', 'price': 23.58, 'currency': 'CNY', 'selling_points': '单人练习'}
        self.config = {'id': 'default', 'name': '离线测试', 'product_ids': ['p1']}
        self.patches = [patch.object(server, 'settings', return_value=self.config),
                        patch.object(server, 'all_of', return_value=[]),
                        patch.object(server, 'get', return_value=self.product),
                        patch.object(server, 'put', side_effect=lambda kind, body: body)]
        for current in self.patches:
            current.start()
        self.room = BoundaryRoom()
        self.room.owner = 'owner'
        self.room.state.update(queue=[sentence(1), sentence(2), sentence(3)], index=0)
        self.room.set_current(sentence(1))
        await asyncio.sleep(0)
        self.room.state['status'] = 'playing'

    async def asyncTearDown(self):
        await self.room.action({'action': 'stop'})
        if self.room.interaction_tasks:
            await asyncio.gather(*list(self.room.interaction_tasks), return_exceptions=True)
        await asyncio.sleep(0)
        for current in reversed(self.patches):
            current.stop()

    def event(self, identity, kind='enter', **extra):
        return {'id': identity, 'source': 'simulated', 'type': kind, 'text': '多少钱？',
                'username': '小明123＋✨', 'session_id': self.room.state['session_id'], **extra}

    async def ack(self, event):
        await self.room.acknowledge('owner', {'event': event, 'epoch': self.room.state['epoch'],
            'attempt_id': self.room.state['current']['attempt_id']})
        await asyncio.sleep(0)

    async def prepared(self):
        for _ in range(200):
            if (self.room.state.get('insertion') or {}).get('pending', {}).get('prepared_audio'):
                return
            await asyncio.sleep(.002)
        self.fail('interaction did not become ready')

    @staticmethod
    def audio(*args):
        time.sleep(.025)
        return {'audio_id': 'test-short', 'url': '/audio/test-short.wav', 'duration': .1}

    async def test_slow_welcome_keeps_script_then_enters_only_at_next_ready_boundary(self):
        with patch.object(server, 'render', side_effect=self.audio) as render:
            result = self.room.welcome_entry(self.event('enter1'))
            self.assertEqual(result['status'], 'queued')
            self.assertEqual(self.room.state['current']['id'], 's1')
            await self.ack('ended')
            self.assertEqual(self.room.state['current']['id'], 's2')
            self.assertIsNotNone(self.room.state['insertion'])
            await self.prepared()
            self.assertEqual(self.room.state['current']['id'], 's2')
            await self.ack('playing')
            await self.ack('ended')
            self.assertTrue(self.room.state['current']['is_welcome'])
            await self.ack('playing')
            await self.ack('ended')
            self.assertEqual(self.room.state['current']['id'], 's3')
            self.assertIsNone(self.room.state['insertion'])
            render.assert_called_once()

    async def test_comment_retrieval_then_tts_then_boundary_resume_no_extra_model(self):
        answer = {'answer': '这套售价二十三点五八元。', 'answer_status': 'answered',
            'citations': [{'chunk_id': 'p1:price', 'quote': '23.58 CNY'}], 'timings_ms': {'total': 1},
            'usage': {}, 'model': 'offline', 'product_id': 'p1'}
        with patch('app.live_reply.build_live_answer', new=AsyncMock(return_value=answer)) as model, \
                patch.object(server, 'render', side_effect=self.audio) as render:
            result = self.room.comment_entry(self.event('comment1', 'comment'))
            self.assertEqual(result['status'], 'preparing')
            await self.ack('ended')
            self.assertEqual(self.room.state['current']['id'], 's2')
            await self.prepared()
            await self.ack('playing')
            await self.ack('ended')
            self.assertEqual(self.room.state['current']['text'], answer['answer'])
            self.assertEqual(self.room.state['status'], 'ready')
            await self.ack('playing')
            await self.ack('ended')
            self.assertEqual(self.room.state['current']['id'], 's3')
            model.assert_awaited_once()
            render.assert_called_once()

    async def test_failed_welcome_does_not_stall_or_cut_script(self):
        with patch.object(server, 'render', side_effect=RuntimeError('private-provider-detail')):
            self.room.welcome_entry(self.event('bad'))
            for _ in range(100):
                if not self.room.state['insertion']:
                    break
                await asyncio.sleep(.002)
            self.assertIsNone(self.room.state['insertion'])
            self.assertEqual(self.room.state['current']['id'], 's1')
            self.assertEqual(self.room.state['status'], 'playing')
            self.assertNotIn('private-provider-detail', str(self.room.state['logs']))

    async def test_reply_failure_keeps_script_and_records_safe_failure(self):
        with patch('app.live_reply.build_live_answer', new=AsyncMock(side_effect=RuntimeError('private-model-detail'))):
            self.room.comment_entry(self.event('bad-comment', 'comment'))
            await asyncio.gather(*list(self.room.interaction_tasks))
            self.assertIsNone(self.room.state['insertion'])
            self.assertEqual(self.room.state['current']['id'], 's1')
            self.assertNotIn('private-model-detail', str(self.room.state['logs']))

    async def test_busy_duplicate_stopped_and_external_comments_never_generate(self):
        self.room.state['status'] = 'paused'
        self.assertEqual(self.room.comment_entry(self.event('paused', 'comment'))['reason'], 'not_playing')
        self.room.state['status'] = 'playing'
        self.assertEqual(self.room.comment_entry(self.event('external', 'comment', source='douyin', purpose='external_test'))['reason'], 'external_test_read_only')
        self.assertEqual(self.room.comment_entry(self.event('lab', 'comment', reply_turn_id='text-only'))['status'], 'ignored')
        with patch.object(server, 'render', side_effect=self.audio):
            self.room.welcome_entry(self.event('welcome'))
            self.assertEqual(self.room.comment_entry(self.event('busy', 'comment'))['reason'], 'insertion_busy')
            self.assertEqual(self.room.comment_entry(self.event('busy', 'comment'))['reason'], 'duplicate_event')

    async def test_ready_welcome_at_last_sentence_returns_to_first_exactly_once(self):
        self.room.state['index'] = 2
        self.room.set_current(self.room.current_item())
        await asyncio.sleep(0)
        self.room.state['status'] = 'playing'
        with patch.object(server, 'render', side_effect=self.audio):
            self.room.welcome_entry(self.event('loop'))
            await self.prepared()
            await self.ack('ended')
            self.assertTrue(self.room.state['current']['is_welcome'])
            await self.ack('playing')
            old = {'event': 'ended', 'epoch': self.room.state['epoch'], 'attempt_id': self.room.state['current']['attempt_id']}
            await self.ack('ended')
            self.assertEqual(self.room.state['current']['id'], 's1')
            await self.room.acknowledge('owner', old)
            self.assertEqual(self.room.state['current']['id'], 's1')

    async def test_expired_paused_reply_resumes_script_without_speaking_stale_answer(self):
        reply = {**sentence(1), 'id': 'reply-stale', 'is_reply': True, 'is_insertion': True,
            'requires_ready': True, 'interaction_event_id': 'old',
            'interaction_session_id': self.room.state['session_id'], 'interaction_expires_at': time.time() - 1,
            'prepared_audio': {'audio_id': 'stale', 'duration': 1}}
        self.room.state.update(index=1, insertion={'kind': 'reply'}, status='paused')
        self.room.resume_item = sentence(2)
        self.room.set_current(reply)
        self.room.state['status'] = 'paused'
        await self.room.action({'action': 'resume'})
        await asyncio.sleep(0)
        self.assertEqual(self.room.state['current']['id'], 's2')
        self.assertIsNone(self.room.state['insertion'])

    async def test_reply_audio_failure_returns_to_next_script_without_stalling(self):
        reply = {**sentence(1), 'id': 'reply-fail', 'is_reply': True, 'is_insertion': True,
            'requires_ready': True, 'interaction_event_id': 'failure',
            'prepared_audio': {'audio_id': 'reply', 'duration': 1}}
        self.room.state.update(index=1, insertion={'kind': 'reply'})
        self.room.resume_item = sentence(2)
        self.room.set_current(reply)
        await self.ack('failed')
        self.assertEqual(self.room.state['current']['id'], 's2')
        self.assertIsNone(self.room.state['insertion'])
