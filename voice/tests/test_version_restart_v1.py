"""Version restart preserves approved queue and seeks within the same occurrence."""
import asyncio
from copy import deepcopy
import unittest
from fastapi import HTTPException
import test_live_interactions_v1 as base

class VersionRestartTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=base.InteractionBoundaryTests.asyncSetUp
    asyncTearDown=base.InteractionBoundaryTests.asyncTearDown

    def queue(self):
        values=[dict(base.sentence(i),script_id=script,occurrence_id=occurrence)
                for i,script,occurrence in [(1,'a','a1'),(2,'a','a1'),(3,'b','b1'),(4,'b','b1'),(5,'a','a2'),(6,'a','a2')]]
        self.room.state.update(queue=values,index=5,current=deepcopy(values[5]),position=.6)
        return deepcopy(values)

    async def test_restart_current_occurrence_not_first_same_script(self):
        original=self.queue();session=self.room.state['session_id']
        await self.room.action({'action':'restart_version','action_id':'same','expected_script_id':'a','expected_occurrence_id':'a2'})
        await asyncio.sleep(0)
        self.assertEqual(self.room.state['index'],4)
        self.assertEqual(self.room.state['queue'],original)
        self.assertEqual(self.room.state['position'],0)
        self.assertEqual(self.room.state['playback_seek_revision'],1)
        self.assertEqual(self.room.state['session_id'],session)
        await self.room.action({'action':'restart_version','action_id':'same'})
        self.assertEqual(self.room.state['playback_seek_revision'],1)

    async def test_changed_version_rejected_without_moving(self):
        self.queue()
        with self.assertRaises(HTTPException):
            await self.room.action({'action':'restart_version','expected_script_id':'b'})
        self.assertEqual(self.room.state['index'],5)

    async def test_restart_during_insertion_clears_old_resume(self):
        self.queue();self.room.resume_item=base.sentence(99)
        self.room.state['insertion']={'pending':dict(base.sentence(88),is_insertion=True)}
        await self.room.action({'action':'restart_version'})
        self.assertIsNone(self.room.state['insertion']);self.assertIsNone(self.room.resume_item)
        self.assertEqual(self.room.state['index'],4)

    async def test_stop_releases_and_cannot_restart_finished_room(self):
        self.queue();await self.room.action({'action':'stop'})
        self.assertEqual(self.room.state['status'],'stopped')
        self.assertIsNone(self.room.state['current']);self.assertIsNone(self.room.state['audio'])
        with self.assertRaises(HTTPException):await self.room.action({'action':'restart_version'})

    async def test_retry_also_forces_seek_of_continuous_audio(self):
        await self.room.action({'action':'retry'})
        self.assertEqual(self.room.state['playback_seek_revision'],1)
