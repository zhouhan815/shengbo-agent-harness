"""New input recovery is conditional; permission/unknown/integrity stay hard."""
from types import SimpleNamespace
import pytest

from shengbo_contracts.common import ContractError
from shengbo_prelive.action_contracts import PREWRITE_CORRECTABLE_ERRORS, recoverable_input_error, uncommitted_input_error
from shengbo_prelive.models import RunScope
from test_runtime_business import env, new_run


@pytest.mark.parametrize('code', sorted(PREWRITE_CORRECTABLE_ERRORS))
def test_prewrite_recovery_needs_its_proven_no_commit_marker(code):
    assert recoverable_input_error(uncommitted_input_error(code, 'input needs correction'))
    assert not recoverable_input_error(ContractError(code, 'unproven'))
    assert not recoverable_input_error(ContractError(code, 'already sent', details={'commit_state':'sent'}))
    assert not recoverable_input_error(uncommitted_input_error(code, 'input', status=403))
    assert not recoverable_input_error(uncommitted_input_error(code, 'input'), pending_unknown=True)


@pytest.mark.parametrize('code', ['BINDING_REPORT_INTEGRITY_INVALID', 'BINDING_RECEIPT_INVALID',
    'SOURCE_BINDING_CHANGED', 'OPERATION_CHANGED', 'HUMAN_REVISION_CHOICE_REQUIRED'])
def test_hard_boundaries_cannot_be_turned_into_input_retries_by_adding_a_marker(code):
    assert not recoverable_input_error(ContractError(code, 'hard', details={'commit_state':'not_committed'}))


@pytest.mark.asyncio
async def test_decision_parameter_errors_do_not_create_question_and_a_real_role_rejection_stays_hard(env):
    engine, _ = env
    run = await new_run(engine, '解释资料，不保存任何正式内容')
    engine.store.update('run',run['id'],status='RUNNING',phase='execute')
    scope=RunScope(run['id'],'decision-correction','execute')
    with pytest.raises(ContractError) as caught:
        await engine.request_decision(scope,{'kind':'','prompt':'bad'},'empty-question')
    assert caught.value.code=='DECISION_INPUT_INVALID' and recoverable_input_error(caught.value)
    assert not engine.store.list('question')
    # The role split is checked even with the same malformed parameters. No
    # arbitrary phase/role object may convert this to an ordinary retry.
    original=engine.assert_scope
    async def same_run(_scope): return engine.store.get('run',run['id'])
    engine.assert_scope=same_run
    try:
        with pytest.raises(ContractError) as denied:
            await engine.request_decision(RunScope(run['id'],'child','execute','product_facts','non-authorized'),{},'child-question')
        assert denied.value.status==403 and not recoverable_input_error(denied.value)
    finally: engine.assert_scope=original
    assert not engine.store.list('question')
