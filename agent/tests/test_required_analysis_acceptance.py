"""Completion requires declared, accepted analysis artifacts, never summaries."""
import pytest

from shengbo_contracts.business import AnalysisReport, MODULE_KEYS
from shengbo_contracts.common import MutationContext, canonical_hash
from shengbo_prelive.models import RunScope
from test_runtime_business import Product, env, human_command, new_run, prepared_text


ROLES = ['product_facts', 'session_config', 'audience_scenes']


async def preparation_run(engine, service, required_roles):
    room, prep, _ = await prepared_text(service)
    await human_command(service, 'save_asset_checklist', {
        'preparation_id': prep['id'], 'expected_revision': prep['revision'],
        'checklist': {'items': [], 'notes': ['本测试仅核验分析采纳']}})
    run = await new_run(engine, '准备整场文案，不生成语音', room_id=room['id'], preparation_id=prep['id'])
    engine.store.update('run', run['id'], phase='execute', status='RUNNING', allowed_effects=[],
        goal={'objective': 'prepare_session', 'required_analyses': required_roles},
        finish={'outcome': 'COMPLETED', 'summary': '三路分析和整场准备已完成'})
    scope = RunScope(run['id'], 'analysis-acceptance', 'execute', 'main')
    return prep['id'], scope


async def accept_report(service, prep_id, role, *, historical=False, product_refs=None):
    prep = await service.query('read_preparation', {'preparation_id': prep_id})
    refs = product_refs if product_refs is not None else prep['body']['selected_product_refs']
    report = AnalysisReport(summary=role + '有据的正式分析', product_ids=[r['id'] for r in refs])
    args = {'role': role, 'report': report.model_dump(mode='json'),
            'report_hash': canonical_hash(report), 'product_refs': refs}
    if not historical:
        args.update(preparation_id=prep_id, expected_revision=prep['ref']['revision'])
    receipt = await human_command(service, 'accept_analysis_report', args)
    return receipt['data']['analysis_ref']


async def save_bundle(service, prep_id, report_refs):
    prep = await service.query('read_preparation', {'preparation_id': prep_id})
    return await human_command(service, 'save_context_bundle', {
        'preparation_id': prep_id, 'expected_revision': prep['ref']['revision'],
        'product_refs': prep['body']['selected_product_refs'],
        'draft_ref': prep['body']['draft_ref'], 'analysis_refs': report_refs})


@pytest.mark.asyncio
async def test_declared_roles_cannot_finish_with_missing_report_or_accepted_branch_label(env):
    engine, service = env
    prep_id, scope = await preparation_run(engine, service, ROLES)
    refs = [await accept_report(service, prep_id, role) for role in ROLES[:2]]
    await save_bundle(service, prep_id, refs)
    engine.store.put('branch', {'id': 'unproven-audience', 'run_id': scope.run_id,
        'role': 'audience_scenes', 'status': 'ACCEPTED', 'report': {'summary': '已经采纳'}})
    check = await engine.verify_finish(scope)
    assert not check['ok']
    product_id = (await service.query('read_preparation', {'preparation_id': prep_id}))['body']['selected_product_refs'][0]['id']
    assert check['missing'] == ['声明的必要分析缺少当前范围内已正式采纳的报告：audience_scenes',
        '声明的必要分析缺少精确商品报告：audience_scenes/' + product_id]


