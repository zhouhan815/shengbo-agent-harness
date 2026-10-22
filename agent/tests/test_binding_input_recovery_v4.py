"""Real SDK main/workflow/M5 binding recovery, with isolated business state."""
from copy import deepcopy
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field
import pytest

from shengbo_contracts.common import ContractError, canonical_hash
from shengbo_prelive.runtime import CURRENT_SCOPE
from test_binding_annotation_review_rc5 import case
from test_runtime_business import BindableFakeModel, env


def bind_call(branch_id, parameters, call_id='bind-without-review'):
    return {'id': call_id, 'name': 'wf_analysis_coordination',
        'args': {'action': 'bind', 'branch_task_ids': [branch_id], 'parameters': parameters}}


class ReviewBindingModel(BindableFakeModel):
    branch_id: str
    parameters: dict
    classification: str = 'nonblocking_boundary_note'
    seen: list = Field(default_factory=list)
    submitted_reviews: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(deepcopy(messages))
        if len(self.seen) == 1:
            message = AIMessage(content='', tool_calls=[bind_call(self.branch_id, self.parameters)])
        elif len(self.seen) == 2:
            feedback = next(item for item in reversed(messages) if isinstance(item, ToolMessage))
            assert feedback.status == 'error'
            error = json.loads(feedback.content)['error']
            assert error['code'] == 'BINDING_REVIEW_REQUIRED'
            details = error['details']
            assert details['commit_state'] == 'not_committed'
            assert details['eligible_branch_task_ids'] == [self.branch_id]
            assert details['automatic_fact_changes'] is False
            review_inputs = details['review_inputs']
            assert len(review_inputs) == 1 and review_inputs[0]['issues']
            review = review_inputs[0]
            dispositions = [{'issue_index': issue['issue_index'], 'issue_hash': issue['issue_hash'],
                'classification': self.classification,
                'rationale': '明确核对原资料与已保存商品事实；本测试选择的注释仅说明范围或授权边界。',
                'basis_refs': deepcopy(review['allowed_basis_refs'])} for issue in review['issues']]
            selected = {'branch_task_id': review['branch_task_id'], 'report_hash': review['report_hash'],
                'issue_dispositions': dispositions}
            self.submitted_reviews.append(deepcopy(selected))
            corrected = {**self.parameters, 'conflict_reviews': [selected]}
            message = AIMessage(content='', tool_calls=[bind_call(self.branch_id, corrected, 'bind-explicit-review')])
        elif len(self.seen) == 3:
            feedback = next(item for item in reversed(messages) if isinstance(item, ToolMessage))
            value = json.loads(feedback.content)
            if self.classification == 'unresolved':
                assert feedback.status == 'error' and value['error']['code'] == 'BINDING_REQUIRES_REPLAN'
                assert value['error']['details']['commit_state'] == 'not_committed'
            else:
                assert feedback.status == 'success' and value['status'] == 'COMPLETED'
                assert value['result']['bound_branch_ids'] and value['result']['accepted_refs']
            # This test proves binding only, and does not claim the whole
            # preparation (scripts/order/audio) has completed.
            message = AIMessage(content='', tool_calls=[{'id': 'stop-after-binding-check', 'name': 'finish_turn',
                'args': {'outcome': 'BLOCKED', 'summary': '本轮仅核验报告关联恢复；其余本场工作尚未完成，保留正式成果。'}}])
        else:
            raise AssertionError('The verified stop must not cause a fourth model call')
        return ChatResult(generations=[ChatGeneration(message=message)])


class OneCallBindingModel(BindableFakeModel):
    branch_id: str
    parameters: dict
    call_count: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.call_count += 1
        assert self.call_count == 1, 'Integrity/authority/unknown errors must not enter a correction loop'
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='',
            tool_calls=[bind_call(self.branch_id, self.parameters)]))])


