import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

from fastapi import HTTPException
from app import server
from app.speech import audio_key
from app.prosody import sentence_style


class QuietRoom(server.Room):
    async def prepare(self, epoch, item):
        pass
    async def broadcast(self):
        pass


class RoomFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = patch.object(server, 'RUNTIME', Path(self.temp.name))
        self.runtime.start()
        self.registry = patch.object(server, 'rooms', {})
        self.registry.start()
        self.roomclass = patch.object(server, 'Room', QuietRoom)
        self.roomclass.start()
        self.starts = patch.object(server, 'start_lock', asyncio.Lock())
        self.starts.start()
        self.product = server.put('product', server.ProductInput(name='测试商品', price=19, selling_points='棉质').record('p1'))
        self.script = server.structure_script(self.product, {'blocks':[{'stage':'identity','sentences':[{'display_text':'测试商品。','kind':'product_claim','fact_ids':['NAME']}]}]}, 'test', '版本A')
        self.script['status'] = 'approved'
        server.put('script', self.script)

    def tearDown(self):
        self.starts.stop(); self.roomclass.stop(); self.registry.stop(); self.runtime.stop(); self.temp.cleanup()

    async def new(self, name='测试直播间'):
        room = await server.create_room({'name':name,'product_ids':['p1'],'selected_script_ids':[self.script['id']]})
        # These flow tests use prerecorded test WAVs, never a real paid TTS request.
        directory = server.production.room_directory(room['id'])
        directory.mkdir(parents=True, exist_ok=True)
        for provider, voice in [('system','Huihui'),('minimax','Wise_Woman')]:
            for sentence in self.script['sentences']:
                target = directory / (audio_key(sentence['spoken_text'], voice, provider, style=sentence_style(sentence, provider)) + '.wav')
                with wave.open(str(target),'wb') as output:
                    output.setnchannels(1);output.setsampwidth(2);output.setframerate(22050)
                    output.writeframes(b'\0\0' * 1000)
        return room

    async def test_room_stores_references_only(self):
        r = await self.new()
        self.assertEqual(r['product_ids'], ['p1'])
        self.assertNotIn('products', r)
        self.assertNotIn('selling_points', str(r))
        changed = deepcopy(self.product); changed['name']='商品库修改后的名称'; server.put('product',changed)
        library = await server.library()
        self.assertEqual(next(p for p in library['products'] if p['id']=='p1')['name'],changed['name'])
        self.assertEqual(server.settings(r['id'])['product_ids'],['p1'])

    async def test_room_configurations_are_independent(self):
        a,b=await self.new('A'),await self.new('B')
        await server.update_room(a['id'],{'product_ids':[],'selected_script_ids':[]})
        self.assertEqual(server.settings(a['id'])['product_ids'],[])
        self.assertEqual(server.settings(b['id'])['product_ids'],['p1'])
        self.assertEqual(len(server.all_of('product')),1)

    async def test_each_room_uses_its_own_voice(self):
        r=await self.new()
        await server.update_room(r['id'],{'tts_provider':'minimax','tts_voice':'Wise_Woman'})
        await server.room_control(r['id'],{'action':'start'})
        self.assertEqual(server.room_instance(r['id']).state['tts']['provider'],'minimax')
        self.assertEqual(server.settings()['tts_provider'],'system')

    async def test_start_can_wait_for_obs_without_player(self):
        r=await self.new()
        await server.room_control(r['id'],{'action':'start'})
        state=server.room_instance(r['id']).snapshot()
        self.assertEqual(state['status'],'preparing')
        self.assertFalse(state['connected_player'])
        self.assertEqual(state['current']['product_name'],'测试商品')

    async def test_only_one_room_live_and_stop_releases_slot(self):
        a,b=await self.new('A'),await self.new('B')
        await server.room_control(a['id'],{'action':'start'})
        with self.assertRaises(HTTPException) as error:await server.room_control(b['id'],{'action':'start'})
        self.assertEqual(error.exception.status_code,409)
        await server.room_control(a['id'],{'action':'stop'})
        await server.room_control(b['id'],{'action':'start'})
        self.assertIsNotNone(server.room_instance(b['id']).state['current'])

    async def test_duplicate_start_does_not_restart(self):
        r=await self.new(); command={'action':'start','action_id':'one'}
        await server.room_control(r['id'],command); target=server.room_instance(r['id']); session=target.state['session_id']
        await server.room_control(r['id'],command)
        self.assertEqual(target.state['session_id'],session)
        with self.assertRaises(HTTPException):await server.room_control(r['id'],{'action':'start','action_id':'two'})

    async def test_unselected_product_script_is_rejected(self):
        r=await self.new()
        await server.update_room(r['id'],{'product_ids':[],'selected_script_ids':[]})
        with self.assertRaises(HTTPException):await server.room_control(r['id'],{'action':'start','script_ids':[self.script['id']]})

    async def test_changed_product_requires_new_script(self):
        r=await self.new(); p=deepcopy(self.product);p['price']=99;server.put('product',p)
        with self.assertRaises(HTTPException) as error:await server.room_control(r['id'],{'action':'start'})
        self.assertEqual(error.exception.status_code,409)

    async def test_unconfirmed_text_can_enter_plan_and_start_with_audio(self):
        self.script['status']='draft';server.put('script',self.script)
        r = await self.new()
        await server.room_control(r['id'], {'action': 'start'})
        self.assertIsNotNone(server.room_instance(r['id']).state['current'])

    async def test_empty_plan_blocks_start(self):
        r=await server.create_room({'name':'空直播间'})
        with self.assertRaises(HTTPException):await server.room_control(r['id'],{'action':'start'})

    async def test_missing_room_does_not_fall_back_to_default(self):
        with self.assertRaises(HTTPException) as error:server.room_instance('missing')
        self.assertEqual(error.exception.status_code,404)
