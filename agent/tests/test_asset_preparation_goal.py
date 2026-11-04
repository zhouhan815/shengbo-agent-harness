"""The asset recommendation task saves a scoped checklist through the Agent."""
from copy import deepcopy
import asyncio
import json
import time

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from shengbo_contracts.common import ContractError
from shengbo_prelive.models import RunScope
from shengbo_prelive.runtime import CURRENT_SCOPE
from shengbo_prelive.workflows import run_workflow
from test_runtime_business import BindableFakeModel, env, new_run, prepared_text, product


ASSET_REQUEST = '请给本场列一份电商视频、商品展示和直播间装修的素材准备清单，按优先级排列。'


@pytest.mark.asyncio
async def test_asset_preparation_reads_context_and_saves_only_checklist(env):
    engine, service = env
    room, prep, _ = await prepared_text(service)
    other = await product(service, '别场的商品')
    original = (await service.query('read_preparation', {'preparation_id': prep['id']}))['body']
    ref = original['selected_product_refs'][0]
    request = ASSET_REQUEST
    run = await new_run(engine, request, room_id=room['id'], preparation_id=prep['id'])
    engine.save_assessment(RunScope(run['id'], 'asset-intent', 'intent'), {
        'objective': 'asset_preparation', 'summary': '整理本场素材建议', 'authorization_quote': request,
    })
    await engine._freeze_goal(engine.store.get('run', run['id']))
    frozen = engine.store.get('run', run['id'])
    assert frozen['allowed_effects'] == ['save_asset_checklist']
    scope = RunScope(run['id'], 'asset-execute', 'execute')
    inputs = (await run_workflow(engine, scope, 'wf_playback_review', {'action': 'asset_inputs'}))['result']
    assert [entry['ref'] for entry in inputs['products']] == [ref]
    assert inputs['draft']['body']['draft']['theme'] == '隔离测试'
    assert inputs['asset_checklist']['status'] == 'not_prepared'

    checklist = {'items': [
        {'item_id': 'room-sign', 'category': 'other', 'purpose': '直播间主题背景',
         'recommended_content': '用不遮挡商品的纯色背景，画面上只放本场已确定的主题。', 'status': 'missing'},
        {'item_id': 'slipper-video', 'product_ref': ref, 'category': 'on_body', 'purpose': '拖鞋上脚短视频',
         'recommended_content': '先拍正侧面，再拍上脚两步及鞋底细节，保留实际商品画面。', 'status': 'missing'},
    ], 'notes': ['清单已按准备优先级排列。素材为建议，不影响声音播出。']}
    bad = deepcopy(checklist)
    bad['items'][1]['product_ref'] = other
    save = {'action': 'save_assets', 'asset_preparation_ref': inputs['preparation_ref'], 'asset_checklist': checklist}
    with pytest.raises(ContractError, match='ASSET_PRODUCT_NOT_SELECTED'):
        await run_workflow(engine, scope, 'wf_playback_review', {**save, 'asset_checklist': bad})
    assert (await service.query('read_asset_checklist', {'preparation_id': prep['id']}))['status'] == 'not_prepared'

    result = await run_workflow(engine, scope, 'wf_playback_review', save)
    saved = await service.query('read_asset_checklist', {'preparation_id': prep['id']})
    assert saved['ref'] == result['result']['data']['asset_checklist_ref']
    replay = await run_workflow(engine, scope, 'wf_playback_review', save)
    assert replay['result'] == result['result']
    changed = deepcopy(save)
    changed['asset_checklist']['notes'] = ['根据旧快照提交的改写。']
    stale = await run_workflow(engine, scope, 'wf_playback_review', changed)
    assert stale['status'] == 'NEEDS_REPLAN'
    assert [item['item_id'] for item in saved['body']['checklist']['items']] == ['room-sign', 'slipper-video']
    assert saved['body']['checklist']['notes'] == checklist['notes']
    assert saved['body']['visual_assets_required_for_sound'] is False
    after = (await service.query('read_preparation', {'preparation_id': prep['id']}))['body']
    for key in ('selected_product_refs', 'draft_ref', 'script_refs', 'playlist_ref', 'audio_manifest_ref'):
        assert after[key] == original[key]
    assert (await engine.finish(scope, 'COMPLETED', '已保存本场素材准备清单'))['accepted']
    assert not engine.store.list('branch')


