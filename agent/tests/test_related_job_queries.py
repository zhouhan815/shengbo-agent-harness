"""Follow-up intent can reconcile a proven ancestor job without old grants."""
from copy import deepcopy
import pytest

from shengbo_contracts.business import MODULE_KEYS
from shengbo_contracts.common import ContractError, MutationContext, canonical_hash
from shengbo_prelive.models import MessageRequest, RunScope
from test_runtime_business import env, human_command, new_run, prepared_text


async def ancestor_job_case(engine, service, kind='scripts', *, depth=1, related=True):
    room, prep_ref, _ = await prepared_text(service)
    prep = await service.query('read_preparation', {'preparation_id': prep_ref['id']})
    target_refs = prep['body']['selected_product_refs']
    target_ids = [ref['id'] for ref in target_refs]
    origin = await new_run(engine, '为明确商品' + '、'.join(target_ids) + '准备本场后继续核实真实后台任务',
        room_id=room['id'], preparation_id=prep_ref['id'])
    command = 'submit_script_change' if kind == 'scripts' else 'prepare_required_audio'
    grant = await engine.client.command('issue_grant', {'run_id': origin['id'], 'scope': origin['scope'],
        'capabilities': [command], 'target_constraints': []}, MutationContext(operation_id='owned-job-grant'),
        run_id=origin['id'], controller=True)
    engine.store.update('run', origin['id'], phase='execute', status='RUNNING', allowed_effects=[command],
        target_product_ids=target_ids, action_target_refs=target_refs,
        goal={'objective': 'prepare_session', 'scope': origin['scope'],
              'target_product_ids': target_ids, 'action_target_refs': target_refs},
        grant_id=grant['data']['grant_id'], write_generation=grant['data']['write_generation'])
    scope = RunScope(origin['id'], 'create-owned-job', 'execute', 'main')
    if kind == 'scripts':
        plan = await human_command(service, 'save_script_plan', {'preparation_id': prep_ref['id'],
            'expected_revision': prep['ref']['revision'], 'bundle_ref': prep['body']['bundle_ref'],
            'items': [{'product_ref': prep['body']['selected_product_refs'][0], 'variant_id': 'ancestor-owned-variant',
                'modules': [{'key': key, 'action': 'disable' if key in {'campaign', 'transition'} else 'generate'} for key in MODULE_KEYS]}]})
        args = {'preparation_id': prep_ref['id'], 'expected_revision': plan['data']['preparation_ref']['revision'],
            'plan_ref': plan['data']['script_plan_ref'], 'context_bundle_ref': prep['body']['bundle_ref'],
            'expected_generation_epoch': plan['data']['generation_epoch']}
    else:
        review = await service.query('create_text_review', {'preparation_id': prep_ref['id']})
        approval = await human_command(service, 'approve_text_for_audio', {'preparation_id': prep_ref['id'],
            'expected_revision': prep['ref']['revision'], 'review_hash': review['review_hash']})
        args = {'preparation_id': prep_ref['id'], 'expected_revision': approval['data']['preparation_ref']['revision'],
            'approval_ref': approval['data']['approval_ref'], 'review_hash': review['review_hash'],
            'expected_generation_epoch': approval['data']['generation_epoch']}
    receipt = await engine.business_command(scope, command, args, 'owned-job-submit')
    # Scripts use the fake adapter; audio's fake adapter fails deliberately.
    # Both exercise actual durable receipts, never a model or TTS provider.
    await service.drain_jobs()
    await engine.client.command('revoke_grant', {'run_id': origin['id'], 'grant_id': grant['data']['grant_id'],
        'expected_write_generation': grant['data']['write_generation'], 'reason': '原任务已关闭'},
        MutationContext(operation_id='revoke-owned-job'), run_id=origin['id'], controller=True)
    engine.store.update('run', origin['id'], status='BLOCKED')
    parent_id = origin['id']
    for level in range(depth):
        result = await engine.submit_message(origin['conversation_id'], MessageRequest(
            text='继续同一本场，先核对已有后台任务结果，不重新调用未知供应商。',
            intent_mode='follow_up' if related else 'new_task', related_run_id=parent_id if related else None,
            client_request_id='read-ancestor-job-' + str(level)))
        parent_id = result['run']['id']
        if level < depth - 1:
            # A real failed intent has no frozen Goal or write grant.
            engine.store.update('run', parent_id, status='BLOCKED', phase='intent')
    return RunScope(parent_id, 'read-owned-ancestor-job', 'intent', 'main'), origin, receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,depth', [('scripts', 1), ('scripts', 2), ('audio', 1), ('audio', 2)])
