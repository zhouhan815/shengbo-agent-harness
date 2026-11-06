'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:fs.existsSync(chromium.executablePath())?chromium.executablePath():'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));let checks=0;
  try{
    await page.goto(process.argv[2]);
    await page.evaluate(()=>{
      const sort=v=>Array.isArray(v)?v.map(sort):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,sort(v[k])])):v;
      const request=async(path,body)=>{const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const v=await r.json();if(!r.ok)throw Object.assign(Error(v.error?.message||'请求失败'),v.error||{});return v;};
      const hash=async value=>{const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(sort(value))));return 'sha256:'+Array.from(new Uint8Array(bytes),byte=>byte.toString(16).padStart(2,'0')).join('');};
      window.ShengboWorkbenchApi={request,query:(query,args)=>request('/api/prelive/v2/queries',{query,args}),command:(command,args,opts={})=>request('/api/prelive/v2/commands',{command,args,mutation:{operation_id:opts.operationId||crypto.randomUUID()}}),uuid:()=>crypto.randomUUID(),hash};
      window.ui=ShengboWorkspaceEditors.mount({container:document.getElementById('editor'),storage:null,onNavigate:d=>window.ui.open(d),onSelect:s=>window.selected=s,onPrepareGeneration:text=>window.generationMessage=text});
    });
    const idle=()=>page.waitForFunction(()=>!document.querySelector('#editor').hasAttribute('aria-busy'));const waitJob=async kind=>{for(let attempt=0;attempt<200;attempt++){const v=await page.evaluate(()=>ShengboWorkbenchApi.query('read_completion_observation',{preparation_id:seed.preparation_id}));if(!v.pending_job_ids.length&&v.jobs.some(j=>j.body.kind===kind&&j.body.status==='succeeded'))return;await new Promise(r=>setTimeout(r,50));}throw Error('Test job did not finish: '+kind);};
    const noError=async()=>{const text=await page.locator('[role=status]').textContent();assert.ok(!/请求失败|格式不正确|不符合合同|不能为空|INVALID|REVISION_CONFLICT|未授权/.test(text),text);checks++;};
    await page.evaluate(()=>window.ui.open({view:'playlist',preparation_id:window.seed.preparation_id,room_id:window.seed.room_id,product_id:null}));
    assert.equal(await page.getByText('尚未编排播放顺序',{exact:true}).count(),1);checks++;
    assert.ok((await page.locator('#editor').textContent()).includes('尚不能检查'));checks++;
    await page.evaluate(()=>window.ui.open({view:'products',preparation_id:window.seed.preparation_id,product_id:null}));
    await page.getByLabel('本场引用 测试足弓拖鞋',{exact:true}).check();
    await page.getByRole('button',{name:'保存本场引用',exact:true}).click();await idle();await noError();
    await page.evaluate(async()=>{
      const current=await ShengboWorkbenchApi.query('read_preparation',{preparation_id:seed.preparation_id});
      const room=await ShengboWorkbenchApi.query('read_room',{room_id:seed.room_id});
      let old=await ShengboWorkbenchApi.command('create_preparation',{room_id:room.ref.id,room_revision:room.ref.revision,goal:'上一场历史研究测试'});
      old=await ShengboWorkbenchApi.command('update_preparation_products',{preparation_id:old.data.preparation_ref.id,expected_revision:old.data.preparation_ref.revision,selected_product_refs:current.body.selected_product_refs});
      const oldDraft=await ShengboWorkbenchApi.command('save_session_draft',{preparation_id:old.data.preparation_ref.id,expected_revision:old.data.preparation_ref.revision,draft:{theme:'上一场主题',commerce:{}}});
      const report={summary:'历史研究：居家使用者可能关心舒适性。',product_ids:[seed.product_id],fields:{context:{market:'中国台湾'}},insights:[{use_scene:'居家久站'}],evidence:[],hypotheses:['历史假设未验证'],conflicts:[],missing_fields:[],asset_suggestions:[],source_refs:[],dependencies:[...current.body.selected_product_refs,oldDraft.data.draft_ref]};
      const result=await ShengboWorkbenchApi.command('accept_analysis_report',{role:'audience_scenes',report,report_hash:await ShengboWorkbenchApi.hash(report),preparation_id:oldDraft.data.preparation_ref.id,expected_revision:oldDraft.data.preparation_ref.revision,product_refs:current.body.selected_product_refs,readset:[oldDraft.data.draft_ref]});
      window.historicalAnalysis=result.data.analysis_ref;
      return ui.open({view:'insights',preparation_id:seed.preparation_id,refresh:true});
    });
    assert.equal(await page.locator('[data-historical-analysis]').count(),1);checks++;
    assert.equal(await page.locator('[data-historical-analysis]').getByRole('button',{name:'让 Agent 修改此项',exact:true}).count(),0,'historical research cannot bind an old preparation artifact as a current target');checks++;
    await page.getByRole('button',{name:'复核后保存为本场建议',exact:true}).click();await idle();
    let unadopted=await page.evaluate(()=>ShengboWorkbenchApi.query('read_preparation',{preparation_id:seed.preparation_id}));
    assert.equal(unadopted.body.analysis_refs.length,0,'historical suggestions are never silently adopted');checks++;
    await page.getByLabel('已复核历史建议对本场的适用性',{exact:true}).check();
    await page.getByRole('button',{name:'复核后保存为本场建议',exact:true}).click();await idle();await noError();
    const copied=await page.evaluate(async()=>{const p=await ShengboWorkbenchApi.query('read_preparation',{preparation_id:seed.preparation_id});return ShengboWorkbenchApi.query('read_analysis_report',{ref:p.body.analysis_refs[0]});});
    assert.equal(copied.body.preparation_id,await page.evaluate(()=>seed.preparation_id));checks++;
    assert.equal(copied.body.report.fields.historical_origin.analysis_ref.id,await page.evaluate(()=>historicalAnalysis.id));checks++;
    assert.ok(copied.body.report.dependencies.every(r=>r.kind==='product'),'old preparation dependencies are explicitly archived as provenance rather than active current dependencies');checks++;
    await page.getByRole('button',{name:'让 Agent 修改此项',exact:true}).click();await idle();
    const selectedBeforeSave=await page.evaluate(()=>window.selected);
    await page.getByLabel('分析摘要',{exact:true}).fill('商家复核后补充：关注舒适与清洗，仍待验证。');
    assert.equal(await page.evaluate(()=>ui.hasDirty(selected)),true,'analysis ID matches its local dirty buffer');checks++;
    await page.evaluate(()=>ui.saveDirty(selected));
    const rebound=await page.evaluate(()=>ui.resolveSelection(selected));
    assert.notEqual(rebound.target.object_ref.id,selectedBeforeSave.target.object_ref.id,'saving a research edit rebinds to actual data.analysis_ref');checks++;
    const savedResearch=await page.evaluate(ref=>ShengboWorkbenchApi.query('read_analysis_report',{ref}),rebound.target.object_ref);
    assert.equal(savedResearch.body.report.summary,'商家复核后补充：关注舒适与清洗，仍待验证。');checks++;
    await page.evaluate(()=>window.ui.open({view:'session',preparation_id:window.seed.preparation_id,product_id:null}));
    await page.getByLabel('本场主题',{exact:true}).fill('舒适居家专场');
    await page.getByLabel('单品优惠方式 测试足弓拖鞋',{exact:true}).selectOption('price');
    await page.getByLabel('本场活动价 测试足弓拖鞋',{exact:true}).fill('107');
    await page.getByRole('button',{name:'保存本场配置',exact:true}).click();await idle();await noError();
    let state=await page.evaluate(()=>ShengboWorkbenchApi.query('get_workspace_snapshot',{preparation_id:window.seed.preparation_id}));
    assert.equal(state.preparation_details.draft.body.draft.commerce.product_overrides[windowSeed(state)].price,107);checks++;
    await page.evaluate(()=>window.ui.open({view:'insights',preparation_id:window.seed.preparation_id}));
    await page.getByLabel('人工建议',{exact:true}).fill('居家久站的女性可能关注脚感；这是商家假设，尚未验证。');
    await page.getByRole('button',{name:'保存为待验证建议',exact:true}).click();await idle();await noError();
    assert.ok((await page.locator('#editor').textContent()).includes('尚未真实验证'));checks++;
    await page.evaluate(async()=>{const p=await ShengboWorkbenchApi.query('read_preparation',{preparation_id:seed.preparation_id});await ShengboWorkbenchApi.command('save_asset_checklist',{preparation_id:seed.preparation_id,expected_revision:p.ref.revision,checklist:{items:[{item_id:'asset-on-body',product_ref:p.body.selected_product_refs[0],category:'on_body',purpose:'上脚展示',recommended_content:'补充一张商品上脚展示图片或说明文档',status:'missing',source_refs:[]}],notes:[]}});return ui.open({view:'overview',preparation_id:seed.preparation_id,refresh:true});});
    await page.getByLabel('材料文件 上脚展示',{exact:true}).setInputFiles({name:'展示说明.txt',mimeType:'text/plain',buffer:Buffer.from('隔离测试：上脚展示说明。','utf8')});
    await page.getByRole('button',{name:'上传并绑定此材料',exact:true}).click();await idle();await noError();
    await page.getByRole('button',{name:'保存材料准备状态',exact:true}).click();await idle();await noError();
    const assets=await page.evaluate(()=>ShengboWorkbenchApi.query('read_asset_checklist',{preparation_id:seed.preparation_id}));
    assert.equal(assets.body.checklist.items[0].status,'available');assert.equal(assets.body.checklist.items[0].blob_ref.kind,'blob');checks+=2;
    await page.evaluate(()=>window.ui.open({view:'session',preparation_id:window.seed.preparation_id}));
    await page.getByLabel('本次新增版本数 测试足弓拖鞋',{exact:true}).fill('1');
    await page.getByRole('button',{name:'将新增要求交给 Agent',exact:true}).click();await idle();await noError();
    assert.ok((await page.evaluate(()=>window.generationMessage)).includes('先读取并对照已有版本'));checks++;
    // Agent execution is deliberately outside this UI contract. The isolated
    // deterministic service fixture submits the same formal business plan.
    await page.evaluate(async()=>{
      const p=await ShengboWorkbenchApi.query('read_preparation',{preparation_id:seed.preparation_id});
      const context=await ShengboWorkbenchApi.command('save_context_bundle',{preparation_id:seed.preparation_id,expected_revision:p.ref.revision,product_refs:p.body.selected_product_refs,analysis_refs:p.body.analysis_refs,draft_ref:p.body.draft_ref,recap_refs:p.body.recap_refs,source_refs:p.body.source_refs,prior_script_refs:p.body.script_refs,user_decisions:[]});
      const plan=await ShengboWorkbenchApi.command('save_script_plan',{preparation_id:seed.preparation_id,expected_revision:context.data.preparation_ref.revision,bundle_ref:context.data.context_bundle_ref,items:[{product_ref:p.body.selected_product_refs[0],variant_id:'variant_'+crypto.randomUUID().replaceAll('-',''),version_number:1,modules:['opening_long','product_intro','selling_points','campaign','cta'].map(key=>({key,action:'generate',instruction:'生成完整版本，再按模块存储。'}))}],recap_dispositions:[]});
      const fresh=await ShengboWorkbenchApi.query('read_preparation',{preparation_id:seed.preparation_id});
      return ShengboWorkbenchApi.command('submit_script_change',{preparation_id:seed.preparation_id,expected_revision:fresh.ref.revision,plan_ref:plan.data.script_plan_ref,context_bundle_ref:context.data.context_bundle_ref,expected_generation_epoch:fresh.body.generation_epoch});
    });
    await waitJob('scripts');
    await page.evaluate(()=>window.ui.open({view:'scripts',preparation_id:window.seed.preparation_id,refresh:true}));
    await page.locator('[data-open-script]').first().click();await idle();
    assert.equal(await page.locator('.wb-script-reader [data-module]').count(),5);checks++;
    assert.equal(await page.locator('.wb-script-reader [data-module="transition"]').count(),0);checks++;
    assert.equal(await page.locator('.wb-script-reader textarea').count(),0);checks++;
    await page.locator('.wb-script-reader [data-select-module="campaign"]').click();await idle();
    assert.equal(await page.evaluate(()=>window.selected.target.module_key),'campaign');checks++;
    await page.getByRole('button',{name:'关闭话术详情',exact:true}).click();await idle();
    await page.getByRole('button',{name:'加入播放顺序',exact:true}).first().click();await idle();
    await page.getByRole('button',{name:'重复此播放项',exact:true}).click();await idle();
    assert.equal(await page.locator('.wb-playlist-item').count(),2);checks++;
    // A second saved position would require the missing short opening. Its
    // targeted creation/approval behavior is checked in the service suite;
    // this UI case preserves the user's one-item choice before approval.
    await page.getByRole('button',{name:'移除此播放项',exact:true}).last().click();await idle();
    await page.getByRole('button',{name:'保存顺序并检查',exact:true}).click();await idle();await noError();
    assert.equal(await page.getByRole('button',{name:'批准当前整包文本',exact:true}).count(),1);checks++;
    await page.getByRole('button',{name:'批准当前整包文本',exact:true}).click();await idle();await noError();
    await page.getByRole('button',{name:'生成本场所需语音',exact:true}).click();await idle();await noError();
    await waitJob('audio');
    await page.evaluate(()=>window.ui.refresh());
    assert.ok(await page.locator('audio').count()>0);checks++;
    await page.getByRole('button',{name:'生成本场播出候选',exact:true}).click();await idle();await noError();
    await page.getByRole('button',{name:'确认采用本场',exact:true}).click();await idle();await noError();
    state=await page.evaluate(()=>ShengboWorkbenchApi.query('get_workspace_snapshot',{preparation_id:window.seed.preparation_id}));
    const review=await page.evaluate(()=>ShengboWorkbenchApi.query('create_review',{preparation_id:window.seed.preparation_id}));
    assert.equal(review.adopted,true);checks++;
    assert.ok(state.preparation.body.candidate_publication_ref);checks++;
    assert.deepEqual(errors,[]);checks++;
    console.log(JSON.stringify({status:'passed',checks,realBusinessService:true,temporaryDatabase:true,realProvidersCalled:false}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
function windowSeed(snapshot){return snapshot.preparation.body.selected_product_refs[0].id;}
