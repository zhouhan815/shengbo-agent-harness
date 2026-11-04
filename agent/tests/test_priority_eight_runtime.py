"""Integration of source scope, typed readiness and model-facing views."""
from copy import deepcopy
import base64
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from langchain_core.messages import ToolMessage
from shengbo_contracts.business import AnalysisReport
from shengbo_contracts.common import ContractError,canonical_hash
from shengbo_prelive.models import RunScope
from shengbo_prelive.models import IntentAssessment
from shengbo_prelive.policy import validate_intent
from shengbo_prelive.material_views import source_view
from shengbo_prelive.providers.chatgpt import ChatGPTPlanModel,payload_text_chars
from test_runtime_business import env,new_run


def report_for(source):
    return {'summary':'按本次原资料整理，不推断额外承诺','evidence':[
        {'id':'e1','source_ref':source,'locator':'text:1','quote':'测试商品，129元'}],
      'material_assessment':{'product_candidates':[{'candidate_id':'p1','name':'测试商品','identity_status':'new','evidence_ids':['e1']}],
        'field_observations':[{'observation_id':'name','product_candidate_id':'p1','field_path':'name',
          'value':'测试商品','claim_kind':'fact','evidence_ids':['e1']}],
        'missing_info':[{'item_id':'story','product_candidate_id':'p1','field_path':'brand_story',
          'level':'optional','why':'可丰富风格但不影响商品介绍','question':'是否补充品牌故事','can_skip':True}]}}


async def report_branch(engine):
    run=await new_run(engine,'依据资料整理商品并生成话术，不生成语音')
    source=engine.materials.accept(run['conversation_id'],'facts.md','text/markdown','测试商品，129元'.encode())['material_ref']
    engine.store.update('run',run['id'],phase='execute',status='RUNNING',input={**run['input'],'material_refs':[source]})
    engine.store.put('branch',{'id':'priority-facts','run_id':run['id'],'role':'product_facts','status':'RUNNING',
      'task':{'role':'product_facts','instruction':'整理真实原文','source_refs':[source],'product_ids':[],'input_refs':[]}})
    return run,source,RunScope(run['id'],'priority-child','execute','product_facts','priority-facts')


def test_optional_assessment_does_not_rewrite_legacy_report_hash():
    report=AnalysisReport(summary='旧报告')
    old=report.model_dump(mode='json')
    assert 'material_assessment' not in old
    assert canonical_hash(old)==canonical_hash(AnalysisReport.model_validate(old).model_dump(mode='json'))


def test_materials_to_script_is_an_explicit_text_goal_without_unrequested_playback():
    text='根据商品资料新建商品并生成话术，只要文本，不生成语音'
    proposal=IntentAssessment(objective='materials_to_script',summary='资料到文本',authorization_quote=text)
    value=validate_intent({'input':{'text':text}},proposal)
    assert value['requires_decision'] is False
    assert {'save_product','save_session_draft','submit_script_change'}<=set(value['allowed_effects'])
    assert not {'save_playlist','prepare_required_audio','publish_preparation'}&set(value['allowed_effects'])
    read='只分析这些商品资料和话术'
    proposal=IntentAssessment(objective='materials_to_script',summary='不应扩大',authorization_quote=read)
    assert validate_intent({'input':{'text':read}},proposal)['requires_decision'] is True


@pytest.mark.asyncio
async def test_real_report_readiness_and_source_ledger_are_not_approval(env):
    engine,_=env
    run,source,scope=await report_branch(engine)
    saved=engine.submit_report(scope,report_for(source))
    assert saved['material_readiness']['ready'] is True
    assert saved['material_readiness']['quality_counts']['missing_by_level']['optional']==1
    main=RunScope(run['id'],'priority-main','execute')
    result=await engine.material_readiness(main,{'purpose':'script_generation','branch_task_ids':['priority-facts']})
    assert result['authorization_changed'] is False
    assert result['items'][0]['readiness']['questions_by_product'][0]['items'][0]['blocks_current_task'] is False
    first=await engine.read_source(main,source,'text:1')
    second=await engine.read_source(main,source,'text:1')
    assert first['text']==second['text']=='测试商品，129元'
    assert second['read_observation']['read_count']==2
    assert 'text' not in second['manifest']
    assert 'text' not in second['manifest']['locators'][0]
    assert engine.materials.read(source,run['conversation_id'])['manifest']['text']=='测试商品，129元'
    assert any(e['type']=='material.read.completed' for e in engine.trace.events(run['id'])['events'])


