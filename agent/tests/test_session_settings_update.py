"""Preserve unrelated settings when the Agent changes named fields."""
import asyncio
import json
import time

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
import pytest

from shengbo_prelive.models import RunScope
from shengbo_prelive.runtime import CURRENT_SCOPE
from shengbo_prelive.workflows import run_workflow
from test_runtime_business import BindableFakeModel, env, human_command, new_run, prepared_text


async def seed_settings(service):
    room, prep, script = await prepared_text(service)
    current = await service.query('read_preparation', {'preparation_id': prep['id']})
    draft = (await service.query('read_context', {'ref': current['body']['draft_ref']}))['body']['draft']
    target = current['body']['selected_product_refs'][0]['id']
    receipt = await human_command(service, 'save_session_draft', {'preparation_id': prep['id'],
        'expected_revision': current['ref']['revision'], 'draft': {**draft, 'theme': '原来的居家主题',
            'objective': '保留原来的讲解目标', 'scheduled_at': '2026-11-12T19:30:00+08:00', 'timezone': 'Asia/Shanghai',
            'commerce': {**draft['commerce'], 'product_overrides': {target: {'mode': 'price', 'price': 129}}}}})
    prep = receipt['data']['preparation_ref']
    draft = (await service.query('read_context', {'ref': receipt['data']['draft_ref']}))['body']['draft']
    return room, prep, target, draft, script


class SessionSettingsReplay(BindableFakeModel):
    request: str
    target: str
    repair: bool = False
    calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        scope = CURRENT_SCOPE.get()
        if self.calls == 1:
            assert scope.phase == 'intent'
            name, args = 'save_task_assessment', {'assessment': {'objective': 'session_config',
                'summary': '更新明确的本场活动配置', 'authorization_quote': self.request}}
        else:
            assert 2 <= self.calls <= 2 + self.repair and scope.phase == 'execute'
            if self.calls == 3:
                error = json.loads(next(msg.content for msg in reversed(messages) if isinstance(msg, ToolMessage)))['error']
                assert error['code'] == 'INVALID_CAMPAIGN_EXPIRY'
                assert error['details']['commit_state'] == 'not_committed'
            ends_at = '2026-11-12T22:00:00' if self.repair and self.calls == 2 else '2026-11-12T22:00:00+08:00'
            name, args = 'wf_context_bundle', {'action': 'apply_session_settings', 'session_updates': [
                {'path': '/theme', 'value': '冬日居家好物'},
                {'path': '/campaign_ends_at', 'value': ends_at},
                {'path': f'/commerce/product_overrides/{self.target}/price', 'value': 99},
            ]}
        message = AIMessage(content='', tool_calls=[{'id': f'session-settings-{self.calls}', 'name': name, 'args': args}])
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.asyncio
@pytest.mark.parametrize('repair', [False, True])
async def test_chat_updates_session_settings_and_preserves_unmentioned_config(env, repair):
    engine, service = env
    room, prep, target, original, script = await seed_settings(service)
    request = '把本场主题改成冬日居家好物，现有拖鞋活动价调整为99元，活动截止为2026年11月12日北京时间22点。'
    model = SessionSettingsReplay(responses=[], request=request, target=target, repair=repair)
    engine._provided_model = model
    await engine.start()
    started = time.perf_counter()
    run = await new_run(engine, request, room_id=room['id'], preparation_id=prep['id'])
    async with asyncio.timeout(15):
        while (current := engine.get_run(run['id']))['status'] not in {'COMPLETED', 'BLOCKED', 'FAILED'}:
            await asyncio.sleep(.04)
    elapsed = time.perf_counter() - started
    assert current['status'] == 'COMPLETED', current.get('error')
    assert model.calls == 2 + repair
    record = await service.query('read_preparation', {'preparation_id': prep['id']})
    saved = (await service.query('read_context', {'ref': record['body']['draft_ref']}))['body']['draft']
    assert saved == {**original, 'theme': '冬日居家好物', 'campaign_ends_at': '2026-11-12T22:00:00+08:00',
        'commerce': {**original['commerce'], 'product_overrides': {target: {**original['commerce']['product_overrides'][target], 'price': 99}}}}
    assert record['body']['script_refs'] == [script]
    assert not current['branches']
    assert any(item['destination'].get('object_ref') == record['body']['draft_ref'] for item in current['artifacts'])
    committed = [op for op in engine.store.list('operation') if op['status'] == 'committed']
    assert len(committed) == 1
    print(f'session settings (repair={repair}): replayed model calls={model.calls}; elapsed_seconds={elapsed:.3f}; paid calls=0')


@pytest.mark.asyncio
async def test_session_patch_rejects_overlaps_and_reuses_same_receipt(env):
    engine, service = env
    room, prep, target, original, _ = await seed_settings(service)
    request = '修改本场主题。'
    run = await new_run(engine, request, room_id=room['id'], preparation_id=prep['id'])
    engine.save_assessment(RunScope(run['id'], 'session-intent', 'intent'), {'objective': 'session_config',
        'summary': request, 'authorization_quote': request})
    await engine._freeze_goal(engine.store.get('run', run['id']))
    scope = RunScope(run['id'], 'session-execute', 'execute')
    args = {'action': 'apply_session_settings', 'session_preparation_ref': prep}
    overlap = await run_workflow(engine, scope, 'wf_context_bundle', {**args, 'session_updates': [
        {'path': '/commerce', 'value': {}}, {'path': f'/commerce/product_overrides/{target}/price', 'value': 39}]})
    invalid = await run_workflow(engine, scope, 'wf_context_bundle', {**args, 'session_updates': [
        {'path': '/theme', 'value': {'value': '非法主题值'}}]})
    assert overlap['status'] == invalid['status'] == 'NEEDS_REPLAN'
    assert not engine.store.list('operation')
    args['session_updates'] = [{'path': '/theme', 'value': '新主题'}]
    saved = await run_workflow(engine, scope, 'wf_context_bundle', args)
    again = await run_workflow(engine, scope, 'wf_context_bundle', args)
    assert again['result'] == saved['result']
    stale = await run_workflow(engine, scope, 'wf_context_bundle', {**args,
        'session_updates': [{'path': '/theme', 'value': '过期主题'}]})
    assert stale['status'] == 'NEEDS_REPLAN'
    assert len(engine.store.list('operation')) == 1
    result = await service.query('read_context', {'ref': saved['result']['data']['draft_ref']})
    assert result['body']['draft'] == {**original, 'theme': '新主题'}
