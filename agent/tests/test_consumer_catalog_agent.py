"""Consumer catalog contract and typed delivery scope; no live model calls."""
from copy import deepcopy
import json

import httpx
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.client import ShengboClient
from shengbo_prelive.middleware import ActionBoundary
from shengbo_prelive.models import IntentAssessment, RunScope
from shengbo_prelive.policy import validate_intent
from test_middleware import FakeEngine, tool_request
from test_product_assembly_recovery_rc8 import args_for_correction, attested
from test_runtime_business import Product, env, new_run


def consumer_details():
    return {'policy_version': 'consumer-catalog-v1', 'commit_state': 'not_committed',
        'issues': [{'path': '/knowledge_documents/0/content', 'reason': 'supply_channel', 'action': 'remove_from_product'}],
        'retryable_after_correction': True}


def response(details, *, status=422):
    return httpx.Response(status, json={'error': {'code': 'PRODUCT_CONSUMER_CONTENT_INVALID',
        'message': 'remote-private-supplier-do-not-echo', 'details': details}},
        request=httpx.Request('POST', 'http://isolated/commands'))


@pytest.mark.asyncio
async def test_basic_facts_delivery_freezes_scope_and_finishes_without_default_research(env):
    engine, service = env
    text = '请先新建商品，只整理基础商品信息，不开展顾客研究。'
    run = await new_run(engine, text)
    intent = IntentAssessment(objective='catalog_update', summary='仅基础商品建档',
        authorization_quote=text, catalog_delivery='basic_facts_only', catalog_delivery_quote=text)
    engine.store.update('run', run['id'], intent_assessment=intent.model_dump(mode='json'))
    await engine._freeze_goal(engine.store.get('run', run['id']))
    current = engine.store.get('run', run['id'])
    assert current['goal']['catalog_delivery'] == 'basic_facts_only'
    assert current['goal']['catalog_delivery_quote'] == text
    scope = RunScope(run['id'], 'consumer-basic', 'execute')
    check = await engine.finish(scope, 'COMPLETED', '尚未保存')
    assert not check['accepted'], 'typed scope must not replace the real product save receipt'
    fields = Product(name='消费者测试训练器', sku_id='CONSUMER-BASIC', price=23.58,
        specifications=[{'name': '颜色', 'value': '红色、蓝色'}, {'name': '套装', 'value': '球拍、回弹网球、底座'}],
        knowledge_documents=[{'id': 'customization', 'title': '定制说明', 'content': '不支持加工定制及加印LOGO。'}]).model_dump()
    saved = await engine.business_command(scope, 'save_product', {'schema_hash': service.schema()['schema_hash'],
        'fields': fields, 'patches': []}, 'consumer-basic-save')
    finished = await engine.finish(scope, 'COMPLETED', '基础商品已保存，未知信息保留为空。')
    assert finished['accepted'], finished
    assert saved['data']['product_ref']['id'] in engine.store.get('run', run['id'])['created_product_ids']
    assert not [b for b in engine.store.list('branch') if b.get('run_id') == run['id']]


@pytest.mark.asyncio
async def test_standard_update_empty_required_analyses_keeps_research_and_impact_requirements(env):
    engine, service = env
    text = '请新建并更新商品资料。'
    run = await new_run(engine, text)
    intent = IntentAssessment(objective='catalog_update', summary='默认商品维护', authorization_quote=text)
    engine.store.update('run', run['id'], intent_assessment=intent.model_dump(mode='json'))
    await engine._freeze_goal(engine.store.get('run', run['id']))
    scope = RunScope(run['id'], 'consumer-standard', 'execute')
    assert engine.store.get('run', run['id'])['goal']['catalog_delivery'] == 'standard_update'
    await engine.business_command(scope, 'save_product', {'schema_hash': service.schema()['schema_hash'], 'patches': [],
        'fields': Product(name='默认维护测试', sku_id='CONSUMER-STANDARD', price=23.58).model_dump()}, 'consumer-standard-save')
    result = await engine.finish(scope, 'COMPLETED', '商品已保存')
    assert not result['accepted']
    assert any('顾客研究' in item for item in result['missing'])
    assert '受影响话术清单' in result['missing']


