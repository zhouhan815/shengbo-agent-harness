"""Catalog acceptance, exact source bridge and explicitly inherited evidence."""
from copy import deepcopy

import pytest

from shengbo_contracts.business import COMMAND_ARGS
from shengbo_contracts.common import ContractError
from shengbo_prelive.models import MessageRequest, RunScope
from shengbo_prelive.product_materials import save_accepted_product
from test_optional_workflows import authorize
from test_runtime_business import Product, env, new_run


@pytest.mark.asyncio
async def test_catalog_accepted_sources_save_and_multihop_followup_preserve_exact_authority(env):
    engine, service = env
    run = await new_run(engine, '按上传的网球商品资料入库，暂不生成卖点或话术')
    source = engine.materials.accept(run['conversation_id'], 'tennis.md', 'text/markdown',
        '# 单人网球回弹训练器\n已确认零售价23.58元，套装含球拍、系绳球和底座。'.encode())['material_ref']
    run = await authorize(engine, run, ['import_blob', 'accept_analysis_report', 'save_product'])
    run = engine.store.update('run', run['id'], input={**run['input'], 'material_refs': [source]},
        goal={**run['goal'], 'objective': 'catalog_update'}, target_product_ids=[], created_product_ids=[], receipts=[])
    scope = RunScope(run['id'], 'catalog-main', 'execute')
    candidate = Product(name='隔离网球回弹训练器', sku_id='CATALOG-TENNIS-TEST', price=23.58, currency='CNY', core_selling_points=[], selling_points='',
                        specifications=[{'name': '套装', 'value': '球拍、系绳球和底座'}]).model_dump()
    branch_id = 'catalog-source-facts'
    engine.store.put('branch', {'id': branch_id, 'run_id': run['id'], 'role': 'product_facts', 'status': 'RUNNING',
        'task': {'role': 'product_facts', 'instruction': '整理商家事实，不补卖点', 'product_ids': [],
                 'source_refs': [source], 'input_refs': []}})
    engine.submit_report(RunScope(run['id'], 'catalog-facts', 'execute', 'product_facts', branch_id),
        {'summary': '按资料与明确价格整理', 'fields': candidate, 'source_refs': [source]})
    # A prepared label must not authorize a Voice alias or the original upload.
    original_args = COMMAND_ARGS['save_product'].model_validate({'fields': candidate, 'patches': [],
        'schema_hash': service.schema()['schema_hash'], 'source_manifest_refs': [source]})
    with pytest.raises(ContractError, match='REPORT_DEPENDENCY_NOT_READY') as blocked:
        await engine._normalize_business_source_refs(scope, original_args)
    assert blocked.value.details['sources'] == [{'source_ref': source, 'status': 'report_required'}]
    assert blocked.value.details['next_actions'][0]['args']['branch_task_ids'] == [branch_id]
    without_sources = original_args.model_copy(update={'source_manifest_refs': []})
    assert (await engine._normalize_business_source_refs(scope, without_sources))['source_manifest_refs'] == []
    reference = (await engine.accept_reports(scope, [branch_id], 'catalog-accept'))['accepted_refs'][0]
    report = await engine.business_query(scope, 'read_context', {'ref': reference})
    alias = report['body']['report']['source_refs'][0]
    assert alias != source and not run['scope']['preparation_id']
    assert (await engine.read_source(scope, alias))['source_material_ref'] == source
    for forged in ({**alias, 'revision': 2}, {**alias, 'content_hash': 'sha256:' + 'a' * 64}):
        with pytest.raises(ContractError) as denied:
            await engine.read_source(scope, forged)
        assert denied.value.status == 403
    accepted_branch = engine.store.get('branch', branch_id)
    engine.store.update('branch', branch_id, status='PREPARED')
    with pytest.raises(ContractError) as denied:
        await engine.read_source(scope, alias)
    assert denied.value.status == 403
    engine.store.put('branch', accepted_branch)
    result = await save_accepted_product(engine, scope,
        {'branch_task_id': branch_id, 'accepted_report_ref': reference}, tool_call_id='catalog-save')
    saved = await service.query('read_product', {'ref': result['data']['product_ref']})
    assert {k: saved['body'][k] for k in candidate} == candidate
    operation = engine.store.get('operation', result['operation_id'])
    assert operation['args']['source_manifest_refs'] == [alias]

    # Use the real message entry to construct the direct follow_up projection.
    engine.store.update('run', run['id'], status='BLOCKED')
    continuation = await engine.submit_message(run['conversation_id'], MessageRequest(
        text='复用上次正式商品报告继续整理；不继承写入授权', intent_mode='follow_up', related_run_id=run['id'],
        client_request_id='catalog-followup'))
    follow = await authorize(engine, continuation['run'], ['save_product'])
    follow = engine.store.update('run', follow['id'], goal={**follow['goal'], 'objective': 'catalog_update'})
    next_scope = RunScope(follow['id'], 'catalog-followup-main', 'execute')
    assert reference in follow['inherited_context']['reusable_refs']
    assert (await engine.business_query(next_scope, 'read_context', {'ref': reference}))['body'] == report['body']
    # Subsequent reads must retain the same exact source-only read authority.
    assert (await engine.business_query(next_scope, 'read_context', {'ref': reference}))['ref'] == reference
    assert (await engine.read_source(next_scope, alias))['source_material_ref'] == source
    assert (await engine._normalize_business_source_refs(next_scope, original_args))['source_manifest_refs'] == [alias]
    with pytest.raises(ContractError, match='FACTS_SAVE_REPORT_DENIED'):
        await save_accepted_product(engine, next_scope,
            {'branch_task_id': branch_id, 'accepted_report_ref': reference}, tool_call_id='cross-run-save-denied')
    inherited = deepcopy(follow['inherited_context'])
    # Knowing a ref or observing it earlier cannot replace the exact direct
    # inheritance, same conversation/scope, original upload and accepted commit.
    variants = [
        {'inherited_context': {**inherited, 'reusable_refs': []}},
        {'conversation_id': 'foreign-conversation'},
        {'scope': {**follow['scope'], 'account_id': 'another-account'}},
        {'input': {**follow['input'], 'intent_mode': 'new_task'}},
        {'input': {**follow['input'], 'material_refs': []}},
    ]
    for changes in variants:
        engine.store.update('run', follow['id'], **changes)
        with pytest.raises(ContractError):
            await engine.read_source(next_scope, alias)
        engine.store.put('run', follow)
    engine.store.update('run', run['id'], status='RUNNING')
    with pytest.raises(ContractError):
        await engine.read_source(next_scope, alias)
    engine.store.update('run', run['id'], status='BLOCKED')
    assert (await engine.read_source(next_scope, alias))['source_material_ref'] == source

    # Real follow_up entries must carry a source-only formal report through
    # three terminal ancestors, without creating a new write authorization.
    chain = [engine.store.get('run', run['id']), engine.store.get('run', follow['id'])]
    for hop in range(2, 4):
        previous = engine.store.update('run', chain[-1]['id'], status='BLOCKED')
        chain[-1] = previous
        message = await engine.submit_message(run['conversation_id'], MessageRequest(
            text='沿用已正式接收的原件报告继续整理，不继承先前写入授权', intent_mode='follow_up',
            related_run_id=previous['id'], client_request_id=f'catalog-followup-{hop}'))
        child = await authorize(engine, message['run'], ['save_product'])
        child = engine.store.update('run', child['id'], goal={**child['goal'], 'objective': 'catalog_update'})
        assert reference in child['inherited_context']['reusable_refs']
        chain.append(child)
    latest = chain[-1]
    latest_scope = RunScope(latest['id'], 'catalog-three-hop-main', 'execute')
    assert set(engine._related_evidence_owner_runs(latest)) == {latest['id']}
    assert set(engine._related_evidence_owner_runs(latest, allow_catalog=True)) == {item['id'] for item in chain}
    assert (await engine.business_query(latest_scope, 'read_context', {'ref': reference}))['ref'] == reference
    assert (await engine.business_query(latest_scope, 'read_context', {'ref': reference}))['body'] == report['body']
    assert (await engine.read_source(latest_scope, alias))['source_material_ref'] == source
    assert (await engine._normalize_business_source_refs(latest_scope, original_args))['source_manifest_refs'] == [alias]
    assert not engine.store.get('run', latest['id']).get('receipts')

    async def denied_after_changes(item, changes):
        original = engine.store.get('run', item['id'])
        engine.store.update('run', item['id'], **changes)
        try:
            with pytest.raises(ContractError):
                await engine.read_source(latest_scope, alias)
            # Previously observed exact refs cannot bypass a broken evidence
            # edge when re-reading a historical source-only formal report.
            with pytest.raises(ContractError):
                await engine.business_query(latest_scope, 'read_context', {'ref': reference})
        finally:
            engine.store.put('run', original)

    for item in chain[1:]:
        inherited = item['inherited_context']
        await denied_after_changes(item, {'inherited_context': {**inherited, 'reusable_refs': []}})
        await denied_after_changes(item, {'input': {**item['input'], 'intent_mode': 'new_task'}})
        await denied_after_changes(item, {'inherited_context': {**inherited, 'authorization_inherited': True}})
        await denied_after_changes(item, {'inherited_context': {**inherited, 'evidence_only': False}})
    for item in chain[:-1]:
        await denied_after_changes(item, {'status': 'RUNNING'})
        await denied_after_changes(item, {'scope': {**item['scope'], 'account_id': 'another-account'}})
        await denied_after_changes(item, {'conversation_id': 'foreign-conversation'})
    await denied_after_changes(latest, {'input': {**latest['input'], 'material_refs': []}})
    assert (await engine.read_source(latest_scope, alias))['source_material_ref'] == source
    assert len([op for op in engine.store.list('operation') if op['key'] == 'save_product']) == 1

