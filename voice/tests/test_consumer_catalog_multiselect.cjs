/* Actual UI over a private HTTP fixture. No production service or model calls. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fixture=require('./test_agent_workbench.cjs');
const {unpackCorePoint,packCorePoint}=require('../web/workspace-editors.js');
async function main(){
  const state=fixture.resetState(),results=[],errors=[];state.editorLocks=[];
  state.products[0].body.hidden_optional_fields=['shelf_life'];
  state.products[0].body.sales_platform='unspecified';
  state.products[0].body.promo_price=99;state.products[0].body.promotion='原有历史信息，不应悄悄清除';
  await new Promise(resolve=>fixture.server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,executablePath:fs.existsSync(chromium.executablePath())?chromium.executablePath():'C:/Program Files/Google/Chrome/Application/chrome.exe'});const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',e=>errors.push(e.message));const nav=d=>page.evaluate(d=>shengboWorkbench.navigate(d),d);
  const idle=()=>page.waitForFunction(()=>!document.querySelector('#workspace-editor').hasAttribute('aria-busy'));
  const messageCount=()=>state.calls.filter(c=>c.path.endsWith('/messages')).length;
  const selectField=k=>page.locator('[data-select-field="'+k+'"]');
  const check=async(name,fn)=>{await fn();results.push(name);};
  try{
    await page.route('**/api/prelive/v2/queries',async route=>{const body=route.request().postDataJSON();if(body.query!=='get_business_schema')return route.continue();const response=await route.fetch(),value=await response.json();value.schema.product.properties.sales_platform={type:'string',default:'unspecified',enum:['unspecified','douyin','tiktok','kuaishou','other']};await route.fulfill({response,json:value});});
    await page.goto('http://127.0.0.1:'+fixture.server.address().port);await page.evaluate(()=>shengboWorkbench.ready);
    await nav({view:'products',product_id:'product-A',preparation_id:null,room_id:null});
    await check('商品仅有一个名称与内容卖点编辑器，知识库不埋在规格内',async()=>{
      assert.equal(await page.locator('[data-field=selling_points]').count(),0);
      assert.equal(await page.locator('[data-field=core_selling_points]').count(),1);
      assert.equal(await page.getByLabel('卖点内容 1',{exact:true}).inputValue(),'轻便');
      assert.equal(await page.getByLabel('卖点名称 1',{exact:true}).inputValue(),'');
      assert.equal(await page.locator('[data-field=promo_price],[data-field=promotion]').count(),0);
      assert.equal(await page.locator('[data-field=knowledge]').isVisible(),true);
      assert.equal(await page.locator('[data-field=knowledge]').evaluate(n=>!!n.closest('details')),false);
      assert.equal(await page.locator('[data-field=shelf_life_value]').isVisible(),false);
      assert.equal(await page.locator('[data-field=shelf_life_unit]').isVisible(),false);
      assert.equal(await page.getByRole('button',{name:'让 Agent 修改此项',exact:true}).count(),0);
    });
    await check('旧无名称卖点与冒号表示可逆，不给旧资料强加名称',async()=>{
      for(const text of ['轻便','材质: EVA 环保材质','足弓支撑：贴合足部曲线','注意：保留内容：后续冒号'])assert.equal(packCorePoint(unpackCorePoint(text)),text);
      assert.equal(packCorePoint({name:'名称',content:'具体说明',original:''}),'名称：具体说明');
    });
    await check('点输入框只编辑；点字段名多选，不建任务或调用模型',async()=>{
      const before=messageCount();await page.locator('[data-field=price]').click();
      assert.equal(await page.locator('.sb-a-selection').isVisible(),false);
      await selectField('price').click();await selectField('knowledge').click();
      assert.equal(await page.locator('.sb-a-selection-chip').count(),2);
      const selected=await page.evaluate(()=>shengboWorkbench.assistant.getState().selected);
      assert.equal(selected.items.length,2);assert.deepEqual(selected.items.map(x=>x.target.field_path),['/price','/knowledge']);
      assert.equal(await selectField('price').getAttribute('aria-pressed'),'true');
      assert.equal(messageCount(),before);
    });
    await check('再次点击取消、键盘添加、逐项移除同步左侧选中态',async()=>{
      await selectField('price').click();assert.equal(await page.locator('.sb-a-selection-chip').count(),1);
      await selectField('price').focus();await page.keyboard.press('Space');await idle();
      assert.equal(await page.locator('.sb-a-selection-chip').count(),2);
      await page.getByRole('button',{name:/^移除 .*商品知识库$/}).click();
      assert.equal(await selectField('knowledge').getAttribute('aria-pressed'),'false');
      assert.equal(await page.locator('.sb-a-selection-chip').count(),1);
      await page.getByRole('button',{name:'取消指定',exact:true}).click();
      assert.equal(await selectField('price').getAttribute('aria-pressed'),'false');
    });
    await check('跨商品多选保持精确引用，发送前保存冲突仍需人工决定',async()=>{
      await page.locator('[data-field=price]').fill('189');await selectField('price').click();
      await nav({view:'products',product_id:'product-B',preparation_id:null});await selectField('name').click();
      const before=messageCount();await page.locator('.sb-a-input').fill('将选中的价格改为189，并将另一个商品名整理清楚。');
      await page.getByRole('button',{name:'发送',exact:true}).click();await page.locator('dialog[open]').waitFor();assert.equal(messageCount(),before);
      await page.getByRole('button',{name:'返回编辑',exact:true}).click();assert.equal(await page.locator('.sb-a-selection-chip').count(),2);
      await page.getByRole('button',{name:'发送',exact:true}).click();await page.locator('dialog[open]').waitFor();
      await Promise.all([page.waitForResponse(r=>r.url().endsWith('/messages')),page.getByRole('button',{name:'保存修改并继续',exact:true}).click()]);
      await page.waitForFunction(()=>!document.querySelector('.sb-a-send').disabled);
      const request=state.calls.filter(c=>c.path.endsWith('/messages')).at(-1).body;
      assert.equal(request.selection.items.length,2);
      assert.equal(request.selection.items[0].target.object_ref.id,'product-A');assert.equal(request.selection.items[0].target.object_ref.revision,2);
      assert.equal(request.selection.items[1].target.object_ref.id,'product-B');
      assert.ok(request.observed_versions.some(r=>r.id==='product-A'&&r.revision===2));assert.ok(request.observed_versions.some(r=>r.id==='product-B'));
      await page.getByRole('button',{name:'清空选择',exact:true}).click();
    });
    await check('核心卖点编辑仅提交canonical patch，兼容投影同步且历史活动保留',async()=>{
      await nav({view:'products',product_id:'product-A',preparation_id:null});
      await page.getByLabel('卖点名称 1',{exact:true}).fill('轻便携带');await page.getByLabel('卖点内容 1',{exact:true}).fill('出游携带方便');
      await page.getByRole('button',{name:'保存商品',exact:true}).click();await idle();
      const save=state.calls.filter(c=>c.body?.command==='save_product').at(-1).body.args;
      assert.equal(save.fields.core_selling_points[0],'轻便携带：出游携带方便');assert.equal(save.fields.core_selling_points[1],'易清洁');
      assert.equal(save.fields.selling_points,save.fields.core_selling_points.join('\n'));
      assert.ok(save.patches.some(p=>p.path==='/core_selling_points'));assert.ok(!save.patches.some(p=>p.path==='/selling_points'));
      assert.equal(save.fields.promo_price,99);assert.equal(save.fields.promotion,'原有历史信息，不应悄悄清除');
    });
    await check('平台只带出需显式采用的中性模板，不制造发货时效或覆盖填写',async()=>{
      const shipping=page.locator('[data-field=shipping_time]'),returns=page.locator('[data-field=return_policy]');
      const oldShipping=await shipping.inputValue(),oldReturns=await returns.inputValue();
      await page.getByLabel('销售平台',{exact:true}).selectOption('douyin');
      assert.equal(await shipping.inputValue(),oldShipping);
      await page.getByRole('button',{name:'采用履约填写模板（仅填空项）',exact:true}).click();
      assert.equal(await shipping.inputValue(),oldShipping);assert.equal(await returns.inputValue(),oldReturns);
      await shipping.fill('');await returns.fill('');await page.getByRole('button',{name:'采用履约填写模板（仅填空项）',exact:true}).click();
      assert.match(await shipping.inputValue(),/订单页约定/);assert.doesNotMatch(await shipping.inputValue(),/48|24|小时/);
      await page.evaluate(()=>shengboWorkbench.editors.discardDirty());
    });
    await check('模块标题可多选且有文本hash，正文仍支持正常手改',async()=>{
      await nav({view:'scripts',preparation_id:'prep-A',room_id:'room-A',product_id:null});
      await page.locator('[data-module=campaign]').click();assert.equal(await page.locator('.sb-a-selection').isVisible(),false);
      await page.locator('[data-select-module=campaign]').click();await idle();await page.locator('[data-select-module=selling_points]').click();await idle();
      const selected=await page.evaluate(()=>shengboWorkbench.assistant.getState().selected);
      assert.equal(selected.items.length,2);for(const item of selected.items)assert.ok(item.target.selected_text_hash.startsWith('sha256:'));
      assert.equal(await page.getByRole('button',{name:'让 Agent 修改此项',exact:true}).count(),0);
      await page.getByRole('button',{name:'清空选择',exact:true}).click();
    });
    await check('关闭自动跟随仍保持视图，另有清晰最新成果入口且偏好可恢复',async()=>{
      await nav({view:'products',product_id:'product-B',preparation_id:null});
      state.run.status='COMPLETED';state.run.artifacts=[{label:'查看准备好的模块',destination:{view:'scripts',preparation_id:'prep-A',room_id:'room-A',resource_id:'script-A1'}}];
      await page.evaluate(()=>shengboWorkbench.assistant.refresh());assert.equal(await page.evaluate(()=>shengboWorkbench.state().view),'products');
      await page.getByRole('button',{name:'查看最新成果',exact:true}).click();await page.waitForFunction(()=>shengboWorkbench.state().view==='scripts');
      await page.getByLabel('自动跟随当前任务成果',{exact:true}).check();await page.reload();await page.evaluate(()=>shengboWorkbench.ready);
      assert.equal(await page.getByLabel('自动跟随当前任务成果',{exact:true}).isChecked(),true);
      await page.getByLabel('自动跟随当前任务成果',{exact:true}).uncheck();
    });
    await check('选择上限20项与标签安全呈现，不执行HTML、不发送任务',async()=>{
      const before=messageCount();await page.evaluate(()=>{const a=shengboWorkbench.assistant;for(let i=0;i<21;i++)a.toggleSelection({label:i===0?'<img src=x onerror=alert(1)>':'字段'+i,target:{object_ref:{kind:'product',id:'product-A',revision:1},field_path:'/field_'+i},destination:{view:'products',product_id:'product-A'}});});
      assert.equal(await page.locator('.sb-a-selection-chip').count(),20);assert.equal(await page.locator('.sb-a-selection img').count(),0);assert.equal(messageCount(),before);
      assert.match(await page.locator('.sb-a-error').textContent(),/最多选择20项/);await page.getByRole('button',{name:'清空选择',exact:true}).click();
    });
    await check('320px标签与核心卖点不造成页面横向溢出',async()=>{
      await nav({view:'products',product_id:'product-A',preparation_id:null});await page.setViewportSize({width:320,height:850});await page.evaluate(()=>shengboWorkbench.focus('work'));
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    });
    assert.deepEqual(errors,[]);
    const out=path.resolve(__dirname,'../verification/consumer-catalog-20261020');fs.mkdirSync(out,{recursive:true});
    fs.writeFileSync(path.join(out,'ui.json'),JSON.stringify({status:'passed',checks:results.length,results,browser_errors:errors,realProvidersCalled:false,formalDatabaseWritten:false},null,2));
    await page.setViewportSize({width:1440,height:1000});await page.screenshot({path:path.join(out,'product-editor.png'),fullPage:true});
    await page.locator('[data-field=core_selling_points]').scrollIntoViewIfNeeded();await selectField('core_selling_points').click();await page.screenshot({path:path.join(out,'product-core-and-knowledge.png'),fullPage:true});
    console.log(JSON.stringify({status:'passed',checks:results.length,results,realProvidersCalled:false,formalDatabaseWritten:false}));
  }finally{await browser.close();await new Promise(resolve=>fixture.server.close(resolve));}
}
main().catch(error=>{console.error(error);fixture.server.close();process.exitCode=1;});
