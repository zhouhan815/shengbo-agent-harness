"""Disclosure affects model schemas, never the underlying permission or executor."""
from types import SimpleNamespace
from unittest.mock import patch
import asyncio,json
import pytest
from langchain_core.messages import HumanMessage
from shengbo_prelive.models import RunScope
from shengbo_prelive.runtime import CURRENT_SCOPE
from shengbo_prelive.middleware import ContextProjection,ToolPolicy,Audit
from shengbo_prelive.tools import build_tools
from shengbo_prelive.tool_presentation import CORE_TOOLS,CONTROL_TOOLS,GROUPS,activate,catalog,expanded_names,presentation_view,visible_names
from shengbo_prelive.policy import check_tool_allowed
from shengbo_prelive.store import RuntimeStore
from test_runtime_business import env
from test_optional_workflows import authorize,new_run
from test_context_window_budget import setup

async def enable(engine,run):
 engine.tool_presentation_enabled=True
 engine.main_tool_names.update(CONTROL_TOOLS)
 return RunScope(run['id'],'presentation-main','execute')

@pytest.mark.asyncio
async def test_full_registration_unchanged_authority_and_autonomous_activation(env):
 engine,_=env
 run=await new_run(engine,'完整准备本场直播')
 run=await authorize(engine,run,['save_product','save_context_bundle','save_script_plan','submit_script_change','save_asset_checklist'])
 run=engine.store.update('run',run['id'],allowed_tools=['save_product','save_asset_checklist'])
 scope=await enable(engine,run)
 allowed=engine.allowed_tool_names(scope); initial=engine.visible_tool_names(scope)
 assert 'save_product' in allowed-initial
 assert 'save_product' in {t.name for t in build_tools(engine)}
 check_tool_allowed(scope,run,'save_product') # No activation execution gate.
 before=engine.store.get('run',run['id'])
 result=activate(engine,scope,names=['save_product','execute','not_registered'])
 assert result['expanded']==['save_product']
 assert set(result['unavailable'])=={'execute','not_registered'}
 assert engine.store.get('run',run['id'])==before
 assert not engine.store.list('operation') and not engine.store.list('question')
 assert 'save_product' in engine.visible_tool_names(scope)
 assert 'execute' not in engine.visible_tool_names(scope)
 activate(engine,scope,all=True)
 assert engine.visible_tool_names(scope)==engine.allowed_tool_names(scope)
 assert catalog(engine,scope,query='save_product')['items'][0]['expanded'] is True

@pytest.mark.asyncio
async def test_scope_isolation_atomic_union_and_revocation(env):
 engine,_=env
 run=await new_run(engine,'完整准备本场直播')
 run=await authorize(engine,run,['save_product','save_asset_checklist'])
 run=engine.store.update('run',run['id'],allowed_tools=['save_product','save_asset_checklist'])
 scope=await enable(engine,run)
 await asyncio.gather(asyncio.to_thread(activate,engine,scope,names=['save_product']),asyncio.to_thread(activate,engine,scope,names=['save_asset_checklist']))
 assert {'save_product','save_asset_checklist'} <= expanded_names(engine,scope)
 child=RunScope(run['id'],'child','execute','product_facts','child-presentation')
 assert expanded_names(engine,child)==set()
 other=await new_run(engine,'更新另一个商品')
 assert expanded_names(engine,RunScope(other['id'],'other','execute'))==set()
 # Presentation can retain a name, but never a revoked permission.
 engine.store.update('run',run['id'],allowed_effects=[],allowed_tools=[])
 assert 'save_product' not in engine.visible_tool_names(scope)
 assert 'save_product' not in {x['name'] for x in catalog(engine,scope)['items']}

