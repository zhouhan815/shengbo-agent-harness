/* 声播播前助手。业务与运行状态来自服务；过程开关只控制可见性。 */
(function (global) {
  'use strict';
  const VERSION = 'PRELIVE-2.3';
  const TERMINAL = new Set(['COMPLETED', 'READY', 'READY_FOR_REVIEW', 'FAILED', 'BLOCKED', 'CANCELLED', 'SUPERSEDED']);
  const STATUS = { QUEUED: '等待执行', ASSESSING_INTENT: '理解任务', AUTHORIZING: '检查授权', RUNNING: '正在执行', WAITING_USER: '等待你确认', WAITING_JOBS: '等待后台工作', FINALIZING: '正在收拢后台工作', CANCELLING: '正在取消', COMPLETED: '已完成', READY: '已就绪', READY_FOR_REVIEW: '待人工检查', RECOVERING: '正在恢复', INTERRUPTED: '已暂停等待恢复', DENIED: '检查未通过', CORRECTING: '补齐完成证据', DEGRADED: '记录待恢复', FAILED: '执行失败', BLOCKED: '需要处理阻碍', CANCELLED: '已取消', SUPERSEDED: '已被新任务替代', PREPARED: '候选结果已生成', ACCEPTED: '结果已保存', SUCCEEDED: '已完成', PENDING: '等待中', SKIPPED: '已跳过' };
  const ROLES = { product_facts: '商品资料分析', session_config: '本场配置整理', audience_scenes: '顾客与场景研究', main: '主 Agent' };
  Object.assign(STATUS, { WAITING: '等待后台', NEEDS_REPLAN: '需调整计划', NEEDS_DECISION: '需要确认', OUTCOME_UNKNOWN: '结果待核实', PARTIAL: '部分完成' });
  const SECRET_KEY = /^(?:authorization|proxy-authorization|cookie|set-cookie|password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|csrf[_-]?token|client_secret|credentials?)$/i;
  const PRIVATE_KEY = /^(?:reasoning|reasoning_content|reasoning_details|chain_of_thought|private_thoughts|thinking)$/i;
  function safeText(value) { return String(value == null ? '' : value).replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+=*/gi, 'Bearer [已隐藏]').replace(/\bsk-[A-Za-z0-9_-]{12,}/g, '[已隐藏密钥]').replace(/([?&](?:token|access_token|refresh_token|api_key|signature|x-amz-signature)=)[^&#\s]+/gi, '$1[已隐藏]'); }
  function redact(value, depth = 0, seen = new WeakSet()) {
    if (depth > 16) return '[嵌套内容过深，请查看安全记录]';
    if (typeof value === 'string') return safeText(value);
    if (!value || typeof value !== 'object') return value;
    if (!Array.isArray(value) && /^(?:reasoning|thinking|redacted_thinking)$/.test(value.type || '')) return '[不展示私有推理块]';
    if (seen.has(value)) return '[循环引用]';
    seen.add(value);
    const out = Array.isArray(value) ? value.map(v => redact(v, depth + 1, seen)) : Object.fromEntries(Object.entries(value).map(([k, v]) => [k, SECRET_KEY.test(k) ? '[已隐藏]' : PRIVATE_KEY.test(k) ? '[不展示私有推理]' : redact(v, depth + 1, seen)]));
    seen.delete(value); return out;
  }
  function jsonText(value) { return JSON.stringify(redact(value), null, 2); }
  function statusName(s) { return STATUS[String(s || '').toUpperCase()] || safeText(s) || '状态未知'; }
  function normalizedRun(raw) { return { ...raw, run_id: raw.run_id || raw.id, status: String(raw.status || 'QUEUED').toUpperCase() }; }
  function uuid() { return global.crypto && global.crypto.randomUUID ? global.crypto.randomUUID() : 'ui-' + Date.now() + '-' + Math.random().toString(16).slice(2); }
  function timeLabel(value) { if (!value) return ''; const d = new Date(value); return Number.isNaN(+d) ? safeText(value) : d.toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
  function eventKind(event) {
    const t = String(event.type || event.event_type || '');
    if (/workflow\.step/.test(t)) return '工具流步骤';
    if (/workflow/.test(t)) return '可选工具流';
    if (/tool|command|query/.test(t)) return '工具调用';
    if (/middleware|guard|fence|budget|policy/.test(t)) return '运行检查';
    if (/branch|analysis_batch|subagent/.test(t)) return '子任务';
    if (/decision|approval|review/.test(t)) return '人工确认';
    if (/recover|retry|reconcil|finaliz/.test(t)) return '恢复与收尾';
    if (/intent|assessment/.test(t)) return '任务识别';
    if (/plan|dispatch|goal/.test(t)) return '任务安排';
    if (/model/.test(t)) return '模型调用';
    return '执行记录';
  }
  function eventTitle(event) { const d = event.data || {}; return safeText(d.title || (d.tool_name ? '调用 ' + d.tool_name : d.middleware ? d.middleware + ' · ' + (d.hook || '检查') : eventKind(event))); }
  function readStore(storage, key, fallback) { try { const value = storage && storage.getItem(key); return value == null ? fallback : JSON.parse(value); } catch (_) { return fallback; } }
  function writeStore(storage, key, value) { try { if (storage) storage.setItem(key, JSON.stringify(value)); } catch (_) {} }
  function defaultStorage() { try { return global.localStorage; } catch (_) { return null; } }
  function selectionItems(value) { return value ? Array.isArray(value.items) ? value.items : [value] : []; }
  function selectionKey(item) { const t=item?.target||{},r=t.object_ref||{};return JSON.stringify([r.kind,r.id,t.field_path||'',t.module_key||'',t.sentence_ids||[]]); }
  function packSelection(items) { return !items.length ? null : items.length === 1 ? structuredClone(items[0]) : {label:'已选 '+items.length+' 项',items:structuredClone(items)}; }

  function mount(options) {
    const container = options.container;
    if (!container || !container.ownerDocument) throw new Error('需要提供助手容器');
    const doc = container.ownerDocument;
    const apiBase = (options.apiBase || '/api/assistant/v2').replace(/\/$/, '');
    const requestFetch = options.fetch || global.fetch.bind(global);
    const storage = options.storage === undefined ? defaultStorage() : options.storage;
    const prefix = options.storageKey || 'shengbo-assistant-v2';
    const state = { destroyed: false, conversation: null, conversations: [], messages: [], runs: new Map(), events: new Map(), cursors: new Map(), hydrated: new Set(), materials: [], showProcess: readStore(storage, prefix + ':process', false) === true, sending: false, uploading: false, preparingTask: false, pollBusy: false, pendingDecisions: new Set(), openDetails: new Set(), questionDrafts: new Map(), payloads: new Map(), visibleEventCounts: new Map(), generation: 0, selected: null, context: {}, error: '', failures: 0, controllers: new Set(), csrf: '', pendingSubmission: null, relatedRunOverride: null };
    let sessionPromise = null;
    let preflighting = false;
    const node = (tag, cls, text) => { const n = doc.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = safeText(text); return n; };
    const button = (text, fn, cls = '') => { const b = node('button', cls, text); b.type = 'button'; b.addEventListener('click', fn); return b; };
    const root = node('section', 'sb-assistant'); root.setAttribute('aria-label', '播前 Agent 对话'); container.append(root);
    const head = node('div', 'sb-a-header');
    const identity = node('div', 'sb-a-identity'); identity.append(node('span', 'sb-a-mark', '声'), node('div', '', ''));
    identity.lastChild.append(node('strong', '', options.title || '播前助手'), node('small', '', options.subtitle || '资料、场景、本场配置与话术'));
    const newTask = button('新对话', () => createConversation(), 'sb-a-new'); head.append(identity, newTask);
    const tools = node('div', 'sb-a-toolbar');
    const picker = node('select', 'sb-a-picker'); picker.setAttribute('aria-label', '选择已有对话'); picker.addEventListener('change', () => loadConversation(picker.value));
    const toggleLabel = node('label', 'sb-a-switch'); const toggle = node('input'); toggle.type = 'checkbox'; toggle.checked = state.showProcess; toggle.setAttribute('role', 'switch'); toggle.setAttribute('aria-label', '显示执行过程'); const track = node('span', 'sb-a-switch-track'); track.setAttribute('aria-hidden', 'true'); toggleLabel.append(toggle, track, node('span', '', '执行过程')); toggle.addEventListener('change', () => { state.showProcess = toggle.checked; writeStore(storage, prefix + ':process', toggle.checked); renderConversation(); }); tools.append(picker, toggleLabel);
    const context = node('div', 'sb-a-context');
    const errorBox = node('div', 'sb-a-error'); errorBox.hidden = true; errorBox.setAttribute('role', 'status');
    const transcript = node('div', 'sb-a-transcript'); transcript.setAttribute('role', 'region'); transcript.setAttribute('aria-label', '对话及任务过程'); transcript.tabIndex = 0;
    const liveNotice = node('div', 'sb-a-sr'); liveNotice.setAttribute('aria-live', 'polite'); liveNotice.setAttribute('role', 'status');
    const form = node('form', 'sb-a-composer'); const attachments = node('div', 'sb-a-attachments');
    const target = node('div', 'sb-a-selection'); target.hidden = true;
    const input = node('textarea', 'sb-a-input'); input.rows = 3; input.maxLength = 16000; input.placeholder = '告诉我这次需要准备什么…'; input.setAttribute('aria-label', '发送给播前助手的要求');
    const bottom = node('div', 'sb-a-compose-bottom'); const upload = node('input', 'sb-a-file'); upload.type = 'file'; upload.multiple = true; upload.accept = '.txt,.md,.doc,.docx,.rtf,.xlsx,.pdf,.json,.csv,.png,.jpg,.jpeg,.webp'; upload.hidden = true;
    const attach = button('＋ 添加资料', () => upload.click(), 'sb-a-attach');
    const mode = node('select', 'sb-a-mode'); mode.setAttribute('aria-label', '任务提交方式'); [['new_task', '新任务'], ['follow_up', '接着当前任务'], ['replace', '停止原任务并重做']].forEach(([v, t]) => { const o = node('option', '', t); o.value = v; mode.append(o); });
    const send = node('button', 'sb-a-send', '发送'); send.type = 'submit'; bottom.append(attach, mode, send);
    const hint = node('p', 'sb-a-hint', 'Enter 发送 · Shift + Enter 换行。执行过程关闭时，后台仍保存安全记录。');
    form.append(target, attachments, input, bottom, upload, hint); root.append(head, tools, context, errorBox, transcript, liveNotice, form);
    function setError(text) { state.error = safeText(text); errorBox.replaceChildren(); errorBox.hidden = !text; if (text) errorBox.append(node('span', '', state.error), button('重试连接', () => refresh(), 'sb-a-link')); }
    function viewContext() { const raw = options.getViewContext ? options.getViewContext() || {} : {}; return { ...raw, ...state.context }; }
    function scopeNow() { const c = viewContext(); return c.preparation_id ? { kind: 'preparation', preparation_id: c.preparation_id, account_id: 'local' } : { kind: 'catalog', account_id: 'local' }; }
    function renderContext() { const c = state.conversation; context.replaceChildren(node('span', '', c && c.preparation_id ? '当前对话 · 本场 ' + c.preparation_id : c && c.room_id ? '当前对话 · 直播间 ' + c.room_id : '当前对话 · 商品库与全局资料')); const v = viewContext(); if (c && (c.room_id !== (v.room_id || null) || c.preparation_id !== (v.preparation_id || null))) { context.append(node('small', '', '左侧已切换，当前对话仍保留原任务。'), button('为当前直播间新建对话', () => createConversation(), 'sb-a-link')); } }
    function renderPicker() { picker.replaceChildren(); if (!state.conversations.length) { const o = node('option', '', '尚无对话'); o.value = ''; picker.append(o); } state.conversations.forEach(c => { const o = node('option', '', c.title || '播前准备对话'); o.value = c.conversation_id; picker.append(o); }); if (state.conversation) picker.value = state.conversation.conversation_id; }
    function setBusy() { send.disabled = state.sending || state.uploading || state.preparingTask; send.textContent = state.sending ? '正在提交…' : state.preparingTask ? '正在绑定本场…' : '发送'; attach.disabled = state.uploading || state.preparingTask; newTask.disabled = state.sending || state.uploading || state.preparingTask; }
    function csrfToken() { const el = doc.querySelector('meta[name="csrf-token"]'); return state.csrf || (el ? el.content : ''); }
    async function ensureSession() { if (state.csrf) return; if (!sessionPromise) sessionPromise = api('/session').then(value => { if (!value.csrf_token) throw Error('本机会话未返回请求校验信息'); state.csrf = value.csrf_token; }).finally(() => { sessionPromise = null; }); await sessionPromise; }
    async function api(path, { method = 'GET', body, multipart = false } = {}) {
      if (method !== 'GET') await ensureSession();
      const controller = new AbortController(); state.controllers.add(controller); const timeout = setTimeout(() => controller.abort(), 25000);
      try { const headers = {}; if (body && !multipart) headers['Content-Type'] = 'application/json'; const csrf = csrfToken(); if (csrf && method !== 'GET') headers['X-CSRF-Token'] = csrf;
        const r = await requestFetch(path.startsWith('/api/') ? path : apiBase + path, { method, headers, credentials: 'same-origin', body: body ? multipart ? body : JSON.stringify(body) : undefined, signal: controller.signal });
        let data; try { data = await r.json(); } catch (_) { throw new Error('服务响应格式异常，请重试连接'); }
        if (!r.ok) { const detail = data.detail || data.error; throw new Error(typeof detail === 'string' ? safeText(detail) : detail && (detail.message || detail.code) || '请求失败（' + r.status + '）'); } return data;
      } finally { clearTimeout(timeout); state.controllers.delete(controller); }
    }
    function addRun(raw) { if (!raw || !(raw.run_id || raw.id)) return; const r = normalizedRun(raw); state.runs.set(r.run_id, { ...(state.runs.get(r.run_id) || {}), ...r }); }
    function absorbRun(raw) { const before = state.runs.get(raw.run_id || raw.id); addRun(raw); for (const message of raw.messages || []) { const id = message.message_id || message.id; if (id && !state.messages.some(item => (item.message_id || item.id) === id)) state.messages.push(message); } const r = state.runs.get(raw.run_id || raw.id); if (r && (!before || before.status !== r.status)) liveNotice.textContent = statusName(r.status); }
    function publicState() { return { conversation: state.conversation ? structuredClone(state.conversation) : null, runs: Array.from(state.runs.values()).map(r => structuredClone(r)), selected: state.selected ? structuredClone(state.selected) : null, pendingRequest: state.pendingSubmission ? structuredClone(state.pendingSubmission.request) : null, inputText: input.value, materials: structuredClone(state.materials), hasPendingInput: !!input.value.trim() || state.materials.length > 0, showProcess: state.showProcess, sending: state.sending }; }
    function announceState() { if (options.onState) options.onState(publicState()); }
    function currentRun() { return Array.from(state.runs.values()).at(-1); }
    async function initialize() { try { await ensureSession(); const list = await api('/conversations'); if (state.destroyed) return; state.conversations = list.items || list.conversations || []; renderPicker(); const saved = readStore(storage, prefix + ':conversation', null); const first = state.conversations.find(x => x.conversation_id === saved) || state.conversations[0]; if (first) await loadConversation(first.conversation_id); else renderConversation(); } catch (e) { if (!state.destroyed) setError('助手暂未连接：' + e.message); } }
    async function createConversation(contextOverride) { if (state.sending || state.uploading) return; try { const v = contextOverride && !contextOverride.type ? { ...viewContext(), ...contextOverride } : viewContext(); const c = await api('/conversations', { method: 'POST', body: { client_request_id: uuid(), scope: v.preparation_id ? { kind: 'preparation', preparation_id: v.preparation_id, account_id: 'local' } : { kind: 'catalog', account_id: 'local' }, room_id: v.room_id || null, preparation_id: v.preparation_id || null } }); const conv = c.conversation || c; state.conversations.unshift(conv); mode.value = 'new_task'; await loadConversation(conv.conversation_id, conv); announceState(); return conv; } catch (e) { setError(e.message); return null; } }
    async function loadConversation(id, supplied) { if (!id) return; const generation = ++state.generation; try { const c = supplied || await api('/conversations/' + encodeURIComponent(id)); if (generation !== state.generation || state.destroyed) return; state.conversation = c.conversation || c; state.messages = c.messages || state.conversation.messages || []; state.runs.clear(); state.events.clear(); state.cursors.clear(); state.hydrated.clear(); state.questionDrafts.clear(); state.materials = []; state.selected = null; state.pendingSubmission = null; state.relatedRunOverride = null; renderSelection(); for (const r of c.runs || []) addRun(r); writeStore(storage, prefix + ':conversation', id); renderPicker(); renderContext(); renderAttachments(); setError(''); renderConversation(); announceState(); await pollRuns(generation, true); } catch (e) { if (generation === state.generation) setError(e.message); } }
    function ingestEvents(runId, events, nextCursor) { const map = state.events.get(runId) || new Map(); let highest = state.cursors.get(runId) || 0; for (const e of events || []) { if (!e || e.schema_version !== VERSION || typeof e.event_id !== 'string' || typeof e.type !== 'string' || e.run_id !== runId || !e.data || typeof e.data !== 'object' || Array.isArray(e.data)) throw Error('执行事件格式与 ' + VERSION + ' 不一致，未展示该记录'); const seq = Number(e.sequence); if (!Number.isSafeInteger(seq) || seq < 1) throw Error('执行事件缺少有效的持久顺序编号'); if (!map.has(e.event_id)) map.set(e.event_id, { ...e, sequence: seq }); highest = Math.max(highest, seq); } if (Number.isFinite(Number(nextCursor))) highest = Math.max(highest, Number(nextCursor)); state.events.set(runId, map); state.cursors.set(runId, highest); writeStore(storage, prefix + ':cursor:' + runId, highest); }
    async function pollRuns(generation = state.generation, force = false) { if (state.pollBusy || state.destroyed) return; state.pollBusy = true; try { const runs = Array.from(state.runs.values()); for (const original of runs) { if (generation !== state.generation || state.destroyed) return; const id = original.run_id; if (!force && TERMINAL.has(original.status) && state.hydrated.has(id)) continue; let [r, page] = await Promise.all([api('/runs/' + encodeURIComponent(id)), api('/runs/' + encodeURIComponent(id) + '/events?after=' + (state.cursors.get(id) || 0))]); if (generation !== state.generation || state.destroyed) return; absorbRun(r.run || r); let pages = 0; do { const before = state.cursors.get(id) || 0; ingestEvents(id, page.events || page.items || [], page.next_cursor); if (!page.has_more) break; if ((state.cursors.get(id) || 0) <= before) throw Error('执行记录分页游标未前进，请重试连接'); page = await api('/runs/' + encodeURIComponent(id) + '/events?after=' + state.cursors.get(id)); if (generation !== state.generation || state.destroyed) return; } while (++pages < 20); if (!page.has_more) state.hydrated.add(id); } state.failures = 0; if (state.error) setError(''); renderConversation(); announceState(); } catch (e) { if (!state.destroyed && generation === state.generation) { state.failures += 1; setError('进度连接中断，任务可能仍在后台运行：' + e.message); } } finally { state.pollBusy = false; } }
    function safeAppendText(parent, text, cls) { const p = node('p', cls, typeof text === 'string' ? text : jsonText(text)); parent.append(p); return p; }
    function details(label, value, id) { const box = node('details', 'sb-a-details'); box.open = state.openDetails.has(id); box.addEventListener('toggle', () => box.open ? state.openDetails.add(id) : state.openDetails.delete(id)); box.append(node('summary', '', label)); const body = node('pre', 'sb-a-json', typeof value === 'string' ? safeText(value) : jsonText(value)); box.append(body, button('复制', async () => { try { await global.navigator.clipboard.writeText(body.textContent); liveNotice.textContent = '已复制安全记录'; } catch (_) { liveNotice.textContent = '无法自动复制，可选中文本手动复制'; } }, 'sb-a-copy')); return box; }
    function eventCard(e, runId) { const d = e.data || {}; const card = node('article', 'sb-a-event'); card.dataset.eventId = e.event_id; const h = node('div', 'sb-a-event-heading'); h.append(node('span', 'sb-a-event-kind', eventKind(e)), node('strong', '', eventTitle(e)), node('small', '', '#' + e.sequence + ' · ' + timeLabel(e.timestamp || e.occurred_at))); card.append(h); if (d.status || e.status) card.append(node('span', 'sb-a-badge', statusName(d.status || e.status))); if (d.summary) safeAppendText(card, d.summary, 'sb-a-event-summary');
      // 只展示模型明确公开的决策摘要；不展示私有推理字段。
      if (d.decision_summary || d.reason_summary) safeAppendText(card, d.decision_summary || d.reason_summary, 'sb-a-reason');
      if (d.progress && Number.isFinite(d.progress.completed) && Number.isFinite(d.progress.total) && d.progress.total > 0) card.append(node('small', 'sb-a-progress', '进度 ' + d.progress.completed + ' / ' + d.progress.total));
      if (d.arguments !== undefined) card.append(details('发送的参数', d.arguments, e.event_id + ':args'));
      else if (d.parameters !== undefined) card.append(details('步骤参数', d.parameters, e.event_id + ':args'));
      if (Number.isFinite(d.duration_ms)) card.append(node('small', 'sb-a-progress', '耗时 ' + (d.duration_ms / 1000).toFixed(2) + ' 秒'));
      if (d.result_preview !== undefined) card.append(details('返回数据摘要', d.result_preview, e.event_id + ':result'));
      if (d.error) safeAppendText(card, typeof d.error === 'string' ? d.error : jsonText(d.error), 'sb-a-event-error');
      card.append(details('事件结构与关联', { schema_version: e.schema_version, event_id: e.event_id, sequence: e.sequence, run_id: e.run_id, invocation_id: e.invocation_id || null, branch_task_id: e.branch_task_id || null, parent_event_id: e.parent_event_id || null, type: e.type, timestamp: e.timestamp, data: d }, e.event_id + ':metadata'));
      const payloadRef = d.payload_ref || (e.payload_refs && e.payload_refs[0] && (e.payload_refs[0].sha256 || e.payload_refs[0].id)); if (payloadRef) { const ref = typeof payloadRef === 'string' ? payloadRef : payloadRef.sha256 || payloadRef.id; const cacheKey = runId + ':' + ref; if (state.payloads.has(cacheKey)) card.append(details('完整安全记录', state.payloads.get(cacheKey), e.event_id + ':full')); else { const b = button('展开完整安全记录', async () => { b.disabled = true; try { const p = await api('/runs/' + encodeURIComponent(runId) + '/payloads/' + encodeURIComponent(ref)); const safe = redact(p.payload !== undefined ? p.payload : p); state.payloads.set(cacheKey, safe); card.append(details('完整安全记录', safe, e.event_id + ':full')); b.remove(); } catch (err) { b.disabled = false; liveNotice.textContent = err.message; } }, 'sb-a-link'); card.append(b); } }
      return card;
    }
    function processPanel(run) { const wrap = node('section', 'sb-a-process'); wrap.setAttribute('aria-label', '任务执行过程'); wrap.append(node('p', 'sb-a-process-note', '这里展示实际调用与公开依据。并行任务分组展示，编号为记录顺序，不代表串行执行。')); const complete = Array.from((state.events.get(run.run_id) || new Map()).values()).sort((a, b) => a.sequence - b.sequence); const visibleCount = state.visibleEventCounts.get(run.run_id) || 120; if (complete.length > visibleCount) wrap.append(button('展开更早的 ' + Math.min(120, complete.length - visibleCount) + ' 条记录（共 ' + complete.length + ' 条）', () => { state.visibleEventCounts.set(run.run_id, visibleCount + 120); renderConversation(); }, 'sb-a-link')); const all = complete.slice(-visibleCount); const main = node('div', 'sb-a-main-events'); const branches = new Map(); for (const e of all) { if (e.branch_task_id || e.data && e.data.branch_task_id) { const id = e.branch_task_id || e.data.branch_task_id; if (!branches.has(id)) branches.set(id, []); branches.get(id).push(e); } else main.append(eventCard(e, run.run_id)); }
      if (main.childElementCount) { const title = node('h4', '', '主 Agent 与服务检查'); wrap.append(title, main); }
      for (const branch of run.branches || run.branch_tasks || []) if (!branches.has(branch.branch_task_id || branch.id)) branches.set(branch.branch_task_id || branch.id, []);
      if (branches.size) { const group = node('section', 'sb-a-branches'); group.append(node('h4', '', '子任务 · 可并行执行')); for (const [id, events] of branches) { const branch = (run.branches || run.branch_tasks || []).find(x => (x.branch_task_id || x.id) === id) || {}; const first = events[0] || {}; const last = events.at(-1) || {}; const role = branch.role || branch.branch_role || first.agent_role || first.data && first.data.role; const box = node('details', 'sb-a-branch'); box.open = state.openDetails.has(run.run_id + ':branch:' + id); box.addEventListener('toggle', () => box.open ? state.openDetails.add(run.run_id + ':branch:' + id) : state.openDetails.delete(run.run_id + ':branch:' + id)); box.append(node('summary', '', (ROLES[role] || branch.title || '分析子任务') + ' · ' + statusName(branch.status || last.data && last.data.status || last.status || 'RUNNING'))); box.append(node('small', 'sb-a-branch-id', id)); if (branch.summary) safeAppendText(box, branch.summary, 'sb-a-event-summary'); events.forEach(e => box.append(eventCard(e, run.run_id))); group.append(box); } wrap.append(group); }
      if (!all.length && !branches.size) wrap.append(node('p', 'sb-a-empty-process', '尚未收到执行事件；不会据此判断任务已开始或已完成。')); return wrap;
    }
    function artifactCard(a) { const b = button(a.label || a.title || '查看成果', () => { const destination = a.destination || a; if (options.onNavigate) options.onNavigate(destination); else root.dispatchEvent(new CustomEvent('assistant:navigate', { bubbles: true, detail: destination })); }, 'sb-a-artifact'); return b; }
    function sellingPointsPreview(q) {
      const preview = q.preview || {}; const wrap = node('section', 'sb-a-benefits');
      wrap.setAttribute('aria-label', '待确认的核心卖点');
      if (preview.product_name) wrap.append(node('p', 'sb-a-benefit-product', preview.product_name));
      const items = Array.isArray(preview.items) ? preview.items : [];
      items.forEach((item, index) => {
        const card = node('article', 'sb-a-benefit');
        card.append(node('span', 'sb-a-benefit-number', String(index + 1).padStart(2, '0')), node('p', 'sb-a-benefit-copy', item.selling_point || '卖点内容待补充'));
        const key = q.decision_id + ':benefit:' + index; const basis = node('details', 'sb-a-benefit-basis');
        basis.open = state.openDetails.has(key);
        basis.addEventListener('toggle', () => basis.open ? state.openDetails.add(key) : state.openDetails.delete(key));
        basis.append(node('summary', '', '顾客场景与商品依据'));
        if (item.audience_need) safeAppendText(basis, '顾客需求：' + item.audience_need);
        if (item.use_scene) safeAppendText(basis, '使用场景：' + item.use_scene);
        const evidence = Array.isArray(item.evidence) ? item.evidence : [];
        if (evidence.length) {
          const list = node('ul', 'sb-a-benefit-evidence');
          evidence.forEach(value => {
            const entry = node('li', '', typeof value === 'string' ? value : value.fact || '商品依据');
            const ref = value && value.source_ref;
            const label = typeof ref === 'string' ? ref : ref && (ref.label || ref.title || ref.id);
            if (label) entry.append(node('small', '', '来源：' + label));
            list.append(entry);
          });
          basis.append(list);
        }
        if (item.limitations && (!Array.isArray(item.limitations) || item.limitations.length)) safeAppendText(basis, '适用边界：' + (Array.isArray(item.limitations) ? item.limitations.join('；') : item.limitations));
        card.append(basis); wrap.append(card);
      });
      return wrap;
    }
    function renderQuestion(q, run) {
      const generation = q.kind === 'selling_points_generation'; const confirmation = q.kind === 'selling_points_confirmation';
      const card = node('article', 'sb-a-question'); card.dataset.decisionId = q.decision_id;
      if (generation || confirmation) card.classList.add('sb-a-question-benefits');
      card.append(node('strong', '', q.title || '需要你确认'), node('p', '', q.prompt || q.question || '请检查这项决定'));
      if (q.summary) safeAppendText(card, q.summary);
      if (confirmation && q.preview) card.append(sellingPointsPreview(q));
      else if (generation && q.preview && q.preview.product_name) card.append(node('p', 'sb-a-benefit-product', '商品：' + q.preview.product_name));
      else if (q.preview) card.append(details('查看内容', q.preview, q.decision_id + ':preview'));
      const choices = q.options || q.choices || []; const row = node('div', 'sb-a-question-actions'); let approveButton = null; let approveUnavailable = false; let answerInput = null;
      const draft = () => (state.questionDrafts.get(q.decision_id) || '').trim();
      for (const item of choices) {
        const val = item.value ?? item.choice_id ?? item.id;
        // Selling-point approval is bound server-side to its proposal, not an audio/business approval command.
        const approval = item.requires_human_action || !confirmation && /approve|audio_approval/.test(q.kind || '') && /approve|confirm/.test(String(val));
        const b = button(item.label || item.title || String(val), () => {
          if (confirmation && val === 'approve' && draft()) { setError('你填写了修改意见，请先点击“按意见修改”；修改后的卖点会再次请你确认。'); return; }
          if (confirmation && val === 'revise' && !draft()) { setError('请先填写希望怎样修改卖点。'); if (answerInput) answerInput.focus(); return; }
          const answer = generation && val === 'generate' ? { choice: 'generate', text: draft() } : confirmation && val === 'revise' ? { choice: 'revise', text: draft() } : val;
          answerQuestion(q, run, answer, item.human_action || q.human_action);
        }, approval || (confirmation && val === 'approve') || (generation && val === 'generate') ? 'sb-a-confirm' : '');
        b.disabled = state.pendingDecisions.has(q.decision_id) || approval && !(item.human_action || q.human_action);
        if (approval && b.disabled && !state.pendingDecisions.has(q.decision_id)) b.title = '尚未收到可校验的人工审批操作，请刷新后重试';
        if (confirmation && val === 'approve') { approveButton = b; approveUnavailable = !!(approval && !(item.human_action || q.human_action)); if (draft()) { b.disabled = true; b.title = '已填写修改意见，请先按意见修改，再确认新内容'; } }
        row.append(b);
      }
      const free = q.allow_text !== false && (!choices.length || q.allow_text === true || q.kind === 'edit_text');
      if (free) {
        const answer = node('textarea', 'sb-a-answer'); answerInput = answer; answer.rows = 2;
        answer.placeholder = q.input_placeholder || (confirmation ? '可选：告诉我希望怎样修改，再点击“按意见修改”' : '补充说明或修改要求');
        answer.setAttribute('aria-label', generation ? '卖点生成方向（可选）' : confirmation ? '卖点修改意见（可选）' : '确认项补充说明');
        answer.dataset.questionDraft = q.decision_id; answer.value = state.questionDrafts.get(q.decision_id) || '';
        answer.disabled = state.pendingDecisions.has(q.decision_id);
        answer.addEventListener('input', () => { state.questionDrafts.set(q.decision_id, answer.value); if (approveButton) { approveButton.disabled = state.pendingDecisions.has(q.decision_id) || approveUnavailable || !!answer.value.trim(); approveButton.title = answer.value.trim() ? '已填写修改意见，请先按意见修改，再确认新内容' : approveUnavailable ? '尚未收到可校验的人工审批操作，请刷新后重试' : ''; } });
        const b = button('提交答复', () => { if (answer.value.trim()) answerQuestion(q, run, { text: answer.value.trim() }); }); b.disabled = state.pendingDecisions.has(q.decision_id);
        card.append(answer); row.append(b);
        if (generation) card.append(node('p', 'sb-a-question-hint', '可补充目标顾客、使用场景或表达方向；商品事实不足时会继续向你询问。'));
        if (confirmation) card.append(node('p', 'sb-a-question-hint', '确认后才写入商品库；若填写修改意见，请先按意见修改。'));
      }
      card.append(row); return card;
    }
    function runCard(run) { const card = node('article', 'sb-a-run'); card.dataset.runId = run.run_id; const h = node('div', 'sb-a-run-heading'); const badge = node('span', 'sb-a-run-status', statusName(run.status)); badge.dataset.status = run.status; h.append(badge, node('small', '', run.run_id)); if (!TERMINAL.has(run.status) && run.status !== 'FINALIZING' && run.status !== 'CANCELLING') h.append(button('停止任务', () => cancelRun(run), 'sb-a-stop')); card.append(h); if (run.summary) safeAppendText(card, run.summary, 'sb-a-run-summary');
      const requirements = run.acceptance && run.acceptance.requirements || []; if (requirements.length) { const list = node('ul', 'sb-a-acceptance'); for (const r of requirements) list.append(node('li', '', (r.satisfied ? '✓ ' : '○ ') + (r.label || r.requirement_id))); card.append(list); }
      const questions = (run.questions || run.decisions || []).filter(q => !q.status || ['OPEN', 'PENDING', 'WAITING'].includes(String(q.status).toUpperCase())); questions.forEach(q => card.append(renderQuestion(q, run))); const artifacts = run.artifacts || run.outputs || []; if (artifacts.length) { const nav = node('div', 'sb-a-artifacts'); artifacts.forEach(a => nav.append(artifactCard(a))); card.append(nav); }
      for (const option of run.continuation_options || []) { const box = node('div', 'sb-a-continuation'); box.append(node('strong', '', option.label || '下一步'), node('p', '', option.summary || (option.unmet_requirement_ids || []).join('、') || option.mode)); if (option.mode === 'resume_remaining') box.append(button('继续未完成部分', () => { state.relatedRunOverride = run.run_id; mode.value = 'follow_up'; input.value = '继续完成任务 ' + run.run_id + ' 的未完成部分'; input.focus(); }, 'sb-a-link')); card.append(box); }
      if (state.showProcess) card.append(processPanel(run)); return card;
    }
    function renderConversation() { if (state.destroyed) return; const active = doc.activeElement; const editingQuestion = active && active.dataset.questionDraft; const selection = editingQuestion ? [active.selectionStart, active.selectionEnd] : null; const nearEnd = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 90; const oldScroll = transcript.scrollTop; transcript.replaceChildren(); if (!state.messages.length && !state.runs.size) { const empty = node('div', 'sb-a-welcome'); empty.append(node('span', 'sb-a-welcome-icon', '声'), node('h3', '', '从商品资料开始准备'), node('p', '', '上传商品信息、指定本场活动，或让我先分析顾客与使用场景。')); const suggestions = node('div', 'sb-a-suggestions'); ['整理上传的商品资料，先列缺项与冲突', '分析这个商品的顾客与使用场景', '检查本场配置和已有话术的差异'].forEach(t => suggestions.append(button(t, () => { input.value = t; input.focus(); }))); empty.append(suggestions); transcript.append(empty); }
      const renderedRuns = new Set(); for (const m of state.messages) { const role = m.role === 'user' ? 'user' : 'assistant'; const el = node('article', 'sb-a-message sb-a-message-' + role); el.append(node('small', '', role === 'user' ? '你' : '播前助手')); const content = m.text ?? m.content ?? ''; safeAppendText(el, typeof content === 'string' ? content : jsonText(content), 'sb-a-message-text'); transcript.append(el); if (m.run_id && state.runs.has(m.run_id) && !renderedRuns.has(m.run_id)) { transcript.append(runCard(state.runs.get(m.run_id))); renderedRuns.add(m.run_id); } }
      for (const run of state.runs.values()) if (!renderedRuns.has(run.run_id)) transcript.append(runCard(run)); if (editingQuestion) { const restored = Array.from(transcript.querySelectorAll('[data-question-draft]')).find(el => el.dataset.questionDraft === editingQuestion); if (restored) { restored.focus({ preventScroll: true }); restored.setSelectionRange(...selection); } } transcript.scrollTop = !state.messages.length && !state.runs.size ? 0 : nearEnd && !editingQuestion ? transcript.scrollHeight : oldScroll; }
    function renderAttachments() { attachments.replaceChildren(); for (const m of state.materials) { const chip = node('div', 'sb-a-attachment'); chip.append(node('span', '', m.name || '资料'), button('×', () => { state.materials = state.materials.filter(x => x !== m); renderAttachments(); }, 'sb-a-remove')); attachments.append(chip); } }
    function renderSelection() {
      target.replaceChildren();const items=selectionItems(state.selected);if(items.length<20&&state.error==='一次最多选择20项，请先移除不需要的项。')setError('');target.hidden=!items.length;if(!items.length)return;
      const heading=node('div','sb-a-selection-heading');heading.append(node('strong','','已选 '+items.length+' 项'),button(items.length===1?'取消指定':'清空选择',()=>{state.selected=null;renderSelection();announceState();options.onSelectionCleared?.();},'sb-a-link'));target.append(heading);
      const tags=node('div','sb-a-selection-tags');items.forEach((item,index)=>{const chip=node('div','sb-a-selection-chip');const label=item.label||item.target?.field_path||item.target?.module_key||'已指定对象';const name=button(label,()=>{if(item.destination&&options.onNavigate)options.onNavigate(item.destination);},'sb-a-selection-name');name.title='定位到 '+label;const remove=button('×',()=>{const next=selectionItems(state.selected).filter((_,i)=>i!==index);state.selected=packSelection(next);renderSelection();announceState();},'sb-a-selection-remove');remove.setAttribute('aria-label','移除 '+label);chip.append(name,remove);tags.append(chip);});target.append(tags,node('small','','点击左侧字段名或模块标题可继续选择；发送后才会开始处理。'));
    }
    function toggleSelection(value,{focus=false}={}) { if(!value?.target?.object_ref)return;const items=selectionItems(state.selected).map(v=>structuredClone(v));const key=selectionKey(value),index=items.findIndex(v=>selectionKey(v)===key);if(index>=0){if(JSON.stringify(items[index].target.object_ref)===JSON.stringify(value.target.object_ref))items.splice(index,1);else items[index]=structuredClone(value);}else{if(items.length>=20){setError('一次最多选择20项，请先移除不需要的项。');return;}items.push(structuredClone(value));}state.selected=packSelection(items);renderSelection();announceState();if(focus)input.focus(); }
    async function sendMessage(e) { if (e) e.preventDefault(); if (preflighting) return; preflighting = true; try { return await performSendMessage(); } finally { preflighting = false; setBusy(); } }
    async function performSendMessage(e) { if (e) e.preventDefault(); const text = input.value.trim(); if ((!text && !state.materials.length) || state.sending || state.uploading || state.preparingTask) return; if (options.beforeSend) { send.disabled = true; try { const checked = await options.beforeSend({ text, selection: state.selected ? structuredClone(state.selected) : null, conversation: state.conversation ? structuredClone(state.conversation) : null }); if (checked === false) return; if (checked && Object.prototype.hasOwnProperty.call(checked, 'selection')) { state.selected = checked.selection ? structuredClone(checked.selection) : null; renderSelection(); } } catch(err) { setError(err.message); return; } } if (!state.conversation) { const preservedSelection = state.selected; await createConversation(); state.selected = preservedSelection; renderSelection(); } if (!state.conversation) return; const active = currentRun(); if (mode.value !== 'new_task' && !active) { setError('当前没有可接续的任务，请选择“新任务”'); return; } state.sending = true; setBusy(); setError('');
      let request = { scope: state.conversation.scope || { kind: state.conversation.preparation_id ? 'preparation' : 'catalog', account_id: 'local', ...(state.conversation.preparation_id ? { preparation_id: state.conversation.preparation_id } : {}) }, text, material_refs: state.materials.map(x => x.material_ref || x.ref || x), selection: state.selected, view_context: viewContext(), intent_mode: mode.value, related_run_id: mode.value === 'new_task' ? null : state.relatedRunOverride || active.run_id, observed_versions: options.getObservedVersions ? structuredClone(options.getObservedVersions(state.selected) || []) : [] };
      // Navigating the workspace does not turn an uncertain transport retry into
      // a new authorized task. Reuse the entire frozen request, not only its ID.
      const { view_context: ignoredViewContext, ...intentFields } = request;
      const signature = JSON.stringify(intentFields); const repeat = state.pendingSubmission && state.pendingSubmission.signature === signature;
      const requestId = repeat ? state.pendingSubmission.requestId : uuid(); request = repeat ? structuredClone(state.pendingSubmission.request) : { ...request, client_request_id: requestId }; state.pendingSubmission = { signature, requestId, request: structuredClone(request) }; announceState();
      try { const receipt = await api('/conversations/' + encodeURIComponent(state.conversation.conversation_id) + '/messages', { method: 'POST', body: request }); const message = receipt.message || { message_id: requestId, role: 'user', text, run_id: receipt.run_id || receipt.run && receipt.run.run_id }; if (!state.messages.some(x => x.message_id === message.message_id)) state.messages.push(message); if (receipt.run) addRun(receipt.run); else if (receipt.run_id) addRun(receipt); state.pendingSubmission = null; state.relatedRunOverride = null; input.value = ''; state.materials = []; renderAttachments(); renderConversation(); await pollRuns(state.generation, true); } catch (err) { setError('尚未确认任务已受理：' + err.message + '。输入已保留，再次发送同一要求会复用请求编号。'); } finally { state.sending = false; setBusy(); announceState(); } }
    async function answerQuestion(q, run, answer, humanAction) { if (state.pendingDecisions.has(q.decision_id)) return; state.pendingDecisions.add(q.decision_id); renderConversation(); try { let humanReceipt = null; if (humanAction) { if (humanAction.kind === 'product_identity') { const body = humanAction.body; if (!body || typeof body.target_name !== 'string' || !Array.isArray(body.candidate_refs)) throw Error('商品身份确认格式不完整'); humanReceipt = await api('/api/prelive/v2/human/product-identity', { method: 'POST', body: { target_name: body.target_name, candidate_refs: body.candidate_refs } }); } else { if (humanAction.kind && humanAction.kind !== 'business_command') throw Error('不支持该人工操作类型'); if (!humanAction.command || typeof humanAction.command !== 'object') throw Error('人工审批命令格式不完整，未提交批准'); humanReceipt = await api('/api/prelive/v2/commands', { method: 'POST', body: humanAction.command }); } } const receipt = await api('/decisions/' + encodeURIComponent(q.decision_id) + '/answers', { method: 'POST', body: { question_revision: q.revision ?? q.question_revision, decision_revision: q.revision ?? q.question_revision, answer, client_request_id: uuid(), operation_id: uuid(), human_receipt: humanReceipt && (humanReceipt.receipt_ref || humanReceipt) } }); if (receipt.run) absorbRun(receipt.run); await pollRuns(state.generation, true); setError(''); } catch (e) { setError('这项确认未完成：' + e.message); } finally { state.pendingDecisions.delete(q.decision_id); renderConversation(); } }
    async function cancelRun(run) { try { await api('/runs/' + encodeURIComponent(run.run_id) + '/cancel', { method: 'POST', body: { operation_id: uuid(), client_request_id: uuid(), expected_run_version: run.version ?? null } }); await pollRuns(state.generation, true); } catch (e) { setError('未能确认任务已停止：' + e.message); } }
    async function uploadFiles(providedFiles) { if(state.preparingTask)throw Error('正在为新增任务绑定本场，请稍后上传资料。');const files = Array.isArray(providedFiles) ? providedFiles : Array.from(upload.files || []); upload.value = ''; if (!files.length) return; if (!state.conversation) await createConversation(); if (!state.conversation) return; state.uploading = true; setBusy(); try { for (const file of files) { if (file.size > (/\.xlsx$/i.test(file.name) ? 128 : 10) * 1024 * 1024) throw Error(file.name + ' 超过本格式的资料大小限制（Excel 128 MiB，其余10 MiB）'); const data = new FormData(); data.append('file', file); data.append('conversation_id', state.conversation.conversation_id); data.append('scope', JSON.stringify(state.conversation.scope || scopeNow())); data.append('client_request_id', uuid()); const m = await api('/materials', { method: 'POST', body: data, multipart: true }); state.materials.push({ ...m, name: m.name || file.name }); renderAttachments(); announceState(); } setError(''); } catch (e) { setError('资料上传未全部完成：' + e.message); if (Array.isArray(providedFiles)) throw e; } finally { state.uploading = false; setBusy(); announceState(); } return structuredClone(state.materials); }
    async function refresh() { if (!state.conversation) return initialize(); return pollRuns(state.generation, true); }
    form.addEventListener('submit', sendMessage); input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(); } }); upload.addEventListener('change', uploadFiles);
    const timer = setInterval(() => { if (!state.destroyed && state.failures < 6 && !doc.hidden) pollRuns(); }, options.pollInterval || 1800);
    const visibility = () => { if (!doc.hidden) refresh(); }; doc.addEventListener('visibilitychange', visibility);
    renderPicker(); renderContext(); renderConversation(); const ready = initialize();
    input.addEventListener('input', announceState);
    return { ready, refresh, uploadFiles, newConversation: createConversation, async prepareTask(text,taskContext={}) {
      if(state.destroyed)throw Error('对话界面已关闭，请刷新后重试');
      if(state.sending||state.uploading||state.pendingSubmission||state.preparingTask)throw Error('当前请求正在提交，请稍后再设置新增要求。');
      if(input.value.trim()||state.materials.length)throw Error('右侧有尚未发送的文字或附件，请先发送或清空，再设置新增版本。');
      const target={...viewContext(),...taskContext},expected=target.preparation_id?{kind:'preparation',preparation_id:target.preparation_id}:{kind:'catalog'},matches=conversation=>{const scope=conversation?.scope;return scope?.kind===expected.kind&&(expected.kind!=='preparation'||scope.preparation_id===expected.preparation_id)&&(!target.room_id||conversation?.room_id===target.room_id);};
      const wasDisabled=input.disabled;state.preparingTask=true;input.disabled=true;setBusy();
      try {
        if(!matches(state.conversation)){const next=await createConversation(target);if(!next||!matches(next))throw Error('未能为当前本场建立匹配的对话，新增要求未提交；请重试连接。');}
        state.selected=null;renderSelection();mode.value='new_task';state.relatedRunOverride=null;input.value=String(text||'');announceState();
      } finally {state.preparingTask=false;input.disabled=wasDisabled;setBusy();}
      input.focus();
    }, getState: publicState, toggleSelection, setContext(value) { state.context = { ...value }; renderContext(); announceState(); }, setSelection(value, selectionOptions = {}) { const items=selectionItems(value);if(items.length>20)throw Error('一次最多选择20项');state.selected=packSelection(items); renderSelection(); announceState(); if (selectionOptions.focus !== false) input.focus(); }, focusInput() { input.focus(); }, setInput(value, { replace = false } = {}) { const text=String(value||'');if(replace||!input.value.trim())input.value=text;else if(!input.value.includes(text))input.value=input.value.replace(/\s+$/,'')+'\n\n'+text;announceState(); }, destroy() { state.destroyed = true; clearInterval(timer); state.controllers.forEach(c => c.abort()); doc.removeEventListener('visibilitychange', visibility); root.remove(); }, version: VERSION, _inspect() { return { conversation_id: state.conversation && state.conversation.conversation_id, runs: state.runs.size, events: Array.from(state.events.values()).reduce((n, m) => n + m.size, 0), cursors: Object.fromEntries(state.cursors), showProcess: state.showProcess }; } };
  }
  const exported = { mount, version: VERSION, _test: { redact, safeText, eventKind, eventTitle, normalizedRun, statusName } }; global.ShengboAssistant = exported; if (typeof module !== 'undefined' && module.exports) module.exports = exported;
})(typeof window !== 'undefined' ? window : globalThis);
