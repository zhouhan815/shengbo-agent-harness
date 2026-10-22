"""Public successful reads retain exact action references through M3, without grants."""
import pytest
from shengbo_prelive.models import RunScope
from test_business_document_reads_rc9 import case, reads
from test_runtime_business import env

@pytest.mark.asyncio
async def test_real_reader_records_project_selected_script_module_refs_without_body(case):
 engine, service, run, scope, prep, script, _=case
 actual=await engine.business_query(scope,'read_script',{'ref':script})
 current=await engine.business_query(scope,'read_preparation',{'preparation_id':prep['id']})
 projection=await engine.project_context(scope)
 workset=projection['observed_business_workset']
 assert workset['status']=='observed_snapshot'
 assert workset['currentness_verified'] is workset['acceptance_implied'] is workset['approval_implied'] is False
 assert workset['preparation']['artifact_ref']==current['ref']
 assert workset['selected_script_count']==1 and workset['available_module_index_count']==1
 item,=workset['scripts']
 assert item['artifact_ref']==actual['ref']
 assert item['module_refs']=={k:v for k,v in actual['body']['module_refs'].items() if v is not None}
 assert 'modules' not in item and 'body' not in item
 restored=engine.run_history(scope,{**item['module_refs_reader'],'max_chars':24000,'limit':100})
 assert restored['document']==actual['body']['module_refs']
 assert engine.store.get('run',run['id'])['allowed_effects']==[]
 assert not engine.store.list('operation') and not engine.store.list('question')
 assert all('script_observation' not in row for row in projection['retrieved_documents']['items'])

@pytest.mark.asyncio
async def test_unread_and_child_projection_does_not_gain_parent_workset(case):
 engine, _, run, scope, prep, _, _=case
 await engine.business_query(scope,'read_preparation',{'preparation_id':prep['id']})
 main=await engine.project_context(scope)
 assert main['observed_business_workset']['available_module_index_count']==0
 engine.store.put('branch',{'id':'workset-child','run_id':run['id'],'role':'session_config','status':'RUNNING',
 'task':{'role':'session_config','source_refs':[],'product_ids':[],'input_refs':[]}})
 child=RunScope(run['id'],'child-observation','execute','session_config','workset-child')
 assert 'observed_business_workset' not in await engine.project_context(child)
 assert engine.store.get('run',run['id'])['allowed_effects']==[]