@pytest.mark.asyncio
async def test_restart_catalog_pagination_and_full_schema_fallback(env):
 engine,_=env
 run=await new_run(engine,'完整准备本场直播')
 run=await authorize(engine,run,['save_product','save_asset_checklist'])
 engine.store.update('run',run['id'],allowed_tools=['save_product','save_asset_checklist'])
 scope=await enable(engine,run)
 original_operations=engine.store.list('operation')
 activate(engine,scope,groups=['product'])
 # Reopening durable storage must not require another disclosure model call.
 reopened=SimpleNamespace(store=RuntimeStore(engine.store.path),tool_presentation_enabled=True)
 assert 'save_product' in expanded_names(reopened,scope)
 assert visible_names(reopened,scope,engine.allowed_tool_names(scope))==engine.visible_tool_names(scope)
 page=catalog(engine,scope,limit=1)
 names=[row['name'] for row in page['items']]
 while page['next_cursor'] is not None:
  page=catalog(engine,scope,cursor=page['next_cursor'],limit=1)
  names.extend(row['name'] for row in page['items'])
 assert len(names)==len(set(names))==len(engine.allowed_tool_names(scope))
 assert set(names)==engine.allowed_tool_names(scope)
 assert catalog(engine,scope,query='保存')['items']
 reopened.tool_presentation_enabled=False
 assert visible_names(reopened,scope,engine.allowed_tool_names(scope))==engine.allowed_tool_names(scope)
 assert engine.store.list('operation')==original_operations

@pytest.mark.asyncio
async def test_receipt_driven_disclosure_keeps_next_tools_and_manual_choices(env):
    engine, _ = env
    run = await authorize(engine, await new_run(engine, '完整准备本场直播'), [
        'save_product', 'save_session_draft', 'save_context_bundle', 'save_script_plan', 'submit_script_change',
        'save_asset_checklist', 'save_playlist', 'compile_playback_plan', 'prepare_required_audio', 'publish_preparation',
        'retry_failed_items'])
    engine.store.update('run', run['id'], allowed_tools=['save_asset_checklist', 'compile_playback_plan',
        'prepare_required_audio', 'publish_preparation', 'retry_failed_items'])
    scope = await enable(engine, run)
    activate(engine, scope, groups=GROUPS, pinned=True)
    allowed = engine.allowed_tool_names(scope)
    initial = engine.visible_tool_names(scope)
    assert {'search_catalog', 'read_voice_options', 'prepare_required_audio'} <= initial

    def record(*commands, **changes):
        engine.store.update('run', run['id'], receipts=[{'command': command} for command in commands], **changes)

    record('save_context_bundle')
    writing = engine.visible_tool_names(scope)
    assert {'wf_script_execution', 'read_generation_output', 'retry_failed_items', 'save_asset_checklist'} <= writing
    assert {'search_catalog', 'read_voice_options', 'prepare_required_audio'} <= allowed - writing
    assert presentation_view(engine, scope, allowed)['workflow_phase'] == 'script'
    assert 'search_catalog' in {row['name'] for row in catalog(engine, scope, query='search_catalog')['items']}
    check_tool_allowed(scope, engine.store.get('run', run['id']), 'search_catalog')
    activate(engine, scope, names=['search_catalog'])
    assert 'search_catalog' in engine.visible_tool_names(scope)

    record('save_context_bundle', 'save_playlist')
    delivery = engine.visible_tool_names(scope)
    assert {'compile_playback_plan', 'prepare_required_audio', 'publish_preparation', 'read_job'} <= delivery
    assert 'search_catalog' in delivery and 'read_voice_options' not in delivery
    assert presentation_view(engine, scope, allowed)['workflow_phase'] == 'delivery'

    record('save_context_bundle', 'save_playlist', pending_unknown_operation='op1')
    assert initial <= engine.visible_tool_names(scope)
    record('save_context_bundle', 'save_playlist', pending_unknown_operation=None)
    engine.store.put('question', {'id': 'question1', 'run_id': run['id'], 'status': 'OPEN'})
    assert initial <= engine.visible_tool_names(scope)
    engine.store.update('question', 'question1', status='ANSWERED')
    engine.store.put('branch', {'id': 'branch1', 'run_id': run['id'], 'status': 'PREPARED'})
    assert initial <= engine.visible_tool_names(scope)
    engine.store.update('branch', 'branch1', status='ACCEPTED')
    assert engine.visible_tool_names(scope) == delivery

    record('save_context_bundle', 'save_playlist', 'save_session_draft')
    assert initial <= engine.visible_tool_names(scope)
    record('save_context_bundle', 'save_script_plan', goal={'objective': 'materials_to_script'})
    assert presentation_view(engine, scope, allowed)['workflow_phase'] == 'script_only'
    assert {'read_voice_options', 'save_asset_checklist', 'prepare_required_audio'} <= allowed - engine.visible_tool_names(scope)


