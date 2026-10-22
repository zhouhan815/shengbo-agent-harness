// Room preparation and persistent background tasks. Playback/CRUD utilities live in control.js.
let audioInfo = null, backgroundJobs = [], removedScripts = new Set(), pollingJobs = false;
const knownJobResults = new Set();
const taskLabels = {queued:'等待中',running:'生成中',completed:'已完成',error:'失败',interrupted:'已中断',not_run:'未执行'};
function productNumber(id) { const p = library?.products.find(p => p.id === id); return `商品${String(p?.display_number || 1).padStart(3,'0')}`; }
function versionNumber(s) { return s?.version_number || library.scripts.filter(v => v.product_id === s?.product_id).findIndex(v => v.id === s?.id)+1; }
function scriptName(s) { return `${productNumber(s.product_id)} · 第${versionNumber(s)}版`; }
function roomScripts() { return order.map(id => library.scripts.find(s => s.id === id)).filter(Boolean); }
function matchingAudio() { const t = ttsForm(); return audioInfo && audioInfo.tts.provider === t.tts_provider && audioInfo.tts.voice === t.tts_voice && audioInfo.tts.model === t.tts_model; }
function setupTab(name) {
  document.querySelectorAll('.setup-step').forEach(el => el.hidden = el.id !== `step-${name}`);
  document.querySelectorAll('[data-tab]').forEach(el => { el.setAttribute('aria-selected',String(el.dataset.tab === name)); el.tabIndex = el.dataset.tab === name ? 0 : -1; });
}
document.querySelectorAll('[data-tab]').forEach(el => {
  el.onclick = () => setupTab(el.dataset.tab);
  el.onkeydown = e => { if (!['ArrowLeft','ArrowRight'].includes(e.key)) return; e.preventDefault(); const tabs = [...document.querySelectorAll('[data-tab]')], i = tabs.indexOf(el), target = tabs[(i+(e.key === 'ArrowRight' ? 1 : tabs.length-1))%tabs.length]; setupTab(target.dataset.tab); target.focus(); };
});
document.querySelectorAll('[data-next-tab]').forEach(el => el.onclick = () => setupTab(el.dataset.nextTab));

