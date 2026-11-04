"""Explicit module intent, exact grant and real isolated patch boundaries."""
from copy import deepcopy

import pytest

from shengbo_contracts.business import MODULE_KEYS
from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.models import IntentAssessment, RunScope
from test_runtime_business import env, human_command, new_run, product
from test_selection_binding import select


def test_explicit_local_edit_needs_a_real_module_in_intent_and_closed_enum():
    from pydantic import ValidationError
    from shengbo_prelive.policy import validate_intent
    text = '只修改话术campaign活动模块，其他模块不动'
    proposal = IntentAssessment(objective='script_change', script_mode='edit', summary='仅活动', authorization_quote=text)
    with pytest.raises(ContractError, match='INTENT_MODULE_SCOPE_REQUIRED'):
        validate_intent({'input': {'text': text}}, proposal)
    with pytest.raises(ValidationError):
        IntentAssessment.model_validate({**proposal.model_dump(), 'requested_modules': ['all_modules_and_more']})
    assert not validate_intent({'input': {'text': text}}, proposal.model_copy(update={'requested_modules': ['campaign']}))['requires_decision']


@pytest.mark.asyncio
async def test_missing_module_intent_returns_to_native_model_without_granting_whole_script():
    import json
    from langchain.agents import create_agent
    from langchain.tools import tool
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from shengbo_prelive.middleware import ActionBoundary
    from shengbo_prelive.policy import validate_intent
    from test_middleware import FakeEngine
    from test_runtime_business import BindableFakeModel
    text = '只修改话术campaign活动模块，其他模块不动'
    @tool
    async def assess(requested_modules: list[str]):
        """Validate the explicit test intent; no business effect."""
        proposal = IntentAssessment(objective='script_change', script_mode='edit', summary='仅活动',
            requested_modules=requested_modules, authorization_quote=text)
        return validate_intent({'input': {'text': text}}, proposal)
    engine = FakeEngine()
    model = BindableFakeModel(responses=[
        AIMessage(content='', tool_calls=[{'name': 'assess', 'args': {'requested_modules': []}, 'id': 'missing'}]),
        AIMessage(content='', tool_calls=[{'name': 'assess', 'args': {'requested_modules': ['campaign']}, 'id': 'scoped'}]),
        AIMessage(content='已明确仅活动模块')])
    graph = create_agent(model, tools=[assess], middleware=[ActionBoundary(engine)], context_schema=RunScope)
    output = await graph.ainvoke({'messages': [HumanMessage(text)]}, context=RunScope('r1', 'scope-intent', 'intent'))
    feedback = [m for m in output['messages'] if isinstance(m, ToolMessage)]
    assert feedback[0].status == 'error'
    assert json.loads(feedback[0].content)['error']['code'] == 'INTENT_MODULE_SCOPE_REQUIRED'
    assert feedback[1].status == 'success'
    assert engine.store.get('run', 'r1')['correctable_tool_errors'] == {'INTENT_MODULE_SCOPE_REQUIRED': 1}


async def baseline(service):
    target = await product(service, '活动模块范围测试商品')
    room = (await human_command(service, 'create_room', {'name': '活动范围隔离直播间'}))['data']['room_ref']
    prep = (await human_command(service, 'create_preparation', {'room_id': room['id'],
        'room_revision': room['revision']}))['data']['preparation_ref']
    prep = (await human_command(service, 'update_preparation_products', {'preparation_id': prep['id'],
        'expected_revision': prep['revision'], 'selected_product_refs': [target]}))['data']['preparation_ref']
    saved = await human_command(service, 'save_session_draft', {'preparation_id': prep['id'],
        'expected_revision': prep['revision'], 'draft': {'theme': '既有稿件供活动局改', 'commerce': {}}})
    bundle = await human_command(service, 'save_context_bundle', {'preparation_id': prep['id'],
        'expected_revision': saved['data']['preparation_ref']['revision'], 'product_refs': [target],
        'draft_ref': saved['data']['draft_ref']})
    plan = await human_command(service, 'save_script_plan', {'preparation_id': prep['id'],
        'expected_revision': bundle['data']['preparation_ref']['revision'],
        'bundle_ref': bundle['data']['context_bundle_ref'], 'items': [{'product_ref': target,
            'variant_id': 'existing-campaign-script', 'modules': [
                {'key': key, 'action': 'disable' if key in {'opening_short', 'transition'} else 'generate'}
                for key in MODULE_KEYS]}]})
    submitted = await human_command(service, 'submit_script_change', {'preparation_id': prep['id'],
        'expected_revision': plan['data']['preparation_ref']['revision'], 'plan_ref': plan['data']['script_plan_ref'],
        'context_bundle_ref': bundle['data']['context_bundle_ref'],
        'expected_generation_epoch': plan['data']['generation_epoch']})
    await service.drain_jobs()
    job = await service.query('read_job', {'job_id': submitted['data']['job_id']})
    assert job['body']['status'] == 'succeeded'
    current = await service.query('read_preparation', {'preparation_id': prep['id']})
    script = await service.query('read_script', {'ref': current['body']['script_refs'][0]})
    assert script['modules']['campaign']['sentences']
    return target, room, current, script


async def freeze(engine, target, room, prep, before, *, with_selection=False):
    text = f"只修改商品{target['id']}的话术campaign活动模块，其他模块一字不动，不生成语音"
    run = await new_run(engine, text, room_id=room['id'], preparation_id=prep['ref']['id'])
    requested = ['campaign']
    if with_selection:
        # A broader model proposal cannot expand the human's exact UI binding.
        requested = ['campaign', 'opening_long']
        selection = select(before['ref'], module='campaign', prep=prep['ref'],
            digest=canonical_hash(before['modules']['campaign']['sentences']))
        run = engine.store.update('run', run['id'], input={**run['input'], 'selection': selection})
    assessment = IntentAssessment(objective='script_change', script_mode='edit', summary='仅活动模块的明确编辑',
        target_product_ids=[target['id']], requested_modules=requested, authorization_quote=text)
    run = engine.store.update('run', run['id'], intent_assessment=assessment.model_dump())
    await engine._freeze_goal(run)
    run = engine.store.get('run', run['id'])
    assert run['phase'] == 'execute' and run.get('grant_id'), engine.store.list('question')
    return run, RunScope(run['id'], 'requested-module-scope', 'execute')