@pytest.mark.asyncio
async def test_accepted_report_must_enter_current_context_bundle(env):
    engine, service = env
    prep_id, scope = await preparation_run(engine, service, ROLES)
    refs = [await accept_report(service, prep_id, role) for role in ROLES]
    await save_bundle(service, prep_id, refs[:2])
    check = await engine.verify_finish(scope)
    assert not check['ok']
    product_id = (await service.query('read_preparation', {'preparation_id': prep_id}))['body']['selected_product_refs'][0]['id']
    assert check['missing'] == ['声明的必要分析尚未进入本场话术上下文：audience_scenes/' + product_id]
    await save_bundle(service, prep_id, refs)
    assert (await engine.verify_finish(scope))['ok']


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_kind', ['other_preparation', 'other_product_revision', 'not_accepted'])
async def test_out_of_scope_or_unaccepted_report_does_not_satisfy_declared_role(env, invalid_kind):
    engine, service = env
    prep_id, scope = await preparation_run(engine, service, ['audience_scenes'])
    prep = await service.query('read_preparation', {'preparation_id': prep_id})
    if invalid_kind == 'other_preparation':
        other = await human_command(service, 'create_preparation', {
            'room_id': prep['body']['room_id'], 'room_revision': 1}, operation='other-analysis-preparation')
        ref = await accept_report(service, other['data']['preparation_ref']['id'],
            'audience_scenes', product_refs=prep['body']['selected_product_refs'])
        engine.store.put('branch', {'id': 'wrong-scope-report', 'run_id': scope.run_id,
            'role': 'audience_scenes', 'status': 'ACCEPTED', 'accepted_ref': ref})
    elif invalid_kind == 'other_product_revision':
        old = prep['body']['selected_product_refs'][0]
        product = await service.query('read_product', {'ref': old})
        fields = {k: v for k, v in product['body'].items() if k in Product.model_fields}
        fields['price'] = 199
        changed = await human_command(service, 'save_product', {
            'product_id': old['id'], 'expected_revision': old['revision'],
            'schema_hash': service.schema()['schema_hash'], 'fields': fields,
            'patches': [{'path': '/price', 'op': 'set', 'value': 199}]})
        ref = await accept_report(service, prep_id, 'audience_scenes', historical=True,
            product_refs=[changed['data']['product_ref']])
        await save_bundle(service, prep_id, [ref])
    else:
        with service.store.transaction(write=True) as c:
            ref = service.store.artifact(c, 'analysis', {'role': 'audience_scenes',
                'preparation_id': prep_id, 'product_refs': prep['body']['selected_product_refs'],
                'accepted': False, 'report': {'summary': '仅为未采纳候选'}})
        await save_bundle(service, prep_id, [ref])
    check = await engine.verify_finish(scope)
    assert not check['ok']
    assert any('已正式采纳的报告：audience_scenes' in item for item in check['missing'])


@pytest.mark.asyncio
async def test_matching_historical_report_in_bundle_can_satisfy_only_declared_role(env):
    engine, service = env
    prep_id, scope = await preparation_run(engine, service, ['audience_scenes'])
    ref = await accept_report(service, prep_id, 'audience_scenes', historical=True)
    await save_bundle(service, prep_id, [ref])
    assert not engine.store.list('branch'), '历史正式报告复用不要求本次重新派发'
    assert (await engine.verify_finish(scope))['ok']


@pytest.mark.asyncio
async def test_prepare_without_declared_analyses_does_not_require_three_branches(env):
    engine, service = env
    _, scope = await preparation_run(engine, service, [])
    assert not engine.store.list('branch')
    assert (await engine.verify_finish(scope))['ok']


