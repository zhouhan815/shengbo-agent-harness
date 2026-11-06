/* Catalog layout + policy-schema consumer contract, over private HTTP only. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fixture=require('./test_agent_workbench.cjs');
async function main(){
  const state=fixture.resetState(),results=[],errors=[];state.editorLocks=[];
  const product=state.products[0];product.body.specifications=[{name:'材质',value:'EVA'},{name:'尺码',value:'36—45'},{name:'颜色',value:'黑色、白色'}];
  product.body.faq='问：脚背较宽如何选？\n答：参考已确认的尺码说明。';
  product.body.knowledge_documents=[{id:'guide-1',title:'使用与保养说明',content:'请勿长时间暴晒；湿地面仍需慢行。'}];
  product.body.sales_platform='douyin';product.body.shipping_time='';product.body.return_policy='';
  const originalDocuments=structuredClone(product.body.knowledge_documents);
  const policy=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../app/data/platform_rules/douyin-20261020.json'),'utf8'));
  await new Promise(resolve=>fixture.server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,executablePath:fs.existsSync(chromium.executablePath())?chromium.executablePath():'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const page=await browser.newPage({viewport:{width:1440,height:1050}});page.on('pageerror',error=>errors.push(error.message));
  const nav=d=>page.evaluate(d=>shengboWorkbench.navigate(d),d),idle=()=>page.waitForFunction(()=>!document.querySelector('#workspace-editor').hasAttribute('aria-busy'));
  const check=async(name,fn)=>{await fn();results.push(name);};
  const field=key=>page.locator('[data-field="'+key+'"]');
  const out=path.resolve(__dirname,'../verification/catalog-classification-v2-20261020');fs.mkdirSync(out,{recursive:true});
  try{
    await page.route('**/api/prelive/v2/queries',async route=>{if(route.request().postDataJSON().query!=='get_business_schema')return route.continue();const response=await route.fetch(),schema=await response.json();schema.platform_policies={douyin:policy};Object.assign(schema.schema.product.properties,{sales_platform:{type:'string',default:'unspecified',enum:['unspecified','douyin','tiktok','kuaishou','other']},specifications:{type:'array',items:{type:'object',properties:{name:{type:'string',maxLength:60},value:{type:'string',maxLength:500}}}},knowledge_documents:{type:'array',items:{type:'object',properties:{id:{type:'string'},title:{type:'string'},content:{type:'string'}}}},hidden_optional_fields:{type:'array',items:{type:'string',enum:['specifications','faq','knowledge','knowledge_documents','shipping_time','return_policy','shelf_life']}}});await route.fulfill({response,json:schema});});
    await page.goto('http://127.0.0.1:'+fixture.server.address().port);await page.evaluate(()=>shengboWorkbench.ready);await nav({view:'products',product_id:'product-A',preparation_id:null,room_id:null});
    await check('核心卖点→规格参数→常见问题→知识文档，全部独立且FAQ无需展开',async()=>{
      const order=await page.evaluate(()=>['core_selling_points','specifications','faq','knowledge','knowledge_documents'].map(key=>document.querySelector('[data-field="'+key+'"]').closest('.wb-field')));
      assert.equal(order.length,5);
      assert.equal(await page.evaluate(()=>{const keys=['core_selling_points','specifications','faq','knowledge','knowledge_documents'];return keys.slice(1).every((key,i)=>document.querySelector('[data-field="'+keys[i]+'"]').compareDocumentPosition(document.querySelector('[data-field="'+key+'"]'))&Node.DOCUMENT_POSITION_FOLLOWING);}),true);
      assert.equal(await field('faq').evaluate(n=>!!n.closest('details')),false);assert.equal(await field('specifications').evaluate(n=>!!n.closest('details')),false);
      assert.equal(await field('faq').isVisible(),true);assert.equal(await field('specifications').isVisible(),true);
      assert.equal(await field('faq').inputValue(),product.body.faq);assert.equal(await page.getByLabel('文档标题',{exact:true}).inputValue(),'使用与保养说明');
    });
    await check('规格逐行横排名称、内容、删除，无纵向大卡片',async()=>{
      assert.equal(await page.locator('[data-specification-row]').count(),3);
      const positions=await page.locator('[data-specification-row]').first().evaluate(row=>Array.from(row.children).map(n=>{const r=n.getBoundingClientRect();return{x:r.x,y:r.y,height:r.height};}));
      assert.ok(positions[0].x<positions[1].x&&positions[1].x<positions[2].x);assert.ok(Math.max(...positions.map(p=>p.y))-Math.min(...positions.map(p=>p.y))<5);
      assert.equal(await page.getByLabel('规格名称 1',{exact:true}).inputValue(),'材质');assert.equal(await page.getByLabel('规格内容 2',{exact:true}).inputValue(),'36—45');
    });
    await check('标题仍可双选，普通规格或FAQ输入不会改变选择',async()=>{
      await page.locator('[data-select-field=specifications]').evaluate(n=>n.click());await page.locator('[data-select-field=faq]').evaluate(n=>n.click());
      const selection=await page.evaluate(()=>shengboWorkbench.assistant.getState().selected);
      assert.deepEqual(selection.items.map(item=>item.target.field_path),['/specifications','/faq']);
      await page.getByLabel('规格内容 1',{exact:true}).click();await field('faq').click();assert.equal(await page.locator('.sb-a-selection-chip').count(),2);
      await page.getByRole('button',{name:'清空选择',exact:true}).click();
    });
    await check('现有抖音只展示后端规则，不在加载时修改空字段',async()=>{
      assert.equal(await field('shipping_time').inputValue(),'');assert.equal(await field('return_policy').inputValue(),'');
      assert.ok((await page.locator('.wb-platform-guidance').textContent()).includes(policy.version));assert.equal(await page.getByRole('link',{name:policy.sources[0].title}).getAttribute('href'),policy.sources[0].url);
      assert.equal(state.calls.filter(c=>c.body?.command).length,0);
    });
    await check('主动选择抖音才从schema自动填默认值，未提供政策的平台不推造',async()=>{
      await page.getByLabel('销售平台',{exact:true}).selectOption('tiktok');assert.equal(await field('shipping_time').inputValue(),'');assert.equal(await page.getByRole('button',{name:'恢复平台默认规则'}).isDisabled(),true);
      await page.getByLabel('销售平台',{exact:true}).selectOption('douyin');assert.equal(await field('shipping_time').inputValue(),policy.defaults.shipping_time);assert.equal(await field('return_policy').inputValue(),policy.defaults.return_policy);
    });
    await check('人工履约文案不被平台切换覆盖，恢复时需明确二次确认',async()=>{
      await field('shipping_time').fill('人工已确认的专用配送安排');await page.getByLabel('销售平台',{exact:true}).selectOption('tiktok');await page.getByLabel('销售平台',{exact:true}).selectOption('douyin');
      assert.equal(await field('shipping_time').inputValue(),'人工已确认的专用配送安排');
      await page.getByRole('button',{name:'恢复平台默认规则'}).click();await idle();assert.equal(await field('shipping_time').inputValue(),'人工已确认的专用配送安排');
      await page.getByRole('button',{name:'保留人工填写',exact:true}).click();await idle();assert.equal(await field('shipping_time').inputValue(),'人工已确认的专用配送安排');
      await page.getByRole('button',{name:'恢复平台默认规则'}).click();await idle();await page.getByRole('button',{name:'确认替换人工填写的履约内容',exact:true}).click();await idle();assert.equal(await field('shipping_time').inputValue(),policy.defaults.shipping_time);
    });
    await check('商家自选发货承诺仅显式采用，覆写人工发货说明先确认且售后不变',async()=>{
      await field('shipping_time').fill('人工专用发货说明');const returns=await field('return_policy').inputValue();
      await page.getByRole('button',{name:policy.shipping_options[0].label,exact:true}).click();await idle();assert.equal(await field('shipping_time').inputValue(),'人工专用发货说明');
      await page.getByRole('button',{name:'确认替换发货承诺',exact:true}).click();await idle();assert.deepEqual([await field('shipping_time').inputValue(),await field('return_policy').inputValue()],[policy.shipping_options[0].text,returns]);
      await page.getByLabel('销售平台',{exact:true}).selectOption('tiktok');await page.getByLabel('销售平台',{exact:true}).selectOption('douyin');
    });
    await check('锁定规格或发货字段时不改写，其他人工输入可继续',async()=>{
      state.editorLocks=[{run_id:'private-lock',status:'RUNNING',targets:[{kind:'product',id:'product-A',field_paths:['/specifications','/shipping_time']}]}];await page.evaluate(()=>shengboWorkbench.refreshEditorLocks());
      assert.equal(await page.getByLabel('规格名称 1',{exact:true}).isDisabled(),true);assert.equal(await page.getByRole('button',{name:'删除规格 1'}).isDisabled(),true);assert.equal(await field('faq').isDisabled(),false);
      const lockedShipping=await field('shipping_time').inputValue();await page.getByLabel('销售平台',{exact:true}).selectOption('tiktok');await page.getByLabel('销售平台',{exact:true}).selectOption('douyin');assert.equal(await field('shipping_time').inputValue(),lockedShipping);
      state.editorLocks=[];await page.evaluate(()=>shengboWorkbench.refreshEditorLocks());assert.equal(await page.getByLabel('规格名称 1',{exact:true}).isDisabled(),false);
    });
    await check('规格增删与FAQ草稿跨导航保留，保存各自字段且不塞进知识文档',async()=>{
      await page.getByLabel('规格内容 1',{exact:true}).fill('已确认EVA');await page.getByRole('button',{name:'删除规格 3'}).click();await idle();
      await page.getByRole('button',{name:'添加规格',exact:true}).click();await idle();await page.getByLabel('规格名称 3',{exact:true}).fill('适用范围');await page.getByLabel('规格内容 3',{exact:true}).fill('日常居家');
      const faq='问：如何打理？\n答：按已确认的清洗说明操作。';await field('faq').fill(faq);
      await nav({view:'products',product_id:'product-B',preparation_id:null});await nav({view:'products',product_id:'product-A',preparation_id:null});
      assert.equal(await field('faq').inputValue(),faq);assert.equal(await page.getByLabel('规格内容 1',{exact:true}).inputValue(),'已确认EVA');
      await page.getByRole('button',{name:'保存商品',exact:true}).click();await idle();const saved=state.calls.filter(c=>c.body?.command==='save_product').at(-1).body.args;
      assert.equal(saved.fields.faq,faq);assert.deepEqual(saved.fields.knowledge_documents,originalDocuments);assert.deepEqual(saved.fields.specifications,[{name:'材质',value:'已确认EVA'},{name:'尺码',value:'36—45'},{name:'适用范围',value:'日常居家'}]);
      assert.ok(saved.patches.some(p=>p.path==='/faq'));assert.ok(saved.patches.some(p=>p.path==='/specifications'));assert.ok(!saved.patches.some(p=>p.path==='/knowledge_documents'));
    });
    await page.locator('[data-field=specifications]').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'specifications-faq-desktop.png'),fullPage:true});
    await check('窄屏必要时折行，规格内容和FAQ仍可编辑且不横向溢出',async()=>{
      await page.setViewportSize({width:390,height:900});await page.evaluate(()=>shengboWorkbench.focus('work'));await field('specifications').scrollIntoViewIfNeeded();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.equal(await page.getByLabel('规格内容 1',{exact:true}).isEnabled(),true);await page.screenshot({path:path.join(out,'specifications-faq-mobile.png'),fullPage:true});
    });
    assert.deepEqual(errors,[]);const report={status:'passed',checks:results.length,results,browser_errors:errors,platformPolicySource:'local backend JSON projected through private fixture schema; no frontend rule text constants',realProvidersCalled:false,formalDatabaseWritten:false};fs.writeFileSync(path.join(out,'ui.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
  }finally{await browser.close();await new Promise(resolve=>fixture.server.close(resolve));}
}
main().catch(error=>{console.error(error);fixture.server.close();process.exitCode=1;});