@pytest.mark.asyncio
@pytest.mark.parametrize("prefetch", [False, True])
async def test_small_source_is_prefetched_once_for_dispatched_worker(env, prefetch):
    import asyncio
    from langchain_core.messages import AIMessage
    from test_selection_dependency_recovery_v1 import ScriptedModel

    engine, _ = env
    await engine.start()
    engine.scheduler.cancel()
    await asyncio.gather(engine.scheduler, return_exceptions=True)
    run, source, scope = await report_branch(engine)
    objective = "materials_to_script" if prefetch else "facts_analysis"
    run = engine.store.update("run", run["id"], goal={"objective": objective})
    branch = engine.store.get("branch", scope.branch_task_id)
    calls = []

    async def logic(messages):
        source_messages = [part["text"] for message in messages if message.type == "human"
            for part in message.content if isinstance(part, dict) and part.get("type") == "text"]
        assert sum("测试商品，129元" in text for text in source_messages) == int(prefetch)
        calls.append(True)
        if not prefetch and len(calls) == 1:
            return AIMessage(content="", tool_calls=[{"id": "explicit-source-read",
                "name": "read_source", "args": {"ref": source}}])
        if not prefetch:
            assert "测试商品，129元" in messages[-1].content
        return AIMessage(content="", tool_calls=[{"id": "prefetch-direct-submit",
            "name": "submit_product_facts_report", "args": {"report": report_for(source)}}])

    engine.model = ScriptedModel(logic)
    engine.workers["product_facts"] = engine._build_graph("product_facts")
    await engine._invoke(run, branch=branch)
    assert len(calls) == (1 if prefetch else 2)
    assert engine.store.get("branch", scope.branch_task_id)["status"] == "PREPARED"
    reads = [row for row in engine.store.list("document_read")
             if row.get("kind") == "material_source_result"]
    assert [(r["artifact_ref"], r["branch_task_id"], r["read_count"]) for r in reads] == [
        (source, scope.branch_task_id, 1)]


@pytest.mark.asyncio
async def test_automatic_handoff_preserves_the_product_revision_under_review(env):
    from test_runtime_business import Product, human_command, product

    engine, service = env
    run, source, child_scope = await report_branch(engine)
    original = await product(service)
    branch = engine.store.get("branch", child_scope.branch_task_id)
    engine.store.update("branch", branch["id"], task={**branch["task"],
        "product_ids": [original["id"]], "input_refs": [original]})
    engine.submit_report(child_scope, {**report_for(source), "fields": {"price": 178},
        "product_ids": [original["id"]], "source_refs": [source], "dependencies": [original]})
    previous = (await service.query("read_product", {"ref": original}))["body"]
    fields = {key: previous[key] for key in Product.model_fields if key in previous}
    fields["price"] = 179
    revised = await human_command(service, "save_product", {
        "product_id": original["id"], "expected_revision": original["revision"], "schema_hash": service.schema()["schema_hash"],
        "fields": fields, "patches": [{"path": "/price", "op": "set", "value": 179}]})
    assert revised["data"]["product_ref"] != original

    result = await engine._accept_clean_analysis(RunScope(run["id"], "main-handoff", "execute"), [branch["id"]])
    assert result["status"] == "NEEDS_REVIEW"
    assert result["product_revision_review"] == [branch["id"]]
    assert engine.store.get("branch", branch["id"])["status"] == "PREPARED"


