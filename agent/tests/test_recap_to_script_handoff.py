"""Source upload -> frozen script plan -> provider wire DTO -> formal text."""
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from shengbo_contracts.business import MODULE_KEYS
from shengbo_contracts.common import ContractError
from shengbo_prelive.models import IntentAssessment, RunScope
from shengbo_prelive.policy import validate_intent
from test_runtime_business import Product, env, human_command, new_run


REVIEW = Path(__file__).with_name('fixtures') / '营地灯直播复盘.md'
ENERGY = '上次开场太平，下一场先请观众想象天黑后找灯的场景，再加一句能跟着做的轻互动。'
PRICE = '建议下次口播直接喊限时半价，活动尚未确认。'


async def prepare_recap(engine, service):
    fields = Product(name='营地灯', price=129,
        selling_points='可用折叠提手提拿，平底座可放在桌面。高亮与低亮两档手动切换。',
        specifications=[{'name': '灯身重量', 'value': '280克'}, {'name': '亮度档位', 'value': '高亮、低亮两档'}],
        usage_instructions='用于营地桌面或帐篷入口的干燥处照明。使用和充电时应避开雨水。').model_dump()
    target = (await human_command(service, 'save_product', {'fields': fields,
        'schema_hash': service.schema()['schema_hash'], 'patches': []}))['data']['product_ref']
    room = (await human_command(service, 'create_room', {'name': '营地灯复盘测试间'}))['data']['room_ref']
    prep = (await human_command(service, 'create_preparation', {'room_id': room['id'],
        'room_revision': room['revision']}))['data']['preparation_ref']
    prep = (await human_command(service, 'update_preparation_products', {'preparation_id': prep['id'],
        'expected_revision': prep['revision'], 'selected_product_refs': [target]}))['data']['preparation_ref']
    draft = await human_command(service, 'save_session_draft', {'preparation_id': prep['id'],
        'expected_revision': prep['revision'], 'draft': {'theme': '周末露营', 'commerce': {}}})
    prep = draft['data']['preparation_ref']
    instruction = '请导入这份复盘并关联本场，再为营地灯生成一版新话术。只要文本，不生成语音。'
    run = await new_run(engine, instruction, room_id=room['id'], preparation_id=prep['id'])
    raw = REVIEW.read_bytes()
    source = engine.materials.accept(run['conversation_id'], REVIEW.name, 'text/markdown', raw)['material_ref']
    intent = IntentAssessment(objective='script_change', summary='导入复盘，生成一版营地灯话术',
        authorization_quote=instruction, target_product_ids=[target['id']],
        requested_script_variants=[{'product_id': target['id'], 'count': 1}])
    run = engine.store.update('run', run['id'], input={**run['input'], 'material_refs': [source]},
        intent_assessment=intent.model_dump())
    await engine._freeze_goal(run)
    scope = RunScope(run['id'], 'recap-to-copy', 'execute')
    current_run = engine.store.get('run', run['id'])
    assert current_run['phase'] == 'execute'
    assert {'import_recap', 'link_recap_to_preparation'} <= set(current_run['allowed_effects'])
    assert not {'save_product', 'prepare_required_audio', 'publish_preparation'} & set(current_run['allowed_effects'])
    for refusal in ('不要导入这份复盘。请为营地灯生成一版新话术。',
                    '导入这份复盘不是我的要求。请为营地灯生成一版新话术。'):
        declined = validate_intent({**run, 'input': {**run['input'], 'text': refusal}},
            intent.model_copy(update={'authorization_quote': refusal}))
        assert 'import_recap' not in declined['allowed_effects']
    imported = await engine.business_command(scope, 'import_recap', {
        'raw_blob_ref': source, 'synthetic': True, 'document': {'synthetic': True, 'findings': [
            {'finding_id': 'open-energy', 'suggestion': ENERGY, 'evidence_ids': ['e1']},
            {'finding_id': 'discount', 'suggestion': PRICE, 'evidence_ids': ['e2']}],
            'evidence': [{'evidence_id': 'e1', 'quote': ENERGY}, {'evidence_id': 'e2', 'quote': PRICE}]}}, 'import')
    recap = imported['data']['recap_ref']
    link = await engine.business_command(scope, 'link_recap_to_preparation', {'preparation_id': prep['id'],
        'expected_revision': prep['revision'], 'recap_refs': [recap]}, 'link')
    bundle = await engine.business_command(scope, 'save_context_bundle', {'preparation_id': prep['id'],
        'expected_revision': link['data']['preparation_ref']['revision'], 'product_refs': [target],
        'draft_ref': draft['data']['draft_ref'], 'recap_refs': [recap]}, 'context')
    directions = {
        'opening_long': '落实已采用的暖场建议。用天黑找灯的动作开场，再给一句能跟着做的互动，不假装已收到观众回复。',
        'product_intro': '用一句话亮出商品和一个有据的用途或结构。',
        'selling_points': '用提手、平底座和两档明暗讲桌面及帐篷入口的实际使用动作。自然保留干燥使用条件。重量只按规格表达，不能推出体积、可装入某处、耐候、照明范围或温度表现。',
        'cta': '回到周末露营干燥处照明需求，准确报价129元，自然邀请按需选购。',
    }
    actions = [{'key': key, 'action': 'generate', 'instruction': directions[key]} if key in directions
               else {'key': key, 'action': 'disable'} for key in MODULE_KEYS]
    args = {'preparation_id': prep['id'], 'expected_revision': bundle['data']['preparation_ref']['revision'],
        'bundle_ref': bundle['data']['context_bundle_ref'],
        'items': [{'product_ref': target, 'variant_id': 'recap-followup', 'modules': actions}],
        'recap_dispositions': [{'recap_ref': recap, 'finding_id': 'open-energy', 'action': 'apply',
                                'reason': '在开场加入场景和互动。', 'target_modules': ['opening_long']},
                               {'recap_ref': recap, 'finding_id': 'discount', 'action': 'defer',
                                'reason': '本场活动尚未确认。'}]}
    return prep, recap, bundle, args, scope, raw