class UnknownBindingModel(BindableFakeModel):
    branch_id: str
    parameters: dict
    call_count: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            call = bind_call(self.branch_id, self.parameters)
        else:
            assert self.call_count == 2
            feedback = next(item for item in reversed(messages) if isinstance(item, ToolMessage))
            value = json.loads(feedback.content)
            assert value['status'] == 'OUTCOME_UNKNOWN'
            assert value['blockers'][0]['code'] == 'BINDING_REVIEW_REQUIRED'
            assert 'correction_count' not in value and 'corrective_hint' not in value
            call = {'id': 'wait-for-reconciliation', 'name': 'finish_turn', 'args': {
                'outcome': 'WAITING_JOBS', 'summary': '原操作结果未知，保留成果等待对账，不继续关联或盲目重发。'}}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='', tool_calls=[call]))])


async def invoke_main(engine, scope, model, thread_id):
    engine.model, engine.saver = model, InMemorySaver()
    graph = engine._build_graph('main')
    token = CURRENT_SCOPE.set(scope)
    try:
        return await graph.ainvoke({'messages': [HumanMessage('核查来源注释后，将研究关联本次新商品；保持原事实。')],
            'files': engine._initial_files('main')},
            {'configurable': {'thread_id': thread_id}, 'recursion_limit': 40}, context=scope)
    finally:
        CURRENT_SCOPE.reset(token)


@pytest.mark.asyncio
async def test_real_main_workflow_missing_review_gets_m5_guidance_and_explicit_review_binds_accepted(env):
    engine, service = env
    run, scope, sources, product_ref, original, parameters = await case(engine, service,
        fields={'segments': [{'need': '顾客场景假设，不作为商品性能事实'}]})
    original_fields, original_conflicts = deepcopy(original['report']['fields']), deepcopy(original['report']['conflicts'])
    missing = {key: value for key, value in parameters.items() if key != 'conflict_reviews'}
    model = ReviewBindingModel(responses=[], branch_id=original['id'], parameters=missing)
    result = await invoke_main(engine, scope, model, 'binding-input-recovery-v4')
    feedback = next(item for item in result['messages'] if isinstance(item, ToolMessage)
        and item.tool_call_id == 'bind-without-review')
    value = json.loads(feedback.content)
    assert value['error']['details']['review_inputs'][0]['report_hash'] == original['report_hash']
    assert value['correction_count'] == 1 and value['correction_limit'] == 3
    assert all('issue' in item and item['issue_hash'] == canonical_hash(item['issue'])
        for item in value['error']['details']['review_inputs'][0]['issues'])
    success = next(item for item in result['messages'] if isinstance(item, ToolMessage)
        and item.tool_call_id == 'bind-explicit-review')
    bound_result = json.loads(success.content)['result']
    bound = engine.store.get('branch', bound_result['bound_branch_ids'][0])
    assert bound['status'] == 'ACCEPTED' and bound['accepted_ref'] in bound_result['accepted_refs']
    assert bound['report']['fields'] == original_fields and bound['report']['conflicts'] == original_conflicts
    assert bound['binding_basis']['original_report'] == original['report']
    assert bound['binding_basis']['conflict_review']['issue_dispositions'][0]['classification'] == 'nonblocking_boundary_note'
    formal = (await service.query('read_analysis_report', {'ref': bound['accepted_ref']}))['body']['report']
    assert formal['fields'] == original_fields and formal['conflicts'] == original_conflicts
    review = formal['evidence'][-1]
    assert review['kind'] == 'main_annotation_review' and review['semantic_decision_by'] == 'main_agent'
    assert review['fact_changes'] is False
    assert engine.store.get('branch', original['id']) == original
    assert len(model.seen) == 3 and len(model.submitted_reviews) == 1
    assert engine.store.get('run', run['id'])['correctable_tool_errors']['BINDING_REVIEW_REQUIRED'] == 1


