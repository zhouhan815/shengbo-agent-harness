/* Native title activation regression. Private fixture, no business/model writes. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs');
const {chromium}=require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fixture=require('./test_agent_workbench.cjs');
async function main(){
  const state=fixture.resetState(),errors=[];state.editorLocks=[];
  await new Promise(resolve=>fixture.server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,executablePath:fs.existsSync(chromium.executablePath())?chromium.executablePath():'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const page=await browser.newPage();page.on('pageerror',error=>errors.push(error.message));
  try{
    await page.route('**/api/prelive/v2/queries',async route=>{if(route.request().postDataJSON().query!=='get_business_schema')return route.continue();const response=await route.fetch(),value=await response.json();value.schema.product.properties.hidden_optional_fields={type:'array',items:{type:'string',enum:['knowledge','material']}};await route.fulfill({response,json:value});});
    await page.goto('http://127.0.0.1:'+fixture.server.address().port);await page.evaluate(()=>shengboWorkbench.ready);
    await page.evaluate(()=>shengboWorkbench.navigate({view:'products',product_id:'product-A',preparation_id:null,room_id:null}));
    const before=state.calls.filter(c=>c.path.endsWith('/messages')).length;
    assert.equal(await page.locator('[data-select-field=knowledge]').evaluate(n=>n.tagName),'LABEL');
    assert.equal(await page.locator('[data-select-field=knowledge]').evaluate(n=>n.htmlFor),'','selection title must not forward activation to textarea');
    await page.evaluate(()=>document.querySelector('[data-select-field=core_selling_points]').click());
    await page.evaluate(()=>document.querySelector('[data-select-field=knowledge]').click());
    const selected=await page.evaluate(()=>shengboWorkbench.assistant.getState().selected);
    assert.deepEqual(selected.items.map(item=>item.target.field_path),['/core_selling_points','/knowledge']);
    assert.equal(await page.locator('.sb-a-selection-chip').count(),2);
    assert.match(await page.locator('#workbench-notice-message').textContent(),/已选择 2 项/);
    assert.equal(await page.evaluate(()=>document.activeElement===document.querySelector('textarea[data-field=knowledge]')),false,'title activation must not focus editable knowledge');
    await page.locator('textarea[data-field=knowledge]').click();
    assert.equal(await page.evaluate(()=>document.activeElement===document.querySelector('textarea[data-field=knowledge]')),true);
    assert.equal(await page.locator('.sb-a-selection-chip').count(),2,'clicking textarea must keep targets unchanged');
    await page.evaluate(()=>document.querySelector('[data-select-field=knowledge]').click());
    assert.equal(await page.locator('.sb-a-selection-chip').count(),1);
    assert.match(await page.locator('#workbench-notice-message').textContent(),/已选择 1 项/);
    await page.getByRole('button',{name:/^移除 .*核心卖点$/}).click();
    assert.equal(await page.locator('.sb-a-selection-chip').count(),0);
    assert.equal(await page.locator('#workbench-notice-message').textContent(),'已取消选择。','removing the last right-side tag must clear the stale count');
    await page.evaluate(()=>{window.noticeMutations=0;const observer=new MutationObserver(records=>window.noticeMutations+=records.length);observer.observe(document.getElementById('workbench-notice-message'),{childList:true,characterData:true,subtree:true});window.noticeObserver=observer;});
    await page.evaluate(()=>shengboWorkbench.assistant.refresh());
    assert.equal(await page.evaluate(()=>window.noticeMutations),0,'unchanged polling must not rewrite the notice');
    await page.evaluate(()=>window.noticeObserver.disconnect());
    await page.evaluate(()=>document.querySelector('[data-select-field=core_selling_points]').click());
    state.run.status='COMPLETED';state.run.artifacts=[{label:'查看商品资料',destination:{view:'products',product_id:'product-A'}}];
    await page.evaluate(()=>shengboWorkbench.assistant.refresh());
    const completionNotice=await page.locator('#workbench-notice-message').textContent();assert.match(completionNotice,/已保存(?:新)?成果/);
    await page.getByRole('button',{name:/^移除 .*核心卖点$/}).click();
    assert.equal(await page.locator('#workbench-notice-message').textContent(),completionNotice,'a later selection count change must not overwrite a completion notice');
    assert.equal(await page.getByRole('checkbox',{name:'商品知识库',exact:true}).count(),0,'unrelated applicability checkbox must have a distinct accessible name');
    assert.equal(await page.getByRole('checkbox',{name:'不适用：商品知识库',exact:true,includeHidden:true}).count(),1);
    assert.equal(state.calls.filter(c=>c.path.endsWith('/messages')).length,before);
    assert.equal(state.calls.filter(c=>c.body?.command).length,0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({status:'passed',checks:20,nativeLabelClickVerified:true,coreAndKnowledgeMultiSelect:true,selectionNoticeSynchronized:true,realProvidersCalled:false,formalDatabaseWritten:false}));
  }finally{await browser.close();await new Promise(resolve=>fixture.server.close(resolve));}
}
main().catch(error=>{console.error(error);fixture.server.close();process.exitCode=1;});
