const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = vm.createContext({console, setTimeout, clearTimeout, WebSocket:{OPEN:1}});
vm.runInContext(fs.readFileSync('web/audio-player.js','utf8')+'\nthis.VoicePlayer=VoicePlayer;',context);
class Audio {
  constructor(){this.paused=true;this.readyState=4;this.attrs={};this.currentTime=0;this.ended=false;}
  set src(value){this.attrs.src=value;} get src(){return this.attrs.src;}
  getAttribute(key){return this.attrs[key]||null;}
  removeAttribute(key){delete this.attrs[key];}
  load(){}
  pause(){this.paused=true;}
  play(){this.paused=false;return Promise.resolve();}
}
const state={player_id:'owner',epoch:1,status:'paused',current:{attempt_id:'try1'},audio:{url:'/audio/test.wav'}};
(async()=>{
  const audio=new Audio(),p=new context.VoicePlayer(audio),messages=[];
  p.identity='owner';p.ws={readyState:1,send:value=>messages.push(JSON.parse(value))};
  p.sync(state);assert.equal(audio.paused,true);
  audio.currentTime=1.25;p.sync({...state,status:'ready'});await new Promise(resolve=>setImmediate(resolve));
  p.sync(state);assert.equal(audio.paused,true);assert.equal(audio.currentTime,1.25,'pause preserves the current media position');
  p.sync({...state,status:'ready'});await new Promise(resolve=>setImmediate(resolve));assert.equal(audio.paused,false);assert.equal(audio.currentTime,1.25,'resume continues the same sentence without rewinding');
  // Reproduce the gesture/resume race: media starts while the server is paused.
  audio.paused=false;p.sync({...state,status:'ready'});
  assert.equal(messages.at(-1).event,'playing');
  const oldEnd=audio.onended;
  p.sync({...state,epoch:2,status:'paused',current:{attempt_id:'try2'}});
  const before=messages.length;audio.ended=true;oldEnd();
  assert.equal(messages.length,before,'stale callback must not acknowledge the new sentence');
  p.sync({...state,epoch:3,status:'ready',player_id:'obs',output_source:'obs'});
  assert.equal(audio.paused,true,'OBS takeover stops local audio');
  assert.equal(p.audio,audio,'the unlocked media element is never replaced');
  p.state={status:'completed',current:null};audio.src='/audio/old.wav';
  await p.unlock();assert.equal(audio.src,'/assets/unlock.wav');assert.equal(audio.paused,true,'unlock never replays old product audio');
  const reconnectAudio=new Audio(),reconnect=new context.VoicePlayer(reconnectAudio);reconnect.identity='owner';reconnect.ws={readyState:1,send:()=>{}};
  reconnect.sync({...state,status:'ready',position:1.6});assert.equal(reconnectAudio.currentTime,1.6,'new sound owner seeks to the server-observed position');
  const unavailable=new Audio(),gated=new context.VoicePlayer(unavailable,()=>{},{beforePlay:async()=>{throw Object.assign(Error('设备断开'),{code:'OUTPUT_DEVICE_UNAVAILABLE'});}}),failures=[];
  gated.identity='owner';gated.ws={readyState:1,send:value=>failures.push(JSON.parse(value))};
  gated.sync({...state,status:'ready'});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(unavailable.paused,true,'the player must not emit audio when the selected route is unavailable');
  assert.equal(failures.at(-1).event,'failed','the backend receives an actual playback failure rather than silently advancing');
  const expiredAudio=new Audio(),expired=new context.VoicePlayer(expiredAudio),expiredMessages=[];
  expired.identity='owner';expired.ws={readyState:1,send:value=>expiredMessages.push(JSON.parse(value))};
  const stale={...state,status:'ready',current:{attempt_id:'expired',is_welcome:true,welcome_expires_at:Date.now()/1000-1}};
  expired.sync(stale);await new Promise(resolve=>setImmediate(resolve));
  assert.equal(expiredAudio.paused,true,'expired welcome never begins playback');
  assert.equal(expiredMessages.at(-1).event,'welcome_expired');
  expired.sync(stale);assert.equal(expiredMessages.length,1,'expiry is reported once for this attempt');
  await expired.unlock();assert.equal(expiredAudio.src,'/assets/unlock.wav','unlock cannot replay an expired welcome');
  const runningAudio=new Audio(),running=new context.VoicePlayer(runningAudio);running.identity='owner';running.ws={readyState:1,send:()=>{}};
  running.sync({...stale,epoch:4,current:{...stale.current,welcome_started:true}});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(runningAudio.paused,false,'a welcome already started can finish past its admission TTL');
  console.log('15 audio-player regression checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
