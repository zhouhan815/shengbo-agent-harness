"""Exercise real grants, reports, product write and script context with a scripted provider."""
import json
from time import perf_counter

import pytest
from langchain_core.messages import AIMessage

from shengbo_contracts.common import ContractError, MutationContext, canonical_hash
from shengbo_prelive.models import IntentAssessment, MessageRequest, RunScope
from shengbo_prelive.workflows import run_workflow
from test_runtime_business import BindableFakeModel, Product, env, human_command


@pytest.mark.asyncio
async def test_worker_checkpoint_completion_cannot_downgrade_an_already_accepted_report(env, monkeypatch):
    from test_optional_workflows import authorize, new_run

    engine, service = env
    run = await authorize(engine, await new_run(engine, '检查顾客研究交接'), ['accept_analysis_report'])
    scope = RunScope(run['id'], 'handoff-race', 'execute')
    batch = await engine.dispatch(scope, [{'role': 'audience_scenes', 'instruction': '整理现有顾客问题'}], '交接测试', 'race')
    identity, = batch['branch_task_ids']

    async def finish_after_report(current, *, branch, invocation_id):
        engine.submit_report(RunScope(current['id'], invocation_id, 'execute', branch['role'], identity),
                             {'summary': '可交接的顾客研究', 'fields': {'concerns': ['收纳空间']}})
        receipt = await engine.accept_reports(scope, [identity], 'concurrent-accept')
        assert receipt['accepted_refs']

    monkeypatch.setattr(engine, '_invoke', finish_after_report)
    await engine._drive_branch(identity)
    branch = engine.store.get('branch', identity)
    assert branch['status'] == 'ACCEPTED'
    formal = await service.query('read_analysis_report', {'ref': branch['accepted_ref']})
    assert formal['body']['report']['fields']['concerns'] == ['收纳空间']
    assert not any(event['type'] == 'branch.failed' for event in engine.events(run['id'])['events'])


@pytest.mark.asyncio
async def test_three_reports_research_to_initial_product_save_then_formal_script(env, monkeypatch):
    await exercise_compound(*env, monkeypatch)