async def source_only_facts_run(engine, service, *, role='product_facts', save_partial=False, no_sources=False, omit_source_schema=False):
    """Actual source import/acceptance/save receipts, isolated from providers."""
    room = (await human_command(service, 'create_room', {'name': '来源链新商品测试'}))['data']['room_ref']
    prep_ref = (await human_command(service, 'create_preparation', {'room_id': room['id'],
        'room_revision': room['revision']}))['data']['preparation_ref']
    prep_id = prep_ref['id']
    run = await new_run(engine, '依据商家资料新建口红并完成本场文案，不生成语音',
        room_id=room['id'], preparation_id=prep_id)
    scope = RunScope(run['id'], 'source-only-main', 'execute', 'main')
    sources = [] if no_sources else [engine.materials.accept(run['conversation_id'], 'source-' + str(i) + '.md',
        'text/markdown', ('# 商品资料\n有据的原始商品信息' + str(i)).encode('utf-8'))['material_ref'] for i in range(2)]
    effects = ['import_blob', 'accept_analysis_report', 'save_product']
    grant = await engine.client.command('issue_grant', {'run_id': run['id'], 'scope': run['scope'],
        'capabilities': effects, 'target_constraints': []}, MutationContext(operation_id='source-facts-grant'),
        run_id=run['id'], controller=True)
    engine.store.update('run', run['id'], input={**run['input'], 'material_refs': sources},
        phase='execute', status='RUNNING', allowed_effects=effects, target_product_ids=[],
        goal={'objective': 'prepare_session', 'scope': run['scope'], 'required_analyses': [role]},
        grant_id=grant['data']['grant_id'], write_generation=grant['data']['write_generation'])
    branch_id = 'source-only-branch'
    engine.store.put('branch', {'id': branch_id, 'branch_task_id': branch_id, 'run_id': run['id'],
        'role': role, 'status': 'RUNNING', 'task': {'role': role, 'instruction': '先分析上传资料，尚无目标商品ID',
            'product_ids': [], 'source_refs': sources, 'input_refs': [], 'prior_report_refs': []}})
    engine.submit_report(RunScope(run['id'], 'source-facts-worker', 'execute', role, branch_id),
        {'summary': '仅依据授权商家资料整理事实，不虚构商品ID', 'product_ids': []})
    ref = (await engine.accept_reports(scope, [branch_id], 'accept-source-only'))['accepted_refs'][0]
    analysis = await service.query('read_analysis_report', {'ref': ref})
    assert analysis['body']['product_refs'] == []
    formal_sources = analysis['body']['report']['source_refs']
    assert len(formal_sources) == len(sources)
    saved_sources = formal_sources[:1] if save_partial else formal_sources
    if omit_source_schema:
        saved_sources = [{k: v for k, v in ref.items() if k != 'schema_version'} for ref in saved_sources]
    saved = await engine.business_command(scope, 'save_product', {'schema_hash': service.schema()['schema_hash'],
        'fields': Product(name='来源链测试新口红', price=129, selling_points='商家资料明确的颜色与规格').model_dump(), 'patches': [],
        'source_manifest_refs': saved_sources}, 'save-from-source-facts')
    product_ref = saved['data']['product_ref']
    prep = await service.query('read_preparation', {'preparation_id': prep_id})
    selected = await human_command(service, 'update_preparation_products', {'preparation_id': prep_id,
        'expected_revision': prep['ref']['revision'], 'selected_product_refs': [product_ref]})
    draft = await human_command(service, 'save_session_draft', {'preparation_id': prep_id,
        'expected_revision': selected['data']['preparation_ref']['revision'], 'draft': {'theme': '来源链测试'}})
    await human_command(service, 'save_asset_checklist', {'preparation_id': prep_id,
        'expected_revision': draft['data']['preparation_ref']['revision'], 'checklist': {'items': [], 'notes': []}})
    await save_bundle(service, prep_id, [ref])
    prep = await service.query('read_preparation', {'preparation_id': prep_id})
    plan = await human_command(service, 'save_script_plan', {'preparation_id': prep_id,
        'expected_revision': prep['ref']['revision'], 'bundle_ref': prep['body']['bundle_ref'],
        'items': [{'product_ref': product_ref, 'variant_id': 'source-binding-variant',
            'modules': [{'key': key, 'action': 'disable' if key in {'campaign', 'transition'} else 'generate'} for key in MODULE_KEYS]}]})
    await human_command(service, 'submit_script_change', {'preparation_id': prep_id,
        'expected_revision': plan['data']['preparation_ref']['revision'], 'plan_ref': plan['data']['script_plan_ref'],
        'context_bundle_ref': prep['body']['bundle_ref'], 'expected_generation_epoch': plan['data']['generation_epoch']})
    await service.drain_jobs()
    prep = await service.query('read_preparation', {'preparation_id': prep_id})
    await human_command(service, 'save_playlist', {'preparation_id': prep_id,
        'expected_revision': prep['ref']['revision'], 'loop_mode': 'infinite',
        'items': [{'occurrence_id': 'source-binding-item', 'product_id': product_ref['id'],
            'script_revision_ref': prep['body']['script_refs'][0]}]})
    engine.store.update('run', run['id'], finish={'outcome': 'COMPLETED', 'summary': '已依据完整来源保存并汇入本场'})
    return scope, ref, saved


