/* Node-only adapter regression: no browser, HTTP, platform, LLM or TTS. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');

class Element {
  constructor(tag){this.tagName=tag.toLowerCase();this.children=[];this.parentNode=null;this.dataset={};this.attributes={};this.style={};this.listeners={};this.value='';this.checked=false;this.disabled=false;this.scrollTop=0;this.scrollHeight=0;this.textWrites=0;this._text='';this.paused=true;this.muted=false;this.volume=1;this.currentTime=0;}
  get lastChild(){return this.children.at(-1);}
  get isConnected(){return this.connectedRoot||!!this.parentNode?.isConnected;}
  set textContent(value){this._text=String(value??'');this.children=[];this.textWrites++;}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
  append(...nodes){for(const node of nodes){node.remove();node.parentNode=this;this.children.push(node);}}
  replaceChildren(...nodes){this.children.forEach(n=>n.parentNode=null);this.children=[];this._text='';this.append(...nodes);}
  remove(){if(this.parentNode){const siblings=this.parentNode.children;siblings.splice(siblings.indexOf(this),1);this.parentNode=null;}}
  setAttribute(key,value){this.attributes[key]=String(value);}
  getAttribute(key){return this.attributes[key]??null;}
  addEventListener(name,handler){(this.listeners[name]??=[]).push(handler);}
  async emit(name){for(const handler of this.listeners[name]||[])await handler({target:this});}
  matches(selector){
    if(selector==='progress')return this.tagName==='progress';
    const match=/^\[([^=\]]+)(?:=(?:"([^"]*)"|([^\]]+)))?\](:checked)?$/.exec(selector);
    if(!match)return false;
    const key=match[1],expected=match[2]??match[3],actual=key.startsWith('data-')?this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]:this[key]??this.attributes[key];
    return actual!==undefined&&(expected===undefined||String(actual)===expected)&&(!match[4]||this.checked);
  }
  querySelectorAll(selector){return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)]);}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}

async function main(){
  const host=new Element('div');host.connectedRoot=true;
  const document={hidden:false,createElement:tag=>new Element(tag)};
  const scope=vm.createContext({document,console,location:{origin:'http://isolated.local'},setInterval:()=>1,clearInterval(){},setTimeout(){},crypto:{randomUUID:()=> 'test-action'},URL,Blob});scope.window=scope;
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/workbench-execution.js'),'utf8'),scope);
  let state={status:'paused',epoch:8,session_id:'live1',index:0,current:{queue_id:'q1',product_name:'拖鞋',product_id:'p1',script_id:'v1',text:'当前句',version:'第1版',ordinal:1,total_sentences:2},queue:[{queue_id:'q1',product_name:'拖鞋',product_id:'p1',script_id:'v1',text:'当前句',version:'第1版',ordinal:1,total_sentences:2},{queue_id:'q2',product_name:'拖鞋',product_id:'p1',script_id:'v1',text:'第二句',version:'第1版',ordinal:2,total_sentences:2}],connected_player:false,logs:[],audience_events:[],position:1,audio:{duration:5}};
  const calls=[],clone=value=>JSON.parse(JSON.stringify(value));
  const api={async request(url,body,options){calls.push({url,body,options});if(url==='/api/library')return {rooms:[{id:'r1'},{id:'r2'}],scripts:[]};if(url.endsWith('/state'))return clone(state);if(url.endsWith('/control')){state.status=body.action==='resume'?'playing':state.status;return {ok:true};}if(url.includes('/live-source'))return {config:{url:'https://live.douyin.com/123456',purpose:'external_test',enabled:false},status:{state:'disconnected',metrics:{}},events:[],next_cursor:0};if(url.endsWith('/reply-knowledge'))return {products:[{id:'p1',name:'拖鞋'}],documents:[],chunk_count:0};if(url.endsWith('/reply-sessions'))return {sessions:[]};if(url.startsWith('/api/jobs?'))return {jobs:[]};throw Error('Unexpected API route '+url);}};
  const execution=scope.ShengboWorkbenchExecution.mount({container:host,api});
  const select=selector=>host.querySelector(selector);
  let checks=0;const check=(condition,message)=>{assert.ok(condition,message);checks++;};
  const events=last=>Array.from({length:120},(_,i)=>({id:'event'+(last-119+i),sequence:last-119+i,type:'comment',source:'simulated',text:'模拟评论 '+(last-119+i)}));
  const logs=last=>Array.from({length:120},(_,i)=>({id:'log'+(last-119+i),message:'执行 '+(last-119+i)}));
  state.audience_events=events(120);state.logs=logs(120);
  await execution.open({room_id:'r1',view:'broadcast'});
  let audience=select('[data-broadcast-audience-events]'),records=select('[data-broadcast-execution-log]');
  const queue=select('[data-broadcast-queue]').children[0],transcript=select('[data-broadcast-transcript]');transcript.scrollTop=99;audience.parentNode.open=true;audience.scrollTop=37;
  check(audience.textContent.includes('模拟评论 120'),'initial bounded comments appear');
  check(audience.textContent.includes('模拟评论 21')&&!audience.textContent.includes('模拟评论 20"'),'audience renderer keeps the latest 100 of the 120 cached events');
  state.audience_events=events(150);state.logs=logs(150);await execution.refresh();
  check(audience.textContent.includes('模拟评论 150')&&!audience.textContent.includes('模拟评论 50"'),'comment 150 refreshes after the fixed 120-element backend cap');
  check(records.textContent.includes('执行 150')&&!records.textContent.includes('执行 100"'),'rolling execution log refreshes when length remains constant');
  check(select('[data-broadcast-queue]').children[0]===queue&&transcript.scrollTop===99,'incoming events do not rebuild the pending queue or move the transcript');
  check(select('[data-broadcast-audience-events]')===audience&&audience.parentNode.open&&audience.scrollTop===37,'refresh preserves expanded event details and their read position');
  check(select('[data-exec-action=resume]').disabled===false,'paused audio continues receiving events while resume remains available');
  state.audience_events.at(-1).text='同一事件的修正内容';state.logs.at(-1).message='同一执行记录的修正内容';await execution.refresh();
  check(audience.textContent.includes('同一事件的修正内容')&&records.textContent.includes('同一执行记录的修正内容'),'same ID/sequence with corrected content refreshes');
  const eventWrites=audience.textWrites,logWrites=records.textWrites;await execution.refresh();
  check(audience.textWrites===eventWrites&&records.textWrites===logWrites,'unchanged event payload does not rewrite the detail panes');
  state.index=1;state.current=state.queue[1];await execution.refresh();
  check(select('[data-broadcast-queue]').children[0]!==queue&&select('[data-broadcast-audience-events]')===audience,'changing playback position rebuilds the queue without replacing event history');

  await execution.open({room_id:'r1',view:'comments'});select('[name=comment]').value='尚未发送的问题';await select('[name=comment]').emit('input');
  check(execution.hasDirty(),'unsent comment remains tracked for a draft warning');
  await execution.open({room_id:'r1',view:'jobs'});check(execution.hasDirty(),'leaving the comment view preserves its draft flag');
  await execution.open({room_id:'r2',view:'broadcast'});check(!execution.hasDirty(),'another internal room does not inherit a comment draft flag');
  await execution.open({room_id:'r1',view:'comments'});check(select('[name=comment]').value==='尚未发送的问题'&&execution.hasDirty(),'returning to the original room restores the unsent question');
  await execution.open({room_id:'r1',view:'platform'});select('[name=source_room]').value='';await select('[name=source_room]').emit('input');
  await execution.open({room_id:'r2',view:'platform'});check(!execution.hasDirty()&&select('[name=source_room]').value.endsWith('/123456'),'source drafts are isolated from another room and saved source still loads');
  await execution.open({room_id:'r1',view:'platform'});check(select('[name=source_room]').value===''&&execution.hasDirty(),'an intentionally cleared source input survives navigation and polling');
  await execution.open({room_id:'r2',view:'broadcast'});select('[name=insert]').value='尚未发送的插播';await select('[name=insert]').emit('input');
  await execution.open({room_id:'r2',view:'jobs'});check(execution.hasDirty(),'a hidden insertion draft remains tracked without becoming a playback-content edit');
  await execution.open({room_id:'r1',view:'broadcast'});check(select('[name=insert]').value===''&&execution.hasDirty(),'room one keeps only its own comment/source drafts');
  await execution.open({room_id:'r2',view:'broadcast'});check(select('[name=insert]').value==='尚未发送的插播','returning restores the insertion draft in its original room');
  check(calls.every(call=>call.body===undefined),'opening, polling and draft navigation perform no write or paid operation');
  // Returning to a paused room must reconnect local audio before server resume.
  let rejectUnlock=false;const soundCalls=[];
  scope.VoicePlayer=class{
    constructor(){this.identity='local';this.state={player_id:'local',session_id:'live1'};}
    async unlock(){soundCalls.push('unlock');if(rejectUnlock)throw Error('test sound permission denied');}
    async connect(){soundCalls.push('connect');state.connected_player=true;state.player_id='local';}
    async claim(){soundCalls.push('claim');}disconnect(){}
  };
  state.status='paused';state.connected_player=false;state.player_id=null;
  await execution.refresh();const firstCall=calls.length;
  await select('[data-exec-action=resume]').emit('click');
  check(soundCalls.join(',')==='unlock,connect','fresh-page continue unlocks and connects its sound player');
  check(calls.slice(firstCall).some(c=>c.body?.action==='resume')&&state.status==='playing','resume follows successful sound connection');
  state.status='paused';state.connected_player=true;state.player_id='obs-owner';state.output_source='obs';
  await execution.refresh();soundCalls.length=0;await select('[data-exec-action=resume]').emit('click');
  check(soundCalls.length===0&&state.status==='playing','connected OBS owner is resumed without stealing its sound');
  state.status='paused';state.connected_player=false;rejectUnlock=true;
  await execution.refresh();const failedCall=calls.length;await select('[data-exec-action=resume]').emit('click');
  check(!calls.slice(failedCall).some(c=>c.body?.action==='resume')&&state.status==='paused','sound unlock failure must not silently resume the server');
  rejectUnlock=false;state.status='paused';state.connected_player=false;state.player_id=null;
  await execution.refresh();soundCalls.length=0;const restartCalls=calls.length;
  await select('[data-exec-action=restart-version]').emit('click');
  check(soundCalls.join(',')==='unlock,connect','version restart unlocks and connects audio on a fresh page');
  const restartWrites=calls.slice(restartCalls).filter(c=>c.body?.action);
  check(restartWrites.length===1&&restartWrites[0].body.action==='restart_version','version restart does not briefly resume the old position');
  check(restartWrites[0].body.expected_epoch===null&&restartWrites[0].body.expected_script_id==='v1'&&restartWrites[0].body.expected_session_id==='live1','restart targets observed version and session');
  execution.dispose();check(host.children.length===0,'adapter disposal removes the view');
  process.stdout.write(JSON.stringify({passed:checks,scope:'Node VM and minimal DOM; no browser/network/LLM/TTS/platform or production data'},null,2)+'\n');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