async def exercise_compound(engine, service, monkeypatch, *, live_model=None):
    begin = perf_counter()
    room = (await human_command(service, 'create_room', {'name': '周末露营专场'}))['data']['room_ref']
    prep = (await human_command(service, 'create_preparation', {'room_id': room['id'], 'room_revision': room['revision']}))['data']['preparation_ref']
    conversation = engine.create_conversation({'room_id': room['id'], 'scope': {'kind': 'preparation', 'preparation_id': prep['id']}})
    facts_source = engine.materials.accept(conversation['id'], '折叠收纳桌.md', 'text/markdown',
        '折叠收纳桌，日常售价129元。桌面展开后可放露营餐具，使用完可折叠。'.encode())['material_ref']
    draft_source = engine.materials.accept(conversation['id'], '周末本场安排.md', 'text/markdown',
        '主题：周末露营收纳。使用普通话。本场无优惠。'.encode())['material_ref']
    quote = '缺少核心卖点时，先根据顾客研究生成并保存核心卖点'
    text = f'按上传的商品资料和本场安排创建商品、保存本场设置、生成话术，不生成语音。{quote}。'
    created = await engine.submit_message(conversation['id'], MessageRequest(text=text, client_request_id='compound-prelive-test',
        intent_mode='new_task', material_refs=[facts_source, draft_source]))
    run = engine.store.get('run', created['run']['id'])
    intent = IntentAssessment(objective='materials_to_script', summary='从商品和本场设置生成话术',
        authorization_quote=text, script_delivery_requirement='new_generation',
        required_analyses=['product_facts', 'session_config', 'audience_scenes'])
    # The scheduler asks for identity when the user starts from an empty room.
    # Here the same fixture pre-establishes its new-product scope before dispatch.
    effects = ['import_blob', 'accept_analysis_report', 'save_product', 'save_session_draft', 'update_preparation_products',
               'save_context_bundle', 'save_script_plan', 'submit_script_change']
    grant = await engine.client.command('issue_grant', {'run_id': run['id'], 'scope': run['scope'],
        'capabilities': effects, 'target_constraints': []}, MutationContext(operation_id=run['id'] + '-compound-grant'),
        run_id=run['id'], controller=True)
    run = engine.store.update('run', run['id'], phase='execute', status='RUNNING', allowed_effects=effects,
        target_product_ids=[], created_product_ids=[], task_assessment={'summary': '三路处理后先补卖点，再汇合生成话术', 'capabilities': []},
        goal={**intent.model_dump(mode='json'), 'scope': run['scope'], 'original_input_hash': canonical_hash(run['input'])},
        grant_id=grant['data']['grant_id'], write_generation=grant['data']['write_generation'])
    scope = RunScope(run['id'], 'compound', 'execute')
    tasks = [{'role': role, 'instruction': '只整理本角色所需资料', 'source_refs': [source]} for role, source in
             [('product_facts', facts_source), ('session_config', draft_source), ('audience_scenes', facts_source)]]
    batch = await engine.dispatch(scope, tasks, '商品事实、本场设置和顾客分析相互独立', 'compound-dispatch',
                                  wait_for_results=True, on_ready='accept_clean')
    ids = dict(zip((task['role'] for task in tasks), batch['branch_task_ids']))
    fields = Product(name='折叠收纳桌', price=129, sku_id='CAMP-TABLE-1',
        usage_instructions='桌面展开后可放露营餐具，使用完可折叠。').model_dump()
    reports = {'session_config': {'draft': {'theme': '周末露营收纳', 'language': 'zh-CN', 'commerce': {}}},
               'audience_scenes': {'segments': [{'audience': '后备箱空间有限的露营者'}], 'scenes': [{'scene': '撤营收拾'}]},
               'product_facts': fields}
    for role in ('session_config', 'audience_scenes', 'product_facts'):
        engine.submit_report(RunScope(run['id'], role, 'execute', role, ids[role]),
            {'summary': role + '完成', 'fields': reports[role]})
    accepted = await engine._accept_clean_analysis(scope, batch['branch_task_ids'])
    assert accepted['status'] == 'ACCEPTED'
    refs = dict(zip(ids, accepted['accepted_refs']))

    calls = []
    class PointsModel(BindableFakeModel):
        async def _agenerate(self, messages, **kwargs):
            from langchain_core.outputs import ChatGeneration, ChatResult
            inputs = json.loads(messages[-1].content)
            if 'evidence_catalog' in inputs:
                calls.append('卖点生成')
                assert '后备箱空间有限的露营者' in str(inputs['customer_context'])
                evidence, = inputs['evidence_catalog']
                name, args = 'submit_selling_point_draft', {'items': [{'title': '撤营方便收',
                    'copy': '吃完收好餐具，桌子可以折起来。', 'audience_need': '收纳', 'use_scene': '撤营',
                    'evidence': [{'evidence_id': evidence}]}]}
            else:
                calls.append('卖点复核')
                assert '使用完可折叠' in str(inputs['candidates'])
                name, args = 'review_selling_point_draft', {'items': [{'index': 0, 'verdict': 'keep'}]}
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content='',
                tool_calls=[{'name': name, 'args': args, 'id': str(len(calls))}]))])

    monkeypatch.setattr(engine, 'model', live_model or PointsModel(responses=[]), raising=False)
    # Use the actual persisted session config, independently of selling-point generation.
    current = await service.query('read_preparation', {'preparation_id': prep['id']})
    draft = await engine.business_command(scope, 'save_session_draft', {'preparation_id': prep['id'],
        'expected_revision': current['ref']['revision'], 'draft': reports['session_config']['draft']}, 'compound-draft')
    args = {'branch_task_id': ids['product_facts'], 'accepted_report_ref': refs['product_facts'],
            'selling_points_from_research': {'branch_task_id': ids['audience_scenes'],
                'accepted_report_ref': refs['audience_scenes'], 'user_quote': quote}}
    with pytest.raises(ContractError, match='FACTS_SAVE_SELLING_POINTS_AUTH_REQUIRED'):
        await run_workflow(engine, scope, 'wf_product_materials', {'action': 'save_from_facts',
            'parameters': {**args, 'selling_points_from_research': {**args['selling_points_from_research'], 'user_quote': '编造授权'}}})
    assert not calls
    result = (await run_workflow(engine, scope, 'wf_product_materials', {'action': 'save_from_facts', 'parameters': args}))['result']
    repeated = (await run_workflow(engine, scope, 'wf_product_materials', {'action': 'save_from_facts', 'parameters': args}))['result']
    assert result['data']['product_ref'] == repeated['data']['product_ref']
    if live_model is None:
        assert calls == ['卖点生成', '卖点复核']
    product = result['data']['product_ref']
    persisted = (await service.query('read_product', {'ref': product}))['body']
    assert persisted['price'] == 129 and persisted['core_selling_points']
    if live_model is None:
        assert persisted['core_selling_points'] == ['撤营方便收：吃完收好餐具，桌子可以折起来。']
    assert result['facts_transfer']['selling_points_handoff']['research_ref'] == refs['audience_scenes']
    selected = await run_workflow(engine, scope, 'wf_product_materials', {'action': 'select', 'parameters': {'selected_product_refs': [product]}})
    assert selected['status'] == 'COMPLETED'
    binding = await engine.bind_source_analysis(scope, [ids['product_facts'], ids['audience_scenes']],
        {'product_ref': product, 'validation_note': '核对同一原始资料，保存商品事实未改变'}, 'compound-bind')
    assert binding['model_requests'] == 0
    bundled = (await run_workflow(engine, scope, 'wf_context_bundle', {'action': 'save',
        'analysis_refs': [refs['session_config'], *binding['accepted_refs']]}))['result']['data']['context_bundle_ref']
    record = (await service.query('read_context', {'ref': bundled}))['body']
    assert record['draft_ref'] == draft['data']['draft_ref']
    assert record['product_refs'] == [product] and set(r['id'] for r in record['analysis_refs']) >= set(r['id'] for r in binding['accepted_refs'])
    script_calls = []

    async def write_once(context, actions, item):
        script_calls.append(context)
        assert item['core_selling_points'] == persisted['core_selling_points']
        assert '后备箱空间有限的露营者' in str(context)
        lines = {'opening_long': '撤营时后备箱已经塞满了？来看看桌子用完怎么收。', 'product_intro': '这是折叠收纳桌，展开能放露营餐具。',
                 'selling_points': persisted['core_selling_points'][0], 'cta': '常去露营又想腾出收纳空间，可以看看这一款。'}
        return {'modules': {a['key']: {'sentences': [lines[a['key']]]} for a in actions if a['action'] == 'generate'}}

    monkeypatch.setattr(service.adapter, 'generate_script', write_once, raising=False)
    plan_input = {'action': 'plan_execute_wait', 'bundle_ref': bundled,
        'items': [{'product_ref': product, 'variant_id': 'outdoor-table', 'modules': [
            {'key': key, 'action': 'generate'} for key in ('opening_long', 'product_intro', 'selling_points', 'cta')]}]}
    execution = await run_workflow(engine, scope, 'wf_script_execution', plan_input)
    assert execution['status'] in {'WAITING', 'COMPLETED'}
    await service.drain_jobs()
    replay = await run_workflow(engine, scope, 'wf_script_execution', plan_input)
    assert replay['status'] == 'COMPLETED' and replay['result']['reused']
    assert len([op for op in engine.store.list('operation') if op['key'] == 'submit_script_change']) == 1
    current = await service.query('read_preparation', {'preparation_id': prep['id']})
    script_ref, = current['body']['script_refs']
    assert len(script_calls) == 1 and script_ref['kind'] == 'script_revision'
    completion = await engine.finish(scope, 'COMPLETED', '新商品、顾客研究、本场配置和正式话术已核对。')
    assert completion['accepted'], completion
    assert not any(job['body']['kind'] == 'audio' for job in (await service.query('read_completion_observation', {'preparation_id': prep['id']}))['jobs'])
    metrics = {'run_id': run['id'], 'script_fixture_calls': len(script_calls), 'script_ref': script_ref,
        'product_ref': product, 'audience_refs': binding['accepted_refs'], 'saved_points': persisted['core_selling_points'],
        'elapsed_seconds': round(perf_counter() - begin, 3)}
    if live_model is None:
        metrics['selling_points_fixture_calls'] = len(calls)
        print('COMPOUND_REPLAY', json.dumps(metrics, ensure_ascii=False))
    return metrics