class AssetRecommendationReplay(BindableFakeModel):
    calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        scope = CURRENT_SCOPE.get()
        if self.calls == 1:
            assert scope.phase == 'intent'
            name, args = 'save_task_assessment', {'assessment': {'objective': 'asset_preparation',
                'summary': '出一份本场素材清单', 'authorization_quote': ASSET_REQUEST}}
        elif self.calls == 2:
            assert scope.phase == 'execute'
            name, args = 'wf_playback_review', {'action': 'asset_inputs'}
        else:
            assert self.calls == 3, '已核验保存后不再调用主模型写总结'
            inputs = json.loads(next(msg.content for msg in reversed(messages) if isinstance(msg, ToolMessage)))['result']
            assert inputs['products'][0]['body']['name'] == '测试拖鞋'
            assert inputs['draft']['body']['draft']['theme'] == '隔离测试'
            name, args = 'wf_playback_review', {'action': 'save_assets', 'asset_preparation_ref': inputs['preparation_ref'],
                'asset_checklist': {'items': [
                    {'item_id': 'slipper-product-video', 'product_ref': inputs['products'][0]['ref'], 'category': 'on_body',
                     'purpose': '优先拍拖鞋上脚短视频', 'recommended_content': '固定镜头先拍正侧面，再拍上脚走两步；保留实际颜色和鞋底细节。'},
                    {'item_id': 'slipper-detail', 'product_ref': inputs['products'][0]['ref'], 'category': 'detail',
                     'purpose': '补鞋底和鞋面展示图', 'recommended_content': '用干净背景各拍一张近景，供讲到外观时对照。'},
                    {'item_id': 'room-lighting', 'category': 'other', 'purpose': '可选布置直播间背景和灯光',
                     'recommended_content': '用暖白主灯、纯色背景和同色桌布，预留手持商品展示位置。'},
                ], 'notes': ['按建议准备顺序排列，背景布置可选。']}}
        message = AIMessage(content='', tool_calls=[{'id': f'asset-replay-{self.calls}', 'name': name, 'args': args}])
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.asyncio
async def test_agent_asset_recommendations_replay_saves_and_closes_without_extra_model(env):
    engine, service = env
    room, prep, _ = await prepared_text(service)
    model = AssetRecommendationReplay(responses=[])
    engine._provided_model = model
    await engine.start()
    started = time.perf_counter()
    run = await new_run(engine, ASSET_REQUEST, room_id=room['id'], preparation_id=prep['id'])
    async with asyncio.timeout(15):
        while (current := engine.get_run(run['id']))['status'] not in {'COMPLETED', 'BLOCKED', 'FAILED'}:
            await asyncio.sleep(.04)
    elapsed = time.perf_counter() - started
    assert current['status'] == 'COMPLETED', current.get('error')
    assert model.calls == 3
    saved = await service.query('read_asset_checklist', {'preparation_id': prep['id']})
    assert [item['status'] for item in saved['body']['checklist']['items']] == ['missing'] * 3
    assert [item['item_id'] for item in saved['body']['checklist']['items']] == ['slipper-product-video', 'slipper-detail', 'room-lighting']
    assert any(item['destination'].get('object_ref') == saved['ref'] and item['destination']['view'] == 'overview'
               for item in current['artifacts'])
    assert not current['branches']
    print(f'asset preparation: replayed model calls={model.calls}; elapsed_seconds={elapsed:.3f}; paid calls=0')
