/* Private browser fixtures: no microphone capture, driver installation, OBS
 * changes, normal browser profile or paid speech generation. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {chromium}=require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const out=path.resolve(process.env.SHENGBO_ROUTE_UI_REPORT_DIR || 'verification/audio-routing-20261017');
const results=[];
const server=http.createServer((req,res)=>{
  if(req.url==='/audio-routing.js'){res.setHeader('Content-Type','application/javascript');res.end(fs.readFileSync('web/audio-routing.js'));return;}
  if(req.url==='/agent-workbench.css'){res.setHeader('Content-Type','text/css');res.end(fs.readFileSync('web/agent-workbench.css'));return;}
  res.setHeader('Content-Type','text/html;charset=utf-8');
  res.end('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><link rel="stylesheet" href="/agent-workbench.css"><main style="padding:20px;max-width:700px"><audio id="audio"></audio><section id="route" class="wb-audio-routing"></section></main><script src="/audio-routing.js"></script><script>window.route=ShengboAudioRouting.create({audio:document.querySelector("audio"),onFault:e=>window.lastFault=e.message,mayPlay:()=>true});route.mount(document.querySelector("#route"));</script>');
});
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  try{
    const context=await browser.newContext({viewport:{width:800,height:850}});
    await context.addInitScript(()=>{
      window.testMedia={authorized:false,denied:false,stops:0,requests:0,changed:null,devices:[{kind:'audiooutput',deviceId:'test-realtek',label:'扬声器 Realtek'},{kind:'audiooutput',deviceId:'test-cable',label:'CABLE Input (VB-Audio Virtual Cable)'}]};
      Object.defineProperty(navigator,'mediaDevices',{value:{
        enumerateDevices:async()=>testMedia.authorized?testMedia.devices:[{kind:'audiooutput',deviceId:'default',label:''}],
        getUserMedia:async()=>{testMedia.requests++;if(testMedia.denied)throw new DOMException('denied','NotAllowedError');testMedia.authorized=true;return{getTracks:()=>[{stop(){testMedia.stops++;}}]};},
        addEventListener:(name,cb)=>{testMedia.changed=cb;},removeEventListener:()=>{testMedia.changed=null;}
      }});
      Object.defineProperty(HTMLMediaElement.prototype,'setSinkId',{value:async function(id){this.testSink=id;}});
      Object.defineProperty(HTMLMediaElement.prototype,'sinkId',{get(){return this.testSink||'';}});
      Object.defineProperty(HTMLMediaElement.prototype,'paused',{get(){return !this.testPlaying;}});
      HTMLMediaElement.prototype.play=async function(){this.testPlaying=true;window.plays=(window.plays||0)+1;};
      HTMLMediaElement.prototype.pause=function(){this.testPlaying=false;};
    });
    const page=await context.newPage();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    const check=async(name,fn)=>{await fn();results.push({name,result:'PASS'});console.log('PASS '+name);};
    await page.goto('http://127.0.0.1:'+server.address().port);await page.evaluate(()=>route.ready);
    const device=page.getByRole('combobox',{name:'播放设备',exact:true});
    await check('默认保持本机播放，不主动授权或播放',async()=>{
      assert.equal(await device.isVisible(),true);
      assert.equal(await device.inputValue(),'');
      assert.deepEqual(await page.evaluate(()=>[testMedia.requests,window.plays||0]),[0,0]);
    });
    await check('设备列表授权前只显示系统默认项',async()=>{
      assert.equal(await device.locator('option').count(),1);
      assert.equal(await page.evaluate(()=>route.getStatus().mode),'local');
    });
    await page.locator('[data-route-authorize]').click();
    await check('点击授权才读取设备，读取后立即关闭所有测试麦克风轨道',async()=>{
      await page.waitForFunction(()=>testMedia.stops===1);
      assert.equal(await device.locator('option').count(),3);
      assert.deepEqual(await page.evaluate(()=>[testMedia.requests,testMedia.stops,window.plays||0]),[1,1,0]);
    });
    await device.selectOption('test-cable');
    await check('选择虚拟输出后显示精确 OBS 输入端与待验证状态',async()=>{
      await page.waitForFunction(()=>route.getStatus().status==='ready');
      assert.equal(await device.inputValue(),'test-cable');
      assert.match(await page.locator('[data-route-guide]').innerText(),/音频输入采集.*CABLE Output/);
      assert.match(await page.locator('[data-route-state]').innerText(),/已选择/);
      assert.equal(await page.evaluate(()=>document.querySelector('audio').sinkId),'test-cable');
    });
    await page.evaluate(()=>{testMedia.devices=testMedia.devices.filter(d=>d.deviceId!=='test-cable');return route.refresh();});
    await check('断开设备时保留选择并阻止播放，不转回扬声器',async()=>{
      assert.equal(await page.locator('#route').getAttribute('data-route-status'),'unavailable');
      assert.equal(await page.evaluate(()=>document.querySelector('audio').sinkId),'test-cable');
      assert.match(await page.locator('[data-route-state]').innerText(),/已暂停/);
      assert.equal(await page.evaluate(async()=>{try{await route.ensureReady();return false;}catch(e){return e.code==='OUTPUT_DEVICE_UNAVAILABLE';}}),true);
    });
    await device.selectOption('');
    await check('用户可显式关闭外部输出，恢复本机默认设备',async()=>{
      await page.waitForFunction(()=>route.getStatus().mode==='local');
      assert.equal(await page.evaluate(()=>document.querySelector('audio').sinkId),'');
      assert.match(await page.locator('[data-route-guide]').innerText(),/桌面音频.*默认/);
    });
    await device.selectOption('test-realtek');
    await check('普通扬声器对应音频输出采集，文案不混淆输入和输出',async()=>{
      await page.waitForFunction(()=>route.getStatus().status==='ready');
      assert.match(await page.locator('[data-route-guide]').innerText(),/音频输出采集.*Realtek/);
    });
    await page.reload();await page.evaluate(()=>route.ready);
    await check('重载无权限时保留外部设置并安全暂停，再授权可恢复',async()=>{
      assert.equal(await page.evaluate(()=>route.getStatus().mode),'external');
      assert.equal(await page.evaluate(()=>route.getStatus().status),'unavailable');
      await page.locator('[data-route-authorize]').click();
      await page.waitForFunction(()=>route.getStatus().status==='ready');
      assert.equal(await page.evaluate(()=>document.querySelector('audio').sinkId),'test-realtek');
      assert.equal(await page.evaluate(()=>window.plays||0),0);
    });
    await device.selectOption('');await page.waitForFunction(()=>route.getStatus().mode==='local'&&!route.getStatus().busy);
    await page.evaluate(()=>{testMedia.denied=true;});
    await page.locator('[data-route-authorize]').click();
    await check('拒绝授权不破坏本机播放设置',async()=>{
      await page.waitForFunction(()=>route.getStatus().message.includes('未获授权'));
      assert.equal(await device.inputValue(),'');
    });
    for(const width of [800,390,320]){
      await page.setViewportSize({width,height:850});
      await check(width+'px 设备面板无横向溢出',async()=>{
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      });
    }
    await check('操作过程中无浏览器异常',async()=>assert.deepEqual(errors,[]));
    fs.mkdirSync(out,{recursive:true});
    fs.writeFileSync(path.join(out,'ui-tests.json'),JSON.stringify({scope:'Private mocked browser audio routing; not real hardware or OBS capture',passed:results.length,results},null,2));
    await context.close();
  }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