async def test_follow_up_intent_reads_proven_ancestor_jobs_across_unfrozen_intent_failure(env, kind, depth):
    engine, service = env
    scope, origin, receipt = await ancestor_job_case(engine, service, kind, depth=depth)
    result = await engine.business_query(scope, 'read_job', {'job_id': receipt['data']['job_id']})
    assert result['body']['id'] == receipt['data']['job_id']
    assert result['body']['kind'] == kind
    assert result['body']['run_id'] == origin['id']
    assert result['body']['preparation_id'] == origin['scope']['preparation_id']
    assert result['ref']['id'] == receipt['data']['job_id']
    assert 'context' not in result['body'] and 'principal' not in result['body']
    snapshot = engine.run_history(scope, {'full': True, 'ref': result['full_ref']})
    assert snapshot['run_id'] == scope.run_id
    assert snapshot['document']['job']['body']['run_id'] == origin['id']
    assert not engine.store.get('run', scope.run_id).get('grant_id'), 'intent只读不继承旧grant'


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['nonancestor', 'foreign_preparation', 'fake_job_id', 'fake_job_ref',
    'local_request_hash', 'remote_request_hash', 'remote_receipt', 'body_id', 'body_owner', 'body_preparation', 'ref_kind'])
async def test_ancestor_job_query_rejects_unproven_receipts_and_mismatched_owner_output(env, invalid):
    engine, service = env
    scope, origin, receipt = await ancestor_job_case(engine, service, related=invalid != 'nonancestor')
    job_id = receipt['data']['job_id']
    if invalid in {'fake_job_id', 'fake_job_ref', 'foreign_preparation'}:
        op = engine.store.get('operation', receipt['operation_id'])
        changed = {**op['receipt'], 'data': dict(op['receipt']['data'])}
        if invalid == 'fake_job_id':
            changed['data']['job_id'] = 'unrelated-job'
        elif invalid == 'fake_job_ref':
            changed['data']['job_ref'] = {**changed['data']['job_ref'], 'id': 'unrelated-job'}
        else:
            args = {**op['args'], 'preparation_id': 'other-preparation'}
            engine.store.update('operation', op['id'], args=args, request_hash=canonical_hash(args))
        engine.store.update('operation', op['id'], receipt=changed)
    elif invalid == 'local_request_hash':
        engine.store.update('operation', receipt['operation_id'], request_hash=canonical_hash({'wrong': 'args'}))
    elif invalid in {'remote_request_hash', 'remote_receipt', 'body_id', 'body_owner', 'body_preparation', 'ref_kind'}:
        query = engine.client.query

        async def corrupted_response(key, *args, **kwargs):
            result = await query(key, *args, **kwargs)
            if key == 'read_operation_receipt' and invalid == 'remote_request_hash':
                return {**result, 'request_hash': canonical_hash({'wrong': 'remote request'})}
            if key == 'read_operation_receipt' and invalid == 'remote_receipt':
                return {**result, 'receipt': {**result['receipt'], 'operation_id': 'different-operation'}}
            if key == 'read_job':
                result = {**result, 'body': dict(result['body']), 'ref': dict(result['ref'])}
                if invalid == 'body_id': result['body']['id'] = 'other-job'
                if invalid == 'body_owner': result['body']['run_id'] = scope.run_id
                if invalid == 'body_preparation': result['body']['preparation_id'] = 'other-preparation'
                if invalid == 'ref_kind': result['ref']['kind'] = 'unrelated-artifact'
            return result

        engine.client.query = corrupted_response
    with pytest.raises(ContractError):
        await engine.business_query(scope, 'read_job', {'job_id': job_id})


@pytest.mark.asyncio
async def test_ancestor_unknown_provider_outcome_is_not_hidden_by_current_run_observation(env):
    engine, service = env
    scope, origin, receipt = await ancestor_job_case(engine, service, depth=2)
    job_id = receipt['data']['job_id']
    # State-only fixture: no supplier request is issued or repeated.
    with service.store.transaction(write=True) as db:
        saved = service.store.get(db, 'prelive_job', job_id)
        body = deepcopy(saved['body'])
        body['status'] = 'interrupted'
        body['items'][0].update(status='interrupted', call_state='timeout_unknown',
            error={'code': 'EXTERNAL_OUTCOME_UNKNOWN'})
        service.store.put(db, 'prelive_job', job_id, body, expected=saved['ref']['revision'])
    current = engine.store.get('run', scope.run_id)
    direct = await engine.client.query('read_completion_observation',
        {'preparation_id': origin['scope']['preparation_id']}, scope=current['scope'], run_id=scope.run_id)
    assert job_id not in direct['unknown_job_ids'], '后端原查询按current run过滤，测试应命中这个缺口'
    await engine.business_query(scope, 'read_job', {'job_id': job_id})
    projection = await engine.project_context(scope)
    projected = next(job for job in projection['unknown_side_effects']['related_jobs']['items']
        if job['body']['id'] == job_id)
    assert projected['body']['run_id'] == origin['id']
    assert projected['body']['items'][0]['call_state'] == 'timeout_unknown'
    assert not engine.store.get('run', scope.run_id).get('grant_id')
    joined = await engine._completion_observation(current)
    assert job_id in joined['unknown_job_ids']
    assert any(job['body']['id'] == job_id and job['body']['run_id'] == origin['id'] for job in joined['jobs'])
    engine.store.update('run', scope.run_id, phase='execute', goal={
        'objective': 'audio_prepare', 'scope': current['scope']},
        finish={'outcome': 'COMPLETED', 'summary': '不能忽略祖先未知结果'})
    check = await engine.verify_finish(RunScope(scope.run_id, 'verify-ancestor-unknown', 'execute'))
    assert not check['ok']
    assert '业务任务尚未成功结束或结果未知' in check['missing']
