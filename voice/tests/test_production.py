import asyncio
import io
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from fastapi import HTTPException
from app import server, production
from app.speech import audio_key


def fake_render(text, directory, voice='Huihui', provider='system', model='speech-2.8-turbo', style=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (audio_key(text, voice, provider, model, style) + '.wav')
    with wave.open(str(target), 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(22050)
        output.writeframes(b'\0\0' * 1200)
    return {'audio_id': target.stem, 'url': '/audio/' + target.name, 'duration': 1200/22050}


class ProductionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(server, 'RUNTIME', Path(self.temp.name)),
                        patch.object(server, 'rooms', {}), patch.object(server, 'start_lock', asyncio.Lock()),
                        patch.object(production, 'TASKS', {})]
        for p in self.patches: p.start()
        self.product = server.put('product', server.ProductInput(name='测试鞋', price=19, selling_points='EVA材质').record('p1'))
        self.script = server.structure_script(self.product, {'blocks': [{'stage': 'identity', 'sentences': [
            {'display_text': '这是测试鞋。', 'fact_ids': ['NAME']},
            {'display_text': '采用EVA材质。', 'fact_ids': ['SELLING']}]}]}, 'test', '内部场景名')
        self.script.update(status='approved', version_number=1)
        server.put('script', self.script)
        self.room = await server.create_room({'name': '测试房间', 'product_ids': ['p1'], 'plan_mode': 'all',
                                             'script_order': [self.script['id']], 'selected_script_ids': [self.script['id']]})
        self.rid = self.room['id']

    async def asyncTearDown(self):
        tasks = list(production.TASKS.values())
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    async def finish(self, job):
        task = production.TASKS.get(job['id'])
        if task: await task
        return server.get('job', job['id'])

    async def synthesize(self):
        with patch.object(server, 'render', side_effect=fake_render) as render:
            job = await production.create_job(self.rid, {'kind': 'audio'})
            result = await self.finish(job)
        return result, render.call_count

    async def test_missing_audio_prevents_start_without_synthesis(self):
        with patch.object(server, 'render') as render:
            with self.assertRaises(HTTPException) as caught:
                await server.room_control(self.rid, {'action': 'start'})
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(server.room_instance(self.rid).state['status'], 'stopped')
            render.assert_not_called()

    async def test_audio_job_stores_in_room_and_reports_per_version(self):
        result, calls = await self.synthesize()
        self.assertEqual((result['status'], result['done'], result['total']), ('completed', 2, 2))
        self.assertEqual(calls, 2)
        status = production.audio_status(self.rid)
        self.assertTrue(status['all_ready'])
        self.assertEqual(status['scripts'][0]['ready'], 2)
        manifest = server.all_of('room_audio')[0]
        self.assertEqual(manifest['room_id'], self.rid)
        self.assertTrue(all(v['url'].startswith('/room-audio/'+self.rid+'/') for v in manifest['sentences']))
        self.assertEqual(len(list(production.room_directory(self.rid).glob('*.wav'))), 2)

    async def test_rerun_reuses_all_files(self):
        await self.synthesize()
        result, calls = await self.synthesize()
        self.assertEqual(calls, 0)
        self.assertEqual(result['items'][0]['reused'], 2)

    async def test_only_missing_sentence_is_synthesized(self):
        await self.synthesize()
        next(production.room_directory(self.rid).glob('*.wav')).unlink()
        result, calls = await self.synthesize()
        self.assertEqual(calls, 1)
        self.assertEqual(result['items'][0]['reused'], 1)

    async def test_corrupt_audio_is_regenerated(self):
        await self.synthesize()
        next(production.room_directory(self.rid).glob('*.wav')).write_bytes(b'broken')
        self.assertFalse(production.audio_status(self.rid)['all_ready'])
        result, calls = await self.synthesize()
        self.assertEqual((result['status'], calls), ('completed', 1))

    async def test_voice_change_requires_new_files_and_switch_back_reuses(self):
        await self.synthesize()
        await server.update_room(self.rid, {'tts_provider': 'minimax', 'tts_voice': 'Wise_Woman'})
        self.assertFalse(production.audio_status(self.rid)['all_ready'])
        result, calls = await self.synthesize()
        self.assertEqual(calls, 2)
        await server.update_room(self.rid, {'tts_provider': 'system', 'tts_voice': 'Huihui'})
        result, calls = await self.synthesize()
        self.assertEqual(calls, 0)

    async def test_regular_playback_never_calls_render(self):
        await self.synthesize()
        with patch.object(server, 'render') as render:
            await server.room_control(self.rid, {'action': 'start'})
            target = server.room_instance(self.rid)
            await target.prepare(target.state['epoch'], deepcopy(target.state['current']))
            await asyncio.sleep(.02)
            self.assertEqual(target.state['status'], 'ready')
            self.assertIn('/room-audio/', target.state['audio']['url'])
            render.assert_not_called()
            target.owner = 'test-player'
            current = target.state['current']
            ack = {'epoch': target.state['epoch'], 'attempt_id': current['attempt_id']}
            await target.acknowledge('test-player', {**ack, 'event': 'playing'})
            await target.acknowledge('test-player', {**ack, 'event': 'ended'})
            await asyncio.sleep(.02)
            self.assertEqual(target.state['current']['ordinal'], 2)
            render.assert_not_called()
            await target.action({'action': 'stop'})

    async def test_missing_file_during_live_is_error_not_online_fallback(self):
        target = server.room_instance(self.rid)
        entry = self.script['sentences'][0]
        target.state.update(epoch=4, status='preparing')
        with patch.object(server, 'render') as render:
            await target.prepare(4, entry)
            self.assertEqual(target.state['status'], 'error')
            render.assert_not_called()

    async def test_insertion_is_the_only_online_playback_synthesis(self):
        target = server.room_instance(self.rid)
        target.state.update(epoch=2, status='preparing')
        with patch.object(server, 'render', side_effect=fake_render) as render:
            await target.prepare(2, {'id': 'insertion', 'text': '欢迎光临', 'is_insertion': True})
        self.assertEqual(render.call_count, 1)
        self.assertEqual(target.state['status'], 'ready')
        self.assertTrue(target.state['audio']['url'].startswith('/audio/'))

    async def test_room_audio_isolated(self):
        await self.synthesize()
        other = await server.create_room({'name': '另一间', 'product_ids': ['p1'], 'plan_mode': 'all', 'script_order': [self.script['id']]})
        self.assertFalse(production.audio_status(other['id'])['all_ready'])

    async def test_restart_preserves_audio_readiness_without_model(self):
        await self.synthesize()
        server.rooms.clear()
        self.assertTrue(production.audio_status(self.rid)['all_ready'])
        with patch.object(server, 'render') as render:
            await server.room_control(self.rid, {'action': 'start'})
            await asyncio.sleep(.02)
            self.assertEqual(server.room_instance(self.rid).state['status'], 'ready')
            render.assert_not_called()
            await server.room_control(self.rid, {'action': 'stop'})

    async def test_text_edit_invalidates_only_changed_sentence(self):
        await self.synthesize()
        edited = await server.edit_script(self.script['id'], {'lines': ['这是新的测试鞋。', '采用EVA材质。']})
        await server.update_room(self.rid, {'script_order': [edited['id']], 'selected_script_ids': []})
        self.assertEqual(production.audio_status(self.rid)['ready'], 1)
        result, calls = await self.synthesize()
        self.assertEqual((calls, result['items'][0]['reused']), (1, 1))

    async def test_text_needs_no_confirmation_after_audio_is_ready(self):
        draft = deepcopy(self.script); draft['status'] = 'draft'; server.put('script', draft)
        await server.update_room(self.rid, {'selected_script_ids': []})
        result, calls = await self.synthesize()
        self.assertEqual(result['status'], 'completed')
        await server.room_control(self.rid, {'action': 'start'})
        self.assertIsNotNone(server.room_instance(self.rid).state['current'])

    async def test_full_plan_cannot_be_bypassed_with_subset(self):
        await self.synthesize()
        with self.assertRaises(HTTPException) as caught:
            await server.room_control(self.rid, {'action': 'start', 'script_ids': []})
        self.assertEqual(caught.exception.status_code, 409)

    async def test_deletion_is_room_only_and_survives_reload(self):
        await server.update_room(self.rid, {'script_order': [], 'selected_script_ids': []})
        self.assertEqual(production.plan_ids(server.settings(self.rid)), [])
        self.assertEqual(server.get('script', self.script['id'])['id'], self.script['id'])

    async def test_empty_plan_cannot_resurrect_legacy_selection(self):
        await server.update_room(self.rid, {'script_order': []})
        config = server.settings(self.rid)
        self.assertEqual(config['selected_script_ids'], [])
        self.assertEqual(production.plan_ids(config), [])

    async def test_all_mode_includes_unconfirmed_text_in_actual_plan(self):
        draft = deepcopy(self.script); draft['id'] = 'draft-extra'; draft['status'] = 'draft'
        server.put('script', draft)
        await server.update_room(self.rid, {'script_order': [self.script['id'], 'draft-extra'], 'selected_script_ids': []})
        self.assertEqual(server.settings(self.rid)['selected_script_ids'], [self.script['id'], 'draft-extra'])

    async def test_script_job_is_background_and_auto_attaches_each_version(self):
        def generate(product, count, prior=None):
            return [server.structure_script(product, {'blocks': [{'stage': 'identity', 'sentences': [
                {'display_text': '新生成话术。', 'fact_ids': ['NAME']}]}]}, 'mock', '不显示的场景名')]
        with patch.object(server, 'generate_remote', side_effect=generate):
            job = await production.create_job(self.rid, {'kind': 'scripts', 'version_count': 2})
            self.assertEqual(job['status'], 'queued')
            self.assertEqual(len(job['items']), 2)
            with self.assertRaises(HTTPException):
                await production.create_job(self.rid, {'kind': 'scripts', 'version_count': 2})
            result = await self.finish(job)
        self.assertEqual((result['status'], result['done']), ('completed', 2))
        self.assertEqual(len(production.plan_ids(server.settings(self.rid))), 3)
        self.assertEqual([v['version_number'] for v in result['items']], [2, 3])
        self.assertEqual(len(server.all_of('job')), 1)

    async def test_partial_generation_failure_preserves_text_and_progress(self):
        count = 0
        def generate(product, n, prior=None):
            nonlocal count
            count += 1
            if count == 2: raise RuntimeError('测试模型故障')
            return [server.structure_script(product, {'blocks': [{'sentences': [
                {'display_text': '生成成功的句子。', 'fact_ids': ['NAME']}]}]}, 'mock', 'label')]
        with patch.object(server, 'generate_remote', side_effect=generate):
            result = await self.finish(await production.create_job(self.rid, {'kind': 'scripts', 'version_count': 3}))
        self.assertEqual((result['status'], result['done']), ('error', 1))
        self.assertEqual([v['status'] for v in result['items']], ['completed', 'error', 'not_run'])
        self.assertEqual(len(production.plan_ids(server.settings(self.rid))), 2)

    async def test_audio_failure_retains_progress_and_retry_skips_completed(self):
        calls = 0
        def failing(*args):
            nonlocal calls
            calls += 1
            if calls == 2: raise RuntimeError('测试声音故障')
            return fake_render(*args)
        with patch.object(server, 'render', side_effect=failing):
            result = await self.finish(await production.create_job(self.rid, {'kind': 'audio'}))
        self.assertEqual((result['status'], result['done']), ('error', 1))
        self.assertEqual(production.audio_status(self.rid)['ready'], 1)
        result, count = await self.synthesize()
        self.assertEqual((result['status'], count), ('completed', 1))

    async def test_restart_marks_pending_job_interrupted(self):
        server.put('job', {'id': 'old', 'room_id': self.rid, 'kind': 'audio', 'status': 'running',
                           'items': [{'status': 'running'}], 'done': 1, 'total': 2})
        self.assertEqual(production.list_jobs(self.rid)[0]['status'], 'interrupted')
        self.assertEqual(server.get('job', 'old')['done'], 1)

    async def test_background_additions_are_not_lost_by_stale_save(self):
        extra = deepcopy(self.script); extra['id'] = 'extra'; extra['version_number'] = 2
        server.put('script', extra)
        production.attach_scripts(self.rid, [extra])
        await server.update_room(self.rid, {'plan_mode': 'all', 'script_order': [self.script['id']],
                'base_script_ids': [self.script['id']], 'removed_script_ids': []})
        self.assertEqual(server.settings(self.rid)['script_order'], [self.script['id'], 'extra'])
        await server.update_room(self.rid, {'plan_mode': 'all', 'script_order': [self.script['id']],
                'base_script_ids': [self.script['id']], 'removed_script_ids': ['extra']})
        self.assertEqual(server.settings(self.rid)['script_order'], [self.script['id']])

    async def test_simulated_events_are_room_scoped_and_never_insert(self):
        target = server.room_instance(self.rid)
        for kind in ['like', 'enter', 'comment']:
            result = await server.test_event(self.rid, {'type': kind, 'username': '测试观众', 'text': '尺码怎么选？'})
            self.assertEqual(result['event']['source'], 'simulated')
            self.assertFalse(result['automatic_reply'])
        self.assertEqual(len(target.state['audience_events']), 3)
        self.assertIsNone(target.state['current'])
        self.assertEqual(len(server.all_of('viewer_event')), 3)
        other = await server.create_room({'name': '其他房间'})
        self.assertEqual(server.room_instance(other['id']).state['audience_events'], [])

    async def test_empty_comment_and_unknown_event_rejected(self):
        for data in [{'type': 'comment', 'text': ''}, {'type': 'purchase', 'text': 'test'}]:
            with self.assertRaises(HTTPException): await server.test_event(self.rid, data)

    async def test_remote_single_version_contract_and_prior_context(self):
        raw = {'versions': [{'label': '内部名称', 'blocks': [{'sentences': [
            {'display_text': '测试鞋，EVA材质。', 'fact_ids': ['NAME', 'SELLING']}]}]}]}
        payload = json.dumps({'choices': [{'message': {'content': json.dumps(raw)}}]}).encode()
        with patch.dict(server.os.environ, {'DEEPSEEK_API_KEY': 'unit-test-only'}), \
             patch.object(server.urllib.request, 'urlopen', return_value=io.BytesIO(payload)) as request:
            result = server.generate_remote(self.product, 1, [{'opening': ['旧开场']}])
        self.assertEqual(len(result), 1)
        body = json.loads(request.call_args.args[0].data)
        user_data = json.loads(body['messages'][1]['content'])
        self.assertEqual(user_data['version_count'], 1)
        self.assertEqual(user_data['prior_versions_for_variation_only'][0]['opening'], ['旧开场'])

    async def test_remote_wrong_version_count_is_rejected(self):
        payload = json.dumps({'choices': [{'message': {'content': '{"versions":[]}'}}]}).encode()
        with patch.dict(server.os.environ, {'DEEPSEEK_API_KEY': 'unit-test-only'}), \
             patch.object(server.urllib.request, 'urlopen', return_value=io.BytesIO(payload)):
            with self.assertRaises(RuntimeError): server.generate_remote(self.product, 1)


if __name__ == '__main__': unittest.main()
