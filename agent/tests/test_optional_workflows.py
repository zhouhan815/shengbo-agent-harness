"""Optional flows retain authority, exact data and main-agent replanning."""
from copy import deepcopy
import pytest

from shengbo_contracts.common import ContractError, MutationContext
from shengbo_prelive.models import RunScope
from shengbo_prelive.tools import build_tools
from shengbo_prelive.workflows import WORKFLOW_SCHEMAS, run_workflow
from shengbo_prelive.runtime import CURRENT_SCOPE, CURRENT_ATTEMPT
from test_runtime_business import env, human_command, new_run, prepared_text
from test_source_alias_bridge import accepted_sources, Product


async def authorize(engine, run, effects):
    grant = await engine.client.command('issue_grant', {'run_id': run['id'], 'scope': run['scope'],
        'capabilities': effects, 'target_constraints': []}, MutationContext(operation_id=run['id']+'-flow-grant'),
        run_id=run['id'], controller=True)
    return engine.store.update('run', run['id'], status='RUNNING', phase='execute',
        allowed_effects=effects, allowed_tools=[], grant_id=grant['data']['grant_id'],
        write_generation=grant['data']['write_generation'],
        goal={'objective':'prepare_session','target_product_ids':[],'scope':run['scope'], 'required_analyses':[]},
        task_assessment={'summary':'测试可选工作流，不固定总路线','capabilities':[]})


@pytest.mark.asyncio
async def test_flows_are_main_capabilities_not_a_fixed_stage_pipeline(env):
    engine, service = env
    run = await new_run(engine, '更新商品资料，不改话术')
    await authorize(engine, run, ['save_product','accept_analysis_report','save_asset_checklist'])
    scope = RunScope(run['id'],'flow-test','execute')
    names = engine.allowed_tool_names(scope)
    assert 'wf_product_materials' in names and 'wf_analysis_coordination' in names
    assert 'dispatch_analysis_batch' in names and 'read_product' in names
    assert 'wf_finalize_preparation' not in names and 'wf_required_audio' not in names
    assert set(WORKFLOW_SCHEMAS) <= {tool.name for tool in build_tools(engine)}
    assert not set(WORKFLOW_SCHEMAS) & {tool.name for tool in build_tools(engine, 'audience_scenes')}
    # A progress label cannot revoke legitimate supplemental research.
    engine.store.update('run', run['id'], recommended_stage='publication')
    assert 'dispatch_analysis_batch' in engine.allowed_tool_names(scope)
    with pytest.raises(ContractError, match='TOOL_SCOPE_DENIED'):
        await run_workflow(engine, scope, 'wf_finalize_preparation', {})


@pytest.mark.asyncio
async def test_context_flow_assembles_current_refs_without_writing_unrequested_scripts(env):
    engine, service = env
    room, prep, script = await prepared_text(service)
    run = await new_run(engine,'准备本场上下文，不生成语音',room_id=room['id'],preparation_id=prep['id'])
    await authorize(engine,run,['save_context_bundle'])
    scope=RunScope(run['id'],'context-flow','execute')
    before=await service.query('read_preparation',{'preparation_id':prep['id']})
    inspected=await run_workflow(engine,scope,'wf_context_bundle',{'action':'inspect'})
    assert inspected['status']=='COMPLETED'
    assert inspected['result']['products'][0]['ref'] == before['body']['selected_product_refs'][0]
    assert inspected['result']['draft']['body']['draft']['theme']=='隔离测试'
    result=await run_workflow(engine,scope,'wf_context_bundle',{'action':'save'})
    assert result['status']=='COMPLETED'
    saved=await service.query('read_preparation',{'preparation_id':prep['id']})
    assert saved['body']['script_refs']==before['body']['script_refs']
    assert saved['body']['selected_product_refs']==before['body']['selected_product_refs']
    events=engine.trace.events(run['id'])['events']
    assert any(e['type']=='workflow.step.completed' and e['data']['duration_ms']>=0 for e in events)


@pytest.mark.asyncio
async def test_missing_approval_returns_replan_without_audio_generation(env):
    engine, service = env
    room,prep,_=await prepared_text(service)
    run=await new_run(engine,'准备本场语音及候选',room_id=room['id'],preparation_id=prep['id'])
    await authorize(engine,run,['prepare_required_audio','publish_preparation'])
    scope=RunScope(run['id'],'audio-flow','execute')
    audio=await run_workflow(engine,scope,'wf_required_audio',{'action':'prepare'})
    assert audio['status']=='NEEDS_DECISION'
    publish=await run_workflow(engine,scope,'wf_finalize_preparation',{})
    assert publish['status']=='NEEDS_REPLAN'
    observation=await service.query('read_completion_observation',{'preparation_id':prep['id']})
    assert not any(j['body']['kind']=='audio' for j in observation['jobs']) and not observation['candidate_ready']


