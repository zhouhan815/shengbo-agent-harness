"""Real isolated Runtime/Voice approval path; no credentials, model or TTS calls.

The tennis record informs the fixture's shape only. Its 1688 supplier prices are
not retail prices; 79 CNY below is an explicit isolated-test value, never a
merchant-approved production price.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.errors import GraphInterrupt
from pydantic import PrivateAttr

from shengbo_contracts.common import ContractError
from shengbo_contracts.common import canonical_hash
from shengbo_contracts.business import AnalysisReport
from shengbo_prelive.models import AnswerRequest, IntentAssessment, MessageRequest, RunScope
from shengbo_prelive.runtime import CURRENT_SCOPE
from shengbo_prelive.workflows import run_workflow
from test_runtime_business import Product, env, human_command, new_run, stop_at_interrupt


async def create_tennis(service):
    fields = Product(
        name='隔离测试单人网球回弹训练器', price=79, currency='CNY',
        material='拍杆材质：镁合金；网线材质：聚酯纤维',
        specifications=[{'name': '套装内容', 'value': '球拍、带线回弹网球一个、底座'}],
        usage_instructions='单人户外练习，使用前检查系绳和底座。',
        knowledge='供货参考价与直播零售价不能混用；未知性能与售后不作承诺。',
        core_selling_points=[], selling_points='',
    ).model_dump()
    return (await human_command(service, 'save_product', {
        'fields': fields, 'schema_hash': service.schema()['schema_hash'], 'patches': [],
    }))['data']['product_ref']


@pytest.fixture
async def case(env):
    engine, service = env
    ref = await create_tennis(service)
    text = '整理' + ref['id'] + '的商品资料，缺少核心卖点时先问我，确认后保存；不要生成话术或语音'
    run = await new_run(engine, text)
    assessment = IntentAssessment(objective='catalog_update', summary='整理指定商品，按需补充核心卖点',
        target_product_ids=[ref['id']], authorization_quote=text)
    run = engine.store.update('run', run['id'], intent_assessment=assessment.model_dump())
    await engine._freeze_goal(run)
    run = engine.store.get('run', run['id'])
    assert run['phase'] == 'execute' and run['status'] == 'RUNNING'
    return SimpleNamespace(engine=engine, service=service, ref=ref, run=run,
        scope=RunScope(run['id'], 'selling-points-acceptance', 'execute'))


def candidates():
    return [{
        'selling_point': '球拍、带线回弹网球和底座组成一套，适合想做单人练习时按套装使用。',
        'audience_need': '一个人安排练习', 'use_scene': '户外单人基础练习',
        'evidence': [{'product_field': '/specifications/0/value', 'fact': '球拍、带线回弹网球一个、底座'}],
        'limitations': '未承诺回弹次数、训练效果或使用寿命。',
    }, {
        'selling_point': '拍杆采用镁合金，网线为聚酯纤维；下单前可按已有材质信息了解套装。',
        'audience_need': '了解所买套装的基础材质', 'use_scene': '购买前比较商品资料',
        'evidence': [{'product_field': '/material', 'fact': '拍杆材质：镁合金；网线材质：聚酯纤维'}],
        'limitations': '不从材质推导轻量、防断、专业比赛等级或适合儿童。',
    }]


async def execute(case, action, parameters, call_id=None):
    from shengbo_prelive.selling_points import execute as invoke
    return await invoke(case.engine, case.scope, {'action': action, 'parameters': parameters},
                        call_id or 'acceptance-' + action)


async def ask(case, monkeypatch, action, parameters, call_id=None):
    monkeypatch.setattr('shengbo_prelive.runtime.interrupt', stop_at_interrupt)
    with pytest.raises(GraphInterrupt):
        await execute(case, action, parameters, call_id)
    questions = case.engine.get_run(case.run['id'])['questions']
    assert len(questions) == 1, questions
    return questions[0]


async def answer(case, question, value, request_id=None):
    return await case.engine.answer(question['id'], AnswerRequest(
        question_revision=question['revision'], answer=value,
        client_request_id=request_id or question['id'] + '-answer'))


async def generate_permission(case, monkeypatch, direction='面向独自练习的入门成年人，不夸大性能'):
    q = await ask(case, monkeypatch, 'offer', {'product_ref': case.ref})
    assert q['kind'] == 'selling_points_generation'
    assert {'generate', 'skip'} <= {o['value'] for o in q['options']}
    await answer(case, q, {'choice': 'generate', 'text': direction})
    return q


async def proposed(case, monkeypatch, items=None, call_id=None):
    q = await ask(case, monkeypatch, 'propose', {'product_ref': case.ref,
        'items': items or candidates()}, call_id)
    assert q['kind'] == 'selling_points_confirmation'
    return q


def proposal_id(question):
    return question['context']['proposal_id']


async def catalog(case):
    return await case.service.query('read_product', {'product_id': case.ref['id']})


def fields(snapshot):
    return {key: deepcopy(value) for key, value in snapshot['body'].items() if key in Product.model_fields}


def assert_no_script_or_audio(case):
    with case.service.store.transaction() as db:
        assert case.service.store.all(db, 'preparation') == []
        assert case.service.store.all(db, 'job') == []
    for operation in case.engine.store.list('operation'):
        assert operation['key'] not in {'submit_script_change', 'patch_script',
            'prepare_required_audio', 'publish_preparation'}


@pytest.mark.asyncio
async def test_report_submit_drafts_two_exact_products_without_repeating_generation_question(env, monkeypatch):
    engine, service = env
    first = await create_tennis(service)
    snapshot = await service.query('read_product', {'ref': first})
    second = (await human_command(service, 'save_product', {
        'fields': {**fields(snapshot), 'name': '隔离测试单人网球回弹训练器 双球款', 'sku_id': 'TEST-SECOND'},
        'schema_hash': service.schema()['schema_hash'], 'patches': [],
    }))['data']['product_ref']
    refs = [first, second]
    conversation = engine.create_conversation({'scope': {'kind': 'catalog'}})
    selection = {'items': [{'label': '核心卖点', 'target': {'object_ref': ref, 'field_path': '/core_selling_points'}}
                           for ref in refs]}
    text = '请为选中的商品生成核心卖点候选。只修改核心卖点，确认后再保存。'
    request = MessageRequest(text=text, intent_mode='new_task', client_request_id='explicit-selling-points',
        view_context={'view': 'products', 'selling_points_action': 'generate'},
        selection=selection, observed_versions=refs)
    with pytest.raises(ContractError, match='SELLING_POINTS_REQUEST_INVALID'):
        await engine.submit_message(conversation['id'], request.model_copy(update={'observed_versions': request.observed_versions[:1]}))
    accepted = await engine.submit_message(conversation['id'], request)
    rid = accepted['run']['id']
    intent = IntentAssessment(objective='catalog_update', summary='仅整理选中两款的核心卖点',
        target_product_ids=[ref['id'] for ref in refs], authorization_quote=text,
        requested_product_paths=['/core_selling_points'], product_field_scope_quote='只修改核心卖点')
    with pytest.raises(ContractError, match='SELLING_POINTS_INPUT_INVALID'):
        engine.save_assessment(RunScope(rid, 'bad-intent', 'intent'), intent.model_copy(update={'target_product_ids': ['unselected']}))
    engine.save_assessment(RunScope(rid, 'explicit-intent', 'intent'), intent)
    await engine._freeze_goal(engine.store.get('run', rid))
    run = engine.store.get('run', rid)
    case = SimpleNamespace(engine=engine, service=service, ref=first, run=run,
                           scope=RunScope(rid, 'explicit-selling-points', 'execute'))

    async def blocked_raw_save(ref):
        current = await service.query('read_product', {'ref': ref})
        raw_points = ['尚未确认的卖点']
        with pytest.raises(ContractError, match='SELLING_POINTS_CONFIRMATION_REQUIRED'):
            await engine.business_command(case.scope, 'save_product', {'product_id': ref['id'],
                'expected_revision': ref['revision'], 'schema_hash': service.schema()['schema_hash'],
                'fields': {**fields(current), 'core_selling_points': raw_points, 'selling_points': raw_points[0]},
                'patches': [{'path': '/core_selling_points', 'op': 'set', 'value': raw_points}]}, 'unapproved-' + ref['id'])

    for index, ref in enumerate(refs):
        case.ref = ref
        await blocked_raw_save(ref)
        offer = await execute(case, 'offer', {'product_ref': ref}, 'offer-explicit-' + str(index))
        assert offer['status'] == 'generation_requested' and 'human_receipt' not in offer
        assert not engine.get_run(rid)['questions']
        question = await proposed(case, monkeypatch, call_id='propose-explicit-' + str(index))
        assert question['preview']['product_ref']['id'] == ref['id']
        assert question['preview']['generation_intent']['source'] == 'submitted_product_selection'
        await blocked_raw_save(ref)
        await answer(case, question, 'approve')
        await execute(case, 'save', {'proposal_id': proposal_id(question)}, 'save-explicit-' + str(index))
        assert (await catalog(case))['body']['core_selling_points']
    questions = [item for item in engine.store.list('question') if item['run_id'] == rid]
    assert [item['kind'] for item in questions] == ['selling_points_confirmation'] * 2


@pytest.mark.asyncio
async def test_approved_exact_candidate_saves_points_only_and_replay_is_idempotent(case, monkeypatch):
    before = await catalog(case)
    await generate_permission(case, monkeypatch)
    q = await proposed(case, monkeypatch)
    assert (await catalog(case)) == before, 'generating candidates must not write the catalog'
    with pytest.raises(ContractError, match='SELLING_POINTS_CONFIRMATION_REQUIRED'):
        await execute(case, 'save', {'proposal_id': proposal_id(q)})
    await answer(case, q, 'approve')
    saved = await execute(case, 'save', {'proposal_id': proposal_id(q)})
    after = await catalog(case)
    expected = [row['selling_point'] for row in candidates()]
    assert after['body']['core_selling_points'] == expected
    assert after['body']['selling_points'] == '\n'.join(expected)
    for key, value in before['body'].items():
        if key not in {'core_selling_points', 'selling_points', 'updated_at'}:
            assert after['body'][key] == value, key
    assert saved['product_ref'] == after['ref']
    replay = await execute(case, 'save', {'proposal_id': proposal_id(q)})
    assert replay['product_ref'] == after['ref']
    assert (await catalog(case))['ref'] == after['ref']
    assert_no_script_or_audio(case)


@pytest.mark.asyncio
async def test_merchant_may_skip_generation_and_keep_basic_product_without_open_question(case, monkeypatch):
    before = await catalog(case)
    q = await ask(case, monkeypatch, 'offer', {'product_ref': case.ref})
    await answer(case, q, 'skip')
    assert (await catalog(case)) == before
    with pytest.raises(ContractError):
        await execute(case, 'propose', {'product_ref': case.ref, 'items': candidates()})
    assert case.engine.store.get('question', q['id'])['status'] == 'ANSWERED'
    assert case.engine.get_run(case.run['id'])['questions'] == []
    assert (await catalog(case)) == before
    assert_no_script_or_audio(case)


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['reject', {'choice': 'revise', 'text': '减少材料介绍，突出套装组成'}])
async def test_reject_or_revise_never_saves_or_reuses_prior_approval(case, monkeypatch, value):
    before = await catalog(case)
    await generate_permission(case, monkeypatch)
    q = await proposed(case, monkeypatch)
    await answer(case, q, value)
    with pytest.raises(ContractError):
        await execute(case, 'save', {'proposal_id': proposal_id(q)})
    assert (await catalog(case)) == before
    assert_no_script_or_audio(case)


@pytest.mark.asyncio
async def test_missing_generation_permission_and_fabricated_fact_quotes_are_rejected(case, monkeypatch):
    with pytest.raises(ContractError):
        await execute(case, 'propose', {'product_ref': case.ref, 'items': candidates()})
    await generate_permission(case, monkeypatch)
    bad = candidates()
    bad[0]['evidence'][0]['fact'] = '实测连续回弹十万次仍不损坏'
    with pytest.raises(ContractError, match='SELLING_POINTS_EVIDENCE_INVALID'):
        await execute(case, 'propose', {'product_ref': case.ref, 'items': bad})
    missing = candidates()
    missing[0]['evidence'] = []
    with pytest.raises(ContractError):
        await execute(case, 'propose', {'product_ref': case.ref, 'items': missing})
    assert (await catalog(case))['body']['core_selling_points'] == []


@pytest.mark.asyncio
async def test_catalog_revision_change_invalidates_pending_confirmation(case, monkeypatch):
    await generate_permission(case, monkeypatch)
    q = await proposed(case, monkeypatch)
    await human_command(case.service, 'save_product', {'product_id': case.ref['id'],
        'expected_revision': case.ref['revision'], 'schema_hash': case.service.schema()['schema_hash'],
        'fields': {**fields(await catalog(case)), 'price': 80},
        'patches': [{'path': '/price', 'op': 'set', 'value': 80}]})
    with pytest.raises(ContractError, match='SELLING_POINTS_PRODUCT_STALE'):
        await answer(case, q, 'approve')
    assert case.engine.store.get('question', q['id'])['status'] == 'SUPERSEDED'
    assert (await catalog(case))['body']['core_selling_points'] == []


@pytest.mark.asyncio
async def test_original_save_product_cannot_bypass_pending_or_approved_candidate(case, monkeypatch):
    await generate_permission(case, monkeypatch)
    q = await proposed(case, monkeypatch)
    args = {'product_id': case.ref['id'], 'expected_revision': case.ref['revision'],
        'schema_hash': case.service.schema()['schema_hash'],
        'fields': {**fields(await catalog(case)), 'core_selling_points': ['未经确认的任意卖点'], 'selling_points': '未经确认的任意卖点'},
        'patches': [{'path': '/core_selling_points', 'op': 'set', 'value': ['未经确认的任意卖点']}]}
    with pytest.raises(ContractError):
        await case.engine.business_command(case.scope, 'save_product', args, 'bypass-before-confirm')
    await answer(case, q, 'approve')
    with pytest.raises(ContractError, match='SELLING_POINTS_BINDING_MISMATCH'):
        await case.engine.business_command(case.scope, 'save_product', args, 'bypass-replace-approved')
    assert (await catalog(case))['body']['core_selling_points'] == []


@pytest.mark.asyncio
async def test_new_candidate_cannot_use_old_candidate_approval(case, monkeypatch):
    await generate_permission(case, monkeypatch)
    first = await proposed(case, monkeypatch, call_id='candidate-one')
    await answer(case, first, 'approve')
    different = candidates()
    different[0]['selling_point'] = '套装包含球拍、带线回弹网球一个及底座，可了解配件后再选择。'
    second = await proposed(case, monkeypatch, items=different, call_id='candidate-two')
    assert proposal_id(first) != proposal_id(second)
    with pytest.raises(ContractError, match='SELLING_POINTS_PROPOSAL_CLOSED'):
        await execute(case, 'save', {'proposal_id': proposal_id(first)})
    with pytest.raises(ContractError, match='SELLING_POINTS_CONFIRMATION_REQUIRED'):
        await execute(case, 'save', {'proposal_id': proposal_id(second)})
    assert (await catalog(case))['body']['core_selling_points'] == []


@pytest.mark.asyncio
async def test_approval_with_edits_requires_new_candidate_not_implicit_save(case, monkeypatch):
    await generate_permission(case, monkeypatch)
    q = await proposed(case, monkeypatch)
    with pytest.raises(ContractError):
        await answer(case, q, {'choice': 'approve', 'text': '顺便改成专业比赛级'})
    assert case.engine.store.get('question', q['id'])['status'] == 'OPEN'
    assert (await catalog(case))['body']['core_selling_points'] == []


@pytest.mark.asyncio
async def test_known_committed_save_recovers_without_second_logical_write(case, monkeypatch):
    await generate_permission(case, monkeypatch)
    q = await proposed(case, monkeypatch)
    await answer(case, q, 'approve')
    real_update = case.engine.store.update
    crashed = False

    def crash_before_local_saved(kind, identity, **updates):
        nonlocal crashed
        if kind == 'selling_point_proposal' and updates.get('status') == 'SAVED' and not crashed:
            crashed = True
            raise RuntimeError('simulate local crash after known business commit')
        return real_update(kind, identity, **updates)

    monkeypatch.setattr(case.engine.store, 'update', crash_before_local_saved)
    with pytest.raises(RuntimeError, match='simulate local crash'):
        await execute(case, 'save', {'proposal_id': proposal_id(q)})
    first_ref = (await catalog(case))['ref']
    assert first_ref['revision'] == case.ref['revision'] + 1
    recovered = await execute(case, 'save', {'proposal_id': proposal_id(q)})
    assert recovered['product_ref'] == first_ref == (await catalog(case))['ref']
    committed = [op for op in case.engine.store.list('operation')
                 if op['key'] == 'save_product' and op['status'] == 'committed']
    assert len(committed) == 1
    assert_no_script_or_audio(case)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage,value', [('generation', 'skip'), ('confirmation', 'reject'),
                                       ('confirmation', {'choice': 'revise', 'text': '换方向再整理'})])
async def test_negative_answer_can_close_question_even_if_product_has_changed(case, monkeypatch, stage, value):
    if stage == 'generation':
        q = await ask(case, monkeypatch, 'offer', {'product_ref': case.ref})
    else:
        await generate_permission(case, monkeypatch)
        q = await proposed(case, monkeypatch)
    await human_command(case.service, 'save_product', {'product_id': case.ref['id'],
        'expected_revision': case.ref['revision'], 'schema_hash': case.service.schema()['schema_hash'],
        'fields': {**fields(await catalog(case)), 'price': 80},
        'patches': [{'path': '/price', 'op': 'set', 'value': 80}]})
    await answer(case, q, value)
    assert case.engine.store.get('question', q['id'])['status'] == 'ANSWERED'
    assert not case.engine.get_run(case.run['id'])['questions']
    assert (await catalog(case))['body']['core_selling_points'] == []


@pytest.mark.asyncio
async def test_existing_accepted_customer_research_and_human_direction_are_bound_to_candidate(case, monkeypatch):
    report = AnalysisReport(summary='单人基础练习场景假设，尚无真实转化数据', product_ids=[case.ref['id']],
        dependencies=[case.ref], insights=[{'audience_need': '独自安排练习', 'use_scene': '户外基础练习'}],
        hypotheses=['用户偏好需后续真实验证'])
    receipt = await human_command(case.service, 'accept_analysis_report', {'role': 'audience_scenes',
        'report': report.model_dump(mode='json'), 'report_hash': canonical_hash(report),
        'product_refs': [case.ref]})
    research_ref = receipt['data']['analysis_ref']
    discovered = await case.engine.business_query(case.scope, 'find_audience_research', {'ref': case.ref})
    assert any(row['research_ref'] == research_ref for row in discovered['items'])
    direction = '面向下班后独自练习的成年人，突出套装组成'
    await generate_permission(case, monkeypatch, direction)
    q = await ask(case, monkeypatch, 'propose', {'product_ref': case.ref, 'items': candidates(),
        'research_refs': [research_ref]})
    assert q['preview']['research_refs'] == [research_ref]
    assert q['preview']['direction'] == direction
    assert q['preview']['requested_direction'] == direction
    assert (await catalog(case))['body']['core_selling_points'] == []
    await answer(case, q, 'approve')
    result = await execute(case, 'save', {'proposal_id': proposal_id(q)})
    assert result['product_ref']['revision'] == case.ref['revision'] + 1
    assert_no_script_or_audio(case)


@pytest.mark.asyncio
async def test_catalog_completion_requires_research_for_final_saved_revision_not_intermediate(case, monkeypatch):
    initial = await catalog(case)
    initial_fields = fields(initial)
    initial_fields['usage_instructions'] += '选择适合练习的空旷区域。'
    first_save = await case.engine.business_command(case.scope, 'save_product', {
        'product_id': case.ref['id'], 'expected_revision': case.ref['revision'],
        'schema_hash': case.service.schema()['schema_hash'], 'fields': initial_fields,
        'patches': [{'path': '/usage_instructions', 'op': 'set', 'value': initial_fields['usage_instructions']}],
    }, 'catalog-initial-facts')
    intermediate_ref = first_save['data']['product_ref']
    case.ref = intermediate_ref
    await generate_permission(case, monkeypatch)
    question = await proposed(case, monkeypatch)
    await answer(case, question, 'approve')
    final = await execute(case, 'save', {'proposal_id': proposal_id(question)})
    final_ref = final['product_ref']
    assert final_ref['revision'] == intermediate_ref['revision'] + 1
    await case.engine.business_query(case.scope, 'inspect_change_impact', {'product_id': final_ref['id']})
    case.engine.store.update('run', case.run['id'], finish={'outcome': 'COMPLETED', 'summary': '已整理商品并保存确认卖点'})
    blocked = await case.engine.verify_finish(case.scope)
    research_missing = [m for m in blocked['missing'] if '当前修订的有效顾客研究' in m]
    assert len(research_missing) == 1, blocked
    assert not blocked['ok'], 'final current revision research remains mandatory'
    report = AnalysisReport(summary='最新商品资料及确认卖点对应的顾客研究',
        product_ids=[final_ref['id']], dependencies=[final_ref], hypotheses=['场景偏好需真实验证'])
    await human_command(case.service, 'accept_analysis_report', {'role': 'audience_scenes',
        'report': report.model_dump(mode='json'), 'report_hash': canonical_hash(report), 'product_refs': [final_ref]})
    completed = await case.engine.verify_finish(case.scope)
    assert completed['ok'], completed
    assert_no_script_or_audio(case)


class NativeSellingPointsModel(BaseChatModel):
    _logic = PrivateAttr()

    def __init__(self, logic):
        super().__init__()
        self._logic = logic

    @property
    def _llm_type(self):
        return 'isolated-selling-points-native-harness'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        raise AssertionError('Only async native Agent execution is expected')

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=await self._logic(messages))])


async def wait_question(engine, run_id, kind):
    for _ in range(240):
        run = engine.get_run(run_id)
        if run['status'] == 'WAITING_USER' and any(q['kind'] == kind for q in run['questions']):
            return next(q for q in run['questions'] if q['kind'] == kind)
        if run['status'] in {'FAILED', 'BLOCKED', 'CANCELLED'}:
            pytest.fail(str(run.get('error') or run))
        await asyncio.sleep(.025)
    pytest.fail('native graph did not reach decision ' + kind + ': ' + str(run))


@pytest.mark.asyncio
async def test_native_deepagents_tool_interrupt_and_resume_save_exact_approved_points(env):
    engine, service = env
    ref = await create_tennis(service)
    text = '整理' + ref['id'] + '的商品资料，缺少核心卖点时先问我，确认后保存；不要生成话术或语音'
    count = 0

    def call(name, arguments):
        nonlocal count
        count += 1
        assert count < 20, 'unexpected tool loop'
        return AIMessage(content='', tool_calls=[{'id': 'native-points-' + str(count),
            'name': name, 'args': arguments}])

    async def logic(messages):
        scope = CURRENT_SCOPE.get()
        run = engine.store.get('run', scope.run_id)
        if scope.phase == 'intent':
            if not run.get('intent_assessment'):
                return call('save_task_assessment', {'assessment': {'objective': 'catalog_update',
                    'summary': '整理明确商品并按需补写核心卖点', 'target_product_ids': [ref['id']],
                    'authorization_quote': text}})
            return AIMessage(content='已明确仅整理商品资料与卖点，不生成话术或语音。')
        if not run.get('task_assessment'):
            return call('save_task_assessment', {'assessment': {'summary': '生成选择、候选确认、精确保存',
                'capabilities': ['wf_selling_points'], 'steps': ['询问', '提炼候选', '确认后保存']}})
        questions = [q for q in engine.store.list('question') if q['run_id'] == run['id']]
        if not any(q['kind'] == 'selling_points_generation' and q['status'] == 'ANSWERED' for q in questions):
            return call('wf_selling_points', {'action': 'offer', 'parameters': {'product_ref': ref}})
        confirmations = [q for q in questions if q['kind'] == 'selling_points_confirmation']
        if not confirmations:
            return call('wf_selling_points', {'action': 'propose', 'parameters': {
                'product_ref': ref, 'items': candidates()}})
        latest = confirmations[-1]
        product = await service.query('read_product', {'product_id': ref['id']})
        if not product['body']['core_selling_points']:
            return call('wf_selling_points', {'action': 'save', 'parameters': {'proposal_id': proposal_id(latest)}})
        return call('request_user_decision', {'kind': 'fact_check',
            'prompt': '隔离原生图已保存确认卖点，停在此处，不执行后续话术或语音。'})

    engine._provided_model = NativeSellingPointsModel(logic)
    await engine.start()
    run = await new_run(engine, text)
    case = SimpleNamespace(engine=engine, service=service, ref=ref, run=run)
    q = await wait_question(engine, run['id'], 'selling_points_generation')
    assert (await catalog(case))['body']['core_selling_points'] == []
    await answer(case, q, {'choice': 'generate', 'text': '突出单人练习和套装组成，勿编造性能'})
    q = await wait_question(engine, run['id'], 'selling_points_confirmation')
    assert (await catalog(case))['body']['core_selling_points'] == []
    await answer(case, q, 'approve')
    await wait_question(engine, run['id'], 'fact_check')
    assert (await catalog(case))['body']['core_selling_points'] == [r['selling_point'] for r in candidates()]
    assert len([q for q in engine.store.list('question') if q['kind'] == 'selling_points_generation']) == 1
    assert len([q for q in engine.store.list('question') if q['kind'] == 'selling_points_confirmation']) == 1
    events = engine.trace.events(run['id'])['events']
    assert any(e['type'] == 'workflow.completed' and e['data']['duration_ms'] >= 0 for e in events)
    assert any(e['type'] == 'selling_points.proposed' for e in events)
    assert any(e['type'] == 'selling_points.saved' for e in events)
    assert_no_script_or_audio(case)
