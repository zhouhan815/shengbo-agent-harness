"""Trusted follow-up evidence reuses a receipted product, never old authority."""
import pytest

from shengbo_contracts.common import MutationContext, canonical_hash
from shengbo_prelive.models import MessageRequest, RunScope
from test_required_analysis_acceptance import env, source_only_facts_run


async def make_follow_up(engine, service, *, depth=1, intent_mode='follow_up', **source_options):
    original_scope, analysis_ref, saved = await source_only_facts_run(engine, service, **source_options)
    original = engine.store.get('run', original_scope.run_id)
    engine.store.update('run', original['id'], status='BLOCKED',
        goal={**original['goal'], 'scope': original['scope']})
    # A terminal predecessor's grant is revoked. Its exact receipt remains
    # readable; follow-up evidence must not depend on reusing write authority.
    await engine.client.command('revoke_grant', {'run_id': original['id'],
        'grant_id': original['grant_id'], 'expected_write_generation': original['write_generation'],
        'reason': '隔离测试原任务已关闭'}, MutationContext(operation_id='revoke-source-origin'),
        run_id=original['id'], controller=True)
    parent_id = original['id']
    for level in range(depth):
        request = MessageRequest(text='继续同一本场，复用正式商品与分析，只完成尚未完成部分，不重复建档或生成语音。',
            client_request_id='source-followup-' + str(level), intent_mode=intent_mode,
            related_run_id=parent_id if intent_mode == 'follow_up' else None)
        result = await engine.submit_message(original['conversation_id'], request)
        current = engine.store.get('run', result['run']['id'])
        engine.store.update('run', current['id'], phase='execute', status='RUNNING', allowed_effects=[],
            goal={'objective': 'prepare_session', 'required_analyses': ['product_facts'], 'scope': current['scope']},
            finish={'outcome': 'COMPLETED', 'summary': '复用已核验来源与精确商品引用完成本场'})
        if level < depth - 1:
            engine.store.update('run', current['id'], status='BLOCKED')
        parent_id = current['id']
    return RunScope(parent_id, 'related-source-check', 'execute', 'main'), original['id'], analysis_ref, saved


@pytest.mark.asyncio
@pytest.mark.parametrize('depth', [1, 2])
async def test_exact_source_facts_reuse_only_trusted_same_preparation_follow_up_chain(env, depth):
    engine, service = env
    scope, origin_id, analysis_ref, saved = await make_follow_up(engine, service, depth=depth)
    original_query = engine.client.query
    receipt_reads = []

    async def record_owner_read(key, *args, **kwargs):
        if key == 'read_operation_receipt':
            receipt_reads.append(kwargs)
        return await original_query(key, *args, **kwargs)

    engine.client.query = record_owner_read
    check = await engine.verify_finish(scope)
    assert check['ok'], check['missing']
    binding = check['analysis_source_bindings'][0]
    assert binding['analysis_ref'] == analysis_ref
    assert binding['product_ref'] == saved['data']['product_ref']
    assert binding['operation_id'] == saved['operation_id']
    assert binding['owner_run_id'] == origin_id
    assert check['checked_operation_ids'] == [], '旧回执不能变成本次写入或扩张commands'
    assert receipt_reads and all(r['run_id'] == origin_id for r in receipt_reads)
    assert all(r['scope'] == engine.store.get('run', origin_id)['goal']['scope'] for r in receipt_reads)
    assert not any(r.get('controller') for r in receipt_reads), '读取祖先证据不继承或提升写入身份'


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['unrelated_same_preparation', 'foreign_conversation', 'different_preparation',
    'missing_ancestor', 'cycle', 'mismatched_related_pointer', 'not_follow_up',
    'claims_authorization_inherited', 'not_evidence_only', 'changed_frozen_scope', 'nonterminal_ancestor',
    'partial_sources', 'unknown_product_save', 'remote_hash_mismatch', 'missing_remote_hash'])
async def test_unproven_old_source_facts_never_satisfy_follow_up_finish(env, invalid):
    engine, service = env
    scope, origin_id, _, saved = await make_follow_up(engine, service,
        intent_mode='new_task' if invalid == 'unrelated_same_preparation' else 'follow_up',
        save_partial=invalid == 'partial_sources')
    current, original = engine.store.get('run', scope.run_id), engine.store.get('run', origin_id)
    inherited = dict(current.get('inherited_context') or {})
    request = dict(current['input'])
    if invalid == 'foreign_conversation':
        engine.store.update('run', origin_id, conversation_id='foreign-conversation')
    elif invalid == 'different_preparation':
        other_scope = {**original['scope'], 'preparation_id': 'another-preparation'}
        engine.store.update('run', origin_id, scope=other_scope, goal={**original['goal'], 'scope': other_scope})
    elif invalid == 'missing_ancestor':
        inherited['related_run_id'] = request['related_run_id'] = 'missing-run'
    elif invalid == 'cycle':
        inherited['related_run_id'] = request['related_run_id'] = scope.run_id
    elif invalid == 'mismatched_related_pointer':
        request['related_run_id'] = 'different-run'
    elif invalid == 'not_follow_up':
        request['intent_mode'] = 'new_task'
    elif invalid == 'claims_authorization_inherited':
        inherited['authorization_inherited'] = True
    elif invalid == 'not_evidence_only':
        inherited['evidence_only'] = False
    elif invalid == 'changed_frozen_scope':
        engine.store.update('run', origin_id, goal={**original['goal'], 'scope': {
            **original['scope'], 'preparation_id': 'untrusted-frozen-scope'}})
    elif invalid == 'nonterminal_ancestor':
        engine.store.update('run', origin_id, status='RUNNING')
    elif invalid == 'unknown_product_save':
        engine.store.update('operation', saved['operation_id'], status='unknown')
    elif invalid in {'remote_hash_mismatch', 'missing_remote_hash'}:
        original_query = engine.client.query

        async def invalid_commitment(key, *args, **kwargs):
            value = await original_query(key, *args, **kwargs)
            if key == 'read_operation_receipt':
                value = {k: v for k, v in value.items() if k != 'request_hash'}
                if invalid == 'remote_hash_mismatch':
                    value['request_hash'] = canonical_hash({'unrelated': 'request'})
            return value

        engine.client.query = invalid_commitment
    if inherited or invalid != 'unrelated_same_preparation':
        engine.store.update('run', scope.run_id, input=request, inherited_context=inherited)
    check = await engine.verify_finish(scope)
    assert not check['ok'], invalid
    assert not check['analysis_source_bindings'], invalid
    assert any('已正式采纳的报告：product_facts' in item for item in check['missing']), check['missing']
