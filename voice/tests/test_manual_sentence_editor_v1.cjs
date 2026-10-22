/* Private browser fixture: human sentence edits; no live model, TTS or database. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fixture=require('./test_agent_workbench.cjs');
const OUT='D:/Codex project/shengbo-ops-agent/output/20261021-scenario-sales-v6/manual-sentence-editor';
async function main(){
 const state=fixture.resetState(),results=[],errors=[],writes=[];state.run.status='COMPLETED';state.editorLocks=[];
 const s=state.details.scripts[0];s.modules.selling_points.sentences.push({id:'selling-second',text:'第二句保留原样。',spoken_text:'第二句保留原样。'});
 const originalOther=structuredClone(s.modules.cta),originalFirst=structuredClone(s.modules.selling_points.sentences[0]);let stale=false;
 await new Promise(r=>fixture.server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({headless:true,executablePath:fs.existsSync(chromium.executablePath())?chromium.executablePath():'C:/Program Files/Google/Chrome/Application/chrome.exe'});
 const page=await browser.newPage({viewport:{width:1440,height:960}});page.setDefaultTimeout(7000);page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/prelive/v2/commands',async route=>{
  const body=route.request().postDataJSON();if(body.command!=='patch_script')return route.continue();writes.push(structuredClone(body));
  if(stale)return route.fulfill({status:409,json:{error:{code:'REVISION_CONFLICT',message:'服务端已有新版本，请刷新比较后再保存。'}}});
  const a=body.args,script=state.details.scripts.find(x=>x.ref.id===a.script_ref.id),oldRef=script.ref;
  assert.equal(a.expected_revision,state.preparation.ref.revision);assert.deepEqual(a.sentence_ids,['sentence-selling_points']);assert.equal(a.selected_text_hash,fixture.hash([script.modules.selling_points.sentences[0]]));
  script.modules.selling_points.sentences[0]={id:'saved-sentence-1',text:a.sentences[0],spoken_text:a.sentences[0]};script.ref=fixture.ref('script_revision','script-A1-human');script.body.script_revision++;
  state.preparation.ref=fixture.ref('preparation','prep-A',state.preparation.ref.revision+1);state.preparation.body.script_refs=[script.ref];
  state.details.playlist.body.items[0].script_revision_ref=script.ref;state.preparation.body.compiled_plan_ref=null;state.preparation.body.requirements_ref=null;state.preparation.body.audio_manifest_ref=null;
  return route.fulfill({json:{schema_version:'PRELIVE-2.3',status:'committed',command:'patch_script',operation_id:body.mutation.operation_id,data:{preparation_ref:state.preparation.ref,script_ref:script.ref,replacement:{old_ref:oldRef,new_ref:script.ref,sentence_ids:['saved-sentence-1']}}}});
 });
 const check=async(name,fn)=>{await fn();results.push(name);console.log('PASS '+name);};
 const reader=()=>page.locator('.wb-script-reader'),firstRow=()=>reader().locator('[data-module="selling_points"] .wb-script-sentence').first();
 try{
  await page.goto('http://127.0.0.1:'+fixture.server.address().port);await page.evaluate(()=>shengboWorkbench.ready);
  await page.evaluate(()=>shengboWorkbench.navigate({view:'scripts',preparation_id:'prep-A',room_id:'room-A',product_id:null}));await page.evaluate(()=>shengboWorkbench.focus('work'));await page.locator('[data-open-script="script-A1"]').first().click();
  await check('默认仍为阅读卡片，编辑输入收起，尚无写入',async()=>{assert.equal(await reader().locator('textarea:visible').count(),0);assert.equal(await reader().locator('[data-edit-sentence]').count(),7);assert.equal(writes.length,0);});
  await check('点击编辑只展开本句；取消保持业务正本不变',async()=>{await firstRow().locator('[data-edit-sentence]').click();await firstRow().locator('textarea').fill('取消的草稿。');assert.equal(await page.evaluate(()=>shengboWorkbench.editors.hasDirty()),true);await firstRow().locator('[data-cancel-sentence]').click();assert.equal(await reader().locator('textarea:visible').count(),0);assert.equal(await page.evaluate(()=>shengboWorkbench.editors.hasDirty()),false);assert.equal(writes.length,0);assert.deepEqual(s.modules.selling_points.sentences[0],originalFirst);});
  await check('未保存句子阻止关闭和切换，输入保持',async()=>{await firstRow().locator('[data-edit-sentence]').click();await firstRow().locator('textarea').fill('家人们，自己练球的方便，我真要好好推荐给你！');await reader().locator('[data-close-script-reader]').click();assert.equal(await reader().count(),1);assert.match(await reader().innerText(),/请先保存或取消/);assert.match(await firstRow().locator('textarea').inputValue(),/自己练球/);const blocked=await page.evaluate(async()=>{try{await shengboWorkbench.navigate({view:'overview',preparation_id:'prep-A',room_id:'room-A'});return false;}catch(error){return /请先保存或取消/.test(error.message);}});assert.equal(blocked,true);assert.equal(await page.evaluate(()=>shengboWorkbench.state().view),'scripts');});
  await check('保存走精确句ID/原句hash/CAS且不调用Agent',async()=>{await firstRow().locator('[data-save-sentence]').click();await reader().locator('[data-sentence-save-status]').filter({hasText:'本句已保存'}).waitFor();assert.equal(writes.length,1);assert.equal(writes[0].args.sentences.length,1);assert.equal(s.body.version_number,1);assert.equal(s.body.variant_id,'variant-A1');assert.equal(s.modules.selling_points.sentences[1].text,'第二句保留原样。');assert.deepEqual(s.modules.cta,originalOther);assert.equal(state.preparation.body.audio_manifest_ref,null);assert.equal(await reader().getAttribute('data-script-id'),'script-A1-human');assert.equal(await page.evaluate(()=>shengboWorkbench.editors.hasDirty()),false);});
  await check('Agent占用卖点区域不可编辑，其他模块仍可编辑',async()=>{state.editorLocks=[{run_id:'test-agent-edit',status:'RUNNING',targets:[{kind:'scripts',id:'prep-A',product_ids:['product-A'],module_keys:['selling_points']}]}];await page.evaluate(()=>shengboWorkbench.refreshEditorLocks());assert.equal(await firstRow().locator('[data-edit-sentence]').isDisabled(),true);assert.equal(await reader().locator('[data-module="cta"] [data-edit-sentence]').isDisabled(),false);state.editorLocks=[];await page.evaluate(()=>shengboWorkbench.refreshEditorLocks());assert.equal(await firstRow().locator('[data-edit-sentence]').isDisabled(),false);});
  await check('版本冲突拒绝后保留输入与可重试状态，不覆盖新版本',async()=>{stale=true;await firstRow().locator('[data-edit-sentence]').click();await firstRow().locator('textarea').fill('保留这份冲突草稿。');await firstRow().locator('[data-save-sentence]').click();await reader().locator('[data-sentence-save-status]').filter({hasText:'服务端已有新版本'}).waitFor();assert.equal(await firstRow().locator('textarea').inputValue(),'保留这份冲突草稿。');assert.equal(await firstRow().locator('.wb-script-sentence-edit').evaluate(x=>x.inert),false);assert.equal(await page.evaluate(()=>shengboWorkbench.editors.hasDirty()),true);assert.match(s.modules.selling_points.sentences[0].text,/自己练球/);await firstRow().locator('[data-cancel-sentence]').click();});
  await check('无模型/语音请求、异常或重复保存，页面无横向溢出',async()=>{assert.deepEqual(errors,[]);assert.deepEqual(state.failures,[]);assert.ok(!state.calls.some(x=>x.path.endsWith('/messages')||['prepare_required_audio','preview_audio'].includes(x.body?.command)));assert.equal(writes.length,2);const x=await page.evaluate(()=>({client:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth}));assert.ok(x.scroll<=x.client+1);});
  fs.mkdirSync(OUT,{recursive:true});await page.screenshot({path:path.join(OUT,'私有夹具逐句编辑.png'),fullPage:true});
 }finally{fs.mkdirSync(OUT,{recursive:true});fs.writeFileSync(path.join(OUT,'browser-tests.json'),JSON.stringify({scope:'Private browser HTTP fixture, not live server or real model',passed:results.length,results,errors,writes:writes.map(x=>({command:x.command,args:x.args})),fixture_errors:state.failures},null,2));await browser.close();await new Promise(r=>fixture.server.close(r));}
}
main().catch(e=>{console.error(e.stack);fixture.server.close();process.exitCode=1;});