@pytest.mark.asyncio
async def test_unresolved_review_is_model_readable_replan_and_never_automatically_accepted(env):
    engine, service = env
    run, scope, _, _, original, parameters = await case(engine, service)
    operations_before = engine.store.list('operation')
    missing = {key: value for key, value in parameters.items() if key != 'conflict_reviews'}
    model = ReviewBindingModel(responses=[], branch_id=original['id'], parameters=missing, classification='unresolved')
    result = await invoke_main(engine, scope, model, 'binding-unresolved-replan-v4')
    feedback = next(item for item in result['messages'] if isinstance(item, ToolMessage)
        and item.tool_call_id == 'bind-explicit-review')
    details = json.loads(feedback.content)['error']['details']
    assert details['commit_state'] == 'not_committed' and details['review_inputs']
    assert not [branch for branch in engine.store.list('branch') if branch.get('binding_basis')]
    assert engine.store.get('branch', original['id']) == original
    assert engine.store.list('operation') == operations_before
    assert len(model.seen) == 3
    counters = engine.store.get('run', run['id'])['correctable_tool_errors']
    assert counters['BINDING_REVIEW_REQUIRED'] == counters['BINDING_REQUIRES_REPLAN'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['report_body', 'report_hash', 'foreign_branch', 'foreign_product', 'receipt'])
async def test_binding_sdk_keeps_integrity_foreign_and_unknown_failures_hard(env, mutation):
    engine, service = env
    run, scope, _, _, original, parameters = await case(engine, service)
    parameters = {key: value for key, value in parameters.items() if key != 'conflict_reviews'}
    expected = 'BINDING_REPORT_INTEGRITY_INVALID'
    if mutation == 'report_body':
        engine.store.update('branch', original['id'], report={**original['report'], 'summary': 'tampered without matching hash'})
    elif mutation == 'report_hash':
        engine.store.update('branch', original['id'], report_hash=canonical_hash('different report'))
    elif mutation == 'foreign_branch':
        engine.store.update('branch', original['id'], run_id='foreign-run')
        expected = 'BRANCH_SCOPE_DENIED'
    elif mutation == 'foreign_product':
        parameters['product_ref'] = {**parameters['product_ref'], 'id': 'foreign-product'}
        expected = 'TARGET_DENIED'
    elif mutation == 'receipt':
        operation = next(value for value in engine.store.list('operation') if value['key'] == 'save_product')
        engine.store.update('operation', operation['id'], receipt={**operation['receipt'], 'operation_id': 'forged-operation'})
        expected = 'BINDING_RECEIPT_INVALID'
    branch_before = engine.store.get('branch', original['id'])
    operations_before = engine.store.list('operation')
    model = OneCallBindingModel(responses=[], branch_id=original['id'], parameters=parameters)
    with pytest.raises(ContractError) as caught:
        await invoke_main(engine, scope, model, 'binding-hard-' + mutation)
    assert caught.value.code == expected and model.call_count == 1
    if mutation in {'foreign_branch', 'foreign_product'}:
        assert caught.value.status == 403
    assert not [branch for branch in engine.store.list('branch') if branch.get('binding_basis')]
    assert engine.store.get('branch', original['id']) == branch_before
    assert engine.store.list('operation') == operations_before
    assert not engine.store.get('run', run['id']).get('correctable_tool_errors')


@pytest.mark.asyncio
async def test_binding_workflow_unknown_preserves_fence_and_waits_without_parameter_correction(env):
    engine, service = env
    run, scope, _, _, original, parameters = await case(engine, service)
    parameters = {key: value for key, value in parameters.items() if key != 'conflict_reviews'}
    pending = 'sent-operation-needs-reconciliation'
    engine.store.update('run', run['id'], pending_unknown_operation=pending)
    operations_before = engine.store.list('operation')
    model = UnknownBindingModel(responses=[], branch_id=original['id'], parameters=parameters)
    result = await invoke_main(engine, scope, model, 'binding-unknown-waits-v4')
    feedback = next(item for item in result['messages'] if isinstance(item, ToolMessage)
        and item.tool_call_id == 'bind-without-review')
    assert json.loads(feedback.content)['status'] == 'OUTCOME_UNKNOWN'
    current = engine.store.get('run', run['id'])
    assert current['pending_unknown_operation'] == pending
    assert current['finish']['outcome'] == 'WAITING_JOBS' and current['acceptance']['ok'] is True
    assert not current.get('correctable_tool_errors') and model.call_count == 2
    assert not [branch for branch in engine.store.list('branch') if branch.get('binding_basis')]
    assert engine.store.get('branch', original['id']) == original
    assert engine.store.list('operation') == operations_before
