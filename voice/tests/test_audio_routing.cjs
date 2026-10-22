'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const scope=vm.createContext({console});vm.runInContext(fs.readFileSync('web/audio-routing.js','utf8'),scope);
const Routing=scope.ShengboAudioRouting;
const key='shengbo-audio-routing-v1';
const local={kind:'audiooutput',deviceId:'realtek',label:'扬声器 Realtek'},cable={kind:'audiooutput',deviceId:'cable',label:'CABLE Input (VB-Audio Virtual Cable)'};
class Audio{constructor(){this.paused=true;this.sinkId='';this.playCount=0;this.time=1.25;}pause(){this.paused=true;}async play(){this.paused=false;this.playCount++;}async setSinkId(id){if(this.reject)throw Object.assign(Error('denied'),{name:'NotAllowedError'});this.sinkId=id;}}
function fixture(pref,initialDevices=[local,cable]){const saved=new Map(pref?[[key,JSON.stringify(pref)]]:[]),audio=new Audio(),faults=[],media={list:initialDevices,async enumerateDevices(){return this.list;},addEventListener(event,cb){this.changed=cb;},removeEventListener(){this.changed=null;}};const storage={getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v)};const route=Routing.create({audio,mediaDevices:media,storage,onFault:e=>faults.push(e.message),mayPlay:()=>true});return{route,audio,media,saved,faults};}
(async()=>{
 let checks=0;const check=(condition,message)=>{assert.ok(condition,message);checks++;};
 let x=fixture();await x.route.ready;check(x.route.getStatus().mode==='local'&&x.audio.sinkId==='','first visit never changes default output');
 x.audio.paused=false;await x.route.selectDevice('cable');check(x.audio.sinkId==='cable'&&!x.audio.paused&&x.audio.time===1.25,'route only changes this media element and preserves current playback');
 check(x.route.getStatus().receiver.type==='音频输入采集'&&x.route.getStatus().receiver.name==='CABLE Output (VB-Audio Virtual Cable)','VB-CABLE shows the recording endpoint, not playback input');
 check(JSON.parse(x.saved.get(key)).deviceId==='cable','successful target is saved locally');
 const reload=fixture(JSON.parse(x.saved.get(key)));await reload.route.ready;check(reload.audio.sinkId==='cable'&&reload.route.getStatus().status==='ready'&&reload.audio.playCount===0,'refresh restores sink without autoplay');
 x.media.list=[local];await x.route.refresh();check(x.audio.paused&&x.route.getStatus().status==='unavailable'&&x.faults.length===1,'unplugging stops this voice output');
 await assert.rejects(()=>x.route.ensureReady(),/尚未就绪/);checks++;
 check(x.audio.sinkId==='cable'&&x.route.getStatus().mode==='external','missing device never silently falls back to speakers');
 await x.route.setLocal();check(x.audio.sinkId===''&&x.route.getStatus().mode==='local','explicit opt out returns to default');
 x.audio.reject=true;await assert.rejects(()=>x.route.selectDevice('realtek'));check(x.route.getStatus().mode==='local','failed selection does not claim an external connection');
 x=fixture({version:1,mode:'external',deviceId:'previous-id',label:cable.label});await x.route.ready;check(x.audio.sinkId==='cable','changed browser IDs restore only with unique exact label');
 const duplicate=fixture({version:1,mode:'external',deviceId:'missing',label:cable.label},[cable,{...cable,deviceId:'duplicate'}]);await duplicate.route.ready;check(duplicate.route.getStatus().status==='unavailable','duplicate names are not guessed');
 const unsupportedAudio={paused:true,pause(){}};const unsupported=Routing.create({audio:unsupportedAudio,mediaDevices:{enumerateDevices:async()=>[]},storage:{getItem:()=>null}});await unsupported.ready;await unsupported.ensureReady();check(!unsupported.getStatus().supported,'unsupported browser can still play the default route');
 x=fixture();await x.route.ready;let calls=0,stops=0;x.media.getUserMedia=async()=>{calls++;return {getTracks:()=>[{stop(){stops++;}}]};};await x.route.authorize();check(calls===1&&stops===1,'manual enumeration authorization immediately closes microphone tracks');
 x.media.getUserMedia=async()=>{throw Object.assign(Error('no'),{name:'NotAllowedError'});};await x.route.authorize();check(x.route.getStatus().mode==='local'&&/未获授权/.test(x.route.getStatus().message),'denied permission preserves original local output');
 check(Routing.receiver('Steam Streaming Speakers').type==='音频输出采集','generic playback endpoint gives OBS output-capture instructions');
 check(x.audio.playCount===0,'opening/authorizing the routing panel never starts playback');
 const racing=fixture();await racing.route.ready;let finish;
 racing.audio.setSinkId=id=>new Promise(resolve=>{finish=()=>{racing.audio.sinkId=id;resolve();};});
 const switching=racing.route.selectDevice('cable');await new Promise(resolve=>setImmediate(resolve));
 racing.media.list=[local];await racing.route.refresh();finish();await switching;
 check(racing.route.getStatus().status==='unavailable'&&racing.audio.paused,'an unplug event during asynchronous switching is checked again after the switch');
 x.route.dispose();check(!x.media.changed,'dispose removes the device listener');
 console.log(checks+' audio-routing lifecycle checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
