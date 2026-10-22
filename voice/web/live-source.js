// The server owns capture lifecycle and persistence. This page only configures it and polls records.
(() => {
  const byId = id => document.getElementById(id);
  const limit = 200;
  const stateLabels = {disabled:'采集已关闭',disconnected:'未连接',connecting:'正在连接',checking:'正在确认直播状态',connected:'已连接抖音',reconnecting:'正在重连',offline:'对方未开播',not_found:'直播间不存在',error:'连接异常',reconnect_required:'等待重新连接'};
  const typeLabels = {comment:'评论',enter:'进场',like:'点赞',gift:'礼物',social:'社交互动',follow:'关注',share:'分享',audience_snapshot:'人数快照',stats_display:'统计展示'};
  let activeRoom = null, generation = 0, mutation = 0, busy = false, polling = false;
  let config = null, status = null, cursor = 0, eventRows = [], filter = 'all', inputRevision = 0, inputDirty = false, refreshFailed = false;
  const form = byId('live-source-form'), roomInput = byId('live-source-room'), purposeInput = byId('live-source-purpose');
  const controls = ['live-source-save','live-source-enabled','source-monitor-enabled','live-source-reconnect'];

  function timeLabel(value) {
    if (!value) return '时间未知';
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString('zh-CN',{hour12:false});
  }
  function textNumber(value) {
    if (value === null || value === undefined || value === '') return '—';
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString('zh-CN') : String(value);
  }
  function eventCursor(event) {
    const value = Number(event.cursor ?? event.sequence ?? 0);
    return Number.isFinite(value) ? value : 0;
  }
  function eventKey(event) {
    return String(event.id || (eventCursor(event) ? `cursor:${eventCursor(event)}` : `${event.time}|${event.type}|${event.username}|${event.text}|${event.external_room_id}`));
  }
  function mergeEvents(incoming) {
    const rows = new Map(eventRows.map(event => [eventKey(event),event]));
    for (const event of incoming || []) if (event && typeof event === 'object') rows.set(eventKey(event),event);
    eventRows = [...rows.values()].sort((a,b) => eventCursor(b)-eventCursor(a) || String(b.time || '').localeCompare(String(a.time || ''))).slice(0,limit);
  }
  function isCurrent(id, revision) { return activeRoom === id && generation === revision && typeof roomId !== 'undefined' && roomId === id; }
  function roomVisible() { return activeRoom && roomId === activeRoom && (!byId('view-room').hidden || !byId('view-monitor').hidden); }
  function feedback(message, error = false) {
    const node = byId('live-source-feedback');
    node.textContent = message || ''; node.hidden = !message; node.dataset.error = String(error);
  }
  function setBusy(value) {
    busy = value;
    for (const id of controls) byId(id).disabled = value || !activeRoom;
    // Prevent a successful save response from replacing an edit made during that save.
    roomInput.disabled = purposeInput.disabled = value;
  }
  function metricMarkup(key, label, fallback) {
    const metric = status?.metrics?.[key] || {};
    const observed = metric.updated_at ? new Date(metric.updated_at).valueOf() : 0;
    const old = !!metric.stale || refreshFailed || !status?.connected || (observed > 0 && Date.now()-observed > 60000);
    const hasValue = metric.value !== null && metric.value !== undefined && metric.value !== '';
    const hasRaw = metric.raw !== null && metric.raw !== undefined && String(metric.raw).trim() !== '';
    const observedValue = hasValue || hasRaw;
    const rawOnly = !hasValue && hasRaw;
    const displayValue = hasValue ? textNumber(metric.value) : hasRaw ? String(metric.raw) : '—';
    const badge = observedValue ? old ? '历史值 / 已过期' : '口径待核对' : '尚未采集';
    const basis = `${rawOnly ? '平台原始展示值，保留原文字、不换算单位；口径待核对。' : ''}${metric.basis || fallback}`;
    return `<article class="source-metric" data-stale="${old}"><div class="source-metric-heading"><span>${esc(label)}</span><small>${badge}</small></div><strong>${esc(displayValue)}</strong><p>${esc(basis)}</p><small>${observedValue ? `更新：${esc(timeLabel(metric.updated_at))}` : '连接后等待平台返回快照'}${hasValue && hasRaw && String(metric.raw) !== String(metric.value) ? ` · 原始值：${esc(metric.raw)}` : ''}</small></article>`;
  }
  function renderMetrics() {
    byId('live-source-metrics').innerHTML = metricMarkup('online','在线人数（候选）','平台人数回传，尚未与主播后台核对；不是进场事件条数。')+
      metricMarkup('total_visits','累计观看 / 人次（候选）','保留平台返回值，去重与累计规则尚未核对。')+
      metricMarkup('likes_total','累计点赞（平台回传）','平台回传累计值；下方“点赞事件”统计的是消息条数。');
  }
  function eventDescription(event) {
    if (event.text) return String(event.text);
    if (event.type === 'enter') return '进入了直播间';
    if (event.type === 'like') return Number(event.count) > 0 ? `点赞 × ${textNumber(event.count)}` : '点赞了直播间';
    if (event.type === 'gift') return Number(event.count) > 0 ? `赠送礼物 × ${textNumber(event.count)}` : '发送了礼物';
    if (event.type === 'audience_snapshot') return '收到平台人数快照，口径待核对';
    if (event.type === 'stats_display') return '收到平台统计展示数据，口径待核对';
    return typeLabels[event.type] || '收到平台事件';
  }
  function renderEvents() {
    const rows = eventRows.filter(event => filter === 'all' || (filter === 'other' ? !['comment','enter','like'].includes(event.type) : event.type === filter));
    const host = byId('live-source-events');
    // Keep a reader's position while reading earlier records. At the top, follow the newest events.
    const oldTop = host.scrollTop, oldHeight = host.scrollHeight;
    host.innerHTML = rows.map(event => `<article class="source-event" data-type="${esc(event.type)}"><div class="source-event-meta"><span class="source-event-type">${esc(typeLabels[event.type] || event.type || '其他')}</span><time>${esc(timeLabel(event.time))}</time><span>抖音真实事件</span><span>外部房间 ${esc(event.external_room_id || '未知')}</span>${event.purpose === 'external_test' ? '<span>公开房间测试</span>' : event.purpose === 'own_room' ? '<span>自己的直播间</span>' : ''}</div><strong>${esc(event.username || event.nickname || '平台事件')}</strong><p class="source-event-content">${esc(eventDescription(event))}</p></article>`).join('') || `<div class="empty-state">${eventRows.length ? '这个类别暂时没有事件，试试“全部”。' : status?.enabled ? '采集已开启，正在等待真实事件；没有数据时不会生成模拟事件。' : '保存来源并开启采集后，评论、进场、点赞等事件会逐条出现在这里。'}</div>`;
    if (oldTop > 20) host.scrollTop = oldTop + (host.scrollHeight-oldHeight);
    else host.scrollTop = 0;
    for (const button of document.querySelectorAll('[data-source-filter]')) button.setAttribute('aria-pressed',String(button.dataset.sourceFilter === filter));
  }
  function renderStatus() {
    const enabled = !!(status?.enabled ?? config?.enabled), state = refreshFailed ? 'error' : status?.state || 'disconnected';
    const label = refreshFailed ? '状态更新失败' : stateLabels[state] || state;
    for (const id of ['live-source-status','source-monitor-status']) { byId(id).textContent = label; byId(id).dataset.state = state; }
    for (const id of ['live-source-enabled','source-monitor-enabled']) byId(id).checked = enabled;
    byId('live-source-switch-note').textContent = busy ? '正在提交操作…' : enabled ? '采集开关已开启。后台独立运行，关闭页面不影响采集。' : '开启时保存当前来源并连接；关闭后历史记录仍保留。';
    const pieces = [`采集开关：${enabled ? '开启' : '关闭'}`];
    if (config?.external_room_id || status?.external_room_id) pieces.push(`外部房间：${config?.external_room_id || status.external_room_id}`);
    if (status?.message) pieces.push(status.message);
    if (status?.last_event_at) pieces.push(`最近事件：${timeLabel(status.last_event_at)}${status.last_event_stale ? '（历史记录 / 已过期）' : ''}`);
    if (status?.collector_ready === false) pieces.push('本机采集组件尚未就绪，请检查组件安装与后端记录');
    byId('live-source-connection').textContent = pieces.join(' · ');
    const counts = status?.counts || {}, historic = Object.values(status?.history_counts || {}).reduce((sum,value) => sum+(Number(value) || 0),0);
    byId('live-source-counts').textContent = `本次采集：评论 ${textNumber(counts.comment ?? 0)} 条 · 进场 ${textNumber(counts.enter ?? 0)} 条 · 点赞事件 ${textNumber(counts.like ?? 0)} 条${historic ? ` · 本机历史 ${textNumber(historic)} 条` : ''}`;
    const external = config?.external_room_id || status?.external_room_id;
    byId('source-monitor-room').textContent = external ? `外部房间 ${external} · ${config?.purpose === 'own_room' ? '自己的直播间' : '公开房间测试'}` : '请先在接入调试中填写抖音直播间链接';
    byId('source-monitor-note').textContent = refreshFailed ? '暂时无法读取后台状态；显示的指标可能已经过期。可进入接入调试查看。' : `${enabled ? '采集在后台持续运行。' : '采集已关闭，历史事件保留。'}声播播出暂停或结束不会自动切换采集开关${status?.last_event_at ? `；最近事件 ${timeLabel(status.last_event_at)}${status.last_event_stale ? '（历史记录 / 已过期）' : ''}` : ''}。`;
    byId('source-audience-badge').textContent = enabled && status?.connected && !refreshFailed ? '抖音采集中 · 来源分别记录' : '保留历史 · 真实 / 模拟分开';
    byId('live-source-reconnect').hidden = !enabled || !!status?.connected || ['connecting','reconnecting'].includes(state);
    renderMetrics();
  }
  function applyEnvelope(result, fillForm = false) {
    if (result.config) config = result.config;
    if (result.status) status = result.status;
    mergeEvents(result.events);
    const next = Number(result.next_cursor);
    if (Number.isFinite(next)) cursor = Math.max(cursor,next);
    for (const event of result.events || []) cursor = Math.max(cursor,eventCursor(event));
    if (fillForm && config) {
      roomInput.value = config.url || config.external_room_id || '';
      purposeInput.value = config.purpose || 'external_test';
      inputDirty = false;
    }
    refreshFailed = false;
    renderStatus(); renderEvents();
  }
  async function operate(action, fromMonitor = false) {
    if (!activeRoom || activeRoom !== roomId || busy) return;
    const id = activeRoom, revision = generation, editRevision = inputRevision;
    let body;
    if (action !== 'disconnect') {
      const room = fromMonitor ? config?.url || config?.external_room_id || '' : roomInput.value.trim();
      if (!room) { feedback('请先填写抖音直播间的网页链接或数字房间号。',true); renderStatus(); if (fromMonitor) openDebug(); else roomInput.focus(); return; }
      body = {room,purpose:fromMonitor ? config?.purpose || 'external_test' : purposeInput.value};
    }
    mutation += 1;
    setBusy(true); feedback(''); renderStatus();
    try {
      const suffix = action === 'save' ? '' : `/${action}`;
      const result = await api(`/api/rooms/${encodeURIComponent(id)}/live-source${suffix}`,action === 'save' ? 'PUT' : 'POST',body);
      if (!isCurrent(id,revision)) return;
      applyEnvelope(result,!fromMonitor && action !== 'disconnect' && editRevision === inputRevision);
      feedback(action === 'save' ? (status?.enabled ? '来源配置已保存，采集开关保持开启。请以连接状态和真实事件判断是否接通。' : '来源配置已保存。开启“持续采集”后连接平台。') : action === 'disconnect' ? '采集已关闭，收到的事件仍保存在本机。' : '连接请求已提交。请观察连接状态与真实事件，开关开启不等于连接成功。');
    } catch (error) {
      if (isCurrent(id,revision)) { feedback(error.message || '操作失败，请重试。',true); renderStatus(); }
    } finally {
      if (isCurrent(id,revision)) { setBusy(false); renderStatus(); }
    }
  }
  async function poll() {
    if (!roomVisible() || document.hidden || busy || polling) return;
    const id = activeRoom, revision = generation, actionRevision = mutation;
    polling = true;
    try {
      const result = await api(`/api/rooms/${encodeURIComponent(id)}/live-source/events?after=${encodeURIComponent(cursor)}`);
      if (!isCurrent(id,revision) || mutation !== actionRevision || busy) return;
      const recovering = refreshFailed;
      applyEnvelope(result,!config && !inputDirty);
      if (recovering) feedback('');
    } catch (error) {
      if (isCurrent(id,revision) && mutation === actionRevision) { refreshFailed = true; renderStatus(); feedback(`状态更新失败：${error.message || '请检查声播服务'}。输入内容已保留。`,true); }
    } finally { polling = false; }
  }
  function openDebug() { view('room'); setupTab('live-source'); byId('step-live-source').scrollIntoView({block:'start',behavior:'smooth'}); }
  window.loadLiveSourceRoom = async id => {
    const revision = ++generation;
    const changed = activeRoom !== id;
    activeRoom = id; mutation += 1; cursor = 0; busy = false;
    if (changed) { config = null; status = null; eventRows = []; filter = 'all'; roomInput.value = ''; purposeInput.value = 'external_test'; inputDirty = false; inputRevision += 1; }
    refreshFailed = false; feedback(''); setBusy(true); renderStatus(); renderEvents();
    const editRevision = inputRevision;
    try {
      const result = await api(`/api/rooms/${encodeURIComponent(id)}/live-source`);
      if (!isCurrent(id,revision)) return;
      applyEnvelope(result,!inputDirty && editRevision === inputRevision);
    } catch (error) {
      if (isCurrent(id,revision)) { refreshFailed = true; renderStatus(); feedback(`来源配置加载失败：${error.message || '请检查服务'}。可以保留输入后重试。`,true); }
    } finally { if (isCurrent(id,revision)) { setBusy(false); renderStatus(); } }
  };
  form.addEventListener('submit',event => { event.preventDefault(); void operate('save'); });
  for (const field of [roomInput,purposeInput]) field.addEventListener('input',() => { inputRevision += 1; inputDirty = true; });
  byId('live-source-enabled').addEventListener('change',event => { void operate(event.target.checked ? 'connect' : 'disconnect'); });
  byId('source-monitor-enabled').addEventListener('change',event => { void operate(event.target.checked ? 'connect' : 'disconnect',true); });
  byId('live-source-reconnect').addEventListener('click',() => { void operate('connect'); });
  byId('source-open-debug').addEventListener('click',openDebug);
  for (const button of document.querySelectorAll('[data-source-filter]')) button.addEventListener('click',() => { filter = button.dataset.sourceFilter; byId('live-source-events').scrollTop = 0; renderEvents(); });
  setBusy(false); renderMetrics();
  const timer = setInterval(() => { void poll(); },1500);
  // Closing the UI must never disconnect the server-side collector.
  window.addEventListener('beforeunload',() => clearInterval(timer));
})();