def patch_args(prep, before, key):
    return {'preparation_id': prep['ref']['id'], 'expected_revision': prep['ref']['revision'],
        'script_ref': before['ref'], 'module_key': key,
        'selected_text_hash': canonical_hash(before['modules'][key]['sentences']),
        'sentences': ['本句是本次明确选择的替换文字。']}


def assert_untouched_modules(before, after, changed_key='campaign'):
    assert {key: ref for key, ref in after['body']['module_refs'].items() if key != changed_key} == {
        key: ref for key, ref in before['body']['module_refs'].items() if key != changed_key}
    assert {key: module for key, module in after['modules'].items() if key != changed_key} == {
        key: module for key, module in before['modules'].items() if key != changed_key}


@pytest.mark.asyncio
async def test_explicit_campaign_scope_rejects_unrequested_opening_without_ui_selection(env):
    engine, service = env
    target, room, prep, before = await baseline(service)
    run, scope = await freeze(engine, target, room, prep, before)
    assert run['goal']['requested_modules'] == ['campaign']
    with pytest.raises(ContractError) as caught:
        await engine.business_command(scope, 'patch_script', patch_args(prep, before, 'opening_long'), 'deny-opening')
    assert caught.value.code in {'MODULE_SCOPE_DENIED', 'SCOPE_CONFLICT', 'SELECTION_TARGET_DENIED'}
    current = await service.query('read_preparation', {'preparation_id': prep['ref']['id']})
    assert current['body']['script_refs'] == [before['ref']]


@pytest.mark.asyncio
async def test_explicit_campaign_scope_can_patch_campaign_and_preserves_other_text_and_refs(env):
    engine, service = env
    target, room, prep, before = await baseline(service)
    _, scope = await freeze(engine, target, room, prep, before)
    original = deepcopy(before)
    receipt = await engine.business_command(scope, 'patch_script', patch_args(prep, before, 'campaign'), 'patch-campaign')
    after = await service.query('read_script', {'ref': receipt['data']['script_ref']})
    assert after['body']['parent_ref'] == before['ref']
    assert after['body']['module_refs']['campaign'] != before['body']['module_refs']['campaign']
    assert_untouched_modules(original, after)
    assert (await service.query('read_script', {'ref': before['ref']})) == original


@pytest.mark.asyncio
async def test_explicit_campaign_scope_keeps_module_writer_available(env):
    engine, service = env
    target, room, prep, before = await baseline(service)
    _, scope = await freeze(engine, target, room, prep, before)
    actions = []
    for key, ref in before['body']['module_refs'].items():
        if key == 'campaign':
            actions.append({'key': key, 'action': 'generate', 'instruction': '用本次明确要求的新活动文案。'})
        elif ref:
            actions.append({'key': key, 'action': 'reuse', 'base_module_ref': ref})
        else:
            actions.append({'key': key, 'action': 'disable'})
    calls = []

    async def generate_module(context, key, product, previous):
        calls.append(key)
        return ['本次选定活动模块的隔离测试正文。']

    service.adapter.generate_module = generate_module
    saved = await engine.business_command(scope, 'save_script_plan', {'preparation_id': prep['ref']['id'],
        'expected_revision': prep['ref']['revision'], 'bundle_ref': prep['body']['bundle_ref'],
        'items': [{'product_ref': target, 'variant_id': before['body']['variant_id'], 'base_script_ref': before['ref'],
            'generation_mode': 'module', 'modules': actions}]}, 'local-campaign-plan')
    submitted = await engine.business_command(scope, 'submit_script_change', {'preparation_id': prep['ref']['id'],
        'expected_revision': saved['data']['preparation_ref']['revision'], 'plan_ref': saved['data']['script_plan_ref'],
        'context_bundle_ref': prep['body']['bundle_ref'], 'expected_generation_epoch': saved['data']['generation_epoch']},
        'local-campaign-submit')
    await service.drain_jobs()
    job = await service.query('read_job', {'job_id': submitted['data']['job_id']})
    assert job['body']['status'] == 'succeeded', job
    current = await service.query('read_preparation', {'preparation_id': prep['ref']['id']})
    after = await service.query('read_script', {'ref': current['body']['script_refs'][0]})
    assert calls == ['campaign']
    assert after['body']['parent_ref'] == before['ref']
    assert_untouched_modules(before, after)


@pytest.mark.asyncio
async def test_exact_ui_module_remains_narrower_than_requested_modules_and_grant(env):
    engine, service = env
    target, room, prep, before = await baseline(service)
    run, scope = await freeze(engine, target, room, prep, before, with_selection=True)
    assert run['goal']['requested_modules'] == ['campaign', 'opening_long']
    assert run['module_allowlist'] == ['campaign']
    with pytest.raises(ContractError) as caught:
        await engine.business_command(scope, 'patch_script', patch_args(prep, before, 'opening_long'), 'ui-deny-opening')
    assert caught.value.code == 'MODULE_SCOPE_DENIED'
    receipt = await engine.business_command(scope, 'patch_script', patch_args(prep, before, 'campaign'), 'ui-allow-campaign')
    after = await service.query('read_script', {'ref': receipt['data']['script_ref']})
    assert_untouched_modules(before, after)