async def submit_recap_script(engine, service, prep, bundle, args, scope):
    plan = await engine.business_command(scope, 'save_script_plan', args, 'plan')
    submitted = await engine.business_command(scope, 'submit_script_change', {'preparation_id': prep['id'],
        'expected_revision': plan['data']['preparation_ref']['revision'], 'plan_ref': plan['data']['script_plan_ref'],
        'context_bundle_ref': bundle['data']['context_bundle_ref'],
        'expected_generation_epoch': plan['data']['generation_epoch']}, 'submit')
    await service.drain_jobs()
    return (await service.query('read_job', {'job_id': submitted['data']['job_id']}))['body']


@pytest.mark.asyncio
async def test_exact_recap_decisions_reach_one_writer_and_formal_script(env):
    from app.prelive.whole_script_generation import generate_one_script

    engine, service = env
    prep, recap, bundle, args, scope, raw = await prepare_recap(engine, service)
    outbound = []
    opening = '天刚黑，伸手在背包里摸半天还找不到灯，你是不是也经历过？'
    interaction = '来，跟我一起想想，你会先把灯放在桌边，还是留在帐篷门口？'

    def provider(request):
        task = json.loads(json.loads(request.content)['messages'][-1]['content'])
        outbound.append(task)
        guidance = task['context_bundle'].get('recap_writing_guidance', [])
        text = [opening, interaction] if any(item['finding_id'] == 'open-energy' for item in guidance) else ['欢迎来到直播间。']
        modules = {'opening_long': [(line, 'engagement', []) for line in text],
                   'product_intro': [('来看这款营地灯。', 'product_claim', ['NAME'])],
                   'selling_points': [('你可以先想一想，晚上最常在哪儿找东西？', 'engagement', [])],
                   'cta': [('如果你也在准备周末露营，现在就来看看。', 'engagement', [])]}
        response = {'purchase_focus': {'audience': '周末露营顾客', 'decision': '提前安排照明', 'scene': '天黑后翻包'},
            'modules': [{'module_key': key, 'sentences': [{'text': sentence, 'kind': kind, 'fact_ids': facts}
                         for sentence, kind, facts in values]} for key, values in modules.items()]}
        return httpx.Response(200, json={'id': 'replay-recap-writer', 'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(response, ensure_ascii=False)}}]})

    async def generate_script(context, modules, fields, *, response_observer):
        return await generate_one_script(fields, context, modules, api_key='isolated-test-key', model='isolated-writer',
            transport=httpx.MockTransport(provider), response_observer=response_observer)

    service.adapter.generate_script = generate_script
    for name, mutate, code in [
        ('missing', lambda rows: rows.pop(), 'RECAP_DISPOSITION_MISSING'),
        ('duplicate', lambda rows: rows.append(rows[0]), 'RECAP_DISPOSITION_INVALID'),
        ('disabled-target', lambda rows: rows[0].update(target_modules=['campaign']), 'RECAP_DISPOSITION_TARGET_INVALID'),
    ]:
        invalid = deepcopy(args)
        mutate(invalid['recap_dispositions'])
        with pytest.raises(ContractError, match=code):
            await engine.business_command(scope, 'save_script_plan', invalid, name)
    assert not outbound
    job = await submit_recap_script(engine, service, prep, bundle, args, scope)
    assert job['status'] == 'succeeded', job
    assert len(outbound) == 1
    writing_context = outbound[0]['context_bundle']
    assert [(item['finding_id'], item['target_modules']) for item in writing_context['recap_writing_guidance']] == [
        ('open-energy', ['opening_long'])]
    assert ENERGY in json.dumps(writing_context, ensure_ascii=False)
    assert PRICE not in json.dumps(writing_context, ensure_ascii=False)
    formal = await service.query('read_script', {'ref': job['items'][0]['output']['script_ref']})
    assert [row['text'] for row in formal['modules']['opening_long']['sentences']] == [opening, interaction]
    assert '半价' not in json.dumps(formal, ensure_ascii=False)
    assert (await engine.finish(scope, 'COMPLETED', '已按复盘生成并核验本场话术。'))['accepted']
    with service.store.transaction() as connection:
        saved = service.store.exact(connection, recap)['body']
        blob = service.store.exact(connection, saved['raw_blob_ref'])['body']
    assert (service.runtime / blob['relative_path']).read_bytes() == raw
    assert [item['suggestion'] for item in saved['document']['findings']] == [ENERGY, PRICE]
