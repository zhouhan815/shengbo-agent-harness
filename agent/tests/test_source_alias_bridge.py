"""Exact upload/voice aliases backed by real accepted reports and MaterialStore."""
from copy import deepcopy

import pytest

from shengbo_contracts.business import COMMAND_ARGS
from shengbo_contracts.common import ContractError, MutationContext, canonical_hash
from shengbo_prelive.models import RunScope
from test_runtime_business import Product, env, human_command, new_run


async def accepted_sources(engine, service, *, new_run_after_acceptance=False):
    room = (await human_command(service, 'create_room', {'name': '精确资料别名测试'}))['data']['room_ref']
    prep = (await human_command(service, 'create_preparation', {'room_id': room['id'], 'room_revision': room['revision']}))['data']['preparation_ref']
    run = await new_run(engine, '依据上传资料新建商品并准备本场，不生成语音', room_id=room['id'], preparation_id=prep['id'])
    sources = [engine.materials.accept(run['conversation_id'], str(index) + '.md', 'text/markdown',
        ('# 精确商家资料\n第' + str(index) + '份，129元；不要把活动价写成常规价。').encode())['material_ref'] for index in range(2)]
    effects = ['import_blob', 'accept_analysis_report', 'save_product', 'save_asset_checklist', 'save_context_bundle']

    async def authorize(value):
        grant = await engine.client.command('issue_grant', {'run_id': value['id'], 'scope': value['scope'],
            'capabilities': effects, 'target_constraints': []}, MutationContext(operation_id=value['id'] + '-grant'), run_id=value['id'], controller=True)
        return engine.store.update('run', value['id'], phase='execute', status='RUNNING', allowed_effects=effects,
            input={**value['input'], 'material_refs': sources}, target_product_ids=[], created_product_ids=[], receipts=[],
            task_assessment={'summary': '按当前资料进行来源复核', 'capabilities': ['product_facts']},
            goal={'objective': 'prepare_session', 'scope': value['scope'], 'required_analyses': ['product_facts']},
            grant_id=grant['data']['grant_id'], write_generation=grant['data']['write_generation'])

    run = await authorize(run)
    scope = RunScope(run['id'], 'source-alias-main', 'execute')
    branch_id = 'alias-facts-branch'
    engine.store.put('branch', {'id': branch_id, 'run_id': run['id'], 'role': 'product_facts', 'status': 'RUNNING',
        'task': {'role': 'product_facts', 'instruction': '整理原资料', 'product_ids': [], 'source_refs': sources, 'input_refs': []}})
    engine.submit_report(RunScope(run['id'], 'source-alias-worker', 'execute', 'product_facts', branch_id), {'summary': '原始资料事实候选'})
    analysis_ref = (await engine.accept_reports(scope, [branch_id], 'accept-exact-source'))['accepted_refs'][0]
    report = await service.query('read_analysis_report', {'ref': analysis_ref})
    business_sources = report['body']['report']['source_refs']
    if new_run_after_acceptance:
        engine.store.update('run', run['id'], status='BLOCKED')
        run = engine.store.put('run', {**run, 'id': 'fresh-source-alias-run', 'grant_id': None, 'write_generation': None})
        run = await authorize(run)
        scope = RunScope(run['id'], 'new-run-source-alias-main', 'execute')
    return run, scope, sources, business_sources, analysis_ref


@pytest.mark.asyncio
@pytest.mark.parametrize('new_run_after_acceptance', [False, True])
async def test_real_accepted_aliases_normalize_product_and_field_sources_before_hash_and_replay(env, new_run_after_acceptance):
    engine, service = env
    run, scope, originals, business, analysis_ref = await accepted_sources(engine, service, new_run_after_acceptance=new_run_after_acceptance)
    fields = Product(name='来源规范化测试商品', price=129).model_dump()
    args = {'schema_hash': service.schema()['schema_hash'], 'patches': [], 'fields': fields, 'source_manifest_refs': originals,
        'field_dispositions': [{'path': '/' + key, 'action': 'set', 'knowledge': 'known' if value not in (None, '', []) else 'unknown',
            'source_refs': [{key: value for key, value in ref.items() if key != 'schema_version'} for ref in originals], 'reason': '已核验商家原始资料'} for key, value in fields.items()]}
    saved = await engine.business_command(scope, 'save_product', args, 'same-source-product-save')
    op = engine.store.get('operation', saved['operation_id'])
    assert op['args']['source_manifest_refs'] == business
    assert all(item['source_refs'] == business for item in op['args']['field_dispositions'])
    assert op['request_hash'] == canonical_hash(op['args'])
    analysis = (await service.query('read_analysis_report', {'ref': analysis_ref}))['body']
    binding = await engine._verify_source_only_facts_binding(engine.store.get('run', run['id']), analysis, [saved['data']['product_ref']], [op])
    assert binding and binding['operation_id'] == saved['operation_id']
    equivalent = deepcopy(args)
    equivalent['source_manifest_refs'] = business
    for item in equivalent['field_dispositions']:
        item['source_refs'] = business
    assert await engine.business_command(scope, 'save_product', equivalent, 'same-source-product-save') == saved
    assert len([item for item in engine.store.list('operation') if item['run_id'] == scope.run_id and item['key'] == 'save_product']) == 1
    for original, alias in zip(originals, business):
        direct = await engine.read_source(scope, original, 'text:1')
        via_alias = await engine.read_source(scope, alias, 'text:1')
        assert via_alias['text'] == direct['text'] and via_alias['manifest'] == direct['manifest']
        assert via_alias['source_material_ref'] == original and via_alias['business_source_ref'] == alias
    # Only declared Ref slots change; arbitrary facts containing a ref-shaped
    # value remain byte-for-byte data, without being interpreted as authority.
    payload = COMMAND_ARGS['save_product'].model_validate({**args, 'fields': {**fields, 'extra_fact': {'source_ref': originals[0]}}})
    normalized = await engine._normalize_business_source_refs(scope, payload)
    assert normalized['fields']['extra_fact'] == {'source_ref': originals[0]}


