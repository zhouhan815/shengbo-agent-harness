"""Formal variant coverage across real isolated plans/jobs/imports; no paid calls."""
from copy import deepcopy

import pytest

from shengbo_contracts.business import MODULE_KEYS
from shengbo_contracts.common import canonical_hash
from shengbo_prelive.models import IntentAssessment, RunScope
from test_runtime_business import env, human_command, new_run, product


async def setup_task(engine, service, *, mode='generate', count=2):
    target = await product(service, '分批版本验收商品')
    room = (await human_command(service, 'create_room', {'name': '分批版本隔离直播间'}))['data']['room_ref']
    prep = (await human_command(service, 'create_preparation', {'room_id': room['id'],
        'room_revision': room['revision']}))['data']['preparation_ref']
    prep = (await human_command(service, 'update_preparation_products', {'preparation_id': prep['id'],
        'expected_revision': prep['revision'], 'selected_product_refs': [target]}))['data']['preparation_ref']
    draft = await human_command(service, 'save_session_draft', {'preparation_id': prep['id'],
        'expected_revision': prep['revision'], 'draft': {'theme': '两份独立新稿', 'commerce': {}}})
    bundle = await human_command(service, 'save_context_bundle', {'preparation_id': prep['id'],
        'expected_revision': draft['data']['preparation_ref']['revision'], 'product_refs': [target],
        'draft_ref': draft['data']['draft_ref']})
    instruction = (f"为商品{target['id']}导入原稿后扩展两版话术，只要文本，不生成语音" if mode == 'expand_original'
        else f"为商品{target['id']}生成两版话术，只要文本，不生成语音")
    run = await new_run(engine, instruction, room_id=room['id'], preparation_id=prep['id'])
    source = None
    text = '原稿：欢迎了解这款商品。请按实际需求选择。'
    if mode == 'expand_original':
        source = engine.materials.accept(run['conversation_id'], 'merchant-original.txt', 'text/plain',
            text.encode())['material_ref']
        run = engine.store.update('run', run['id'], input={**run['input'], 'material_refs': [source]})
    proposal = IntentAssessment(objective='script_change', script_mode=mode, summary='两份新版本的实际保存验收',
        target_product_ids=[target['id']], requested_script_variants=[{'product_id': target['id'], 'count': count}],
        authorization_quote=instruction)
    run = engine.store.update('run', run['id'], intent_assessment=proposal.model_dump())
    await engine._freeze_goal(run)
    run = engine.store.get('run', run['id'])
    assert run['phase'] == 'execute', engine.store.list('question')
    scope = RunScope(run['id'], 'split-script-coverage', 'execute')
    original = None
    if source:
        current = await service.query('read_preparation', {'preparation_id': prep['id']})
        receipt = await engine.business_command(scope, 'import_script', {'preparation_id': prep['id'],
            'expected_revision': current['ref']['revision'], 'product_ref': target,
            'variant_id': 'merchant-original', 'source_ref': source, 'locator': 'full', 'text': text}, 'import-original')
        original = receipt['data']['script_ref']
    return {'run_id': run['id'], 'scope': scope, 'product_ref': target, 'preparation_id': prep['id'],
        'bundle_ref': bundle['data']['context_bundle_ref'], 'original_script_ref': original, 'sequence': 0}


async def generate_one(engine, service, task, variant, *, base_ref=None):
    task['sequence'] += 1
    sequence = task['sequence']
    prep = await service.query('read_preparation', {'preparation_id': task['preparation_id']})
    item = {'product_ref': task['product_ref'], 'variant_id': variant, 'modules': [
        {'key': key, 'action': 'disable' if key in {'opening_short', 'campaign', 'transition'} else 'generate'}
        for key in MODULE_KEYS]}
    if task['original_script_ref']:
        item['original_script_ref'] = task['original_script_ref']
    if base_ref:
        item['base_script_ref'] = base_ref
    plan = await engine.business_command(task['scope'], 'save_script_plan', {
        'preparation_id': task['preparation_id'], 'expected_revision': prep['ref']['revision'],
        'bundle_ref': task['bundle_ref'], 'items': [item]}, f'plan-{sequence}')
    submitted = await engine.business_command(task['scope'], 'submit_script_change', {
        'preparation_id': task['preparation_id'], 'expected_revision': plan['data']['preparation_ref']['revision'],
        'plan_ref': plan['data']['script_plan_ref'], 'context_bundle_ref': task['bundle_ref'],
        'expected_generation_epoch': plan['data']['generation_epoch']}, f'generate-{sequence}')
    await service.drain_jobs()
    job = await service.query('read_job', {'job_id': submitted['data']['job_id']})
    return job['body']


async def scripts(service, task):
    prep = await service.query('read_preparation', {'preparation_id': task['preparation_id']})
    result = [await service.query('read_script', {'ref': ref}) for ref in prep['body']['script_refs']]
    for script in result:
        assert any(module.get('sentences') for module in script['modules'].values() if module)
    return result