@pytest.mark.asyncio
@pytest.mark.parametrize('omit_source_schema', [False, True])
async def test_source_only_facts_bind_through_complete_sources_and_real_current_save_receipt(env, omit_source_schema):
    engine, service = env
    scope, analysis_ref, saved = await source_only_facts_run(engine, service, omit_source_schema=omit_source_schema)
    check = await engine.verify_finish(scope)
    assert check['ok'], check['missing']
    assert check['analysis_source_bindings'][0]['analysis_ref'] == analysis_ref
    assert check['analysis_source_bindings'][0]['product_ref'] == saved['data']['product_ref']
    assert check['analysis_source_bindings'][0]['operation_id'] == saved['operation_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['partial_sources', 'no_sources', 'prior_run', 'missing_remote_receipt',
    'audience_role', 'different_pinned_revision', 'tampered_local_sources', 'legacy_remote_reader', 'unfrozen_owner'])
async def test_source_only_binding_rejects_incomplete_or_unproven_product_link(env, invalid):
    engine, service = env
    scope, analysis_ref, saved = await source_only_facts_run(engine, service,
        role='audience_scenes' if invalid == 'audience_role' else 'product_facts',
        save_partial=invalid in {'partial_sources', 'tampered_local_sources'}, no_sources=invalid == 'no_sources')
    if invalid == 'prior_run':
        engine.store.update('operation', saved['operation_id'], run_id='older-run')
    if invalid == 'unfrozen_owner':
        run = engine.store.get('run', scope.run_id)
        engine.store.update('run', scope.run_id, goal={key: value for key, value in run['goal'].items() if key != 'scope'})
    if invalid == 'missing_remote_receipt':
        with service.store.transaction(write=True) as db:
            db.execute('DELETE FROM prelive_operations WHERE operation_id=?', (saved['operation_id'],))
    if invalid == 'tampered_local_sources':
        analysis = await service.query('read_analysis_report', {'ref': analysis_ref})
        operation = engine.store.get('operation', saved['operation_id'])
        args = {**operation['args'], 'source_manifest_refs': analysis['body']['report']['source_refs']}
        engine.store.update('operation', saved['operation_id'], args=args, request_hash=canonical_hash(args))
    if invalid == 'legacy_remote_reader':
        original_query = engine.client.query
        async def without_request_commitment(key, *args, **kwargs):
            value = await original_query(key, *args, **kwargs)
            return {k: v for k, v in value.items() if k != 'request_hash'} if key == 'read_operation_receipt' else value
        engine.client.query = without_request_commitment
    if invalid == 'different_pinned_revision':
        old = saved['data']['product_ref']
        product = await service.query('read_product', {'ref': old})
        fields = {k: v for k, v in product['body'].items() if k in Product.model_fields}
        fields['price'] = 149
        changed = await human_command(service, 'save_product', {'product_id': old['id'],
            'expected_revision': old['revision'], 'schema_hash': service.schema()['schema_hash'], 'fields': fields,
            'patches': [{'path': '/price', 'op': 'set', 'value': 149}]})
        run = engine.store.get('run', scope.run_id)
        prep = await service.query('read_preparation', {'preparation_id': run['scope']['preparation_id']})
        await human_command(service, 'update_preparation_products', {'preparation_id': prep['ref']['id'],
            'expected_revision': prep['ref']['revision'], 'selected_product_refs': [changed['data']['product_ref']]})
    check = await engine.verify_finish(scope)
    assert not check['ok']
    assert not check['analysis_source_bindings']
    assert any('已正式采纳的报告' in item for item in check['missing'])
