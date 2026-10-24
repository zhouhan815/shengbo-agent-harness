'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/continuous-player.js','utf8');
let nodes=[],fetches=0,wall=0;
class Node{connect(){}disconnect(){}start(when,offset){this.when=when;this.offset=offset;this.started=true;}stop(when){this.stopAt=when;}}
class Context{constructor(options){this.options=options;this.sinkId=options?.sinkId;this.currentTime=0;this.sampleRate=32000;}createMediaStreamDestination(){return {stream:{}};}createBufferSource(){const n=new Node();nodes.push(n);return n;}createBuffer(){return {duration:.00003125};}decodeAudioData(){return Promise.resolve({duration:20});}resume(){return Promise.resolve();}close(){return Promise.resolve();}}
const ctx=vm.createContext({console,performance:{now:()=>wall},AudioContext:Context,setInterval:()=>1,clearInterval:()=>{},queueMicrotask,fetch:async()=>{fetches++;return {ok:true,arrayBuffer:async()=>new ArrayBuffer(1)};}});
vm.runInContext(source+'\nthis.Player=ContinuousTrackPlayer;',ctx);
const media={paused:true,play:async()=>{media.paused=false;},pause:()=>{media.paused=true;}};
const trace=[],acks=[],url='/audio/'+('a'.repeat(64))+'.wav';
const state=(epoch,start,end,status='ready')=>({epoch,status,position:0,current:{id:'s'+epoch},audio:{url,duration:end-start,continuous_track:{start,end}}});
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{
 const p=new ctx.Player(media,async()=>{},(e,d)=>trace.push({e,d}),e=>{throw e;});
 await p.sync(state(1,0,3),(e,d)=>acks.push({epoch:1,e,d}));
 assert.equal(p.context.options.sinkId.type,'none','MediaStream renderer uses a hardware-independent clock');
 assert.equal(acks.filter(x=>x.e==='playing').length,0,'scheduled source alone is not proof of playback');
 p.context.currentTime=.05;p.tick();assert.equal(acks.at(-1).e,'playing','acknowledge only after clock advances');
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
 // Explicit restart within the same first sentence must rewind, not retain.
 await p.sync({...state(3,0,3),audio:null,playback_seek_revision:1},()=>{});
 assert.equal(p.physical.running,false,'stop old sound immediately while deliberate seek is preparing');
 const restart={...state(3,0,3),playback_seek_revision:1};
 await p.sync(restart,()=>{});const restarted=p.physical.node;p.context.currentTime+=.7;
 await p.sync({...restart,epoch:4,playback_seek_revision:2},()=>{});
 assert.notEqual(p.physical.node,restarted);assert.equal(p.physical.offset,0);
 const retained=p.physical.node;p.context.currentTime+=3.01;
 await p.sync({...state(5,3,7),playback_seek_revision:2},()=>{});
 assert.equal(p.physical.node,retained,'after a deliberate seek ordinary sentence boundaries remain continuous');
 p.dispose();assert.equal(media.srcObject,null);
 const waitingMedia={paused:true,play:()=>new Promise(()=>{}),pause(){this.paused=true;}};
 const waitingPlayer=new ctx.Player(waitingMedia,async()=>{},()=>{},e=>{throw e;});
 await Promise.race([waitingPlayer.sync(state(8,0,3),()=>{}),new Promise((_,reject)=>setTimeout(()=>reject(Error('MediaStream first-sample deadlock')),200))]);
 assert.ok(waitingPlayer.physical.node.started);waitingPlayer.dispose();
 const failures=[],stalledAcks=[];
 const frozenMedia={paused:true,play(){this.paused=false;return Promise.resolve();},pause(){this.paused=true;}};
 const frozen=new ctx.Player(frozenMedia,async()=>{},()=>{},e=>failures.push(e));
 await frozen.sync(state(30,0,3),(event)=>stalledAcks.push(event));wall+=3001;frozen.tick();
 assert.equal(failures.length,1);assert.equal(failures[0].name,'AudioClockError');
 assert.equal(stalledAcks.includes('playing'),false);assert.equal(frozen.physical.running,false);
 const deadContext=frozen.context;await frozen.unlock(true);assert.notEqual(frozen.context,deadContext,'explicit retry recreates a frozen rendering context');
 frozen.dispose();
 const blockedMedia={paused:true,play:()=>new Promise(()=>{}),pause(){this.paused=true;}};
 const blocked=new ctx.Player(blockedMedia,async()=>{},()=>{},e=>failures.push(e));
 await blocked.sync(state(31,0,3),()=>{});blocked.context.currentTime=1;wall+=3001;blocked.tick();
 assert.equal(failures.length,2,'advancing clock with a paused output is also a failed start');blocked.dispose();
 class DirectContext extends Context{
   constructor(options){super(options);this.destination={hardware:true};}
   async setSinkId(id){this.sinkId=id;}
 }
 ctx.AudioContext=DirectContext;
 const directAcks=[],directMedia={paused:true,sinkId:'earphones',play(){throw Error('direct audio must not use MediaStream HTML playback');},pause(){this.paused=true;}};
 const direct=new ctx.Player(directMedia,async()=>{},()=>{},e=>{throw e;});
 await direct.sync(state(40,0,3),event=>directAcks.push(event));
 assert.equal(direct.directOutput,true);assert.equal(direct.context.sinkId,'earphones');
 assert.equal(directMedia.srcObject,undefined,'direct mode never adds a second MediaStream output');
 direct.context.currentTime=.1;direct.tick();assert.ok(directAcks.includes('playing'));
 await direct.outputRoute.setSinkId('speaker');assert.equal(direct.context.sinkId,'speaker','runtime route changes reach the actual audio graph');
 direct.dispose();
 console.log('PASS continuous source retained, single fetch, offset resume, predecoded insertion, exact scheduled cut, frozen boundary choice, cleanup');
})().catch(e=>{console.error(e);process.exitCode=1;});