async function enterRoom(id, monitor = false) {
  if (dirty && roomId && roomId !== id) await saveRoom();
  roomId = id; roomConfig = library.rooms.find(r => r.id === id); if (!roomConfig) throw Error('直播间不存在');
  chosenProducts = new Set(roomConfig.product_ids || []);
  order = [...(roomConfig.plan_mode === 'all' ? roomConfig.script_order || [] : roomConfig.script_order?.length ? roomConfig.script_order : roomConfig.selected_script_ids || [])]; removedScripts = new Set(); audioInfo = null;
  const f = $('config-form'); f.elements.name.value = roomConfig.name; f.elements.version_count.value = roomConfig.version_count; f.elements.loop.checked = roomConfig.loop; f.elements.tts_provider.value = roomConfig.tts_provider; updateVoiceChoices(roomConfig.tts_voice); f.elements.tts_model.value = roomConfig.tts_model;
  loadCommerceEditor(); dirty = false; $('save-status').textContent = '已保存'; $('output-target').value = 'browser'; setupTab('voice'); renderRoom(); connectMonitor(id);
  renderState(await api(`/api/rooms/${id}/state`)); view(monitor ? 'monitor' : 'room'); await checkAudio();
  if (typeof loadLiveSourceRoom === 'function') void loadLiveSourceRoom(id);
}
function renderLibrary() {
  $('service-status').textContent = library.service.deepseek_configured ? 'DeepSeek 已配置' : 'DeepSeek 尚未配置'; $('product-count').textContent = `${library.products.length} 款商品`;
  $('products').innerHTML = library.products.map(p => {
    return `<article class="product-card"><div class="product-top">${p.image ? `<img class="product-thumb" alt="商品图片" src="${esc(p.image)}">` : '<div class="product-thumb">▥</div>'}<div><small class="muted">${productNumber(p.id)} · SKU ${esc(p.sku_id||p.id)} · ${p.sale_status==='unavailable'?'不可售':'可售'}</small><h2>${esc(p.name)}</h2></div></div><div class="price">${esc(p.price)} <span>${esc(p.currency)}</span></div><p class="product-summary">${esc(p.selling_points)}</p><div class="muted">话术在直播间内生成与管理</div><div class="product-actions"><button data-edit="${p.id}">编辑商品</button><button data-configure-product="${p.id}">到直播间生成话术</button></div></article>`;
  }).join('') || '<div class="panel">商品库为空，请添加商品。</div>';
  document.querySelectorAll('[data-edit]').forEach(el => el.onclick = () => openProduct(el.dataset.edit));
  document.querySelectorAll('[data-configure-product]').forEach(el => el.onclick = () => { if (!roomId) { view('rooms'); notify('请先创建或进入直播间'); } else { if(library.products.find(p=>p.id===el.dataset.configureProduct)?.sale_status==='unavailable')return notify('不可售商品不能加入直播间'); chosenProducts.add(el.dataset.configureProduct); markDirty(); renderRoom(); view('room'); setupTab('generate'); } });
  $('rooms').innerHTML = library.rooms.map(r => `<article class="product-card room-card"><div class="toolbar"><span class="room-icon">◉</span><span class="badge" data-room-status="${r.id}">待配置</span></div><h2>${esc(r.name)}</h2><p class="muted">${r.product_ids.length} 件商品 · ${(r.plan_mode === 'all' ? r.script_order || [] : r.script_order?.length ? r.script_order : r.selected_script_ids || []).length} 版本场话术</p><p class="muted">普通话 · ${r.tts_provider === 'minimax' ? 'MiniMax' : '本机音色'}</p><div class="product-actions"><button data-room="${r.id}" class="primary">配置直播间</button><button data-monitor="${r.id}">直播监控</button><a class="button-link" href="/test?room=${encodeURIComponent(r.id)}">评论测试</a></div></article>`).join('');
  document.querySelectorAll('[data-room]').forEach(el => el.onclick = () => enterRoom(el.dataset.room).catch(e => notify(e.message)));
  document.querySelectorAll('[data-monitor]').forEach(el => el.onclick = () => enterRoom(el.dataset.monitor,true).catch(e => notify(e.message)));
  for (const r of library.rooms) api(`/api/rooms/${r.id}/state`).then(s => { const node = document.querySelector(`[data-room-status="${r.id}"]`); if (node) { node.textContent = s.status === 'stopped' ? '待开播' : statuses[s.status]; node.classList.toggle('live',s.status === 'playing'); } }).catch(() => {});
  if (roomId) renderRoom();
}
function renderRoom() {
  const valid = new Set(library.products.map(p => p.id)); chosenProducts = new Set([...chosenProducts].filter(id => valid.has(id)));
  order = order.filter(id => { const s = library.scripts.find(s => s.id === id); return !s || chosenProducts.has(s.product_id); });
  const products = library.products.filter(p => chosenProducts.has(p.id));
  $('room-products').innerHTML = products.map(p => `<div class="room-product"><div><strong>${productNumber(p.id)} · ${esc(p.name)}</strong><p class="muted">日常售价 ${esc(p.price)} ${esc(p.currency)} · ${p.sale_status==='unavailable'?'不可售 · ':''}引用商品库</p></div><button data-room-product-config="${p.id}">${commerceDraft?.product_overrides?.[p.id]?'✓ 已配置 · 本场配置':'本场配置'}</button><button data-remove-product="${p.id}" class="danger">移出本场</button></div>`).join('') || '<div class="empty-state">尚未选择商品，点击“从商品库选择”。</div>';
  paintCommerceScopes();syncRoomQuoteContext();document.querySelectorAll('[data-room-product-config]').forEach(el=>el.onclick=()=>openRoomProduct(el.dataset.roomProductConfig));
  document.querySelectorAll('[data-remove-product]').forEach(el => el.onclick = () => { chosenProducts.delete(el.dataset.removeProduct); markDirty(); renderRoom(); });
  $('generation-products').innerHTML = products.map(p => `<div class="room-product"><div><strong>${productNumber(p.id)} · ${esc(p.name)}</strong><p class="muted">本场已有 ${roomScripts().filter(s => s.product_id === p.id).length} 版</p></div><button data-room-generate="${p.id}">为此商品新增话术</button></div>`).join('') || '<div class="empty-state">请先在第二步选择商品。</div>';
  document.querySelectorAll('[data-room-generate]').forEach(el => el.onclick = () => generateScripts([el.dataset.roomGenerate]));
  $('script-list').innerHTML = roomScripts().map((s,i) => {
    const a = matchingAudio() ? audioInfo.scripts.find(v => v.script_id === s.id) : null;
    const state = a?.status === 'ready' ? '语音已生成' : a?.ready ? `语音 ${a.ready}/${a.total}句` : matchingAudio() ? '语音未生成' : '语音待检查';
    return `<div class="script-row"><span class="order-number">${i+1}</span><div class="script-info"><strong>${scriptName(s)} <span class="muted">${esc(s.product_snapshot.name)}</span></strong><p><span class="status-chip ${a?.status === 'ready' ? 'ok' : ''}">${state}</span> ${scriptSegments(s).length}段 · ${s.sentences.length}句${stale(s)||a?.configuration_stale ? ' · 商品或本场配置已更新，请更新话术' : ''}</p></div><div class="updown"><button data-script-up="${s.id}" ${i === 0 ? 'disabled' : ''} aria-label="上移${scriptName(s)}">↑</button><button data-script-down="${s.id}" ${i === order.length-1 ? 'disabled' : ''} aria-label="下移${scriptName(s)}">↓</button></div><button data-script="${s.id}">分段查看 / 编辑</button><button data-remove-script="${s.id}" class="danger">删除</button></div>`;
  }).join('') || '<div class="empty-state">本场没有话术，请到第三步为本直播间生成话术。</div>';
  document.querySelectorAll('[data-script]').forEach(el => el.onclick = () => openScript(el.dataset.script));
  document.querySelectorAll('[data-remove-script]').forEach(el => el.onclick = () => { order = order.filter(id => id !== el.dataset.removeScript); removedScripts.add(el.dataset.removeScript); markDirty(); renderRoom(); });
  for (const direction of ['up','down']) document.querySelectorAll(`[data-script-${direction}]`).forEach(el => el.onclick = () => { const i = order.indexOf(el.getAttribute(`data-script-${direction}`)), j = i + (direction === 'up' ? -1 : 1); if (j >= 0 && j < order.length) { [order[i],order[j]] = [order[j],order[i]]; markDirty(); renderRoom(); } });
  updateSummary();
}
function updateSummary() {
  const scripts = roomScripts(), n = scripts.reduce((sum,s) => sum+s.sentences.length,0), count = Number($('config-form').elements.version_count.value) || 2;
  $('plan-summary').textContent = `本场：${chosenProducts.size} 件商品 · ${scripts.length} 版话术 · ${n} 句`;
  $('generation-summary').textContent = `本次将为 ${chosenProducts.size} 件商品，每件新增 ${count} 版，共 ${chosenProducts.size*count} 版话术。`;
  $('warmup-status').textContent = matchingAudio() ? `当前音色：本场 ${n} 句，已生成 ${scripts.reduce((sum,s) => sum+(audioInfo.scripts.find(v => v.script_id === s.id)?.ready || 0),0)} 句。生成语音会检查整个列表，已有有效语音直接复用。` : '保存设置并检查语音后显示状态；更换音色需重新生成对应语音。';
  $('generate-all').disabled = !chosenProducts.size || backgroundJobs.some(j => j.room_id === roomId && j.kind === 'scripts' && ['queued','running'].includes(j.status));
  $('warmup-audio').disabled = !scripts.length || backgroundJobs.some(j => j.room_id === roomId && j.kind === 'audio' && ['queued','running'].includes(j.status));
}
async function saveRoom() {
  if (!roomId) throw Error('请先创建直播间'); const id = roomId, f = $('config-form');
  const result = await api(`/api/rooms/${id}`,'PUT',{name:f.elements.name.value,product_ids:[...chosenProducts],version_count:Number(f.elements.version_count.value),loop:f.elements.loop.checked,plan_mode:'all',script_order:order,base_script_ids:roomConfig.script_order || [],removed_script_ids:[...removedScripts],selected_script_ids:roomScripts().map(s => s.id),...ttsForm()});
  if (roomId !== id) return result;
  roomConfig = result; order = [...result.script_order]; dirty = false; removedScripts.clear(); $('save-status').textContent = '已保存'; $('page-title').textContent = result.name; library.rooms[library.rooms.findIndex(r => r.id === id)] = result; renderRoom(); return result;
}
async function checkAudio() { if (!roomId) return; const id = roomId; try { const result = await api(`/api/rooms/${id}/audio-status`); if (roomId === id) { const changed = JSON.stringify(result) !== JSON.stringify(audioInfo); audioInfo = result; if (changed) renderRoom(); else updateSummary(); } } catch(e) { notify(e.message); } }
function showTasks() { $('task-drawer').hidden = false; $('show-tasks').setAttribute('aria-expanded','true'); }
$('show-tasks').onclick = showTasks; $('close-tasks').onclick = () => { $('task-drawer').hidden = true; $('show-tasks').setAttribute('aria-expanded','false'); };
async function generateScripts(ids) {
  try { await saveRoom(); const job = await api(`/api/rooms/${roomId}/jobs`,'POST',{kind:'scripts',product_ids:ids,version_count:Number($('config-form').elements.version_count.value)}); backgroundJobs.unshift(job); renderTasks(); showTasks(); notify(`已开始生成${job.total}版话术，可以继续其他操作`); }
  catch(e) { notify(e.message); }
}
$('generate-all').onclick = () => generateScripts([...chosenProducts]);
$('warmup-audio').onclick = async () => { try { await saveRoom(); const job = await api(`/api/rooms/${roomId}/jobs`,'POST',{kind:'audio'}); backgroundJobs.unshift(job); renderTasks(); showTasks(); notify('正在检查并补齐本场语音，已有语音直接复用'); } catch(e) { notify(e.message); } };
$('check-audio').onclick = async () => { try { await saveRoom(); await checkAudio(); } catch(e) { notify(e.message); } };
$('save-plan').onclick = async () => { try { await saveRoom(); await checkAudio(); notify('本场顺序已保存'); } catch(e) { notify(e.message); } };
$('config-form').elements.version_count.oninput = () => { markDirty(); updateSummary(); };
$('config-form').addEventListener('input', () => { if (library && roomId) renderRoom(); });
function renderTasks() {
  const running = backgroundJobs.filter(j => ['queued','running'].includes(j.status)); $('task-count').textContent = running.length; $('show-tasks').classList.toggle('task-running',running.length > 0);
  $('task-list').innerHTML = backgroundJobs.map(j => `<article class="task-card"><div class="toolbar"><strong>${j.kind === 'scripts' ? '生成话术' : '生成语音'}</strong><span class="status-chip ${j.status === 'completed' ? 'ok' : ''}">${taskLabels[j.status]}</span></div><p class="muted">${esc(library?.rooms.find(r => r.id === j.room_id)?.name || j.room_id)} · ${j.done}/${j.total}${j.kind === 'scripts' ? '版' : '句'}</p><progress max="${j.total || 1}" value="${j.done}" aria-label="任务总进度"></progress>${j.items.map(item => `<div class="task-item"><div><strong>${productNumber(item.product_id)} · 第${item.version_number || versionNumber(library.scripts.find(s => s.id === item.script_id))}版</strong><span>${taskLabels[item.status]}</span></div><small>${esc(item.product_name)} · ${item.done}/${item.total}${j.kind === 'scripts' ? '版' : '句'}${item.reused ? ` · 复用${item.reused}句` : ''}</small><progress max="${item.total || 1}" value="${item.done}" aria-label="商品版本生成进度"></progress>${item.status === 'running' && j.kind === 'scripts' ? '<small>正在生成本版文本，模型返回后更新进度。</small>' : ''}</div>`).join('')}${j.error ? `<p class="task-error">${esc(j.error)}</p>` : ''}</article>`).join('') || '<div class="empty-state">暂无生成任务。</div>';
  if (library && roomId) updateSummary();
}
async function pollJobs() {
  if (pollingJobs) return; pollingJobs = true;
  try {
    const next = (await api('/api/jobs')).jobs, results = next.flatMap(j => (j.result_script_ids || []).map(id => ({id,room:j.room_id}))), fresh = results.filter(v => !knownJobResults.has(v.id)); backgroundJobs = next;
    if (fresh.length) {
      await refresh(); const saved = library.rooms.find(r => r.id === roomId);
      for (const v of fresh) { if (v.room === roomId && saved?.script_order.includes(v.id) && !removedScripts.has(v.id) && !order.includes(v.id) && chosenProducts.has(library.scripts.find(s => s.id === v.id)?.product_id)) order.push(v.id); knownJobResults.add(v.id); }
      if (roomId) renderRoom();
    }
    renderTasks(); if (roomId) await checkAudio();
  } catch(e) { $('task-count').textContent = '连接中'; }
  finally { pollingJobs = false; }
}
setInterval(pollJobs,2000);

