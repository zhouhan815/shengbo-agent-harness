'use strict';
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let library, roomId, sessionId, knowledge, conversation, busy = false, polling = false, generation = 0, renderSignature = '', timer;
const statusLabels = {queued:'已保存 · 等待回复',running:'检索完成 · DeepSeek 正在回复',completed:'已完成',error:'回复失败',interrupted:'生成已中断'};
function notify(message) { $('notice').textContent = message; $('notice').hidden = false; clearTimeout(timer); timer = setTimeout(() => $('notice').hidden = true, 6500); }
async function api(path, method='GET', body) {
  let r;
  try { r = await fetch(path, {method, headers:body ? {'Content-Type':'application/json'} : {}, body:body ? JSON.stringify(body) : undefined}); }
  catch { throw Error('本地服务未连接，请双击桌面“声播”重新启动。'); }
  const data = await r.json(); if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : '请求参数不正确'); return data;
}
function syncURL() { history.replaceState(null, '', '/test?' + new URLSearchParams({room:roomId, ...(sessionId ? {session:sessionId} : {})})); }
function cacheDraft() { if (roomId) localStorage.setItem('reply-draft:' + roomId, JSON.stringify({text:$('lab-comment').value,username:$('lab-username').value,product:$('lab-product').value})); }
function restoreDraft() {
  let d = {}; try { d = JSON.parse(localStorage.getItem('reply-draft:' + roomId) || '{}'); } catch {}
  $('lab-comment').value = d.text || ''; $('lab-username').value = d.username || '测试观众';
  if ([...$('lab-product').options].some(o => o.value === d.product)) $('lab-product').value = d.product;
}
async function loadKnowledge(token=generation) {
  const result = await api(`/api/rooms/${roomId}/reply-knowledge`); if (token !== generation) return;
  const before = $('lab-product').value; knowledge = result;
  $('lab-product').innerHTML = '<option value="">自动识别商品 / 延续对话</option>' + result.products.map(p => `<option value="${esc(p.id)}">商品${String(p.display_number || 1).padStart(3,'0')} · ${esc(p.name)}</option>`).join('');
  if (result.products.some(p => p.id === before)) $('lab-product').value = before;
  $('knowledge-count').textContent = `${result.products.length} 件商品 · ${result.documents.length} 份资料 · ${result.chunk_count} 个片段`;
  $('document-list').innerHTML = result.documents.map(d => `<details class="knowledge-document"><summary>${esc(d.title)}</summary><small>${d.chunk_count} 个片段 · 资料版本 ${esc(d.version)}<br>更新 ${esc(d.updated_at || '未记录')}</small><pre>${esc(d.content)}</pre></details>`).join('') || '<div class="empty-state">本场尚未选择商品。请到直播间配置中从商品库选品。</div>';
  $('reply-prompt').textContent = result.prompt; $('prompt-revision').textContent = '提示词版本 ' + result.prompt_revision;
  $('model-state').textContent = result.deepseek_configured ? 'DeepSeek 已配置' : 'DeepSeek 尚未配置';
}
async function loadSessions(preferred, token=generation) {
  const result = await api(`/api/rooms/${roomId}/reply-sessions`); if (token !== generation) return;
  $('lab-session').innerHTML = result.sessions.map(s => `<option value="${esc(s.id)}">${esc(s.title)} · ${esc(s.created_at.slice(0,16).replace('T',' '))}</option>`).join('');
  if (!result.sessions.length) { const s = await api(`/api/rooms/${roomId}/reply-sessions`,'POST'); if (token !== generation) return; return loadSessions(s.id, token); }
  sessionId = result.sessions.some(s => s.id === preferred) ? preferred : result.sessions[0].id;
  $('lab-session').value = sessionId; localStorage.setItem('reply-last-session:' + roomId, sessionId); syncURL(); await pollConversation(true);
}
async function selectRoom(id, preferred) {
  cacheDraft(); generation++; const token = generation; roomId = id; sessionId = null; conversation = null; renderSignature = ''; busy = false;
  $('lab-room').value = id; localStorage.setItem('reply-last-room', id); $('room-monitor').href = '/#monitor=' + encodeURIComponent(id);
  $('chat-feed').innerHTML = '<div class="empty-state">正在加载本地测试记录…</div>'; $('send-comment').disabled = true;
  await loadKnowledge(token); if (token !== generation) return; restoreDraft();
  await loadSessions(preferred || localStorage.getItem('reply-last-session:' + id), token);
}
function sourceDetails(turn) {
  const r = turn.retrieval; if (!r) return '';
  const cited = new Set((turn.citations || []).map(c => c.chunk_id));
  return `<details class="evidence-details"><summary>查看检索依据 · 检索 ${r.chunks.length} 个片段 · 模型引用 ${cited.size} 个</summary><p class="evidence-meta">${esc(turn.product_name || '待确认商品')} · ${esc(turn.model || '尚未返回模型结果')}<br>检索方式：本地中文关键词 BM25 · 命中分数仅用于排序<br>检索问题：${esc(r.query)}${r.topics?.length ? '<br>主题扩展：' + esc(r.topics.join(' / ')) : ''}<br>耗时 ${turn.elapsed_seconds ?? '—'} 秒 · 本机持久保存</p>${r.chunks.map(c => `<div class="source-card"><strong><span class="source-tag ${cited.has(c.id) ? 'used' : ''}">${cited.has(c.id) ? '模型引用' : '已检索'}</span>${esc(c.title)}</strong><p class="evidence-meta">片段${c.ordinal} · 原文第${c.line}行 · 分数${c.score} · ${esc(c.reason)}<br>资料版本 ${esc(c.version)} · ${esc(c.updated_at || '')}</p><blockquote>${esc(c.text)}</blockquote>${(turn.citations || []).filter(x => x.chunk_id === c.id).map(x => `<p class="citation-quote">核对引用原文：${esc(x.quote)}</p>`).join('')}</div>`).join('') || '<p class="evidence-meta">本轮未检索商品片段，需要先确认商品。</p>'}<p class="evidence-meta">“模型引用”表示来源编号与原文核对通过；回答是否被原文完整支持仍需复核。</p></details>`;
}
function renderConversation() {
  const turns = conversation?.turns || [], signature = JSON.stringify(turns); busy = turns.some(t => ['queued','running'].includes(t.status));
  $('send-comment').disabled = busy || !sessionId; $('new-session').disabled = false;
  $('reply-progress').textContent = busy ? '生成中 · 关闭页面不影响回复' : '全部记录已保存到本机';
  if (signature === renderSignature) return;
  renderSignature = signature; const feed = $('chat-feed'), nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 100;
  const open = new Set([...feed.querySelectorAll('.chat-turn')].filter(el => el.querySelector('.evidence-details')?.open).map(el => el.dataset.id));
  feed.innerHTML = turns.map(t => `<article class="chat-turn" data-id="${esc(t.id)}"><div class="chat-comment"><small>${esc(t.username)} · 模拟评论 · ${esc(t.created_at.slice(11,19))}</small>${esc(t.comment)}</div><div class="chat-answer ${t.status === 'completed' ? '' : ['error','interrupted'].includes(t.status) ? 'answer-error' : 'pending'}"><small>主播回复<span class="answer-status">${esc(statusLabels[t.status])}${t.answer_status === 'needs_confirmation' ? ' · 有待确认信息' : ''}</span></small>${esc(t.status === 'completed' ? t.answer : t.error || '正在准备回复…')}${t.missing_information?.length ? `<p class="missing-info">待确认：${esc(t.missing_information.join('；'))}</p>` : ''}${['error','interrupted'].includes(t.status) ? `<div class="chat-actions"><button data-retry="${esc(t.id)}">重试回复</button></div>` : ''}</div>${sourceDetails(t)}</article>`).join('') || '<div class="empty-state">可以开始提问了。<br>试试问尺码、价格、发货、注意事项；每次回答都会保存检索依据。</div>';
  feed.querySelectorAll('.chat-turn').forEach(el => { if (open.has(el.dataset.id) && el.querySelector('.evidence-details')) el.querySelector('.evidence-details').open = true; });
  feed.querySelectorAll('[data-retry]').forEach(b => b.onclick = async () => { b.disabled = true; try { await api('/api/reply-turns/' + b.dataset.retry + '/retry','POST'); await pollConversation(true); } catch(e) { notify(e.message); b.disabled = false; } });
  if (nearBottom || turns.length === 1) feed.scrollTop = feed.scrollHeight;
}
async function pollConversation(force=false) {
  if (!sessionId || (polling && !force)) return; const id = sessionId, token = generation; polling = true;
  try { const result = await api('/api/reply-sessions/' + id); if (id === sessionId && token === generation) {
    conversation = result; const option = [...$('lab-session').options].find(o => o.value === id);
    if (option) option.textContent = result.session.title + ' · ' + result.session.created_at.slice(0,16).replace('T',' ');
    renderConversation();
  } }
  catch(e) { $('reply-progress').textContent = e.message; }
  finally { polling = false; }
}
$('lab-room').onchange = () => selectRoom($('lab-room').value).catch(e => notify(e.message));
$('lab-session').onchange = () => { sessionId = $('lab-session').value; renderSignature = ''; localStorage.setItem('reply-last-session:' + roomId,sessionId); syncURL(); pollConversation(true); };
$('refresh-knowledge').onclick = async () => { try { await loadKnowledge(); notify('本场商品与知识文档已重新读取'); } catch(e) { notify(e.message); } };
$('new-session').onclick = async () => { try { const token = generation, s = await api(`/api/rooms/${roomId}/reply-sessions`,'POST'); if (token === generation) { renderSignature = ''; await loadSessions(s.id,token); } } catch(e) { notify(e.message); } };
for (const id of ['lab-comment','lab-username','lab-product']) $(id).addEventListener('input',cacheDraft);
$('lab-comment').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!busy && $('lab-comment').value.trim()) $('comment-form').requestSubmit(); } };
$('comment-form').onsubmit = async e => {
  e.preventDefault(); if (busy || !sessionId) return;
  const id = sessionId, boundRoom = roomId; const text = $('lab-comment').value.trim(); if (!text) return;
  $('send-comment').disabled = true; busy = true;
  try { await api(`/api/reply-sessions/${id}/comments`,'POST',{text,username:$('lab-username').value,product_id:$('lab-product').value || null,request_id:crypto.randomUUID()});
    if (id === sessionId && boundRoom === roomId) { $('lab-comment').value = ''; cacheDraft(); await pollConversation(true); } }
  catch(e) { notify(e.message); busy = false; $('send-comment').disabled = false; }
};
for (const [id,type] of [['simulate-enter','enter'],['simulate-like','like']]) $(id).onclick = async () => { try { await api(`/api/rooms/${roomId}/test-events`,'POST',{type,username:$('lab-username').value}); $('simulator-result').textContent = '已发送模拟' + (type === 'enter' ? '进房' : '点赞') + '，可在直播监控中查看。'; } catch(e) { notify(e.message); } };
for (const [id,other,target] of [['show-docs','show-prompt','knowledge-view'],['show-prompt','show-docs','prompt-view']]) $(id).onclick = () => { $(id).setAttribute('aria-selected','true'); $(other).setAttribute('aria-selected','false'); $('knowledge-view').hidden = target !== 'knowledge-view'; $('prompt-view').hidden = target !== 'prompt-view'; };
$('export-test').onclick = () => { if (!conversation) return; const url = URL.createObjectURL(new Blob([JSON.stringify(conversation,null,2)],{type:'application/json'})), a = document.createElement('a'); a.href = url; a.download = '声播评论测试记录.json'; a.click(); URL.revokeObjectURL(url); };
async function boot() {
  library = await api('/api/library'); const q = new URLSearchParams(location.search), chosen = q.get('room') || localStorage.getItem('reply-last-room');
  $('lab-room').innerHTML = library.rooms.map(r => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('');
  const room = library.rooms.find(r => r.id === chosen) || library.rooms.find(r => r.product_ids.length) || library.rooms[0];
  if (!room) throw Error('请先到直播管理创建一个直播间。');
  await selectRoom(room.id, q.get('session')); setInterval(() => pollConversation(), 1200);
}
boot().catch(e => notify(e.message));
