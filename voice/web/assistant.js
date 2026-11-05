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
  function runStatusName(run) {
    return run.status === 'COMPLETED' && run.delivery_record?.delivery_quality === 'partial'
      ? '任务部分达成' : statusName(run.status);
  }
  function isUnfinished(run) {
    return ['FAILED', 'BLOCKED'].includes(run.status);
  }
  function toolFailureCode(data) {
    if (data.reason_code || data.error?.code) return data.reason_code || data.error.code;
    let preview = data.result_preview;
    if (typeof preview === 'string') {
      try { preview = JSON.parse(preview); } catch (_) { return null; }
    }
    return preview?.reason_code || null;
  }
  function finalFailureEvents(run, events) {
    if (!isUnfinished(run) || !run.error?.code || events.length < 2) return [];
    const last = events.at(-1);
    const failure = events.at(-2);
    const tool = events.at(-3);
    if (failure.type !== 'run.error' || failure.data.error_code !== run.error.code
        || last.type !== 'run.closed' || last.data.status !== run.status
        || failure.branch_task_id || failure.data.branch_task_id
        || last.branch_task_id || last.data.branch_task_id || last.sequence !== failure.sequence + 1) return [];
    if (tool?.type === 'tool.failed' && !isGraphPause(tool)
        && !tool.branch_task_id && !tool.data.branch_task_id
        && tool.sequence + 1 === failure.sequence && toolFailureCode(tool.data) === run.error.code) return [tool, failure, last];
    return [failure, last];
  }
  const STEP_LABELS = {
    save_task_assessment: '核对本次任务要求',
    read_context: '读取话术生成依据',
    read_run_history: '查看此前的执行记录',
    read_script: '核对话术原文',
    save_script_change_plan: '保存话术方案',
    submit_script_change: '提交话术生成',
  };
  function correctionSteps(events) {
    const steps = [];
    const invocationKey = event => JSON.stringify([event.invocation_id, event.branch_task_id || event.data.branch_task_id || null]);
    for (const [index, correction] of events.entries()) {
      const data = correction.data;
      if (correction.type !== 'middleware.check' || data.hook !== 'correctable_tool_input'
          || data.status !== 'CORRECTING' || !data.reason_code || !correction.invocation_id) continue;
      const key = invocationKey(correction);
      const failure = events.slice(0, index).findLast(event => event.type === 'tool.failed' && invocationKey(event) === key);
      if (!failure?.data.tool_name || toolFailureCode(failure.data) !== data.reason_code) continue;
      const matching = events.slice(index + 1).filter(event => invocationKey(event) === key
        && event.data.tool_name === failure.data.tool_name);
      const retry = matching.find(event => event.type === 'tool.started');
      const result = retry && matching.find(event => event.parent_event_id === retry.event_id
        && ['tool.completed', 'tool.failed'].includes(event.type));
      const succeeded = result?.type === 'tool.completed' && result.data.status === 'SUCCEEDED';
      steps.push({ label: STEP_LABELS[failure.data.tool_name] || '工具步骤', succeeded, retried: !!retry,
        failedAgain: result?.type === 'tool.failed' && !isGraphPause(result) });
    }
    return steps;
  }
  function savedScriptCount(run) {
    const refs = (run.artifacts || run.outputs || []).map(item => item.destination?.object_ref || item.ref)
      .filter(ref => ref?.kind === 'script_revision' && ref.id && Number.isInteger(ref.revision) && ref.content_hash);
    return new Set(refs.map(ref => JSON.stringify([ref.id, ref.revision, ref.content_hash]))).size;
  }
  function runFailureCopy(run) {
    if (!isUnfinished(run)) return null;
    switch (run.error?.code) {
      case 'DUPLICATE_VARIANT_ID':
        return '本次话术计划里有多个方案使用了同一个编号，这批方案未通过检查。';
      case 'TEXT_SELECTION_QUERY_INVALID':
        return '检查话术原文时，读取范围未通过校验。';
      default:
        return null;
    }
  }
  function uuid() { return global.crypto && global.crypto.randomUUID ? global.crypto.randomUUID() : 'ui-' + Date.now() + '-' + Math.random().toString(16).slice(2); }
  function timeLabel(value) { if (!value) return ''; const d = new Date(value); return Number.isNaN(+d) ? safeText(value) : d.toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
  function eventKind(event) {
    const t = String(event.type || event.event_type || '');
    if (t === 'workflow.progress') return '任务进度';
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
  function isGraphPause(event) {
    return /^(tool|model)\.interrupted$/.test(event.type)
      || /^(tool|model)\.failed$/.test(event.type) && event.data?.status === 'INTERRUPTED';
  }
  function branchIdOf(event) {
    return event.branch_task_id || event.data.branch_task_id;
  }
  function fallbackProgress(events) {
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index];
      const branchId = branchIdOf(event);
      if (event.type === 'catalog_intake.fallback' && !branchId) {
        return { event, title: '正在换一种方式读取资料', status: 'RECOVERING' };
      }
      if (event.type === 'model.request') {
        return { event, title: branchId ? '等待子任务的模型结果' : '等待主 Agent 的模型结果', status: 'RUNNING' };
      }
      if (event.type === 'model.response' || event.type === 'model.completed') {
        return { event, title: branchId ? '子任务正在处理模型结果' : '主 Agent 正在处理模型结果', status: 'RUNNING' };
      }
      if (event.type === 'tool.started' || event.type === 'tool.completed') {
        const knownStep = STEP_LABELS[event.data.tool_name];
        const role = branchId ? '子任务' : '主 Agent';
        const title = event.type === 'tool.started' ? role + '正在执行工具' : role + '正在核对工具结果';
        return { event, title: knownStep || title, status: 'RUNNING' };
      }
      if (event.type === 'middleware.check' && !branchId && event.data.status === 'CORRECTING') {
        return { event, title: '正在处理上一步遇到的问题', status: 'RECOVERING' };
      }
    }
    return null;
  }
  function decisionText(value) { return global.ShengboDecisionCopy?.text(value) ?? safeText(value); }
  function decisionKey(q) { return q.decision_id + ':' + (q.revision ?? q.question_revision ?? 0); }
  function openQuestions(run) {
    if (TERMINAL.has(run.status) || ['CANCELLING', 'FINALIZING'].includes(run.status)) return [];
    return (run.questions || run.decisions || []).filter(q => q.decision_id
      && (!q.status || ['OPEN', 'PENDING', 'WAITING'].includes(String(q.status).toUpperCase())));
  }
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
    const state = { destroyed: false, conversation: null, conversations: [], messages: [], runs: new Map(), events: new Map(), cursors: new Map(), hydrated: new Set(), materials: [], showProcess: readStore(storage, prefix + ':process', false) === true, sending: false, uploading: false, preparingTask: false, pollBusy: null, hydrating: false, creatingConversation: false, drafts: new Map(), scopeNames: new Map(), pendingDecisions: new Set(), openDetails: new Set(), questionDrafts: new Map(), payloads: new Map(), visibleEventCounts: new Map(), generation: 0, selected: null, context: {}, error: '', failures: 0, controllers: new Set(), csrf: '', pendingSubmission: null, relatedRunOverride: null, scriptGeneration: null };
    let sessionPromise = null;
    let nextProgressPollAt = 0;
    let preflighting = false;
    let showWelcome = true;
    let highlightedMessageId = null;
    let historySignature = null;
    let deletingConversation = false;
    let deletionTarget = null;
    const messageTime = new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
    });
    const pointRequestStorageKey = prefix + ':catalog-point-requests';
    const storedPointRequests = readStore(storage, pointRequestStorageKey, []);
    const catalogPointRequests = new Map((Array.isArray(storedPointRequests) ? storedPointRequests : [])
      .filter(entry => entry?.request?.client_request_id && entry.conversationId && Array.isArray(entry.productIds))
      .map(entry => [entry.request.client_request_id, entry]));
    let submittingCatalogPoints = null;
    let reconciledCatalogPointRunId = null;
    const productMaterialBatches = new Map();
    let submittingProductBatch = null;
    let activeDecision = null;
    let decisionSignature = '';
    let focusBeforeDecision = null;
    const seenDecisions = new Set();
    const answeredDecisions = new Set();
    const decisionErrors = new Map();
    const decisionRequests = new Map();
    const storedCatalogDeferrals = readStore(storage, prefix + ':catalog-deferrals', []);
    const catalogDeferrals = new Set(Array.isArray(storedCatalogDeferrals) ? storedCatalogDeferrals : []);
    const node = (tag, cls, text) => { const n = doc.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = safeText(text); return n; };
    const button = (text, fn, cls = '') => { const b = node('button', cls, text); b.type = 'button'; b.addEventListener('click', fn); return b; };
    const root = node('section', 'sb-assistant'); root.setAttribute('aria-label', '声波 Agent 对话'); container.append(root);
    const head = node('div', 'sb-a-header');
    function avatar(className) {
      const mark = node('span', className);
      mark.setAttribute('aria-hidden', 'true');
      const img = node('img', 'sb-a-avatar');
      img.src = '/assets/shengbo-agent-avatar.png';
      img.alt = '';
      mark.append(img);
      return mark;
    }
    const identity = node('div', 'sb-a-identity');
    const title = node('div');
    title.append(node('strong', '', '声波 Agent'), node('small', '', options.subtitle || '整理资料、修改内容、查看保存结果'));
    identity.append(avatar('sb-a-mark'), title);
    const newTask = button('新对话', () => { if (!preflighting) createConversation(); }, 'sb-a-new');
    head.append(identity, newTask);
    const tabbar = node('div', 'sb-a-tabbar');
    const tabs = node('div', 'sb-a-tabs');
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', '切换对话');
    tabbar.append(tabs);
    tabs.addEventListener('keydown', event => {
      const choices = tabChoices().filter(tab => !tab.disabled);
      const index = choices.indexOf(doc.activeElement);
      if (index < 0) return;
      if (event.key === 'Delete') {
        event.preventDefault();
        showDeleteConversation(choices[index].dataset.conversationId);
        return;
      }
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? choices.length - 1 :
        (index + (event.key === 'ArrowRight' ? 1 : choices.length - 1)) % choices.length;
      event.preventDefault();
      choices.forEach((tab, position) => { tab.tabIndex = position === next ? 0 : -1; });
      choices[next].focus({ preventScroll: true });
      revealTab(choices[next]);
    });
    const tools = node('div', 'sb-a-toolbar');
    const history = node('details', 'sb-a-history');
    const historyToggle = node('summary', 'sb-a-history-toggle', '对话记录 · 0 条');
    const historyMenu = node('div', 'sb-a-history-menu');
    history.append(historyToggle, historyMenu);
    history.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || !history.open) return;
      event.preventDefault();
      history.open = false;
      historyToggle.focus();
    });
    const dismissHistory = event => {
      if (history.open && !history.contains(event.target)) history.open = false;
    };
    doc.addEventListener('pointerdown', dismissHistory);
    const toggleLabel = node('label', 'sb-a-switch');
    const toggle = node('input');
    toggle.type = 'checkbox';
    toggle.checked = state.showProcess;
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-label', '显示执行过程');
    const track = node('span', 'sb-a-switch-track');
    track.setAttribute('aria-hidden', 'true');
    toggleLabel.append(toggle, track, node('span', '', '执行过程'));
    toggle.addEventListener('change', () => {
      state.showProcess = toggle.checked;
      writeStore(storage, prefix + ':process', toggle.checked);
      revealConversation();
      renderConversation();
    });
    tools.append(history, toggleLabel);
    const context = node('div', 'sb-a-context');
    const errorBox = node('div', 'sb-a-error'); errorBox.hidden = true; errorBox.setAttribute('role', 'status');
    const transcript = node('div', 'sb-a-transcript');
    transcript.id = 'sb-transcript-' + uuid();
    transcript.setAttribute('role', 'region');
    transcript.setAttribute('aria-label', '对话及任务过程');
    transcript.tabIndex = 0;
    const liveNotice = node('div', 'sb-a-sr'); liveNotice.setAttribute('aria-live', 'polite'); liveNotice.setAttribute('role', 'status');
    const decisionBar = node('div', 'sb-a-decision-bar'); decisionBar.hidden = true;
    const decisionLabel = node('span', '', '');
    const showDecisions = button('查看待确认', () => {
      const next = pendingQuestions()[0];
      if (next) {
        revealConversation();
        activeDecision = next.key;
        renderDecisionPanel();
        decisionTitle.focus();
      }
    });
    showDecisions.setAttribute('aria-haspopup', 'dialog');
    decisionBar.append(decisionLabel, showDecisions);
    const decisionDialog = node('dialog', 'sb-a-decision-dialog');
    const decisionTitle = node('h2', 'sb-a-decision-title'); decisionTitle.id = 'sb-decision-' + uuid(); decisionTitle.tabIndex = -1;
    decisionDialog.setAttribute('aria-labelledby', decisionTitle.id);
    const decisionCount = node('p', 'sb-a-decision-count');
    const decisionHeader = node('header', 'sb-a-decision-header');
    const decisionHeading = node('div', '', ''); decisionHeading.append(decisionCount, decisionTitle);
    decisionHeader.append(decisionHeading, button('稍后处理', hideDecision, 'sb-a-decision-later'));
    const decisionPicker = node('div', 'sb-a-decision-picker'); decisionPicker.setAttribute('aria-label', '选择待确认事项');
    const decisionBody = node('div', 'sb-a-decision-body');
    decisionDialog.append(decisionHeader, decisionPicker, decisionBody);
    decisionDialog.addEventListener('keydown', e => { if (e.key === 'Escape') { e.preventDefault(); hideDecision(); } });
    const auditDialog = node('dialog', 'sb-a-decision-dialog sb-a-start-audit');
    let auditScope = null;
    const auditTitle = node('h2', 'sb-a-decision-title', '开播前还需完成');
    auditTitle.id = 'sb-start-audit-' + uuid();
    auditDialog.setAttribute('aria-labelledby', auditTitle.id);
    const auditHeader = node('header', 'sb-a-decision-header');
    const auditClose = button('稍后处理', () => auditDialog.close(), 'sb-a-decision-later');
    auditHeader.append(auditTitle, auditClose);
    const auditBody = node('div', 'sb-a-decision-body');
    auditDialog.append(auditHeader, auditBody);
    auditDialog.addEventListener('keydown', e => { if (e.key === 'Escape') { e.preventDefault(); auditDialog.close(); } });
    const deleteDialog = node('dialog', 'sb-a-decision-dialog sb-a-delete-conversation');
    const deleteTitle = node('h2', '', '删除这段对话？');
    deleteTitle.id = 'sb-delete-conversation-' + uuid();
    deleteDialog.setAttribute('aria-labelledby', deleteTitle.id);
    const deleteCopy = node('p', 'sb-a-delete-description');
    const deleteError = node('p', 'sb-a-decision-error');
    deleteError.setAttribute('role', 'alert');
    deleteError.hidden = true;
    const deleteActions = node('div', 'sb-a-delete-actions');
    const cancelDelete = button('取消', () => deleteDialog.close());
    const confirmDelete = button('删除对话', deleteConversation, 'sb-a-delete-confirm');
    deleteActions.append(cancelDelete, confirmDelete);
    deleteDialog.append(deleteTitle, deleteCopy, deleteError, deleteActions);
    deleteDialog.addEventListener('cancel', event => {
      if (deletingConversation) event.preventDefault();
    });
    const form = node('form', 'sb-a-composer'); const attachments = node('div', 'sb-a-attachments');
    const target = node('div', 'sb-a-selection'); target.hidden = true;
    const input = node('textarea', 'sb-a-input'); input.rows = 3; input.maxLength = 16000; input.placeholder = '告诉我这次需要准备什么…'; input.setAttribute('aria-label', '发送给播前助手的要求');
    input.addEventListener('focus', revealConversation);
    const bottom = node('div', 'sb-a-compose-bottom'); const upload = node('input', 'sb-a-file'); upload.type = 'file'; upload.multiple = true; upload.accept = '.txt,.md,.doc,.docx,.rtf,.xlsx,.pdf,.json,.csv,.png,.jpg,.jpeg,.webp'; upload.hidden = true;
    const attach = button('＋ 添加资料', () => { revealConversation(); upload.click(); }, 'sb-a-attach');
    const mode = node('select', 'sb-a-mode'); mode.setAttribute('aria-label', '任务提交方式'); [['new_task', '新任务'], ['follow_up', '接着当前任务'], ['replace', '停止原任务并重做']].forEach(([v, t]) => { const o = node('option', '', t); o.value = v; mode.append(o); });
    const send = node('button', 'sb-a-send', '发送'); send.type = 'submit'; bottom.append(attach, mode, send);
    const hint = node('p', 'sb-a-hint', 'Enter 发送 · Shift + Enter 换行。执行过程关闭时，后台仍保存安全记录。');
    form.append(context, target, attachments, input, bottom, upload, hint);
    root.append(head, tabbar, tools, errorBox, transcript, liveNotice, decisionBar, form, decisionDialog, auditDialog, deleteDialog);
    function setError(text) { state.error = safeText(text); errorBox.replaceChildren(); errorBox.hidden = !text; if (text) errorBox.append(node('span', '', state.error), button('重试连接', () => refresh(), 'sb-a-link')); }
    function viewContext() { const raw = options.getViewContext ? options.getViewContext() || {} : {}; return { ...raw, ...state.context }; }
    function scopeNow() { const c = viewContext(); return c.preparation_id ? { kind: 'preparation', preparation_id: c.preparation_id, account_id: 'local' } : { kind: 'catalog', account_id: 'local' }; }
    function renderContext() {
      const c = state.conversation, view = viewContext();
      const preparation = c ? c.scope?.preparation_id || c.preparation_id : view.preparation_id;
      const productMaterial = activeProductMaterialBatch();
      const label = productMaterial
        ? (productMaterial.composeProduct ? '更新商品 · ' + productMaterial.composeProduct.name : '新增商品 · 本批资料')
        : preparation
        ? state.scopeNames.get(preparation) || (c?.title ? '「' + c.title + '」绑定的本场' : '当前选中的本场')
        : '共享商品库与全局资料';
      const mismatch = !!c && (preparation || null) !== (view.preparation_id || null);
      context.dataset.scopeMismatch = String(mismatch);
      const scope = node('div', 'sb-a-send-scope');
      scope.append(node('span', '', '本次发送到'), node('strong', '', label));
      context.replaceChildren(scope);
      if (mismatch) {
        const action = view.preparation_id ? '为左侧本场新建对话' : '新建商品库对话';
        context.append(node('small', '', '左侧已切换，当前对话仍绑定原范围。'),
          button(action, () => { if (!preflighting) createConversation(); }, 'sb-a-link'));
      }
    }
    function revealConversation() {
      if (!showWelcome) return;
      showWelcome = false;
      renderTabs();
      renderConversation();
    }
    function canSwitchConversation() {
      return !(preflighting || state.sending || state.uploading || state.preparingTask || state.creatingConversation || deletingConversation);
    }
    function tabChoices() { return [...tabs.querySelectorAll('.sb-a-tab')]; }
    function revealTab(tab) {
      if (!tab || !tabs.clientWidth) return;
      const rail = tabs.getBoundingClientRect();
      const item = tab.parentElement.getBoundingClientRect();
      if (item.left < rail.left) tabs.scrollLeft += item.left - rail.left;
      else if (item.right > rail.right) tabs.scrollLeft += item.right - rail.right;
    }
    function renderTabs() {
      const focused = tabs.contains(doc.activeElement) ? doc.activeElement?.dataset.conversationId : null;
      const current = state.conversation?.conversation_id;
      tabs.replaceChildren();
      for (const conversation of state.conversations) {
        const id = conversation.conversation_id;
        const tab = button(conversation.title || '播前准备对话', () => {
          if (!canSwitchConversation()) return;
          if (id === state.conversation?.conversation_id && !state.hydrating) revealConversation();
          else loadConversation(id);
        }, 'sb-a-tab');
        tab.dataset.conversationId = id;
        tab.title = tab.textContent;
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-selected', String(!showWelcome && id === current));
        tab.setAttribute('aria-controls', transcript.id);
        tab.tabIndex = id === (focused || current || state.conversations[0]?.conversation_id) ? 0 : -1;
        tab.disabled = !canSwitchConversation();
        const slot = node('div', 'sb-a-tab-slot');
        slot.setAttribute('role', 'presentation');
        const remove = button('×', () => showDeleteConversation(id), 'sb-a-tab-delete');
        remove.setAttribute('aria-label', '删除对话「' + tab.textContent + '」');
        remove.title = '删除对话';
        remove.tabIndex = -1;
        remove.disabled = !canSwitchConversation();
        slot.append(tab, remove);
        tabs.append(slot);
      }
      tabbar.hidden = !state.conversations.length;
      const focusedTab = tabChoices().find(tab => tab.dataset.conversationId === focused);
      if (focusedTab && !focusedTab.disabled) focusedTab.focus({ preventScroll: true });
      revealTab(focusedTab || tabChoices().find(tab => tab.dataset.conversationId === current));
    }
    function setBusy() {
      send.disabled = state.sending || state.uploading || state.preparingTask || state.hydrating || state.creatingConversation;
      send.textContent = state.sending ? '正在提交…' : state.preparingTask ? '正在绑定本场…' : '发送';
      const materialTask = !!activeProductMaterialBatch();
      attach.disabled = send.disabled || preflighting || materialTask || !!state.scriptGeneration;
      mode.disabled = materialTask || !!state.scriptGeneration;
      newTask.disabled = send.disabled || preflighting;
      for (const control of tabs.querySelectorAll('button')) control.disabled = !canSwitchConversation();
    }
    function messageId(message) { return message.message_id || message.id; }
    function messageSummary(message) {
      const content = safeText(message.text ?? message.content).replace(/\s+/g, ' ').trim();
      if (!content) return '上传资料';
      const characters = [...content];
      return characters.length > 38 ? characters.slice(0, 38).join('') + '…' : content;
    }
    function sentTime(message) {
      const raw = message.created_at ?? message.sent_at ?? message.timestamp;
      if (!raw) return null;
      const time = new Date(raw);
      return Number.isNaN(time.getTime()) ? null : time;
    }
    function locateMessage(id) {
      const targetMessage = [...transcript.querySelectorAll('[data-message-id]')]
        .find(article => article.dataset.messageId === id);
      if (!targetMessage) return;
      const offset = targetMessage.getBoundingClientRect().top - transcript.getBoundingClientRect().top;
      transcript.scrollTop += offset - 12;
      targetMessage.focus({ preventScroll: true });
    }
    function renderHistory() {
      const prompts = state.messages.filter(message => message.role === 'user' && messageId(message));
      const signature = JSON.stringify([state.conversation?.conversation_id,
        prompts.map(message => [messageId(message), messageSummary(message), sentTime(message)?.getTime()])]);
      if (signature === historySignature) return;
      historySignature = signature;
      historyToggle.textContent = '对话记录 · ' + prompts.length + ' 条';
      historyMenu.replaceChildren();
      if (!prompts.length) {
        historyMenu.append(node('p', 'sb-a-history-empty', '当前对话还没有发送过提示词。'));
        return;
      }
      for (const message of prompts) {
        const id = messageId(message);
        const entry = button('', () => {
          history.open = false;
          highlightedMessageId = id;
          if (showWelcome) revealConversation();
          else renderConversation();
          locateMessage(id);
        }, 'sb-a-history-entry');
        entry.dataset.targetMessageId = id;
        const time = sentTime(message);
        const stamp = node('time', 'sb-a-history-time', time ? messageTime.format(time) : '发送时间不可用');
        if (time) stamp.dateTime = time.toISOString();
        entry.append(node('span', 'sb-a-history-summary', messageSummary(message)), stamp);
        historyMenu.append(entry);
      }
    }
    function showDeleteConversation(id) {
      if (!canSwitchConversation() || state.hydrating || deleteDialog.open) return;
      deletionTarget = state.conversations.find(item => item.conversation_id === id);
      if (!deletionTarget) return;
      deleteCopy.textContent = '删除「' + (deletionTarget.title || '播前准备对话') +
        '」及该对话的未发送草稿。已保存的商品、本场配置和话术会保留。';
      deleteError.hidden = true;
      deleteError.textContent = '';
      deleteDialog.showModal();
      cancelDelete.focus();
    }
    async function deleteConversation() {
      if (!deletionTarget || deletingConversation) return;
      const id = deletionTarget.conversation_id;
      deletingConversation = true;
      cancelDelete.disabled = true;
      confirmDelete.disabled = true;
      confirmDelete.textContent = '正在删除…';
      deleteError.hidden = true;
      setBusy();
      try {
        const reply = await api('/conversations/' + encodeURIComponent(id), { method: 'DELETE' });
        if (!reply.deleted || reply.conversation_id !== id) throw Error('暂未确认对话是否已删除，请重试。');
        const position = state.conversations.findIndex(item => item.conversation_id === id);
        state.conversations = state.conversations.filter(item => item.conversation_id !== id);
        state.drafts.delete(id);
        productMaterialBatches.delete(id);
        if (state.conversation?.conversation_id === id) {
          const next = state.conversations[Math.max(0, position)] || state.conversations.at(-1);
          await loadConversation(next?.conversation_id || null, undefined, { moveDraft: true, reset: !next });
          if (state.conversation?.conversation_id === id) {
            await loadConversation(null, undefined, { moveDraft: true, reset: true });
          }
        } else {
          renderTabs();
        }
        deleteDialog.close();
        deletionTarget = null;
        liveNotice.textContent = '对话已删除';
      } catch (error) {
        deleteError.textContent = safeText(error.message || '删除失败，请重试。');
        deleteError.hidden = false;
      } finally {
        deletingConversation = false;
        cancelDelete.disabled = false;
        confirmDelete.disabled = false;
        confirmDelete.textContent = '删除对话';
        setBusy();
        if (!deleteDialog.open) (tabChoices().find(tab => tab.tabIndex === 0) || newTask).focus({ preventScroll: true });
      }
    }
    function csrfToken() { const el = doc.querySelector('meta[name="csrf-token"]'); return state.csrf || (el ? el.content : ''); }
    async function ensureSession() { if (state.csrf) return; if (!sessionPromise) sessionPromise = api('/session').then(value => { if (!value.csrf_token) throw Error('本机会话未返回请求校验信息'); state.csrf = value.csrf_token; }).finally(() => { sessionPromise = null; }); await sessionPromise; }
    async function api(path, { method = 'GET', body, multipart = false } = {}) {
      if (method !== 'GET') await ensureSession();
      const controller = new AbortController(); state.controllers.add(controller); const timeout = setTimeout(() => controller.abort(), 25000);
      try { const headers = {}; if (body && !multipart) headers['Content-Type'] = 'application/json'; const csrf = csrfToken(); if (csrf && method !== 'GET') headers['X-CSRF-Token'] = csrf;
        const r = await requestFetch(path.startsWith('/api/') ? path : apiBase + path, { method, headers, credentials: 'same-origin', body: body ? multipart ? body : JSON.stringify(body) : undefined, signal: controller.signal });
        let data; try { data = await r.json(); } catch (_) { throw new Error('服务响应格式异常，请重试连接'); }
        if (!r.ok) { const detail = data.detail || data.error; throw Object.assign(new Error(typeof detail === 'string' ? safeText(detail) : detail && (detail.message || detail.code) || '请求失败（' + r.status + '）'), { status: r.status, code: detail?.code }); } return data;
      } finally { clearTimeout(timeout); state.controllers.delete(controller); }
    }
    function addRun(raw) { if (!raw || !(raw.run_id || raw.id)) return; const r = normalizedRun(raw); state.runs.set(r.run_id, { ...(state.runs.get(r.run_id) || {}), ...r }); }
    function absorbRun(raw) { const before = state.runs.get(raw.run_id || raw.id); addRun(raw); for (const message of raw.messages || []) { const id = message.message_id || message.id; if (id && !state.messages.some(item => (item.message_id || item.id) === id)) state.messages.push(message); } const r = state.runs.get(raw.run_id || raw.id); if (r && (!before || before.status !== r.status)) liveNotice.textContent = runStatusName(r); }
    function publicState() {
      const pendingRequests = Array.from(state.drafts.entries())
        .filter(([id]) => id !== state.conversation?.conversation_id)
        .map(([, draft]) => draft.pendingSubmission?.request)
        .filter(Boolean);
      if (state.pendingSubmission) pendingRequests.push(state.pendingSubmission.request);
      for (const batch of productMaterialBatches.values()) if (batch.pending) pendingRequests.push(batch.pending);
      for (const entry of catalogPointRequests.values()) pendingRequests.push(entry.request);
      const pendingRequest = submittingCatalogPoints || submittingProductBatch?.pending || state.pendingSubmission?.request || null;
      const materialTask = activeProductMaterialBatch();
      const materials = materialTask?.materials || state.materials;
      return { pendingRequests: structuredClone(pendingRequests), pendingDecisionCount: pendingQuestions().length, conversation: state.conversation ? structuredClone(state.conversation) : null, runs: Array.from(state.runs.values()).map(r => structuredClone(r)), selected: !materialTask && state.selected ? structuredClone(state.selected) : null, pendingRequest: structuredClone(pendingRequest), reconciledCatalogPointRunId, inputText: input.value, materials: structuredClone(materials), hasPendingInput: !!input.value.trim() || materials.length > 0 || !!state.scriptGeneration, showProcess: state.showProcess, sending: state.sending, hydrating: state.hydrating }; }
    function announceState() { if (options.onState) options.onState(publicState()); }
    function currentRun() { return Array.from(state.runs.values()).at(-1); }
    async function initialize() { try { await ensureSession(); const list = await api('/conversations'); if (state.destroyed) return; state.conversations = list.items || list.conversations || []; renderTabs(); const saved = readStore(storage, prefix + ':conversation', null); const first = state.conversations.find(x => x.conversation_id === saved) || state.conversations[0]; if (first) await loadConversation(first.conversation_id, undefined, { keepWelcome: true }); else renderConversation(); } catch (e) { if (!state.destroyed) setError('助手暂未连接：' + e.message); } }
    function composerDraft() {
      return {
        text: input.value, mode: mode.value, materials: state.materials, selected: state.selected,
        pendingSubmission: state.pendingSubmission, relatedRunOverride: state.relatedRunOverride,
        scriptGeneration: state.scriptGeneration
      };
    }
    async function requestConversation(view, title) {
      const response = await api('/conversations', { method: 'POST', body: {
        client_request_id: uuid(), scope: view.preparation_id
          ? { kind: 'preparation', preparation_id: view.preparation_id, account_id: 'local' }
          : { kind: 'catalog', account_id: 'local' },
        room_id: view.room_id || null, preparation_id: view.preparation_id || null,
        ...(title ? { title } : {})
      } });
      const conversation = response.conversation || response;
      if (!conversation.conversation_id) throw Error('服务没有返回新对话编号，请检查连接。');
      state.conversations.unshift(conversation);
      return conversation;
    }
    async function createConversation(contextOverride, { moveDraft = false } = {}) {
      if (state.sending || state.uploading || state.hydrating || state.creatingConversation) return;
      state.creatingConversation = true;
      setBusy();
      try {
        if (moveDraft && state.pendingSubmission) throw Error('原对话有待核实的请求，请先在原对话确认是否已受理。');
        if (moveDraft && state.materials.length) throw Error('附件绑定原对话。原草稿已保留，请在目标本场的新对话中重新添加资料。');
        const v = contextOverride && !contextOverride.type ? { ...viewContext(), ...contextOverride } : viewContext();
        const carry = moveDraft || !state.conversation ? composerDraft() : null;
        const conv = await requestConversation(v);
        if (carry) state.drafts.set(conv.conversation_id, { ...carry, mode: 'new_task', relatedRunOverride: null });
        await loadConversation(conv.conversation_id, conv, { moveDraft });
        return conv;
      } catch (e) { setError(e.message); return null; }
      finally { state.creatingConversation = false; setBusy(); }
    }
    async function loadConversation(id, supplied, { moveDraft = false, keepWelcome = false, reset = false } = {}) {
      if (!id && !reset) return;
      const generation = ++state.generation;
      state.hydrating = true;
      setBusy();
      try {
        const c = id ? supplied || await api('/conversations/' + encodeURIComponent(id)) : {};
        if (generation !== state.generation || state.destroyed) return;
        const oldId = state.conversation?.conversation_id;
        if (oldId) {
          if (moveDraft) state.drafts.delete(oldId);
          else state.drafts.set(oldId, composerDraft());
        }
        const saved = state.drafts.get(id);
        state.conversation = id ? c.conversation || c : null;
        if (!id || !keepWelcome) showWelcome = !id;
        if (oldId !== id) {
          highlightedMessageId = null;
          history.open = false;
        }
        state.messages = c.messages || state.conversation?.messages || [];
        state.runs.clear();
        state.events.clear();
        state.cursors.clear();
        state.hydrated.clear();
        activeDecision = null;
        input.value = saved?.text || '';
        mode.value = saved?.mode || 'new_task';
        state.materials = saved?.materials || [];
        state.selected = saved?.selected || null;
        state.pendingSubmission = saved?.pendingSubmission || null;
        state.relatedRunOverride = saved?.relatedRunOverride || null;
        state.scriptGeneration = saved?.scriptGeneration || null;
        for (const r of c.runs || []) addRun(r);
        writeStore(storage, prefix + ':conversation', id);
        renderTabs();
        renderContext();
        renderSelection();
        renderAttachments();
        setError('');
        renderConversation();
        announceState();
        if (id) await pollRuns(generation, true);
      } catch (e) {
        if (generation === state.generation) {
          renderTabs();
          setError(e.message);
        }
      } finally {
        if (generation === state.generation) {
          state.hydrating = false;
          setBusy();
          announceState();
        }
      }
    }
    function ingestEvents(runId, events, nextCursor) { const map = state.events.get(runId) || new Map(); let highest = state.cursors.get(runId) || 0; for (const e of events || []) { if (!e || e.schema_version !== VERSION || typeof e.event_id !== 'string' || typeof e.type !== 'string' || e.run_id !== runId || !e.data || typeof e.data !== 'object' || Array.isArray(e.data)) throw Error('执行事件格式与 ' + VERSION + ' 不一致，未展示该记录'); const seq = Number(e.sequence); if (!Number.isSafeInteger(seq) || seq < 1) throw Error('执行事件缺少有效的持久顺序编号'); if (!map.has(e.event_id)) map.set(e.event_id, { ...e, sequence: seq }); highest = Math.max(highest, seq); } if (Number.isFinite(Number(nextCursor))) highest = Math.max(highest, Number(nextCursor)); state.events.set(runId, map); state.cursors.set(runId, highest); writeStore(storage, prefix + ':cursor:' + runId, highest); }
    async function pollRuns(generation = state.generation, force = false) {
      if (state.pollBusy === generation || state.destroyed) return;
      state.pollBusy = generation;
      try {
        const pageSize = 200;
        const runs = Array.from(state.runs.values());
        for (const original of runs) {
          if (generation !== state.generation || state.destroyed) return;
          const id = original.run_id;
          if (!force && TERMINAL.has(original.status) && state.hydrated.has(id)) continue;
          const readPage = after => api('/runs/' + encodeURIComponent(id) + '/events?after=' + after + '&limit=' + pageSize);
          let [r, page] = await Promise.all([api('/runs/' + encodeURIComponent(id)), readPage(state.cursors.get(id) || 0)]);
          if (generation !== state.generation || state.destroyed) return;
          absorbRun(r.run || r);
          for (let pages = 0; pages < 20; pages += 1) {
            if (generation !== state.generation || state.destroyed) return;
            const before = state.cursors.get(id) || 0;
            const events = page.events || page.items || [];
            ingestEvents(id, events, page.next_cursor);
            const more = typeof page.has_more === 'boolean' ? page.has_more : events.length === pageSize;
            if (!more) {
              state.hydrated.add(id);
              break;
            }
            const cursor = state.cursors.get(id) || 0;
            if (cursor <= before) throw Error('执行记录分页游标未前进，请重试连接');
            state.hydrated.delete(id);
            if (pages < 19) page = await readPage(cursor);
          }
        }
        state.failures = 0;
        nextProgressPollAt = 0;
        if (state.error) setError('');
        renderConversation();
        announceState();
      } catch (e) {
        if (!state.destroyed && generation === state.generation) {
          state.failures += 1;
          const retryDelay = Math.min(30000, 1000 * 2 ** Math.min(state.failures - 1, 5));
          nextProgressPollAt = Date.now() + retryDelay;
          setError('进度连接中断，正在重新连接。任务可能仍在后台运行：' + e.message);
        }
      } finally {
        if (state.pollBusy === generation) state.pollBusy = null;
      }
    }
    function safeAppendText(parent, text, cls) { const p = node('p', cls, typeof text === 'string' ? text : jsonText(text)); parent.append(p); return p; }
    function details(label, value, id) { const box = node('details', 'sb-a-details'); box.open = state.openDetails.has(id); box.addEventListener('toggle', () => box.open ? state.openDetails.add(id) : state.openDetails.delete(id)); box.append(node('summary', '', label)); const body = node('pre', 'sb-a-json', typeof value === 'string' ? safeText(value) : jsonText(value)); box.append(body, button('复制', async () => { try { await global.navigator.clipboard.writeText(body.textContent); liveNotice.textContent = '已复制安全记录'; } catch (_) { liveNotice.textContent = '无法自动复制，可选中文本手动复制'; } }, 'sb-a-copy')); return box; }
    function eventCard(e, runId) { const d = e.data || {}; const paused = isGraphPause(e); const card = node('article', 'sb-a-event'); card.dataset.eventId = e.event_id; const h = node('div', 'sb-a-event-heading'); h.append(node('span', 'sb-a-event-kind', paused ? '人工确认' : eventKind(e)), node('strong', '', paused ? '执行已暂停' : eventTitle(e)), node('small', '', '#' + e.sequence + ' · ' + timeLabel(e.timestamp || e.occurred_at))); card.append(h); if (paused || d.status || e.status) card.append(node('span', 'sb-a-badge', paused ? '等待人工答复' : statusName(d.status || e.status))); if (d.summary) safeAppendText(card, d.summary, 'sb-a-event-summary');
      // 只展示模型明确公开的决策摘要；不展示私有推理字段。
      if (d.decision_summary || d.reason_summary) safeAppendText(card, d.decision_summary || d.reason_summary, 'sb-a-reason');
      if (d.progress && Number.isFinite(d.progress.completed) && Number.isFinite(d.progress.total) && d.progress.total > 0) card.append(node('small', 'sb-a-progress', '进度 ' + d.progress.completed + ' / ' + d.progress.total));
      if (d.arguments !== undefined) card.append(details('发送的参数', d.arguments, e.event_id + ':args'));
      else if (d.parameters !== undefined) card.append(details('步骤参数', d.parameters, e.event_id + ':args'));
      if (Number.isFinite(d.duration_ms)) card.append(node('small', 'sb-a-progress', '耗时 ' + (d.duration_ms / 1000).toFixed(2) + ' 秒'));
      if (!paused && d.result_preview !== undefined) card.append(details('返回数据摘要', d.result_preview, e.event_id + ':result'));
      if (!paused && d.error) safeAppendText(card, typeof d.error === 'string' ? d.error : jsonText(d.error), 'sb-a-event-error');
      card.append(details('事件结构与关联', { schema_version: e.schema_version, event_id: e.event_id, sequence: e.sequence, run_id: e.run_id, invocation_id: e.invocation_id || null, branch_task_id: e.branch_task_id || null, parent_event_id: e.parent_event_id || null, type: e.type, timestamp: e.timestamp, data: d }, e.event_id + ':metadata'));
      const payloadRef = d.payload_ref || (e.payload_refs && e.payload_refs[0] && (e.payload_refs[0].sha256 || e.payload_refs[0].id)); if (payloadRef) { const ref = typeof payloadRef === 'string' ? payloadRef : payloadRef.sha256 || payloadRef.id; const cacheKey = runId + ':' + ref; if (state.payloads.has(cacheKey)) card.append(details('完整安全记录', state.payloads.get(cacheKey), e.event_id + ':full')); else { const b = button('展开完整安全记录', async () => { b.disabled = true; try { const p = await api('/runs/' + encodeURIComponent(runId) + '/payloads/' + encodeURIComponent(ref)); const safe = redact(p.payload !== undefined ? p.payload : p); state.payloads.set(cacheKey, safe); card.append(details('完整安全记录', safe, e.event_id + ':full')); b.remove(); } catch (err) { b.disabled = false; liveNotice.textContent = err.message; } }, 'sb-a-link'); card.append(b); } }
      return card;
    }
    function runOwnsProgressStatus(run) {
      return TERMINAL.has(run.status)
        || ['WAITING_USER', 'WAITING_JOBS', 'FINALIZING', 'CANCELLING'].includes(run.status);
    }
    function currentProgress(run, events, milestones) {
      const latest = milestones.at(-1);
      const ownsStatus = runOwnsProgressStatus(run);
      const milestone = latest ? { event: latest, ...latest.data } : fallbackProgress(events);
      const current = node('section', 'sb-a-current-progress');
      current.setAttribute('aria-label', '当前进度');
      current.dataset.status = ownsStatus ? run.status : milestone?.status || run.status;
      current.append(node('span', 'sb-a-current-label', '当前进度'),
        node('strong', '', safeText(ownsStatus ? runStatusName(run) : milestone?.title || runStatusName(run))));
      const recovery = run.recovery_pending;
      const summary = ownsStatus ? runFailureCopy(run) || run.summary
        : recovery?.state === 'planning' ? recovery.message : milestone?.summary || (!milestone && run.summary);
      if (summary) safeAppendText(current, summary, 'sb-a-current-summary');
      const time = milestone?.event && timeLabel(milestone.event.timestamp || milestone.event.occurred_at);
      if (time && !ownsStatus) current.append(node('small', 'sb-a-current-time', '最近更新 ' + time));
      const requests = events.filter(event => event.type === 'model.request');
      if (requests.length) {
        const children = requests.filter(branchIdOf).length;
        current.append(node('small', 'sb-a-current-requests', 'Agent 模型请求 '
          + requests.length + ' 次（主 Agent ' + (requests.length - children) + '，子任务 ' + children + '）'));
      }
      return current;
    }
    function processPanel(run) {
      const wrap = node('section', 'sb-a-process');
      wrap.setAttribute('aria-label', '任务执行过程');
      const complete = Array.from((state.events.get(run.run_id) || new Map()).values()).sort((a, b) => a.sequence - b.sequence);
      const recoveryMilestones = new Set(['runtime.recovery.planning', 'runtime.recovery.waiting',
        'runtime.recovery.resuming', 'runtime.recovery.source_restored']);
      const milestones = complete.filter(event => (event.type === 'workflow.progress' || recoveryMilestones.has(event.type))
        && !branchIdOf(event) && event.data.title);
      wrap.append(currentProgress(run, complete, milestones));
      const previous = runOwnsProgressStatus(run) ? milestones : milestones.slice(0, -1);
      if (previous.length) {
        const history = node('ol', 'sb-a-progress-history');
        for (const event of previous) {
          const row = node('li', '');
          const time = timeLabel(event.timestamp || event.occurred_at);
          if (time) row.append(node('time', '', time));
          row.append(node('span', '', safeText(event.data.title)));
          if (event.data.summary) safeAppendText(row, event.data.summary, 'sb-a-event-summary');
          history.append(row);
        }
        wrap.append(history);
      }
      const steps = [];
      const receipts = run.receipts || [];
      if (receipts.some(entry => entry.command === 'save_script_plan' && ['committed', 'replayed'].includes(entry.receipt?.status))) {
        steps.push('已保存话术方案');
      }
      const generationJobs = new Set(receipts
        .filter(entry => entry.command === 'submit_script_change' && entry.receipt?.status === 'accepted')
        .map(entry => entry.receipt.data?.job_id).filter(Boolean));
      if (generationJobs.size) {
        steps.push(generationJobs.size === 1 ? '已提交文字生成' : '已提交 ' + generationJobs.size + ' 次文字生成');
      }
      if (steps.length) {
        const progress = node('section', 'sb-a-run-progress');
        progress.append(node('h4', '', '话术进度'), ...steps.map(label => node('p', 'sb-a-process-note', label)));
        wrap.append(progress);
      }
      const corrections = correctionSteps(complete);
      if (corrections.length) {
        const section = node('section', 'sb-a-corrections');
        const list = node('ul');
        for (const step of corrections) {
          const outcome = step.succeeded ? '已修正并重试成功'
            : step.failedAgain ? '已修正并重试，仍未通过'
            : step.retried ? '已重试，尚未确认成功' : '进入修正，未记录重试';
          list.append(node('li', '', step.label + '：' + outcome));
        }
        section.append(node('h4', '', '任务修正'), list);
        wrap.append(section);
      }
      const rawKey = run.run_id + ':raw-events';
      const raw = node('details', 'sb-a-details sb-a-raw-events');
      raw.open = state.openDetails.has(rawKey);
      raw.addEventListener('toggle', () => raw.open ? state.openDetails.add(rawKey) : state.openDetails.delete(rawKey));
      raw.append(node('summary', '', '详细执行记录'),
        node('p', 'sb-a-process-note', '并行子任务单独分组，编号为记录顺序。'));
      wrap.append(raw);
      const final = finalFailureEvents(run, complete);
      const timeline = final.length ? complete.slice(0, -final.length) : complete;
      const visibleCount = state.visibleEventCounts.get(run.run_id) || 120;
      if (timeline.length > visibleCount) raw.append(button('展开更早的 ' + Math.min(120, timeline.length - visibleCount) + ' 条记录（共 ' + timeline.length + ' 条）', () => { state.visibleEventCounts.set(run.run_id, visibleCount + 120); renderConversation(); }, 'sb-a-link'));
      const all = timeline.slice(-visibleCount);
      const main = node('div', 'sb-a-main-events');
      const branches = new Map();
      for (const event of all) {
        const branchId = branchIdOf(event);
        if (branchId) {
          if (!branches.has(branchId)) branches.set(branchId, []);
          branches.get(branchId).push(event);
        } else {
          main.append(eventCard(event, run.run_id));
        }
      }
      if (final.length) {
        const detail = node('details', 'sb-a-details');
        const key = run.run_id + ':final-failure';
        detail.open = state.openDetails.has(key);
        detail.addEventListener('toggle', () => detail.open ? state.openDetails.add(key) : state.openDetails.delete(key));
        detail.append(node('summary', '', '本次结束的详细记录'), ...final.map(event => eventCard(event, run.run_id)));
        main.append(detail);
      }
      if (main.childElementCount) { const title = node('h4', '', '主 Agent 与服务检查'); raw.append(title, main); }
      for (const branch of run.branches || run.branch_tasks || []) if (!branches.has(branch.branch_task_id || branch.id)) branches.set(branch.branch_task_id || branch.id, []);
      if (branches.size) { const group = node('section', 'sb-a-branches'); group.append(node('h4', '', '子任务 · 可并行执行')); for (const [id, events] of branches) { const branch = (run.branches || run.branch_tasks || []).find(x => (x.branch_task_id || x.id) === id) || {}; const first = events[0] || {}; const last = events.at(-1) || {}; const role = branch.role || branch.branch_role || first.agent_role || first.data && first.data.role; const box = node('details', 'sb-a-branch'); box.open = state.openDetails.has(run.run_id + ':branch:' + id); box.addEventListener('toggle', () => box.open ? state.openDetails.add(run.run_id + ':branch:' + id) : state.openDetails.delete(run.run_id + ':branch:' + id)); box.append(node('summary', '', (ROLES[role] || branch.title || '分析子任务') + ' · ' + statusName(branch.status || last.data && last.data.status || last.status || 'RUNNING'))); box.append(node('small', 'sb-a-branch-id', id)); if (branch.summary) safeAppendText(box, branch.summary, 'sb-a-event-summary'); events.forEach(e => box.append(eventCard(e, run.run_id))); group.append(box); } raw.append(group); }
      if (!all.length && !branches.size && !final.length) raw.append(node('p', 'sb-a-empty-process', '尚未收到执行事件；不会据此判断任务已开始或已完成。')); return wrap;
    }
    function openArtifact(destination) {
      const show = options.onResult || options.onNavigate;
      if (show) return show(destination);
      root.dispatchEvent(new CustomEvent('assistant:navigate', { bubbles: true, detail: destination }));
    }
    function artifactCard(a) {
      const b = button(a.label || a.title || '查看成果', () => openArtifact(a.destination || a), 'sb-a-artifact');
      b.title = '在左侧打开对应内容';
      if (a.destination?.object_ref) b.dataset.savedResult = 'true';
      return b;
    }
    function catalogIntakeProducts(run) {
      const products = run?.catalog_intake_result?.products;
      if (run?.status !== 'COMPLETED' || !Array.isArray(products)) return [];
      return products.filter(item => typeof item?.product_id === 'string' && item.product_id && typeof item.name === 'string' && item.name);
    }
    async function readCatalogProduct(item) {
      const latest = await api('/api/prelive/v2/queries', { method: 'POST', body: {
        contract_version: VERSION, query: 'read_product', args: { product_id: item.product_id }
      } });
      const ref = latest.ref;
      if (ref?.kind !== 'product' || ref.id !== item.product_id || !Number.isInteger(ref.revision)
          || !ref.content_hash || !latest.body || latest.body.archived) {
        throw Error('无法打开「' + item.name + '」，请从商品知识库核对。');
      }
      return { latest, destination: { authority: 'voice', view: 'products', product_id: ref.id, preparation_id: null, room_id: null } };
    }
    async function openCatalogSellingEditor(item) {
      const generation = state.generation;
      try {
        const { latest, destination } = await readCatalogProduct(item);
        if (!state.destroyed && generation === state.generation) {
          await openArtifact({ ...destination, object_ref: latest.ref, field_path: '/core_selling_points' });
        }
      } catch (error) {
        if (!state.destroyed && generation === state.generation) setError(error.message);
      }
    }
    function saveCatalogPointRequests() {
      writeStore(storage, pointRequestStorageKey, Array.from(catalogPointRequests.values()));
    }
    async function submitCatalogSellingPoints(items, reportRunId, retry = null) {
      if (preflighting || state.sending || state.uploading || state.preparingTask || state.hydrating || state.creatingConversation) return;
      const conversation = state.conversation;
      const conversationId = conversation?.conversation_id;
      if (!conversationId || conversation.scope?.kind !== 'catalog' || conversation.room_id || conversation.preparation_id) {
        setError('请打开原商品入库对话，再生成核心卖点。');
        return;
      }
      const generation = state.generation;
      const productIds = items.map(item => item.product_id);
      state.sending = true;
      setBusy();
      setError('');
      try {
        let entry = retry;
        if (entry && (entry.conversationId !== conversationId || !catalogPointRequests.has(entry.request.client_request_id))) return;
        if (!entry) {
          if (Array.from(catalogPointRequests.values()).some(previous => previous.productIds.some(id => productIds.includes(id)))) {
            throw Error('有商品的卖点任务尚待核实。请用原报告的「核实卖点任务」。');
          }
          if (Array.from(state.runs.values()).some(run => {
            const action = run.input?.view_context?.selling_points_action;
            return !TERMINAL.has(run.status) && ['generate', 'generate_and_save'].includes(action)
              && !(action === 'generate' && run.status === 'WAITING_USER')
              && run.input.observed_versions?.some(ref => productIds.includes(ref.id));
          })) {
            throw Error('已有卖点任务在处理这些商品，请在对话中查看进度。');
          }
          const records = await Promise.all(items.map(readCatalogProduct));
          if (state.destroyed || generation !== state.generation) return;
          const filled = records.filter(({ latest }) => latest.body.core_selling_points?.some(point =>
            typeof point === 'string' && point.trim()) || latest.body.selling_points?.trim());
          if (filled.length) {
            await Promise.all(filled.map(({ latest }) => refreshCatalogRequirement(latest.ref.id)));
            throw Error('有商品已填写核心卖点，请核对更新后的入库报告再继续。');
          }
          const refs = records.map(({ latest }) => latest.ref);
          const selection = packSelection(records.map(({ latest, destination }) => ({
            label: '商品库 / ' + latest.body.name + ' / 核心卖点',
            target: { object_ref: latest.ref, field_path: '/core_selling_points' },
            destination: { ...destination, field_path: '/core_selling_points' }
          })));
          const text = [
            '请为选中的商品生成并保存核心卖点。只补齐尚未填写的核心卖点，不覆盖已有内容，不生成直播话术或语音。',
            '优先复用已经验收且适用于所选商品的顾客研究。需要新研究时，以同一系列共同的顾客、使用场景、需求和顾虑分析一次，款式只补充影响选购的差异。不同系列分别分析。研究任务只绑定本次选中的商品，未选商品只能参考事实，不能纳入研究任务目标或写入。',
            '研究结果按现有流程验收，引用逐款核对商品及版本。卖点以系列共有的购买理由为主，每条都要有当前款的商品事实作为依据。当前款有明确差异时，最多补一条款式专属卖点。按所选顺序逐款使用现有卖点流程，按这次按钮授权生成并保存。'
          ].join('\n');
          if (options.beforeSend && await options.beforeSend({ text, selection, conversation, source: 'catalog_points' }) === false) return;
          if (state.destroyed || generation !== state.generation) return;
          const request = { scope: conversation.scope, text, material_refs: [], selection,
            view_context: { view: 'products', product_id: refs.length === 1 ? refs[0].id : null,
              room_id: null, preparation_id: null, selling_points_action: 'generate_and_save' },
            intent_mode: 'new_task', related_run_id: null, observed_versions: refs, client_request_id: uuid() };
          entry = { conversationId, reportRunId, productIds, request };
          catalogPointRequests.set(request.client_request_id, entry);
          saveCatalogPointRequests();
        }
        const request = entry.request;
        submittingCatalogPoints = request;
        announceState();
        let receipt;
        try {
          receipt = await api('/conversations/' + encodeURIComponent(conversationId) + '/messages', { method: 'POST', body: request });
          if (!(receipt.run?.run_id || receipt.run?.id || receipt.run_id)) throw Error('服务未返回任务编号。');
        } catch (error) {
          if (!retry && error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
            catalogPointRequests.delete(request.client_request_id);
            saveCatalogPointRequests();
            throw error;
          }
          throw Error('暂未确认卖点任务是否已受理。点击「核实卖点任务」可用同一请求继续核实。');
        }
        catalogPointRequests.delete(request.client_request_id);
        saveCatalogPointRequests();
        if (state.destroyed || generation !== state.generation) return;
        const run = receipt.run || { run_id: receipt.run_id };
        const id = run.run_id || run.id;
        const message = receipt.message || { message_id: request.client_request_id, role: 'user', text: request.text, run_id: id };
        if (!state.messages.some(value => value.message_id === message.message_id)) state.messages.push(message);
        if (!state.runs.has(id)) addRun({ input: request, ...run });
        const olderPreview = pendingQuestions().find(({ key, q, run: owner }) => key === activeDecision
          && owner.status === 'WAITING_USER' && owner.input?.view_context?.selling_points_action === 'generate'
          && q.kind === 'selling_points_confirmation' && entry.productIds.includes(q.preview?.product_ref?.id));
        if (olderPreview) {
          seenDecisions.add(olderPreview.key);
          activeDecision = null;
        }
        submittingCatalogPoints = null;
        if (retry) reconciledCatalogPointRunId = id;
        renderConversation();
        announceState();
        reconciledCatalogPointRunId = null;
        await pollRuns(generation, true);
      } catch (error) {
        if (!state.destroyed && generation === state.generation) setError(error.message);
      } finally {
        submittingCatalogPoints = null;
        reconciledCatalogPointRunId = null;
        state.sending = false;
        setBusy();
        if (!state.destroyed && generation === state.generation) renderConversation();
        announceState();
      }
    }
    function catalogRequirementChoices(item, key, runId) {
      const row = node('div', 'sb-a-catalog-required-actions');
      const deferred = catalogDeferrals.has(key);
      const labeled = (label, action) => {
        const control = button(label, action);
        control.setAttribute('aria-label', label + ' · ' + item.name);
        return control;
      };
      function changeDeferral(value) {
        if (value) catalogDeferrals.add(key);
        else catalogDeferrals.delete(key);
        writeStore(storage, prefix + ':catalog-deferrals', Array.from(catalogDeferrals));
        const next = catalogRequirementChoices(item, key, runId);
        row.replaceWith(next);
        next.querySelector('button')?.focus({ preventScroll: true });
      }
      if (deferred) {
        row.append(node('p', '', '已选择稍后填写，核心卖点仍待补。'),
          labeled('查看填写选项', () => changeDeferral(false)));
      } else {
        row.append(labeled('生成并保存核心卖点', () => submitCatalogSellingPoints([item], runId)),
          labeled('自己填写', () => openCatalogSellingEditor(item)),
          labeled('稍后去商品库填写', () => changeDeferral(true)));
      }
      return row;
    }
    function catalogIntakeReport(products, runId) {
      const report = node('section', 'sb-a-catalog-report');
      report.setAttribute('aria-label', '商品入库报告');
      const incompleteCount = products.filter(item => item.missing_required?.length).length;
      const summary = '已保存 ' + products.length + ' 件商品' +
        (incompleteCount ? ' · ' + incompleteCount + ' 件待补必填信息' : '');
      report.append(node('h4', '', '商品入库报告'), node('p', 'sb-a-catalog-caption', summary));
      const missingPoints = products.filter(item => item.required_actions?.some(action => action.id === 'core_selling_points'));
      if (missingPoints.length > 1) {
        const batch = node('div', 'sb-a-catalog-required-actions');
        batch.append(button('一起生成核心卖点', () => submitCatalogSellingPoints(missingPoints, runId), 'sb-a-confirm'),
          node('p', 'sb-a-catalog-caption', '点击即为 ' + missingPoints.length + ' 件待补商品生成并保存核心卖点。'));
        report.append(batch);
      }
      for (const entry of catalogPointRequests.values()) {
        if (entry.reportRunId === runId && entry.conversationId === state.conversation?.conversation_id) {
          const retry = button('核实卖点任务', () => submitCatalogSellingPoints([], runId, entry));
          retry.disabled = state.sending;
          report.append(retry);
        }
      }
      for (const item of products) {
        const product = node('article', 'sb-a-catalog-product');
        product.append(node('h5', '', item.name));
        if (item.sku_id) product.append(node('p', 'sb-a-catalog-caption', '规格编号 ' + item.sku_id));

        const configured = node('section', 'sb-a-catalog-group sb-a-catalog-group--configured');
        const groups = (item.configured_groups || []).filter(group => group.items?.length);
        const configuredCount = groups.reduce((count, group) => count + group.items.length, 0);
        configured.append(node('h6', '', '已配置 ' + configuredCount + ' 项'));
        if (configuredCount) {
          const fields = node('dl', 'sb-a-catalog-fields');
          for (const group of groups) {
            fields.append(node('dt', '', group.label + ' · ' + group.items.length + ' 项'), node('dd', '', group.items.join('、')));
          }
          configured.append(fields);
        }

        const required = node('section', 'sb-a-catalog-group sb-a-catalog-group--required');
        const missing = item.missing_required || [];
        const actions = item.required_actions || [];
        required.append(node('h6', 'sb-a-catalog-readiness', missing.length ? '必填信息待补 ' + missing.length + ' 项' : '必填信息齐全'));
        if (missing.length) {
          required.dataset.incomplete = 'true';
          const remaining = missing.filter(label => !actions.some(action => action.label === label));
          if (remaining.length) required.append(node('p', '', remaining.join('、')));
          for (const action of actions) {
            const entry = node('div', 'sb-a-catalog-requirement');
            entry.append(node('strong', '', action.label));
            if (action.reason) entry.append(node('p', '', action.reason));
            if (action.id === 'core_selling_points') entry.append(catalogRequirementChoices(item, JSON.stringify([runId, item.product_id, action.id]), runId));
            required.append(entry);
          }
        }

        const optional = node('section', 'sb-a-catalog-group sb-a-catalog-group--optional');
        optional.append(node('h6', '', '选填建议'));
        const recommendations = item.recommendations || [];
        if (recommendations.length) {
          const suggestions = node('ul', 'sb-a-catalog-recommendations');
          for (const entry of recommendations) {
            const row = node('li', '');
            row.append(node('strong', '', entry.label));
            if (entry.reason) row.append(node('p', '', entry.reason));
            suggestions.append(row);
          }
          optional.append(suggestions);
        } else optional.append(node('p', '', '暂无待补的选填建议'));
        product.append(configured, required, optional);

        const ref = item.product_ref?.kind === 'product' && item.product_ref.id === item.product_id ? item.product_ref : null;
        const open = artifactCard({ label: '打开商品详情', destination: {
          authority: 'voice', view: 'products', product_id: item.product_id, preparation_id: null, room_id: null,
          ...(ref ? { object_ref: ref } : {})
        } });
        open.setAttribute('aria-label', '打开商品详情 · ' + item.name);
        product.append(open);
        report.append(product);
      }
      report.append(node('p', 'sb-a-catalog-caption', '选填资料可在左侧商品知识库补充，缺少它们不影响本次入库，也不会单独阻止开播。补充后可让直播讲解和顾客问答更完整。'));
      return report;
    }
    function sellingPointsPreview(q) {
      const preview = q.preview || {}; const wrap = node('section', 'sb-a-benefits');
      wrap.setAttribute('aria-label', '待确认的核心卖点');
      if (preview.product_name) wrap.append(node('p', 'sb-a-benefit-product', decisionText(preview.product_name)));
      const items = Array.isArray(preview.items) ? preview.items : [];
      items.forEach((item, index) => {
        const card = node('article', 'sb-a-benefit');
        card.append(node('span', 'sb-a-benefit-number', String(index + 1).padStart(2, '0')), node('p', 'sb-a-benefit-copy', decisionText(item.selling_point || '卖点内容待补充')));
        const key = q.decision_id + ':benefit:' + index; const basis = node('details', 'sb-a-benefit-basis');
        basis.open = state.openDetails.has(key);
        basis.addEventListener('toggle', () => basis.open ? state.openDetails.add(key) : state.openDetails.delete(key));
        basis.append(node('summary', '', '顾客场景与商品依据'));
        if (item.audience_need) safeAppendText(basis, '顾客需求：' + decisionText(item.audience_need));
        if (item.use_scene) safeAppendText(basis, '使用场景：' + decisionText(item.use_scene));
        const evidence = Array.isArray(item.evidence) ? item.evidence : [];
        if (evidence.length) {
          const list = node('ul', 'sb-a-benefit-evidence');
          evidence.forEach(value => {
            const entry = node('li', '', decisionText(typeof value === 'string' ? value : value.fact || '商品依据'));
            const ref = value && value.source_ref;
            const label = decisionText(typeof ref === 'string' ? ref : ref && (ref.label || ref.title));
            if (label) entry.append(node('small', '', '来源：' + label));
            list.append(entry);
          });
          basis.append(list);
        }
        if (item.limitations && (!Array.isArray(item.limitations) || item.limitations.length)) safeAppendText(basis, '适用边界：' + decisionText(Array.isArray(item.limitations) ? item.limitations.join('；') : item.limitations));
        card.append(basis); wrap.append(card);
      });
      return wrap;
    }
    function pendingQuestions() {
      return Array.from(state.runs.values()).reverse()
        .flatMap(run => openQuestions(run).map(q => ({ q, run, key: decisionKey(q) })))
        .filter(entry => !answeredDecisions.has(entry.key));
    }
    function questionTitle(q) { return global.ShengboDecisionCopy?.title(q) || decisionText(q.title) || '需要你确认'; }
    function hideDecision() {
      for (const entry of pendingQuestions()) seenDecisions.add(entry.key);
      activeDecision = null;
      renderDecisionPanel();
    }
    function renderDecisionPanel() {
      const pending = pendingQuestions();
      decisionBar.hidden = !pending.length;
      const label = pending.length ? pending.length + ' 项需要你确认' : '';
      if (decisionLabel.textContent !== label) decisionLabel.textContent = label;
      let current = showWelcome ? null : pending.find(entry => entry.key === activeDecision);
      if (!current && !showWelcome) current = pending.find(entry => !seenDecisions.has(entry.key));
      activeDecision = current?.key || null;
      if (!current) {
        if (decisionDialog.open) {
          const focusInside = decisionDialog.contains(doc.activeElement);
          decisionDialog.close();
          if (focusInside) (pending.length ? showDecisions : focusBeforeDecision?.isConnected ? focusBeforeDecision : input).focus({ preventScroll: true });
          focusBeforeDecision = null;
        }
        if (decisionSignature) { decisionSignature = ''; decisionBody.replaceChildren(); decisionPicker.replaceChildren(); }
        return;
      }
      seenDecisions.add(current.key);
      const { q, run, key } = current;
      const signature = JSON.stringify([q, pending.map(({ q, key }) => [key, questionTitle(q)]),
        state.pendingDecisions.has(key), decisionErrors.get(key)]);
      if (signature !== decisionSignature) {
        const active = doc.activeElement;
        const editing = active?.dataset.questionDraft === key;
        const selection = editing ? [active.selectionStart, active.selectionEnd] : null;
        const focusInside = decisionBody.contains(active) || decisionPicker.contains(active);
        decisionSignature = signature;
        decisionTitle.textContent = questionTitle(q);
        decisionCount.textContent = '人工确认 · ' + (pending.findIndex(entry => entry.key === key) + 1) + ' / ' + pending.length;
        decisionPicker.replaceChildren();
        decisionPicker.hidden = pending.length < 2;
        if (pending.length > 1) {
          const list = node('select', '', ''); list.setAttribute('aria-label', '待确认事项');
          pending.forEach((entry, index) => {
            const option = node('option', '', (index + 1) + ' · ' + questionTitle(entry.q)); option.value = entry.key; list.append(option);
          });
          list.value = key;
          list.addEventListener('change', () => { activeDecision = list.value; renderDecisionPanel(); decisionTitle.focus(); });
          decisionPicker.append(list);
        }
        decisionBody.replaceChildren(renderQuestion(q, run));
        if (editing) {
          const restored = decisionBody.querySelector('[data-question-draft]');
          if (restored && !restored.disabled) { restored.focus({ preventScroll: true }); restored.setSelectionRange(...selection); }
        } else if (focusInside) decisionTitle.focus({ preventScroll: true });
      }
      if (!decisionDialog.open) {
        if (auditDialog.open) auditDialog.close();
        focusBeforeDecision = doc.activeElement;
        decisionDialog.show();
        if (focusBeforeDecision && focusBeforeDecision !== doc.body) focusBeforeDecision.focus({ preventScroll: true });
      }
    }
    function questionError(q, message) {
      decisionErrors.set(decisionKey(q), message);
      const alert = decisionBody.querySelector('.sb-a-decision-error');
      if (alert) { alert.hidden = !message; alert.textContent = decisionText(message); }
    }
    function renderQuestion(q, run) {
      const generation = q.kind === 'selling_points_generation'; const confirmation = q.kind === 'selling_points_confirmation';
      const recovering = q.kind === 'runtime_recovery';
      const key = decisionKey(q);
      const saving = state.pendingDecisions.has(key);
      const card = node('article', 'sb-a-question'); card.dataset.decisionId = q.decision_id;
      if (generation || confirmation) card.classList.add('sb-a-question-benefits');
      if (recovering) card.append(node('p', 'sb-a-decision-catalog-pending', '原任务已保留，答复后继续当前步骤'));
      if (q.kind === 'catalog_intake_price') card.classList.add('sb-a-question-required');
      if (['catalog_confirm', 'catalog_intake_approval', 'catalog_intake_price', 'new_product_import'].includes(q.kind)) {
        const updating = run.input?.view_context?.product_material_action === 'update';
        card.append(node('p', 'sb-a-decision-catalog-pending', updating ? '待确认，尚未保存本次修改' : '待确认，尚未入库'));
      }
      const prompt = decisionText(q.prompt || q.question) || '请检查这项决定。';
      card.append(node('p', 'sb-a-decision-prompt', prompt));
      const summary = decisionText(q.agent_explanation || q.summary);
      if (summary && summary !== prompt) safeAppendText(card, summary);
      if (confirmation && q.preview) card.append(sellingPointsPreview(q));
      else if (generation && q.preview?.product_name) card.append(node('p', 'sb-a-benefit-product', '商品：' + decisionText(q.preview.product_name)));
      else {
        const preview = global.ShengboDecisionCopy?.preview(q);
        if (preview?.rows?.length) {
          const list = node('dl', 'sb-a-decision-preview');
          preview.rows.forEach(({ label, text }) => list.append(node('dt', '', label), node('dd', '', text)));
          card.append(node('strong', '', preview.title), list);
        }
      }
      const rawChoices = q.options ?? q.choices ?? [];
      const suppliedChoices = Array.isArray(rawChoices) ? rawChoices : [null];
      const choices = suppliedChoices.map(item => {
        if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
        let choice = item;
        // Older servers may persist a model-authored mapping instead of a labelled option.
        if (q.kind === 'catalog_confirm' && !q.human_action && !item.requires_human_action && !item.human_action) {
          const entries = Object.entries(item);
          if (entries.length === 1) {
            const [[value, label]] = entries;
            if (/^[a-z][a-z0-9_.:-]{0,99}$/i.test(value)
                && !['id', 'value', 'choice_id', 'label', 'title', 'text', 'description', 'detail', 'option'].includes(value)) {
              choice = { value, label };
            }
          }
        }
        const value = choice.value ?? choice.choice_id ?? choice.id;
        const label = global.ShengboDecisionCopy
          ? global.ShengboDecisionCopy.option(choice) : decisionText(choice.label || choice.title);
        return typeof value === 'string' && value.trim() && label ? { ...choice, value, label } : null;
      });
      const ambiguous = choices.some(item => !item)
        || new Set(choices.map(item => item?.value)).size !== choices.length
        || new Set(choices.map(item => item?.label)).size !== choices.length;
      const row = node('div', 'sb-a-question-actions');
      const revision = q.revision_text;
      const protectedChoice = suppliedChoices.some(item => item?.requires_human_action || item?.human_action) || !!q.human_action;
      const free = q.allow_text !== false && (!protectedChoice || q.allow_text === true)
        && (!suppliedChoices.length || q.allow_text === true || ['edit_text', 'catalog_confirm'].includes(q.kind));
      const draft = () => (state.questionDrafts.get(key) || '').trim();
      let answerInput;
      const affectedButtons = [];
      if (!ambiguous) choices.forEach(item => {
        const val = item.value;
        const approval = item.requires_human_action || !confirmation && /approve|audio_approval/.test(q.kind || '') && /approve|confirm/.test(String(val));
        const action = item.human_action || (approval ? q.human_action : null);
        const unavailable = approval && !action;
        const revise = confirmation && val === 'revise';
        const b = button(item.label, () => {
          if (revise && !draft()) { questionError(q, '请先填写希望怎样修改。'); answerInput?.focus(); return; }
          const answer = generation && val === 'generate' ? { choice: 'generate', text: draft() } : confirmation && val === 'revise' ? { choice: 'revise', text: draft() } : val;
          answerQuestion(q, run, answer, action);
        }, approval || (confirmation && val === 'approve') || (generation && val === 'generate') ? 'sb-a-confirm' : '');
        b.disabled = saving || unavailable;
        if (unavailable) b.title = '这项确认目前不可提交，请重试连接。';
        if (approval || confirmation && val === 'approve' || free && !generation && !confirmation) affectedButtons.push({ b, unavailable });
        row.append(b);
      });
      if (ambiguous) card.append(node('p', '', free
        ? '选项说明不完整，请在下方写明你的要求。' : '选项说明不完整，请刷新后重试。'));
      else if (choices.length) card.append(node('strong', 'sb-a-decision-section', '选择一个方案'), row);
      if (free || revision) {
        const answer = node('textarea', 'sb-a-answer'); answerInput = answer; answer.rows = 2;
        const price = q.kind === 'catalog_intake_price';
        const inputLabel = price ? '日常售价金额' : generation ? '卖点生成方向（可选）' : confirmation ? '卖点修改意见（可选）'
          : recovering ? '补充原文位置或说明' : '补充说明或修改要求';
        if (price) answer.inputMode = 'decimal';
        answer.placeholder = decisionText(revision?.placeholder || q.input_placeholder) || '例如：先说明价格差异，整理好后再让我确认。';
        answer.setAttribute('aria-label', price || generation || confirmation || recovering ? inputLabel : '确认项补充说明');
        answer.dataset.questionDraft = key; answer.value = state.questionDrafts.get(key) || '';
        answer.disabled = saving; answer.maxLength = 6000;
        const b = button(decisionText(revision?.label) || (recovering ? '提交说明并继续' : '提交答复'), () => {
          if (draft()) answerQuestion(q, run, revision ? { choice: revision.choice, text: draft() } : { text: draft() });
        }, 'sb-a-decision-submit-text');
        function updateInputActions() {
          b.disabled = saving || !draft();
          for (const { b, unavailable } of affectedButtons) {
            b.disabled = saving || unavailable || !!draft();
            b.title = unavailable ? '这项确认目前不可提交，请重试连接。' : draft() ? '请提交已填写的答复，或清空后选择方案。' : '';
          }
        }
        answer.addEventListener('input', () => { state.questionDrafts.set(key, answer.value); questionError(q, ''); updateInputActions(); });
        updateInputActions();
        card.append(node('strong', 'sb-a-decision-section', inputLabel), answer);
        if (!confirmation) card.append(b);
        if (free && !generation && !confirmation && choices.length && !ambiguous) card.append(node('p', 'sb-a-question-hint', '可选一个方案，也可输入并提交自己的答复。'));
        if (generation) card.append(node('p', 'sb-a-question-hint', '可补充目标顾客、使用场景或表达方向；商品事实不足时会继续向你询问。'));
        if (confirmation) {
          const reviseLabel = choices.find(choice => choice?.value === 'revise')?.label;
          if (reviseLabel) card.append(node('p', 'sb-a-question-hint', '填写修改意见后，请选择「' + reviseLabel + '」。'));
        }
        if (revision) card.append(node('p', 'sb-a-question-hint', '提交修改要求会退回处理，仍需确认更新后的内容。'));
      }
      if (recovering && q.context?.can_restore_source) {
        const recoveryActions = node('div', 'sb-a-question-actions');
        const original = node('input', '', '');
        original.type = 'file';
        original.hidden = true;
        original.setAttribute('aria-label', '选择同一份原件');
        const reupload = button('重新上传原件', () => original.click());
        reupload.disabled = saving;
        original.addEventListener('change', async () => {
          const file = original.files?.[0];
          original.value = '';
          if (!file || reupload.disabled) return;
          reupload.disabled = true;
          try {
            const conversationId = run.conversation_id || state.conversation?.conversation_id;
            if (!conversationId || !run.scope) throw Error('找不到这项任务的原会话，请刷新后重试。');
            const upload = await uploadFile(file, conversationId, run.scope);
            if (!upload.material_ref) throw Error('上传未返回原件记录。');
            await answerQuestion(q, run, { text: draft() || '重新上传了同一份原件', source_ref: upload.material_ref });
          } catch (err) { questionError(q, err.message); }
          finally { reupload.disabled = false; }
        });
        recoveryActions.append(original, reupload);
        card.append(recoveryActions);
      }
      const error = node('p', 'sb-a-decision-error', decisionText(decisionErrors.get(key)));
      error.setAttribute('role', 'alert'); error.hidden = !decisionErrors.has(key) || !decisionErrors.get(key);
      card.append(error);
      if (saving) { const busy = node('p', 'sb-a-decision-busy', '正在提交你的决定…'); busy.setAttribute('role', 'status'); card.append(busy); }
      return card;
    }
    function focusRunCard(runId) {
      const card = [...transcript.querySelectorAll('.sb-a-run')].find(element => element.dataset.runId === runId);
      if (!card) return;
      card.focus({ preventScroll: true });
      card.scrollIntoView({ block: 'start' });
    }
    const IMPORT_FIELDS = { promotion: '本场活动说明', coupon: '本场优惠券', price: '活动价',
      campaign_starts_at: '活动开始时间', campaign_ends_at: '活动截止时间', scheduled_at: '计划时间', product_overrides: '本场单品优惠',
      product_ids: '适用商品', theme: '本场主题', objective: '本场目标', currency: '币种',
      scene: '直播场景', '活动说明': '本场活动说明', '优惠券': '本场优惠券',
      '直播排期': '计划开播时间', '本场主题': '本场主题', '本场目标': '本场目标',
      '直播场景': '直播场景', '活动开始时间': '活动开始时间', '活动截止时间': '活动截止时间' };
    function importFieldName(value) {
      if (typeof value !== 'string') return '配置项';
      if (/^单品活动价(?:\s|$)/.test(value)) return '单品活动价';
      return IMPORT_FIELDS[value.split(/[./]/).at(-1)] || '配置项';
    }
    function importMissingText(value) {
      const label = typeof value === 'string' ? value : value?.message || value?.summary || value?.field;
      return typeof label === 'string' && /[\u3400-\u9fff]/.test(label) ? safeText(label) : importFieldName(label);
    }
    function importFieldValue(value) {
      if (value == null || value === '') return '未填写';
      if (typeof value !== 'object') return safeText(value);
      if (value.coupon && typeof value.coupon === 'object') return importFieldValue(value.coupon);
      for (const key of ['promotion', 'theme', 'objective', 'scene', 'scheduled_at', 'campaign_starts_at', 'campaign_ends_at']) {
        if (typeof value[key] === 'string') return importFieldValue(value[key]);
      }
      if (value.mode === 'none') return '未设置优惠';
      if (value.mode === 'coupon') {
        const amount = value.minimum_spend == null ? '抵扣 ' + value.amount
          : '满 ' + value.minimum_spend + ' 减 ' + value.amount;
        return safeText(amount + (value.condition ? ' · ' + value.condition : ''));
      }
      if (value.mode === 'price') return safeText(value.price);
      if (value.mode === 'discount') return safeText('折扣比率 ' + value.discount);
      return '请查看本场配置中的详细内容';
    }
    function documentImportReport(run) {
      const result = run.document_import_result;
      if (!result || typeof result !== 'object') return null;
      const hasDestination = typeof result.room_id === 'string' && result.room_id
        && typeof result.preparation_id === 'string' && result.preparation_id;
      const conflicts = Array.isArray(result.conflicts) ? result.conflicts : [];
      const missing = Array.isArray(result.missing_fields) ? result.missing_fields : [];
      if (!hasDestination && !conflicts.length && !missing.length && !result.summary) return null;
      const section = node('section', 'sb-a-catalog-report sb-a-document-import-report');
      section.append(node('strong', '', '资料导入结果'));
      if (typeof result.summary === 'string') safeAppendText(section, result.summary, 'sb-a-run-summary');
      if (typeof result.room_name === 'string') safeAppendText(section, '直播间：' + safeText(result.room_name), 'sb-a-run-summary');
      if (result.activity_found === true) safeAppendText(section, '资料中识别到本场活动。', 'sb-a-run-summary');
      if (missing.length) safeAppendText(section, '尚需补充：' + missing.map(importMissingText).join('、'), 'sb-a-run-summary');
      if (conflicts.length) {
        const list = node('ul', 'sb-a-acceptance');
        for (const conflict of conflicts) {
          const label = typeof conflict === 'string' ? conflict
            : conflict?.message || conflict?.summary || conflict?.label
              || (conflict?.field ? importFieldName(conflict.field) + '：原值 ' + importFieldValue(conflict.current)
                + '；资料 ' + importFieldValue(conflict.incoming) : '有一项资料需要核对');
          list.append(node('li', '', safeText(label)));
        }
        section.append(node('p', 'sb-a-run-summary', '需要核对的资料'), list);
      }
      if (hasDestination) {
        section.append(button(run.input?.view_context?.catalog_create_room ? '打开新直播间' : '查看本场配置',
          () => openArtifact({ view: 'session', room_id: result.room_id, preparation_id: result.preparation_id,
            product_id: null, catalog_import_result: !!run.input?.view_context?.catalog_create_room }), 'sb-a-artifact'));
      }
      return section;
    }
    function runCard(run) {
      const card = node('article', 'sb-a-run');
      card.dataset.runId = run.run_id;
      card.tabIndex = -1;
      const h = node('div', 'sb-a-run-heading');
      const failureCopy = runFailureCopy(run);
      const savedCount = savedScriptCount(run);
      const unfinished = isUnfinished(run);
      const badgeText = unfinished && savedCount ? '部分话术已保存'
        : failureCopy ? '任务未完成' : runStatusName(run);
      const badge = node('span', 'sb-a-run-status', badgeText);
      badge.dataset.status = run.status;
      h.append(badge);
      if (run.run_id !== currentRun()?.run_id && TERMINAL.has(run.status)) h.append(node('small', '', '历史任务'));
      if (state.showProcess && !failureCopy) h.append(node('small', '', run.run_id));
      if (!TERMINAL.has(run.status) && !['FINALIZING', 'CANCELLING'].includes(run.status)) h.append(button('停止任务', () => cancelRun(run), 'sb-a-stop'));
      card.append(h);
      const products = catalogIntakeProducts(run);
      if (products.length) card.append(catalogIntakeReport(products, run.run_id));
      else if (openQuestions(run).length) safeAppendText(card, '请在 Agent 确认窗口处理。', 'sb-a-run-summary');
      else if (unfinished && (failureCopy || savedCount)) {
        const saved = savedCount ? '已保存 ' + savedCount + ' 版话术，任务仍有步骤未完成。' : '';
        safeAppendText(card, saved + (failureCopy || (run.summary ? safeText(run.summary) : '')), 'sb-a-run-summary');
      }
      else if (run.status === 'CANCELLED') safeAppendText(card, '任务已停止。', 'sb-a-run-summary');
      else if (run.summary) safeAppendText(card, run.summary, 'sb-a-run-summary');
      const importReport = documentImportReport(run);
      if (importReport) card.append(importReport);
      const requirements = run.acceptance && run.acceptance.requirements || []; if (requirements.length) { const list = node('ul', 'sb-a-acceptance'); for (const r of requirements) list.append(node('li', '', (r.satisfied ? '✓ ' : '○ ') + (r.label || r.requirement_id))); card.append(list); }
      const reportedIds = new Set(products.map(item => item.product_id));
      const artifacts = (run.artifacts || run.outputs || []).filter(item =>
        item.destination?.view !== 'products' || !reportedIds.has(item.destination.product_id));
      if (artifacts.length) { const nav = node('div', 'sb-a-artifacts'); artifacts.forEach(a => nav.append(artifactCard(a))); card.append(nav); }
      const relatedRun = state.runs.get(run.input?.related_run_id);
      const followups = [...state.runs.values()].filter(item => item.input?.related_run_id === run.run_id);
      if (relatedRun) card.append(button('查看接续前的任务', () => focusRunCard(relatedRun.run_id), 'sb-a-link'));
      for (const followup of followups) {
        const count = savedScriptCount(followup);
        const text = count ? '后续任务已保存 ' + count + ' 版话术'
          + (isUnfinished(followup) ? '，仍有步骤未完成。' : '。')
          : '后续任务：' + statusName(followup.status) + '。';
        const related = node('div', 'sb-a-continuation');
        related.append(node('p', '', text), button('查看后续任务', () => focusRunCard(followup.run_id), 'sb-a-link'));
        card.append(related);
      }
      for (const option of run.continuation_options || []) { const box = node('div', 'sb-a-continuation'); box.append(node('strong', '', option.label || '下一步'), node('p', '', option.summary || (option.mode === 'resume_remaining' ? '可以继续处理这项任务。' : '展开执行过程查看详情。'))); if (option.mode === 'resume_remaining') box.append(button('继续未完成部分', () => { state.relatedRunOverride = run.run_id; mode.value = 'follow_up'; input.value = '继续完成这项任务的未完成部分'; input.focus(); }, 'sb-a-link')); card.append(box); }
      if (state.showProcess) card.append(processPanel(run)); return card;
    }
    function welcomeCover() {
      const cover = node('div', 'sb-a-welcome sb-a-welcome-cover');
      cover.append(avatar('sb-a-welcome-icon'), node('p', 'sb-a-welcome-eyebrow', '直播准备工作台'),
        node('h3', '', '今天准备哪一场直播？'),
        node('p', '', '从一份商品资料、一个直播间，或一版话术开始。'));
      const actions = node('div', 'sb-a-welcome-actions');
      const entries = [
        ['catalog', '库', '导入商品知识库', '上传资料，按商品整理规格、价格和问答。'],
        ['session', '播', '准备直播间和本场配置', '选择直播间，核对选品、排期和本场优惠。'],
        ['scripts', '稿', '生成直播话术', '从已选商品和本场规则开始，生成可编辑的稿件。']
      ];
      for (const [action, icon, label, detail] of entries) {
        const item = button('', () => {
          if (options.onWelcomeAction) Promise.resolve(options.onWelcomeAction(action)).catch(error => setError(error.message));
          else { revealConversation(); input.value = label; input.focus(); }
        }, 'sb-a-welcome-action');
        const mark = node('span', 'sb-a-welcome-action-icon', icon);
        mark.setAttribute('aria-hidden', 'true');
        const words = node('span');
        words.append(node('strong', 'sb-a-welcome-action-title', label), node('span', 'sb-a-welcome-action-detail', detail));
        item.append(mark, words);
        actions.append(item);
      }
      cover.append(actions);
      if (showWelcome && state.conversation && (state.messages.length || state.runs.size)) {
        cover.append(button('继续上次对话', revealConversation, 'sb-a-welcome-return'));
      }
      return cover;
    }
    function renderConversation() {
      if (state.destroyed) return;
      renderHistory();
      const nearEnd = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 90;
      const oldScroll = transcript.scrollTop;
      const focusedId = doc.activeElement?.dataset.messageId;
      transcript.replaceChildren();
      if (showWelcome) {
        transcript.append(welcomeCover());
        transcript.scrollTop = 0;
        renderDecisionPanel();
        return;
      }
      if (!state.messages.length && !state.runs.size) {
        const batch = productMaterialBatches.get(state.conversation?.conversation_id);
        if (batch?.materials.length) {
          const empty = node('div', 'sb-a-welcome');
          const guidance = batch.compose
            ? (batch.productId ? '这批资料仅更新已选商品。' : '这批资料仅用于新增商品。需要更新已有商品时，请从该商品详情提交资料。') + '可在下方补充要求并发送。'
            : '从左侧提交商品整理任务后，执行进度会显示在这里。';
          empty.append(avatar('sb-a-welcome-icon'), node('h3', '', '已加入 ' + batch.materials.length + ' 份资料'), node('p', '', guidance));
          transcript.append(empty);
        } else {
          transcript.append(welcomeCover());
        }
      }
      const renderedRuns = new Set();
      let focusedMessage = null;
      for (const message of state.messages) {
        const role = message.role === 'user' ? 'user' : 'assistant';
        if (role === 'user' || !catalogIntakeProducts(state.runs.get(message.run_id)).length) {
          const id = messageId(message);
          const article = node('article', 'sb-a-message sb-a-message-' + role);
          if (id) {
            article.dataset.messageId = id;
            article.tabIndex = -1;
            if (id === highlightedMessageId) article.classList.add('sb-a-message-target');
            if (id === focusedId) focusedMessage = article;
          }
          article.append(node('small', '', role === 'user' ? '你' : '声波 Agent'));
          const content = message.text ?? message.content ?? '';
          safeAppendText(article, typeof content === 'string' ? content : jsonText(content), 'sb-a-message-text');
          transcript.append(article);
        }
        if (message.run_id && state.runs.has(message.run_id) && !renderedRuns.has(message.run_id)) {
          transcript.append(runCard(state.runs.get(message.run_id)));
          renderedRuns.add(message.run_id);
        }
      }
      for (const run of state.runs.values()) if (!renderedRuns.has(run.run_id)) transcript.append(runCard(run));
      transcript.scrollTop = !state.messages.length && !state.runs.size ? 0 : nearEnd ? transcript.scrollHeight : oldScroll;
      focusedMessage?.focus({ preventScroll: true });
      renderDecisionPanel();
    }
    function renderAttachments() {
      const batch = activeProductMaterialBatch();
      const script = !batch && state.scriptGeneration;
      input.placeholder = batch ? '补充这批资料的整理要求（选填）' : script
        ? '补充话术写作要求（选填）' : '告诉我这次需要准备什么…';
      input.maxLength = script ? 6000 : 16000;
      attachments.replaceChildren();
      if (script) {
        const chip = node('div', 'sb-a-attachment');
        chip.append(node('span', '', '新增话术 · ' + script.label));
        const discard = button('取消新增', () => {
          if (state.sending || state.pendingSubmission) return;
          state.scriptGeneration = null;
          renderAttachments();
          setBusy();
          announceState();
        }, 'sb-a-link');
        chip.append(discard);
        attachments.append(chip);
      }
      for (const material of batch?.materials || state.materials) {
        const chip = node('div', 'sb-a-attachment');
        chip.append(node('span', '', material.name || '资料'));
        if (!batch) chip.append(button('×', () => {
          state.materials = state.materials.filter(value => value !== material);
          renderAttachments();
        }, 'sb-a-remove'));
        attachments.append(chip);
      }
    }
    function renderSelection() {
      target.replaceChildren();const items=activeProductMaterialBatch()?[]:selectionItems(state.selected);if(items.length<20&&state.error==='一次最多选择20项，请先移除不需要的项。')setError('');target.hidden=!items.length;if(!items.length)return;
      const heading=node('div','sb-a-selection-heading');heading.append(node('strong','','已选 '+items.length+' 项'),button(items.length===1?'取消指定':'清空选择',()=>{state.selected=null;renderSelection();announceState();options.onSelectionCleared?.();},'sb-a-link'));target.append(heading);
      const tags=node('div','sb-a-selection-tags');items.forEach((item,index)=>{const chip=node('div','sb-a-selection-chip');const label=item.label||item.target?.field_path||item.target?.module_key||'已指定对象';const name=button(label,()=>{if(item.destination&&options.onNavigate)options.onNavigate(item.destination);},'sb-a-selection-name');name.title='定位到 '+label;const remove=button('×',()=>{const next=selectionItems(state.selected).filter((_,i)=>i!==index);state.selected=packSelection(next);renderSelection();announceState();},'sb-a-selection-remove');remove.setAttribute('aria-label','移除 '+label);chip.append(name,remove);tags.append(chip);});target.append(tags);
    }
    function toggleSelection(value,{focus=false}={}) { if(!value?.target?.object_ref)return;const items=selectionItems(state.selected).map(v=>structuredClone(v));const key=selectionKey(value),index=items.findIndex(v=>selectionKey(v)===key);if(index>=0){if(JSON.stringify(items[index].target.object_ref)===JSON.stringify(value.target.object_ref))items.splice(index,1);else items[index]=structuredClone(value);}else{if(items.length>=20){setError('一次最多选择20项，请先移除不需要的项。');return;}items.push(structuredClone(value));}state.selected=packSelection(items);renderSelection();announceState();if(focus)input.focus(); }
    async function sendMessage(e) {
      if (e) e.preventDefault();
      if (preflighting || state.sending || state.uploading || state.hydrating || state.creatingConversation) return;
      const entry = activeProductMaterialBatch();
      if (entry) {
        const conversationId = entry.conversation.conversation_id;
        const instructions = entry.pending ? entry.composedText : input.value;
        try {
          await submitProductMaterials({batch:{conversationId,version:entry.version},
            product:entry.composeProduct, instructions});
        } catch(error) {
          setError(error.unknown ? '尚未确认任务是否已受理。请再次点击「发送」核实同一任务。' : error.message);
        }
        return;
      }
      preflighting = true;
      revealConversation();
      try { return await performSendMessage(); } finally { preflighting = false; setBusy(); }
    }
    async function performSendMessage(e) { if (e) e.preventDefault(); const notes = input.value.trim(); const script = state.scriptGeneration;
      const text = script ? script.text + (notes ? '\n\n补充写作要求：\n' + notes : '') : notes;
      if (script && state.selected) { setError('新增话术使用上方商品范围，请先取消单独指定的内容。'); return; }
      if ((!text && !state.materials.length) || state.sending || state.uploading || state.preparingTask || state.hydrating || state.creatingConversation) return; if (options.beforeSend) { send.disabled = true; try { const checked = await options.beforeSend({ text, selection: state.selected ? structuredClone(state.selected) : null, conversation: state.conversation ? structuredClone(state.conversation) : null }); if (checked === false) return; if (checked && Object.prototype.hasOwnProperty.call(checked, 'selection')) { state.selected = checked.selection ? structuredClone(checked.selection) : null; renderSelection(); } } catch(err) { setError(err.message); return; } } if (!state.conversation) { const preservedSelection = state.selected; await createConversation(); state.selected = preservedSelection; renderSelection(); } if (!state.conversation) return; const active = currentRun(); if (mode.value !== 'new_task' && !active) { setError('当前没有可接续的任务，请选择“新任务”'); return; } state.sending = true; setBusy(); setError('');
      let request = { scope: state.conversation.scope || { kind: state.conversation.preparation_id ? 'preparation' : 'catalog', account_id: 'local', ...(state.conversation.preparation_id ? { preparation_id: state.conversation.preparation_id } : {}) }, text, material_refs: state.materials.map(x => x.material_ref || x.ref || x), selection: state.selected, view_context: viewContext(), intent_mode: mode.value, related_run_id: mode.value === 'new_task' ? null : state.relatedRunOverride || active.run_id, observed_versions: options.getObservedVersions ? structuredClone(options.getObservedVersions(state.selected) || []) : [] };
      if (script) {
        request.script_generation = { ...script.request, ...(notes ? { writing_notes: notes } : {}) };
      }
      delete request.view_context.selling_points_action;
      // Navigating the workspace does not turn an uncertain transport retry into
      // a new authorized task. Reuse the entire frozen request, not only its ID.
      const { view_context: ignoredViewContext, ...intentFields } = request;
      const signature = JSON.stringify(intentFields); const repeat = state.pendingSubmission && state.pendingSubmission.signature === signature;
      const requestId = repeat ? state.pendingSubmission.requestId : uuid(); request = repeat ? structuredClone(state.pendingSubmission.request) : { ...request, client_request_id: requestId }; state.pendingSubmission = { signature, requestId, request: structuredClone(request) }; announceState();
      try { const receipt = await api('/conversations/' + encodeURIComponent(state.conversation.conversation_id) + '/messages', { method: 'POST', body: request }); const message = receipt.message || { message_id: requestId, role: 'user', text, run_id: receipt.run_id || receipt.run && receipt.run.run_id }; if (!state.messages.some(x => x.message_id === message.message_id)) state.messages.push(message); if (receipt.run) addRun(receipt.run); else if (receipt.run_id) addRun(receipt); state.pendingSubmission = null; state.relatedRunOverride = null; state.scriptGeneration = null; input.value = ''; state.materials = []; renderAttachments(); renderConversation(); await pollRuns(state.generation, true); } catch (err) { setError('尚未确认任务已受理：' + err.message + '。输入已保留，再次发送同一要求会复用请求编号。'); } finally { state.sending = false; setBusy(); announceState(); } }
    async function answerQuestion(q, run, answer, humanAction) {
      const key = decisionKey(q);
      if (state.pendingDecisions.has(key)
          || !pendingQuestions().some(entry => entry.run.run_id === run.run_id && entry.key === key)) return;
      const generation = state.generation;
      const signature = JSON.stringify([key, answer, humanAction || null]);
      let request = decisionRequests.get(signature);
      if (!request) {
        request = { question_revision: q.revision ?? q.question_revision, decision_revision: q.revision ?? q.question_revision,
          answer, client_request_id: uuid(), operation_id: uuid(), human_receipt: null };
        decisionRequests.set(signature, request);
      }
      state.pendingDecisions.add(key);
      decisionErrors.delete(key);
      renderDecisionPanel();
      try {
        if (humanAction && !request.human_receipt) {
          let receipt;
          if (humanAction.kind === 'product_identity') {
            const { target_name, candidate_refs } = humanAction.body || {};
            if (typeof target_name !== 'string' || !Array.isArray(candidate_refs)) throw Error('商品身份确认信息不完整。');
            receipt = await api('/api/prelive/v2/human/product-identity', { method: 'POST', body: { target_name, candidate_refs } });
          } else if (humanAction.kind === 'verify_existing_receipt') {
            if (typeof humanAction.operation_id !== 'string' || !humanAction.operation_id) throw Error('缺少此前的确认记录。');
            receipt = { operation_id: humanAction.operation_id };
          } else {
            if (humanAction.kind && humanAction.kind !== 'business_command' || !humanAction.command || typeof humanAction.command !== 'object')
              throw Error('这项审批信息不完整。');
            receipt = await api('/api/prelive/v2/commands', { method: 'POST', body: humanAction.command });
          }
          request.human_receipt = receipt.receipt_ref || receipt;
        }
        const receipt = await api('/decisions/' + encodeURIComponent(q.decision_id) + '/answers', { method: 'POST', body: request });
        answeredDecisions.add(key);
        decisionRequests.delete(signature);
        state.questionDrafts.delete(key);
        if (generation !== state.generation) return;
        if (receipt.run) absorbRun(receipt.run);
        else {
          const latest = await api('/runs/' + encodeURIComponent(run.run_id));
          if (generation !== state.generation) return;
          absorbRun(latest.run || latest);
        }
        renderConversation();
        await pollRuns(generation, true);
      } catch (e) {
        if (generation === state.generation) questionError(q, '这项确认未完成。' + e.message);
      } finally {
        state.pendingDecisions.delete(key);
        if (generation === state.generation && !state.destroyed) renderDecisionPanel();
      }
    }
    async function cancelRun(run) { try { await api('/runs/' + encodeURIComponent(run.run_id) + '/cancel', { method: 'POST', body: { operation_id: uuid(), client_request_id: uuid(), expected_run_version: run.version ?? null } }); await pollRuns(state.generation, true); } catch (e) { setError('未能确认任务已停止：' + e.message); } }
    async function uploadFile(file, conversationId, scope) {
      if (file.size > (/\.xlsx$/i.test(file.name) ? 128 : 10) * 1024 * 1024) {
        throw Error(file.name + ' 超过本格式的资料大小限制（Excel 128 MiB，其余10 MiB）');
      }
      const data = new FormData();
      data.append('file', file);
      data.append('conversation_id', conversationId);
      data.append('scope', JSON.stringify(scope));
      data.append('client_request_id', uuid());
      const material = await api('/materials', { method: 'POST', body: data, multipart: true });
      return { ...material, name: material.name || file.name };
    }
    async function uploadFiles(providedFiles) {
      if (activeProductMaterialBatch()) return;
      if (preflighting || state.sending || state.uploading || state.preparingTask || state.hydrating || state.creatingConversation) {
        if (Array.isArray(providedFiles)) throw Error('助手正在处理当前操作，请稍后上传资料。');
        upload.value = '';
        return;
      }
      const files = Array.isArray(providedFiles) ? providedFiles : Array.from(upload.files || []);
      upload.value = '';
      if (!files.length) return;
      if (!state.conversation) await createConversation();
      if (!state.conversation) return;
      state.uploading = true;
      setBusy();
      try {
        for (const file of files) {
          state.materials.push(await uploadFile(file, state.conversation.conversation_id, state.conversation.scope || scopeNow()));
          renderAttachments();
          announceState();
        }
        setError('');
      } catch (error) {
        setError('资料上传未全部完成：' + error.message);
        if (Array.isArray(providedFiles)) throw error;
      } finally { state.uploading = false; setBusy(); announceState(); }
      return structuredClone(state.materials);
    }
    function activeProductMaterialBatch() {
      const entry = productMaterialBatches.get(state.conversation?.conversation_id);
      return entry?.compose && !entry.receipt ? entry : null;
    }
    function checkProductMaterialAction() {
      if (state.destroyed) throw Error('对话界面已关闭，请刷新后重试。');
      if (preflighting || state.sending || state.uploading || state.preparingTask || state.hydrating || state.creatingConversation) {
        throw Error('助手正在处理当前操作，请稍后重试。');
      }
    }
    function productMaterialTarget(product) {
      if (product === null) return null;
      const ref = product?.ref;
      if (ref?.kind !== 'product' || !ref.id || !Number.isInteger(ref.revision) || !ref.content_hash || !product.name) {
        throw Error('无法核实要更新的商品版本，请刷新商品资料。');
      }
      return ref.id;
    }
    function roomMaterialTarget(room) {
      if (!room) return null;
      const { room_id, preparation_id, preparation_ref, room_name } = room;
      if (!room_id || !preparation_id || preparation_ref?.kind !== 'preparation'
          || preparation_ref.id !== preparation_id || !Number.isInteger(preparation_ref.revision)
          || !preparation_ref.content_hash) throw Error('无法核实本场版本，请刷新后再导入资料。');
      return { room_id, preparation_id, preparation_ref: structuredClone(preparation_ref), room_name };
    }
    function readProductMaterialBatch(batch, productId, room) {
      const entry = productMaterialBatches.get(batch?.conversationId);
      if (!entry || entry.productId !== productId || entry.version !== batch.version
          || (entry.room?.room_id || null) !== (room?.room_id || null)
          || (entry.room?.preparation_id || null) !== (room?.preparation_id || null)) {
        throw Error('这批资料与当前目标或上传记录不一致，请核对后再操作。');
      }
      return entry;
    }
    async function uploadProductMaterials({ files, product = null, batch = null, compose = false, room = null }) {
      checkProductMaterialAction();
      const productId = productMaterialTarget(product);
      const roomTarget = roomMaterialTarget(room);
      if (roomTarget && (product || compose)) throw Error('本场资料必须从本场导入入口提交。');
      let entry = batch ? readProductMaterialBatch(batch, productId, roomTarget) : null;
      if (!files?.length && !(compose && entry?.materialRefs.length && !entry.failed?.length)) throw Error('请先选择文件。');
      if (entry?.pending) throw Error('这批资料的任务正在核实，请先重试原提交。');
      if (entry?.receipt) throw Error('这批资料已提交，请为下一项任务重新上传。');
      const pendingFiles = (files || []).filter(file => !entry?.files.has(file));
      if ((entry?.materials.length || 0) + pendingFiles.length > 64) throw Error('每次最多整理 64 份资料。');
      state.uploading = true;
      setBusy();
      try {
        const newConversation = !entry;
        if (newConversation) {
          if (!state.conversation && (input.value.trim() || state.materials.length || state.selected)) {
            const draft = composerDraft();
            const previous = await requestConversation(viewContext());
            state.drafts.set(previous.conversation_id, draft);
            await loadConversation(previous.conversation_id, previous);
          }
          const title = roomTarget ? '本场资料导入' + (roomTarget.room_name ? ' · ' + roomTarget.room_name : '')
            : product ? '资料更新 · ' + product.name : '资料新建商品';
          const conversation = await requestConversation(roomTarget || {}, title);
          const scopeMatches = roomTarget
            ? conversation.scope?.kind === 'preparation' && conversation.scope.preparation_id === roomTarget.preparation_id
              && conversation.preparation_id === roomTarget.preparation_id && conversation.room_id === roomTarget.room_id
            : conversation.scope?.kind === 'catalog' && !conversation.room_id && !conversation.preparation_id;
          if (!scopeMatches) throw Error('未能建立绑定正确范围的资料对话，上传尚未开始。');
          entry = { conversation, productId, room: roomTarget, version: 0, materials: [], materialRefs: [], files: new WeakSet() };
          productMaterialBatches.set(conversation.conversation_id, entry);
        }
        const conversationId = entry.conversation.conversation_id;
        const failed = [];
        for (const file of pendingFiles) {
          try {
            const material = await uploadFile(file, conversationId, entry.conversation.scope);
            const ref = material.material_ref || material.ref;
            if (ref?.kind !== 'source_manifest' || !ref.id) throw Error('未能确认文件的上传回执。');
            entry.files.add(file);
            entry.materials.push(material);
            entry.materialRefs.push(ref);
          } catch (error) { failed.push({ name: file.name, message: error.message }); }
        }
        entry.failed = failed;
        if (compose && !failed.length && entry.materialRefs.length) {
          entry.compose = true;
          entry.composeProduct = structuredClone(product);
        }
        entry.version++;
        if (entry.materials.length && state.conversation?.conversation_id !== conversationId) {
          await loadConversation(conversationId, newConversation ? entry.conversation : undefined);
        } else if (state.conversation?.conversation_id === conversationId) {
          renderContext(); renderSelection(); renderAttachments(); renderConversation();
        }
        return structuredClone({ conversationId, productId, version: entry.version,
          materials: entry.materials, materialRefs: entry.materialRefs, failed,
          composeAttached: entry.compose && state.conversation?.conversation_id === conversationId });
      } finally { state.uploading = false; setBusy(); announceState(); }
    }
    async function submitProductMaterials({ batch, product = null, instructions = '', room = null }) {
      checkProductMaterialAction();
      const productId = productMaterialTarget(product);
      const entry = readProductMaterialBatch(batch, productId, roomMaterialTarget(room));
      if (entry.receipt) return structuredClone(entry.receipt);
      if (!entry.materialRefs.length || entry.failed?.length) throw Error('请先完成本批资料上传。');
      const conversationId = entry.conversation.conversation_id;
      const retry = !!entry.pending;
      state.sending = true;
      submittingProductBatch = entry;
      setBusy();
      try {
        if (!retry) {
          const destination = entry.room
            ? { view: 'session', room_id: entry.room.room_id, preparation_id: entry.room.preparation_id }
            : { view: 'products', product_id: productId, room_id: null, preparation_id: null };
          const baseText = entry.room
            ? '请导入这批资料到当前本场。商品资料归入共享商品库并关联本场，活动资料只保存到当前本场配置。'
            : product
            ? '请根据本次上传资料，只更新商品库中的「' + product.name + '」。先核对资料与当前商品身份，仅保存有明确依据的字段。遇到身份不一致、事实冲突或不明确的信息请先问我，禁止创建新商品或修改其他商品。'
            : '请根据本次上传资料核对商品身份，并新增资料能够支持的新商品。若可能与已有商品相同、资料涉及多个难以区分的商品，或关键事实不明确，请先向我确认。禁止修改已有商品。本次只整理并保存基础商品资料（基础信息、规格、商品知识库和常见问答），暂不开展顾客研究，不生成直播话术或语音。';
          const note = entry.room ? '本场导入补充说明：' : '在上述商品范围内的补充要求：';
          const text = instructions.trim() ? baseText + '\n\n' + note + instructions.trim() : baseText;
          let selection = product ? { label: '商品库 / ' + product.name, target: { object_ref: product.ref }, destination } : null;
          if (selection && options.beforeSend) {
            const checked = await options.beforeSend({ text, selection: structuredClone(selection),
              conversation: structuredClone(entry.conversation), source: 'product_material' });
            if (checked === false) throw Error('已取消提交，已上传的资料仍可使用。');
            if (checked && Object.hasOwn(checked, 'selection')) selection = checked.selection;
            if (selection?.target?.object_ref?.id !== productId) throw Error('要更新的商品已变化，请重新核对。');
          }
          await ensureSession();
          entry.composedText = instructions;
          setError('');
          const action = entry.room ? { room_material_action: 'import' }
            : product ? { product_material_action: 'update' }
              : { product_material_action: 'create', catalog_create_room: true };
          entry.pending = structuredClone({ scope: entry.conversation.scope, text, material_refs: entry.materialRefs, selection,
            view_context: { ...destination, ...action },
            intent_mode: 'new_task', related_run_id: null,
            observed_versions: selection ? [selection.target.object_ref] : [],
            client_request_id: uuid() });
        }
        announceState();
        const request = entry.pending;
        let receipt;
        try {
          receipt = await api('/conversations/' + encodeURIComponent(conversationId) + '/messages', { method: 'POST', body: request });
          if (!(receipt.run?.run_id || receipt.run?.id || receipt.run_id)) throw Error('服务未返回任务编号。');
        } catch (error) {
          if (!retry && error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
            entry.pending = null;
            throw error;
          }
          throw Object.assign(Error('暂未确认任务是否已受理。请点击原按钮重试，会核实同一请求。'), { unknown: true });
        }
        entry.receipt = structuredClone(receipt);
        entry.pending = null;
        try {
          if (state.conversation?.conversation_id !== conversationId) await loadConversation(conversationId);
          if (state.conversation?.conversation_id !== conversationId) throw Error('暂时无法打开资料对话。');
          const run = receipt.run || { run_id: receipt.run_id };
          const message = receipt.message || { message_id: request.client_request_id, role: 'user',
            text: request.text, run_id: run.run_id || run.id };
          if (!state.messages.some(value => value.message_id === message.message_id)) state.messages.push(message);
          if (entry.compose && input.value === entry.composedText) input.value = '';
          addRun(run);
          renderContext();
          renderSelection();
          renderAttachments();
          renderConversation();
          announceState();
          await pollRuns(state.generation, true);
        } catch (_) {
          setError('任务已受理，进度暂未载入。可在右侧重新打开这次资料对话。');
        }
        return structuredClone(receipt);
      } finally { submittingProductBatch = null; state.sending = false; setBusy(); announceState(); }
    }
    async function refresh() { if (!state.conversation) return initialize(); return pollRuns(state.generation, true); }
    async function refreshCatalogRequirement(productId) {
      const reports = Array.from(state.runs.values()).filter(run =>
        catalogIntakeProducts(run).some(item => item.product_id === productId));
      if (!reports.length) return;
      const generation = state.generation;
      try {
        const current = await Promise.all(reports.map(run => api('/runs/' + encodeURIComponent(run.run_id))));
        if (state.destroyed || generation !== state.generation) return;
        for (const entry of current) absorbRun(entry.run || entry);
        renderConversation();
        announceState();
      } catch (_) {
        if (!state.destroyed && generation === state.generation) {
          setError('商品资料已保存，入库报告暂未刷新。可重试连接。');
        }
      }
    }
    form.addEventListener('submit', sendMessage); input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(); } }); upload.addEventListener('change', uploadFiles);
    const timer = setInterval(() => {
      if (!state.destroyed && !doc.hidden && Date.now() >= nextProgressPollAt) pollRuns();
    }, options.pollInterval || 1800);
    const visibility = () => { if (!doc.hidden) refresh(); }; doc.addEventListener('visibilitychange', visibility);
    renderTabs(); renderContext(); renderConversation(); const ready = initialize();
    input.addEventListener('input', announceState);
    async function prepareTask(text,taskContext={},selection=null) {
      if(state.destroyed)throw Error('对话界面已关闭，请刷新后重试');
      if(state.sending||state.uploading||state.pendingSubmission||state.preparingTask)throw Error('当前请求正在提交，请稍后再设置新增要求。');
      if(input.value.trim()||state.materials.length||state.scriptGeneration||activeProductMaterialBatch())throw Error('右侧有尚未发送的要求或附件，请先发送或清空，再准备新任务。');
      const target={...viewContext(),...taskContext},expected=target.preparation_id?{kind:'preparation',preparation_id:target.preparation_id}:{kind:'catalog'},matches=conversation=>{const scope=conversation?.scope;return scope?.kind===expected.kind&&(expected.kind!=='preparation'||scope.preparation_id===expected.preparation_id)&&(!target.room_id||conversation?.room_id===target.room_id);};
      const wasDisabled=input.disabled;state.preparingTask=true;input.disabled=true;setBusy();
      try {
        if(!matches(state.conversation)){const next=await createConversation(target);if(!next||!matches(next))throw Error('未能为当前本场建立匹配的对话，新增要求未提交；请重试连接。');}
        state.selected=selection;renderSelection();mode.value='new_task';state.relatedRunOverride=null;
        state.scriptGeneration=taskContext.script_generation ? {
          request:structuredClone(taskContext.script_generation), label:taskContext.script_generation_label||'已选商品', text:String(text||'')
        } : null;
        input.value=state.scriptGeneration ? '' : String(text||'');
        renderAttachments();
        announceState();
      } finally {state.preparingTask=false;input.disabled=wasDisabled;setBusy();}
      input.focus();
    }
    function showStartAudit({issues, context: taskContext, onNavigate}) {
      hideDecision();
      auditScope = {room_id: taskContext.room_id, preparation_id: taskContext.preparation_id};
      const question = node('section', 'sb-a-question');
      question.append(node('p', 'sb-a-decision-prompt', '当前本场还不能开播。处理下面的问题后，再点击「开播」复查。'));
      const list = node('ul', 'sb-a-start-audit-items');
      for (const issue of issues) {
        const item = node('li', '', issue.text);
        if (issue.action) {
          const status = node('span', 'sb-a-decision-error');
          status.setAttribute('role', 'status');
          const act = button(issue.actionLabel, async () => {
            act.disabled = true;
            const scope = auditScope;
            try {
              await issue.action();
              if (auditScope === scope && auditDialog.open) auditDialog.close();
            } catch (error) {
              status.textContent = safeText(error.message);
            } finally {
              act.disabled = false;
            }
          }, 'sb-a-link');
          item.append(' ', act, status);
        } else if (issue.destination) {
          item.append(' ', button(issue.actionLabel, () => {
            auditDialog.close();
            onNavigate(issue.destination);
          }, 'sb-a-link'));
        }
        list.append(item);
      }
      question.append(list);
      const actionable = issues.filter(issue => issue.agentCanHelp);
      if (taskContext.preparation_id && actionable.length) {
        const target = node('select', '', '');
        target.setAttribute('aria-label', '选择要处理的缺项');
        actionable.forEach((issue, index) => {
          const option = node('option', '', issue.text);
          option.value = String(index);
          target.append(option);
        });
        const reply = node('textarea', 'sb-a-answer');
        reply.rows = 3;
        reply.maxLength = 3000;
        reply.placeholder = '例如：用已有商品资料补一个核心卖点。';
        reply.setAttribute('aria-label', '补齐开播缺项的要求');
        const status = node('p', 'sb-a-decision-error');
        status.setAttribute('role', 'status');
        const handoff = button('发送给 Agent', async () => {
          const text = reply.value.trim();
          if (!text) {
            status.textContent = '请写明你想补齐哪项内容。';
            reply.focus();
            return;
          }
          handoff.disabled = true;
          try {
            const scope = auditScope;
            const selectedIssue = actionable[Number(target.value)];
            await prepareTask('本场开播检查发现必填项需要处理。\n' + selectedIssue.text +
              '\n我的处理要求：' + text +
              '\n只处理我指定的当前本场缺项。价格等缺失事实请向我询问，保留原有人工审批。', taskContext, selectedIssue.selection || null);
            if (auditScope !== scope || !auditDialog.open) return;
            auditDialog.close();
            await sendMessage();
          } catch (error) {
            status.textContent = safeText(error.message);
          } finally {
            handoff.disabled = false;
          }
        }, 'sb-a-confirm');
        const actions = node('div', 'sb-a-question-actions');
        actions.append(handoff);
        question.append(node('strong', 'sb-a-decision-section', '让 Agent 帮你补齐'));
        if (actionable.length > 1) question.append(target);
        question.append(reply, actions, status);
      }
      auditBody.replaceChildren(question);
      if (!auditDialog.open) auditDialog.show();
      auditClose.focus({preventScroll: true});
    }
    function setContext(value) {
      state.context = { ...value };
      if (auditDialog.open && (auditScope.room_id !== value.room_id || auditScope.preparation_id !== value.preparation_id)) {
        auditDialog.close();
      }
      if (value.preparation_id && value.prep_label) state.scopeNames.set(value.preparation_id, value.prep_label);
      renderContext();
      announceState();
    }
    return { ready, refresh, refreshCatalogRequirement, uploadFiles, uploadProductMaterials, submitProductMaterials, newConversation: createConversation, prepareTask, showStartAudit, getState: publicState, toggleSelection, setContext, setSelection(value, selectionOptions = {}) { const items=selectionItems(value);if(items.length>20)throw Error('一次最多选择20项');state.selected=packSelection(items); renderSelection(); announceState(); if (selectionOptions.focus !== false) input.focus(); }, focusInput() { input.focus(); }, setInput(value, { replace = false } = {}) { const text=String(value||'');if(replace||!input.value.trim())input.value=text;else if(!input.value.includes(text))input.value=input.value.replace(/\s+$/,'')+'\n\n'+text;announceState(); }, destroy() { state.destroyed = true; clearInterval(timer); state.controllers.forEach(c => c.abort()); doc.removeEventListener('visibilitychange', visibility); doc.removeEventListener('pointerdown', dismissHistory); root.remove(); }, version: VERSION, _inspect() { return { conversation_id: state.conversation && state.conversation.conversation_id, runs: state.runs.size, events: Array.from(state.events.values()).reduce((n, m) => n + m.size, 0), cursors: Object.fromEntries(state.cursors), showProcess: state.showProcess }; } };
  }
  const exported = { mount, version: VERSION, _test: { redact, safeText, eventKind, eventTitle, normalizedRun, statusName } }; global.ShengboAssistant = exported; if (typeof module !== 'undefined' && module.exports) module.exports = exported;
})(typeof window !== 'undefined' ? window : globalThis);
