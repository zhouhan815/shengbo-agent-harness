/* Inline execution tools for the Agent workbench. All mutations require an explicit human gesture. */
(function(global){
  'use strict';
  const copy=x=>x==null?x:JSON.parse(JSON.stringify(x));
  const uid=()=>global.crypto.randomUUID();
  const labels={stopped:'声音未启动 / 已停止',preparing:'准备音频',ready:'等待声音输出',buffering:'正在缓冲',playing:'声播声音输出中',paused:'已暂停',blocked:'声音被拦截',error:'失败',completed:'已完成',succeeded:'已完成',failed:'失败',partially_failed:'部分失败',queued:'等待中',running:'运行中',interrupted:'已中断',cancelled:'已取消',unknown:'结果待核实'};
  const sourceLabels={disabled:'采集关闭',disconnected:'未连接',connecting:'连接中',checking:'核验直播状态',connected:'已连接抖音',reconnecting:'重连中',offline:'对方未开播',not_found:'未找到直播间',error:'连接异常',reconnect_required:'需要重连'};
  const phases={opening_long:'完整开场',opening_short:'换品短引入',product_intro:'商品介绍',selling_points:'卖点讲解',campaign:'本场活动',transition:'衔接',welcome:'开场',identity:'商品介绍',benefit:'卖点讲解',promotion:'优惠说明',cta:'促单',faq:'常见问题'};
  function E(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text??'');if(cls)n.className=cls;return n;}
  function mount(options){
    const api=options.api||global.ShengboWorkbenchApi, host=options.container;
    if(!host||!api)throw Error('执行工具缺少容器或本机 API');
    const root=E('div',undefined,'wb-execution'),feedback=E('p',undefined,'wb-inline-alert');feedback.setAttribute('role','status');feedback.hidden=true;
    const audio=options.passive?null:E('audio');
    root.append(feedback);
    if(audio){audio.preload='auto';audio.hidden=true;root.append(audio);}
    let player=null,testAudio=null,view=null,context={},library=null,playback=null,pane=null,polling=false,disposed=false,generation=0,filter='all',cursor=0,source=null,sourceEvents=[],sessionId=null,conversation=null,knowledge=null,legacyJobs=[],v2Jobs=[],evaluation=null,broadcastSignature='',broadcastLogsSignature='',broadcastEventsSignature='',sourceSignature='',commentsSignature='',jobsSignature='';
    const drafts=new Map();let sourceDirty=false,sourceEditRevision=0,commentDirty=false,requesting=false,quickStarting=false;
    const audioRouting=options.passive ? null : global.ShengboAudioRouting?.create({audio,onChange:()=>options.onAudioRouteChange?.(),onFault:error=>player?.failOutput(error.message),mayPlay:()=>!!player?.state?.current&&player.identity===player.state.player_id&&!['paused','stopped','completed','error'].includes(player.state.status)});
    if(audioRouting&&options.audioRouteContainer)audioRouting.mount(options.audioRouteContainer);
    const q=s=>pane?.querySelector(s),qa=s=>[...(pane?.querySelectorAll(s)||[])];
    const room=()=>context.room_id||null;
    const path=s=>'/api/rooms/'+encodeURIComponent(room())+s;
    const isActive=state=>!!state?.status&&!['stopped','completed'].includes(state.status);
    function info(message,error=false){feedback.textContent=message||'';feedback.hidden=!message;feedback.dataset.error=String(error);if(error)options.onError?.(Error(message));}
    async function req(url,body,method){return api.request(url,body,method?{method}:{});}
    function button(title,action,fn,primary=false){const b=E('button',title,'wb-button'+(primary?' wb-button--primary':''));b.type='button';b.dataset.execAction=action;b.addEventListener('click',async()=>{if(b.disabled)return;const token=generation;b.disabled=true;b.dataset.execBusy='true';try{await fn();}catch(e){if(token===generation)info(e.message||'操作未完成',true);}finally{delete b.dataset.execBusy;if(b.isConnected){b.disabled=false;availability();}}});return b;}
    function availability(){
      if(view==='broadcast'&&playback){
        const active=!!playback.current;
        const disabled={
          pause:!active||playback.status==='paused',
          resume:!active||quickStarting||!['paused','blocked'].includes(playback.status),
          stop:!isActive(playback),
          'local-sound':quickStarting||!isActive(playback),
          'copy-obs':!q('[name=obs_url]')?.value
        };
        for(const action of [...Object.keys(disabled),'restart-version','retry','next','next_product','insert-now','insert-after']){
          const control=q(`[data-exec-action="${action}"]`);
          if(control)control.disabled=disabled[action]??!active;
        }
      }
      if (view === 'broadcast' || view === 'broadcast-setup') {
        if (!playback && view === 'broadcast') {
          for (const action of ['pause', 'resume', 'restart-version', 'retry', 'next', 'next_product', 'stop', 'insert-now', 'insert-after', 'local-sound', 'copy-obs']) {
            const control = q(`[data-exec-action="${action}"]`);
            if (control) control.disabled = true;
          }
        }
        const start = q('[data-exec-action=start]');
        const message = q('[data-start-block]');
        const blocker = options.getStartBlock?.() || '';
        const active = isActive(playback);
        if (start) {
          if (view === 'broadcast-setup') {
            start.textContent = active ? '查看播出监控' : '开播';
            start.disabled = !playback || (!active && !!blocker && !options.onStartBlocked);
          } else start.disabled = !playback || active || (!!blocker && !options.onStartBlocked);
        }
        if (message) {
          message.hidden = !blocker || active;
          message.textContent = blocker;
        }
        const test = q('[data-exec-action=sound-test]');
        if(test)test.disabled=!playback||active||isActive(player?.state)||quickStarting||!audioRouting;
      }
      if(view==='comments'){const send=q('[data-exec-action=comments-send]');if(send)send.disabled=!!conversation?.turns?.some(t=>['queued','running'].includes(t.status));}
      if(view==='platform')for(const action of ['platform-save','platform-connect','platform-disconnect']){const b=q(`[data-exec-action="${action}"]`);if(b)b.disabled=requesting;}
      qa('[data-exec-busy=true]').forEach(b=>b.disabled=true);
    }
    function card(title,description){const s=E('section',undefined,'wb-card wb-execution-card');if(title)s.append(E('h3',title));if(description)s.append(E('p',description,'wb-muted'));return s;}
    function row(...nodes){const r=E('div',undefined,'wb-row wb-execution-actions');r.append(...nodes.filter(Boolean));return r;}
    function field(title,node){const l=E('label',undefined,'wb-field');l.append(E('span',title),node);return l;}
    function detail(title,value){const d=E('details',undefined,'wb-execution-detail');d.append(E('summary',title),E('pre',typeof value==='string'?value:JSON.stringify(value,null,2)));return d;}
    function input(name,value='',type='text'){const n=E('input');n.name=name;n.type=type;n.value=value;return n;}
    function textarea(name,value=''){const n=E('textarea');n.name=name;n.value=value;n.rows=3;return n;}
    function download(name,value){const a=E('a'),url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
    async function mutation(action,extra={}){
      if(!room())throw Error('请先选择直播间。');
      const sessionControl = ['pause', 'resume', 'stop'].includes(action);
      const observed = playback ? {
        expected_session_id: playback.session_id,
        ...(!sessionControl ? { expected_epoch: playback.epoch } : {})
      } : {};
      const body = { action, action_id: uid(), ...observed, ...extra };
      try{const result=await req(path('/control'),body);await refresh();return result;}
      catch(e){if(!e.status||e.unknown)info('操作结果尚未确定，请刷新确认播出状态；没有自动重发。操作编号：'+body.action_id,true);throw e;}
    }
    function cacheDraft(){if(!room()||!pane)return;if(view==='platform'){drafts.set('source:'+room(),{room:q('[name=source_room]')?.value||'',purpose:q('[name=source_purpose]:checked')?.value||'external_test',dirty:sourceDirty});}if(view==='comments'){drafts.set('comment:'+room(),{username:q('[name=username]')?.value||'测试观众',text:q('[name=comment]')?.value||'',like_count:q('[name=like_count]')?.value||'1',product_id:q('[name=comment_product]:checked')?.value||'',dirty:commentDirty});}if(view==='broadcast')drafts.set('insert:'+room(),q('[name=insert]')?.value||'');}
    function toolbar(){const bar=row();for(const[key,title]of [['broadcast','播出监控'],['platform','平台采集'],['comments','评论测试'],['jobs','后台任务']]){const b=button(title,'view-'+key,()=>options.onNavigate?options.onNavigate({...context,view:key}):open({...context,view:key}));b.setAttribute('aria-current',String(view===key));bar.append(b);}return bar;}
    async function open(dest={}){
      cacheDraft();const next={...(options.getContext?.()||{}),...dest},nextRoom=next.room_id||null;
      testAudio?.pause();
      if(nextRoom!==room()){library=null;playback=null;source=null;sourceEvents=[];cursor=0;sessionId=null;conversation=null;knowledge=null;legacyJobs=[];v2Jobs=[];}
      context=next;view=['broadcast','broadcast-setup','platform','comments','jobs'].includes(dest.view)?dest.view:'broadcast';generation++;
      // Drafts belong to their internal room, independently of the current tool.
      // Keeping a comment/source draft must not make a different room appear dirty.
      sourceDirty=!!drafts.get('source:'+nextRoom)?.dirty;commentDirty=!!drafts.get('comment:'+nextRoom)?.dirty;
      root.dataset.wbExecutionView=view;feedback.hidden=true;pane?.remove();pane=E('div',undefined,'wb-execution-pane');root.append(pane);
      if(!root.isConnected||root.parentNode!==host){host.replaceChildren(root);}if(options.showNavigation!==false)pane.append(toolbar());
      if(!room()){const empty=card('先选一个直播间','播出、平台采集和评论测试都绑定具体直播间。');empty.append(button('查看直播间','choose-room',()=>options.onNavigate?.({view:'rooms'}),true));pane.append(empty);return;}
      if(view==='broadcast')buildBroadcast();if(view==='broadcast-setup')buildBroadcastSetup();if(view==='platform')buildPlatform();if(view==='comments')buildComments();if(view==='jobs')buildJobs();
      const token=generation;
      try{await refresh();}catch(e){if(token===generation)info(e.message,true);}return root;
    }
    function buildBroadcastSetup(){
      const state=E('p','正在读取播出状态…','wb-muted');
      state.dataset.broadcastSetupStatus='';
      const blocker=E('p','','wb-start-block');
      blocker.dataset.startBlock='';blocker.hidden=true;
      pane.append(state,blocker,row(
        button('开播','start',()=>isActive(playback)?enterBroadcastMonitor():startInPlaceWithSound(),true),
        button('试听输出设备','sound-test',testOutputDevice)
      ));
      availability();
    }
    function renderBroadcastSetup(){
      const state=q('[data-broadcast-setup-status]');
      const message=isActive(playback)?labels[playback.status]||playback.status:'待开播';
      if(state.textContent!==message)state.textContent=message;
      state.hidden=!isActive(playback);
      availability();
    }
    async function testOutputDevice(){
      if(!audioRouting||isActive(playback)||isActive(player?.state)||quickStarting)return;
      const token=generation;
      await audioRouting.ensureReady();
      if(token!==generation||quickStarting)return;
      if(!testAudio){
        testAudio=E('audio');
        testAudio.preload='none';
        testAudio.src='/assets/output-test.wav';
      }
      await audioRouting.registerMedia(testAudio);
      if(token!==generation||isActive(playback)||isActive(player?.state)||quickStarting)return;
      testAudio.currentTime=0;
      await testAudio.play();
      if(token!==generation||quickStarting){testAudio.pause();return;}
      info('已发送短提示音，请确认是否从所选设备听到。');
    }
    function buildBroadcast(){
      broadcastSignature = '';
      const compact=options.inline&&!options.splitPlayback;
      const status=card(compact?'':options.splitPlayback?'当前状态与播出控制':'开播检查与声音控制',
        compact?'':'这些按钮控制此直播间当前的声播输出。');
      const state = E('div');
      state.dataset.broadcastStatus = '';
      const blocker = E('p', '', 'wb-start-block');
      blocker.dataset.startBlock = '';
      blocker.hidden = true;
      const platform = E('div', undefined, 'wb-broadcast-checks');
      platform.append(
        E('p', '平台采集在「平台接入」单独检查和关闭。'),
        E('p', '抖音开播和收播在平台端操作。观众端声音与画面请用另一账号确认。')
      );
      if (options.onNavigate && !compact) platform.append(button('查看平台接入', 'check-platform', () => options.onNavigate({ view: 'platform' })));
      status.append(state, blocker, compact?E('span'):platform, row(
        options.splitPlayback?null:button('开始声播播出', 'start', startInPlaceWithSound, true),
        button('暂停声音', 'pause', () => mutation('pause')),
        button('继续声音', 'resume', resumePlayback),
        button('当前版本从头播放', 'restart-version', () => restartVersionWithSound(context, playback)),
        button('重播当前句', 'retry', () => mutation('retry')),
        button('下一句', 'next', () => mutation('next')),
        button('下一商品', 'next_product', () => mutation('next_product')),
        button('停止声播声音', 'stop', async () => {
          await mutation('stop');
          info('声播声音已停止。请到「平台接入」关闭采集，并在抖音端结束直播。');
        }),
        button('导出全部记录', 'export', async () => download('声播全部记录.json', await req('/api/export')))
      ));
      const output=card('声音输出与接入','在「播放设备」选择设备，再让 OBS 采集对应音源。也可使用 OBS 浏览器来源，两种方式选一种即可。');const url=input('obs_url');url.readOnly=true;url.setAttribute('aria-label','OBS 播出链接');const progress=E('p','等待播出','wb-muted');progress.dataset.outputStatus='';output.append(row(url,button('复制 OBS 链接','copy-obs',async()=>{if(!url.value)throw Error('请先开播再复制播出链接');try{await navigator.clipboard.writeText(url.value);info('OBS 链接已复制');}catch{url.select();info('链接已选中，可手动复制。');}})),row(button('将声音切到本机','local-sound',()=>enableSound(),true)),progress,detail('OBS 声音设置','浏览器来源宽 1080、高 1920。勾选“通过 OBS 控制音频”，检查混音器电平；如声音被拦截，在 OBS 来源“交互”中点击启用声音。OBS 接管后，本机声音自动让出。'));
      const current=card('当前播出 · 逐句定位');const currentBox=E('div',undefined,'wb-execution-current');currentBox.dataset.currentSentence='';const timeline=E('progress');timeline.max=1;timeline.value=0;timeline.setAttribute('aria-label','当前句播放进度');current.append(currentBox,timeline);
      const insert=card('手动插播','立即插播后重播被打断句；句尾插播后继续下一句。插播会按本场音色实时合成。');const text=textarea('insert',drafts.get('insert:'+room())||'');text.maxLength=500;text.setAttribute('aria-label','插播内容');const go=async mode=>{const value=text.value.trim();if(!value)throw Error('请填写插播内容');await mutation('insert',{text:value,mode});text.value='';cacheDraft();};text.addEventListener('input',cacheDraft);insert.append(text,row(button('句尾插播','insert-after',()=>go('after_sentence')),button('立即插播','insert-now',()=>go('immediate'),true)));
      const queue=card('商品 / 版本队列','上下移动仅影响待播版本。停止讲解商品会结束该商品当前及待播内容。');queue.append(E('div',undefined,'wb-execution-queue'));queue.lastChild.dataset.broadcastQueue='';
      const transcript=card('本场逐句稿');const lines=E('div',undefined,'wb-execution-transcript');lines.dataset.broadcastTranscript='';transcript.append(lines);
      const preview=card('播出画面预览 · 静音');const frame=E('iframe');frame.title='静音播出预览';frame.src='/live/'+encodeURIComponent(room())+'?preview=1';frame.loading='lazy';preview.append(frame);
      const log=card('执行记录与观众事件');const records=E('div');records.dataset.broadcastLogs='';
      const executionLog=detail('执行记录（最近 50 条）',[]),audienceLog=detail('观众事件（真实 / 模拟来源保留，最近 100 条）',[]);
      executionLog.lastChild.dataset.broadcastExecutionLog='';audienceLog.lastChild.dataset.broadcastAudienceEvents='';records.append(executionLog,audienceLog);broadcastLogsSignature='';broadcastEventsSignature='';
      log.append(records);
      if(compact){
        const extra=E('details',undefined,'wb-inline-execution-extra');
        const extraActions=row(...['restart-version','retry','next','next_product','export']
          .map(action=>status.querySelector('[data-exec-action="'+action+'"]')));
        extra.append(E('summary','更多播出控制'),extraActions,platform,output,current,insert,queue,transcript,preview,log);
        pane.append(status,extra);
      }else pane.append(status,output,current,insert,queue,transcript,preview,log);
      availability();
    }
    async function enterBroadcastMonitor(){
      const target={...context,view:'broadcast',room_section:null};
      if(options.onNavigate)await options.onNavigate(target);
      else await open(target);
    }
    async function start(){
      const blocker = options.getStartBlock?.();
      const boundRoom=room(),boundPreparation=context.preparation_id,token=generation;
      const assertCurrent=()=>{if(boundRoom!==room()||token!==generation)throw Error('已切换操作对象，本次开播请求尚未提交；请在当前直播间重新检查。');};
      if(playback&&!['stopped','completed'].includes(playback.status)){info('本直播间已经开播，当前显示实时监控。');return;}
      if (blocker) {
        if (!options.onStartBlocked) throw Error(blocker);
        options.onStartBlocked([{message:blocker}],copy(context));
        return false;
      }
      if (boundPreparation && options.onStartBlocked) {
        const check=async()=>{
          const result=await api.query('read_start_readiness',{preparation_id:boundPreparation});
          assertCurrent();
          if(result.room_id!==boundRoom||result.preparation_ref?.id!==boundPreparation)throw Error('本场与直播间不一致，请重新选择。');
          return result;
        };
        const readiness=await check();
        if(!readiness.ready){
          options.onStartBlocked(readiness.blockers,copy(context),readiness.preparation_ref);
          return false;
        }
        try{
          await api.command('start_prepared_room',{publication_ref:readiness.publication_ref,review_hash:readiness.review_hash,expected_room_revision:readiness.room_ref.revision,action_id:uid()});
        }catch(error){
          if(error.unknown||!error.status)throw error;
          const latest=await check();
          if(latest.ready)throw error;
          options.onStartBlocked(latest.blockers,copy(context),latest.preparation_ref);
          return false;
        }
        if(!options.splitPlayback)await refresh();
        if(boundRoom===room())info('本场已启动。请开启本机声音，或复制链接导入 OBS。');
        return;
      }
      const cfg=library?.rooms?.find(r=>r.id===boundRoom);
      let publication=cfg?.adopted_publication_ref;
      if(boundPreparation){
        const snapshot=await api.query('get_workspace_snapshot',{preparation_id:boundPreparation});
        assertCurrent();
        const preparation=snapshot.preparation;
        if(preparation?.body?.room_id&&preparation.body.room_id!==boundRoom)throw Error('本场与直播间不一致，请重新选择。');
        publication=preparation?.body?.candidate_publication_ref;
        if(!publication){
          options.onNavigate?.({...context,view:'playlist',room_section:null});
          throw Error('本场尚未生成可采用的播出整包，请先完成话术、语音与采用。');
        }
      }
      if(publication){
        const review=await api.query('create_review',{ref:publication});assertCurrent();
        if(!review.adopted){
          if(boundPreparation)options.onNavigate?.({...context,view:'playlist',room_section:null});
          throw Error('请先在左侧「话术与排序」检查并采用本场，再开播。');
        }
        await api.command('start_prepared_room',{publication_ref:publication,review_hash:review.review_hash,expected_room_revision:review.room_ref.revision,action_id:uid()});
        if(!options.splitPlayback)await refresh();
      }else{
        assertCurrent();
        await mutation('start');
      }
      if(boundRoom===room())info('本场已启动。请开启本机声音，或复制链接导入 OBS。');
    }
    function localPlayer(){
      if(!audio)throw Error('请使用当前直播间的声音与播出面板。');
      const Player=global.VoicePlayer||(typeof VoicePlayer==='function'?VoicePlayer:null);
      if(!Player)throw Error('声音播放器尚未加载，请重新打开应用。');
      if(!player)player=new Player(audio,message=>{const n=q('[data-output-status]');if(n)n.textContent=message;},{beforePlay:()=>audioRouting?.ensureReady(),onContinuousOutput:output=>audioRouting?.registerMedia(output)});
      return player;
    }
    async function startInPlaceWithSound(){
      if(options.getStartBlock?.())return start();
      if(quickStarting)throw Error('正在启动本场，请稍候。');
      const boundRoom=room(),boundPreparation=context.preparation_id,token=generation;
      quickStarting=true;
      availability();
      try{
        testAudio?.pause();
        const local=localPlayer();
        if(options.splitPlayback&&local.enabled&&local.roomId&&local.roomId!==boundRoom)await audioRouting?.ensureReady();
        else await local.unlock();
        if(boundRoom!==room()||boundPreparation!==context.preparation_id||token!==generation)throw Error('已切换本场，本次没有启动声音。');
        if(await start()===false)return;
        if(boundRoom!==room()||boundPreparation!==context.preparation_id||token!==generation)throw Error('页面已切换，请核对原直播间状态。');
        if(options.splitPlayback&&view==='broadcast-setup'){
          await enterBroadcastMonitor();
          if(boundRoom!==room()||boundPreparation!==context.preparation_id||view!=='broadcast')return;
          const monitorGeneration=generation;
          try{await enableSound({unlocked:true});}
          catch(error){if(monitorGeneration===generation)info('声播播出已启动。声音通道连接失败，可在监控页重新连接：'+error.message,true);}
        }else await enableSound();
      }finally{quickStarting=false;availability();}
    }
    async function startWithSound(dest){
      if(quickStarting)throw Error('正在启动本场，请稍候。');
      const target=copy(dest||options.getContext?.()||context);
      if(!target.room_id||!target.preparation_id)throw Error('请先进入要播放的本场。');
      if(options.splitPlayback){
        if(view!=='broadcast-setup'||room()!==target.room_id||context.preparation_id!==target.preparation_id)throw Error('请先在当前直播间配置页开播。');
        return isActive(playback)?enterBroadcastMonitor():startInPlaceWithSound();
      }
      const sameTarget=()=>{const current=options.getContext?.()||context;return current.room_id===target.room_id&&current.preparation_id===target.preparation_id;};
      quickStarting=true;
      try{
        // Unlock this persistent media element inside the original human click,
        // before navigation, review requests or the start request consume time.
        await localPlayer().unlock();
        if(!sameTarget())throw Error('已切换本场，本次没有启动声音，请在当前本场重新操作。');
        if(options.onNavigate)await options.onNavigate({...target,view:'broadcast'});
        else await open({...target,view:'broadcast'});
        if(!sameTarget()||room()!==target.room_id||context.preparation_id!==target.preparation_id)throw Error('已切换本场，本次没有提交开播。');
        const token=generation;
        if(await start()===false)return;
        if(!sameTarget()||generation!==token)throw Error('播出请求已处理，但页面已切换；请核对原直播间状态，本次没有接管其他场次的声音。');
        await enableSound();
      }finally{quickStarting=false;}
    }
    async function resumeWithSound(dest){
      const target=copy(dest||options.getContext?.()||context);
      if(!target.room_id)throw Error('请先选择直播间。');
      await localPlayer().unlock();
      const current=options.getContext?.()||context;
      if(current.room_id!==target.room_id||current.preparation_id!==target.preparation_id)throw Error('已切换本场，本次没有恢复声音。');
      if(options.onNavigate)await options.onNavigate({...target,view:'broadcast'});
      else await open({...target,view:'broadcast'});
      if(room()!==target.room_id||context.preparation_id!==target.preparation_id)throw Error('已切换本场，本次没有恢复声音。');
      await enableSound({ resume: true });
    }
    async function restartVersionWithSound(dest,observed=playback){
      const target=copy(dest||context),initial=copy(observed);
      if(!initial?.current||!target.room_id)throw Error('请先开始本场播出。');
      const local=!initial.connected_player||player?.identity===initial.player_id;
      // Unlock in the click, before navigation and HTTP. Do not resume old audio.
      if(local)await localPlayer().unlock();
      const sameTarget=()=>{const current=options.getContext?.()||context;return current.room_id===target.room_id&&current.preparation_id===target.preparation_id;};
      if(!sameTarget())throw Error('已切换本场，本次没有从头播放。');
      if(view!=='broadcast'){
        if(options.onNavigate)await options.onNavigate({...target,view:'broadcast'});
        else await open({...target,view:'broadcast'});
      }else await refresh();
      if(!sameTarget()||room()!==target.room_id)throw Error('已切换本场，本次没有从头播放。');
      if(local){
        await player.connect(target.room_id);
        if(!sameTarget())return;
        if(player.state?.player_id!==player.identity)await player.claim();
      }
      if(!sameTarget()||playback?.session_id!==initial.session_id)throw Error('场次已变化，请重新选择要播放的版本。');
      const item=initial.queue?.[initial.index]||initial.current;
      await mutation('restart_version',{expected_epoch:null,expected_script_id:item.script_id,...(item.occurrence_id?{expected_occurrence_id:item.occurrence_id}:{})});
      info('当前版本已从第一句话重新播放，后续仍按原队列顺序播出。');
    }
    async function resumePlayback(){
      // A fresh page has no media owner: resuming only the server clock cannot
      // produce sound. Reconnect/unlock locally in this click before resuming.
      // Preserve an already connected OBS/other-window owner instead of stealing it.
      if(playback?.connected_player&&player?.identity!==playback.player_id){
        await mutation('resume');return;
      }
      await enableSound({ resume: true });
    }
    async function enableSound({ resume = false, unlocked = false } = {}){
      const boundRoom=room(),token=generation;
      localPlayer();
      // The persistent media element is unlocked before network requests, within the human gesture.
      if(!unlocked)await player.unlock();
      if(boundRoom!==room()||token!==generation)throw Error('已切换直播间，本次未接管新的声音来源。');
      await player.connect(boundRoom);
      if(boundRoom!==room()||token!==generation)return;
      if(player.state?.player_id!==player.identity)await player.claim();
      if(boundRoom!==room()||token!==generation)return;
      if(resume&&['paused','blocked'].includes(playback?.status)){
        await req(path('/control'),{action:'resume',action_id:uid(),expected_session_id:player.state?.session_id||playback.session_id});
        await refresh();
      }
      info('声音播放器已连接，输出设备：'+(audioRouting?.getStatus().label||'浏览器默认设备')+'。');
    }
    function groups(){const list=[];for(const item of playback?.queue?.slice((playback.index??-1)+1)||[]){const last=list.at(-1);const key=item.occurrence_id||item.script_id;if(!last||last.key!==key)list.push({key,script_id:item.script_id,product_id:item.product_id,items:[item]});else last.items.push(item);}return list;}
    async function jump(target){const pending=playback.queue.slice(playback.index+1),index=pending.findIndex(x=>x.queue_id===target);if(index<0)throw Error('目标已不在待播队列中，请刷新后选择。');const epoch=playback.epoch,session=playback.session_id,boundRoom=room(),token=generation;const rotated=[...pending.slice(index),...pending.slice(0,index)];await mutation('reorder',{queue_ids:rotated.map(x=>x.queue_id),expected_epoch:epoch,expected_session_id:session});if(boundRoom!==room()||token!==generation)throw Error('待播顺序已调整；因为已切换操作页面，本次没有继续跳播，请核对原直播间。');try{await mutation('next',{expected_epoch:epoch,expected_session_id:session});}catch(e){info('待播顺序已调整，但当前句已变化，未强制跳播。请核对当前定位后继续。',true);throw e;}}
    function renderBroadcast(){
      if(!playback||view!=='broadcast')return;const s=playback,c=s.current,active=!!c;const state=q('[data-broadcast-status]');state.replaceChildren(E('span',labels[s.status]||s.status,'wb-badge'),E('span',s.connected_player?(s.output_source==='obs'?'OBS 已接管声音':'本机声音已连接'):'等待声音来源','wb-muted'));
      q('[name=obs_url]').value=s.queue?.length&&s.status!=='stopped'?location.origin+'/live/'+encodeURIComponent(room())+'?output=obs':'';
      const local=getAudioStatus();
      q('[data-output-status]').textContent=global.ShengboAudioOutput?global.ShengboAudioOutput.describe(s,local).local:s.status==='paused'?'语音已暂停，当前不会输出声音':s.connected_player?'声音通道已连接；播放状态以上方实时状态为准':'等待声音来源';
      q('[data-current-sentence]').replaceChildren(E('p',c?`${c.product_name} · ${c.version} · 第 ${c.ordinal||'插播'} / ${c.total_sentences||'—'} 句 · ${phases[c.stage]||c.stage||''}`:'尚未播出','wb-record-meta'),E('p',c?.text||s.error||'采用本场内容后，点击「开始声播播出」。'));
      const progress=q('progress');progress.max=s.audio?.duration||1;progress.value=s.position||0;
      for(const action of ['pause','resume','restart-version','retry','next','next_product','stop','insert-now','insert-after']){const b=q(`[data-exec-action="${action}"]`);b.disabled=action==='pause'?!active||s.status==='paused':action==='resume'?!active||!['paused','blocked'].includes(s.status):action==='stop'?['stopped','completed'].includes(s.status):!active;}
      availability();
      // Queue changes and incoming events have separate lifecycles. Bounded event
      // arrays continue rolling at the same length, including while audio is paused.
      const signature=JSON.stringify([s.queue,s.current?.queue_id,s.index,s.publication_ref,library?.scripts,library?.rooms?.find(r=>r.id===room())?.script_order]);
      if(signature!==broadcastSignature){broadcastSignature=signature;
      const queue=q('[data-broadcast-queue]');queue.replaceChildren();const pendingGroups=groups();pendingGroups.forEach((g,i)=>{const first=g.items[0],item=card((first.product_name||'商品')+' · '+(first.version||'话术版本'));item.append(E('p','待播 '+g.items.length+' 句','wb-muted'),row(button('播这个版本','jump-version',()=>jump(first.queue_id)),button('上移','queue-up',async()=>{const all=groups();if(i<1)return;[all[i-1],all[i]]=[all[i],all[i-1]];await mutation('reorder',{queue_ids:all.flatMap(v=>v.items.map(x=>x.queue_id))});}),button('下移','queue-down',async()=>{const all=groups();if(i>=all.length-1)return;[all[i+1],all[i]]=[all[i],all[i+1]];await mutation('reorder',{queue_ids:all.flatMap(v=>v.items.map(x=>x.queue_id))});}),button(g.product_id === c?.product_id ? '停止讲解该商品' : '移除此商品的待播内容','cancel-product',()=>mutation('cancel_product',{product_id:g.product_id}))));
        if(!s.publication_ref){const alternatives=(library?.scripts||[]).filter(v=>v.product_id===g.product_id&&v.id!==g.script_id&&(v.room_id===room()||library?.rooms?.find(r=>r.id===room())?.script_order?.includes(v.id)));if(alternatives.length){const details=E('details');details.append(E('summary','替换待播稿'));for(const script of alternatives)details.append(button('替换为第 '+(script.version_number||1)+' 版','replace-pending',()=>mutation('replace_pending',{script_id:script.id,target_script_id:g.script_id})));item.append(details);}}queue.append(item);});if(!pendingGroups.length)queue.append(E('p','暂无待播版本','wb-muted'));
      const transcript=q('[data-broadcast-transcript]'),oldTop=transcript.scrollTop;transcript.replaceChildren();for(const [i,line]of(s.queue||[]).entries()){const item=E('article',undefined,'wb-execution-sentence');item.dataset.active=String(c?.queue_id===line.queue_id);item.append(E('small',`${line.product_name} · ${line.version} · 第${line.ordinal}/${line.total_sentences}句`),E('p',line.text));if(i>s.index)item.append(button('从这句开始','jump-sentence',()=>jump(line.queue_id)));transcript.append(item);}transcript.scrollTop=oldTop;
      }
      const logs=(s.logs||[]).slice(-50).reverse(),logsSignature=JSON.stringify(logs);if(logsSignature!==broadcastLogsSignature){broadcastLogsSignature=logsSignature;q('[data-broadcast-execution-log]').textContent=JSON.stringify(logs,null,2);}
      const events=(s.audience_events||[]).slice(-100).reverse(),eventsSignature=JSON.stringify(events);if(eventsSignature!==broadcastEventsSignature){broadcastEventsSignature=eventsSignature;q('[data-broadcast-audience-events]').textContent=JSON.stringify(events,null,2);}
    }
    function buildPlatform(){
      sourceSignature='';const d=drafts.get('source:'+room())||{};sourceDirty=!!d.dirty;const form=card(options.inline?'':'抖音来源接入',options.inline?'':'采集独立于声播播出。关闭页面或结束声音播出不会关闭采集；这里不生成模拟平台数据。');const url=input('source_room',d.room??source?.config?.url??source?.config?.external_room_id??'');url.maxLength=300;url.placeholder=options.inline?'抖音链接或房间号':'https://live.douyin.com/数字，或数字房间号';url.setAttribute('aria-label','抖音直播间链接或房间号');url.addEventListener('input',()=>{sourceDirty=true;sourceEditRevision++;cacheDraft();});form.append(field(options.inline?'抖音链接或房间号':'直播间网页地址 / 数字房间号',url));const purpose=row();for(const[value,title]of [['external_test',options.inline?'公开测试':'公开直播间测试'],['own_room',options.inline?'我的直播间':'自己的直播间']]){const radio=input('source_purpose',value,'radio');radio.checked=(d.purpose||source?.config?.purpose||'external_test')===value;radio.addEventListener('change',()=>{sourceDirty=true;sourceEditRevision++;cacheDraft();});purpose.append(field(title,radio));}form.append(purpose,row(button(options.inline?'保存':'保存来源','platform-save',()=>platformAction('save')),button(options.inline?'连接':'开启 / 重新连接','platform-connect',()=>platformAction('connect'),true),button(options.inline?'关闭':'关闭采集','platform-disconnect',()=>platformAction('disconnect'))));if(options.inline){
      for(const [action,label] of [['platform-save','保存来源'],['platform-connect','连接采集'],['platform-disconnect','关闭采集']]){
        const control=form.querySelector('[data-exec-action="'+action+'"]');
        control.title=label;control.setAttribute('aria-label',label);
      }
      }const status=E('div');status.dataset.sourceStatus='';form.append(status);
      if(options.hidePlatformActivity){pane.append(form);return;}
      const metrics=card('平台指标','人数口径以平台返回为准；进场条数不能代替在线人数。');metrics.dataset.sourceMetrics='';const events=card('实时事件');const filters=row();for(const[k,title]of [['all','全部'],['comment','评论'],['enter','进场'],['like','点赞'],['other','其他']])filters.append(button(title,'source-filter-'+k,()=>{filter=k;renderPlatform(true);}));const feed=E('div',undefined,'wb-execution-feed');feed.dataset.sourceEvents='';events.append(filters,feed);if(options.inline){const extra=E('details',undefined,'wb-inline-execution-extra');extra.append(E('summary','查看平台指标与事件'),metrics,events);pane.append(form,extra);}else pane.append(form,metrics,events);
    }
    async function platformAction(action){
      const body=action==='disconnect'?undefined:{room:q('[name=source_room]').value.trim(),purpose:q('[name=source_purpose]:checked')?.value||'external_test'};
      if(body&&!body.room)throw Error('请填写抖音直播间网页地址中的数字或完整链接。');
      const token=generation,editRevision=sourceEditRevision;requesting=true;availability();
      try{const result=await req(path('/live-source')+(action==='save'?'':'/'+action),body,action==='save'?'PUT':'POST');if(token!==generation)return;source=result;if(!options.hidePlatformActivity)mergeEvents(source.events);if(editRevision===sourceEditRevision){sourceDirty=false;cacheDraft();}renderPlatform(true);info(action==='disconnect'?'采集已关闭，历史事件保留。':'请求已受理，请以连接状态和真实事件确认采集是否成功。');}finally{requesting=false;availability();}
    }
    function mergeEvents(rows=[]){const m=new Map(sourceEvents.map(e=>[e.id||e.cursor||JSON.stringify(e),e]));rows.forEach(e=>m.set(e.id||e.cursor||JSON.stringify(e),e));sourceEvents=[...m.values()].sort((a,b)=>(b.cursor||b.sequence||0)-(a.cursor||a.sequence||0)||String(b.time||'').localeCompare(String(a.time||''))).slice(0,200);cursor=Math.max(cursor,Number(source?.next_cursor)||0,...rows.map(e=>Number(e.cursor??e.sequence)||0));}
    function renderPlatform(force=false){if(view!=='platform'||!source)return;const st=source.status||{},cfg=source.config||{};const signature=JSON.stringify([st,sourceEvents,filter]);if(!force&&signature===sourceSignature)return;sourceSignature=signature;if(!sourceDirty){q('[name=source_room]').value=cfg.url||cfg.external_room_id||'';qa('[name=source_purpose]').forEach(r=>r.checked=r.value===(cfg.purpose||'external_test'));}q('[data-source-status]').replaceChildren(E('p',(sourceLabels[st.state]||st.state||'未连接')+(options.inline?'':' · 采集开关：'+((st.enabled??cfg.enabled)?'开':'关')),'wb-badge'),E('p',st.message||'等待平台状态','wb-muted'),E('small','外部房间 '+(cfg.external_room_id||st.external_room_id||'未设置')+' · 最近事件 '+(st.last_event_at||'尚无')));
      if(options.hidePlatformActivity)return;
      const metrics=q('[data-source-metrics]');metrics.replaceChildren(E('h3','平台指标'));for(const[k,title]of [['online','在线人数（候选）'],['total_visits','累计观看 / 人次（候选）'],['likes_total','累计点赞（平台回传）']]){const m=st.metrics?.[k]||{},box=card(title);box.append(E('strong',m.value??m.raw??'—'),E('p',m.stale||!st.connected?'历史值 / 已过期':'口径待核对','wb-muted'),E('small',m.basis||'等待平台返回真实快照'));metrics.append(box);}const feed=q('[data-source-events]'),top=feed.scrollTop,height=feed.scrollHeight;feed.replaceChildren();const filtered=sourceEvents.filter(e=>filter==='all'||(filter==='other'?!['comment','enter','like'].includes(e.type):e.type===filter));filtered.forEach(e=>{const item=E('article',undefined,'wb-execution-event');item.append(E('small',`${e.time||''} · 抖音真实事件 · ${e.type} · 外部房间${e.external_room_id||'未知'}`),E('strong',e.username||e.nickname||'平台事件'),E('p',e.text||({enter:'进入直播间',like:'点赞 × '+(e.count||1),gift:'赠送礼物'}[e.type]||'平台事件')));feed.append(item);});if(!filtered.length)feed.append(E('p','尚无该类真实事件；开启采集不代表已经接到数据。','wb-muted'));if(top>20)feed.scrollTop=top+(feed.scrollHeight-height);}
    function buildComments(){
      commentsSignature='';knowledge=null;const d=drafts.get('comment:'+room())||{};commentDirty=!!d.dirty;const box=card('模拟评论测试','「发送测试评论」仅测试文字回复与知识依据，不会口播。「模拟评论并播报」向当前本机播出发送语音评论信号，会调用已配置模型与 TTS，准备好后在当前句结束时插播。请先开始本机播出并开启声音；此处不会自动开播。');box.append(row(button('新建测试会话','comments-new',async()=>{const token=generation;const result=await req(path('/reply-sessions'),{});if(token!==generation)return;sessionId=result.id;await loadCommentData();}),button('刷新知识库','knowledge-refresh',async()=>{const token=generation;const result=await req(path('/reply-knowledge'));if(token!==generation)return;knowledge=result;renderKnowledge();info('已读取本场商品与知识资料。');}),button('导出当前测试','comments-export',()=>download('声播评论测试记录.json',conversation||{}))));const sessions=E('div',undefined,'wb-row');sessions.dataset.commentSessions='';box.append(sessions);const username=input('username',d.username||'测试观众');username.maxLength=40;const text=textarea('comment',d.text||'');text.maxLength=500;text.setAttribute('aria-label','模拟评论内容');for(const n of [username,text])n.addEventListener('input',()=>{commentDirty=true;cacheDraft();});const likeCount=input('like_count',d.like_count??1,'number');likeCount.min='1';likeCount.step='1';likeCount.style.width='100px';likeCount.setAttribute('aria-label','本次点赞数');likeCount.addEventListener('input',()=>{commentDirty=true;cacheDraft();});const products=E('div',undefined,'wb-row');products.dataset.commentProducts='';box.append(field('观众名称',username),products,field('观众问题',text),row(button('发送测试评论','comments-send',sendComment,true),button('模拟评论并播报','simulate-comment',()=>simulate('comment')),button('模拟进场','simulate-enter',()=>simulate('enter'))),row(field('本次点赞数',likeCount),button('模拟点赞并感谢','simulate-like',()=>simulate('like'))));const feed=card('回复记录与引用证据');feed.dataset.commentFeed='';const docs=card('本场知识与回复提示词');docs.dataset.commentKnowledge='';box.append(E('p','模拟进场按60秒欢迎间隔尝试语音欢迎；点赞感谢按人处理，同一位观众短时间连续点赞合并，多位观众依次排队，不因正在插播而丢弃。填写的是这次点赞数量，不是累计总数。感谢使用当前商品与价格，不自动开播。语音评论使用此刻正在播出的商品，以上商品选择仅用于独立文字测试。','wb-muted'));pane.append(box,feed,docs);
    }
    async function simulate(type){
      const token=generation,boundRoom=room(),eventPath=path('/test-events'),body={type,username:q('[name=username]').value.trim()||'测试观众'};
      if(type==='comment'){
        body.text=q('[name=comment]').value.trim();if(!body.text)throw Error('请先填写要模拟播报的观众问题。');
        const observed=await req(path('/state'));if(token!==generation||boundRoom!==room())throw Error('直播间已切换，本次未发送语音评论。');
        if(!observed.current||!['playing','ready','buffering'].includes(observed.status)||!observed.connected_player)throw Error('请先在播出监控开始本机播出并开启声音，再模拟评论；这里不会自动开播。');
      }
      if(type==='like'){
        const count=Number(q('[name=like_count]').value);if(!Number.isSafeInteger(count)||count<1)throw Error('本次点赞数须为大于零的整数，不是累计点赞数。');body.like_count=count;
        const observed=await req(path('/state'));if(token!==generation||boundRoom!==room())throw Error('直播间已切换，本次未发送点赞感谢。');
        if(!observed.current||!['playing','ready','buffering'].includes(observed.status)||!observed.connected_player)throw Error('请先在播出监控开始本机播出并开启声音，再模拟点赞感谢；这里不会自动开播。');
      }
      const result=await req(eventPath,body);if(token!==generation||boundRoom!==room())return;
      if(type==='like'){
        const thanks=result.like_thanks||{},status=thanks.status,reason=thanks.reason||status;
        if(status==='collecting'){info('正在合并这位观众的连续点赞，随后排队感谢。多位观众会依次排队，不因当前插播忙碌而丢弃；可到播出监控查看进度。');return;}
        if(status==='queued'){info('这位观众的点赞感谢已排队，将在前面的互动结束、到达自然句尾时依次播出，再回到脚本下一句。感谢使用当前商品与价格，可到播出监控查看进度。');return;}
        if(['preparing','ready'].includes(status)){info('模拟点赞已受理，正在准备感谢语音；就绪后在当前句结束时插播，再回到脚本下一句。感谢使用当前商品与价格，可到播出监控查看进度。');return;}
        if(['insertion_busy','busy','cooldown','playback_cooldown'].includes(reason)){info('服务返回状态与当前点赞排队策略不一致，请到播出监控核对。本界面不会把忙碌当作应丢弃的点赞，也不会自动重发。',true);return;}
        const reasons={not_playing:'当前尚未开播或已暂停，请先开始本机播出并开启声音。',duplicate_event:'这条点赞信号已处理，没有重复感谢。',external_test_read_only:'外部直播间仅用于只读采集，不触发本机感谢。',session_changed:'本场已变化，请核对当前播出后重新模拟。',product_changed:'当前商品已变化，请按正在播出的商品重新模拟。',expired:'这条点赞感谢已过期，没有进入播报。',room_changed:'直播间已变化，本次感谢未播出，请核对当前房间。',source_changed:'商品或售价已变化，本次感谢已取消，请核对当前播出资料。',product_unavailable:'当前商品资料不可用，尚未生成点赞感谢。',price_unavailable:'当前售价资料不可用，尚未生成点赞感谢。',product_or_price_unavailable:'当前商品或售价资料不可用，请核对商品库和本场配置。',invalid_event_or_session:'点赞信号与当前场次不匹配，请重新模拟。',already_started:'这条点赞感谢已经开始播放，不重复播放。',missing_like_count:'缺少本次点赞数量，未使用累计点赞数代替。',invalid_like_count:'本次点赞数量无效，请填写大于零的整数。'};
        info('本次没有播出点赞感谢：'+(reasons[reason]||(['skipped','ignored','not_playing'].includes(status)?'本次被跳过（'+String(reason||'未提供原因')+'），请检查播出状态。':'未收到明确的感谢语音受理状态，请到播出监控核对；不会自动重发。')),true);return;
      }
      if(type!=='comment'){info('已保存模拟'+(type==='enter'?'进场':'点赞')+'事件，可在播出监控中查看。');return;}
      const reply=result.reply||{},status=reply.status,reason=reply.reason||status;
      if(['preparing','queued','ready'].includes(status)){info('模拟评论已受理，正在准备回复与语音；就绪后会在当前句结束时插播，再回到脚本下一句。请到播出监控查看进度。');return;}
      const reasons={not_playing:'当前尚未开播或已暂停，请先开始本机播出并开启声音。',insertion_busy:'已有欢迎或回复正在准备/插播，请等它结束后再试。',busy:'已有欢迎或回复正在准备/插播，请等它结束后再试。',duplicate_event:'这条评论信号已经处理过，没有重复播报。',text_lab_only:'这是独立文字测试记录，不会触发语音。',external_test_read_only:'外部直播间仅用于只读采集，不触发本机语音。',session_changed:'本场已变化，请核对当前播出后重新模拟。',product_changed:'当前商品已变化，请按正在播出的商品重新提问。',expired:'这条语音评论已过期，没有进入播报。'};
      info('本次没有进入语音播报：'+(reasons[reason]||(['skipped','ignored','not_playing','busy'].includes(status)?'评论被跳过（'+String(reason||'未提供原因')+'），请检查播出状态。':'未收到明确的语音受理状态，请到播出监控核对；不会自动重发。')),true);
    }
    async function sendComment(){
      const text=q('[name=comment]').value.trim(),rawText=q('[name=comment]').value,username=q('[name=username]').value||'测试观众',product_id=q('[name=comment_product]:checked')?.value||null,token=generation,boundRoom=room();
      if(!text)throw Error('请填写测试问题');if(conversation?.turns?.some(t=>['queued','running'].includes(t.status)))throw Error('当前回复尚未完成，请等待。');
      let boundSession=sessionId;if(!boundSession){const created=await req(path('/reply-sessions'),{});if(token!==generation||boundRoom!==room())return;boundSession=sessionId=created.id;}
      await req('/api/reply-sessions/'+encodeURIComponent(boundSession)+'/comments',{text,username,product_id,request_id:uid()});
      if(token!==generation||boundRoom!==room()){
        const previous=drafts.get('comment:'+boundRoom);
        if(previous?.text===rawText)drafts.set('comment:'+boundRoom,{...previous,text:'',dirty:false});
        return;
      }
      if(q('[name=comment]').value===rawText){q('[name=comment]').value='';commentDirty=false;}cacheDraft();await loadCommentData();
    }
    async function loadCommentData(){
      const token=generation,id=room();
      const [currentKnowledge,list]=await Promise.all([req(path('/reply-knowledge')),req(path('/reply-sessions'))]);
      if(token!==generation||id!==room())return;
      knowledge=currentKnowledge;
      const sessions=list.sessions||[];
      if(!sessions.some(s=>s.id===sessionId))sessionId=sessions[0]?.id||null;
      const listNode=q('[data-comment-sessions]');
      listNode.replaceChildren();
      for(const session of sessions){
        const b=button((session.title||'测试会话')+' · '+String(session.created_at||'').slice(0,16),'comment-session',async()=>{
          const token=generation;
          sessionId=session.id;
          const result=await req('/api/reply-sessions/'+encodeURIComponent(session.id));
          if(token!==generation||sessionId!==session.id)return;
          conversation=result;commentsSignature='';renderComments();
        });
        b.dataset.sessionId=session.id;b.setAttribute('aria-pressed',String(session.id===sessionId));listNode.append(b);
      }
      renderKnowledge();
      const selectedSession=sessionId;
      const result=selectedSession?await req('/api/reply-sessions/'+encodeURIComponent(selectedSession)):null;
      if(token!==generation||selectedSession!==sessionId)return;
      conversation=result;
      renderComments();
    }
    function renderKnowledge(){if(view!=='comments'||!knowledge)return;const d=drafts.get('comment:'+room())||{},picked=q('[name=comment_product]:checked')?.value??d.product_id??'';const products=q('[data-comment-products]');products.replaceChildren();for(const [id,name]of [['','自动识别 / 延续对话'],...(knowledge.products||[]).map(p=>[p.id,p.name])]){const radio=input('comment_product',id,'radio');radio.checked=id===picked;radio.addEventListener('change',()=>{commentDirty=true;cacheDraft();});products.append(field(name,radio));}const box=q('[data-comment-knowledge]');box.replaceChildren(E('h3','本场知识与回复提示词'),E('p',`${knowledge.products?.length||0} 件商品 · ${knowledge.documents?.length||0} 份文档 · ${knowledge.chunk_count||0} 个片段 · ${knowledge.deepseek_configured?'DeepSeek 已配置':'DeepSeek 尚未配置'}`,'wb-muted'));for(const d of knowledge.documents||[])box.append(detail(d.title+' · 版本 '+d.version,d.content));box.append(detail('回复提示词 · '+(knowledge.prompt_revision||''),knowledge.prompt||'暂无提示词'));}
    function renderComments(){if(view!=='comments')return;availability();const turns=conversation?.turns||[],signature=JSON.stringify(turns);if(signature===commentsSignature)return;commentsSignature=signature;const box=q('[data-comment-feed]'),top=box.scrollTop;box.replaceChildren(E('h3','回复记录与引用证据'));for(const t of turns){const item=card(t.username||'测试观众');item.dataset.commentTurn=t.id;item.append(E('p',t.comment),E('small',labels[t.status]||t.status||'—'),E('p',t.status==='completed'?t.answer:t.error||'正在检索与生成回复…'));if(t.missing_information?.length)item.append(E('p','待确认：'+t.missing_information.join('；'),'wb-inline-alert'));if(['error','interrupted'].includes(t.status))item.append(button('重试回复','comment-retry',async()=>{await req('/api/reply-turns/'+encodeURIComponent(t.id)+'/retry',{});await loadCommentData();}));if(t.retrieval){const evidence=E('details');evidence.append(E('summary','检索 '+(t.retrieval.chunks?.length||0)+' 个片段 · 查看引用依据'),E('p','检索问题：'+t.retrieval.query+' · '+(t.model||'尚未返回模型')+' · '+(t.elapsed_seconds??'—')+' 秒','wb-muted'));for(const chunk of t.retrieval.chunks||[]){const cited=(t.citations||[]).filter(c=>c.chunk_id===chunk.id),c=card(chunk.title);c.append(E('small',`${cited.length?'模型引用':'已检索'} · 片段${chunk.ordinal} · 原文第${chunk.line}行 · 分数${chunk.score} · 版本${chunk.version}`),E('blockquote',chunk.text));cited.forEach(v=>c.append(E('p','核对引用原文：'+v.quote)));evidence.append(c);}item.append(evidence);}box.append(item);}if(!turns.length)box.append(E('p','可以先创建测试会话，或直接发送一条测试评论。','wb-muted'));box.scrollTop=top;q('[data-exec-action=comments-send]').disabled=turns.some(t=>['queued','running'].includes(t.status));qa('[data-session-id]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.sessionId===sessionId)));}
    function buildJobs(){jobsSignature='';const box=card('后台任务','已完成内容保留。只有明确失败或中断的任务才显示重试；结果未知的任务先核实，不自动重发。');box.append(row(button('刷新任务','jobs-refresh',refresh),button('全局话术评测与语音样本','evaluation',loadEvaluation)));const list=E('div');list.dataset.jobsList='';box.append(list);const evalBox=card('话术评测');evalBox.dataset.evaluation='';evalBox.hidden=true;pane.append(box,evalBox);}
    function renderJobs(){
      if(view!=='jobs')return;
      const signature=JSON.stringify([legacyJobs,v2Jobs]);if(signature===jobsSignature)return;jobsSignature=signature;
      const list=q('[data-jobs-list]');list.replaceChildren();
      for(const j of v2Jobs){
        const body=j.body||j,ref=j.ref,items=body.items||[];
        const done=items.filter(i=>['completed','succeeded'].includes(i.status)).length,unknown=items.some(i=>i.call_state==='timeout_unknown');
        const item=card(body.kind||body.type||'本场准备任务');
        item.append(E('p',(labels[body.status]||body.status)+' · '+done+' / '+items.length,'wb-badge'));
        const progress=E('progress');progress.max=items.length||1;progress.value=done;item.append(progress,detail('任务详细状态',j));
        if(unknown)item.append(E('p','部分请求结果未知，请先核实已有结果；不会自动重复生成或计费。','wb-inline-alert'));
        else if(['error','failed','interrupted','partially_failed','partial_failed'].includes(body.status)||items.some(i=>['failed','interrupted'].includes(i.status)))item.append(button('重试失败项目','job-retry',async()=>{await api.command('retry_failed_items',{job_id:ref?.id||body.id});await refresh();}));
        if(['queued','running'].includes(body.status))item.append(button('取消未完成任务','job-cancel',async()=>{await api.command('cancel_job',{job_id:ref?.id||body.id});await refresh();}));
        list.append(item);
      }
      for(const j of legacyJobs){
        const item=card(j.kind==='scripts'?'历史话术生成任务':'历史语音生成任务');item.append(E('p',(labels[j.status]||j.status)+' · '+j.done+'/'+j.total,'wb-badge'));
        const progress=E('progress');progress.max=j.total||1;progress.value=j.done||0;item.append(progress);
        for(const it of j.items||[])item.append(E('p',`${it.product_name||'商品'} · 第${it.version_number||'—'}版 · ${labels[it.status]||it.status} · ${it.done||0}/${it.total||0}`,'wb-muted'));
        if(j.error)item.append(E('p',j.error,'wb-inline-alert'));
        if(['error','interrupted'].includes(j.status)){
          const canUseLegacy=()=>{if(library?.rooms?.find(r=>r.id===room())?.adopted_publication_ref||context.preparation_id){options.onNavigate?.({...context,view:'playlist'});throw Error('此本场请在“顺序与语音”中按整包确认生成，不能使用旧任务入口。');}};
          if(context.preparation_id||library?.rooms?.find(r=>r.id===room())?.adopted_publication_ref){item.append(E('p','此处保留历史任务记录。当前本场的新增话术在本场配置中设置，语音通过整包批准后准备。','wb-muted'),button(j.kind==='audio'?'查看本场语音准备':'到本场配置设置新增版本','formal-task-destination',()=>options.onNavigate?.({...context,view:j.kind==='audio'?'scripts':'session'})));}
          else if(j.kind==='audio')item.append(button('补齐本场缺失语音','legacy-audio-retry',async()=>{canUseLegacy();await req(path('/jobs'),{kind:'audio'});await refresh();}));
          else item.append(button('为失败商品重新生成新版本','legacy-script-retry',async()=>{canUseLegacy();const failed=[...new Set((j.items||[]).filter(i=>i.status!=='completed').map(i=>i.product_id))];if(!failed.length)throw Error('没有需要重试的商品');await req(path('/jobs'),{kind:'scripts',product_ids:failed,version_count:1});await refresh();}));
        }
        list.append(item);
      }
      if(!v2Jobs.length&&!legacyJobs.length)list.append(E('p','当前直播间暂无后台生成任务。','wb-muted'));
    }
    async function loadEvaluation(){
      const token=generation,result=await req('/api/copywriting/evaluation');
      if(token!==generation||view!=='jobs')return;
      evaluation=result;
      const box=q('[data-evaluation]'),routing=options.sampleAudioRouting||audioRouting;
      if(!box)return;
      for(const sample of box.querySelectorAll('audio'))sample.pause();
      box.hidden=false;
      box.replaceChildren(E('h3','全局话术评测与语音样本'),E('p','各直播间共享这些参考资料。规则检查与模型评分供核对，成交和听感仍需实际验证。','wb-muted'),
        detail('评测方法与评分标准',{methodology:evaluation.methodology,rubric:evaluation.rubric}));
      for(const run of evaluation.runs||[])box.append(detail((run.run||'实验')+' · '+run.completed_versions+'/'+run.requested_versions+' 版',run));
      for(const sample of evaluation.audio_samples||[]){
        const item=card(sample.label||sample.file||'语音样本');
        if(typeof sample.url==='string'&&sample.url.startsWith('/tts-samples/')){
          const a=E('audio');a.controls=true;a.preload='none';a.src=sample.url;item.append(a);
          routing?.registerMedia(a).catch(e=>{if(token===generation)info(e.message,true);});
        }
        item.append(detail('样本记录',sample));box.append(item);
      }
    }
    async function refresh(){
      if(disposed||!room()||requesting)return;const token=generation,id=room(),selectedView=view;
      if(selectedView==='broadcast'||selectedView==='broadcast-setup'){
        const [lib,s]=await Promise.all([req('/api/library'),req(path('/state'))]);
        if(token!==generation||id!==room())return;
        library=lib;playback=s;
        if(selectedView==='broadcast')renderBroadcast();else renderBroadcastSetup();
      }
      if(selectedView==='platform'){const result=await req(path('/live-source')+(options.hidePlatformActivity?'?summary_only=true':source?'/events?after='+encodeURIComponent(cursor):''));if(token!==generation)return;source=result;if(!options.hidePlatformActivity)mergeEvents(result.events);renderPlatform();}
      if(selectedView==='comments'){if(!knowledge){await loadCommentData();}else if(sessionId){const selectedSession=sessionId,c=await req('/api/reply-sessions/'+encodeURIComponent(selectedSession));if(token!==generation||selectedSession!==sessionId)return;conversation=c;renderComments();}}
      if(selectedView==='jobs'){const queries=[req('/api/jobs?room_id='+encodeURIComponent(id)),library||req('/api/library')];if(context.preparation_id)queries.push(api.query('read_completion_observation',{preparation_id:context.preparation_id}));const [legacy,lib,observation]=await Promise.all(queries);if(token!==generation)return;library=lib;legacyJobs=legacy.jobs||[];v2Jobs=observation?.jobs||[];renderJobs();}
    }
    const timer=setInterval(async()=>{if(disposed||!root.isConnected||polling||document.hidden||requesting||(options.inline&&!root.getClientRects().length))return;const token=generation;polling=true;try{await refresh();}catch(e){if(token===generation)info('状态更新失败：'+e.message+'。输入内容已保留。',true);}finally{polling=false;}},1500);
    function getAudioStatus(){return {ownsPlayback:!!player?.identity&&player.state?.player_id===player.identity,paused:player?.continuousEngine?.directOutput?!player.continuousEngine.physical?.running:audio?.paused??true,muted:audio?.muted??false,volume:audio?.volume??1,hasAudio:!!player?.continuousEngine?.physical||!!audio?.srcObject||(!!audio?.getAttribute('src')&&audio.getAttribute('src')!=='/assets/unlock.wav'),currentTime:audio?.currentTime??0,sinkId:audio?.sinkId||'default',routing:audioRouting?.getStatus()};}
    function hasDirty(id=room()){
      const insertion=String(drafts.get('insert:'+id)||'').trim();
      if(id!==room())return !!(drafts.get('source:'+id)?.dirty||drafts.get('comment:'+id)?.dirty||insertion);
      return !!(sourceDirty||commentDirty||(view==='broadcast'?q('[name=insert]')?.value.trim():insertion));
    }
    return {open,refresh,refreshControls:availability,startWithSound,resumeWithSound,restartVersionWithSound,getAudioStatus,audioRouting,dispose(){disposed=true;clearInterval(timer);testAudio?.pause();player?.disconnect();audioRouting?.dispose();root.remove();},hasDirty,getState(){return {view,context:copy(context),playback:copy(playback),source:copy(source),sessionId,hasDirty:hasDirty()};}};
  }
  function mountDiagnostics(options){
    const host=options.container;
    if(!host)throw Error('互动与诊断缺少弹窗容器。');
    const id=host.id||'room-diagnostics';
    const dialog=E('dialog',undefined,'wb-room-diagnostics'),header=E('header'),heading=E('div');
    dialog.dataset.roomDiagnostics='';
    const title=E('h2','互动与诊断'),roomLabel=E('p');
    title.id=id+'-title';roomLabel.id=id+'-room';
    dialog.setAttribute('aria-labelledby',title.id);dialog.setAttribute('aria-describedby',roomLabel.id);
    const closeButton=E('button','关闭','wb-button');closeButton.type='button';
    closeButton.addEventListener('click',()=>close());
    heading.append(title,roomLabel);header.append(heading,closeButton);
    const tablist=E('div',undefined,'wb-room-diagnostic-tabs');
    tablist.setAttribute('role','tablist');tablist.setAttribute('aria-label','互动与诊断工具');
    dialog.append(header,tablist);
    const entries=new Map();
    let context=null,active=null,returnFocus=null;
    for(const [view,label] of [['comments','评论测试'],['jobs','任务与评测']]){
      const tab=E('button',label,'wb-button'),panel=E('section',undefined,'wb-room-diagnostic-panel');
      tab.type='button';tab.id=id+'-'+view+'-tab';panel.id=id+'-'+view+'-panel';
      tab.setAttribute('role','tab');tab.setAttribute('aria-controls',panel.id);tab.setAttribute('aria-selected','false');tab.tabIndex=-1;
      panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',tab.id);panel.tabIndex=0;panel.hidden=true;
      tab.addEventListener('click',()=>void select(view));
      tablist.append(tab);dialog.append(panel);
      const tool=mount({container:panel,api:options.api,passive:true,inline:true,showNavigation:false,
        sampleAudioRouting:options.sampleAudioRouting,onError:options.onError,
        onNavigate:options.onNavigate&&((destination)=>{
          close({restoreFocus:false});
          return options.onNavigate({...destination,room_section:null});
        })});
      entries.set(view,{tab,panel,tool,scope:null,request:0});
    }
    host.append(dialog);
    tablist.addEventListener('keydown',event=>{
      const views=[...entries.keys()],current=views.findIndex(view=>entries.get(view).tab===event.target);
      if(current<0)return;
      const offset={ArrowRight:1,ArrowLeft:-1}[event.key];
      if(offset===undefined&&!['Home','End'].includes(event.key))return;
      event.preventDefault();
      const index=offset!==undefined?(current+offset+views.length)%views.length:event.key==='Home'?0:views.length-1;
      void select(views[index],true);
    });
    dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
    dialog.addEventListener('close',()=>{if(!dialog.open&&context)finishClose(true);});
    function pauseSamples(){
      for(const audio of entries.get('jobs').panel.querySelectorAll('audio'))audio.pause();
    }
    async function select(view,focusTab=false){
      if(!context||!entries.has(view))return;
      const destination={...context,view},scope=JSON.stringify([destination.room_id,destination.preparation_id]);
      if(active==='jobs'&&view!=='jobs')pauseSamples();
      active=view;
      for(const [key,{tab,panel}] of entries){
        const selected=key===view;
        tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;
        panel.hidden=!selected;
      }
      const entry=entries.get(view),request=++entry.request;
      if(focusTab)entry.tab.focus({preventScroll:true});
      entry.panel.setAttribute('aria-busy','true');
      try{
        if(entry.scope===scope)await entry.tool.refresh();
        else{
          entry.scope=scope;
          entry.panel.scrollTop=0;
          await entry.tool.open(destination);
        }
      }catch(error){if(dialog.open&&active===view&&entry.request===request)options.onError?.(error);}
      finally{if(entry.request===request)entry.panel.removeAttribute('aria-busy');}
    }
    async function open(destination={}, {opener=document.activeElement,preserveFocus=false}={}){
      const current=options.getContext?.()||{},next={...current,...destination};
      if(!next.room_id)throw Error('请先选择直播间。');
      if(context&&(context.room_id!==next.room_id||context.preparation_id!==next.preparation_id))pauseSamples();
      context=next;
      const name=destination.room_name||(current.room_id===next.room_id?current.room_name:'')||next.room_id;
      roomLabel.textContent='当前直播间 · '+name;
      const opening=!dialog.open;
      if(opening){returnFocus=opener;dialog.showModal();}
      await select(entries.has(next.view)?next.view:'comments',opening||!preserveFocus);
      return dialog;
    }
    function finishClose(restoreFocus){
      pauseSamples();
      const opener=returnFocus;
      returnFocus=null;
      const previous=context;
      context=null;active=null;
      if(restoreFocus&&opener?.isConnected)opener.focus({preventScroll:true});
      options.onClose?.(copy(previous));
    }
    function close({restoreFocus=true}={}){
      if(!dialog.open)return;
      dialog.close();
      finishClose(restoreFocus);
    }
    return {open,close,isOpen:()=>dialog.open,
      hasDirty:()=>entries.get('comments').tool.hasDirty(options.getContext?.()?.room_id||context?.room_id||null),
      getState:()=>({open:dialog.open,view:active,context:copy(context)}),
      dispose(){close({restoreFocus:false});for(const {tool} of entries.values())tool.dispose();dialog.remove();}
    };
  }
  global.ShengboWorkbenchExecution={mount,mountDiagnostics};
})(window);
