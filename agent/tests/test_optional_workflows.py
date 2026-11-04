"""Optional flows retain authority, exact data and main-agent replanning."""
from copy import deepcopy
import pytest

from shengbo_contracts.common import ContractError, MutationContext, canonical_hash
from shengbo_prelive.action_contracts import recoverable_input_error
from shengbo_prelive.models import RunScope
from shengbo_prelive.tools import build_tools
from shengbo_prelive.workflows import WORKFLOW_SCHEMAS, run_workflow
from shengbo_prelive.runtime import CURRENT_SCOPE, CURRENT_ATTEMPT
from test_runtime_business import env, human_command, new_run, prepared_text, product
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
    assert second['next_actions'][0]['args']['branch_task_ids']==['fresh-report']
    assert engine.store.get('branch','fresh-report')['status']=='PREPARED'


@pytest.mark.asyncio
async def test_dispatch_reuses_exact_work_in_this_run_without_partial_batches(env):
    engine, _ = env
    run = await authorize(engine, await new_run(engine, '分派分析'), ['accept_analysis_report'])
    scope = RunScope(run['id'], 'dispatch-main', 'execute')
    task = {'role': 'product_facts', 'instruction': '整理当前商品事实'}
    different = {**task, 'instruction': '有明确新依据后补查商品事实'}
    first = await engine.dispatch(scope, [task, task], '共同依据', 'first-dispatch')
    branch_id, = first['branch_task_ids']
    assert [item['reused'] for item in first['items']] == [False]
    again = await engine.dispatch(scope, [task], '不同备注不产生相同工作', 'second-dispatch')
    assert again['branch_task_ids'] == [branch_id]
    assert again['items'][0]['reused']
    assert len(engine.store.list('branch')) == 1
    with pytest.raises(ContractError, match='DISPATCH_REPLAY_MISMATCH'):
        await engine.dispatch(scope, [task, different], '同一调用不能改参数', 'first-dispatch')
    with pytest.raises(ContractError, match='BRANCH_TARGET_DENIED'):
        await engine.dispatch(scope, [different, {**task, 'product_ids': ['foreign']}], '无效批次', 'invalid-dispatch')
    assert len(engine.store.list('branch')) == 1
    second = await engine.dispatch(scope, [different], '不同分析', 'refine')
    assert second['branch_task_ids'] != first['branch_task_ids']
    other = await authorize(engine, await new_run(engine, '另一项分析'), ['accept_analysis_report'])
    other_result = await engine.dispatch(RunScope(other['id'], 'dispatch-main', 'execute'),
        [task], '另一项分析', 'first-dispatch')
    assert other_result['branch_task_ids'] != first['branch_task_ids']


@pytest.mark.asyncio
async def test_selling_points_research_uses_the_single_selected_product(env):
    engine, service = env
    selected = await product(service, "轻量款")
    sibling = await product(service, "长续航款")
    run = await authorize(engine, await new_run(engine, "整理所选商品的核心卖点"), ["accept_analysis_report"])
    submitted = {**run["input"], "view_context": {"view": "products", "selling_points_action": "generate"},
                 "selection": {"target": {"object_ref": selected, "field_path": "/core_selling_points"}},
                 "observed_versions": [selected]}
    engine.store.update("run", run["id"], input=submitted, target_product_ids=[selected["id"]],
        goal={**run["goal"], "objective": "catalog_update", "target_product_ids": [selected["id"]],
              "original_input_hash": canonical_hash(submitted)})
    scope = RunScope(run["id"], "selling-points-scope", "execute")
    task = {"role": "audience_scenes", "instruction": "分析系列共有的顾客需求",
            "product_ids": [selected["id"], sibling["id"]], "input_refs": [selected, sibling]}
    first = await engine.dispatch(scope, [task], "研究商品", "single-selection")
    branch_id, = first["branch_task_ids"]
    stored = engine.store.get("branch", branch_id)["task"]
    assert stored["product_ids"] == [selected["id"]]
    assert [ref["id"] for ref in stored["input_refs"]] == [selected["id"]]
    for provided in ([], [sibling["id"]]):
        replay = await engine.dispatch(scope, [{**task, "product_ids": provided}], "研究商品", "single-selection")
        assert replay["branch_task_ids"] == [branch_id]
    assert len(engine.store.list("branch")) == 1