@pytest.mark.asyncio
async def test_assessment_rejects_missing_evidence_and_foreign_source(env):
    engine,_=env
    run,source,scope=await report_branch(engine)
    bad=report_for(source);bad['evidence']=[]
    with pytest.raises(ContractError) as caught:engine.submit_report(scope,bad)
    assert caught.value.code=='MATERIAL_EVIDENCE_MISSING'
    bad=report_for(source);bad['evidence'][0]['source_ref']={**source,'id':'foreign-source'}
    with pytest.raises(ContractError) as caught:engine.submit_report(scope,bad)
    assert caught.value.status==403
    assert engine.store.get('branch','priority-facts').get('report') is None


@pytest.mark.asyncio
async def test_assessment_validation_feedback_has_exact_paths_without_input_values(env):
    engine,_=env
    run,source,scope=await report_branch(engine)
    bad=report_for(source)
    bad['material_assessment']['blocking_gaps']='private-untrusted-fixture-do-not-echo'
    with pytest.raises(ContractError) as caught:engine.submit_report(scope,bad)
    assert caught.value.code=='MATERIAL_ASSESSMENT_INVALID'
    issues=caught.value.details['issues']
    assert any('blocking_gaps' in x['loc'] for x in issues)
    assert all('input' not in x for x in issues)
    assert 'private-untrusted-fixture-do-not-echo' not in str(caught.value.details)


def test_source_projection_preserves_one_copy_and_full_original():
    original={'text':'原文','manifest':{'text':'原文','locators':[{'id':'p1','text':'原文'}],'pages':[{'number':1,'text':'原文'}]}}
    view=source_view(original)
    assert view['text']=='原文' and view['manifest']['locators'][0]['id']=='p1'
    assert original['manifest']['text']=='原文' and original['manifest']['pages'][0]['text']=='原文'


def test_provider_keeps_tool_images_and_does_not_count_base64_as_text():
    output=BytesIO();Image.new('RGB',(2,2),'white').save(output,format='PNG')
    uri='data:image/png;base64,'+base64.b64encode(output.getvalue()).decode()
    # Build payload only; no credential access or external model call.
    model=ChatGPTPlanModel.model_construct(model_name='gpt-6.1-sol')
    payload=model._payload([ToolMessage(content=[{'type':'text','text':'原图位置image:1'},
        {'type':'image_url','image_url':{'url':uri}}],tool_call_id='source-image')])
    parts=payload['input'][0]['output']
    assert parts[1]['type']=='input_image' and parts[1]['image_url']==uri
    assert payload_text_chars(payload)<len(str(payload))


def test_role_skill_references_are_available_on_demand_without_other_roles(env):
    engine,_=env
    files=engine._initial_files('product_facts')
    prefix='/skills/product_facts/prelive-product-facts/'
    assert prefix+'SKILL.md' in files
    assert prefix+'references/material-assessment-schema.json' in files
    assert all(name.startswith(prefix) for name in files)
    assert not any(name.endswith(('.py','.env')) for name in files)


@pytest.mark.asyncio
async def test_normal_script_status_reuses_exact_observation_without_raw_provider_reread():
    from shengbo_prelive.workflows import scripts
    job={'ref':{'id':'existing-job'},'body':{'kind':'scripts','status':'succeeded'}}
    run={'id':'run','scope':{},'pending_unknown_operation':None}
    flags=[]
    async def observed(_):return {'jobs':[job],'pending_job_ids':[],'unknown_job_ids':[]}
    def projected(_,value,include_provider_outputs):
        flags.append(include_provider_outputs)
        return {**value,'items_projection':{'unknown_item_count':0}}
    engine=SimpleNamespace(store=SimpleNamespace(get=lambda *_:run),_completion_observation=observed,
        _completion_observation_view=lambda *_:{},_job_view=projected)
    class Flow:
        scope=SimpleNamespace(run_id='run')
        async def prep(self):return {'body':{}}
        async def step(self,_name,call):return await call()
        async def query(self,*_):pytest.fail('normal status must not reread the full provider job')
        def result(self,status,**kwargs):return {'status':status,**kwargs}
    flow=Flow();flow.engine=engine
    result=await scripts(flow,{'action':'status','job_id':'existing-job'})
    assert result['status']=='COMPLETED'
    assert flags==[False]
