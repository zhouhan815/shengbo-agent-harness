/* Isolated execution-adapter protocol checks. No real DB/model/TTS/platform/broadcast is touched. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const {chromium}=require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const script=fs.readFileSync(path.join(__dirname,'../web/workbench-execution.js'),'utf8');
async function main(){
  const server=http.createServer((req,res)=>{res.setHeader('Content-Type',req.url==='/execution.js'?'application/javascript; charset=utf-8':'text/html; charset=utf-8');res.end(req.url==='/execution.js'?script:req.url.startsWith('/live/')?'<!doctype html>':'<!doctype html><meta charset="utf-8"><div id="tools"></div><script src="/execution.js"></script>');});await new Promise(r=>server.listen(0,'127.0.0.1',r));
  let browser;try{browser=await chromium.launch({headless:true});}catch{browser=await chromium.launch({channel:'msedge',headless:true});}
  const page=await browser.newPage();let checked=0;const check=(condition,message)=>{assert.ok(condition,message);checked++;};
  try{
    await page.goto('http://127.0.0.1:'+server.address().port);
    await page.evaluate(()=>{
      window.calls=[];window.nav=[];window.errors=[];window.ctx={room_id:'r1'};
      window.VoicePlayer=class{
        constructor(){this.identity='local-player';this.state={player_id:'local-player'};}
        async unlock(){calls.push({sound:'unlock'});if(window.deferSound)await new Promise(resolve=>window.releaseSound=resolve);}
        async connect(id){calls.push({sound:'connect',room:id});}
        async claim(){calls.push({sound:'claim'});}
        disconnect(){}
      };
      window.playback={status:'playing',epoch:8,session_id:'live1',index:0,current:{id:'s1',queue_id:'q1',product_name:'拖鞋',product_id:'p1',script_id:'v1',text:'当前句',version:'第1版',ordinal:1,total_sentences:2},queue:[{id:'s1',queue_id:'q1',product_name:'拖鞋',product_id:'p1',script_id:'v1',text:'当前句',version:'第1版',ordinal:1,total_sentences:2},{id:'s2',queue_id:'q2',product_name:'拖鞋',product_id:'p1',script_id:'v1',text:'第二句',version:'第1版',ordinal:2,total_sentences:2},{id:'s3',queue_id:'q3',product_name:'梳子',product_id:'p2',script_id:'v2',text:'梳子话术',version:'第3版',ordinal:1,total_sentences:1}],connected_player:true,output_source:'browser',logs:[],audience_events:[],position:1,audio:{duration:5}};
      window.lib={rooms:[{id:'r1',name:'第一直播间',product_ids:['p1']}],scripts:[]};
      window.sourceView={config:{url:'123',purpose:'external_test',enabled:false},status:{state:'disconnected',enabled:false,metrics:{}},events:[],next_cursor:0};
      window.sessionList=[];window.turns=[];window.obs={jobs:[]};window.review={adopted:false,room_ref:{id:'r1',revision:3},review_hash:'review1'};window.candidate={kind:'publication',id:'pub1',revision:1,content_hash:'x',schema_version:'PRELIVE-2.3'};
      const clone=x=>JSON.parse(JSON.stringify(x));
      const api={
        async request(url,body,options={}){window.calls.push({url,body:clone(body??null),method:options.method||(body===undefined?'GET':'POST')});
          if(url==='/api/library')return clone(window.lib);
          if(url==='/api/audio/output-status')return {available:true,devices:[],browser_sessions:[]};
          if(url.endsWith('/state'))return clone(window.playback);
          if(url.endsWith('/control')){
            if(window.advanceBeforeControl){window.playback.epoch++;window.advanceBeforeControl=false;}
            if(body.expected_session_id!=null&&body.expected_session_id!==window.playback.session_id)throw Error('场次已变化');
            if(body.expected_epoch!=null&&body.expected_epoch!==window.playback.epoch)throw Error('播出位置已变化');
            if(body.action==='pause')window.playback.status='paused';
            if(body.action==='resume')window.playback.status='playing';
            if(body.action==='start')window.playback.status='ready';
            if(body.action==='stop')Object.assign(window.playback,{status:'stopped',current:null});
            return {ok:true};
          }
          if(url.includes('/live-source'))return clone(window.sourceView);
          if(url.endsWith('/reply-knowledge'))return {products:[{id:'p1',name:'拖鞋'}],documents:[{title:'商品须知',version:'rev1',content:'浴室遇水小心'}],chunk_count:1,prompt:'按资料回答',prompt_revision:'p1',deepseek_configured:true};
          if(url.endsWith('/reply-sessions')){if(body!==undefined){const s={id:'session1',title:'评论测试',created_at:'2026-10-16T00:00:00Z'};window.sessionList.push(s);return clone(s);}return {sessions:clone(window.sessionList)};}
          if(url==='/api/reply-sessions/session1')return {session:{id:'session1'},turns:clone(window.turns)};
          if(url.endsWith('/comments')){window.turns.push({id:'turn1',comment:body.text,username:body.username,status:'running'});return {ok:true};}
          if(url.endsWith('/retry'))return {ok:true};
          if(url.includes('/api/jobs?'))return {jobs:[]};
          if(url.endsWith('/test-events'))return {event:{source:'simulated'},reply:window.testEventReply||{status:'preparing'},like_thanks:window.testLikeThanks||{status:'preparing'}};
          if(url==='/api/copywriting/evaluation')return {rubric:{},methodology:{},runs:[],audio_samples:[]};
          throw Error('Unexpected route '+url);
        },
        async query(name,args){window.calls.push({query:name,args:clone(args)});if(name==='get_workspace_snapshot')return {preparation:{body:{candidate_publication_ref:window.candidate}}};if(name==='create_review')return clone(window.review);if(name==='read_completion_observation')return clone(window.obs);throw Error('Unexpected query '+name);},
        async command(name,args){window.calls.push({command:name,args:clone(args)});return {data:{}};}
      };
      window.executionApi=api;window.tool=ShengboWorkbenchExecution.mount({container:document.getElementById('tools'),api,onNavigate:d=>window.nav.push(d),onError:e=>window.errors.push(e.message),getContext:()=>window.ctx,getStartBlock:()=>window.startBlock||''});
    });
    const open=dest=>page.evaluate(d=>window.tool.open(d),dest);
    const action=name=>page.locator('[data-exec-action="'+name+'"]');
    if(process.argv.includes('--likes-only')){
      await open({view:'comments'});
      check(await action('simulate-like').innerText()==='模拟点赞并感谢'&&await page.getByLabel('本次点赞数').inputValue()==='1','per-event count input and explicit like voice action remain');
      await page.locator('[name=username]').fill('点赞观众甲');await page.getByLabel('本次点赞数').fill('8');
      for(const [status,expected]of [['collecting','正在合并这位观众的连续点赞，随后排队感谢'],['queued','这位观众的点赞感谢已排队'],['preparing','正在准备感谢语音']]){
        await page.evaluate(status=>window.testLikeThanks={status},status);await action('simulate-like').click();await page.waitForTimeout(15);
        check(await page.locator('#tools').innerText().then(text=>text.includes(expected)),'like thanks '+status+' has accurate non-dropping feedback');
      }
      const sent=await page.evaluate(()=>calls.filter(c=>c.url?.endsWith('/test-events')&&c.body?.type==='like').at(-1));
      check(sent.body.like_count===8&&sent.body.username==='点赞观众甲'&&!('total_count'in sent.body),'likes remain per-event count without cumulative total');
      await page.evaluate(()=>window.testLikeThanks={status:'skipped',reason:'duplicate_event'});await action('simulate-like').click();await page.waitForTimeout(15);
      check(await page.locator('#tools').innerText().then(text=>text.includes('这条点赞信号已处理，没有重复感谢')),'duplicate-event skip has a valid explicit reason');
      await page.evaluate(()=>window.testLikeThanks={status:'skipped',reason:'insertion_busy'});await action('simulate-like').click();await page.waitForTimeout(15);
      check(await page.locator('#tools').innerText().then(text=>text.includes('服务返回状态与当前点赞排队策略不一致')&&!text.includes('已有欢迎、回复或感谢正在准备/插播，请等它结束后再试')),'legacy busy skip is identified as incompatible instead of accepted policy');
      check(await page.locator('#tools').innerText().then(text=>text.includes('模拟进场按60秒欢迎间隔')&&text.includes('同一位观众短时间连续点赞合并')&&text.includes('多位观众依次排队')&&!text.includes('模拟点赞按滚动60秒')),'welcome interval remains while likes are per-person combined and queued');
      await page.evaluate(()=>tool.dispose());process.stdout.write(JSON.stringify({passed:checked,scope:'targeted private like-status UI only; no full browser batch, real backend, model or TTS'},null,2)+'\n');return;
    }
    await open({view:'broadcast'});
    check(await page.locator('[data-current-sentence]').innerText().then(s=>s.includes('当前句')&&s.includes('第 1 / 2 句')),'current sentence has product/version/ordinal: '+await page.locator('#tools').innerText());
    check((await page.evaluate(()=>calls)).every(c=>!c.command&&(!c.method||c.method==='GET')),'opening tools never starts generation/broadcast');
    check(await page.locator('[name=obs_url]').inputValue().then(s=>s.endsWith('/live/r1?output=obs')),'OBS browser source URL');
    check(await action('start').innerText()==='开始声播播出'&&await action('stop').innerText()==='停止声播声音','controls describe Shengbo audio scope');
    await page.evaluate(()=>window.advanceBeforeControl=true);
    await action('pause').click();await page.waitForFunction(()=>window.playback.status==='paused'&&!document.querySelector('[data-exec-action=pause]').dataset.execBusy);
    check(await action('pause').isDisabled(),'pause remains disabled after async finally');
    check(!(await action('resume').isDisabled()),'resume is available while paused');
    const pause=await page.evaluate(()=>calls.find(c=>c.body?.action==='pause'));
    check(pause.body.expected_epoch==null&&pause.body.expected_session_id==='live1'&&/^[0-9a-f-]{36}$/.test(pause.body.action_id),'pause binds the session and succeeds across a sentence boundary');
    await action('local-sound').click();
    await page.waitForFunction(()=>!document.querySelector('[data-exec-action=local-sound]').dataset.execBusy);
    check(await page.evaluate(()=>playback.status==='paused'&&!calls.some(c=>c.body?.action==='resume')),'changing local output preserves pause');
    await action('resume').click();await page.waitForTimeout(30);check(await action('resume').isDisabled(),'resume disabled after playing');
    await page.locator('[name=insert]').fill('这是插播');await action('insert-after').click();await page.waitForTimeout(30);
    check(await page.evaluate(()=>calls.some(c=>c.body?.action==='insert'&&c.body.mode==='after_sentence'&&c.body.text==='这是插播')),'sentence end insertion payload');
    await action('jump-version').last().click();await page.waitForTimeout(40);const jumps=await page.evaluate(()=>calls.filter(c=>['reorder','next'].includes(c.body?.action)));
    check(jumps.length===2&&jumps[0].body.queue_ids.join(',')==='q3,q2'&&jumps[1].body.action==='next','version jump uses complete pending queue then guarded next');
    const beforeStop=await page.evaluate(()=>structuredClone(playback));
    await page.evaluate(()=>window.advanceBeforeControl=true);
    await action('stop').click();await page.waitForFunction(()=>playback.status==='stopped');
    const stop=await page.evaluate(()=>calls.find(c=>c.body?.action==='stop'));
    check(stop.body.expected_epoch==null&&stop.body.expected_session_id==='live1','stop binds the session and succeeds across a sentence boundary');
    await page.getByText(/声播声音已停止.*关闭采集.*抖音端结束直播/).waitFor();
    await page.evaluate(previous=>{window.playback=previous;},beforeStop);
    await open({view:'platform'});await page.locator('[name=source_room]').fill('https://live.douyin.com/456');await action('platform-save').click();await page.waitForTimeout(30);
    check(await page.evaluate(()=>calls.some(c=>c.url?.endsWith('/live-source')&&c.method==='PUT'&&c.body.room.endsWith('/456'))),'platform save uses PUT and source DTO');
    await page.locator('[name=source_room]').fill('unsaved-source');await page.evaluate(()=>window.tool.refresh());check(await page.locator('[name=source_room]').inputValue()==='unsaved-source','platform polling preserves unsaved source input');
    await page.evaluate(()=>{const real=executionApi.request;executionApi.request=async(...args)=>{if(args[0].endsWith('/live-source')&&args[2]?.method==='PUT')return new Promise(resolve=>window.releaseSource=async()=>resolve(await real(...args)));return real(...args);};});
    await action('platform-save').click();await page.waitForFunction(()=>typeof window.releaseSource==='function');await page.locator('[name=source_room]').fill('newer-input-during-save');await page.evaluate(()=>window.releaseSource());await page.waitForTimeout(30);
    check(await page.locator('[name=source_room]').inputValue()==='newer-input-during-save','successful save cannot replace newer input made while awaiting response');
    await action('platform-disconnect').click();await page.waitForTimeout(30);check(await page.evaluate(()=>calls.some(c=>c.url?.endsWith('/disconnect')&&c.method==='POST')),'disconnect POST with no body');
    await open({view:'comments'});check(await page.locator('[data-comment-knowledge]').textContent().then(s=>s.includes('商品须知')&&s.includes('按资料回答')),'knowledge and prompt visible inline');
    check(await page.evaluate(()=>calls.filter(c=>c.url?.endsWith('/reply-sessions')&&c.method==='POST').length)===0,'viewing comments does not create a session');
    check(await action('simulate-comment').innerText()==='模拟评论并播报','voice-comment entry is separate from text-only reply-lab');
    await page.locator('[name=username]').fill('模拟观众甲');await page.locator('[name=comment]').fill('这一套有什么？');await action('simulate-comment').click();await page.waitForTimeout(30);
    const voiceSignal=await page.evaluate(()=>calls.filter(c=>c.url?.endsWith('/test-events')&&c.body?.type==='comment').at(-1));
    check(voiceSignal.url==='/api/rooms/r1/test-events'&&voiceSignal.body.username==='模拟观众甲'&&voiceSignal.body.text==='这一套有什么？'&&!('reply_turn_id'in voiceSignal.body),'voice signal carries current username/text directly to test-events, without text-lab recursion marker');
    check(await page.locator('#tools').innerText().then(s=>s.includes('模拟评论已受理')&&s.includes('当前句结束')),'preparing response explains natural-boundary voice insertion');
    const voiceCount=await page.evaluate(()=>calls.filter(c=>c.url?.endsWith('/test-events')).length);await page.evaluate(()=>playback.status='paused');await action('simulate-comment').click();await page.waitForTimeout(30);
    check(await page.evaluate(()=>calls.filter(c=>c.url?.endsWith('/test-events')).length)===voiceCount,'paused room blocks voice signal and never auto-starts');
    check(await page.locator('#tools').innerText().then(s=>s.includes('请先在播出监控开始本机播出')),'blocked voice simulation gives clear start/sound instruction');
    await page.evaluate(()=>{playback.status='playing';window.testEventReply={status:'skipped',reason:'insertion_busy'};});await action('simulate-comment').click();await page.waitForTimeout(30);
    check(await page.locator('#tools').innerText().then(s=>s.includes('已有欢迎或回复正在准备/插播')),'busy response is explicitly skipped, not falsely reported accepted');
    await page.evaluate(()=>window.testEventReply={status:'skipped',reason:'not_playing'});await action('simulate-comment').click();await page.waitForTimeout(30);
    check(await page.locator('#tools').innerText().then(s=>s.includes('当前尚未开播或已暂停')),'server not-playing race gives direct explanation');
    await page.evaluate(()=>window.testEventReply={status:'preparing'});
    check(await page.getByLabel('本次点赞数').inputValue()==='1'&&await action('simulate-like').innerText()==='模拟点赞并感谢','like UI has a small per-event count defaulting to one and clear voice action');
    await page.getByLabel('本次点赞数').fill('12');await action('simulate-like').click();await page.waitForTimeout(30);
    const likeSignal=await page.evaluate(()=>calls.filter(c=>c.url?.endsWith('/test-events')&&c.body?.type==='like').at(-1));
    check(likeSignal.url==='/api/rooms/r1/test-events'&&likeSignal.body.username==='模拟观众甲'&&likeSignal.body.like_count===12&&!('total_count'in likeSignal.body)&&!('reply_turn_id'in likeSignal.body),'like signal uses exact per-event like_count, never cumulative total_count or reply-lab marker');
    check(await page.locator('#tools').innerText().then(s=>s.includes('模拟点赞已受理')&&s.includes('感谢使用当前商品与价格')),'like admission explains current-product/price thanks at sentence boundary');
    await page.evaluate(()=>window.testLikeThanks={status:'collecting'});await action('simulate-like').click();await page.waitForTimeout(30);
    check(await page.locator('#tools').innerText().then(s=>s.includes('正在合并这位观众的连续点赞，随后排队感谢')&&!s.includes('本次没有播出点赞感谢')),'consecutive likes collect per person instead of global cooldown');
    const likesBefore=await page.evaluate(()=>calls.filter(c=>c.url?.endsWith('/test-events')&&c.body?.type==='like').length);await page.getByLabel('本次点赞数').fill('2.5');await action('simulate-like').click();await page.waitForTimeout(30);
    check(await page.evaluate(()=>calls.filter(c=>c.url?.endsWith('/test-events')&&c.body?.type==='like').length)===likesBefore,'fractional per-event like count is rejected without sending');
    await page.getByLabel('本次点赞数').fill('12');await page.evaluate(()=>playback.status='paused');await action('simulate-like').click();await page.waitForTimeout(30);
    check(await page.evaluate(()=>calls.filter(c=>c.url?.endsWith('/test-events')&&c.body?.type==='like').length)===likesBefore,'paused like simulation never submits or auto-starts');
    await page.evaluate(()=>{playback.status='playing';window.testLikeThanks={status:'queued'};});await action('simulate-like').click();await page.waitForTimeout(30);
    check(await page.locator('#tools').innerText().then(s=>s.includes('这位观众的点赞感谢已排队')),'occupied insertion slot keeps like thanks queued');
    await page.evaluate(()=>window.testLikeThanks={status:'skipped',reason:'source_changed'});await action('simulate-like').click();await page.waitForTimeout(30);
    check(await page.locator('#tools').innerText().then(s=>s.includes('商品或售价已变化')),'source-revision skip has a direct human explanation');
    await page.evaluate(()=>window.testLikeThanks={status:'preparing'});
    await page.locator('[name=comment]').fill('尺码怎么选？');await action('comments-send').click();await page.waitForTimeout(40);
    const sent=await page.evaluate(()=>calls.find(c=>c.url?.endsWith('/comments')));
    check(sent.body.text==='尺码怎么选？'&&!!sent.body.request_id,'comment sends standard text/request id');
    check(await action('comments-send').isDisabled(),'queued reply prevents duplicate comment submission');
    check(await page.evaluate(()=>calls.filter(c=>c.url?.endsWith('/reply-sessions')&&c.method==='POST').length)===1,'explicit send lazily creates exactly one session');
    await page.evaluate(()=>{turns[0]={...turns[0],status:'completed',answer:'按尺码表选择',retrieval:{query:'尺码',chunks:[{id:'ch1',title:'尺码表',ordinal:1,line:2,version:'r1',score:4,text:'脚背宽建议大一码'}]},citations:[{chunk_id:'ch1',quote:'建议大一码'}]};return tool.refresh();});
    check(await page.locator('[data-comment-feed]').textContent().then(s=>s.includes('尺码表')&&s.includes('建议大一码')),'retrieval source and verified citation preserved');
    await action('simulate-enter').click();await page.waitForTimeout(20);check(await page.evaluate(()=>calls.some(c=>c.body?.type==='enter'&&c.url.endsWith('/test-events'))),'simulated event goes to separate test-events endpoint');
    await page.evaluate(()=>{window.ctx.preparation_id='prep1';window.obs={jobs:[{ref:{id:'job1'},body:{kind:'audio',status:'partially_failed',items:[{status:'failed',call_state:'timeout_unknown'},{status:'succeeded',call_state:'result_received'}]}}]};});
    await open({view:'jobs',preparation_id:'prep1'});check(await action('job-retry').count()===0,'unknown external result has no retry button');
    check(await page.locator('[data-jobs-list]').innerText().then(s=>s.includes('1 / 2')&&s.includes('结果未知')),'v2 succeeded counts and unknown explanation');
    await page.evaluate(()=>{obs.jobs[0].body.items[0].call_state='error_received';return tool.refresh();});await action('job-retry').click();await page.waitForTimeout(30);
    check(await page.evaluate(()=>calls.some(c=>c.command==='retry_failed_items'&&c.args.job_id==='job1')),'v2 failed retry uses typed job command');
    await action('evaluation').click();await page.waitForTimeout(30);check(await page.locator('[data-evaluation]').isVisible(),'evaluation inline');
    await page.evaluate(()=>{const real=executionApi.request;executionApi.request=async(...args)=>args[0]==='/api/copywriting/evaluation'?new Promise(resolve=>window.releaseEvaluation=async()=>resolve(await real(...args))):real(...args);window.errors.length=0;});
    await action('evaluation').click();await page.waitForFunction(()=>typeof window.releaseEvaluation==='function');await open({view:'broadcast',preparation_id:'prep1'});await page.evaluate(()=>window.releaseEvaluation());await page.waitForTimeout(30);
    check(await page.evaluate(()=>window.errors.length===0&&tool.getState().view==='broadcast'),'late evaluation response cannot write into a different view');
    await page.evaluate(()=>{playback.status='stopped';playback.current=null;calls.length=0;});await open({view:'broadcast',preparation_id:'prep1'});await action('start').click();await page.waitForTimeout(30);
    check(await page.evaluate(()=>!calls.some(c=>c.command==='start_prepared_room'||c.body?.action==='start')&&nav.at(-1)?.view==='playlist'),'unadopted preparation cannot bypass human review');
    await page.evaluate(()=>{
      window.review.adopted=true;
      window.calls.length=0;
      window.startBlock='请先保存商品或本场的修改';
      document.querySelector('[data-exec-action=start]').click();
    });
    await page.locator('[data-start-block]').waitFor();
    check(await action('start').isDisabled(),'monitor start shows and enforces current blocker');
    check(await page.evaluate(()=>!calls.some(c=>c.sound||c.command==='start_prepared_room'||c.body?.action==='start')),'monitor start blocks unsaved edits before unlocking or committing');
    await page.evaluate(()=>{window.startBlock='';window.calls.length=0;return tool.refresh();});
    await action('start').click();await page.waitForTimeout(30);
    check(await page.evaluate(()=>calls.some(c=>c.command==='start_prepared_room'&&c.args.publication_ref.id==='pub1'&&c.args.review_hash==='review1'&&c.args.expected_room_revision===3)),'adopted preparation uses exact human start command');
    await page.evaluate(()=>{
      const command=executionApi.command;executionApi.command=async(name,args)=>{const result=await command(name,args);if(name==='start_prepared_room')playback.status='ready';return result;};
      playback.status='stopped';playback.current=null;review.adopted=true;calls.length=0;
    });
    await open({view:'broadcast',preparation_id:'prep1'});
    await page.evaluate(()=>tool.startWithSound({room_id:'r1',preparation_id:'prep1'}));
    check(await page.evaluate(()=>{const unlock=calls.findIndex(c=>c.sound==='unlock'),start=calls.findIndex(c=>c.command==='start_prepared_room'),connect=calls.findIndex(c=>c.sound==='connect');return unlock>=0&&unlock<start&&start<connect&&calls[connect].room==='r1';}),'one click unlocks audio before review/start, then connects the same room without TTS');
    await page.evaluate(()=>{playback.status='stopped';playback.current=null;review.adopted=false;calls.length=0;nav.length=0;});
    await open({view:'broadcast',preparation_id:'prep1'});
    await page.evaluate(()=>tool.startWithSound({room_id:'r1',preparation_id:'prep1'}).catch(e=>window.quickStartError=e.message));
    check(await page.evaluate(()=>!calls.some(c=>c.command==='start_prepared_room'||c.sound==='connect')&&nav.at(-1)?.view==='playlist'&&!!quickStartError),'one-click start preserves adoption gate and never connects rejected content');
    await page.evaluate(()=>{review.adopted=true;calls.length=0;nav.length=0;window.deferSound=true;window.quickStartAttempt=tool.startWithSound({room_id:'r1',preparation_id:'prep1'}).catch(e=>window.quickStartError=e.message);});
    await page.waitForFunction(()=>typeof window.releaseSound==='function');
    await page.evaluate(()=>{ctx.preparation_id='prep2';window.releaseSound();return window.quickStartAttempt;});
    check(await page.evaluate(()=>!calls.some(c=>c.command==='start_prepared_room'||c.sound==='connect')&&nav.length===0&&quickStartError.includes('切换本场')),'changing preparation while sound unlock waits prevents starting a stale target');
    await page.evaluate(()=>{
      ctx.preparation_id='prep1';window.deferSound=false;playback.status='paused';playback.session_id='live-resume';calls.length=0;
      VoicePlayer.prototype.connect=async function(id){calls.push({sound:'connect',room:id});playback.epoch++;this.state={player_id:this.identity,session_id:playback.session_id};};
    });
    await open({view:'broadcast',preparation_id:'prep1'});
    await page.evaluate(()=>tool.resumeWithSound({room_id:'r1',preparation_id:'prep1'}));
    check(await page.evaluate(()=>calls.some(c=>c.url?.endsWith('/control')&&c.body.action==='resume'&&c.body.expected_session_id==='live-resume'&&c.body.expected_epoch===undefined)&&!calls.some(c=>c.command==='start_prepared_room')),'reconnecting paused sound resumes the same session despite owner epoch changes, without restarting');
    await page.evaluate(()=>window.tool.dispose());check(await page.locator('.wb-execution').count()===0,'dispose removes execution tools');

    await page.evaluate(async()=>{
      ctx={room_id:'r1',preparation_id:'prep1'};
      Object.assign(playback,{status:'stopped',current:null,connected_player:false});
      calls.length=0;
      nav.length=0;
      errors.length=0;
      startBlock='请先采用本场';
      review.adopted=true;
      HTMLMediaElement.prototype.play=async function(){calls.push({sound:'test',src:this.getAttribute('src')});};
      window.ShengboAudioRouting={create:()=>({
        async ensureReady(){calls.push({sound:'route-ready'});},
        async registerMedia(node){calls.push({sound:'route-sample',src:node.getAttribute('src')});},
        getStatus(){return {label:'测试输出设备'};},
        dispose(){}
      })};
      VoicePlayer.prototype.connect=async function(id){
        calls.push({sound:'connect',room:id});
        if(window.failSplitConnection)throw Error('隔离声音通道故障');
        this.state={player_id:this.identity,session_id:playback.session_id};
      };
      const command=executionApi.command;
      executionApi.command=async(name,args)=>{
        if(name==='start_prepared_room'&&window.rejectSplitStart){
          calls.push({command:name,args});
          throw Error('隔离开播拒绝');
        }
        const result=await command(name,args);
        if(name==='start_prepared_room')playback.current=playback.queue[0];
        return result;
      };
      window.splitTool=ShengboWorkbenchExecution.mount({
        container:document.getElementById('tools'),api:executionApi,inline:true,splitPlayback:true,showNavigation:false,
        getContext:()=>ctx,getStartBlock:()=>startBlock,onError:error=>errors.push(error.message),
        async onNavigate(destination){nav.push(destination);calls.push({route:destination.view});if(destination.view==='broadcast')await splitTool.open(destination);}
      });
      await splitTool.open({...ctx,view:'broadcast-setup'});
      window.originalAudio=document.querySelector('#tools > .wb-execution > audio');
    });
    check(await action('start').innerText()==='开播'&&await action('start').isDisabled(),'configuration shows one blocked start and no live controls');
    check(await action('pause').count()===0&&await page.locator('[name=insert]').count()===0&&await action('sound-test').isVisible(),'configuration exposes device test and leaves pause and inserts to the monitor');
    await action('sound-test').click();
    await page.getByText('已发送短提示音，请确认是否从所选设备听到。',{exact:true}).waitFor();
    check(await page.evaluate(()=>calls.filter(c=>c.sound).map(c=>[c.sound,c.src]).join('|')===
      'route-ready,|route-sample,/assets/output-test.wav|test,/assets/output-test.wav'),'explicit device test plays a short local sample through the same output router');
    check(await page.evaluate(()=>!calls.some(c=>c.command||c.method==='POST')),'device test and opening setup perform no server mutation');

    await page.evaluate(()=>{calls.length=0;return splitTool.open({...ctx,view:'broadcast'});});
    check(await action('start').count()===0&&await action('pause').isVisible()&&await page.getByLabel('插播内容').isVisible(),'direct monitor link shows the controls and no second start');
    check(await page.evaluate(()=>!calls.some(c=>c.command||c.sound)&&originalAudio===document.querySelector('#tools > .wb-execution > audio')),'opening monitor from an Agent route is read-only and retains playback media');

    await page.evaluate(async()=>{startBlock='';rejectSplitStart=true;await splitTool.open({...ctx,view:'broadcast-setup'});calls.length=0;});
    await action('start').click();
    await page.getByText('隔离开播拒绝',{exact:true}).waitFor();
    check(await page.evaluate(()=>splitTool.getState().view==='broadcast-setup'&&!calls.some(c=>c.route==='broadcast'||c.sound==='connect')),'rejected start stays in setup and does not connect playback');
    await page.evaluate(()=>{rejectSplitStart=false;calls.length=0;});
    await action('start').click();
    await page.getByText('声音播放器已连接，输出设备：测试输出设备。',{exact:true}).waitFor();
    check(await page.evaluate(()=>{
      const start=calls.findIndex(c=>c.command==='start_prepared_room'),monitor=calls.findIndex(c=>c.route==='broadcast'),connect=calls.findIndex(c=>c.sound==='connect');
      return calls.filter(c=>c.sound==='unlock').length===1&&start>0&&start<monitor&&monitor<connect&&
        calls[start].args.publication_ref.id==='pub1'&&calls[start].args.expected_room_revision===3&&
        calls.filter(c=>c.query==='create_review').length===1&&originalAudio===document.querySelector('#tools > .wb-execution > audio');
    }),'successful start unlocks one persistent player once, commits exact adoption, then routes to monitor and connects');
    await page.getByLabel('插播内容').fill('监控页插播');
    await action('insert-after').click();
    await page.waitForFunction(()=>calls.some(c=>c.body?.action==='insert'&&c.body.text==='监控页插播'));
    check(await action('stop').isVisible(),'monitor retains explicit live controls and sends insert for current room only');

    await page.evaluate(async()=>{
      Object.assign(playback,{status:'stopped',current:null});
      await splitTool.open({...ctx,view:'broadcast-setup'});
      failSplitConnection=true;
      calls.length=0;
    });
    await action('start').click();
    await page.getByText(/声播播出已启动。声音通道连接失败.*隔离声音通道故障/).waitFor();
    check(await page.evaluate(()=>splitTool.getState().view==='broadcast'&&calls.filter(c=>c.command==='start_prepared_room').length===1),'post-commit sound failure stays in monitor for recovery without another start');
    await page.evaluate(async()=>{await splitTool.open({...ctx,view:'broadcast-setup'});calls.length=0;});
    check(await action('start').innerText()==='查看播出监控'&&await action('sound-test').isDisabled(),'an already active setup offers monitor and prevents test sound');
    await action('start').click();
    await action('stop').waitFor();
    check(await page.evaluate(()=>!calls.some(c=>c.command||c.sound==='connect')&&calls.filter(c=>c.route==='broadcast').length===1),'returning to monitor for an active room never claims sound or restarts');
    await page.evaluate(()=>splitTool.dispose());
    process.stdout.write(JSON.stringify({passed:checked,scope:'isolated execution protocol; no real model, TTS, platform, DB or live actions'},null,2)+'\n');
  }finally{await browser.close();await new Promise(r=>server.close(r));}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
