"""A nonempty query envelope is not evidence that current research exists."""
import pytest
from shengbo_contracts.common import canonical_hash
from shengbo_prelive.models import RunScope
from test_runtime_business import env, product, new_run, human_command


@pytest.mark.asyncio
async def test_catalog_acceptance_requires_research_for_saved_exact_revision(env):
    engine, service = env
    ref = await product(service)
    run = await new_run(engine, '更新测试拖鞋资料和顾客研究，不改稿')
    engine.store.update('run', run['id'], phase='execute', status='RUNNING',
        goal={'objective':'catalog_update'}, finish={'outcome':'COMPLETED','summary':'已保存'}, allowed_effects=[])
    engine.store.put('operation', {'id':'saved-product','run_id':run['id'],'status':'committed','key':'save_product',
        'receipt':{'data':{'product_ref':ref}}})
    engine.store.put('query_evidence', {'id':'empty-history','run_id':run['id'],'key':'find_audience_research','result':{'items':[]}})
    engine.store.put('query_evidence', {'id':'impact-read','run_id':run['id'],'key':'inspect_change_impact','result':{'items':[]}})
    scope = RunScope(run['id'],'acceptance-probe','execute','main')
    invalid = await engine.verify_finish(scope)
    assert not invalid['ok']
    assert any('当前修订的有效顾客研究' in item for item in invalid['missing'])
    from shengbo_contracts.business import AnalysisReport
    report=AnalysisReport(summary='资料支持居家场景，尚无真实转化验证',product_ids=[ref['id']],hypotheses=['居家场景需要后续验证'])
    await human_command(service,'accept_analysis_report',{'role':'audience_scenes','report':report.model_dump(mode='json'),
        'report_hash':canonical_hash(report),'product_refs':[ref]})
    assert (await engine.verify_finish(scope))['ok']
