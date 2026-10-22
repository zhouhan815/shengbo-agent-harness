/* Real headless browser UI tests against a private mock HTTP origin.
 * No application process, database, OAuth store or provider is used. */
const fs=require('fs'),path=require('path'),os=require('os'),http=require('http'),crypto=require('crypto'),assert=require('assert/strict');
const {chromium}=require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const WEB=path.resolve(__dirname,'../web');
const sorted=v=>Array.isArray(v)?v.map(sorted):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,sorted(v[k])])):v;
const hash=v=>'sha256:'+crypto.createHash('sha256').update(JSON.stringify(sorted(v))).digest('hex');
const ref=(kind,id,revision=1)=>({kind,id,revision,content_hash:hash({kind,id,revision}),...(kind==='product'||kind==='config'||kind==='preparation'?{}:{schema_version:'PRELIVE-2.3'})});
function fixture(){
 const A=ref('product','product-A'),B=ref('product','product-B');
 const scripts=['A1','A2','B1'].map(id=>({ref:ref('script_revision','script-'+id),body:{product_id:id[0]==='A'?A.id:B.id,version_number:Number(id[1]),script_revision:1},modules:{opening_long:{sentences:[{id:'sentence-'+id,text:id+'原始句。',spoken_text:id+'原始句。'}]}}}));
 const items=scripts.map((s,i)=>({occurrence_id:'occurrence-'+i,product_id:s.body.product_id,script_revision_ref:s.ref}));
 return {products:{items:[{ref:A,name:'商品A'},{ref:B,name:'商品B'}]},preparation:{ref:ref('preparation','prep-1'),body:{id:'prep-1',room_id:'room-1',goal:'隔离浏览器测试',status:'ready_to_adopt',generation_epoch:1,candidate_publication_ref:ref('publication','publication-1')}},preparation_details:{scripts,playlist:{ref:ref('playlist','playlist-1'),body:{items,loop_mode:'infinite',product_group_order:[A.id,B.id]}},draft:{ref:ref('draft','draft-1'),body:{draft:{theme:'本场主题'}}},asset_checklist:null},calls:[],previews:[],adopted:false,roomRevision:1,token:'mock-csrf',sessions:0,expireNext:false,differences:false};
}
let state=fixture();
function bump(){state.preparation.ref=ref('preparation','prep-1',state.preparation.ref.revision+1)}
function preview(spec){
 assert.match(spec.seed,/^[0-9a-f]{32}$/);
 const items=structuredClone(state.preparation_details.playlist.body.items),groups=spec.product_group_order;
 let out;
 if(spec.mode==='grouped')out=groups.flatMap(p=>items.filter(i=>i.product_id===p));
 else if(spec.mode==='interleave'){
  const q=Object.fromEntries(groups.map(p=>[p,items.filter(i=>i.product_id===p)]));out=[];let previous;
  while(Object.values(q).some(v=>v.length)){let choices=groups.filter(p=>q[p].length&&p!==previous);if(!choices.length)choices=groups.filter(p=>q[p].length);choices.sort((a,b)=>q[b].length-q[a].length||groups.indexOf(a)-groups.indexOf(b));previous=choices[0];out.push(q[previous].shift())}
 }else out=items.sort((a,b)=>hash(spec.seed+a.occurrence_id).localeCompare(hash(spec.seed+b.occurrence_id)));
 const result={proposed_items:out,resolved_spec:spec,preview_hash:hash({base:state.preparation_details.playlist.ref,spec,out})};state.previews.push(result);return result;
}
function review(){
 const pinned=ref('product','product-A'),head=ref('product','product-A',2);
 const currentness={blockers:[],differences:state.differences?[{pinned_ref:pinned,observed_head_ref:head,diff_hash:hash('diff'),differences:[{path:'/price',old:178,new:199}]}]:[],revision_choice_refs:[]};
 return {room_ref:ref('config','room-1',state.roomRevision),review_hash:hash({room:state.roomRevision,currentness}),adopted:state.adopted,currentness};
}
const html=`<!doctype html><html lang="zh"><meta charset="utf-8"><title>隔离播前工作台测试</title><link rel="stylesheet" href="/assets/style.css"><link rel="stylesheet" href="/assets/assistant.css"><link rel="stylesheet" href="/assets/assistant-host.css"><body><main><section class="view" id="view-room"></section></main><aside id="assistant-panel"></aside><button id="toggle-assistant" aria-expanded="false">助手</button><button id="open-prelive-workspace">本场准备</button><dialog id="prelive-workspace"><button id="close-prelive-workspace">关闭</button><p id="prelive-workspace-feedback" hidden></p><div id="prelive-workspace-content"></div></dialog><script>let roomId='room-1',chosenProducts=new Set(['product-A','product-B']);window.hostCalls=[];function refresh(){hostCalls.push('refresh');return Promise.resolve()}function enterRoom(id){hostCalls.push('enter:'+id);return Promise.resolve()}function view(id){hostCalls.push('view:'+id)}function connectMonitor(id){hostCalls.push('monitor:'+id)}window.ShengboAssistant={mount(options){window.hostOptions=options;return {_inspect(){return {conversation_id:'conversation-1'}},refresh(){hostCalls.push('assistant-refresh')},setSelection(value){hostCalls.push('selected:'+value.id)}}}};</script><script src="/assets/prelive-workspace.js"></script><script src="/assets/assistant-host.js"></script></body></html>`;
const server=http.createServer(async(req,res)=>{
 try{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname==='/'){res.setHeader('Content-Type','text/html; charset=utf-8');res.end(html);return}
  if(['/assets/prelive-workspace.js','/assets/assistant-host.js','/assets/style.css','/assets/assistant.css','/assets/assistant-host.css'].includes(url.pathname)){res.setHeader('Content-Type',url.pathname.endsWith('.css')?'text/css; charset=utf-8':'application/javascript; charset=utf-8');res.end(fs.readFileSync(path.join(WEB,path.basename(url.pathname))));return}
  if(url.pathname==='/favicon.ico'){res.writeHead(204).end();return}
  res.setHeader('Content-Type','application/json');
  if(url.pathname==='/api/assistant/v2/session'){state.sessions++;state.token='mock-csrf-'+state.sessions;res.end(JSON.stringify({csrf_token:state.token}));return}
  if(url.pathname==='/api/assistant/v2/workspace'){res.end(JSON.stringify({voice_snapshot:{preparation:state.preparation}}));return}
  let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw||'{}');
  state.calls.push({path:url.pathname,body,csrf:req.headers['x-csrf-token']});
  if(req.headers['x-csrf-token']!==state.token){res.writeHead(403);res.end(JSON.stringify({error:{code:'CSRF_REJECTED',message:'CSRF失效'}}));return}
  if(state.expireNext){state.expireNext=false;res.writeHead(401);res.end(JSON.stringify({error:{code:'LOCAL_SESSION_REQUIRED',message:'会话过期'}}));return}
  if(body.query){let result;
   if(body.query==='get_workspace_snapshot')result={products:state.products,preparation:state.preparation,preparation_details:state.preparation_details};
   else if(body.query==='preview_playlist_reorder'){assert.deepEqual(body.args.ref,state.preparation_details.playlist.ref);result=preview(body.args.spec)}
   else if(body.query==='create_review')result=review();else throw Error('unexpected query '+body.query);
   res.end(JSON.stringify(result));return;
  }
  const a=body.args;let data={};
  assert.equal(body.contract_version,'PRELIVE-2.3');assert.ok(body.mutation.operation_id);
  if(body.command==='save_playlist'){
   assert.equal(a.expected_revision,state.preparation.ref.revision);
   if(a.reorder_spec){const p=state.previews.at(-1);assert.equal(a.preview_hash,p.preview_hash);assert.deepEqual(a.items,p.proposed_items);assert.deepEqual(a.reorder_spec,p.resolved_spec)}
   assert.deepEqual(a.product_group_order,['product-A','product-B']);
   state.preparation_details.playlist={ref:ref('playlist','playlist-1',state.preparation_details.playlist.ref.revision+1),body:{items:a.items,loop_mode:a.loop_mode,product_group_order:a.product_group_order}};bump();data={preparation_ref:state.preparation.ref,playlist_ref:state.preparation_details.playlist.ref};
  }else if(body.command==='compile_playback_plan'){assert.equal(a.expected_revision,state.preparation.ref.revision);assert.deepEqual(a.playlist_ref,state.preparation_details.playlist.ref);bump();data={preparation_ref:state.preparation.ref};
  }else if(body.command==='patch_script'){
   const script=state.preparation_details.scripts.find(s=>s.ref.id===a.script_ref.id),module=script.modules[a.module_key];assert.equal(a.selected_text_hash,hash(module.sentences));assert.equal(a.expected_revision,state.preparation.ref.revision);module.sentences=a.sentences.map((text,i)=>({id:'new-'+i,text,spoken_text:text}));bump();data={preparation_ref:state.preparation.ref};
  }else if(body.command==='adopt_snapshot'){assert.equal(a.review_hash,review().review_hash);assert.equal(a.expected_room_revision,state.roomRevision);state.adopted=true;state.roomRevision++;data={room_ref:ref('config','room-1',state.roomRevision)};
  }else if(body.command==='start_prepared_room'){assert.equal(state.adopted,true);assert.equal(a.review_hash,review().review_hash);assert.equal(a.expected_room_revision,state.roomRevision);assert.ok(a.action_id);data={session_id:'mock-session',room_id:'room-1'};
  }else if(body.command==='record_product_revision_choice'){assert.deepEqual(a.pinned_product_ref,ref('product','product-A'));assert.deepEqual(a.observed_head_ref,ref('product','product-A',2));assert.equal(a.diff_hash,hash('diff'));if(a.choice==='refresh'){bump();state.preparation.body.candidate_publication_ref=null}else assert.equal(a.choice,'keep_pinned');state.differences=false;data={};
  }else throw Error('unexpected command '+body.command);
  res.end(JSON.stringify({data}));
 }catch(error){res.writeHead(500);res.end(JSON.stringify({error:{message:'MOCK_ASSERTION: '+error.message}}));state.failure=error.stack}
});
async function main(){
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 let browser;try{browser=await chromium.launch({headless:true})}catch{browser=await chromium.launch({channel:'msedge',headless:true})}
 const page=await browser.newPage({viewport:{width:1280,height:1100}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 const base='http://127.0.0.1:'+server.address().port;
 async function fresh(){state=fixture();await page.goto(base);await page.locator('#open-prelive-workspace').click();await page.locator('.prelive-order-card').first().waitFor();}
 async function settle(text){await page.getByRole('status').count().catch(()=>{});await page.waitForFunction(t=>document.getElementById('prelive-workspace-feedback').textContent.includes(t),text);assert.equal(state.failure,undefined,state.failure);}
 try{
  await fresh();await page.locator('#close-prelive-workspace').click();await page.locator('#toggle-assistant').click();assert.equal(await page.locator('body').evaluate(n=>n.classList.contains('assistant-expanded')),true);await page.locator('#open-prelive-workspace').click();await page.locator('.prelive-order-card').first().waitFor();
  const initial=state.preparation_details.playlist.body.items.map(i=>i.occurrence_id);
  await page.locator('.prelive-order-card').nth(1).getByRole('button',{name:'↑',exact:true}).click();
  await page.getByRole('button',{name:'保存顺序',exact:true}).click();await settle('顺序已保存');
  assert.deepEqual(state.preparation_details.playlist.body.items.map(i=>i.occurrence_id),[initial[1],initial[0],initial[2]]);
  for(const [label,mode]of [['同商品连续','grouped'],['商品穿插','interleave'],['随机重排','full_shuffle']]){
   await page.getByRole('button',{name:label,exact:true}).click();await settle('已预览重排');assert.equal(state.previews.at(-1).resolved_spec.mode,mode);
   await page.getByRole('button',{name:'保存顺序',exact:true}).click();await settle('顺序已保存');
  }
  const cards=page.locator('.prelive-order-card');await cards.nth(0).dragTo(cards.nth(2));await page.getByRole('button',{name:'保存顺序',exact:true}).click();await settle('顺序已保存');
  await page.locator('.prelive-script').first().locator('summary').click();await page.getByRole('textbox',{name:'长开场逐句话术'}).first().fill('修改后的第一句。\n第二句。');
  await page.getByRole('button',{name:'保存此模块',exact:true}).first().click();await settle('该模块已保存');
  assert.ok(state.calls.some(c=>c.body.command==='patch_script'));
  // A genuine 401/CSRF rejection refreshes the browser session once; it never
  // invents a human approval or retries an already accepted mutation.
  state.expireNext=true;await page.getByRole('button',{name:'刷新',exact:true}).click();await page.locator('.prelive-order-card').first().waitFor();await page.waitForFunction(()=>document.getElementById('prelive-workspace-feedback').textContent==='');assert.equal(state.sessions,2);
  await page.getByRole('button',{name:'检查并采用到直播间',exact:true}).click();await settle('已采用到直播间');
  await page.getByRole('button',{name:'采用后开播',exact:true}).click();await page.waitForFunction(()=>!document.getElementById('prelive-workspace').open);
  assert.deepEqual(await page.evaluate(()=>hostCalls.filter(x=>x.startsWith('enter:')||x.startsWith('view:')||x.startsWith('monitor:'))),['enter:room-1','view:monitor','monitor:room-1']);
  await fresh();state.differences=true;await page.getByRole('button',{name:'查看采用前差异',exact:true}).click();await page.getByRole('button',{name:'保留旧版',exact:true}).click();await page.waitForFunction(()=>document.getElementById('prelive-workspace-feedback').textContent==='');assert.equal(state.calls.filter(c=>c.body.command==='record_product_revision_choice').at(-1).body.args.choice,'keep_pinned');
  state.differences=true;await page.getByRole('button',{name:'查看采用前差异',exact:true}).click();await page.getByRole('button',{name:'刷新到新版',exact:true}).click();await page.waitForFunction(()=>document.getElementById('prelive-workspace-feedback').textContent==='');assert.equal(state.preparation.body.candidate_publication_ref,null);
  assert.equal(errors.length,0,errors.join('\n'));assert.equal(state.failure,undefined,state.failure);
  const output=fs.mkdtempSync(path.join(os.tmpdir(),'shengbo-prelive-browser-'));await page.screenshot({path:path.join(output,'workspace.png'),fullPage:true});
  console.log(JSON.stringify({status:'passed',scenarios:['host navigation','manual up/down','drag order','grouped/interleave/full_shuffle exact preview hash','selected module text hash','CSRF session refresh','human adoption then start','observed_head_ref keep/refresh'],page_errors:errors,screenshot:path.join(output,'workspace.png')}));
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
}
main().catch(error=>{console.error(error.stack);server.close();process.exitCode=1});