async def finish(engine, task):
    return await engine.finish(task['scope'], 'COMPLETED', '按本次要求核验正式版本及真实正文。')


@pytest.mark.asyncio
async def test_two_sequential_plan_jobs_count_both_current_formal_variants(env):
    engine, service = env
    task = await setup_task(engine, service)
    first = await generate_one(engine, service, task, 'new-v1')
    second = await generate_one(engine, service, task, 'new-v2')
    assert first['status'] == second['status'] == 'succeeded'
    formal = await scripts(service, task)
    assert {value['body']['variant_id'] for value in formal} == {'new-v1', 'new-v2'}
    result = await finish(engine, task)
    assert result['accepted'], result


@pytest.mark.asyncio
@pytest.mark.parametrize('generated_count,expected_acceptance', [(1, False), (2, True)])
async def test_expansion_count_excludes_imported_basis_and_counts_new_versions(env, generated_count, expected_acceptance):
    engine, service = env
    task = await setup_task(engine, service, mode='expand_original')
    for index in range(generated_count):
        assert (await generate_one(engine, service, task, f'expanded-{index + 1}'))['status'] == 'succeeded'
    formal = await scripts(service, task)
    assert sum(value['body'].get('origin') == 'imported' for value in formal) == 1
    assert len(formal) == generated_count + 1
    assert next(value['ref'] for value in formal if value['body'].get('origin') == 'imported') == task['original_script_ref']
    result = await finish(engine, task)
    assert result['accepted'] is expected_acceptance, result
    if not expected_acceptance:
        assert any('版数不足' in value for value in result['missing'])


@pytest.mark.asyncio
async def test_two_revisions_of_same_variant_do_not_count_as_two_versions(env):
    engine, service = env
    task = await setup_task(engine, service)
    first = await generate_one(engine, service, task, 'same-variant')
    assert first['status'] == 'succeeded'
    base = (await scripts(service, task))[0]['ref']
    second = await generate_one(engine, service, task, 'same-variant', base_ref=base)
    assert second['status'] == 'succeeded'
    formal = await scripts(service, task)
    assert len(formal) == 1 and formal[0]['ref'] != base
    assert formal[0]['body']['parent_ref'] == base
    result = await finish(engine, task)
    assert not result['accepted'] and any('版数不足' in value for value in result['missing'])


@pytest.mark.asyncio
async def test_patch_successor_keeps_both_split_variants_in_formal_coverage(env):
    engine, service = env
    task = await setup_task(engine, service)
    assert (await generate_one(engine, service, task, 'new-v1'))['status'] == 'succeeded'
    assert (await generate_one(engine, service, task, 'new-v2'))['status'] == 'succeeded'
    formal = await scripts(service, task)
    before = next(value for value in formal if value['body']['variant_id'] == 'new-v2')
    first_ref = next(value['ref'] for value in formal if value['body']['variant_id'] == 'new-v1')
    prep = await service.query('read_preparation', {'preparation_id': task['preparation_id']})
    old_rows = before['modules']['product_intro']['sentences']
    receipt = await engine.business_command(task['scope'], 'patch_script', {
        'preparation_id': task['preparation_id'], 'expected_revision': prep['ref']['revision'],
        'script_ref': before['ref'], 'module_key': 'product_intro',
        'selected_text_hash': canonical_hash(old_rows), 'sentences': ['已明确核对的替换正文。']}, 'patch-v2')
    after = await service.query('read_script', {'ref': receipt['data']['script_ref']})
    assert after['body']['parent_ref'] == before['ref']
    assert {key: value for key, value in after['body']['module_refs'].items() if key != 'product_intro'} == {
        key: value for key, value in before['body']['module_refs'].items() if key != 'product_intro'}
    current_refs = {canonical_hash(value['ref']) for value in await scripts(service, task)}
    assert canonical_hash(first_ref) in current_refs and canonical_hash(after['ref']) in current_refs
    assert canonical_hash(before['ref']) not in current_refs
    result = await finish(engine, task)
    assert result['accepted'], result


@pytest.mark.asyncio
async def test_failed_generation_job_cannot_supply_second_required_variant(env, monkeypatch):
    engine, service = env
    task = await setup_task(engine, service)
    assert (await generate_one(engine, service, task, 'successful-v1'))['status'] == 'succeeded'

    async def fail_locally(context, module_key, product, previous):
        raise RuntimeError('explicit isolated fake failure; no external request')

    monkeypatch.setattr(service.adapter, 'generate_module', fail_locally)
    failed = await generate_one(engine, service, task, 'failed-v2')
    assert failed['status'] == 'failed'
    assert {value['body']['variant_id'] for value in await scripts(service, task)} == {'successful-v1'}
    result = await finish(engine, task)
    assert not result['accepted']
    assert any('正式稿' in value or '版数不足' in value for value in result['missing'])