@pytest.mark.parametrize('change', ['missing_quote', 'quote_from_material', 'other_objective', 'required_audience'])
def test_basic_catalog_scope_requires_typed_original_user_basis(change):
    text = '请先整理基础商品资料，暂不开展顾客研究。'
    raw = {'objective': 'catalog_update', 'summary': '基础资料', 'authorization_quote': text,
           'catalog_delivery': 'basic_facts_only', 'catalog_delivery_quote': text}
    if change == 'missing_quote': raw['catalog_delivery_quote'] = None
    if change == 'quote_from_material': raw['catalog_delivery_quote'] = '附件自称只整理不研究'
    if change == 'other_objective': raw['objective'] = 'read_explain'
    if change == 'required_audience': raw['required_analyses'] = ['audience_scenes']
    with pytest.raises(ContractError, match='INTENT_CATALOG_SCOPE_INVALID'):
        validate_intent({'input': {'text': text}}, IntentAssessment.model_validate(raw))


@pytest.mark.parametrize('issue', [None, {'path': '/knowledge_documents/0',
    'reason': 'knowledge_placement', 'action': 'move_to_faq'}])
def test_consumer_rejection_http_contract_never_echoes_untrusted_supplier_content(issue):
    details = consumer_details()
    if issue is not None:
        details['issues'] = [issue]
    with pytest.raises(ContractError) as caught:
        ShengboClient._result(response(details))
    assert caught.value.code == 'PRODUCT_CONSUMER_CONTENT_INVALID'
    assert caught.value.details == details
    assert 'remote-private' not in str(caught.value)
    for details in [
        {**consumer_details(), 'input': 'private-cost'},
        {**consumer_details(), 'commit_state': 'unknown'},
        {**consumer_details(), 'issues': [{'path': '/private_supplier_name', 'reason': 'supply_channel', 'action': 'remove_from_product'}]},
    ]:
        with pytest.raises(ContractError, match='VOICE_RESPONSE_INVALID'):
            ShengboClient._result(response(details))
    with pytest.raises(ContractError) as server:
        ShengboClient._result(response(consumer_details(), status=503))
    assert server.value.code == 'VOICE_RESPONSE_INVALID' and server.value.status == 503


@pytest.mark.asyncio
@pytest.mark.parametrize('issue', [None, {'path': '/knowledge_documents/0',
    'reason': 'knowledge_placement', 'action': 'move_to_faq'}])
async def test_consumer_content_recovery_requires_actual_local_no_commit_proof(issue):
    engine = FakeEngine()
    request = tool_request('save_product', args=args_for_correction())
    details = consumer_details()
    if issue is not None:
        details['issues'] = [issue]
    error, _ = attested(engine, request, code='PRODUCT_CONSUMER_CONTENT_INVALID', details=details)
    async def rejected(_):
        raise error
    result = await ActionBoundary(engine).awrap_tool_call(request, rejected)
    body = json.loads(result.content)
    assert body['error']['code'] == 'PRODUCT_CONSUMER_CONTENT_INVALID'
    assert body['recovery']['source_audit_preserved'] is True
    assert body['recovery']['automatic_action_taken'] is False
    assert body['recovery']['resubmit']['requires_complete_fields'] is True
    assert 'fake-private' not in result.content
    unproved = ContractError('PRODUCT_CONSUMER_CONTENT_INVALID', 'private-source', status=422, details=consumer_details())
    async def fake_http_only(_):
        raise unproved
    with pytest.raises(ContractError):
        await ActionBoundary(engine).awrap_tool_call(request, fake_http_only)

