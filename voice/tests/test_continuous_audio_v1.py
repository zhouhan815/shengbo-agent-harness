"""No providers: timeline accuracy, immutable bindings and late-ready races."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.audio_timeline import align_subtitles, archive_provider_subtitles
from app.prosody import continuous_host_style
from app.speech import minimax_request_body, audio_key
from app.continuous_audio import overlay, choice_id
from app.prelive.storage import BusinessError, digest
from test_live_interactions_v1 import InteractionBoundaryTests


def test_subtitles_keep_punctuation_and_collapse_only_same_source_phonemes():
    source=[{'timestamped_words':[
        {'word':'家长','word_begin':0,'word_end':2,'time_begin':0,'time_end':300},
        {'word':'家长','word_begin':0,'word_end':2,'time_begin':300,'time_end':500},
        {'word':'。','time_begin':500,'time_end':700},
        {'word':'家长','word_begin':3,'word_end':5,'time_begin':700,'time_end':1100},
        {'word':'！','time_begin':1100,'time_end':1300}]}]
    result=align_subtitles(source,[{'id':'s1','spoken_text':'家长。'},{'id':'s2','spoken_text':'家长！'}],1.5)
    assert result==[{'sentence_id':'s1','start':0,'end':.7},{'sentence_id':'s2','start':.7,'end':1.5}]


@pytest.mark.parametrize('source',[
    [{'text':'错的','time_begin':0,'time_end':1000}],
    [{'text':'正确','time_begin':0,'time_end':5000}],
    [{'text':'正确','time_begin':1000,'time_end':500}],
    [{'text':'正确','time_begin':float('nan'),'time_end':1000}],
])
def test_bad_alignment_never_becomes_playable(source):
    with pytest.raises(ValueError):
        align_subtitles(source,[{'id':'s','spoken_text':'正确'}],2)


def test_boundary_inside_unaligned_word_is_not_estimated():
    with pytest.raises(ValueError,match='inside'):
        align_subtitles([{'text':'家长','time_begin':0,'time_end':1000}],
                        [{'id':'1','spoken_text':'家'},{'id':'2','spoken_text':'长'}],2)


def test_subtitle_failure_preserves_safe_status_no_remote_retry(tmp_path):
    with patch('app.audio_timeline._public_json',side_effect=ValueError('DO_NOT_PERSIST_URL')) as fetch:
        archive_provider_subtitles({'subtitle_file':'https://example.invalid/signed'},tmp_path,'asset')
    body=json.loads((tmp_path/'asset.subtitles.json').read_text())
    assert body['status']=='unavailable' and 'signed' not in json.dumps(body)
    assert fetch.call_count==1


def test_uniform_style_sent_as_real_parameters_and_separate_cache():
    style=continuous_host_style();body=minimax_request_body('正文','Friendly_Person','speech-2.8-turbo',style)
    assert body['subtitle_enable'] and body['subtitle_type']=='word'
    assert body['voice_setting']=={'voice_id':'Friendly_Person','speed':1.,'vol':1.,'pitch':0,'emotion':'happy'}
    assert 'prompt' not in body and body['text']=='正文'
    assert audio_key('正文',provider='minimax',style=style)!=audio_key('正文',provider='minimax',style={**style,'subtitle_enable':False})


def test_no_adoption_leaves_original_entry_untouched():
    class Connection:
        def execute(self,*a):return SimpleNamespace(fetchone=lambda:None)
    entry={'script_revision_ref':{'id':'s'},'sentences':[{'id':'sentence'}]}
    assert overlay(None,Connection(),{'id':'p'},entry) is entry


def test_overlay_requires_exact_script_and_timeline_hash():
    pub={'id':'p'};entry={'script_revision_ref':{'id':'s'},'sentences':[{'id':'sentence','audio':{'old':True}}]}
    timeline=[{'sentence_id':'sentence','start':0.,'end':1.}]
    body={'publication_ref':pub,'script_ref':entry['script_revision_ref'],'timeline':timeline,
          'timeline_hash':digest(timeline),'audio':{'audio_id':'new','duration':1.}}
    class Connection:
        def execute(self,*a):return SimpleNamespace(fetchone=lambda:[json.dumps(body)])
    result=overlay(None,Connection(),pub,entry)
    assert result['sentences'][0]['audio']['continuous_track']['end']==1
    assert entry['sentences'][0]['audio']=={'old':True}
    body['timeline_hash']='tampered'
    with pytest.raises(BusinessError,match='CONTINUOUS_BINDING_MISMATCH'):overlay(None,Connection(),pub,entry)


class ContinuousBoundaryRaceTests(InteractionBoundaryTests):
    async def test_ready_interaction_is_deferred_if_audio_already_crossed_boundary(self):
        with patch('app.server.render',side_effect=self.audio):
            self.room.welcome_entry(self.event('late'))
            await self.prepared()
            pending=deepcopy(self.room.state['insertion']['pending'])
            await self.room.acknowledge('owner',{'event':'ended','epoch':self.room.state['epoch'],
                'attempt_id':self.room.state['current']['attempt_id'],'boundary_choice':None})
            await asyncio.sleep(.01)
            assert self.room.state['current']['id']=='s2'
            assert self.room.state['insertion']['pending']['queue_id']==pending['queue_id']
            await self.ack('playing')
            await self.room.acknowledge('owner',{'event':'ended','epoch':self.room.state['epoch'],
                'attempt_id':self.room.state['current']['attempt_id'],'boundary_choice':pending['queue_id']})
            assert self.room.state['current']['is_welcome']
