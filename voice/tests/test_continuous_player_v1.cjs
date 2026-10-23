'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/continuous-player.js','utf8');
let nodes=[],fetches=0;
class Node{connect(){}disconnect(){}start(when,offset){this.when=when;this.offset=offset;this.started=true;}stop(when){this.stopAt=when;}}
class Context{constructor(){this.currentTime=0;this.sampleRate=32000;}createMediaStreamDestination(){return {stream:{}};}createBufferSource(){const n=new Node();nodes.push(n);return n;}createBuffer(){return {duration:.00003125};}decodeAudioData(){return Promise.resolve({duration:20});}resume(){return Promise.resolve();}close(){return Promise.resolve();}}
const ctx=vm.createContext({console,AudioContext:Context,setInterval:()=>1,clearInterval:()=>{},queueMicrotask,fetch:async()=>{fetches++;return {ok:true,arrayBuffer:async()=>new ArrayBuffer(1)};}});
vm.runInContext(source+'\nthis.Player=ContinuousTrackPlayer;',ctx);
const media={paused:true,play:async()=>{media.paused=false;},pause:()=>{media.paused=true;}};
const trace=[],acks=[],url='/audio/'+('a'.repeat(64))+'.wav';
const state=(epoch,start,end,status='ready')=>({epoch,status,position:0,current:{id:'s'+epoch},audio:{url,duration:end-start,continuous_track:{start,end}}});
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{
 const p=new ctx.Player(media,async()=>{},(e,d)=>trace.push({e,d}),e=>{throw e;});
 await p.sync(state(1,0,3),(e,d)=>acks.push({epoch:1,e,d}));
 const physical=p.physical.node;p.context.currentTime=3.01;p.alarm.onended();
 assert.equal(acks.at(-1).e,'ended');assert.equal(acks.at(-1).d.boundary_choice,null);
 await p.sync(state(2,3,7),(e,d)=>acks.push({epoch:2,e,d}));
 assert.equal(p.physical.node,physical,'normal sentence boundary must retain physical source');assert.equal(fetches,1);
 p.context.currentTime=4;p.pause();assert.equal(media.paused,true);
 await p.sync(state(2,3,7),(e,d)=>acks.push({epoch:2,e,d}));assert.equal(p.physical.offset,4,'resume must keep absolute track offset');
 const s={...state(2,3,7,'playing'),insertion:{pending:{queue_id:'interaction',prepared_audio:{url:'/audio/'+('b'.repeat(64))+'.wav'}}}};
 await p.sync(s,(e,d)=>acks.push({epoch:2,e,d}));await flush();
 assert.equal(p.cut.queueId,'interaction');assert.equal(p.physical.node.stopAt,7);
 p.context.currentTime=7;p.alarm.onended();assert.equal(acks.at(-1).d.boundary_choice,'interaction');
 p.dispose();assert.equal(media.srcObject,null);
 console.log('PASS continuous source retained, single fetch, offset resume, predecoded insertion, exact scheduled cut, frozen boundary choice, cleanup');
})().catch(e=>{console.error(e);process.exitCode=1;});