@pytest.mark.asyncio
async def test_aliases_rebind_typed_asset_bundle_and_dispatch_slots_without_widening_child_scope(env):
    engine, service = env
    run, scope, originals, business, analysis_ref = await accepted_sources(engine, service)
    prep_id = run['scope']['preparation_id']
    prep = await service.query('read_preparation', {'preparation_id': prep_id})
    saved = await engine.business_command(scope, 'save_asset_checklist', {'preparation_id': prep_id, 'expected_revision': prep['ref']['revision'],
        'checklist': {'items': [{'item_id': 'source-photo', 'purpose': '原始资料索引', 'recommended_content': '待商家提供展示照片', 'source_refs': originals}], 'notes': []}}, 'asset-alias')
    checklist = await service.query('read_asset_checklist', {'ref': saved['data']['asset_checklist_ref']})
    assert checklist['body']['checklist']['items'][0]['source_refs'] == business
    prep = await service.query('read_preparation', {'preparation_id': prep_id})
    draft = await human_command(service, 'save_session_draft', {'preparation_id': prep_id, 'expected_revision': prep['ref']['revision'], 'draft': {'theme': '来源桥接'}})
    bundle = await engine.business_command(scope, 'save_context_bundle', {'preparation_id': prep_id, 'expected_revision': draft['data']['preparation_ref']['revision'],
        'product_refs': [], 'analysis_refs': [analysis_ref], 'draft_ref': draft['data']['draft_ref'], 'source_refs': originals,
        'user_decisions': [{'source_ref': originals[0]}]}, 'bundle-alias')
    body = (await service.query('read_context', {'ref': bundle['data']['context_bundle_ref']}))['body']
    assert body['source_refs'] == business and body['user_decisions'] == [{'source_ref': originals[0]}]
    dispatched = await engine.dispatch(scope, [{'role': 'product_facts', 'instruction': '仅复核分配的第一份原资料', 'source_refs': [business[0]]}], '按当前授权资料定向复核', 'dispatch-alias')
    branch_id = dispatched['branch_task_ids'][0]
    branch = engine.store.get('branch', branch_id)
    assert branch['task']['source_refs'] == originals[:1]
    engine.store.update('branch', branch_id, status='RUNNING')
    child = RunScope(run['id'], 'child-alias', 'execute', 'product_facts', branch_id)
    assert (await engine.read_source(child, business[0]))['source_material_ref'] == originals[0]
    with pytest.raises(ContractError) as denied:
        await engine.read_source(child, business[1])
    assert denied.value.status == 403


@pytest.mark.asyncio
async def test_aliases_require_current_selected_accepted_report_exact_input_and_scope(env):
    engine, service = env
    run, scope, originals, business, analysis_ref = await accepted_sources(engine, service)
    foreign = engine.materials.accept('other-conversation', 'foreign.txt', 'text/plain', b'foreign')['material_ref']
    for ref in [foreign, {**business[0], 'revision': 2}, {**business[0], 'content_hash': 'sha256:forged'}]:
        with pytest.raises(ContractError) as denied:
            await engine.read_source(scope, ref)
        assert denied.value.status == 403
    engine.store.update('run', run['id'], input={**run['input'], 'material_refs': originals[1:]})
    with pytest.raises(ContractError) as denied:
        await engine.read_source(scope, business[0])
    assert denied.value.status == 403
    engine.store.update('run', run['id'], input=run['input'])
    # A cached branch mapping is insufficient once the report is no longer
    # selected by the current preparation.
    with service.store.transaction(write=True) as db:
        prep = service.store.get(db, 'preparation', run['scope']['preparation_id'])
        service.store.put(db, 'preparation', prep['ref']['id'], {**prep['body'], 'analysis_refs': []}, expected=prep['ref']['revision'])
    with pytest.raises(ContractError) as denied:
        await engine.read_source(scope, business[0])
    assert denied.value.status == 403
    with pytest.raises(ContractError, match='REPORT_DEPENDENCY_NOT_READY'):
        await engine.business_command(scope, 'save_product', {'schema_hash': service.schema()['schema_hash'], 'fields': Product(name='不得写入', price=1).model_dump(),
            'patches': [], 'source_manifest_refs': originals}, 'no-selected-proof')
    assert not [item for item in engine.store.list('operation') if item['key'] == 'save_product']


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['other_preparation', 'not_accepted', 'changed_lineage'])
async def test_selected_report_alias_proof_requires_actual_acceptance_scope_and_lineage(env, invalid):
    engine, service = env
    run, scope, originals, business, analysis_ref = await accepted_sources(engine, service)
    body = (await service.query('read_analysis_report', {'ref': analysis_ref}))['body']
    if invalid == 'other_preparation':
        body['preparation_id'] = 'some-other-preparation'
    elif invalid == 'not_accepted':
        body['accepted'] = False
    else:
        body['lineage']['prepared_report_hash'] = 'sha256:unverified-original'
    with service.store.transaction(write=True) as db:
        invalid_ref = service.store.artifact(db, 'analysis', body)
        prep = service.store.get(db, 'preparation', run['scope']['preparation_id'])
        service.store.put(db, 'preparation', prep['ref']['id'], {**prep['body'], 'analysis_refs': [invalid_ref]}, expected=prep['ref']['revision'])
    with pytest.raises(ContractError) as denied:
        await engine.read_source(scope, business[0])
    assert denied.value.status == 403
    assert not [item for item in engine.store.list('operation') if item['key'] == 'save_product']
