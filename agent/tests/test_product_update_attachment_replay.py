"""Existing product update from the same upload message used by the workbench."""
import time

import pytest

from shengbo_prelive.models import MessageRequest, RunScope
from shengbo_prelive.workflows import run_workflow
from test_runtime_business import Product, env, human_command, product


@pytest.mark.asyncio
async def test_existing_product_upload_preserves_condition_price_and_neighbor(env):
    engine, service = env
    initial = Product(name='雾灰通勤伞', price=59.9, sku_id='UM-GY-01', material='涤纶',
        knowledge='伞套随商品提供。').model_dump()
    target = (await human_command(service, 'save_product', {'schema_hash': service.schema()['schema_hash'],
        'patches': [], 'fields': initial}))['data']['product_ref']
    other = await product(service, '午夜蓝通勤伞')
    source_text = '雾灰通勤伞，SKU UM-GY-01。只有伞面完全晾干后才能收纳。下单满两把且同一地址才包邮。'
    text = '请根据本次上传资料，只更新商品库中的「雾灰通勤伞」。本次只整理并保存基础商品资料，暂不开展顾客研究。'
    conversation = engine.create_conversation({'scope': {'kind': 'catalog'}})
    uploaded = engine.accept_material(conversation['id'], '通勤伞补充说明.txt', 'text/plain', source_text.encode())
    source = uploaded['material_ref']
    started = time.perf_counter()
    accepted = await engine.submit_message(conversation['id'], MessageRequest(text=text, client_request_id='umbrella-update',
        intent_mode='new_task', material_refs=[source], selection={'target': {'object_ref': target}}, observed_versions=[target],
        view_context={'view': 'products', 'product_material_action': 'update', 'product_id': target['id']}))
    run_id = accepted['run']['id']
    scope = RunScope(run_id, 'umbrella-main', 'execute')
    engine.save_assessment(RunScope(run_id, 'umbrella-intent', 'intent'), {'objective': 'catalog_update',
        'summary': '仅更新雾灰款的条件说明', 'authorization_quote': text,
        'required_analyses': ['product_facts'], 'catalog_delivery': 'basic_facts_only', 'catalog_delivery_quote': text})
    await engine._freeze_goal(engine.store.get('run', run_id))
    engine.save_assessment(scope, {'summary': '核对附件与所选精确商品，只更新依据明确的问答', 'capabilities': ['product_facts', 'wf_product_materials']})
    batch = await engine.dispatch(scope, [{'role': 'product_facts', 'instruction': '核对选中款式和有条件的使用、运费说明。',
        'product_ids': [target['id']], 'source_refs': [source], 'input_refs': [target]}], '资料补录', 'umbrella-facts')
    branch_id, = batch['branch_task_ids']
    engine.store.update('branch', branch_id, status='RUNNING')
    worker = RunScope(run_id, 'umbrella-worker', 'execute', 'product_facts', branch_id)
    read = await engine.read_source(worker, source, 'text:1')
    assert read['text'] == source_text
    faq = '问：怎么收纳？\n答：只有伞面完全晾干后才能收纳。\n问：什么时候包邮？\n答：下单满两把且同一地址才包邮。'
    engine.submit_report(worker, {'summary': 'SKU与选中款一致，两条条件明确', 'fields': {'faq': faq},
        'product_ids': [target['id']], 'source_refs': [source], 'dependencies': [target],
        'evidence': [{'id': 'umbrella-conditions', 'source_ref': source, 'locator': 'text:1', 'quote': source_text}]},
        strict_evidence=True)
    formal, = (await engine.accept_reports(scope, [branch_id], 'umbrella-facts-accept'))['accepted_refs']
    flow = await run_workflow(engine, scope, 'wf_product_materials', {'action': 'save_from_facts', 'parameters': {
        'branch_task_id': branch_id, 'accepted_report_ref': formal, 'product_ref': target}})
    updated = flow['result']['data']['product_ref']
    saved = (await service.query('read_product', {'ref': updated}))['body']
    assert updated['id'] == target['id'] and updated['revision'] == target['revision'] + 1
    assert {key: saved[key] for key in initial} == {**initial, 'faq': faq}
    assert (await service.query('read_product', {'product_id': other['id']}))['ref'] == other
    with service.store.transaction() as db:
        assert len(service.store.all(db, 'product')) == 2
    closure = await engine.finish(scope, 'COMPLETED', '已按附件更新所选商品')
    assert closure['accepted'], closure['missing']
    proof, = engine.store.list('product_candidate_save_resolution')
    engine.store.update('product_candidate_save_resolution', proof['id'],
        update_proof={**proof['update_proof'], 'base_product_ref': other})
    rejected = await engine.finish(scope, 'COMPLETED', '不应接受没有原商品证明的报告')
    assert not rejected['accepted']
    assert any('product_facts' in missing for missing in rejected['missing'])
    print(f'product attachment: injected one typed facts report, elapsed_seconds={time.perf_counter() - started:.3f}; paid calls=0')