@pytest.mark.asyncio
async def test_source_only_report_binding_needs_no_second_model_and_is_exact(env):
    engine, service=env
    run,scope,sources,aliases,_=await accepted_sources(engine,service)
    fields=Product(name='纯关联新商品',price=129).model_dump()
    args={'schema_hash':service.schema()['schema_hash'],'patches':[],'fields':fields,'source_manifest_refs':sources,
        'field_dispositions':[{'path':'/'+key,'action':'set','knowledge':'known' if value not in (None,'',[]) else 'unknown',
            'source_refs':sources,'reason':'核验来源'} for key,value in fields.items()]}
    receipt=await engine.business_command(scope,'save_product',args,'source-only-new-product')
    ref=receipt['data']['product_ref']
    branch='original-audience'
    engine.store.put('branch',{'id':branch,'run_id':run['id'],'role':'audience_scenes','status':'RUNNING',
        'task':{'role':'audience_scenes','instruction':'从原始资料分析','product_ids':[],'source_refs':sources,'input_refs':[]}})
    engine.submit_report(RunScope(run['id'],'worker','execute','audience_scenes',branch),
        {'summary':'资料支持的顾客假设，主Agent已检查','source_refs':sources,'hypotheses':['需后续验证偏好']})
    result=await engine.bind_source_analysis(scope,[branch],{'product_ref':ref,'validation_note':'来源及保存事实一致，无新冲突'},'bind-one')
    assert result['model_requests']==0
    research=await service.query('find_audience_research',{'ref':ref})
    assert any(i['compatibility']=='exact' for i in research['items'])
    assert engine.store.get('branch',branch)['report']['product_ids']==[]
    again=await engine.bind_source_analysis(scope,[branch],{'product_ref':ref,'validation_note':'来源及保存事实一致，无新冲突'},'bind-two')
    assert again['accepted_refs']==result['accepted_refs']
    with pytest.raises(ContractError,match='BINDING_REVIEW_REQUIRED'):
        await engine.bind_source_analysis(scope,[branch],{'product_ref':ref},'no-review')


@pytest.mark.asyncio
async def test_branch_views_deduplicate_delivery_but_allow_explicit_full_read(env):
    engine,service=env
    run=await new_run(engine,'分析商品，不改话术')
    await authorize(engine,run,['accept_analysis_report'])
    scope=RunScope(run['id'],'new-report-view','execute')
    engine.store.put('branch',{'id':'fresh-report','run_id':run['id'],'role':'product_facts','status':'PREPARED',
        'report':{'summary':'完整报告','fields':{'price':99,'currency':'CNY'}},'report_hash':'not-authority'})
    first=engine.read_batch_view(scope,[])
    second=engine.read_batch_view(scope,[])
    explicit=engine.read_batch_view(scope,[],'full')
    assert first['items'][0]['report']==explicit['items'][0]['report']
    assert 'report' not in second['items'][0]
    assert second['items'][0]['report_path']=='/report'
    assert second['items'][0]['full_ref']==first['items'][0]['full_ref']
    assert engine.store.get('branch','fresh-report')['status']=='PREPARED'


@pytest.mark.asyncio
async def test_observed_usage_and_safe_response_survive_local_parse_failure_and_restart(env):
    engine,service=env
    run=await new_run(engine,'只读说明')
    engine.store.update('run',run['id'],phase='execute',status='RUNNING')
    scope=RunScope(run['id'],'observed-usage','execute')
    ticket=engine.store.reserve_attempt(run['id'],scope.invocation_id,'main')
    s=CURRENT_SCOPE.set(scope)
    a=CURRENT_ATTEMPT.set(ticket['id'])
    try:
        engine._model_event({'type':'model.sent','attempt_id':'provider-1'})
        engine._model_event({'type':'model.response_observed','attempt_id':'provider-1',
            'response_status':'completed','usage':{'input_tokens':12,'output_tokens':5}})
        engine._model_response({'status':'completed','output':[{'type':'function_call','arguments':'invalid'}]})
        # Simulate restart between full provider response and local parser.
        engine.store.recover()
        with engine.store.tx() as db:
            import json
            value=json.loads(db.execute('SELECT body FROM attempts WHERE id=?',(ticket['id'],)).fetchone()[0])
        assert value['usage']=={'input_tokens':12,'output_tokens':5}
        assert value['state']=='known_failed' and value['recovery_result_available']
        assert value['response_payload_ref']
    finally:
        CURRENT_ATTEMPT.reset(a)
        CURRENT_SCOPE.reset(s)
