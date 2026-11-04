'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const unit = require('./workspace-editors.js');
assert.equal(unit.safeUrl('/room-audio/room1/abc.wav'), '/room-audio/room1/abc.wav');
assert.equal(unit.safeUrl('javascript:alert(1)'), null);
assert.equal(unit.safeUrl('//external.example/audio.wav'), null);
assert.equal(unit.safeUrl('/room-audio/../secret'), null);
assert.deepEqual(unit.schemaDefault({type:'array'},{}), []);
assert.equal(unit.scriptTextMetrics({modules:{opening_long:{sentences:[{text:'欢迎，朋友！'}]},opening_short:{sentences:[{text:'看这款。'}]},cta:{sentences:[{text:'了解23元这款'}]}}},2).text,'看这款。\n了解23元这款');
assert.equal(unit.scriptTextMetrics({modules:{cta:{sentences:[{text:'甲'.repeat(1920)}]}}}).estimated_minutes,8);
assert.equal(unit.currentScriptVariants([{ref:{id:'old',revision:1},body:{product_id:'p1',variant_id:'v1',script_revision:1}},{ref:{id:'new',revision:2},body:{product_id:'p1',variant_id:'v1',script_revision:2}}])[0].ref.id,'new');
const legacyMetrics=unit.scriptTextMetrics({modules:{product_intro:{sentences:[{text:'售价23元',spoken_text:'售价二十三元'}]},transition:{sentences:[{text:'过去的衔接'.repeat(100)}]}}});
assert.deepEqual({text:legacyMetrics.text,characters:legacyMetrics.characters,playback_keys:legacyMetrics.playback_keys},{text:'售价二十三元',characters:6,playback_keys:['product_intro']},'spoken normalization and intro-only imports follow actual playback; legacy transition cannot inflate duration');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:fs.existsSync(chromium.executablePath())?chromium.executablePath():'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  let checks=9;
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.route('http://localhost:19372/**',route=>route.fulfill({contentType:'text/html',body:'<!doctype html><meta charset="utf-8"><div id="editor"></div><textarea id="chat" aria-label="聊天"></textarea>'}));
    await page.goto('http://localhost:19372/');
    await page.addStyleTag({content:['agent-workbench.css','catalog-layout.css','scripts-layout.css'].map(name=>fs.readFileSync(path.join(__dirname,name),'utf8')).join('\n')+'#editor{height:800px;width:700px;position:relative;overflow:auto}#editor:has(.wb-catalog-view){display:flex;flex-direction:column}#chat{position:fixed;right:10px;top:10px;width:250px}'});
    await page.addScriptTag({path:path.join(__dirname,'workspace-editors.js')});
    await page.evaluate(()=>{
      const ref=(kind,id,revision=1)=>({kind,id,revision,content_hash:'sha256:'+id+'-'+revision,...(['script_revision','module','draft','playlist','analysis','requirements'].includes(kind)?{schema_version:'PRELIVE-2.3'}:{})});
      const props={name:{type:'string'},sku_id:{type:'string',default:''},price:{type:'number',minimum:0},currency:{type:'string',default:'CNY',enum:['CNY','TWD','USD','AUD']},selling_points:{type:'string',default:''},core_selling_points:{type:'array',items:{type:'string'}},material:{type:'string',default:''},knowledge:{type:'string',default:''},image:{type:'string',default:''},sale_status:{type:'string',default:'available',enum:['available','unavailable']},promo_price:{anyOf:[{type:'number'},{type:'null'}],default:null},specifications:{type:'array',items:{$ref:'#/$defs/Specification'}},knowledge_documents:{type:'array',items:{$ref:'#/$defs/Document'}},hidden_optional_fields:{type:'array',items:{type:'string',enum:['material','knowledge']}}};
      window.fixture={products:[{ref:ref('product','p1'),body:{name:'拖鞋',price:178,currency:'TWD',selling_points:'足弓支撑',core_selling_points:[],material:'EVA',knowledge:'遇水小心',image:'',sale_status:'available',promo_price:null,specifications:[{name:'尺码',value:'36-45'}],knowledge_documents:[{id:'d1',title:'发货',content:'2至8天'}],hidden_optional_fields:[]}},{ref:ref('product','p2'),body:{name:'收纳盒',price:88,currency:'CNY',selling_points:'整洁收纳',core_selling_points:[],material:'PP',knowledge:'',image:'',sale_status:'available',promo_price:null,specifications:[],knowledge_documents:[],hidden_optional_fields:[]}}],room:{ref:ref('config','r1'),body:{id:'r1',name:'日用直播间',commerce:{}}},prep:{ref:ref('preparation','prep1'),body:{id:'prep1',room_id:'r1',goal:'准备拖鞋直播',status:'draft',generation_epoch:1,selected_product_refs:[ref('product','p1'),ref('product','p2')],analysis_refs:[],recap_refs:[],source_refs:[],script_refs:[ref('script_revision','s1'),ref('script_revision','s2')],draft_ref:ref('draft','draft1'),playlist_ref:null,compiled_plan_ref:null,requirements_ref:null,approval_ref:null,candidate_publication_ref:null}},draft:{ref:ref('draft','draft1'),body:{draft:{theme:'舒适居家',objective:'介绍固定商品',language:'zh-CN',commerce:{scene:'居家女性',promotion:'新品',coupon:{mode:'none',currency:'TWD',scope:'all',product_ids:[]},product_overrides:{}},tts_selection:{provider:'system',voice:'Huihui',model:'speech-2.8-turbo'},loop_mode:'infinite'}}},scripts:[{ref:ref('script_revision','s1'),body:{product_id:'p1',product_ref:ref('product','p1'),variant_id:'v1',version_number:1,script_revision:1,module_refs:{}},modules:{campaign:{sentences:[{text:'现在介绍本场活动。'}]},selling_points:{sentences:[{text:'足弓支撑，更贴合。'}]}}},{ref:ref('script_revision','s2'),body:{product_id:'p2',product_ref:ref('product','p2'),variant_id:'v2',version_number:1,script_revision:1,module_refs:{}},modules:{campaign:{sentences:[{text:'收纳好物。'}]}}}],playlist:null};
      window.selections=[];window.calls=[];window.changes=[];window.lastSnapshotArgs=[];
      const clone=v=>JSON.parse(JSON.stringify(v));
      const query=async(name,args)=>{window.calls.push({type:'query',name,args:clone(args)});const f=window.fixture;
        if(name==='get_business_schema')return{schema_hash:'sha256:schema',schema:{product:{properties:props,$defs:{Specification:{type:'object',properties:{name:{type:'string'},value:{type:'string'}}},Document:{type:'object',properties:{id:{type:'string'},title:{type:'string'},content:{type:'string'}}}}}}};
        if(name==='get_workspace_snapshot'){window.lastSnapshotArgs.push(clone(args));return clone({products:{items:f.products.map(p=>({ref:p.ref,...p.body})),next_cursor:null},rooms:[f.room],...(args.preparation_id?{preparation:f.prep,preparation_details:{draft:f.draft,scripts:f.scripts,playlist:f.playlist,requirements:null,compiled_plan:null,asset_checklist:null}}:{})});}
        if(name==='read_product')return clone(f.products.find(p=>p.ref.id===(args.product_id||args.ref.id)));
        if(name==='validate_product_assembly')return{valid:true,normalized_fields:clone(args.fields)};
        if(name==='read_preparation')return clone(f.prep);
        if(name==='read_room')return clone(f.room);
        if(name==='read_session_history')return clone({preparations:[f.prep]});
        if(name==='read_script')return clone(f.scripts.find(s=>s.ref.id===args.ref.id));
        if(name==='read_completion_observation')return{jobs:[],pending_job_ids:[]};
        if(name==='create_text_review')throw Object.assign(Error('先编排播放清单'),{code:'DEPENDENCY_NOT_READY'});
        if(name==='voice_options')return{providers:['system','minimax'],voices:{system:['Huihui'],minimax:['Wise_Woman']},models:{system:['speech-2.8-turbo'],minimax:['speech-2.8-turbo']}};
        if(name==='find_audience_research')return{items:[]};
        if(name==='read_analysis_report')return clone(f.analyses.find(item=>item.ref.id===args.ref.id));
        if(name==='read_events')return{events:[]};
        throw Error('未覆盖的测试查询 '+name);
      };
      const command=async(name,args)=>{window.calls.push({type:'command',name,args:clone(args)});const f=window.fixture;let data={};
        if(name==='save_product'){const p=f.products.find(p=>p.ref.id===args.product_id);if(p&&args.expected_revision!==p.ref.revision)throw Object.assign(Error('商品版本已变化'),{code:'REVISION_CONFLICT'});if(p){p.body=clone(args.fields);p.ref=ref('product',p.ref.id,p.ref.revision+1);data={product_ref:p.ref};}else{const p={ref:ref('product','p3'),body:clone(args.fields)};f.products.push(p);data={product_ref:p.ref};}}
        else if(name==='save_session_draft'){if(args.expected_revision!==f.prep.ref.revision)throw Object.assign(Error('本场版本已变化'),{code:'REVISION_CONFLICT'});f.draft.body.draft=clone(args.draft);f.prep.ref=ref('preparation','prep1',f.prep.ref.revision+1);data={preparation_ref:f.prep.ref,draft_ref:f.draft.ref};}
        else if(name==='patch_script'){const s=f.scripts.find(s=>s.ref.id===args.script_ref.id);if(!s)throw Error('稿件引用不属于本场');const old=s.ref;s.ref=ref('script_revision',s.ref.id+'n');s.modules[args.module_key].sentences=args.sentences.map(text=>({text}));f.prep.body.script_refs=f.scripts.map(v=>v.ref);f.prep.ref=ref('preparation','prep1',f.prep.ref.revision+1);data={preparation_ref:f.prep.ref,replacement:{old_ref:old,new_ref:s.ref}};}
        else if(name==='save_playlist'){f.playlist={ref:ref('playlist','list1'),body:clone(args)};f.prep.ref=ref('preparation','prep1',f.prep.ref.revision+1);data={preparation_ref:f.prep.ref,playlist_ref:f.playlist.ref};}
        else if(name==='compile_playback_plan'){data={preparation_ref:f.prep.ref};}
        else if(name==='update_preparation_products'){f.prep.body.selected_product_refs=clone(args.selected_product_refs);f.prep.ref=ref('preparation','prep1',f.prep.ref.revision+1);data={preparation_ref:f.prep.ref};}
        else throw Error('未覆盖的测试命令 '+name);
        return clone({command:name,data,status:'committed'});
      };
      const request=async(name,args)=>{
        window.calls.push({type:'request',name,args:clone(args)});
        if(name!=='/api/prelive/v2/human/script-delete')return {};
        const f=window.fixture;
        const previousCount=f.playlist.body.items.length;
        f.scripts=f.scripts.filter(s=>s.ref.id!==args.script_ref.id);
        f.prep.body.script_refs=f.scripts.map(s=>s.ref);
        f.playlist.body.items=f.playlist.body.items.filter(item=>item.script_revision_ref.id!==args.script_ref.id);
        f.playlist.ref=ref('playlist','list1',f.playlist.ref.revision+1);
        f.prep.ref=ref('preparation','prep1',f.prep.ref.revision+1);
        return {preparation_ref:f.prep.ref,playlist_ref:f.playlist.ref,deleted_script_ref:args.script_ref,removed_occurrence_count:previousCount-f.playlist.body.items.length};
      };
      window.ShengboWorkbenchApi={query,command,request,hash:async v=>'sha256:'+JSON.stringify(v).length,uuid:()=>crypto.randomUUID()};
      window.mountEditor=()=>{window.ui=ShengboWorkspaceEditors.mount({
        container:document.getElementById('editor'),
        onSelect:s=>window.selections.push(s),
        onNavigate:d=>window.ui.open(d),
        onChange:c=>window.changes.push(c),
        onUploadProductMaterials:async ({files})=>({
          materialRefs:files.map(file=>ref('source_manifest','source-'+file.name)),
          materials:files.map(file=>({name:file.name}))
        }),
        onSubmitProductMaterials:async result=>{window.productSubmission=result;return {status:'accepted'};},
        onPrepareGeneration:text=>window.generationMessage=text
      });};window.mountEditor();
    });
    const idle=()=>page.waitForFunction(()=>!document.querySelector('#editor').hasAttribute('aria-busy'));
    await page.evaluate(()=>window.ui.open({view:'products',product_id:'p1',preparation_id:null}));
    assert.equal(await page.locator('[data-field="name"]').first().inputValue(),'拖鞋');checks++;
    assert.equal(await page.locator('[data-field="price"]').inputValue(),'178');checks++;
    assert.equal(await page.locator('[data-field="knowledge_documents"] [data-field="title"]').inputValue(),'发货');checks++;
    await page.locator('[data-field="price"]').click();
    assert.equal(await page.evaluate(()=>window.selections.length),0,'ordinary field focus does not bind Agent target');checks++;
    await page.locator('[data-field="price"]').fill('168');
    await page.evaluate(()=>{window.originalPriceNode=document.querySelector('[data-field="price"]');return ui.open({view:'products',product_id:'p1',refresh:true});});
    assert.equal(await page.evaluate(()=>window.originalPriceNode===document.querySelector('[data-field="price"]')),true,'artifact refresh cannot recreate a dirty editor');checks++;
    assert.ok((await page.locator('.wb-product-dialog [role=status]').textContent()).includes('未保存编辑'));checks++;
    await page.locator('[data-select-field="price"]').click();await idle();
    const chosen=await page.evaluate(()=>window.selections.at(-1));
    assert.equal(chosen.target.object_ref.id,'p1');assert.equal(chosen.target.field_path,'/price');assert.equal(chosen.dirty,true);checks+=3;
    assert.equal(await page.evaluate(()=>window.ui.hasDirty()),true);checks++;
    await page.evaluate(()=>window.ui.refresh());
    assert.equal(await page.locator('[data-field="price"]').inputValue(),'168','refresh preserves dirty input');checks++;
    const productDialog=page.locator('[data-catalog-dialog="detail"]');
    await productDialog.getByRole('button',{name:'关闭',exact:true}).click();await idle();
    await productDialog.getByRole('button',{name:'保留草稿并关闭',exact:true}).click();await idle();
    await page.evaluate(()=>window.ui.open({view:'products',product_id:'p2'}));
    await page.evaluate(()=>window.ui.saveDirty());
    let save=await page.evaluate(()=>window.calls.filter(c=>c.name==='save_product').at(-1));
    assert.equal(save.args.product_id,'p1','save buffered form keeps its original product');assert.equal(save.args.fields.price,168);assert.equal(save.args.expected_revision,1);checks+=3;
    await page.evaluate(()=>window.ui.open({view:'products',product_id:'p1'}));
    await page.locator('[data-field="price"]').fill('158');
    await page.evaluate(()=>{window.fixture.products[0].ref.revision=3;window.fixture.products[0].ref.content_hash='sha256:p1-3';window.fixture.products[0].body.material='新版 EVA';});
    await page.getByRole('button',{name:'保存修改',exact:true}).click();await idle();
    assert.equal(await page.locator('[data-field="price"]').inputValue(),'158','CAS conflict keeps original input');checks++;
    assert.equal(await page.getByText('商品版本冲突',{exact:true}).count(),1);checks++;
    await page.getByRole('button',{name:'基于新版重新应用我的修改'}).click();await idle();
    await page.getByRole('button',{name:'保存修改',exact:true}).click();await idle();
    save=await page.evaluate(()=>window.calls.filter(c=>c.name==='save_product').at(-1));
    assert.equal(save.args.fields.material,'新版 EVA','explicit rebase preserves unrelated newer fields');assert.equal(save.args.expected_revision,3);checks+=2;
    await page.evaluate(()=>window.ui.open({view:'products',product_id:'p1'}));
    await page.locator('[data-field="core_selling_points"] textarea').first().fill('新的卖点，尚未保存');
    await page.evaluate(()=>{window.ui.dispose();window.mountEditor();return window.ui.open({view:'products',product_id:'p1',preparation_id:null});});
    assert.notEqual(await page.locator('[data-field="core_selling_points"] textarea').first().inputValue(),'新的卖点，尚未保存','local cache is not auto-applied');checks++;
    await page.getByRole('button',{name:'恢复本机未保存内容'}).click();await idle();
    assert.equal(await page.locator('[data-field="core_selling_points"] textarea').first().inputValue(),'新的卖点，尚未保存');checks++;
    await page.evaluate(()=>window.ui.discardDirty());
    await page.evaluate(()=>window.ui.open({view:'scripts',product_id:null,preparation_id:'prep1',room_id:'r1'}));
    assert.equal(await page.getByRole('button',{name:'生成新版本话术',exact:true}).count(),0,'existing scripts have no creation form');checks++;
    assert.equal(await page.locator('.wb-available-scripts').count(),0,'versions and order share a single list');
    assert.equal(await page.locator('[data-occurrence]').count(),2,'legacy playable versions immediately appear in order');checks+=2;
    await page.evaluate(()=>ui.setEditorLocks([{run_id:'working-agent',status:'RUNNING',targets:[{kind:'playlist',id:'prep1'}]}]));
    assert.equal(await page.locator('[data-script-id="s1"] [data-open-script]').isEnabled(),true,'Agent locks keep safe read-only variant access');assert.equal(await page.locator('[data-script-id="s1"]').getByRole('button',{name:'下移',exact:true}).isDisabled(),true,'Agent locks still prohibit queue mutations');checks+=2;
    const readerBackScroll=await page.evaluate(()=>{const editor=document.getElementById('editor');editor.style.height='300px';editor.scrollTop=120;return editor.scrollTop;});
    assert.ok(readerBackScroll>0,'regression begins with a genuinely scrolled version list');checks++;
    await page.locator('[data-script-id="s1"] [data-open-script]').evaluate(element=>element.click());await idle();
    const readerPosition=await page.evaluate(()=>{const editor=document.getElementById('editor'),reader=document.querySelector('.wb-script-reader');return{scrollTop:editor.scrollTop,top:reader.getBoundingClientRect().top-editor.getBoundingClientRect().top};});
    assert.equal(readerPosition.scrollTop,0,'reader opening resets the scrollable container');assert.ok(readerPosition.top>=-1&&readerPosition.top<=8,'dialog is visually inside the left viewport: '+JSON.stringify(readerPosition));checks+=2;
    assert.equal(await page.getByRole('dialog',{name:'话术版本详情'}).getAttribute('aria-modal'),'false');checks++;
    assert.equal(await page.locator('.wb-script-reader textarea:visible').count(),0,'reading a script does not open sentence editors');assert.equal(await page.getByRole('button',{name:'保存此模块',exact:true}).count(),0);checks+=2;
    await page.locator('.wb-script-reader [data-select-module="campaign"]').click();await idle();
    const moduleSelection=await page.evaluate(()=>window.selections.at(-1));
    assert.equal(moduleSelection.target.module_key,'campaign');assert.equal(moduleSelection.target.object_ref.id,'s1');assert.equal(moduleSelection.target.product_ref.id,'p1');checks+=3;
    await page.locator('#chat').fill('只修改这一个模块的表达');assert.equal(await page.locator('#chat').inputValue(),'只修改这一个模块的表达','left reader does not block right chat');checks++;
    await page.getByRole('button',{name:'下一版本',exact:true}).click();await idle();
    assert.equal(await page.evaluate(()=>document.getElementById('editor').scrollTop),0,'switching variants keeps the reader visible');checks++;
    assert.ok((await page.locator('.wb-script-reader-heading').textContent()).includes('收纳盒'));assert.ok((await page.locator('.wb-script-reader-body').textContent()).includes('收纳好物'));checks+=2;
    await page.getByRole('button',{name:'上一版本',exact:true}).click();await idle();
    await page.locator('.wb-script-reader [data-select-module="selling_points"]').click();await idle();
    assert.equal(await page.evaluate(()=>window.selections.at(-1).target.module_key),'selling_points');checks++;
    await page.keyboard.press('Escape');assert.equal(await page.locator('.wb-script-reader').count(),0);checks++;
    assert.equal(await page.evaluate(()=>document.getElementById('editor').scrollTop),readerBackScroll,'closing restores the original list scroll position');
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.openScript),'s1','closing restores focus without changing list scroll');checks+=2;
    await page.evaluate(()=>document.getElementById('editor').style.height='800px');
    await page.evaluate(()=>ui.setEditorLocks([]));
    assert.equal(await page.evaluate(()=>window.ui.hasDirty()),false,'read-only module inspection never creates a patch buffer');checks++;
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.name==='patch_script').length),0);checks++;
    await page.evaluate(()=>window.ui.open({view:'playlist',product_id:null,preparation_id:'prep1'}));
    assert.equal(await page.evaluate(()=>window.ui.getContext().view),'scripts','legacy playlist and scripts destinations share one view');checks++;
    await page.getByRole('button',{name:'重复此播放项'}).first().click();await idle();
    const occurrences=await page.locator('[data-occurrence]').evaluateAll(ns=>ns.map(n=>n.dataset.occurrence));
    assert.equal(occurrences.length,3);assert.equal(new Set(occurrences).size,3,'repeats have independent stable occurrence IDs');checks+=2;
    await page.locator('[data-occurrence]').first().dragTo(page.locator('[data-occurrence]').last());await idle();
    assert.equal(await page.locator('[data-occurrence]').last().getAttribute('data-occurrence'),occurrences[0],'drag moves a complete product/version item');assert.equal(await page.locator('[data-module][draggable=true]').count(),0,'modules are never draggable');checks+=2;
    await page.getByRole('button',{name:'商品穿插',exact:true}).click();await idle();
    const names=await page.locator('[data-occurrence] strong').allTextContents();assert.ok(names[1].includes('收纳盒'),'interleaving alternates product groups');checks++;
    await page.getByRole('button',{name:'保存顺序并检查'}).click();await idle();
    const playlist=await page.evaluate(()=>window.calls.filter(c=>c.name==='save_playlist').at(-1));
    assert.equal(playlist.args.items.length,3);assert.equal(playlist.args.items[1].product_id,'p2');checks+=2;
    await page.evaluate(()=>window.ui.open({view:'session',product_id:null,preparation_id:'prep1',room_id:'r1'}));
    await page.evaluate(()=>ui.setEditorLocks([{run_id:'generating-agent',status:'RUNNING',targets:[{kind:'scripts',id:'prep1'}]}]));
    assert.equal(await page.getByRole('button',{name:'新增话术版本',exact:true}).isEnabled(),true,'script generation lock preserves read-only navigation');
    assert.equal(await page.locator('[data-script-generation]').count(),0,'configuration links to the generator without a second form');
    await page.getByRole('button',{name:'新增话术版本',exact:true}).click();await idle();
    assert.equal(await page.evaluate(()=>ui.getContext().view),'scripts','generation opens from configuration in the script workspace');
    assert.equal(await page.getByRole('button',{name:'将新增要求交给 Agent',exact:true}).isDisabled(),true,'generation itself remains locked');
    assert.equal(await page.getByLabel('本次新增版本数 拖鞋',{exact:true}).isDisabled(),true,'generation inputs remain locked');checks+=5;
    await page.evaluate(()=>ui.setEditorLocks([]));
    await page.getByRole('button',{name:'新增话术版本',exact:true}).click();await idle();
    assert.equal(await page.locator('[data-script-generation]').isVisible(),false,'the script list stays visible when the generation form is collapsed');checks++;
    await page.getByRole('button',{name:'新增话术版本',exact:true}).click();await idle();
    await page.getByLabel('本次新增版本数 拖鞋',{exact:true}).fill('2');
    await page.getByLabel('为此商品新增话术 收纳盒',{exact:true}).uncheck();
    assert.equal(await page.locator('[data-script-generation]').count(),1,'the script workspace contains a single generation form');checks++;
    const commandsBeforeGeneration=await page.evaluate(()=>window.calls.filter(c=>c.type==='command').length);
    await page.getByRole('button',{name:'将新增要求交给 Agent',exact:true}).click();await idle();
    const generation=await page.evaluate(()=>window.generationMessage);
    assert.ok(generation.includes('8–10分钟')&&generation.includes('1920–2400字'));assert.ok(generation.includes('先读取并对照已有版本'));assert.ok(generation.includes('"id": "s1"')&&generation.includes('"count": 2'));checks+=3;
    assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.type==='command').length),commandsBeforeGeneration,'preparing an Agent task does not silently execute business writes');checks++;
    await page.evaluate(()=>window.ui.open({view:'products',product_id:null,preparation_id:null,room_id:null,action:null}));
    assert.equal(await page.evaluate(()=>window.ui.getContext().scope.kind),'catalog');assert.equal(await page.evaluate(()=>window.lastSnapshotArgs.at(-1).preparation_id),undefined,'explicit null does not retain prior preparation');checks+=2;
    const materialFlow=page.locator('[data-product-material-flow="create"]');
    await page.getByRole('button',{name:'上传资料新增商品',exact:true}).click();await idle();
    await materialFlow.getByLabel('上传资料新增商品',{exact:true}).setInputFiles({name:'商品补充资料.txt',mimeType:'text/plain',buffer:Buffer.from('测试商品资料：简体中文。','utf8')});
    await materialFlow.getByRole('button',{name:'上传并加入对话',exact:true}).click();await idle();
    assert.ok((await materialFlow.textContent()).includes('商品补充资料.txt'));checks++;
    assert.equal(await page.evaluate(()=>window.productSubmission),undefined);checks++;
    await materialFlow.getByRole('button',{name:'交给 Agent 新增商品',exact:true}).click();await idle();
    const submission=await page.evaluate(()=>window.productSubmission);
    assert.equal(submission.product,null);
    assert.deepEqual(submission.batch.materialRefs.map(ref=>ref.id),['source-商品补充资料.txt']);checks++;
    await page.locator('[data-catalog-dialog="upload"]').getByRole('button',{name:'关闭',exact:true}).click();await idle();
    await page.evaluate(()=>ui.open({view:'scripts',preparation_id:'prep1',resource_id:'s2',module_key:'campaign'}));
    assert.ok((await page.locator('.wb-script-reader-heading').textContent()).includes('收纳盒'),'artifact location opens the exact version without ordering confirmation');checks++;
    await page.getByRole('button',{name:'关闭话术详情',exact:true}).click();
    await page.evaluate(()=>ui.open({view:'playlist',preparation_id:'prep1'}));
    await page.evaluate(()=>ui.open({view:'products',product_id:null,preparation_id:null,room_id:null,action:null}));
    assert.equal(await page.evaluate(()=>ui.getContext().preparation_id),null,'cached catalog editor does not inherit another view preparation');checks++;
    await page.evaluate(()=>{
      const prep=fixture.prep;
      fixture.analyses=['first','saved'].map(id=>({
        ref:{kind:'analysis',id,revision:1,content_hash:'sha256:'+id},
        body:{role:'audience_scenes',accepted:true,preparation_id:prep.ref.id,report:{summary:id,hypotheses:[]}}
      }));
      prep.body.analysis_refs=fixture.analyses.map(item=>item.ref);
      window.savedInsight={view:'insights',preparation_id:prep.ref.id,room_id:prep.body.room_id,product_id:null,object_ref:fixture.analyses[1].ref};
      document.getElementById('chat').focus();
      return ui.open({...savedInsight,preserve_focus:true});
    });
    assert.equal(await page.locator('.wb-located[data-analysis-id="saved"]').getByLabel('分析摘要').inputValue(),'saved','result opens the exact accepted research card');checks++;
    assert.equal(await page.evaluate(()=>document.activeElement.id),'chat','automatic result navigation leaves chat focus unchanged');checks++;
    await page.evaluate(()=>ui.open({...savedInsight,object_ref:{...savedInsight.object_ref,content_hash:'sha256:other'}}));
    assert.equal(await page.locator('.wb-located[data-analysis-id]').count(),0,'a different artifact hash cannot mark an unrelated card');checks++;
    assert.match(await page.locator('#editor > [role=status]').textContent(),/找不到这份已保存的顾客与场景建议/);checks++;
    await page.evaluate(()=>ui.open({view:'scripts',preparation_id:'prep1',room_id:'r1',product_id:null,object_ref:fixture.playlist.ref}));
    assert.equal(await page.locator('.wb-located[data-result-section="playlist"]').count(),1,'playlist result highlights the saved order section');checks++;
    const deleteVersion=page.locator('[data-delete-script="s2"]');
    const deleteConfirm=page.getByRole('region',{name:'删除话术版本确认'});
    const deletePath='/api/prelive/v2/human/script-delete';
    const expectedDelete=await page.evaluate(()=>({revision:fixture.prep.ref.revision,script:fixture.scripts.find(s=>s.ref.id==='s2').ref}));
    await deleteVersion.click();await idle();
    assert.match(await deleteConfirm.textContent(),/收纳盒.+所有播放位置/);checks++;
    await deleteConfirm.getByRole('button',{name:'取消',exact:true}).click();await idle();
    assert.equal(await page.evaluate(name=>calls.filter(c=>c.name===name).length,deletePath),0,'cancelling never mutates the version');checks++;
    await deleteVersion.click();await idle();
    await deleteConfirm.getByRole('button',{name:'确认删除',exact:true}).click();await idle();
    const deletion=await page.evaluate(name=>calls.find(c=>c.name===name).args,deletePath);
    assert.equal(deletion.expected_revision,expectedDelete.revision);
    assert.deepEqual(deletion.script_ref,expectedDelete.script);
    assert.ok(deletion.operation_id,'version deletion carries an idempotency token');
    assert.deepEqual(await page.locator('[data-occurrence]').evaluateAll(rows=>rows.map(row=>row.dataset.scriptId)),['s1','s1'],'deleting one version preserves other repeat occurrences');checks+=4;
    await page.evaluate(()=>{
      document.getElementById('editor').style.height='405px';
      for(let index=1;index<=24;index++){
        const id='inventory-'+index;
        fixture.products.push({ref:{kind:'product',id,revision:1,content_hash:'sha256:'+id},body:{...fixture.products[1].body,name:'库存商品 '+index,sku_id:'SKU-'+index,price:100+index,
          selling_point_preview:'可堆叠收纳',selling_points_preview:['可堆叠收纳','透明可视','方便归类'],
          promotion_preview:'满两件送隔板',shipping_time_preview:'付款后 48 小时内发货',
          sales_platform:'douyin',specification_count:2,knowledge_document_count:1,
          knowledge_documents:[{id:'source-doc',title:'收纳说明',content:'叠放前清理外壳。'}],
          thumbnail_url:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aT9sAAAAASUVORK5CYII='
        }});
      }
      return ui.open({view:'products',product_id:null,preparation_id:null,room_id:null,refresh:true});
    });
    const rows=page.locator('[data-catalog-product-id]');
    const firstId=await rows.first().getAttribute('data-catalog-product-id');
    const smallCount=await rows.count();
    assert.ok(smallCount>=1&&smallCount<26,'visible rows fit the available area');checks++;
    assert.equal(await page.evaluate(()=>{const editor=document.getElementById('editor');return editor.scrollHeight<=editor.clientHeight+1;}),true,'the catalog needs no vertical editor scroll at an ordinary size');checks++;
    await page.getByRole('button',{name:'第 2 页',exact:true}).click();await idle();
    assert.notEqual(await rows.first().getAttribute('data-catalog-product-id'),firstId);checks++;
    assert.equal(await page.getByRole('button',{name:'第 2 页',exact:true}).getAttribute('aria-current'),'page');checks++;
    await page.evaluate(()=>document.getElementById('editor').style.height='620px');
    await page.waitForFunction(count=>document.querySelectorAll('[data-catalog-product-id]').length>count,smallCount);checks++;
    const search=page.getByLabel('搜索商品',{exact:true});
    await search.fill('SKU-24');
    assert.equal(await rows.count(),1);checks++;
    assert.equal(await rows.first().locator('.wb-catalog-thumbnail img').count(),1,'safe local thumbnail renders');checks++;
    assert.match(await rows.first().locator('.wb-catalog-product-info').textContent(),/抖音.*2 项规格/);checks++;
    assert.deepEqual(await rows.first().locator('.wb-catalog-selling-point').allTextContents(),['可堆叠收纳','透明可视','方便归类']);checks++;
    assert.deepEqual(await rows.first().locator('.wb-catalog-fulfillment dd').allTextContents(),['满两件送隔板','付款后 48 小时内发货']);checks++;
    assert.deepEqual(await page.locator('.wb-catalog-columns > span:visible').allTextContents(),['商品与资料','日常售价','在售状态','操作']);checks++;
    assert.equal(await rows.first().evaluate(row=>row.querySelector('.wb-catalog-price').getBoundingClientRect().right<row.querySelector('.wb-catalog-status').getBoundingClientRect().left),true,'price and status occupy different columns');checks++;
    await page.evaluate(()=>document.getElementById('editor').style.width='900px');
    assert.deepEqual(await page.locator('.wb-catalog-columns > span:visible').allTextContents(),['商品与资料','促销 / 物流','日常售价','在售状态','操作']);checks++;
    const wideCatalog=await rows.first().evaluate(row=>{
      const bounds=selector=>row.querySelector(selector).getBoundingClientRect();
      return {height:row.getBoundingClientRect().height,productRight:bounds('.wb-catalog-product-text').right,
        logisticsLeft:bounds('.wb-catalog-fulfillment').left,logisticsRight:bounds('.wb-catalog-fulfillment').right,
        priceLeft:bounds('.wb-catalog-price').left,overflow:row.scrollHeight>row.clientHeight+1||row.scrollWidth>row.clientWidth};
    });
    assert.ok(wideCatalog.height>=90&&wideCatalog.height<=101,'wide product rows are compact');checks++;
    assert.ok(wideCatalog.productRight<wideCatalog.logisticsLeft&&wideCatalog.logisticsRight<wideCatalog.priceLeft,'fulfillment sits between product data and price');checks++;
    assert.equal(wideCatalog.overflow,false);checks++;
    await page.evaluate(()=>document.getElementById('editor').style.width='390px');
    assert.equal(await rows.first().locator('.wb-catalog-price .wb-catalog-cell-label').isVisible(),true);checks++;
    assert.equal(await rows.first().locator('.wb-catalog-status .wb-catalog-cell-label').isVisible(),true);checks++;
    assert.equal(await rows.first().evaluate(row=>row.scrollWidth<=row.clientWidth&&row.scrollHeight<=row.clientHeight+1),true,'narrow list rows fit every action and detail');checks++;
    await page.evaluate(()=>document.getElementById('editor').style.width='700px');
    await rows.first().getByRole('button',{name:'编辑',exact:true}).click();await idle();
    assert.equal(await productDialog.getByLabel('商品名称',{exact:true}).inputValue(),'库存商品 24');checks++;
    assert.equal(await page.evaluate(()=>ui.getContext().product_label),'库存商品 24');checks++;
    assert.equal(await productDialog.locator('[data-product-material-flow]').count(),0,'existing product initially uses only the product form');checks++;
    await productDialog.getByRole('button',{name:'上传资料更新此商品',exact:true}).click();await idle();
    assert.equal(await productDialog.locator('[data-product-material-flow="update"]').isVisible(),true);checks++;
    assert.equal(await productDialog.locator('.wb-product-form').isVisible(),false);checks++;
    await productDialog.getByRole('button',{name:'返回编辑',exact:true}).click();await idle();
    assert.equal(await productDialog.getByLabel('商品名称',{exact:true}).inputValue(),'库存商品 24');checks++;
    await productDialog.getByRole('button',{name:'关闭',exact:true}).click();await idle();
    assert.equal(await search.inputValue(),'SKU-24','closing a product preserves its catalog search');checks++;
    await page.evaluate(()=>ui.open({view:'products',product_id:'p1',preparation_id:null,room_id:null,object_ref:fixture.products[0].ref,field_path:'/knowledge_documents',knowledge_document_id:'d1'}));
    assert.equal(await productDialog.locator('[data-product-document-id="d1"].wb-located').count(),1,'Agent result opens the exact document within the product dialog');checks++;
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.field),'content');checks++;
    assert.equal(await page.evaluate(()=>{const field=document.activeElement;const fieldBounds=field.getBoundingClientRect();const scrollBounds=field.closest('.wb-product-dialog-scroll').getBoundingClientRect();return fieldBounds.bottom>scrollBounds.top&&fieldBounds.top<scrollBounds.bottom;}),true,'exact document is scrolled into the detail viewport');checks++;
    await productDialog.getByRole('button',{name:'关闭',exact:true}).click();await idle();
    assert.equal(await search.inputValue(),'SKU-24','Agent result leaves the prior search intact');checks++;
    const commandsBeforeCopy=await page.evaluate(()=>calls.filter(call=>call.type==='command').length);
    await rows.first().getByRole('button',{name:'复制',exact:true}).click();await idle();
    const copied=page.getByRole('dialog',{name:'复制为新商品',exact:true});
    assert.equal(await copied.getByLabel('商品名称',{exact:true}).inputValue(),'库存商品 24（副本）');checks++;
    assert.equal(await copied.locator('[data-field="sku_id"]').inputValue(),'','copy clears the source SKU');checks++;
    assert.equal(await copied.locator('[data-field="price"]').inputValue(),'124');checks++;
    assert.equal(await page.evaluate(()=>calls.filter(call=>call.type==='command').length),commandsBeforeCopy,'opening a copy makes no business write');checks++;
    await copied.getByRole('button',{name:'保存商品',exact:true}).click();await idle();
    assert.equal(await copied.count(),0);checks++;
    const copySave=await page.evaluate(()=>calls.filter(call=>call.name==='save_product').at(-1).args);
    assert.equal(copySave.product_id,null);assert.equal(copySave.fields.sku_id,'');assert.notEqual(copySave.fields.knowledge_documents[0].id,'source-doc');checks+=3;
    assert.match(await page.locator('[data-catalog-product-id="p3"]').textContent(),/库存商品 24（副本）/);checks++;
    await search.fill('SKU-24');
    await rows.first().getByRole('button',{name:'删除',exact:true}).click();await idle();
    const archive=page.getByRole('dialog',{name:'删除商品',exact:true});
    assert.match(await archive.innerText(),/库存商品 24.*历史商品资料和话术会保留/);checks++;
    await archive.getByRole('button',{name:'取消',exact:true}).click();await idle();
    assert.equal(await rows.count(),1,'canceling the removal leaves the list unchanged');checks++;
    assert.deepEqual(errors,[]);checks++;
    console.log(JSON.stringify({status:'passed',checks,realProvidersCalled:false}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
