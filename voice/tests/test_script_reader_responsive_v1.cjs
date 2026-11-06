/* Private Chromium fixture: reading geometry and existing interactions, no audio. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fixture=require('./test_agent_workbench.cjs');
const OUT=path.resolve('D:/Codex project/shengbo-ops-agent/output/20261021-script-reliability/script-reader-ui');

async function main(){
  const state=fixture.resetState(),results=[],errors=[],geometry=[];state.editorLocks=[];state.run.status='COMPLETED';
  state.products[0].body.name='单人网球回弹训练器';
  const first=state.details.scripts[0];
  for(const [key,module]of Object.entries(first.modules))module.sentences=Array.from({length:key.startsWith('opening')?2:12},(_,i)=>({
    id:key+'-'+i,text:'这里是已保存的完整话术，按模块逐段核对商品特点与顾客使用场景。'.repeat(3),
    spoken_text:'这里是已保存的完整话术，按模块逐段核对商品特点与顾客使用场景。'.repeat(3)}));
  const second=structuredClone(first);second.ref=fixture.ref('script_revision','script-A2');second.body.version_number=2;second.body.variant_id='variant-A2';
  state.details.scripts.push(second);state.preparation.body.script_refs.push(second.ref);
  state.details.playlist.body.items.push({occurrence_id:'occ-A2',product_id:second.body.product_id,script_revision_ref:second.ref});
  await new Promise(resolve=>fixture.server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,executablePath:fs.existsSync(chromium.executablePath())?chromium.executablePath():'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const page=await browser.newPage({viewport:{width:726,height:764}});page.setDefaultTimeout(7000);page.on('pageerror',error=>errors.push(error.message));
  const idle=()=>page.waitForFunction(()=>!document.querySelector('#workspace-editor').hasAttribute('aria-busy'));
  const check=async(name,fn)=>{await fn();results.push(name);console.log('PASS '+name);};
  const open=async()=>{await page.locator('.wb-version-card[data-script-id="script-A2"] .wb-version-card-info').click();await page.locator('.wb-script-reader').waitFor();await page.waitForTimeout(180);};
  const close=async()=>{await page.locator('[data-close-script-reader]').click();await page.locator('.wb-script-reader').waitFor({state:'detached'});};
  const beforeCalls=state.calls.length;
  try{
    await page.goto('http://127.0.0.1:'+fixture.server.address().port);await page.evaluate(()=>shengboWorkbench.ready);
    await page.evaluate(()=>shengboWorkbench.navigate({view:'scripts',preparation_id:'prep-A',room_id:'room-A',product_id:null}));
    await page.evaluate(()=>shengboWorkbench.focus('work'));await idle();
    for(const size of [{width:726,height:764,minBody:220},{width:1440,height:900,minBody:350},
                       {width:375,height:667,minBody:140},{width:844,height:600,minBody:110}]){
      await check(size.width+'×'+size.height+' reader provides a usable scrolling body inside its result panel',async()=>{
        await page.setViewportSize({width:size.width,height:size.height});await page.evaluate(()=>shengboWorkbench.focus('work'));await page.waitForTimeout(250);await open();
        const value=await page.evaluate(()=>{const r=document.querySelector('.wb-script-reader'),b=r.querySelector('.wb-script-reader-body'),p=document.querySelector('#work-panel'),c=document.querySelector('#chat-panel'),rect=n=>{const x=n.getBoundingClientRect();return{x:x.x,y:x.y,width:x.width,height:x.height,bottom:x.bottom,right:x.right};};return{reader:rect(r),body:rect(b),panel:rect(p),chat:rect(c),chatVisible:getComputedStyle(c).display!=='none',client:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,bodyScrollable:b.scrollHeight>b.clientHeight,bodyOverflow:getComputedStyle(b).overflowY};});
        geometry.push({viewport:size,...value});
        assert.ok(value.body.height>=size.minBody,JSON.stringify(value));
        assert.ok(Math.abs(value.reader.y-value.panel.y)<=2&&Math.abs(value.reader.bottom-value.panel.bottom)<=2,JSON.stringify(value));
        assert.ok(value.reader.x>=value.panel.x&&value.reader.right<=value.panel.right,JSON.stringify(value));
        assert.ok(value.scroll<=value.client+1,JSON.stringify(value));
        assert.ok(value.bodyScrollable&&value.bodyOverflow==='auto');
        if(size.width>800){assert.ok(value.chatVisible);assert.ok(value.reader.right<value.chat.x+1);}
        await page.locator('.wb-script-reader-body').evaluate(node=>node.scrollTop=500);
        assert.ok(await page.locator('.wb-script-reader-body').evaluate(node=>node.scrollTop>0));await close();
      });
    }
    await page.setViewportSize({width:726,height:764});
    await check('reading cards keep editing collapsed and retain exact Agent multi-selection/version navigation',async()=>{
      await open();const reader=page.locator('.wb-script-reader');
      assert.equal(await reader.locator('input:visible,textarea:visible,[draggable="true"],[data-save-module]').count(),0);
      await reader.locator('[data-select-module="product_intro"]').click();await idle();
      await reader.locator('[data-select-module="selling_points"]').click();await idle();
      const selected=await page.evaluate(()=>shengboWorkbench.assistant.getState().selected);
      assert.deepEqual(selected.items.map(item=>item.target.module_key),['product_intro','selling_points']);
      for(const item of selected.items){assert.equal(item.target.object_ref.id,'script-A2');assert.match(item.target.selected_text_hash,/^sha256:/);}
      await reader.locator('[data-reader-focus="previous"]').click();await idle();assert.equal(await reader.getAttribute('data-script-id'),'script-A1');
      await reader.locator('[data-switch-script="script-A2"]').click();await idle();assert.equal(await reader.getAttribute('data-script-id'),'script-A2');
      await close();assert.equal(await page.locator('.wb-version-card[data-script-id="script-A2"]').count(),1);
    });
    await check('closing or navigating away restores background controls and keyboard focus',async()=>{
      const before=await page.locator('#work-panel').evaluate(panel=>[...panel.children].filter(node=>node.id!=='workspace-editor').map(node=>({inert:node.inert,hidden:node.getAttribute('aria-hidden')})));
      await open();assert.ok(await page.locator('#work-panel').evaluate(panel=>[...panel.children].filter(node=>node.id!=='workspace-editor').every(node=>node.inert&&node.getAttribute('aria-hidden')==='true')));
      await page.keyboard.press('Escape');await page.locator('.wb-script-reader').waitFor({state:'detached'});
      assert.equal(await page.locator('#work-panel').evaluate(node=>node.classList.contains('wb-script-reader-open')),false);
      assert.deepEqual(await page.locator('#work-panel').evaluate(panel=>[...panel.children].filter(node=>node.id!=='workspace-editor').map(node=>({inert:node.inert,hidden:node.getAttribute('aria-hidden')}))),before);
      assert.equal(await page.evaluate(()=>document.activeElement.dataset.scriptId),'script-A2');
      await open();await page.evaluate(()=>shengboWorkbench.navigate({view:'overview',preparation_id:'prep-A',room_id:'room-A'}));
      assert.equal(await page.locator('.wb-script-reader').count(),0);assert.equal(await page.locator('#work-panel').getAttribute('class'),'wb-work-panel');
      assert.equal(await page.locator('.wb-view-tabs').evaluate(node=>node.inert),false);
    });
    await check('reading never sends a mutation, Agent message or audio command',async()=>{
      const calls=state.calls.slice(beforeCalls);assert.ok(!calls.some(call=>call.body?.command||call.path.endsWith('/messages')),JSON.stringify(calls));
      assert.deepEqual(errors,[]);assert.deepEqual(state.failures,[]);
    });
  }finally{
    fs.mkdirSync(OUT,{recursive:true});fs.writeFileSync(path.join(OUT,'results.json'),JSON.stringify({passed:results.length,results,geometry,errors,finalState:await page.evaluate(()=>({state:shengboWorkbench.state(),readers:[...document.querySelectorAll('.wb-script-reader')].map(node=>({html:node.outerHTML.slice(0,600),parent:node.parentElement.outerHTML.slice(0,300)}))}))},null,2));
    await page.screenshot({path:path.join(OUT,'fixture-last.png')});
    await browser.close();await new Promise(resolve=>fixture.server.close(resolve));
  }
}
main().catch(error=>{console.error(error.stack);fixture.server.close();process.exitCode=1;});
