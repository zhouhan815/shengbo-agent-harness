/* NotebookLM-style Agent workbench. Navigation never changes a submitted task. */
(function (global) {
  'use strict';
  const $ = id => document.getElementById(id);
  const clone = value => value == null ? value : structuredClone(value);
  const VIEWS = { overview: ['准备概览', '已保存资料、准备进度与待处理事项。'], products: ['商品资料', '共享商品库与本场引用；在此检查与微调。'], insights: ['顾客与卖点', '查看研究依据、卖点建议和所需素材。'], session: ['本场配置', '本场主题、活动、语言、声音与新增话术要求。'], scripts: ['话术与排序', '按完整版本查看、排列；点击卡片打开只读模块详情。'], changes: ['变更记录', '查看已保存修改与版本差异。'], rooms: ['直播间', '管理直播间并选择本场准备。'] };
  const api = global.ShengboWorkbenchApi;
  let editors, execution, assistant, lastAssistantState, busyDialog = false, manualNavigation = 0;
  let context = { view: 'overview', room_id: null, preparation_id: null };
  let followResults = false, startingBroadcast = false, latestArtifact = null, latestResultButton = null;
  try{followResults=localStorage.getItem('shengbo-workbench-follow-results-v2')==='true';}catch{}
  const selectionItems=value=>value?(Array.isArray(value.items)?value.items:[value]):[];
  let headerPlayback = null, playbackHeaderPolling = false, playbackControlBusy = false;
  let outputDiagnostic = {}, outputPolling = false, outputReadAt = 0;
  const seenResults = new Set();
  const seenCompletions = new Set();
  Object.assign(VIEWS, { broadcast: ['播出监控', '当前播放、逐句控制、人工插播与 OBS 来源。'], platform: ['平台接入', '配置公开直播间来源，查看实际采集状态。'], comments: ['评论测试', '检索商品知识、测试回复并查看依据。'], jobs: ['任务与评测', '后台任务、失败重试与已有话术评测。'] });
  const EXECUTION_VIEWS = new Set(['broadcast', 'platform', 'comments', 'jobs']);
  let rooms = [], headerGeneration = 0, navigationGeneration = 0;
  let serverEditorLocks=[],pendingEditorLocks=[],lockPollBusy=false,selectionNoticeCount=null;
  function pendingLocks(request){
    if(!request)return[];const pid=request.scope?.preparation_id;let targets=[];
    for(const selected of selectionItems(request.selection)){const target=selected.target,ref=target?.object_ref;if(!ref)continue;const kind={product:'product',preparation:'session',script_revision:'scripts',analysis:'insights'}[ref.kind];if(kind)targets.push({kind,id:kind==='product'?ref.id:pid||'catalog',field_paths:target.field_path?[target.field_path]:null,module_keys:target.module_key?[target.module_key]:null,...(kind==='scripts'?{script_id:ref.id,product_ids:target.product_ref?[target.product_ref.id]:null}:{})});}
    if(!targets.length&&pid)targets=['session','scripts','insights','assets','playlist','product_selection'].map(kind=>({kind,id:pid}));
    else if(!targets.length)targets=[{kind:'product_create',id:'catalog'}];
    return[{run_id:'submitting:'+request.client_request_id,status:'SUBMITTING',reason:'正在核实任务受理状态',targets}];
  }
  function paintEditorLocks(){editors?.setEditorLocks({locks:[...serverEditorLocks,...pendingEditorLocks]});}
  async function refreshEditorLocks(){
    if(lockPollBusy)return;lockPollBusy=true;
    try{const value=await api.request('/api/assistant/v2/editor-locks');serverEditorLocks=value.locks||[];pendingEditorLocks=pendingLocks(assistant?.getState().pendingRequest);paintEditorLocks();}
    catch(_){/* Keep known locks when the network is uncertain; a failed poll must not release them. */}
    finally{lockPollBusy=false;}
  }
  function anyDirty() { return !!editors?.hasDirty() || !!execution?.hasDirty?.(); }
  // Test comments and collector settings do not change the adopted audio plan.
  function hasPlaybackBlockingChanges() { return !!editors?.hasDirty(); }
  function rememberLocation() { try { sessionStorage.setItem('shengbo-workbench-location-v2', JSON.stringify({ view: context.view, product_id: context.product_id || null, preparation_id: context.preparation_id || null, room_id: context.room_id || null, action: null })); } catch (_) {} }
  function notice(message) { selectionNoticeCount=null;$('workbench-notice-message').textContent = message || ''; $('workbench-notice').hidden = !message; }
  function showSelectionNotice(count){notice(count?'已选择 '+count+' 项。在右侧输入修改要求后发送。':'已取消选择。');selectionNoticeCount=count;}
  $('dismiss-workbench-notice').addEventListener('click', () => notice(''));
  function focus(mode) {
    if (!['work', 'chat'].includes(mode)) return;
    document.body.dataset.focusMode = mode; document.body.dataset.mobilePanel = mode;
    for (const panel of ['work', 'chat']) {
      $('focus-' + panel).setAttribute('aria-pressed', String(panel === mode));
      $('mobile-' + panel).setAttribute('aria-selected', String(panel === mode));
    }
  }
  // Layout changes keep the original DOM, cursor, selection and scroll positions.
  for (const side of ['work', 'chat']) {
    $(side + '-panel').addEventListener('pointerdown', () => focus(side));
    $(side + '-panel').addEventListener('focusin', () => focus(side));
    $('focus-' + side).addEventListener('click', () => focus(side));
    $('mobile-' + side).addEventListener('click', () => focus(side));
  }
  function normalizeDestination(raw) {
    const d = clone(raw || {});
    const aliases = { 'product-detail': 'products', 'product-list': 'products', catalog: 'products', 'session-config': 'session', 'room-config': 'rooms', preparation: 'overview', playlist:'scripts', audio: 'scripts', adopted: 'scripts', task: 'overview' };
    d.view = aliases[d.view] || d.view || (d.product_id ? 'products' : 'overview');
    if (!VIEWS[d.view]) throw Error('该成果没有可识别的工作区位置，请查看执行记录。');
    if (d.resource_id && d.view === 'products' && !d.product_id) d.product_id = d.resource_id;
    if (d.resource_id && d.view === 'scripts' && !d.script_id) d.script_id = d.resource_id;
    if(d.view==='scripts'&&!d.script_id&&!d.resource_id){d.script_id=null;d.resource_id=null;d.module_key=null;}
    // Destinations are data. Arbitrary model-supplied URLs are never executed.
    delete d.url; delete d.href;
    return d;
  }
  function heading() {
    const [title, subtitle] = VIEWS[context.view] || VIEWS.overview;
    $('workspace-title').textContent = title; $('workspace-subtitle').textContent = subtitle;
    for (const tab of document.querySelectorAll('[data-view]')) if (tab.getAttribute('role') === 'tab') tab.setAttribute('aria-selected', String(tab.dataset.view === context.view));
    $('workspace-editor').setAttribute('aria-labelledby', 'workspace-title');
    $('workspace-status').textContent = hasPlaybackBlockingChanges() ? '有未保存修改' : execution?.hasDirty?.() ? '有调试草稿（不影响播出）' : '已读取';
    $('review-preparation').hidden = !context.preparation_id;
    $('review-preparation').disabled = !context.preparation_id;
    const liveState = headerPlayback?.room_id === context.room_id ? headerPlayback : null;
    const canPause = !!liveState?.current && ['playing', 'ready', 'preparing', 'buffering', 'paused'].includes(liveState.status);
    $('start-preparation').hidden = !context.preparation_id || context.view === 'broadcast' || canPause;
    $('start-preparation').disabled = startingBroadcast || hasPlaybackBlockingChanges();
    $('start-preparation').title = hasPlaybackBlockingChanges() ? '请先保存商品或本场的修改，再开始播出' : '通过所选设备播放声播声音，不会自动在抖音开播';
    $('start-preparation').textContent = startingBroadcast ? '正在开始…' : '▶ 开始播出';
    $('pause-preparation').hidden = !canPause;
    $('pause-preparation').disabled = playbackControlBusy;
    $('pause-preparation').textContent = playbackControlBusy ? '正在处理…' : liveState?.status === 'paused' ? '▶ 继续播出' : '⏸ 暂停播出';
    for (const tab of document.querySelectorAll('[data-execution-view]')) tab.setAttribute('aria-pressed', String(tab.dataset.executionView === context.view));
    $('open-catalog').setAttribute('aria-current', context.view === 'products' && !context.preparation_id ? 'page' : 'false');
    $('open-rooms').setAttribute('aria-current', context.view === 'rooms' ? 'page' : 'false');
    renderAudioOutput(liveState);
  }
  function renderAudioOutput(liveState) {
    const strip=$('playback-strip');
    strip.hidden=!context.room_id;
    if(strip.hidden||!global.ShengboAudioOutput)return;
    const media=execution?.getAudioStatus?.()||{},route=media.routing;
    const externalRoute=route?.mode==='external'&&liveState?.output_source!=='obs';
    const scopedDiagnostic=externalRoute?{...outputDiagnostic,browser_sessions:(outputDiagnostic.browser_sessions||[]).filter(x=>x.device_name===route.label)}:outputDiagnostic;
    const value=global.ShengboAudioOutput.describe(liveState||{},media,scopedDiagnostic);
    if(externalRoute){
      value.device=route.label||'等待选择外部输出设备';
      value.recommendation=route.status==='ready'?'OBS 添加“'+route.receiver.type+'”，设备选择“'+route.receiver.name+'”。':route.message;
      if(route.status!=='ready')value.local='外部音频通道未就绪，已阻止播放';
      else if(!media.ownsPlayback&&liveState?.connected_player)value.local='声音来自其他窗口；在“播出监控”点击“开启本机声音”，让本页按所选设备播放。';
    }
    execution?.audioRouting?.setHostDevices(outputDiagnostic.devices||[]);
    for(const [id,key]of [['audio-playback-status','status'],['audio-local-status','local'],['audio-current-meta','meta'],['audio-current-text','text'],['audio-output-device','device'],['audio-output-advice','recommendation'],['audio-output-signal','signal']])$(id).textContent=value[key];
    $('audio-output-meter').value=value.peak;
    $('audio-output-details').textContent=JSON.stringify(outputDiagnostic,null,2);
    strip.dataset.playbackStatus=liveState?.status||'stopped';
    strip.dataset.view=context.view;
    $('audio-current-meta').hidden=!liveState?.current;
    // The monitor already shows the complete current sentence in its main card.
    $('audio-current-text').hidden=!liveState?.current||context.view==='broadcast';
    const routeFault=route?.mode==='external'&&route.status!=='ready';
    const playbackFault=['blocked','error'].includes(liveState?.status);
    const warning=$('audio-output-warning');
    warning.hidden=!routeFault&&!playbackFault;
    warning.textContent=routeFault ? (route.message||'外部声音设备未就绪，请展开声音设置检查。') : playbackFault ? (liveState?.error||value.local||'声音输出需要检查，请展开声音设置。') : '';
  }
  async function refreshAudioOutput() {
    if(outputPolling||Date.now()-outputReadAt<3000)return;
    outputPolling=true;outputReadAt=Date.now();
    try{outputDiagnostic=await api.request('/api/audio/output-status');}
    catch(_){outputDiagnostic={...outputDiagnostic,stale:true,message:'设备检测暂不可用，请检查 Windows 音量混合器'};}
    finally{outputPolling=false;}
  }
  async function refreshPlaybackHeader() {
    const roomId = context.room_id;
    if (!roomId) { headerPlayback = null; heading(); return; }
    if (playbackHeaderPolling) return;
    playbackHeaderPolling = true;
    try {
      const [observed] = await Promise.all([api.request('/api/rooms/' + encodeURIComponent(roomId) + '/state'),refreshAudioOutput()]);
      if (roomId === context.room_id) { headerPlayback = { ...observed, room_id: roomId }; heading(); }
    } catch (_) { /* Keep the last session-bound control when a refresh fails. */ }
    finally { playbackHeaderPolling = false; }
  }
  async function navigate(raw, { automatic = false } = {}) {
    const generation = ++navigationGeneration;
    const dest = normalizeDestination(raw);
    if (!automatic) manualNavigation++;
    const next = { ...context, ...dest };
    if (EXECUTION_VIEWS.has(dest.view)) {
      if (!execution) throw Error('播出工作区尚未加载，请刷新页面。');
      $('workspace-editor').hidden = true; $('execution-editor').hidden = false;
      await execution.open(next);
    } else {
      $('execution-editor').hidden = true; $('workspace-editor').hidden = false;
      await editors.open(next);
    }
    if (generation !== navigationGeneration) return clone(context);
    context = EXECUTION_VIEWS.has(dest.view) ? next : { ...context, ...dest, ...editors.getContext(), view: dest.view };
    rememberLocation();
    assistant?.setContext(context); heading();
    await Promise.all([syncHeader(), refreshPlaybackHeader()]);
    if (!automatic) focus('work');
    return clone(context);
  }
  async function syncHeader() {
    const generation = ++headerGeneration;
    try {
      const snapshot = await api.query('get_workspace_snapshot', context.preparation_id ? { preparation_id: context.preparation_id } : {});
      if (generation !== headerGeneration) return;
      rooms = snapshot.rooms || [];
      const room = rooms.find(r => r.ref.id === context.room_id);
      $('context-room').textContent = room?.body.name || '全部工作区';
      $('context-preparation').textContent = snapshot.preparation ? window.ShengboWorkspaceEditors.preparationTitle(snapshot.preparation, snapshot.preparation_details?.draft) : (context.room_id ? '在左侧选择或新建本场' : '商品资料与直播间');
      $('context-room').title = context.room_id || '全部工作区';
      $('service-status').textContent = '本机服务已连接'; $('service-status').dataset.connected = 'true';
    } catch (e) { $('service-status').textContent = '连接需检查'; notice(e.message); }
  }
  function choose(message, choices) {
    return new Promise(resolve => {
      busyDialog = true;
      const dialog = document.createElement('dialog'); dialog.className = 'wb-confirm-dialog';
      const title = document.createElement('h2'); title.textContent = '发送前确认';
      const text = document.createElement('p'); text.textContent = message;
      const actions = document.createElement('div'); actions.className = 'wb-form-actions';
      function finish(value) { busyDialog = false; dialog.close(); dialog.remove(); resolve(value); }
      for (const choice of choices) { const b = document.createElement('button'); b.type = 'button'; b.className = 'wb-button'; b.textContent = choice.label; b.addEventListener('click', () => finish(choice.value)); actions.append(b); }
      dialog.append(title, text, actions); document.body.append(dialog);
      dialog.addEventListener('cancel', e => { e.preventDefault(); finish('cancel'); }); dialog.showModal();
    });
  }
  async function beforeSend({ selection, conversation }) {
    if (selection && editors.hasDirty(selection)) {
      const choice = await choose('选中的对象还有未保存修改。请先决定使用哪份内容；你的对话输入会保留。', [ { label: '保存修改并继续', value: 'save' }, { label: '放弃修改并继续', value: 'discard' }, { label: '返回编辑', value: 'cancel' } ]);
      if (choice === 'cancel') return false;
      if (choice === 'save') await editors.saveDirty(selection); else await editors.discardDirty(selection);
      if (editors.hasDirty(selection)) throw Error('仍有未保存内容，请先检查保存结果。');
    }
    if (selection) {
      selection = await editors.resolveSelection(selection);
      if (selection.stale) throw Error('选中的内容已出现新修订。请先查看差异，重新指定要修改的版本。输入已保留。');
      const preparations=[...new Set(selectionItems(selection).map(item=>item.preparation_id||item.destination?.preparation_id).filter(Boolean))];
      if(preparations.length>1)throw Error('一次任务只能修改同一本场中的内容，请移除属于其他本场的选中项。');
      const selectedPreparation=preparations[0];
      if (selectedPreparation && conversation && conversation.preparation_id !== selectedPreparation) {
        const choice = await choose('选中的内容属于另一本场。当前对话仍绑定原任务；是否为选中的本场新建对话后发送？', [ { label: '为选中本场新建对话并发送', value: 'new' }, { label: '取消发送', value: 'cancel' } ]);
        if (choice !== 'new') return false;
        const selectedDestination=selectionItems(selection).find(item=>(item.preparation_id||item.destination?.preparation_id)===selectedPreparation)?.destination;
        const next = await assistant.newConversation({ ...selectedDestination, preparation_id: selectedPreparation });
        if (!next) return false;
      }
    }
    return { selection };
  }
  function observed(selection) {
    const refs = selectionItems(selection).flatMap(item=>[item.target?.object_ref,item.target?.product_ref]);
    return refs.filter((r, i) => r && refs.findIndex(x => x?.kind === r.kind && x?.id === r.id && x?.revision === r.revision) === i).map(clone);
  }
  function assistantState(value) {
    lastAssistantState = value;
    editors?.setSelected(value.selected);
    const selectedCount=selectionItems(value.selected).length;
    if(selectionNoticeCount!==null&&selectedCount!==selectionNoticeCount)showSelectionNotice(selectedCount);
    if(value.pendingRequest)pendingEditorLocks=pendingLocks(value.pendingRequest);
    paintEditorLocks();refreshEditorLocks();
    for (const run of value.runs) {
      if (!['COMPLETED', 'READY', 'READY_FOR_REVIEW'].includes(run.status) || seenCompletions.has(run.run_id)) continue;
      seenCompletions.add(run.run_id);
      if (run.artifacts?.length) notice('Agent 已保存新成果。点击对话中的成果入口可读取最新版本；未保存修改会保留。');
    }
    const candidates = value.runs.flatMap(run => (run.artifacts || []).map(a => ({ ...a, run })));
    const latest=candidates.filter(a=>a.destination).at(-1);if(latest){latestArtifact=latest;if(latestResultButton){latestResultButton.hidden=false;latestResultButton.title=latest.label||'查看 Agent 最近保存的成果';}}
    for (const a of candidates) {
      const key = JSON.stringify([a.run.run_id, a.ref, a.destination]);
      if (seenResults.has(key)) continue; seenResults.add(key);
      if (!followResults || !a.destination || value.hasPendingInput || anyDirty() || busyDialog || manualNavigation > 0) continue;
      if (a.destination.preparation_id && a.destination.preparation_id !== context.preparation_id) continue;
      navigate({ ...a.destination, refresh: true }, { automatic: true }).catch(e => notice(e.message));
    }
  }
  editors = global.ShengboWorkspaceEditors.mount({
    container: $('workspace-editor'), api,
    onSelect(selection) { assistant.toggleSelection(selection,{focus:false});showSelectionNotice(selectionItems(assistant.getState().selected).length);focus('work'); },
    onNavigate: dest => navigate(dest),
    onDirty: () => heading(),
    onMaterialUpload: files => assistant.uploadFiles(files),
    onPrepareMessage: text => { assistant.setInput(text); notice('整理要求已补充到右侧，原有未发送内容已保留。检查后发送即可。'); },
    onPrepareGeneration: async (text,taskContext) => { await assistant.prepareTask(text,taskContext); focus('chat'); notice('新增版本要求已放入右侧。检查后发送，Agent 将对照已有话术生成新稿。'); },
    onError: e => notice(e.message),
    onChange({ context: next }) { context = { ...context, ...next }; heading(); assistant?.setContext(context); syncHeader(); }
  });
  if (global.ShengboWorkbenchExecution) execution = global.ShengboWorkbenchExecution.mount({ container: $('execution-editor'), api, showNavigation: false, getContext: () => clone(context), onError: e => notice(e.message), onNavigate: dest => navigate(dest),audioRouteContainer:$('audio-route-controls'),onAudioRouteChange:()=>heading() });
  $('execution-editor').addEventListener('input', heading);
  $('execution-editor').addEventListener('change', heading);
  assistant = global.ShengboAssistant.mount({
    container: $('assistant-container'), title: '播前 Agent', subtitle: '通过对话准备本场，在左侧查看与微调',
    storageKey: 'shengbo-agent-workbench-v2', getViewContext: () => clone(context),
    onNavigate: dest => navigate({ ...dest, refresh: true }).catch(e => notice(e.message)),
    beforeSend, getObservedVersions: observed, onState: assistantState
  });
  for (const tab of document.querySelectorAll('.wb-view-tabs [data-view]')) tab.addEventListener('click', () => navigate({ view: tab.dataset.view, product_id: null, action: null }).catch(e => notice(e.message)));
  $('open-catalog').addEventListener('click', () => navigate({ view: 'products', product_id: null, preparation_id: null, room_id: null, action: null }).catch(e => notice(e.message)));
  $('open-rooms').addEventListener('click', () => navigate({ view: 'rooms', product_id: null, action: null }).catch(e => notice(e.message)));
  $('review-preparation').addEventListener('click', () => navigate({ view: 'playlist', action: 'review' }).catch(e => notice(e.message)));
  $('pause-preparation').addEventListener('click', async () => {
    if (playbackControlBusy || !headerPlayback?.session_id || headerPlayback.room_id !== context.room_id) return;
    const observed = clone(headerPlayback), action = observed.status === 'paused' ? 'resume' : 'pause';
    playbackControlBusy = true; heading();
    try {
      // Pause the current sentence in this session, including across a sentence
      // boundary. An epoch fence here would make ordinary pause clicks fail.
      if (action === 'resume' && !observed.connected_player) await execution.resumeWithSound(clone(context));
      else await api.request('/api/rooms/' + encodeURIComponent(observed.room_id) + '/control', {
        action, action_id: api.uuid(), expected_session_id: observed.session_id
      });
      if (context.room_id === observed.room_id) {
        await refreshPlaybackHeader();
        if (context.view === 'broadcast') await execution?.refresh();
        notice(action === 'pause' ? '语音已暂停，点击“继续播出”从当前位置继续。' : '语音已继续播放。');
      }
    } catch (e) { notice(e.message || '操作未完成，请检查当前播放状态。'); }
    finally { playbackControlBusy = false; await refreshPlaybackHeader(); heading(); }
  });
  $('start-preparation').addEventListener('click', async () => {
    if (startingBroadcast) return;
    if (hasPlaybackBlockingChanges()) { notice('请先保存商品或本场的修改，再开始播出。调试评论草稿可以保留。'); return; }
    const target = clone(context);
    if (!target.room_id || !target.preparation_id) { notice('请先进入要播放的本场。'); return; }
    startingBroadcast = true; heading();
    try { await execution.startWithSound(target); }
    catch (e) { notice(e.message || '未能开始播出，请检查本场准备状态。'); }
    finally { startingBroadcast = false; await refreshPlaybackHeader(); heading(); }
  });
  for (const tab of document.querySelectorAll('[data-execution-view]')) tab.addEventListener('click', () => navigate({ view: tab.dataset.executionView, action: null }).catch(e => notice(e.message)));
  const followLabel = document.createElement('label'); followLabel.className = 'wb-follow';
  const follow = document.createElement('input'); follow.id = 'follow-results'; follow.type = 'checkbox'; follow.setAttribute('aria-label', '自动跟随当前任务成果');
  follow.checked=followResults;follow.addEventListener('change', () => { followResults = follow.checked; manualNavigation = 0;try{localStorage.setItem('shengbo-workbench-follow-results-v2',String(followResults));}catch{} });
  followLabel.append(follow, document.createTextNode('自动跟随成果'));
  document.querySelector('.wb-panel-footer').append(followLabel);
  latestResultButton=document.createElement('button');latestResultButton.type='button';latestResultButton.className='wb-button wb-latest-result';latestResultButton.textContent='查看最新成果';latestResultButton.hidden=!latestArtifact;latestResultButton.addEventListener('click',()=>{if(latestArtifact?.destination)navigate({...latestArtifact.destination,refresh:true}).catch(e=>notice(e.message));});document.querySelector('.wb-panel-footer').append(latestResultButton);
  focus('chat');
  const editorLockTimer=setInterval(()=>{if(!document.hidden){refreshEditorLocks();refreshPlaybackHeader();}},1500);
  global.addEventListener('pagehide',()=>clearInterval(editorLockTimer),{once:true});
  const ready = (async () => {
    await assistant.ready;
    if (manualNavigation) return;
    const conv = assistant.getState().conversation;
    let saved = null; try { saved = JSON.parse(sessionStorage.getItem('shengbo-workbench-location-v2') || 'null'); } catch (_) {}
    context = { ...context, room_id: conv?.room_id || null, preparation_id: conv?.preparation_id || null, ...(saved && VIEWS[saved.view] ? saved : {}) };
    if (!context.room_id && !context.preparation_id && context.view === 'overview') context.view = 'rooms';
    if (EXECUTION_VIEWS.has(context.view) && execution) { $('workspace-editor').hidden = true; $('execution-editor').hidden = false; await execution.open(context); }
    else { if (EXECUTION_VIEWS.has(context.view)) context.view = 'overview'; await editors.open(context); context = { ...context, ...editors.getContext() }; }
    heading(); assistant.setContext(context); await Promise.all([syncHeader(),refreshEditorLocks(),refreshPlaybackHeader()]);
  })().catch(e => { notice('工作区读取失败：' + e.message); $('service-status').textContent = '连接需检查'; });
  global.shengboWorkbench = { ready, focus, navigate, editors, execution, assistant, refreshEditorLocks, state: () => ({ focusMode: document.body.dataset.focusMode, mobilePanel: document.body.dataset.mobilePanel, view: context.view, context: clone(context), hasDirty: editors.hasDirty() || !!execution?.hasDirty?.(), followResults,editorLocks:clone(serverEditorLocks) }) };
  global.addEventListener('beforeunload', e => { if (anyDirty() || lastAssistantState?.hasPendingInput) { e.preventDefault(); e.returnValue = ''; } });
})(window);
