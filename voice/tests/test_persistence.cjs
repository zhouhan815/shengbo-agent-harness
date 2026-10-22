const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup() {
  const elements = {name:{value:'房间A'},version_count:{value:'2'},loop:{checked:false},tts_provider:{value:'system'},tts_model:{value:'speech-2.8-turbo'}};
  const nodes = {'config-form':{elements},'save-status':{textContent:''},'page-title':{textContent:''}};
  const storage = new Map(), events = {}, requests = [];
  const c = vm.createContext({
    Set, Promise, JSON, Error, encodeURIComponent,
    $:id=>nodes[id], localStorage:{setItem:(k,v)=>storage.set(k,v),getItem:k=>storage.get(k)||null,removeItem:k=>storage.delete(k)},
    setTimeout:fn=>fn,clearTimeout:()=>{},window:{addEventListener:(k,fn)=>events[k]=fn},
    document:{querySelectorAll:()=>[]}, location:{href:''}, dirty:true, roomId:'roomA',
    roomConfig:{id:'roomA',script_order:['s1']},chosenProducts:new Set(['p1']),order:['s1'],removedScripts:new Set(),
    library:{rooms:[{id:'roomA'}]},ttsForm:()=>({tts_provider:'system',tts_voice:'Huihui',tts_model:'speech-2.8-turbo'}),
    roomScripts:()=>c.order.map(id=>({id})),renderRoom:()=>{},notify:()=>{},updateVoiceChoices:()=>{},saveRoom:()=>{},
    enterRoom:async id=>{c.roomId=id;},markDirty:()=>{c.dirty=true;vm.runInContext('queueRoomSave()',c)},
    api:(endpoint,method,payload)=>new Promise(resolve=>requests.push({endpoint,method,payload,resolve})),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/persistence.js'),'utf8'),c);
  return {c,storage,events,requests,elements,nodes};
}
async function tick() { await Promise.resolve(); await Promise.resolve(); }
async function main() {
  const a = setup(); vm.runInContext('queueRoomSave()',a.c);
  assert.equal(JSON.parse(a.storage.get('shengbo-room-draft:roomA')).name,'房间A');
  console.log('PASS close-before-save keeps a recoverable local draft');

  const first = a.c.saveRoom(); await tick();
  assert.deepEqual([...a.requests[0].payload.product_ids],['p1']);
  a.requests[0].resolve({id:'roomA',name:'房间A',script_order:['s1']}); await first;
  assert.equal(a.c.dirty,false); assert.equal(a.storage.has('shengbo-room-draft:roomA'),false);
  assert.equal(a.nodes['save-status'].textContent,'已保存到本机');
  console.log('PASS successful server save clears the draft only after confirmation');

  a.c.dirty=true; const old = a.c.saveRoom(); await tick();
  a.c.order=['s1','s2']; vm.runInContext('queueRoomSave()',a.c);
  a.requests[1].resolve({id:'roomA',name:'房间A',script_order:['s1']}); await old;
  assert.equal(a.c.dirty,true); assert.deepEqual(a.c.order,['s1','s2']);
  assert.deepEqual(JSON.parse(a.storage.get('shengbo-room-draft:roomA')).script_order,['s1','s2']);
  console.log('PASS an older network response cannot overwrite a newer edit');

  const latest = a.c.saveRoom(); await tick();
  assert.deepEqual([...a.requests[2].payload.script_order],['s1','s2']);
  a.requests[2].resolve({id:'roomA',name:'房间A',script_order:['s1','s2']}); await latest;
  assert.equal(a.c.dirty,false);
  console.log('PASS serialized saves commit the latest script order');

  const b=setup(); b.storage.set('shengbo-room-draft:roomA',JSON.stringify({name:'恢复的配置',product_ids:['p1'],script_order:['s1','s3'],removed_script_ids:[],version_count:3,loop:true,tts_provider:'system',tts_voice:'Huihui',tts_model:'speech-2.8-turbo'}));
  await b.c.enterRoom('roomA');
  assert.equal(b.elements.name.value,'恢复的配置'); assert.equal(b.elements.version_count.value,3);
  assert.deepEqual([...b.c.order],['s1','s3']); assert.equal(b.c.dirty,true);
  console.log('PASS entering a room restores and queues its unsynced configuration');

  b.elements.name.value='关闭前的最后修改'; b.events.pagehide();
  assert.equal(JSON.parse(b.storage.get('shengbo-room-draft:roomA')).name,'关闭前的最后修改');
  console.log('PASS closing the page captures the latest local form values');

  const d=setup();
  d.c.readRoomCommerce=()=>({scene:'本场周年庆',coupon:{mode:'none'},product_overrides:{p1:{mode:'price',price:129}}});
  vm.runInContext('queueRoomSave()',d.c);
  assert.equal(JSON.parse(d.storage.get('shengbo-room-draft:roomA')).commerce.product_overrides.p1.price,129);
  const businessSave=d.c.saveRoom();await tick();
  assert.equal(d.requests[0].payload.commerce.scene,'本场周年庆');
  d.requests[0].resolve({id:'roomA',name:'房间A',script_order:['s1'],commerce:d.requests[0].payload.commerce});await businessSave;
  console.log('PASS room commerce follows both local drafts and serialized server saves');

  const e=setup();let recovered;
  e.c.loadCommerceEditor=value=>{recovered=value;};
  e.storage.set('shengbo-room-draft:roomA',JSON.stringify({name:'恢复业务配置',product_ids:['p1'],script_order:['s1'],version_count:2,loop:false,tts_provider:'system',tts_voice:'Huihui',tts_model:'speech-2.8-turbo',commerce:{scene:'恢复专场',product_overrides:{p1:{mode:'price',price:119}}}}));
  await e.c.enterRoom('roomA');
  assert.equal(recovered.product_overrides.p1.price,119);
  console.log('PASS restoring a closed room also restores its unsynced commerce configuration');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
