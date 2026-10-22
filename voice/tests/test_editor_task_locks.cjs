/* Actual browser consumer: private fixture only, no providers or production data. */
'use strict';
const assert=require('node:assert/strict');
const {chromium}=require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fixture=require('./test_agent_workbench.cjs');
async function main(){
  const state=fixture.resetState(),results=[];state.editorLocks=[];
  await new Promise(resolve=>fixture.server.listen(0,'127.0.0.1',resolve));
  let browser;try{browser=await chromium.launch({headless:true});}catch{browser=await chromium.launch({channel:'msedge',headless:true});}const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const check=async(name,fn)=>{await fn();results.push(name);};
  const locks=async targets=>{state.editorLocks=targets.length?[{run_id:'historical-run',status:'RUNNING',targets}]:[];await page.evaluate(()=>shengboWorkbench.refreshEditorLocks());};
  const nav=async d=>page.evaluate(d=>shengboWorkbench.navigate(d),d);
  try{
    await page.goto('http://127.0.0.1:'+fixture.server.address().port);await page.evaluate(()=>shengboWorkbench.ready);
    await nav({view:'products',product_id:'product-A',preparation_id:null,room_id:null});
    await check('精确字段锁不重建DOM，保留输入与无关字段可编辑',async()=>{
      await page.getByLabel('商品名称',{exact:true}).fill('我的未保存商品名');
      await page.evaluate(()=>window.originalPrice=document.querySelector('[data-field=price]'));
      await locks([{kind:'product',id:'product-A',field_paths:['/price']}]);
      assert.equal(await page.locator('[data-field=price]').isDisabled(),true);
      assert.equal(await page.locator('[data-field=name]').isDisabled(),false);
      assert.equal(await page.locator('[data-field=name]').inputValue(),'我的未保存商品名');
      assert.equal(await page.evaluate(()=>originalPrice===document.querySelector('[data-field=price]')),true);
      assert.equal(await page.getByRole('button',{name:'保存商品',exact:true}).isDisabled(),true);
    });
    await check('另一个商品仍可编辑，历史会话锁不依赖右侧当前会话',async()=>{
      await nav({view:'products',product_id:'product-B',preparation_id:null});
      assert.equal(await page.locator('[data-field=price]').isDisabled(),false);
      await nav({view:'products',product_id:'product-A',preparation_id:null});
      assert.equal(await page.locator('[data-field=price]').isDisabled(),true);
    });
    await check('取消排空期间保持锁；终态释放时保留原disabled及草稿',async()=>{
      state.editorLocks[0].status='CANCELLING';await page.evaluate(()=>shengboWorkbench.refreshEditorLocks());
      assert.equal(await page.locator('[data-field=price]').isDisabled(),true);
      await locks([]);assert.equal(await page.locator('[data-field=price]').isDisabled(),false);
      await page.evaluate(()=>document.querySelector('[data-field=name]').disabled=true);
      await locks([{kind:'product',id:'product-A'}]);await locks([]);
      assert.equal(await page.locator('[data-field=name]').isDisabled(),true);
      assert.equal(await page.locator('[data-field=price]').isDisabled(),false);
      assert.equal(await page.locator('[data-field=name]').inputValue(),'我的未保存商品名');
      await page.evaluate(()=>document.querySelector('[data-field=name]').disabled=false);
    });
    await check('刷新后仍从全局持久锁检测相同对象',async()=>{
      await locks([{kind:'product',id:'product-A',field_paths:['/price']}]);
      await page.reload();await page.evaluate(()=>shengboWorkbench.ready);await page.evaluate(()=>shengboWorkbench.refreshEditorLocks());
      assert.equal(await page.locator('[data-field=price]').isDisabled(),true);
      await locks([]);
    });
    await check('脚本仅锁指定版本及模块，其他模块保持编辑',async()=>{
      await nav({view:'scripts',preparation_id:'prep-A',room_id:'room-A',product_id:null});
      await locks([{kind:'scripts',id:'prep-A',script_id:'script-A1',module_keys:['campaign']}]);
      assert.equal(await page.locator('[data-module=campaign]').isDisabled(),true);
      assert.equal(await page.locator('[data-module=selling_points]').isDisabled(),false);
      await locks([]);
    });
    await check('本场字段锁不影响活动/声音其他配置',async()=>{
      await nav({view:'session',preparation_id:'prep-A',room_id:'room-A'});
      await locks([{kind:'session',id:'prep-A',field_paths:['/draft/theme']}]);
      assert.equal(await page.getByLabel('本场主题',{exact:true}).isDisabled(),true);
      assert.equal(await page.getByLabel('本场目标',{exact:true}).isDisabled(),false);
      assert.equal(await page.getByLabel('语音供应商',{exact:true}).isDisabled(),false);
      assert.equal(await page.getByRole('button',{name:'保存本场配置',exact:true}).isDisabled(),true);
      await locks([]);
    });
    await check('循环规则固定，没有单轮结束选项',async()=>{
      assert.equal(await page.getByRole('option',{name:'只播一轮'}).count(),0);
      await nav({view:'playlist',preparation_id:'prep-A'});
      assert.equal(await page.getByRole('option',{name:'只播一轮'}).count(),0);
      assert.match(await page.locator('#workspace-editor').textContent(),/从头循环/);
    });
    await check('播放项拖动在连续轮询中保持锁定，解除后恢复可拖动',async()=>{
      const target={kind:'playlist',id:'prep-A'};
      await locks([target]);assert.equal(await page.locator('[data-occurrence]').first().getAttribute('draggable'),'false');
      await page.evaluate(()=>shengboWorkbench.refreshEditorLocks());assert.equal(await page.locator('[data-occurrence]').first().getAttribute('draggable'),'false');
      await locks([]);assert.equal(await page.locator('[data-occurrence]').first().getAttribute('draggable'),'true');
    });
    await check('补充资料整理要求保留未发送聊天内容且不重复追加',async()=>{
      await nav({view:'products',product_id:null,preparation_id:null,room_id:null});
      await page.locator('.sb-a-input').fill('这是我正在写的原始要求');
      await page.getByRole('button',{name:'请 Agent 整理这些资料',exact:true}).click();
      await page.getByRole('button',{name:'请 Agent 整理这些资料',exact:true}).click();
      const text=await page.locator('.sb-a-input').inputValue();
      assert.ok(text.startsWith('这是我正在写的原始要求'));
      assert.equal(text.split('请分析我上传的资料').length,2);
    });
    await check('变更记录请求本场与catalog最新记录并使用游标加载更多',async()=>{
      const queries=[];
      await page.route('**/api/prelive/v2/queries',async route=>{const body=route.request().postDataJSON();if(body.query!=='read_events')return route.continue();queries.push(body.args);const start=body.args.cursor||501,events=Array.from({length:50},(_,i)=>({event_id:'business-'+(start-i-1),sequence:start-i-1,type:'change-'+(start-i-1),stream_id:'prep-A'}));await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({events,next_cursor:events.at(-1).sequence,has_more:queries.length<2})});});
      await nav({view:'changes',preparation_id:'prep-A',room_id:'room-A'});
      assert.equal(await page.locator('.wb-change-list details').count(),50);
      assert.match(await page.locator('.wb-change-list details').first().textContent(),/change-500/);
      await page.getByRole('button',{name:'加载更早记录',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.wb-change-list details').length===100);
      assert.deepEqual(queries.map(q=>({scope_id:q.scope_id,include_catalog:q.include_catalog,order:q.order,cursor:q.cursor,limit:q.limit})),[{scope_id:'prep-A',include_catalog:true,order:'desc',cursor:0,limit:50},{scope_id:'prep-A',include_catalog:true,order:'desc',cursor:451,limit:50}]);
      assert.equal(await page.getByRole('button',{name:'加载更早记录',exact:true}).count(),0);
      await page.unroute('**/api/prelive/v2/queries');
    });
    await check('试听结果不明时保留原始操作ID，仅核实而不重复生成',async()=>{
      const previews=[],receipts=[];
      await page.route('**/api/prelive/v2/human/voice-preview',async route=>{previews.push(route.request().postDataJSON());await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({error:{code:'PREVIEW_OUTCOME_UNKNOWN',message:'上一调用结果未知',details:{original_operation_id:'original-preview-operation'}}})});});
      await page.route('**/api/prelive/v2/queries',async route=>{const body=route.request().postDataJSON();if(body.query!=='read_operation_receipt')return route.continue();receipts.push(body.args);await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({state:'outcome_unknown',preview_state:'unknown'})});});
      await nav({view:'session',preparation_id:'prep-A',room_id:'room-A'});
      await page.getByRole('button',{name:'试听当前音色',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('#workspace-editor').hasAttribute('aria-busy'));
      await page.getByRole('button',{name:'试听当前音色',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('#workspace-editor').hasAttribute('aria-busy'));
      assert.equal(previews.length,1);assert.ok(previews[0].operation_id);assert.deepEqual(receipts,[{operation_id:'original-preview-operation'}]);
      assert.match(await page.locator('#workspace-editor [role=status]').first().textContent(),/未再次请求语音生成/);
      await page.unroute('**/api/prelive/v2/queries');await page.unroute('**/api/prelive/v2/human/voice-preview');
    });
    await check('提交受理前立即锁定选中字段，传输不确定保持锁',async()=>{
      await nav({view:'products',product_id:'product-A',preparation_id:null,room_id:null});
      await page.evaluate(()=>shengboWorkbench.editors.discardDirty());
      await page.locator('[data-select-field=price]').click();
      await page.locator('.sb-a-input').fill('更新这个价格字段');
      state.blockMessages=true;
      await page.getByRole('button',{name:'发送',exact:true}).click();
      await page.waitForFunction(()=>!document.querySelector('.sb-a-send').disabled);
      assert.equal(await page.locator('[data-field=price]').isDisabled(),true);
      assert.equal(await page.locator('[data-field=name]').isDisabled(),false);
      assert.ok((await page.evaluate(()=>shengboWorkbench.assistant.getState())).pendingRequest);
      state.blockMessages=false;
    });
    assert.deepEqual(errors,[]);assert.deepEqual(state.failures,[]);
    console.log(JSON.stringify({status:'passed',checks:results.length,results,realProvidersCalled:false,formalDatabaseWritten:false}));
  }finally{await browser.close();await new Promise(resolve=>fixture.server.close(resolve));}
}
main().catch(e=>{console.error(e.stack);fixture.server.close();process.exitCode=1;});
