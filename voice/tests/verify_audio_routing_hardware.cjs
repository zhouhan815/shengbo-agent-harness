/* Opt-in local hardware smoke test. Uses a private Chrome context, an existing
 * paid-test WAV and the existing Steam render endpoint. It never opens a
 * microphone track, changes Windows defaults, touches business state, installs
 * drivers, controls OBS or starts an external live stream. */
'use strict';
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const assert=require('node:assert/strict');
const {chromium}=require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const meta=JSON.parse(fs.readFileSync('verification/minimax-smoke-20261017/result.json'));
const out=path.resolve('verification/audio-routing-20261017');
const server=http.createServer((req,res)=>{
  if(req.url==='/audio-routing.js'){res.setHeader('Content-Type','application/javascript');res.end(fs.readFileSync('web/audio-routing.js'));return;}
  if(req.url==='/clip.wav'){res.setHeader('Content-Type','audio/wav');res.end(fs.readFileSync(meta.assets[0].path));return;}
  res.setHeader('Content-Type','text/html;charset=utf-8');
  res.end('<!doctype html><meta charset="utf-8"><audio id="audio" src="/clip.wav" loop></audio><button id="play">播放现有测试音频</button><button id="pause">暂停</button><script src="/audio-routing.js"></script><script>window.route=ShengboAudioRouting.create({audio:document.querySelector("audio"),mayPlay:()=>true});document.querySelector("#play").onclick=async()=>{await route.ensureReady();await audio.play();};document.querySelector("#pause").onclick=()=>audio.pause();</script>');
});
(async()=>{
  if(!process.argv.includes('--run'))throw Error('Pass --run to explicitly run the existing-WAV hardware output test.');
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const origin='http://127.0.0.1:'+server.address().port;
  const browser=await chromium.launch({channel:'chrome',headless:true,ignoreDefaultArgs:['--mute-audio']});
  const result={scope:'Real HTMLMediaElement.setSinkId + read-only Windows output signal; OBS reception not tested',timestamp:new Date().toISOString(),paid_calls:0,microphone_tracks_opened:0};
  try{
    const context=await browser.newContext();
    // Permission exists only in this disposable test context. Enumerate output
    // devices directly; getUserMedia is never called and no recording occurs.
    await context.grantPermissions(['microphone'],{origin});
    const page=await context.newPage();await page.goto(origin);await page.evaluate(()=>route.ready);
    const devices=await page.evaluate(()=>route.getStatus().devices);
    result.device_labels=devices.map(x=>x.label);
    const device=devices.find(x=>/Steam Streaming Speakers/.test(x.label));
    if(!device)throw Error('Existing Steam Streaming Speakers render endpoint not available to the test browser.');
    await page.evaluate(id=>route.selectDevice(id),device.deviceId);
    assert.equal(await page.evaluate(()=>document.querySelector('audio').sinkId),device.deviceId);
    await page.click('#play');
    await page.waitForFunction(()=>!document.querySelector('audio').paused&&document.querySelector('audio').currentTime>.1);
    result.route=await page.evaluate(()=>({mode:route.getStatus().mode,status:route.getStatus().status,label:route.getStatus().label,receiver:route.getStatus().receiver}));
    result.observations=[];
    for(let i=0;i<3;i++){
      const response=await fetch('http://127.0.0.1:8765/api/audio/output-status');
      const status=await response.json();
      result.observations.push({default_device:status.devices.find(d=>d.default)?.name,sessions:status.browser_sessions.filter(x=>x.device_name===device.label).map(x=>({application:x.application,device_name:x.device_name,active:x.active,peak:x.peak,muted:x.muted,volume_percent:x.volume_percent}))});
    }
    result.signal_verified=result.observations.some(o=>o.sessions.some(x=>x.active&&!x.muted&&x.peak>.001));
    await page.click('#pause');assert.equal(await page.evaluate(()=>document.querySelector('audio').paused),true);
    await page.evaluate(()=>route.setLocal());
    result.restored_to_local=await page.evaluate(()=>route.getStatus().mode==='local'&&document.querySelector('audio').sinkId==='');
    assert.equal(result.restored_to_local,true);assert.equal(result.signal_verified,true,'selected real Windows endpoint should show actual test speech signal');
    await context.close();result.result='PASS';
  }catch(e){result.result='FAIL';result.error=e.message;throw e;}
  finally{fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'hardware-test.json'),JSON.stringify(result,null,2));await browser.close();server.close();console.log(JSON.stringify(result,null,2));}
})().catch(e=>{console.error(e.message);server.close();process.exitCode=1;});
