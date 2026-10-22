"""Typed product field scope replaces task-wide price keyword inference."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from shengbo_contracts.business import PRODUCT_FIELD_PATHS
from shengbo_contracts.common import ContractError
from shengbo_prelive.models import IntentAssessment, RunScope
from shengbo_prelive.policy import validate_intent
from test_runtime_business import Product, env, human_command, new_run


async def setup_product(service):
    fields = Product(name='字段范围测试训练器', sku_id='TYPED-SCOPE', price=23.58,
        knowledge_documents=[{'id': 'customization', 'title': '定制说明', 'content': '不支持加工定制。'}]).model_dump()
    return (await human_command(service, 'save_product', {
        'fields': fields, 'patches': [], 'schema_hash': service.schema()['schema_hash'],
    }))['data']['product_ref']


async def freeze(engine, ref, text, paths=None, quote=None, *, selection=None):
    run = await new_run(engine, text)
    if selection:
        run = engine.store.update('run', run['id'], input={**run['input'], 'selection': selection})
    intent = IntentAssessment(objective='catalog_update', summary='按本次明确范围整理商品',
        target_product_ids=[ref['id']], authorization_quote=text,
        requested_product_paths=paths or [], product_field_scope_quote=quote)
    engine.store.update('run', run['id'], intent_assessment=intent.model_dump(mode='json'))
    await engine._freeze_goal(engine.store.get('run', run['id']))
    return engine.store.get('run', run['id']), RunScope(run['id'], 'typed-product-scope', 'execute')


def values(snapshot):
    return {k: deepcopy(v) for k, v in snapshot['body'].items() if k in Product.model_fields}


@pytest.mark.asyncio
async def test_cleanup_with_only_and_preserved_price_does_not_become_price_only(env):
    engine, service = env
    ref = await setup_product(service)
    text = '请更新商品' + ref['id'] + '，只清理基础资料，售价保留23.58元。'
    run, scope = await freeze(engine, ref, text)
    assert run['goal']['requested_product_paths'] == []
    assert run['allowed_product_paths'] == []
    snapshot = await service.query('read_product', {'ref': ref})
    fields = {**values(snapshot), 'knowledge': '不支持加工定制及加印LOGO。'}
    saved = await engine.business_command(scope, 'save_product', {'product_id': ref['id'],
        'expected_revision': ref['revision'], 'schema_hash': service.schema()['schema_hash'], 'fields': fields,
        'patches': [{'path': '/knowledge', 'op': 'set', 'value': fields['knowledge']}]}, 'cleanup-knowledge')
    after = await service.query('read_product', {'ref': saved['data']['product_ref']})
    assert after['body']['price'] == 23.58
    assert after['body']['knowledge'] == fields['knowledge']


@pytest.mark.asyncio
async def test_explicit_typed_price_only_allows_price_and_denies_knowledge(env):
    engine, service = env
    ref = await setup_product(service)
    quote = '只修改零售价，其余字段不动'
    text = '请更新商品' + ref['id'] + '，' + quote + '。'
    run, scope = await freeze(engine, ref, text, ['/price'], quote)
    assert run['allowed_product_paths'] == ['/price']
    assert run['goal']['product_field_scope_quote'] == quote
    current = await service.query('read_product', {'ref': ref})
    fields = {**values(current), 'price': 24.58}
    saved = await engine.business_command(scope, 'save_product', {'product_id': ref['id'],
        'expected_revision': ref['revision'], 'schema_hash': service.schema()['schema_hash'], 'fields': fields,
        'patches': [{'path': '/price', 'op': 'set', 'value': 24.58}]}, 'price-only')
    current = await service.query('read_product', {'ref': saved['data']['product_ref']})
    fields = {**values(current), 'knowledge': '不支持加印LOGO。'}
    with pytest.raises(ContractError, match='FIELD_SCOPE_DENIED'):
        await engine.business_command(scope, 'save_product', {'product_id': ref['id'],
            'expected_revision': current['ref']['revision'], 'schema_hash': service.schema()['schema_hash'], 'fields': fields,
            'patches': [{'path': '/knowledge', 'op': 'set', 'value': fields['knowledge']}]}, 'price-only-denied')


@pytest.mark.asyncio
async def test_exact_ui_field_remains_narrower_than_typed_product_paths(env):
    engine, service = env
    ref = await setup_product(service)
    quote = '只允许改价格和知识说明'
    text = '更新商品' + ref['id'] + '，' + quote + '，这次操作左侧选中的知识说明。'
    run, scope = await freeze(engine, ref, text, ['/price', '/knowledge'], quote,
        selection={'target': {'object_ref': ref, 'field_path': '/knowledge'}})
    assert run['allowed_product_paths'] == ['/knowledge']
    snapshot = await service.query('read_product', {'ref': ref})
    with pytest.raises(ContractError, match='FIELD_SCOPE_DENIED'):
        await engine.business_command(scope, 'save_product', {'product_id': ref['id'],
            'expected_revision': ref['revision'], 'schema_hash': service.schema()['schema_hash'],
            'fields': {**values(snapshot), 'price': 25.58},
            'patches': [{'path': '/price', 'op': 'set', 'value': 25.58}]}, 'ui-stays-narrow')


def test_product_field_path_vocabulary_matches_actual_product_schema():
    assert set(PRODUCT_FIELD_PATHS) == {'/' + name for name in Product.model_fields}


@pytest.mark.parametrize('case', ['missing_quote', 'material_quote', 'no_save_objective', 'quote_without_paths'])
def test_product_field_scope_requires_original_user_basis_and_existing_save_capability(case):
    text = '请更新这个商品，只改价格。'
    raw = {'objective': 'catalog_update', 'summary': '字段范围', 'authorization_quote': text,
           'requested_product_paths': ['/price'], 'product_field_scope_quote': '只改价格'}
    if case == 'missing_quote': raw['product_field_scope_quote'] = None
    if case == 'material_quote': raw['product_field_scope_quote'] = '附件说只修改售价'
    if case == 'no_save_objective': raw['objective'] = 'read_explain'
    if case == 'quote_without_paths': raw['requested_product_paths'] = []
    with pytest.raises(ContractError, match='INTENT_PRODUCT_FIELD_SCOPE_INVALID'):
        validate_intent({'input': {'text': text}}, IntentAssessment.model_validate(raw))


@pytest.mark.parametrize('paths', [['/id'], ['/knowledge_documents/0/content'], ['/price', '/price']])
def test_only_unique_real_top_level_paths_are_valid_intent_fields(paths):
    with pytest.raises(ValidationError):
        IntentAssessment(objective='catalog_update', summary='无效字段', authorization_quote='请更新',
            requested_product_paths=paths, product_field_scope_quote='请更新')

