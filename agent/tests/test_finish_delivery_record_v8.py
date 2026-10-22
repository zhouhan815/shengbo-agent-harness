"""Final public judgement binds verified outputs without a second plan write."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_runtime_business import env
from test_split_script_completion_v3 import setup_task, generate_one
from shengbo_contracts.common import canonical_hash
from shengbo_prelive.middleware import CompletionGuard


@pytest.mark.asyncio
async def test_finish_summary_binds_two_formal_scripts_without_rewriting_plan(env):
    engine, service = env
    task = await setup_task(engine, service)
    assert (await generate_one(engine, service, task, 'finish-v1'))['status'] == 'succeeded'
    assert (await generate_one(engine, service, task, 'finish-v2'))['status'] == 'succeeded'
    before = deepcopy(engine.store.get('run', task['run_id']))
    statement = '两版稿已核对来源、价格与适用条件；保留未知规格，交付当前两版正式文本。'
    result = await engine.finish(task['scope'], 'COMPLETED', statement)
    assert result['accepted'] is True
    current = engine.store.get('run', task['run_id'])
    assert current.get('task_assessment') == before.get('task_assessment')
    assert canonical_hash(current['goal']) == canonical_hash(before['goal'])
    record = current['delivery_record']
    assert record['statement'] == current['summary'] == statement
    assert record['source'] == 'finish_turn.summary'
    assert record['formal_script_refs'] == current['acceptance']['script_coverage']['formal_script_refs']
    assert len(record['formal_script_refs']) == 2
    assert record['checked_operation_ids'] == current['acceptance']['checked_operation_ids']
    assert record['human_approval_granted'] is False
    assert record['semantic_approval_granted'] is False
    assert record['task_assessment_rewritten'] is False
    assert result['finish_metrics']['verified_script_count'] == 2
    assert result['finish_metrics']['verification_ms'] >= 0
    assert result['finish_metrics']['extra_model_request_required'] is False
    assert await CompletionGuard(engine).abefore_model({}, SimpleNamespace(context=task['scope'])) == {'jump_to': 'end'}
    preparation = await service.query('read_preparation', {'preparation_id': task['preparation_id']})
    assert preparation['body'].get('approval_ref') is None


@pytest.mark.asyncio
async def test_rejected_finish_does_not_create_delivery_or_overwrite_plan(env):
    engine, service = env
    task = await setup_task(engine, service, count=2)
    assert (await generate_one(engine, service, task, 'only-v1'))['status'] == 'succeeded'
    before = deepcopy(engine.store.get('run', task['run_id']))
    result = await engine.finish(task['scope'], 'COMPLETED', '不能用结束说明冒充缺少的第二版。')
    assert result['accepted'] is False
    assert any('版数不足' in missing for missing in result['missing'])
    current = engine.store.get('run', task['run_id'])
    assert current.get('delivery_record') is None
    assert current.get('finish') is None
    assert current.get('task_assessment') == before.get('task_assessment')
    assert canonical_hash(current['goal']) == canonical_hash(before['goal'])
    assert await CompletionGuard(engine).abefore_model({}, SimpleNamespace(context=task['scope'])) is None


@pytest.mark.asyncio
async def test_waiting_is_not_recorded_as_a_reviewed_delivery(env, monkeypatch):
    engine, service = env
    task = await setup_task(engine, service)
    async def pending_observation(_run):
        return {'pending_job_ids': ['pending-job'], 'unknown_job_ids': [], 'jobs': []}
    monkeypatch.setattr(engine, '_completion_observation', pending_observation)
    result = await engine.finish(task['scope'], 'WAITING_JOBS', '已受理工作尚未完成，等待实际结果。')
    assert result['accepted'] is True
    current = engine.store.get('run', task['run_id'])
    assert current.get('delivery_record') is None
    assert current['finish']['outcome'] == 'WAITING_JOBS'
    assert current['finish_metrics']['verified_script_count'] == 0