@pytest.mark.asyncio
async def test_batched_selling_points_research_scopes_each_report_to_one_selected_product(env):
    engine, service = env
    selected = [await product(service, name) for name in ("轻量款", "长续航款")]
    foreign = await product(service, "未选款")
    ids = [ref["id"] for ref in selected]
    run = await authorize(engine, await new_run(engine, "整理所选商品的核心卖点"), ["accept_analysis_report"])
    submitted = {**run["input"], "view_context": {"view": "products", "selling_points_action": "generate"},
                 "selection": {"items": [{"target": {"object_ref": ref, "field_path": "/core_selling_points"}}
                                          for ref in selected]},
                 "observed_versions": selected}
    engine.store.update("run", run["id"], input=submitted, target_product_ids=ids,
        goal={**run["goal"], "objective": "catalog_update", "target_product_ids": ids,
              "original_input_hash": canonical_hash(submitted)})
    scope = RunScope(run["id"], "selling-points-batch-scope", "execute")
    task = {"role": "audience_scenes", "instruction": "分析系列共有的顾客需求", "product_ids": ids}
    with pytest.raises(ContractError, match="SELLING_POINTS_RESEARCH_SCOPE_INVALID") as failed:
        await engine.dispatch(scope, [task], "研究商品", "ambiguous-selection")
    assert recoverable_input_error(failed.value)
    assert set(failed.value.details["allowed_product_ids"]) == set(ids)
    assert not engine.store.list("branch")
    result = await engine.dispatch(scope, [{**task, "product_ids": [sku, foreign["id"]]} for sku in ids],
                                   "研究商品", "one-report-per-product")
    assert [engine.store.get("branch", branch_id)["task"]["product_ids"]
            for branch_id in result["branch_task_ids"]] == [[sku] for sku in ids]


@pytest.mark.asyncio
async def test_dispatch_can_register_one_wait_and_wake_only_for_selected_branch(env):
    engine, _ = env
    run = await authorize(engine, await new_run(engine, '等待选中的资料分析'), ['accept_analysis_report'])
    scope = RunScope(run['id'], 'dispatch-main', 'execute')
    other = await engine.dispatch(scope, [{'role': 'session_config', 'instruction': '读取独立本场信息'}],
        '并行的独立任务', 'other-branch')
    wait = await engine.dispatch(scope, [{'role': 'product_facts', 'instruction': '整理商品资料'}],
        '下一步需要事实', 'selected-branch', wait_for_results=True)
    branch_id, = wait['branch_task_ids']
    assert wait['waiting']
    current = engine.store.get('run', run['id'])
    assert current['finish']['outcome'] == 'WAITING_JOBS'
    assert current['wait_for_branch_ids'] == [branch_id]

    waiting = engine.store.update('run', run['id'], status='WAITING_JOBS')
    engine.store.update('branch', other['branch_task_ids'][0], status='PREPARED')
    await engine._wake_waiting(waiting)
    assert engine.store.get('run', run['id'])['status'] == 'WAITING_JOBS'
    engine.store.update('branch', branch_id, status='BLOCKED', error={'code': 'TEST'})
    await engine._wake_waiting(waiting)
    resumed = engine.store.get('run', run['id'])
    assert resumed['status'] == 'RUNNING'
    assert any(action['args']['branch_task_ids'] == [branch_id]
               for action in resumed['append_event']['analysis_next_actions'])


@pytest.mark.asyncio
async def test_first_plan_and_dispatch_share_validation_and_replay(env):
    engine, _ = env
    run = await authorize(engine, await new_run(engine, '从已读资料分派分析'), ['accept_analysis_report'])
    engine.store.update('run', run['id'], task_assessment=None)
    scope = RunScope(run['id'], 'dispatch-main', 'execute')
    task = {'role': 'product_facts', 'instruction': '整理当前商品事实'}
    plan = {'summary': '据已读资料整理商品事实', 'capabilities': ['product_facts'], 'steps': ['分析资料']}
    with pytest.raises(ContractError, match='PLAN_CAPABILITY_DENIED'):
        await engine.dispatch(scope, [task], '同轮执行', 'bad-plan', assessment={**plan, 'capabilities': ['unknown']})
    with pytest.raises(ContractError, match='BRANCH_TARGET_DENIED'):
        await engine.dispatch(scope, [{**task, 'product_ids': ['foreign']}], '无效任务', 'bad-task', assessment=plan)
    assert not engine.store.get('run', run['id'])['task_assessment']
    assert not engine.store.list('branch')
    first = await engine.dispatch(scope, [task], '首次派发', 'combined-plan', assessment=plan)
    assert engine.store.get('run', run['id'])['task_assessment']['summary'] == plan['summary']
    later = {**plan, 'summary': '单独保存的后续计划'}
    engine.save_assessment(scope, later)
    replay = await engine.dispatch(scope, [task], '首次派发', 'combined-plan', assessment=plan)
    assert replay['branch_task_ids'] == first['branch_task_ids']
    assert engine.store.get('run', run['id'])['task_assessment']['summary'] == later['summary']
    with pytest.raises(ContractError, match='DISPATCH_REPLAY_MISMATCH'):
        await engine.dispatch(scope, [task], '首次派发', 'combined-plan', assessment=later)


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
