"""Already-bound reports get precise diagnostics, without accepting or rebinding."""
import pytest
from shengbo_contracts.common import ContractError
from shengbo_prelive.models import RunScope
from test_runtime_business import env
from test_source_alias_bridge import Product, accepted_sources

@pytest.mark.asyncio
@pytest.mark.parametrize('same', [True, False])
async def test_existing_product_dependency_is_reported_without_binding_or_accepting(env, same):
 engine, service = env
 run, scope, sources, _, _ = await accepted_sources(engine, service)
 saved = await engine.business_command(scope, 'save_product', {
  'schema_hash':service.schema()['schema_hash'], 'patches':[],
  'fields':Product(name='依赖诊断测试',price=129).model_dump(),
  'source_manifest_refs':sources},'diagnostic-create')
 ref=saved['data']['product_ref']
 dep=ref if same else {**ref,'revision':ref['revision']+1}
 branch='diagnostic-branch'
 engine.store.put('branch', {'id':branch,'run_id':run['id'],'role':'audience_scenes','status':'RUNNING',
  'task':{'role':'audience_scenes','instruction':'只分析指定商品','product_ids':[ref['id']], 'source_refs':sources,'input_refs':[dep]}})
 engine.submit_report(RunScope(run['id'],'diagnostic-worker','execute','audience_scenes',branch), {
  'summary':'已基于指定商品进行顾客分析','product_ids':[ref['id']], 'dependencies':[dep], 'source_refs':sources})
 before=engine.store.get('branch',branch)
 with pytest.raises(ContractError) as caught:
  await engine.bind_source_analysis(scope,[branch],{'product_ref':ref,'validation_note':'检查已有依赖，不自动改绑。'},'diagnostic-bind')
 assert caught.value.code=='BINDING_REQUIRES_REPLAN'
 details=caught.value.details
 assert details['same_exact_product_dependency'] is same
 assert details['existing_accepted_ref'] is None
 assert details['automatic_acceptance'] is details['automatic_fact_changes'] is False
 assert engine.store.get('branch',branch)==before
 assert not [b for b in engine.store.list('branch') if b['id'].startswith('bound_')]
