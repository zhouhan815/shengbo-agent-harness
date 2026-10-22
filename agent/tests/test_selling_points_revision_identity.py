"""One native-graph regression for revise/resume/new identical submission."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from shengbo_prelive.runtime import CURRENT_SCOPE
from test_runtime_business import env, new_run
from test_selling_points_workflow import (
    NativeSellingPointsModel, answer, candidates, catalog, create_tennis,
    proposal_id, wait_question,
)


@pytest.mark.asyncio
async def test_native_revise_replay_returns_answer_then_new_identical_call_opens_new_card(env):
    engine, service = env
    ref = await create_tennis(service)
    text = '整理' + ref['id'] + '的商品资料，缺少核心卖点时先问我，确认后保存；不要生成话术或语音'
    count = 0
    saw_revise_tool_result = False

    def call(name, arguments):
        nonlocal count
        count += 1
        assert count < 20, 'revise caused a model/tool loop'
        return AIMessage(content='', tool_calls=[{
            'id': 'revision-model-call-' + str(count), 'name': name, 'args': arguments,
        }])

    async def logic(messages):
        nonlocal saw_revise_tool_result
        scope = CURRENT_SCOPE.get()
        run = engine.store.get('run', scope.run_id)
        if scope.phase == 'intent':
            if not run.get('intent_assessment'):
                return call('save_task_assessment', {'assessment': {
                    'objective': 'catalog_update', 'summary': '整理明确商品并按需补写核心卖点',
                    'target_product_ids': [ref['id']], 'authorization_quote': text,
                }})
            return AIMessage(content='已明确仅整理商品资料与卖点。')
        if not run.get('task_assessment'):
            return call('save_task_assessment', {'assessment': {
                'summary': '先生成选择，再按用户确认保存精确候选',
                'capabilities': ['wf_selling_points'], 'steps': ['选择', '候选', '确认', '保存'],
            }})
        questions = [q for q in engine.store.list('question') if q['run_id'] == run['id']]
        if not any(q['kind'] == 'selling_points_generation' and q['status'] == 'ANSWERED' for q in questions):
            return call('wf_selling_points', {'action': 'offer', 'parameters': {'product_ref': ref}})
        confirmations = [q for q in questions if q['kind'] == 'selling_points_confirmation']
        if not confirmations:
            return call('wf_selling_points', {'action': 'propose', 'parameters': {
                'product_ref': ref, 'items': candidates(),
            }})
        latest = confirmations[-1]
        choice = latest.get('answer')
        choice = choice.get('choice') if isinstance(choice, dict) else choice
        if choice == 'revise':
            # Resume must finish the interrupted old call and deliver its
            # answer before the model can choose a new identical submission.
            saw_revise_tool_result = any(isinstance(m, ToolMessage) and 'revise' in str(m.content)
                                         for m in messages)
            assert saw_revise_tool_result
            assert len(confirmations) == 1
            return call('wf_selling_points', {'action': 'propose', 'parameters': {
                'product_ref': ref, 'items': candidates(),
            }})
        if not (await service.query('read_product', {'product_id': ref['id']}))['body']['core_selling_points']:
            return call('wf_selling_points', {'action': 'save', 'parameters': {'proposal_id': proposal_id(latest)}})
        return call('request_user_decision', {'kind': 'fact_check', 'prompt': '隔离测试已完成，停在此处。'})

    engine._provided_model = NativeSellingPointsModel(logic)
    await engine.start()
    run = await new_run(engine, text)
    case = SimpleNamespace(engine=engine, service=service, ref=ref, run=run)
    generation = await wait_question(engine, run['id'], 'selling_points_generation')
    await answer(case, generation, {'choice': 'generate', 'text': '先按基础资料整理，不夸大性能'})
    first = await wait_question(engine, run['id'], 'selling_points_confirmation')
    await answer(case, first, {'choice': 'revise', 'text': '重新提交给我核对'})
    second = await wait_question(engine, run['id'], 'selling_points_confirmation')
    assert saw_revise_tool_result
    assert second['id'] != first['id']
    assert proposal_id(second) != proposal_id(first)
    assert second['selling_points_binding'] == first['selling_points_binding']
    assert (await catalog(case))['body']['core_selling_points'] == []
    await answer(case, second, 'approve')
    await wait_question(engine, run['id'], 'fact_check')
    assert (await catalog(case))['body']['core_selling_points'] == [row['selling_point'] for row in candidates()]
    confirmations = [q for q in engine.store.list('question')
                     if q['run_id'] == run['id'] and q['kind'] == 'selling_points_confirmation']
    assert len(confirmations) == 2, 'GraphInterrupt replay must not create extra confirmation cards'
    assert len([p for p in engine.store.list('selling_point_proposal') if p['run_id'] == run['id']]) == 2
    assert len([o for o in engine.store.list('operation')
                if o['run_id'] == run['id'] and o['key'] == 'save_product']) == 1

