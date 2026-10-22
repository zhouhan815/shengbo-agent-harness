"""Real RuntimeEngine + isolated voice business + fake SDK integration tests."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import sys

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt
import pytest
from pydantic import ValidationError

VOICE_ROOT = Path('D:/Codex project/ecommerce-voice-agent')
if str(VOICE_ROOT) not in sys.path:
    sys.path.insert(0, str(VOICE_ROOT))
from app.prelive import PreliveService
from app.prelive.service import Product
from app.prelive.storage import BusinessError
from shengbo_contracts.business import AnalysisReport as SharedReport, MODULE_KEYS
from shengbo_contracts.common import ArtifactRef, ContractError, MutationContext, Principal, WorkScope, canonical_hash
from shengbo_prelive.models import AnalysisReport, AnswerRequest, IntentAssessment, MessageRequest, RunScope
from shengbo_prelive.runtime import RuntimeEngine


class FakeAdapter:
    def voice_options(self):
        return {"providers": ["system"], "voices": ["Huihui"]}

    async def generate_module(self, context, module_key, product, previous):
        return [product['name'] + '：' + module_key + '的真实隔离测试文字。']

    async def render_audio(self, *args):
        raise AssertionError('This integration suite must not perform TTS')


class DirectBusinessClient:
    """Same shared DTO/services as HTTP; no network or live files."""
    def __init__(self, service):
        self.service = service

    def principal(self, run_id, controller):
        return Principal(kind='controller' if controller else 'agent', actor_id='controller-runtime' if controller else 'agent-runtime', run_id=run_id)

    async def query(self, key, args=None, *, scope=None, run_id=None, controller=False):
        try:
            return await self.service.query(key, args or {}, principal=self.principal(run_id, controller), scope=scope)
        except BusinessError as e:
            raise ContractError(e.code, e.message, status=e.status, details=e.details) from None

    async def command(self, key, args, mutation, *, scope=None, run_id=None, controller=False):
        try:
            return await self.service.execute_command(key, args, mutation, self.principal(run_id, controller), scope=scope)
        except BusinessError as e:
            raise ContractError(e.code, e.message, status=e.status, details=e.details) from None

    async def read_identity_decision(self, ref, *, run_id=None):
        try:
            return self.service.read_identity_decision(ref, principal=self.principal(run_id, True))
        except BusinessError as e:
            raise ContractError(e.code, e.message, status=e.status, details=e.details) from None

    async def close(self):
        await self.service.close()


class BindableFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
async def env(tmp_path):
    service = PreliveService(tmp_path / 'voice', FakeAdapter())
    client = DirectBusinessClient(service)
    engine = RuntimeEngine(tmp_path / 'agent', client, model=BindableFakeModel(responses=[AIMessage(content='fake')]))
    yield engine, service
    await engine.stop()


async def human_command(service, key, args, operation=None):
    return await service.execute_command(key, args, MutationContext(operation_id=operation or 'test-' + key + '-' + canonical_hash(args)[7:23]), Principal(kind='human'))


async def product(service, name='测试拖鞋'):
    fields = Product(name=name, price=178, selling_points='舒适轻便').model_dump()
    return (await human_command(service, 'save_product', {'fields': fields, 'schema_hash': service.schema()['schema_hash'], 'patches': []}))['data']['product_ref']


async def new_run(engine, text, *, room_id=None, preparation_id=None):
    scope = {'kind': 'preparation', 'account_id': 'local', 'preparation_id': preparation_id} if preparation_id else {'kind': 'catalog', 'account_id': 'local'}
    conversation = engine.create_conversation({'room_id': room_id, 'scope': scope})
    result = await engine.submit_message(conversation['id'], MessageRequest(text=text, client_request_id='request-' + canonical_hash(text)[7:20], intent_mode='new_task', scope=WorkScope.model_validate(scope)))
    return engine.store.get('run', result['run']['id'])


async def prepared_text(service):
    p = await product(service)
    room = (await human_command(service, 'create_room', {'name': '隔离测试直播间'}))['data']['room_ref']
    prep = (await human_command(service, 'create_preparation', {'room_id': room['id'], 'room_revision': room['revision']}))['data']['preparation_ref']
    selected = await human_command(service, 'update_preparation_products', {'preparation_id': prep['id'], 'expected_revision': prep['revision'], 'selected_product_refs': [p]})
    prep = selected['data']['preparation_ref']
    draft = await human_command(service, 'save_session_draft', {'preparation_id': prep['id'], 'expected_revision': prep['revision'], 'draft': {'theme': '隔离测试', 'commerce': {}}})
    prep = draft['data']['preparation_ref']
    bundle = await human_command(service, 'save_context_bundle', {'preparation_id': prep['id'], 'expected_revision': prep['revision'], 'product_refs': [p], 'draft_ref': draft['data']['draft_ref']})
    prep = bundle['data']['preparation_ref']
    plan = await human_command(service, 'save_script_plan', {'preparation_id': prep['id'], 'expected_revision': prep['revision'], 'bundle_ref': bundle['data']['context_bundle_ref'], 'items': [{'product_ref': p, 'variant_id': 'test-variant', 'modules': [{'key': key, 'action': 'disable' if key in {'campaign', 'transition'} else 'generate'} for key in MODULE_KEYS]}]})
    prep = plan['data']['preparation_ref']
    await human_command(service, 'submit_script_change', {'preparation_id': prep['id'], 'expected_revision': prep['revision'], 'plan_ref': plan['data']['script_plan_ref'], 'context_bundle_ref': bundle['data']['context_bundle_ref'], 'expected_generation_epoch': plan['data']['generation_epoch']})
    await service.drain_jobs()
    current = await service.query('read_preparation', {'preparation_id': prep['id']})
    script = current['body']['script_refs'][0]
    order = await human_command(service, 'save_playlist', {'preparation_id': prep['id'], 'expected_revision': current['ref']['revision'], 'items': [{'occurrence_id': 'test-o1', 'product_id': p['id'], 'script_revision_ref': script}], 'loop_mode': 'once'})
    compiled = await human_command(service, 'compile_playback_plan', {'preparation_id': prep['id'], 'expected_revision': order['data']['preparation_ref']['revision'], 'playlist_ref': order['data']['playlist_ref']})
    return room, compiled['data']['preparation_ref'], script


def stop_at_interrupt(value):
    raise GraphInterrupt((Interrupt(value=value),))


@pytest.mark.asyncio
async def test_report_type_and_conversation_creation_are_single_contract_and_idempotent(env):
    engine, service = env
    assert AnalysisReport is SharedReport
    first = engine.create_conversation({'client_request_id': 'create-once', 'scope': {'kind': 'catalog'}})
    assert engine.create_conversation({'client_request_id': 'create-once', 'scope': {'kind': 'catalog'}}) == first
    with pytest.raises(ContractError, match='IDEMPOTENCY_CONFLICT'):
        engine.create_conversation({'client_request_id': 'create-once', 'title': 'changed', 'scope': {'kind': 'catalog'}})


@pytest.mark.asyncio
async def test_prepare_goal_initializes_only_human_room_selection_and_workspace(env):
    engine, service = env
    selected = await product(service)
    other = await product(service, '不应自动选择的商品')
    room = (await human_command(service, 'create_room', {'name': '测试房间'}))['data']['room_ref']
    with service.store.transaction(write=True) as db:
        old = service.store.get(db, 'config', room['id'])
        body = {**old['body'], 'product_ids': [selected['id']]}
        service.store.put(db, 'config', room['id'], body, expected=old['ref']['revision'])
    run = await new_run(engine, '准备整场直播，先不生成语音', room_id=room['id'])
    proposal = IntentAssessment(objective='prepare_session', script_delivery_requirement='new_generation', summary='准备整场', authorization_quote=run['input']['text'])
    run = engine.store.update('run', run['id'], intent_assessment=proposal.model_dump())
    await engine._freeze_goal(run)
    current = engine.store.get('run', run['id'])
    preparation = await service.query('read_preparation', {'preparation_id': current['scope']['preparation_id']})
    assert [ref['id'] for ref in preparation['body']['selected_product_refs']] == [selected['id']]
    assert other['id'] not in current['target_product_ids']
    assert current['scope_key'] == canonical_hash(current['scope'])
    competing = {**current, 'id': 'another-run', 'run_id': 'another-run'}
    assert not engine.store.acquire_scope(competing), 'resolved preparation remains exclusively owned'
    original_tools = set("""
        accept_analysis_reports build_context_bundle compile_playback_plan dispatch_analysis_batch execute_script_plan
        find_audience_research finish_turn import_recap inspect_change_impact link_recap_to_preparation
        prepare_required_audio preview_playlist_reorder publish_preparation read_analysis_batch read_asset_checklist
        read_audio_readiness read_business_schema read_capabilities read_context read_job read_preparation
        read_product read_recap read_room read_run_history read_script read_script_usage read_session_history
        read_source read_text_review read_voice_options request_user_decision retry_failed_items save_asset_checklist
        save_playlist save_product save_script_change_plan save_session_draft save_task_assessment search_catalog
        update_preparation_products validate_product_assembly wf_analysis_coordination wf_context_bundle
        wf_finalize_preparation wf_playback_review wf_product_materials wf_required_audio wf_script_execution wf_selling_points
    """.split())
    assert len(engine.main_tool_names) == 56
    assert 'read_generation_output' in engine.main_tool_names
    assert engine.main_tool_names - {'read_generation_output', 'patch_script', 'import_script',
        'read_script_audio_preview', 'list_script_audio_previews', 'preview_script_audio'} == original_tools
    assert "read_audio_readiness" in engine.main_tool_names
    snapshot = await engine.workspace(conversation_id=run['conversation_id'])
    assert snapshot['voice_snapshot']['preparation']['ref']['id'] == current['scope']['preparation_id']
    assert engine.get_run(run['id'])['artifacts'][-1]['destination']['view'] == 'preparation'


@pytest.mark.asyncio
async def test_uploaded_new_products_confirm_once_then_create_and_select_without_manual_catalog(env):
    engine, service = env
    room = (await human_command(service, 'create_room', {'name': '全新商品导入测试'}))['data']['room_ref']
    conversation = engine.create_conversation({'room_id': room['id'], 'scope': {'kind': 'catalog'}})
    upload = engine.accept_material(conversation['id'], 'new-product.txt', 'text/plain', '商品名称：测试情人节精华；价格99元。'.encode('utf-8'))
    text = '准备整场直播，请整理上传商品资料并生成话术，先不生成语音'
    accepted = await engine.submit_message(conversation['id'], MessageRequest(text=text, client_request_id='import-new', intent_mode='new_task', material_refs=[upload['material_ref']]))
    run = engine.store.get('run', accepted['run']['id'])
    proposal = IntentAssessment(objective='prepare_session', script_delivery_requirement='new_generation', summary='准备上传商品的整场直播', authorization_quote=text)
    run = engine.store.update('run', run['id'], intent_assessment=proposal.model_dump())
    # A generic, unrelated scope confirmation cannot authorize new-product import.
    engine.store.put('question', {'id': 'unrelated-confirm', 'run_id': run['id'], 'kind': 'task_scope', 'status': 'ANSWERED', 'answer': 'confirm'})
    await engine._freeze_goal(run)
    current = engine.get_run(run['id'])
    assert current['status'] == 'WAITING_USER' and not current.get('goal')
    question = current['questions'][0]
    assert question['kind'] == 'new_product_import'
    with pytest.raises(ContractError, match='ANSWER_INVALID'):
        await engine.answer(question['id'], AnswerRequest(question_revision=question['revision'], answer='confirm', client_request_id='generic-not-import'))
    engine.store.update('run', run['id'], intent_assessment={**proposal.model_dump(), 'summary': '已变化的导入提案'})
    with pytest.raises(ContractError, match='DECISION_STALE'):
        await engine.answer(question['id'], AnswerRequest(question_revision=question['revision'], answer='confirm_new_products', client_request_id='stale-import'))
    engine.store.update('run', run['id'], intent_assessment=proposal.model_dump())
    await engine.answer(question['id'], AnswerRequest(question_revision=question['revision'], answer='confirm_new_products', client_request_id='confirm-import'))
    run = engine.store.get('run', run['id'])
    assert not run.get('confirmed_intent_hash'), 'import confirmation cannot approve an unrelated intent scope'
    await engine._freeze_goal(run)
    current = engine.get_run(run['id'])
    assert current['phase'] == 'execute' and current['status'] == 'RUNNING'
    assert not current['questions'] and current['target_product_ids'] == []
    prep_id = current['scope']['preparation_id']
    prep = await service.query('read_preparation', {'preparation_id': prep_id})
    assert prep['body']['selected_product_refs'] == []
    scope = RunScope(run['id'], 'new-import-test', 'execute')
    fields = Product(name='测试情人节精华', price=99).model_dump()
    created = await engine.business_command(scope, 'save_product', {'fields': fields, 'schema_hash': service.schema()['schema_hash'], 'patches': []}, 'create-from-upload')
    new_ref = created['data']['product_ref']
    await engine.business_command(scope, 'update_preparation_products', {'preparation_id': prep_id, 'expected_revision': prep['ref']['revision'], 'selected_product_refs': [new_ref]}, 'select-from-upload')
    assert (await service.query('read_preparation', {'preparation_id': prep_id}))['body']['selected_product_refs'] == [new_ref]
    assert len([q for q in engine.store.list('question') if q.get('kind') == 'new_product_import']) == 1


@pytest.mark.asyncio
async def test_no_materials_cannot_create_empty_preparation(env):
    engine, service = env
    room = (await human_command(service, 'create_room', {'name': '禁止猜选品测试'}))['data']['room_ref']
    run = await new_run(engine, '准备整场直播，先不生成语音', room_id=room['id'])
    proposal = IntentAssessment(objective='prepare_session', script_delivery_requirement='new_generation', summary='准备整场', authorization_quote=run['input']['text'])
    run = engine.store.update('run', run['id'], intent_assessment=proposal.model_dump())
    await engine._freeze_goal(run)
    question = engine.get_run(run['id'])['questions'][0]
    assert question['kind'] == 'product_selection_required'
    await engine.answer(question['id'], AnswerRequest(question_revision=question['revision'], answer='confirm', client_request_id='still-no-selection'))
    await engine._freeze_goal(engine.store.get('run', run['id']))
    current = engine.get_run(run['id'])
    assert current['status'] == 'WAITING_USER' and not current.get('goal')
    assert current['scope']['kind'] == 'catalog' and current['questions']
    with service.store.transaction() as db:
        assert service.store.all(db, 'preparation') == []


@pytest.mark.asyncio
async def test_native_scheduler_resumes_new_product_import_confirmation_once(env):
    engine, service = env
    room = (await human_command(service, 'create_room', {'name': '导入恢复图测试'}))['data']['room_ref']
    text = '准备整场直播，请整理上传商品资料，先不生成语音'
    engine._provided_model = BindableFakeModel(responses=[
        AIMessage(content='', tool_calls=[{'id': 'import-intent', 'name': 'save_task_assessment', 'args': {'assessment': {'objective': 'prepare_session', 'script_delivery_requirement': 'new_generation', 'summary': '整理新商品用于本场', 'authorization_quote': text}}}]),
        AIMessage(content='', tool_calls=[{'id': 'import-next', 'name': 'request_user_decision', 'args': {'kind': 'fact_check', 'prompt': '已进入资料核对阶段，请确认商品规格。'}}]),
    ])
    await engine.start()
    conversation = engine.create_conversation({'room_id': room['id'], 'scope': {'kind': 'catalog'}})
    upload = engine.accept_material(conversation['id'], 'new-product.txt', 'text/plain', '商品：测试精华，售价99元。'.encode('utf-8'))
    result = await engine.submit_message(conversation['id'], MessageRequest(text=text, client_request_id='graph-new-import', intent_mode='new_task', material_refs=[upload['material_ref']]))
    for _ in range(180):
        current = engine.get_run(result['run']['id'])
        if current['status'] == 'WAITING_USER' and current['questions']:
            break
        await asyncio.sleep(.05)
    question = current['questions'][0]
    assert question['kind'] == 'new_product_import'
    await engine.answer(question['id'], AnswerRequest(question_revision=question['revision'], answer='confirm_new_products', client_request_id='graph-import-approved'))
    for _ in range(180):
        current = engine.get_run(result['run']['id'])
        if current['phase'] == 'execute' and current['status'] == 'WAITING_USER' and current['questions']:
            break
        await asyncio.sleep(.05)
    assert current['phase'] == 'execute', current
    assert [q['kind'] for q in current['questions']] == ['fact_check']
    assert current['scope']['kind'] == 'preparation'
    assert len([q for q in engine.store.list('question') if q.get('kind') == 'new_product_import']) == 1


@pytest.mark.asyncio
async def test_agent_can_add_own_created_product_without_manual_selection(env):
    engine, service = env
    pinned = await product(service, '已选化妆品')
    room = (await human_command(service, 'create_room', {'name': '新增商品测试'}))['data']['room_ref']
    prep = (await human_command(service, 'create_preparation', {'room_id': room['id'], 'room_revision': room['revision']}))['data']['preparation_ref']
    prep = (await human_command(service, 'update_preparation_products', {'preparation_id': prep['id'], 'expected_revision': prep['revision'], 'selected_product_refs': [pinned]}))['data']['preparation_ref']
    run = await new_run(engine, '准备整场直播，使用已选化妆品并新增测试精华，先不生成语音', preparation_id=prep['id'])
    proposal = IntentAssessment(objective='prepare_session', script_delivery_requirement='new_generation', summary='准备本场并整理新商品', authorization_quote=run['input']['text'], target_product_ids=[pinned['id']])
    run = engine.store.update('run', run['id'], intent_assessment=proposal.model_dump())
    await engine._freeze_goal(run)
    scope = RunScope(run['id'], 'selection-add-test', 'execute')
    assert 'update_preparation_products' in engine.allowed_tool_names(scope)
    assert 'refresh_product_reference' not in engine.main_tool_names
    fields = Product(name='新增测试精华', price=99).model_dump()
    created = await engine.business_command(scope, 'save_product', {'fields': fields, 'schema_hash': service.schema()['schema_hash'], 'patches': []}, 'create-product')
    assert created['data']['created'] is True
    new_ref = created['data']['product_ref']
    current = await service.query('read_preparation', {'preparation_id': prep['id']})
    args = {'preparation_id': prep['id'], 'expected_revision': current['ref']['revision'], 'selected_product_refs': [pinned, new_ref]}
    receipt = await engine.business_command(scope, 'update_preparation_products', args, 'add-created')
    assert (await service.query('read_preparation', {'preparation_id': prep['id']}))['body']['selected_product_refs'] == [pinned, new_ref]
    assert await engine.business_command(scope, 'update_preparation_products', args, 'add-created') == receipt
    frozen = engine.store.get('run', run['id'])
    assert frozen['goal']['target_product_ids'] == [pinned['id']]
    assert frozen['created_product_ids'] == [new_ref['id']]


@pytest.mark.asyncio
async def test_selection_addition_guards_runtime_and_voice_against_refresh_removal_and_expansion(env):
    engine, service = env
    pinned = await product(service, '已选测试商品')
    unrelated = await product(service, '范围外商品')
    room = (await human_command(service, 'create_room', {'name': '选品边界测试'}))['data']['room_ref']
    prep = (await human_command(service, 'create_preparation', {'room_id': room['id'], 'room_revision': room['revision']}))['data']['preparation_ref']
    prep = (await human_command(service, 'update_preparation_products', {'preparation_id': prep['id'], 'expected_revision': prep['revision'], 'selected_product_refs': [pinned]}))['data']['preparation_ref']
    run = await new_run(engine, '准备整场直播，使用已选测试商品，先不生成语音', preparation_id=prep['id'])
    proposal = IntentAssessment(objective='prepare_session', script_delivery_requirement='new_generation', summary='保留明确选品', authorization_quote=run['input']['text'], target_product_ids=[pinned['id']])
    run = engine.store.update('run', run['id'], intent_assessment=proposal.model_dump())
    await engine._freeze_goal(run)
    run = engine.store.get('run', run['id'])
    fields = Product(name='已选测试商品', price=179, selling_points='舒适轻便', sku_id=pinned['id']).model_dump()
    refreshed = (await human_command(service, 'save_product', {'product_id': pinned['id'], 'expected_revision': pinned['revision'], 'fields': fields, 'schema_hash': service.schema()['schema_hash'], 'patches': [{'path': '/price', 'op': 'set', 'value': 179}]}))['data']['product_ref']
    assert refreshed != pinned
    # Mutable runtime projections cannot enlarge the immutable Goal targets.
    engine.store.update('run', run['id'], target_product_ids=[pinned['id'], unrelated['id']])
    scope = RunScope(run['id'], 'selection-guard-test', 'execute')
    principal = Principal(kind='agent', actor_id='agent-runtime', run_id=run['id'])
    cases = [([], 'PRODUCT_SELECTION_REMOVAL_DENIED'), ([refreshed], 'HUMAN_REVISION_CHOICE_REQUIRED'), ([pinned, unrelated], 'TARGET_DENIED')]
    for index, (refs, code) in enumerate(cases):
        args = {'preparation_id': prep['id'], 'expected_revision': prep['revision'], 'selected_product_refs': refs}
        with pytest.raises(ContractError, match=code):
            await engine.business_command(scope, 'update_preparation_products', args, 'runtime-deny-' + str(index))
        with pytest.raises(BusinessError) as error:
            await service.execute_command('update_preparation_products', args, MutationContext(operation_id='direct-deny-' + str(index), grant_id=run['grant_id'], expected_write_generation=run['write_generation']), principal, scope=run['scope'])
        assert error.value.code == code
        assert (await service.query('read_preparation', {'preparation_id': prep['id']}))['body']['selected_product_refs'] == [pinned]
    # The trusted manual path keeps its independent selection authority.
    await human_command(service, 'update_preparation_products', {'preparation_id': prep['id'], 'expected_revision': prep['revision'], 'selected_product_refs': [refreshed, unrelated]})


@pytest.mark.asyncio
async def test_audio_answer_requires_current_exact_human_operation_not_forged_status(env, monkeypatch):
    engine, service = env
    room, prep, script = await prepared_text(service)
    run = await new_run(engine, '准备语音', room_id=room['id'], preparation_id=prep['id'])
    engine.store.update('run', run['id'], phase='execute', status='RUNNING')
    scope = RunScope(run['id'], 'test-invocation', 'execute')
    monkeypatch.setattr('shengbo_prelive.runtime.interrupt', stop_at_interrupt)
    with pytest.raises(GraphInterrupt):
        await engine.request_decision(scope, {'kind': 'text_audio_approval', 'prompt': '批准本场文案？'}, 'ask-audio')
    question = engine.get_run(run['id'])['questions'][0]
    request = AnswerRequest(question_revision=question['revision'], answer='approve', client_request_id='answer-audio', human_receipt={'operation_id': question['approval_binding']['operation_id'], 'status': 'approved'})
    with pytest.raises(ContractError, match='TEXT_APPROVAL_MISSING'):
        await engine.answer(question['id'], request)
    body = question['human_action']['command']
    receipt = await service.execute_command(body['command'], body['args'], MutationContext.model_validate(body['mutation']), Principal(kind='human'))
    request.human_receipt = receipt
    result = await engine.answer(question['id'], request)
    assert result['run']['status'] == 'RUNNING'
    saved = engine.store.get('question', question['id'])
    assert saved['verified_human_receipt']['status'] == 'current'
    assert saved['verified_human_receipt']['approval']['operation_id'] == body['mutation']['operation_id']
    assert (await engine.answer(question['id'], request))['answer_event_ref'] == result['answer_event_ref']


@pytest.mark.asyncio
async def test_identity_proof_is_created_by_human_and_read_back_before_answer(env, monkeypatch):
    engine, service = env
    existing = await product(service)
    run = await new_run(engine, '整理上传的商品资料')
    engine.store.update('run', run['id'], status='ASSESSING_INTENT')
    scope = RunScope(run['id'], 'identity-invoke', 'intent')
    monkeypatch.setattr('shengbo_prelive.runtime.interrupt', stop_at_interrupt)
    with pytest.raises(GraphInterrupt):
        await engine.request_decision(scope, {'kind': 'product_identity', 'prompt': '同名是否新建？', 'context': {'target_name': '测试拖鞋'}}, 'identity-call')
    question = engine.get_run(run['id'])['questions'][0]
    request = AnswerRequest(question_revision=question['revision'], answer='create_distinct', client_request_id='identity-answer', human_receipt={'identity_decision_ref': {'kind': 'human_decision', 'id': 'invented', 'revision': 1, 'content_hash': 'sha256:invented'}})
    with pytest.raises(ContractError):
        await engine.answer(question['id'], request)
    request.human_receipt = service.record_identity_decision('测试拖鞋', [existing], principal=Principal(kind='human'))
    await engine.answer(question['id'], request)
    assert engine.store.get('question', question['id'])['verified_human_receipt']['identity_decision_ref'] == request.human_receipt['identity_decision_ref']


@pytest.mark.asyncio
async def test_execute_identity_selection_cannot_expand_frozen_targets(env, monkeypatch):
    engine, service = env
    existing = await product(service)
    run = await new_run(engine, '整理这份商品资料')
    engine.store.update('run', run['id'], phase='execute', status='RUNNING', target_product_ids=[], goal={'objective': 'catalog_update'})
    monkeypatch.setattr('shengbo_prelive.runtime.interrupt', stop_at_interrupt)
    with pytest.raises(GraphInterrupt):
        await engine.request_decision(RunScope(run['id'], 'identity-execute', 'execute'), {'kind': 'product_identity', 'prompt': '确认已有身份', 'context': {'target_name': '测试拖鞋'}}, 'identity-exec-call')
    question = engine.get_run(run['id'])['questions'][0]
    with pytest.raises(ContractError, match='SCOPE_CHANGE_REQUIRED'):
        await engine.answer(question['id'], AnswerRequest(question_revision=question['revision'], answer='update:' + existing['id'], client_request_id='do-not-expand'))
    assert engine.store.get('question', question['id'])['status'] == 'OPEN'
    assert engine.store.get('run', run['id'])['target_product_ids'] == []


@pytest.mark.asyncio
async def test_answer_type_guard_and_cancellation_during_review_do_not_restore_run(env):
    engine, service = env
    run = await new_run(engine, '准备语音')
    engine.store.update('run', run['id'], phase='execute', status='WAITING_USER')
    question = engine.store.put('question', {'id': 'q-cancel', 'decision_id': 'q-cancel', 'run_id': run['id'], 'kind': 'text_audio_approval', 'status': 'OPEN', 'preview': {'review_hash': 'old'}, 'approval_binding': {'preparation_id': 'test-prep', 'review_hash': 'old', 'operation_id': 'approve-op'}})
    engine.store.update('run', run['id'], scope={'kind': 'preparation', 'account_id': 'local', 'preparation_id': 'test-prep'})
    with pytest.raises(ContractError, match='ANSWER_INVALID'):
        await engine.answer(question['id'], AnswerRequest(question_revision=question['revision'], answer=True, client_request_id='bool-rejected'))
    async def cancelled_during_read(key, *args, **kwargs):
        engine.store.update('run', run['id'], status='CANCELLED', dispatch_frozen=True)
        return {'review_hash': 'new'}
    engine.client.query = cancelled_during_read
    with pytest.raises(ContractError, match='TEXT_REVIEW_STALE'):
        await engine.answer(question['id'], AnswerRequest(question_revision=question['revision'], answer='approve', client_request_id='cancel-answer', human_receipt={'operation_id': 'approve-op'}))
    assert engine.store.get('run', run['id'])['status'] == 'CANCELLED'


@pytest.mark.asyncio
async def test_script_acceptance_reads_module_text_and_requires_current_task_change(env):
    engine, service = env
    room, prep, script = await prepared_text(service)
    run = await new_run(engine, '修改本场话术', room_id=room['id'], preparation_id=prep['id'])
    proposal = IntentAssessment(objective='script_change', script_mode='edit', summary='修改本场话术', authorization_quote=run['input']['text'])
    run = engine.store.update('run', run['id'], intent_assessment=proposal.model_dump())
    await engine._freeze_goal(run)
    engine.store.update('run', run['id'], finish={'outcome': 'COMPLETED', 'summary': 'test'})
    scope = RunScope(run['id'], 'verify-invoke', 'execute')
    check = await engine.verify_finish(scope)
    assert not check['ok'] and '本次话术真实修改或生成回执' in check['missing']
    current = await service.query('read_preparation', {'preparation_id': prep['id']})
    text = await service.query('read_script', {'ref': script})
    await engine.business_command(scope, 'patch_script', {'preparation_id': prep['id'], 'expected_revision': current['ref']['revision'],
        'script_ref': script, 'module_key': 'selling_points', 'selected_text_hash': canonical_hash(text['modules']['selling_points']['sentences']),
        'sentences': ['本次真实修改并保存的正文。']}, 'test-script-op')
    engine.store.update('run', run['id'], finish={'outcome': 'COMPLETED', 'summary': 'test'})
    assert (await engine.verify_finish(scope))['ok']
    original_query = engine.client.query
    async def empty_modules(key, *args, **kwargs):
        data = await original_query(key, *args, **kwargs)
        if key == 'read_script':
            data = deepcopy(data)
            for module in data['modules'].values():
                if module:
                    module['sentences'] = []
        return data
    engine.client.query = empty_modules
    assert not (await engine.verify_finish(scope))['ok']


@pytest.mark.asyncio
async def test_actual_engine_native_sdk_intent_execute_and_closure(env):
    engine, service = env
    text = '查看当前声播状态'
    engine._provided_model = BindableFakeModel(responses=[
        AIMessage(content='', tool_calls=[{'id': 'intent-assess', 'name': 'save_task_assessment', 'args': {'assessment': {'objective': 'read_explain', 'summary': '读取状态', 'authorization_quote': text}}}]),
        AIMessage(content='任务识别完成'),
        AIMessage(content='', tool_calls=[{'id': 'execute-assess', 'name': 'save_task_assessment', 'args': {'assessment': {'summary': '按真实状态回答', 'capabilities': [], 'steps': [], 'decision_basis': []}}}]),
        AIMessage(content='', tool_calls=[{'id': 'finish-read', 'name': 'finish_turn', 'args': {'outcome': 'COMPLETED', 'summary': '当前无待处理本场任务'}}]),
        AIMessage(content='当前无待处理本场任务')])
    await engine.start()
    run = await new_run(engine, text)
    for _ in range(150):
        current = engine.get_run(run['id'])
        if current['status'] in {'COMPLETED', 'BLOCKED', 'FAILED'}:
            break
        await asyncio.sleep(.05)
    assert current['status'] == 'COMPLETED', current
    assert current.get('closure_ref')
    assert not (await service.query('read_completion_observation', {}))['pending_job_ids']
    assert any(event['type'] == 'model.completed' for event in engine.events(run['id'])['events'])


@pytest.mark.asyncio
async def test_actual_engine_corrects_bad_quote_via_error_tool_message(env):
    engine, service = env
    text = '查看当前声播状态'
    engine._provided_model = BindableFakeModel(responses=[
        AIMessage(content='', tool_calls=[{'id': 'bad-intent', 'name': 'save_task_assessment', 'args': {'assessment': {'objective': 'read_explain', 'summary': '读取状态', 'authorization_quote': '这是模型改写的授权'}}}]),
        AIMessage(content='', tool_calls=[{'id': 'fixed-intent', 'name': 'save_task_assessment', 'args': {'assessment': {'objective': 'read_explain', 'summary': '读取状态', 'authorization_quote': text}}}]),
        AIMessage(content='已修正为真实原文'),
        AIMessage(content='', tool_calls=[{'id': 'finish-corrected', 'name': 'finish_turn', 'args': {'outcome': 'COMPLETED', 'summary': '已读取当前状态'}}]),
        AIMessage(content='已读取当前状态')])
    await engine.start()
    run = await new_run(engine, text)
    for _ in range(180):
        current = engine.get_run(run['id'])
        if current['status'] in {'COMPLETED', 'BLOCKED', 'FAILED'}:
            break
        await asyncio.sleep(.05)
    assert current['status'] == 'COMPLETED', current
    assert current['intent_assessment']['authorization_quote'] == text
    assert current['correctable_tool_errors']['INTENT_EVIDENCE_INVALID'] == 1
    assert any(event['type'] == 'middleware.check' and event['data'].get('reason_code') == 'INTENT_EVIDENCE_INVALID' for event in engine.events(run['id'])['events'])


def test_assessment_effect_schema_matches_policy_and_rejects_natural_language():
    from shengbo_prelive.models import TaskAssessment
    from shengbo_prelive.policy import EFFECTS
    from shengbo_prelive.tools import build_tools
    schema = IntentAssessment.model_json_schema()['properties']['proposed_effects']
    assert set(schema['items']['enum']) == set().union(*EFFECTS.values())
    assert '命令ID' in schema['description']
    assert '自然语言' in TaskAssessment.model_json_schema()['properties']['capabilities']['description']
    tool = next(item for item in build_tools(object()) if item.name == 'save_task_assessment')
    public = tool.tool_call_schema
    public_effects = public.model_json_schema()['$defs']['IntentAssessment']['properties']['proposed_effects']
    assert public_effects['items']['enum'] == schema['items']['enum']
    bad = {'objective': 'read_explain', 'summary': '查看状态', 'authorization_quote': '查看状态', 'proposed_effects': ['整理商品并生成话术']}
    with pytest.raises(ValidationError):
        IntentAssessment.model_validate(bad)
    with pytest.raises(ValidationError):
        public.model_validate({'assessment': bad})


@pytest.mark.asyncio
async def test_actual_engine_corrects_unknown_effect_enum_without_grant_expansion(env):
    engine, service = env
    text = '查看当前声播状态'
    def assess(identity, effects):
        return AIMessage(content='', tool_calls=[{'id': identity, 'name': 'save_task_assessment', 'args': {'assessment': {'objective': 'read_explain', 'summary': '读取状态', 'authorization_quote': text, 'proposed_effects': effects}}}])
    engine._provided_model = BindableFakeModel(responses=[
        assess('bad-effect', ['整理商品并生成话术']), assess('fixed-effect', []),
        AIMessage(content='参数已修正'),
        AIMessage(content='', tool_calls=[{'id': 'finish-enum', 'name': 'finish_turn', 'args': {'outcome': 'COMPLETED', 'summary': '已读取当前状态'}}]),
        AIMessage(content='已读取当前状态')])
    await engine.start()
    run = await new_run(engine, text)
    for _ in range(180):
        current = engine.get_run(run['id'])
        if current['status'] in {'COMPLETED', 'BLOCKED', 'FAILED'}:
            break
        await asyncio.sleep(.05)
    assert current['status'] == 'COMPLETED', current
    assert current['correctable_tool_errors']['INVALID_EFFECT_ENUM'] == 1
    assert current['intent_assessment']['proposed_effects'] == []
    assert current['allowed_effects'] == []
    assert not current.get('grant_id')
    assert any(event['type'] == 'middleware.check' and event['data'].get('reason_code') == 'INVALID_EFFECT_ENUM' for event in engine.events(run['id'])['events'])


@pytest.mark.asyncio
@pytest.mark.parametrize('effects,expected_code,corrections', [
    (['整理商品并生成话术'], 'INVALID_EFFECT_ENUM', 3),
    (['save_product'], 'GOAL_SCOPE_CONFLICT', 0),
])
async def test_actual_engine_bounds_unknown_enum_but_keeps_true_scope_conflict_hard(env, effects, expected_code, corrections):
    engine, service = env
    text = '查看当前声播状态'
    engine._provided_model = BindableFakeModel(responses=[AIMessage(content='', tool_calls=[{
        'id': 'bad-effects-' + str(index), 'name': 'save_task_assessment', 'args': {'assessment': {
            'objective': 'read_explain', 'summary': '读取状态', 'authorization_quote': text, 'proposed_effects': effects}}}]) for index in range(4)])
    await engine.start()
    run = await new_run(engine, text)
    for _ in range(180):
        current = engine.get_run(run['id'])
        if current['status'] in {'COMPLETED', 'BLOCKED', 'FAILED'}:
            break
        await asyncio.sleep(.05)
    assert current['status'] == 'BLOCKED', (current.get('error'), current.get('correctable_tool_errors'))
    assert current['error']['code'] == expected_code
    assert current.get('correctable_tool_errors', {}).get('INVALID_EFFECT_ENUM', 0) == corrections
    assert not current.get('intent_assessment') and not current.get('goal') and not current.get('grant_id')


@pytest.mark.asyncio
async def test_full_prelive_parallel_human_approval_audio_candidate_closed_loop(tmp_path):
    """Exercise the entire real harness/services with silent local test audio."""
    import hashlib
    import json
    import wave
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import PrivateAttr
    from shengbo_prelive.runtime import CURRENT_SCOPE

    class SilentAdapter(FakeAdapter):
        rendered = 0
        async def generate_module(self, context, module_key, product, previous):
            await asyncio.sleep(.03)
            return await super().generate_module(context, module_key, product, previous)

        async def render_audio(self, text, room_id, voice):
            self.rendered += 1
            await asyncio.sleep(.01)
            identity = hashlib.sha256((text + str(voice)).encode()).hexdigest()
            path = tmp_path / 'voice' / 'room_audio' / room_id / (identity + '.wav')
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), 'wb') as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b'\x00\x00' * 1600)
            return {'path': str(path), 'audio_id': identity, 'duration': .1, 'url': '/test-audio/' + identity + '.wav'}

    class PlannerModel(BaseChatModel):
        _logic = PrivateAttr()
        def __init__(self, logic):
            super().__init__()
            self._logic = logic
        @property
        def _llm_type(self):
            return 'isolated-scripted-harness-test'
        def bind_tools(self, tools, **kwargs):
            return self
        def _generate(self, *args, **kwargs):
            raise AssertionError('Only async native Agent execution is expected')
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=await self._logic(messages))])

    adapter = SilentAdapter()
    service = PreliveService(tmp_path / 'voice', adapter)
    client = DirectBusinessClient(service)
    engine = RuntimeEngine(tmp_path / 'agent', client, model=BindableFakeModel(responses=[AIMessage(content='unused')]))
    p = await product(service)
    room = (await human_command(service, 'create_room', {'name': '全流程隔离直播间'}))['data']['room_ref']
    text = '为' + room['id'] + '准备整场直播，使用' + p['id'] + '，生成话术并准备语音'
    counter = 0

    def call(name, args):
        nonlocal counter
        counter += 1
        return AIMessage(content='', tool_calls=[{'id': 'full-test-' + str(counter), 'name': name, 'args': args}])

    async def logic(messages):
        scope = CURRENT_SCOPE.get()
        run = engine.store.get('run', scope.run_id)
        if scope.role != 'main':
            branch = engine.store.get('branch', scope.branch_task_id)
            if not branch.get('report'):
                await asyncio.sleep(.08)  # Three actual branch lifetimes overlap.
                return call('submit_' + scope.role + '_report', {'report': {'summary': scope.role + '已完成完整结构化候选', 'product_ids': [p['id']], 'fields': {'context': {'market': '中国', 'language': '普通话'}}, 'insights': [{'use_scene': '居家日常使用'}], 'evidence': [], 'hypotheses': ['目标顾客仍需后续真实验证'], 'conflicts': [], 'missing_fields': [], 'asset_suggestions': [], 'source_refs': [], 'dependencies': [p]}})
            return AIMessage(content='本角色报告已暂存，等待主 Agent 验收')
        if scope.phase == 'intent':
            if not run.get('intent_assessment'):
                return call('save_task_assessment', {'assessment': {'objective': 'prepare_session', 'script_delivery_requirement': 'new_generation', 'summary': '完成用户明确指定商品的整场播前准备', 'authorization_quote': text, 'room_id': room['id'], 'target_product_ids': [p['id']], 'required_analyses': ['product_facts', 'session_config', 'audience_scenes']}})
            return AIMessage(content='已按真实用户原文识别任务')
        last = messages[-1] if messages else None
        if getattr(last, 'type', None) == 'tool' and getattr(last, 'name', None) == 'finish_turn':
            try:
                if json.loads(last.content).get('accepted'):
                    return AIMessage(content='本轮结果已通过可信检查')
            except (ValueError, TypeError):
                pass
        if not run.get('task_assessment'):
            return call('save_task_assessment', {'assessment': {'summary': '并行分析后汇合完整资料，生成并确认本场播出候选', 'capabilities': ['product_facts', 'session_config', 'audience_scenes'], 'steps': ['并行分析', '接受报告', '生成话术', '人工顺序与整包批准', '语音与候选'], 'decision_basis': ['只使用用户指定商品', '语音必须人工批准']}})
        branches = [branch for branch in engine.store.list('branch') if branch['run_id'] == run['id']]
        if not branches:
            return call('dispatch_analysis_batch', {'reason': '三类研究使用共同稳定商品事实，允许独立并行', 'tasks': [{'role': role, 'instruction': '完成本角色分析，保留完整结构、事实与假设边界', 'product_ids': [p['id']], 'source_refs': [], 'input_refs': [p]} for role in ('product_facts', 'session_config', 'audience_scenes')]})
        if any(branch['status'] in {'QUEUED', 'RUNNING', 'RECOVERING'} for branch in branches):
            return call('finish_turn', {'outcome': 'WAITING_JOBS', 'summary': '等待已持久派发的并行分析'})
        if any(branch['status'] != 'ACCEPTED' for branch in branches):
            return call('accept_analysis_reports', {'branch_task_ids': [branch['id'] for branch in branches]})
        prep_id = run['scope']['preparation_id']
        prep = await service.query('read_preparation', {'preparation_id': prep_id})
        body = prep['body']
        base = {'preparation_id': prep_id, 'expected_revision': prep['ref']['revision']}
        if not body['draft_ref']:
            return call('save_session_draft', {**base, 'draft': {'theme': '居家好物介绍', 'commerce': {}}})
        if not body['asset_checklist_ref']:
            return call('save_asset_checklist', {**base, 'checklist': {'items': [{'item_id': 'product-overview', 'product_ref': p, 'category': 'product_overview', 'purpose': '说明产品外观', 'recommended_content': '上传商家已有产品照片', 'status': 'missing'}], 'notes': ['可选展示材料缺失不阻塞声音测试']}})
        if not body['bundle_ref']:
            return call('build_context_bundle', {**base, 'product_refs': body['selected_product_refs'], 'analysis_refs': body['analysis_refs'], 'draft_ref': body['draft_ref'], 'recap_refs': [], 'source_refs': [], 'prior_script_refs': [], 'user_decisions': []})
        if not body['plan_ref']:
            return call('save_script_change_plan', {**base, 'bundle_ref': body['bundle_ref'], 'items': [{'product_ref': p, 'variant_id': 'full-harness-variant', 'modules': [{'key': key, 'action': 'disable' if key in {'campaign', 'transition'} else 'generate'} for key in MODULE_KEYS]}]})
        obs = await service.query('read_completion_observation', {'preparation_id': prep_id})
        if obs['pending_job_ids']:
            return call('finish_turn', {'outcome': 'WAITING_JOBS', 'summary': '等待已经提交的业务生成任务'})
        if not body['script_refs']:
            return call('execute_script_plan', {**base, 'plan_ref': body['plan_ref'], 'context_bundle_ref': body['bundle_ref'], 'expected_generation_epoch': body['generation_epoch']})
        questions = [q for q in engine.store.list('question') if q['run_id'] == run['id']]
        if not body['playlist_ref']:
            if not any(q['kind'] == 'playlist_order' and q['status'] == 'ANSWERED' for q in questions):
                return call('request_user_decision', {'kind': 'playlist_order', 'prompt': '本场仅此一商品版本，确认按该顺序播放？', 'context': {'playlist': {
                    'preparation_id': prep_id, 'items': [{'occurrence_id': 'full-o1', 'product_id': p['id'], 'script_revision_ref': body['script_refs'][0]}], 'loop_mode': 'infinite'}}})
            return call('save_playlist', {**base, 'items': [{'occurrence_id': 'full-o1', 'product_id': p['id'], 'script_revision_ref': body['script_refs'][0]}], 'loop_mode': 'once'})
        if not body['compiled_plan_ref']:
            return call('compile_playback_plan', {**base, 'playlist_ref': body['playlist_ref']})
        if not body['approval_ref']:
            return call('request_user_decision', {'kind': 'text_audio_approval', 'prompt': '请检查完整文案、顺序与声音，再批准生成语音'})
        if not body['audio_manifest_ref']:
            review = await service.query('create_text_review', {'preparation_id': prep_id})
            return call('prepare_required_audio', {**base, 'approval_ref': body['approval_ref'], 'review_hash': review['review_hash'], 'expected_generation_epoch': body['generation_epoch']})
        if not body['candidate_publication_ref']:
            return call('publish_preparation', {**base, 'approval_ref': body['approval_ref']})
        return call('finish_turn', {'outcome': 'READY_FOR_REVIEW', 'summary': '并行分析、完整上下文、实际话术、用户编排和批准、语音与未采用候选均已就绪'})

    engine._provided_model = PlannerModel(logic)
    try:
        await engine.start()
        run = await new_run(engine, text, room_id=room['id'])
        human_answers = []
        for _ in range(900):
            current = engine.get_run(run['id'])
            for question in current['questions']:
                if question['kind'] == 'playlist_order':
                    await engine.answer(question['id'], AnswerRequest(question_revision=question['revision'], answer='confirmed', client_request_id='full-playlist-answer'))
                    human_answers.append('playlist')
                elif question['kind'] == 'text_audio_approval':
                    command = question['human_action']['command']
                    receipt = await service.execute_command(command['command'], command['args'], MutationContext.model_validate(command['mutation']), Principal(kind='human'))
                    await engine.answer(question['id'], AnswerRequest(question_revision=question['revision'], answer='approve', client_request_id='full-audio-answer', human_receipt=receipt))
                    human_answers.append('audio')
            if current['status'] in {'READY_FOR_REVIEW', 'COMPLETED', 'BLOCKED', 'FAILED'}:
                break
            await asyncio.sleep(.04)
        assert current['status'] == 'READY_FOR_REVIEW', current
        assert current['closure_ref']
        assert human_answers == ['playlist', 'audio']
        assert {branch['role'] for branch in current['branches'] if branch['status'] == 'ACCEPTED'} == {'product_facts', 'session_config', 'audience_scenes'}
        prep = await service.query('read_preparation', {'preparation_id': current['scope']['preparation_id']})
        assert len(prep['body']['analysis_refs']) == 3
        bundle = await service.query('read_context', {'ref': prep['body']['bundle_ref']})
        assert len(bundle['body']['analysis_refs']) == 3
        assert len(bundle['body']['records']) >= 5
        assert prep['body']['candidate_publication_ref']
        assert adapter.rendered > 0
        assert (await service.query('read_text_approval', {'preparation_id': prep['ref']['id']}))['status'] == 'current'
        events = engine.events(run['id'], limit=1000)['events']
        started = [event for event in events if event['type'] == 'branch.started']
        ended = [event for event in events if event['type'] == 'branch.prepared']
        assert len(started) == len(ended) == 3
        assert max(event['timestamp'] for event in started) < min(event['timestamp'] for event in ended), 'three branch lifetimes actually overlap'
        assert any(event['type'] == 'tool.completed' and event['data'].get('tool_name') == 'prepare_required_audio' for event in events)
        assert all(not str(path).endswith('.env') for path in tmp_path.rglob('*'))
    finally:
        await engine.stop()