@pytest.mark.asyncio
async def test_legacy_expansions_keep_their_original_visibility(env):
    engine, _ = env
    run = await authorize(engine, await new_run(engine, '完整准备本场直播'), ['save_context_bundle'])
    scope = await enable(engine, run)
    activate(engine, scope, names=['search_catalog'])
    row = engine.store.list('tool_presentation')[0]
    engine.store.put('tool_presentation', {key: value for key, value in row.items()
        if key not in {'manual_names', 'pinned_names'}})
    engine.store.update('run', run['id'], receipts=[{'command': 'save_context_bundle'}])
    assert 'search_catalog' in engine.visible_tool_names(scope)


@pytest.mark.asyncio
async def test_disclosure_never_expands_intent_or_child_permissions(env):
 engine,_=env
 run=await new_run(engine,'完整准备本场直播')
 scope=await enable(engine,run)
 intent=RunScope(run['id'],'intent','intent')
 child=RunScope(run['id'],'expert','execute','product_facts','child-identity')
 for restricted in (intent,child):
  allowed=engine.allowed_tool_names(restricted)
  assert not CONTROL_TOOLS & allowed
  assert visible_names(engine,restricted,allowed)==allowed
  with pytest.raises(Exception) as failure:
   check_tool_allowed(restricted,run,'activate_tools')
  assert getattr(failure.value,'code',None)=='TOOL_SCOPE_DENIED'

@pytest.mark.asyncio
async def test_native_sdk_actual_wire_uses_shared_visible_set_and_durable_activation(setup):
 engine,_,run,sent,credentials=setup
 original_operations=engine.store.list('operation')
 original_questions=engine.store.list('question')
 scope=await enable(engine,run)
 function={'type':'function_call','call_id':'disclose-query','name':'activate_tools','arguments':json.dumps({'names':['search_catalog']})}
 engine.test_response_tool_calls=[[function]]
 with patch('shengbo_prelive.runtime.main_middleware',lambda e:[ContextProjection(e),ToolPolicy(e),Audit(e)]):
  graph=engine._build_graph('main')
 config={'configurable':{'thread_id':'progressive-real-wire'},'recursion_limit':15}
 token=CURRENT_SCOPE.set(scope)
 try:
  await graph.ainvoke({'messages':[HumanMessage('按需展开商品检索，不执行商品写入')],'files':engine._initial_files('main')},config,context=scope)
 finally: CURRENT_SCOPE.reset(token)
 normal=[p for p in sent if p.get('tools')]
 assert len(normal)==2
 first={t['name'] for ns in normal[0]['tools'] for t in ns['tools']}
 second={t['name'] for ns in normal[1]['tools'] for t in ns['tools']}
 assert 'search_catalog' not in first and 'activate_tools' in first
 assert 'search_catalog' in second
 assert second==engine.visible_tool_names(scope)
 assert 'search_catalog' in expanded_names(engine,scope)
 assert all(engine._estimate_payload(p)<=engine.context_input_token_limit for p in sent)
 assert engine.store.list('operation')==original_operations
 assert engine.store.list('question')==original_questions
 assert any(op['id']=='unknown-audio' and op['status']=='unknown' for op in original_operations)
