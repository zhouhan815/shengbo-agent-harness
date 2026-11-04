"""Formal variant coverage across real isolated plans/jobs/imports; no paid calls."""
from copy import deepcopy

import pytest

from shengbo_contracts.business import MODULE_KEYS
from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import IntentAssessment, RunScope
from test_runtime_business import env, human_command, new_run, product


async def setup_task(engine, service, *, mode='generate', count=2, second_product=False):
    target = await product(service, '分批版本验收商品')
    targets = [target]
    if second_product:
        targets.append(await product(service, '同版号另一商品'))
    room = (await human_command(service, 'create_room', {'name': '分批版本隔离直播间'}))['data']['room_ref']
    prep = (await human_command(service, 'create_preparation', {'room_id': room['id'],
        'room_revision': room['revision']}))['data']['preparation_ref']
    prep = (await human_command(service, 'update_preparation_products', {'preparation_id': prep['id'],
        'expected_revision': prep['revision'], 'selected_product_refs': targets}))['data']['preparation_ref']
    draft = await human_command(service, 'save_session_draft', {'preparation_id': prep['id'],
        'expected_revision': prep['revision'], 'draft': {'theme': '两份独立新稿', 'commerce': {}}})
    bundle = await human_command(service, 'save_context_bundle', {'preparation_id': prep['id'],
        'expected_revision': draft['data']['preparation_ref']['revision'], 'product_refs': targets,
        'draft_ref': draft['data']['draft_ref']})
    quantity = {1: '一版', 2: '两版', 3: '三版'}.get(count, f'{count}版')
    instruction = (f"为商品{target['id']}导入原稿后扩展{quantity}话术，只要文本，不生成语音" if mode == 'expand_original'
        else f"为商品{'、'.join(value['id'] for value in targets)}分别生成{quantity}话术，只要文本，不生成语音")
    run = await new_run(engine, instruction, room_id=room['id'], preparation_id=prep['id'])
    source = None
    text = '原稿：欢迎了解这款商品。请按实际需求选择。'
    if mode == 'expand_original':
        source = engine.materials.accept(run['conversation_id'], 'merchant-original.txt', 'text/plain',
            text.encode())['material_ref']
        run = engine.store.update('run', run['id'], input={**run['input'], 'material_refs': [source]})
    proposal = IntentAssessment(objective='script_change', script_mode=mode, summary='两份新版本的实际保存验收',
        target_product_ids=[value['id'] for value in targets],
        requested_script_variants=[{'product_id': value['id'], 'count': count} for value in targets],
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
    return {'run_id': run['id'], 'scope': scope, 'product_ref': target, 'product_refs': targets, 'preparation_id': prep['id'],
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
        item['variant_id'] = (await service.query('read_script', {'ref': base_ref}))['body']['variant_id']
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
async def test_two_products_can_each_save_version_one_from_same_plan_label_and_replay(env):
    engine, service = env
    task = await setup_task(engine, service, count=1, second_product=True)
    prep = await service.query('read_preparation', {'preparation_id': task['preparation_id']})
    args = {'preparation_id': task['preparation_id'], 'expected_revision': prep['ref']['revision'],
        'bundle_ref': task['bundle_ref'], 'items': [
            {'product_ref': ref, 'variant_id': 'v1', 'version_number': 1, 'generation_mode': 'auto',
                'modules': [{'key': key, 'action': 'disable' if key in {'opening_short', 'campaign', 'transition'} else 'generate'}
                    for key in MODULE_KEYS]} for ref in task['product_refs']]}
    plan = await engine.business_command(task['scope'], 'save_script_plan', args, 'both-version-one')
    assert await engine.business_command(task['scope'], 'save_script_plan', args, 'both-version-one') == plan
    changed = deepcopy(args)
    changed['items'][0]['version_number'] = 2
    with pytest.raises(ContractError) as mismatch:
        await engine.business_command(task['scope'], 'save_script_plan', changed, 'both-version-one')
    assert mismatch.value.code == 'OPERATION_CHANGED'
    operations = [op for op in engine.store.list('operation') if op['key'] == 'save_script_plan']
    assert len(operations) == 1
    items = operations[0]['args']['items']
    ids = {item['variant_id'] for item in items}
    assert len(ids) == 2 and all(identity.startswith('variant_') for identity in ids)
    assert {item['version_number'] for item in items} == {1}
    assert operations[0]['request_hash'] == canonical_hash(operations[0]['args'])
    submitted = await engine.business_command(task['scope'], 'submit_script_change', {
        'preparation_id': task['preparation_id'], 'expected_revision': plan['data']['preparation_ref']['revision'],
        'plan_ref': plan['data']['script_plan_ref'], 'context_bundle_ref': task['bundle_ref'],
        'expected_generation_epoch': plan['data']['generation_epoch']}, 'both-generate')
    await service.drain_jobs()
    job = await service.query('read_job', {'job_id': submitted['data']['job_id']})
    assert job['body']['status'] == 'succeeded', job
    formal = await scripts(service, task)
    assert {value['body']['variant_id'] for value in formal} == ids
    assert {value['body']['version_number'] for value in formal} == {1}
    assert (await finish(engine, task))['accepted']


@pytest.mark.asyncio
async def test_retrying_one_failed_product_keeps_original_variant_and_allows_completion(env):
    from app.prelive.module_generation import validate_output

    engine, service = env
    task = await setup_task(engine, service, count=1, second_product=True)
    writer_calls = []

    async def generate_whole(_context, modules, product):
        writer_calls.append(product['name'])
        keys = [module['key'] for module in modules if module['action'] == 'generate']
        result = {key: {'sentences': [product['name'] + '的隔离正文。']} for key in keys}
        if 'opening_short' in keys and writer_calls.count(product['name']) == 1:
            validate_output({'module_key': 'opening_short', 'sentences': [
                {'display_text': text, 'spoken_text': text, 'kind': 'engagement', 'fact_ids': []}
                for text in ('第一句。', '第二句。', '第三句。')]}, product, 'opening_short')
        return {'modules': result}

    service.adapter.generate_script = generate_whole
    items = []
    for ref, opening in zip(task['product_refs'], ('opening_long', 'opening_short')):
        active = {opening, 'product_intro', 'selling_points', 'cta'}
        items.append({'product_ref': ref, 'variant_id': 'v1', 'version_number': 1, 'modules': [
            {'key': key, 'action': 'generate' if key in active else 'disable'} for key in MODULE_KEYS]})

    async def run_plan(chosen, name):
        prep = await service.query('read_preparation', {'preparation_id': task['preparation_id']})
        plan = await engine.business_command(task['scope'], 'save_script_plan', {
            'preparation_id': task['preparation_id'], 'expected_revision': prep['ref']['revision'],
            'bundle_ref': task['bundle_ref'], 'items': chosen}, name + '-plan')
        submitted = await engine.business_command(task['scope'], 'submit_script_change', {
            'preparation_id': task['preparation_id'], 'expected_revision': plan['data']['preparation_ref']['revision'],
            'plan_ref': plan['data']['script_plan_ref'], 'context_bundle_ref': task['bundle_ref'],
            'expected_generation_epoch': plan['data']['generation_epoch']}, name + '-submit')
        await service.drain_jobs()
        return (await service.query('read_job', {'job_id': submitted['data']['job_id']}))['body']

    first = await run_plan(items, 'first')
    succeeded, failed = first['items']
    assert succeeded['status'] == 'succeeded'
    assert failed['status'] == 'failed' and failed['error']['code'] == 'SHORT_MODULE_TOO_LONG'
    retry = deepcopy(items[1])
    next(module for module in retry['modules'] if module['key'] == 'opening_short')['instruction'] = '只写一句完整短开场。'
    corrected = (await run_plan([retry], 'retry'))['items'][0]
    assert corrected['status'] == 'succeeded'
    assert corrected['input']['variant_id'] == failed['input']['variant_id']
    assert writer_calls == ['分批版本验收商品', '同版号另一商品', '同版号另一商品']
    formal = await scripts(service, task)
    assert {value['ref']['id'] for value in formal} == {item['output']['script_ref']['id'] for item in (succeeded, corrected)}
    assert {value['body']['version_number'] for value in formal} == {1}

    script_ref = corrected['output']['script_ref']
    module = await engine.business_query(task['scope'], 'read_script', {'ref': script_ref, 'module_key': 'opening_short'})
    exact = await engine.business_query(task['scope'], 'read_script', {'ref': script_ref, 'sections': ['opening_short']})
    assert module == exact
    sentence_id = module['modules']['opening_short']['sentences'][0]['id']
    selection = await engine.business_query(task['scope'], 'read_script', {
        'ref': script_ref, 'module_key': 'opening_short', 'sentence_ids': [sentence_id]})
    assert selection['selection']['sentence_ids'] == [sentence_id]
    with pytest.raises(ContractError) as rejected:
        await engine.business_query(task['scope'], 'read_script', {
            'ref': script_ref, 'module_key': 'opening_short', 'sections': ['selling_points']})
    assert rejected.value.code == 'TEXT_SELECTION_QUERY_INVALID'
    assert (await finish(engine, task))['accepted']


@pytest.mark.asyncio
async def test_single_new_draft_keeps_one_whole_writer_request(env):
    engine, service = env
    task = await setup_task(engine, service, count=1)
    prep = await service.query('read_preparation', {'preparation_id': task['preparation_id']})
    actions = [{'key': key, 'action': 'disable' if key in {'opening_short', 'campaign', 'transition'} else 'generate'}
               for key in MODULE_KEYS]
    args = {'preparation_id': task['preparation_id'], 'expected_revision': prep['ref']['revision'],
            'bundle_ref': task['bundle_ref'], 'items': [{'product_ref': task['product_ref'], 'variant_id': 'ordinary',
                'generation_mode': 'module', 'modules': actions}]}
    with pytest.raises(ContractError) as rejected:
        await engine.business_command(task['scope'], 'save_script_plan', args, 'wrong-route')
    assert rejected.value.code == 'SCRIPT_MODE_CONFLICT'
    assert rejected.value.details['commit_state'] == 'not_committed'
    assert not [op for op in engine.store.list('operation') if op['key'] == 'save_script_plan']

    writer_calls = []

    async def generate_whole(context, modules, product):
        keys = [module['key'] for module in modules if module['action'] == 'generate']
        writer_calls.append(keys)
        return {'modules': {key: {'sentences': [product['name'] + '的隔离正文。']} for key in keys}}

    async def unexpected_module(*_args):
        raise AssertionError('A full draft must not call the module writer')

    service.adapter.generate_script = generate_whole
    service.adapter.generate_module = unexpected_module
    job = await generate_one(engine, service, task, 'ordinary')
    assert job['status'] == 'succeeded', job
    assert job['items'][0]['input']['generation_mode'] == 'whole_script'
    assert len(writer_calls) == 1
    assert set(writer_calls[0]) == {'opening_long', 'product_intro', 'selling_points', 'cta'}
    assert (await finish(engine, task))['accepted']


@pytest.mark.asyncio
async def test_two_sequential_plan_jobs_count_both_current_formal_variants(env):
    engine, service = env
    task = await setup_task(engine, service)
    first = await generate_one(engine, service, task, 'new-v1')
    second = await generate_one(engine, service, task, 'new-v2')
    assert first['status'] == second['status'] == 'succeeded'
    formal = await scripts(service, task)
    expected = {job['items'][0]['input']['variant_id'] for job in (first, second)}
    assert len(expected) == 2
    assert {value['body']['variant_id'] for value in formal} == expected
    result = await finish(engine, task)
    assert result['accepted'], result


@pytest.mark.asyncio
@pytest.mark.parametrize('generated_count,expected_acceptance', [(1, False), (2, True)])
async def test_expansion_count_excludes_imported_basis_and_counts_new_versions(env, generated_count, expected_acceptance):
    engine, service = env
    task = await setup_task(engine, service, mode='expand_original')
    async def render(context, key, product, previous):
        return ['先从常见用途说起。' if task['sequence'] == 1 else '这次聊聊如何选择。']
    service.adapter.generate_module = render
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
async def test_expansion_writes_two_distinct_whole_variants_and_keeps_exact_basis(env):
    engine, service = env
    task = await setup_task(engine, service, mode='expand_original')
    original = await service.query('read_script', {'ref': task['original_script_ref']})
    opening_samples = ['第一次露营，先想好你最需要解决什么。', '挑日常用品，先从你每周重复的那个动作想起。']
    calls = []

    async def write_variant(context, modules, product):
        basis = context['original_script']
        assert basis['ref'] == original['ref']
        assert basis['body']['raw_text'] == original['body']['raw_text']
        opening = opening_samples[len(calls)]
        calls.append(context['variant']['variant_id'])
        values = {'opening_long': opening, 'product_intro': f'我们来看这款{product["name"]}。',
                  'selling_points': '把使用场景想清楚，再按你自己的需求挑。', 'cta': '想好了就来挑一挑。'}
        return {'modules': {item['key']: {'sentences': [values[item['key']]]}
                            for item in modules if item['action'] == 'generate'}}

    service.adapter.generate_script = write_variant
    for index in range(2):
        job = await generate_one(engine, service, task, f'expanded-direction-{index + 1}')
        assert job['status'] == 'succeeded'
        assert job['items'][0]['input']['generation_mode'] == 'whole_script'
    assert len(set(calls)) == 2
    formal = await scripts(service, task)
    assert len(formal) == 3
    assert next(value for value in formal if value['ref'] == original['ref']) == original
    assert {value['modules']['opening_long']['sentences'][0]['text'] for value in formal
            if value['ref'] != original['ref']} == set(opening_samples)
    assert (await finish(engine, task))['accepted']


@pytest.mark.asyncio
@pytest.mark.parametrize('count,copy_last', [(2, False), (3, False), (3, True)])
async def test_expansion_compares_successful_siblings_and_keeps_same_facts(env, count, copy_last):
    engine, service = env
    task = await setup_task(engine, service, mode='expand_original', count=count)
    original = await service.query('read_script', {'ref': task['original_script_ref']})
    openings = ['把周末留给自己，先从营地桌边那盏亮起来的灯说起，收拾东西时就不用在包里乱翻。',
        '天刚擦黑正要进帐篷，谁也不想双手都被照明工具占住，来听听有哪些细节可以先挑清楚。',
        '选随身的照明装备，我先看平时喜欢怎么摆，再想临时换地方能不能提着走。']
    calls = []

    async def write(context, actions, product):
        pos = len(calls)
        from app.prelive.expansion_expression import EXPRESSION_DIRECTIONS
        brief = context['expansion_expression']
        assert 'previous_expansions' not in brief
        assert brief['expression_direction'] == EXPRESSION_DIRECTIONS[pos]
        calls.append(pos)
        opening = openings[0] if copy_last and pos == count - 1 else openings[pos]
        if copy_last and pos == count - 1:
            # Keep the copied text despite punctuation and an extra sentence.
            opening = '先来看看。' + opening.replace('，', '！')
        values = {'opening_long': opening, 'product_intro': '看看这款好物。',
                  'selling_points': '可以按需挑选。', 'cta': '喜欢就去选一选。'}
        return {'modules': {item['key']: {'sentences': [values[item['key']]]}
                            for item in actions if item['action'] == 'generate'}}

    service.adapter.generate_script = write
    from shengbo_prelive.workflows import run_workflow
    flow = await run_workflow(engine, task['scope'], 'wf_script_execution', {'action': 'plan_execute_wait',
        'bundle_ref': task['bundle_ref'], 'items': [{'product_ref': task['product_ref'],
            'original_script_ref': task['original_script_ref'], 'variant_id': f'expansion-{i}',
            'modules': [{'key': key, 'action': 'disable' if key in {'opening_short', 'campaign', 'transition'} else 'generate'}
                        for key in MODULE_KEYS]} for i in range(count)]})
    assert flow['status'] == 'WAITING'
    await service.drain_jobs()
    job = (await service.query('read_job', {'job_id': flow['result']['data']['job_id']}))['body']
    assert len(calls) == count
    saved = await scripts(service, task)
    assert next(value for value in saved if value['ref'] == original['ref']) == original
    assert len(saved) == 1 + count - int(copy_last)
    assert job['status'] == ('partially_failed' if copy_last else 'succeeded'), job
    if copy_last:
        error = job['items'][-1]['error']
        assert error['code'] == 'SCRIPT_EXPANSION_REPEATED_EXPRESSION'
        assert error['details']['module_key'] == 'opening_long'
    assert (await finish(engine, task))['accepted'] is (not copy_last)


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
    first = await generate_one(engine, service, task, 'new-v1')
    second = await generate_one(engine, service, task, 'new-v2')
    assert first['status'] == second['status'] == 'succeeded'
    formal = await scripts(service, task)
    before = next(value for value in formal if value['body']['variant_id'] == second['items'][0]['input']['variant_id'])
    first_ref = next(value['ref'] for value in formal if value['body']['variant_id'] == first['items'][0]['input']['variant_id'])
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
    successful = await generate_one(engine, service, task, 'successful-v1')
    assert successful['status'] == 'succeeded'

    async def fail_locally(context, module_key, product, previous):
        raise RuntimeError('explicit isolated fake failure; no external request')

    monkeypatch.setattr(service.adapter, 'generate_module', fail_locally)
    failed = await generate_one(engine, service, task, 'failed-v2')
    assert failed['status'] == 'failed'
    assert {value['body']['variant_id'] for value in await scripts(service, task)} == {successful['items'][0]['input']['variant_id']}
    result = await finish(engine, task)
    assert not result['accepted']
    assert any('正式稿' in value or '版数不足' in value for value in result['missing'])