$('start-broadcast').onclick = async () => {
  if (currentState && !['stopped','completed'].includes(currentState.status)) { view('monitor'); return; }
  const button = $('start-broadcast'); button.disabled = true;
  try {
    $('preview-audio').pause(); $('voice-audio').pause(); const local = $('output-target').value === 'browser'; if (local) await localPlayer.unlock();
    await saveRoom(); if (!order.length) throw Error('本场没有话术，请先为本直播间生成话术');
    if (roomScripts().some(s => stale(s))) { setupTab('scripts'); throw Error('商品资料已更新，请先更新对应话术并重新生成语音'); }
    await checkAudio(); if(audioInfo?.scripts.some(s=>s.configuration_stale)){setupTab('scripts');throw Error('商品或本场配置已更新，请重新生成对应话术和语音')} if (!audioInfo?.all_ready) { setupTab('scripts'); throw Error('本场语音尚未全部生成，请点击生成语音'); }
    if (await command('start',{script_ids:order})) { if (local) await localPlayer.connect(roomId); else localPlayer.disconnect(); view('monitor'); notify(local ? '已开播，正在播放本地语音；OBS 链接已生成' : '已开播，请复制 OBS 链接导入浏览器来源'); }
  } catch(e) { notify(e.message); } finally { button.disabled = false; }
};
// Reuse the playback rendering and controls; compact the queue through CSS.
const renderPlaybackState = renderState;
renderState = function(state) {
  renderPlaybackState(state);
  if (state.current) {
    const c = state.current; $('current-location').textContent = `${productNumber(c.product_id)} · ${c.version} · ${c.ordinal ? `第${c.ordinal}/${c.total_sentences}句` : '插播'}`;
    $('current-card').innerHTML = `<small>${productNumber(c.product_id)} · ${esc(c.version)} · ${c.ordinal ? `第${c.ordinal}/${c.total_sentences}句` : '插播'} · ${state.tts?.provider === 'minimax' ? 'MiniMax' : '本机普通话'}</small>${esc(c.text)}`;
  }
  $('start-broadcast').textContent = !['stopped','completed'].includes(state.status) ? '进入直播监控' : '开播并获取 OBS 链接';
  if (state.status === 'preparing') $('output-status').textContent = state.current?.is_insertion ? '正在生成插播语音' : '正在读取本地语音';
  $('audience-feed').innerHTML = [...(state.audience_events || [])].reverse().map(e => {
    const time=String(e.time||e.received_at||''),source=e.source==='simulated'?'模拟事件':e.platform==='douyin'?`抖音实采 · ${e.external_room_id}`:'观众事件';
    const action={like:'点赞了直播间',enter:'进入了直播间',comment:'发布评论',gift:'赠送礼物',social:'进行了社交互动',follow:'关注了主播',share:'分享了直播间'}[e.type]||'产生互动';
    return `<div class="audience-event"><small>${esc(time.slice(11,19))} · ${esc(source)} · ${esc(time.slice(0,10))}</small><strong>${esc(e.username||'匿名观众')}</strong> ${esc(action)}${e.type==='like'&&e.count?` · 本条${esc(e.count)}赞`:''}${e.text?`<p>${esc(e.text)}</p>`:''}</div>`;
  }).join('') || '<div class="empty-state">暂无观众事件。抖音接入可在开播前调试，模拟输入请前往评论测试台。</div>';
};
window.addEventListener('DOMContentLoaded', () => boot().then(pollJobs));
