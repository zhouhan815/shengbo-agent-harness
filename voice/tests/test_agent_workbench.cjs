/* Independent browser acceptance of the Agent-first workbench.
 * Private HTTP fixtures only: no real application process, model, credentials,
 * paid speech synthesis, normal database or external website is accessed.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const { chromium } = require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const WEB = path.resolve(__dirname, '../web');
const OUT = path.resolve(__dirname, process.argv.includes('--start-audit-only')
  ? '../verification/20261104-start-audit' : '../verification/agent-workbench-ui');
const VERSION = 'PRELIVE-2.3';
const sorted = x => Array.isArray(x) ? x.map(sorted) : x && typeof x === 'object' ? Object.fromEntries(Object.keys(x).sort().map(k => [k, sorted(x[k])])) : x;
const hash = x => 'sha256:' + crypto.createHash('sha256').update(JSON.stringify(sorted(x))).digest('hex');
const ref = (kind, id, revision = 1) => ({ kind, id, revision, content_hash: hash({ kind, id, revision }), ...(['product', 'config', 'preparation'].includes(kind) ? {} : { schema_version: VERSION }) });
const clone = x => structuredClone(x);
function product(id, name) {
  return { ref: ref('product', id), body: { id, name, price: 178, currency: 'TWD', sku_id: id, sale_status: 'available', selling_points: '轻便、易清洁，适合居家使用。', core_selling_points: ['轻便', '易清洁'], knowledge: '浴室遇水仍需小心行走。', knowledge_documents: [], material: 'EVA', target_audience: '居家用户', specifications: [{ name: '尺码', value: '36—45' }], ingredients: '', storage_conditions: '', shelf_life_value: null, shelf_life_unit: 'month', shipping_origin: '中国', shipping_time: '2至8个工作日寄出', return_policy: '以店铺售后政策为准', usage_instructions: '避免高温', faq: '脚背较宽建议大一码', hidden_optional_fields: [], archived: false } };
}
function fixtures() {
  const p1 = product('product-A', '足弓支撑拖鞋 A'), p2 = product('product-B', '宠物清洁梳 B');
  const rooms = ['A', 'B'].map(v => ({ ref: ref('config', 'room-' + v), body: { id: 'room-' + v, name: '测试直播间 ' + v, product_ids: [p1.ref.id, p2.ref.id], scripts: [], voice: 'Huihui', voice_provider: 'system', language: 'zh-CN', commerce: { scene: '', promotion: '', product_overrides: {}, coupon: { mode: 'none', currency: 'TWD', scope: 'all', product_ids: [] }, knowledge_documents: [], hidden_sections: [] } } }));
  const script = { ref: ref('script_revision', 'script-A1'), body: { preparation_id: 'prep-A', product_ref: p1.ref, product_id: p1.ref.id, variant_id: 'variant-A1', version_number: 1, script_revision: 1, module_refs: {} }, modules: {} };
  for (const key of ['opening_long', 'opening_short', 'product_intro', 'selling_points', 'campaign', 'cta', 'transition']) { script.body.module_refs[key] = ref('module', 'module-' + key); script.modules[key] = { key, sentences: [{ id: 'sentence-' + key, text: key === 'campaign' ? '本场活动价一百零七新台币。' : key + '原始讲解句。', spoken_text: key === 'campaign' ? '本场活动价一百零七新台币。' : key + '原始讲解句。' }] }; }
  const preparation = { ref: ref('preparation', 'prep-A'), body: { id: 'prep-A', room_id: 'room-A', goal: '为固定商品准备本场直播', status: 'draft', generation_epoch: 1, selected_product_refs: [p1.ref, p2.ref], script_refs: [script.ref], analysis_refs: [], source_refs: [], recap_refs: [], draft_ref: ref('draft', 'draft-A'), playlist_ref: ref('playlist', 'playlist-A'), candidate_publication_ref: null } };
  const details = { scripts: [script], draft: { ref: preparation.body.draft_ref, body: { preparation_id: 'prep-A', draft: { theme: '居家好物专场', objective: '讲清使用场景', scheduled_at: '', campaign_ends_at: '', timezone: 'Asia/Shanghai', language: 'zh-CN', commerce: rooms[0].body.commerce, tts_selection: { provider: 'system', voice: 'Huihui', model: 'speech-2.8-turbo' }, loop_mode: 'infinite', requested_variant_counts: {}, unknowns: [] } } }, playlist: { ref: preparation.body.playlist_ref, body: { items: [{ occurrence_id: 'occ-A1', product_id: p1.ref.id, script_revision_ref: script.ref }], loop_mode: 'infinite', product_group_order: [p1.ref.id, p2.ref.id] } }, compiled_plan: null, requirements: null, asset_checklist: { ref: ref('asset_checklist', 'assets-A'), body: { items: [{ item_id: 'asset-1', product_ref: p1.ref, category: 'use_scene', purpose: '展示日常使用', recommended_content: '提供浴室或居家实拍', status: 'missing', source_refs: [] }], notes: [] } } };
  const scope = { kind: 'preparation', preparation_id: 'prep-A', account_id: 'local' };
  const run = { run_id: 'run-A', id: 'run-A', status: 'RUNNING', version: 1, summary: '正在整理本场资料', questions: [], branches: [{ branch_task_id: 'branch-facts', role: 'product_facts', status: 'RUNNING' }], artifacts: [] };
  const conversation = { conversation_id: 'conversation-A', id: 'conversation-A', title: '本场 A 的准备', room_id: 'room-A', preparation_id: 'prep-A', scope, messages: [{ message_id: 'message-0', role: 'user', text: '准备本场', run_id: run.run_id }], runs: [run] };
  return { products: [p1, p2], rooms, preparation, details, scope, run, conversation, events: [], calls: [], messages: [], csrf: 'private-fixture-csrf', failures: [], blockMessages: false };
}
let state = fixtures();
function snapshot(preparationId) {
  return { voice_event_cursor: 1, products: { items: state.products.map(p => ({ ref: p.ref, ...p.body })) }, rooms: state.rooms, ...(preparationId ? { preparation: state.preparation, preparation_details: state.details } : {}) };
}
function emit(type, data = {}) {
  const event = { schema_version: VERSION, event_id: 'event-' + (state.events.length + 1), sequence: state.events.length + 1, run_id: state.run.run_id, timestamp: '2026-10-16T08:00:00Z', type, data }; state.events.push(event); return event;
}
function businessQuery(name, a) {
  if (name === 'get_workspace_snapshot') return snapshot(a.preparation_id);
  if (name === 'read_start_readiness') return {ready:false,room_id:'room-A',preparation_ref:state.preparation.ref,blockers:state.startAuditBlockers || [
    {code:'START_PRODUCT_SELLING_POINTS_REQUIRED',product_ref:state.products[0].ref,product_name:state.products[0].body.name},
    {code:'TEXT_APPROVAL_NOT_CURRENT'}
  ]};
  if (name === 'get_business_schema') {
    const properties = Object.fromEntries(Object.keys(state.products[0].body).filter(k => !['id', 'archived'].includes(k))
      .map(k => [k, { type: k === 'price' ? 'number' : ['knowledge_documents', 'core_selling_points', 'specifications', 'hidden_optional_fields'].includes(k) ? 'array' : 'string' }]));
    properties.knowledge_documents.items = { type: 'object', properties: {
      id: { type: 'string' }, title: { type: 'string' }, content: { type: 'string' }
    } };
    return { contract_version: VERSION, schema_hash: hash('fixture-schema'), schema: {
      product: { type: 'object', properties }, commerce: { type: 'object', properties: {} },
      module_keys: Object.keys(state.details.scripts[0]?.modules || {})
    } };
  }
  if (name === 'search_products') return { items: state.products.filter(p => !a.q || p.body.name.includes(a.q)).map(p => ({ ref: p.ref, ...p.body })), next_cursor: null };
  if (name === 'read_product') return clone(state.products.find(p => p.ref.id === (a.product_id || a.ref?.id)));
  if (name === 'read_preparation') return clone(state.preparation);
  if (name === 'read_room') return clone(state.rooms.find(r => r.ref.id === a.room_id));
  if (name === 'read_script') return clone(state.details.scripts.find(s => s.ref.id === (a.ref?.id || a.script_id)));
  if (name === 'list_script_audio_previews') return {items:clone((state.scriptAudioPreviews||[]).filter(item=>item.script_ref?.id===a.ref?.id)),total:(state.scriptAudioPreviews||[]).length,next_cursor:null};
  if (name === 'voice_options') return { providers: ['system', 'minimax'], voices: { system: ['Huihui'], minimax: ['female-shaonv'] }, models: { system: ['system'], minimax: ['speech-2.8-turbo'] }, system: { available: true }, minimax: { available: false } };
  if (name === 'read_completion_observation') return { jobs: [], pending_job_ids: [], unknown_job_ids: [], preparation: state.preparation, candidate_ready: false };
  if (name === 'validate_product_assembly') return { valid: true, normalized_fields: a.fields, errors: [] };
  if (name === 'read_analysis') return { items: [] };
  if (name === 'read_session_history') return { items: [state.preparation], preparations: [state.preparation] };
  if (name === 'list_preparations') return { items: [state.preparation] };
  if (name === 'read_change_history' || name === 'read_changes' || name === 'read_impact') return { items: [], affected: [] };
  if (name === 'read_operation_receipt') return { status: 'not_found' };
  if (name === 'create_text_review') return { review_hash: hash('text-review'), preparation_ref: state.preparation.ref, compiled_plan_ref: null, modules: [], requirements: [], approval: null, texts: [], cost_scope: { characters: 0 }, voice: { provider: 'system', voice: 'Huihui' }, plan_summary: {} };
  if (name === 'read_text_approval') return { status: 'not_reviewed' };
  if (name === 'read_events') return { events: [], next_cursor: 0 };
  if (name === 'create_review') return { review_hash: hash('review'), currentness: { blockers: ['尚未准备语音'], differences: [] } };
  throw Error('unhandled query ' + name);
}
function businessCommand(name, a) {
  if (name === 'record_product_revision_choice') return {preparation_ref:state.preparation.ref};
  if (name === 'save_product') {
    let p = state.products.find(p => p.ref.id === a.product_id);
    if (!p) { p = product('product-new', a.fields.name); state.products.push(p); }
    else assert.equal(a.expected_revision, p.ref.revision, 'product update must carry actual base revision');
    p.body = { ...p.body, ...clone(a.fields) };
    p.ref = ref('product', p.ref.id, p.ref.revision + 1);
    return { product_ref: p.ref, product: p.body };
  }
  if (name === 'patch_script') {
    const s = state.details.scripts.find(s => s.ref.id === a.script_ref.id);
    assert.equal(a.selected_text_hash, hash(s.modules[a.module_key].sentences), 'manual module update binds exact source text');
    const old = s.ref; s.modules[a.module_key].sentences = a.sentences.map((text, i) => ({ id: 'patched-' + i, text, spoken_text: text }));
    s.ref = ref('script_revision', s.ref.id + '-new'); s.body.script_revision++;
    state.preparation.ref = ref('preparation', 'prep-A', state.preparation.ref.revision + 1);
    return { preparation_ref: state.preparation.ref, script_ref: s.ref, replacement_ref: s.ref, old_ref: old, replacement: { old_ref: old, new_ref: s.ref } };
  }
  if (name === 'save_session_draft') { state.details.draft.body.draft = clone(a.draft); state.preparation.ref = ref('preparation', 'prep-A', state.preparation.ref.revision + 1); return { preparation_ref: state.preparation.ref, draft_ref: state.details.draft.ref }; }
  if (name === 'update_preparation_products') { state.preparation.body.selected_product_refs = clone(a.selected_product_refs); state.preparation.ref = ref('preparation', 'prep-A', state.preparation.ref.revision + 1); return { preparation_ref: state.preparation.ref }; }
  throw Error('unhandled command ' + name);
}
const server = http.createServer(async (req, res) => {
  const json = (data, code = 200) => { res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(data)); };
  try {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/' || url.pathname === '/agent' || url.pathname === '/agent-workbench') { res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(fs.readFileSync(path.join(WEB, 'agent-workbench.html'))); return; }
    if (url.pathname.startsWith('/assets/')) { const file = path.join(WEB, path.basename(url.pathname)); if (!fs.existsSync(file)) return json({ error: { message: 'Missing asset ' + path.basename(file) } }, 404); res.writeHead(200, { 'Content-Type': file.endsWith('.css') ? 'text/css' : file.endsWith('.svg') ? 'image/svg+xml' : 'application/javascript' }); res.end(fs.readFileSync(file)); return; }
    if (url.pathname.endsWith('favicon.ico')) { res.writeHead(204); res.end(); return; }
    let raw = ''; for await (const chunk of req) raw += chunk; const body = raw ? JSON.parse(raw) : null;
    state.calls.push({ path: url.pathname, method: req.method, body: clone(body), csrf: req.headers['x-csrf-token'] });
    if (url.pathname === '/api/audio/output-status') return json({schema_version:'AUDIO-OUTPUT-1',available:true,devices:[{id:'speakers',name:'测试扬声器',default:true}],browser_sessions:[{application:'Chrome',device_name:'测试扬声器',active:true,peak:.25,muted:false,volume_percent:100}]});
    if (/^\/api\/rooms\/[^/]+\/state$/.test(url.pathname)) return json(state.playback || { status:'stopped', current:null });
    if (/^\/api\/rooms\/[^/]+\/live-source$/.test(url.pathname) && req.method === 'GET') return json({ config:{enabled:false}, status:{state:'disabled',connected:false}, events:[], next_cursor:0 });
    if (/^\/api\/rooms\/[^/]+\/control$/.test(url.pathname)) {
      assert.equal(body.expected_session_id, state.playback.session_id);
      assert.equal(body.expected_epoch, undefined, 'pause/resume apply to the active session across sentence boundaries');
      state.playback.status = body.action === 'pause' ? 'paused' : 'ready';
      return json({ok:true});
    }
    if (url.pathname === '/api/assistant/v2/editor-locks') return json({schema_version:VERSION,locks:state.editorLocks||[]});
    if (url.pathname === '/api/assistant/v2/session') return json({ csrf_token: state.csrf });
    if (url.pathname === '/api/assistant/v2/conversations' && req.method === 'GET') return json({ items: [state.conversation] });
    if (url.pathname === '/api/assistant/v2/conversations' && req.method === 'POST') return json({ ...state.conversation, ...body, conversation_id: 'conversation-' + crypto.randomUUID(), messages: [], runs: [] });
    if (url.pathname === '/api/assistant/v2/conversations/conversation-A') return json({ ...state.conversation, messages: [...state.conversation.messages, ...state.messages], runs: [state.run] });
    if (url.pathname.endsWith('/messages')) { assert.equal(req.headers['x-csrf-token'], state.csrf); const m = { message_id: 'message-' + state.messages.length, role: 'user', text: body.text, run_id: state.run.run_id }; state.messages.push(m); if (state.blockMessages) { res.destroy(); return; } return json({ message: m, run: state.run, run_id: state.run.run_id }, 202); }
    if (url.pathname === '/api/assistant/v2/runs/run-A') return json(state.run);
    if (url.pathname === '/api/assistant/v2/runs/run-A/events') { const after = Number(url.searchParams.get('after') || 0); return json({ events: state.events.filter(e => e.sequence > after), next_cursor: state.events.length, has_more: false }); }
    if (url.pathname === '/api/assistant/v2/events') { res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.end(': fixture idle\n\n'); return; }
    if (url.pathname === '/api/assistant/v2/workspace') return json({ voice_snapshot: snapshot('prep-A'), runs: [state.run], artifacts: state.run.artifacts, conversation: state.conversation });
    if (url.pathname === '/api/prelive/v2/queries') { assert.equal(req.headers['x-csrf-token'], state.csrf); return json(businessQuery(body.query, body.args || {})); }
    if (url.pathname === '/api/prelive/v2/commands') { assert.equal(req.headers['x-csrf-token'], state.csrf); assert.equal(body.contract_version, VERSION); assert.ok(body.mutation.operation_id); return json({ schema_version: VERSION, command: body.command, operation_id: body.mutation.operation_id, status: 'committed', data: businessCommand(body.command, body.args), changed_paths: [], invalidated_refs: [], event_cursor: 1 }); }
    if (url.pathname === '/api/library') return json({ products: state.products.map(p => ({ ...p.body, revision: p.ref.revision })), rooms: state.rooms.map(r => ({ ...r.body, revision: r.ref.revision })) });
    if (url.pathname === '/api/prelive/v2/preparations') return json({ items: [state.preparation] });
    if (url.pathname.startsWith('/api/prelive/v2/preparations')) return json({ items: [state.preparation] });
    if (url.pathname.endsWith('/health') || url.pathname.endsWith('/status')) return json({ status: 'ready', connected: true, active: false });
    throw Error('unhandled route ' + req.method + ' ' + url.pathname);
  } catch (error) { state.failures.push(error.stack); json({ error: { code: 'MOCK_ASSERTION', message: error.message } }, 500); }
});

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;
  let browser; try { browser = await chromium.launch({ headless: true }); } catch { browser = await chromium.launch({ channel: 'msedge', headless: true }); }
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage(); page.setDefaultTimeout(7000);
  const errors = [], results = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.type() === 'beforeunload' ? dialog.accept() : dialog.dismiss());
  const check = async (name, fn) => { try { await fn(); results.push({ name, result: 'PASS' }); console.log('PASS ' + name); } catch (error) { results.push({ name, result: 'FAIL', detail: error.message }); console.error('FAIL ' + name + ': ' + error.message); } };
  const ui = code => page.evaluate(code);
  const navigate = async dest => { await page.evaluate(d => window.shengboWorkbench.navigate(d), dest); };
  const openProduct = async id => { await navigate({ area: 'catalog', view: 'product-detail', resource_id: id, product_id: id }); const more = page.locator('.wb-product-form > details').filter({hasText:'规格参数与其他可选资料'}).first(); if(await more.count()&&!await more.evaluate(n=>n.open))await more.locator(':scope > summary').click(); };
  const focus = async mode => { await page.evaluate(mode => window.shengboWorkbench.focus(mode), mode); await page.waitForTimeout(260); };
  const field = name => name==='selling_points'?page.locator('[data-field="core_selling_points"] textarea').first():page.locator('[data-field="' + name + '"]').first();
  const chatInput = () => page.locator('.sb-a-input');
  try {
    await page.goto(base); await page.waitForFunction(() => !!window.shengboWorkbench); await ui(() => window.shengboWorkbench.ready);
    if(process.argv.includes('--start-audit-only')){
      await navigate({view:'room-config',room_id:'room-A',preparation_id:'prep-A'});
      const start=page.locator('#room-broadcast-tile [data-exec-action="start"]');
      await start.click();
      const dialog=page.locator('#chat-panel dialog.sb-a-start-audit[open]');
      await dialog.waitFor();
      assert.match(await dialog.innerText(),/足弓支撑拖鞋 A.*缺少核心卖点/);
      assert.match(await dialog.innerText(),/审核并批准当前话术/);
      assert.equal(state.calls.filter(c=>c.body?.query==='read_start_readiness').length,1);
      assert.equal(state.calls.filter(c=>c.body?.command==='start_prepared_room'||c.body?.action==='start'||c.path.endsWith('/messages')).length,0);
      await dialog.screenshot({path:path.join(OUT,'start-audit.png')});
      await dialog.getByRole('textbox').fill('根据已有资料，帮我补一条拖鞋的核心卖点。');
      await Promise.all([page.waitForResponse(r=>r.url().endsWith('/messages')),dialog.getByRole('button',{name:'发送给 Agent'}).click()]);
      const request=state.calls.filter(c=>c.path.endsWith('/messages')).at(-1).body;
      assert.equal(request.scope.preparation_id,'prep-A');
      assert.equal(request.selection.target.object_ref.id,'product-A');
      assert.equal(request.selection.target.field_path,'/core_selling_points');
      assert.match(request.text,/足弓支撑拖鞋 A.*缺少核心卖点/);
      assert.match(request.text,/根据已有资料，帮我补一条拖鞋的核心卖点/);
      assert.equal(state.calls.filter(c=>c.body?.command==='start_prepared_room'||c.body?.action==='start').length,0);
      const original=state.products[0].ref, updated=ref('product',original.id,2), diffHash=hash('confirmed revision difference');
      state.startAuditBlockers=[{code:'START_PRODUCT_VERSION_REQUIRED',product_ref:updated,product_name:state.products[0].body.name,
        pinned_product_ref:original,diff_hash:diffHash}];
      await start.click();
      await dialog.waitFor();
      await Promise.all([page.waitForResponse(r => r.url().endsWith('/commands')),dialog.getByRole('button',{name:'将本场改用商品新版'}).click()]);
      const choice=state.calls.filter(c=>c.body?.command==='record_product_revision_choice').at(-1).body.args;
      assert.deepEqual(choice,{preparation_id:'prep-A',expected_revision:state.preparation.ref.revision,
        pinned_product_ref:original,observed_head_ref:updated,diff_hash:diffHash,choice:'refresh'});
      assert.equal(state.calls.filter(c=>c.body?.command==='start_prepared_room'||c.body?.action==='start').length,0);
      results.push({name:'开播右侧检查、缺项提交及新版商品显式采用',result:'PASS'});
      console.log('Start audit UI: scoped checks, exact Agent submission, exact human revision choice, no start command');
      return;
    }
    await check('UX00 Agent 主工作台与左右内容真实挂载', async () => { assert.equal(await page.locator('#work-panel').count(), 1); assert.equal(await page.locator('#chat-panel .sb-a-input').count(), 1); assert.equal(await page.locator('#workspace-editor').count(), 1); assert.ok((await page.title()).includes('Agent') || (await page.title()).includes('播前')); });
    await check('过程默认关闭，自动跟随默认开启并尊重用户关闭偏好', async () => {
      const follow = page.getByRole('checkbox', { name: '自动跟随当前任务成果' });
      assert.equal(await page.getByRole('switch', { name: '显示执行过程' }).isChecked(), false);
      assert.equal(await follow.isChecked(), true);
      assert.equal((await ui(() => window.shengboWorkbench.state())).followResults, true);
      await follow.uncheck();
      await page.reload();
      await ui(() => window.shengboWorkbench.ready);
      assert.equal(await follow.isChecked(), false);
    });
    await openProduct('product-A'); await field('name').waitFor();
    for (const width of [1440, 1024, 768, 390, 320]) await check('UX08 ' + width + 'px 双焦点布局与无页面横向溢出', async () => {
      await page.setViewportSize({ width, height: 1000 });
      for (const mode of ['chat', 'work']) { if (width <= 800) { await page.locator(mode === 'chat' ? '#mobile-chat' : '#mobile-work').click(); await page.waitForTimeout(260); } else await focus(mode); const geometry = await ui(() => { const w = document.querySelector('#work-panel'), c = document.querySelector('#chat-panel'); return { focus: document.body.dataset.focusMode, work: w.getBoundingClientRect().width, chat: c.getBoundingClientRect().width, workVisible: getComputedStyle(w).display !== 'none', chatVisible: getComputedStyle(c).display !== 'none', client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }; }); assert.equal(geometry.focus, mode); assert.ok(geometry.scroll <= geometry.client + 1, JSON.stringify(geometry)); if (width > 800) { assert.ok(geometry.workVisible && geometry.chatVisible); assert.ok(mode === 'chat' ? geometry.chat > geometry.work : geometry.work > geometry.chat, JSON.stringify(geometry)); } else { assert.equal(mode === 'chat' ? geometry.chatVisible : geometry.workVisible, true); assert.equal(mode === 'chat' ? geometry.workVisible : geometry.chatVisible, false); } const control = mode === 'chat' ? page.locator('.sb-a-send') : field('name'); const box = await control.boundingBox(); assert.ok(box && box.width > 25 && box.x >= -1 && box.x + box.width <= width + 1, 'primary control must remain in viewport: ' + JSON.stringify(box)); }
      await page.screenshot({ path: path.join(OUT, 'layout-' + width + '.png'), fullPage: true });
    });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await openProduct('product-A'); await field('name').waitFor();
    await check('UX04 普通商品输入与文字选中不会指定 Agent 修改目标', async () => { await field('name').click(); await field('name').evaluate(n => n.setSelectionRange(0, 4)); assert.equal(await page.locator('.sb-a-selection').isVisible(), false); });
    await check('UX01 双栏聚焦保留编辑器 DOM、值、选区与未发指令', async () => {
      await field('selling_points').fill('尚未保存的卖点草稿，继续编辑。'); await field('selling_points').evaluate(n => { n.dataset.identity = 'same-editor'; n.setSelectionRange(2, 6); });
      await chatInput().fill('未发送的对话指令。'); await chatInput().evaluate(n => n.setSelectionRange(3, 7));
      assert.equal(await ui(() => document.body.dataset.focusMode), 'chat');
      await field('selling_points').click(); await focus('work');
      assert.equal(await field('selling_points').inputValue(), '尚未保存的卖点草稿，继续编辑。'); assert.equal(await field('selling_points').getAttribute('data-identity'), 'same-editor'); assert.equal(await chatInput().inputValue(), '未发送的对话指令。');
      assert.deepEqual(await chatInput().evaluate(n => [n.selectionStart, n.selectionEnd]), [3, 7]);
    });
    await check('UX02 hover 与后台消息不改变焦点、草稿或当前编辑器', async () => {
      await field('selling_points').focus(); await field('selling_points').evaluate(n => n.setSelectionRange(2, 6)); await focus('work');
      await page.locator('#chat-panel').hover(); emit('tool.started', { tool_name: 'read_product', arguments: { product_id: 'product-A' }, summary: '后台读取资料' });
      state.run.messages = [{ id: 'background-result', role: 'assistant', content: '后台新消息', run_id: state.run.run_id }]; await ui(() => window.shengboWorkbench.assistant.refresh());
      assert.equal(await ui(() => document.body.dataset.focusMode), 'work'); assert.equal(await field('selling_points').getAttribute('data-identity'), 'same-editor'); assert.deepEqual(await field('selling_points').evaluate(n => [n.selectionStart, n.selectionEnd]), [2, 6]); assert.equal(await field('selling_points').inputValue(), '尚未保存的卖点草稿，继续编辑。');
    });
    await check('UX03 键盘进入左右输入与显式按钮能够切换聚焦', async () => {
      await field('name').focus(); await page.keyboard.press('Tab'); assert.equal(await ui(() => document.body.dataset.focusMode), 'work');
      await page.locator('#focus-chat').focus(); await page.keyboard.press('Enter'); await page.waitForTimeout(260); assert.equal(await ui(() => document.body.dataset.focusMode), 'chat');
      await chatInput().focus(); await page.keyboard.press('Shift+Tab'); assert.equal(await ui(() => document.body.dataset.focusMode), 'chat');
      await page.locator('#focus-work').focus(); await page.keyboard.press('Enter'); await page.waitForTimeout(260); assert.equal(await ui(() => document.body.dataset.focusMode), 'work');
    });
    await check('人工商品编辑通过统一命令并携带基础修订保存', async () => {
      await field('price').fill('188'); await page.getByRole('button', { name: '保存修改', exact: true }).click(); await page.waitForFunction(() => window.shengboWorkbench.editors.hasDirty() === false);
      const command = state.calls.filter(c => c.body?.command === 'save_product').at(-1); assert.ok(command); assert.equal(command.body.args.expected_revision, 1); assert.equal(Number(state.products[0].body.price), 188); assert.equal(state.products[0].body.core_selling_points[0], '尚未保存的卖点草稿，继续编辑。');assert.equal(state.products[0].body.core_selling_points[1],'易清洁');assert.equal(state.products[0].body.selling_points,state.products[0].body.core_selling_points.join('\n'));
    });
    await check('UX04 明确指定价格字段后保持左侧，并显示目标路径', async () => { await page.locator('[data-select-field="price"]').click(); assert.equal(await ui(() => document.body.dataset.focusMode), 'work'); assert.equal(await page.locator('.sb-a-selection').isVisible(), true); assert.match(await page.locator('.sb-a-selection').textContent(), /价|price/); });
    await check('UX05/09 导航 B 后发送仍冻结 A 字段与原对话本场', async () => {
      await openProduct('product-B'); await field('name').waitFor(); await chatInput().fill('只把日常售价改为189'); await Promise.all([page.waitForResponse(r => r.url().endsWith('/messages')), page.getByRole('button', { name: '发送', exact: true }).click()]); await page.waitForFunction(() => !document.querySelector('.sb-a-send').disabled);
      const message = state.calls.filter(c => c.path.endsWith('/messages')).at(-1); assert.ok(message); assert.equal(message.body.scope.preparation_id, 'prep-A'); assert.equal(message.body.selection.target.object_ref.id, 'product-A'); assert.ok((message.body.selection.target.field_path || '').includes('price')); assert.ok(message.body.observed_versions.some(r => r.id === 'product-A' && r.revision === 2));
      await page.locator('[data-select-field="name"]').click(); assert.equal(message.body.selection.target.object_ref.id, 'product-A', 'new selection cannot mutate frozen prior message');
    });
    await check('UX06 成果入口左侧 inline 定位，自动跟随关闭保持原视图', async () => {
      const before = await ui(() => window.shengboWorkbench.state().view);
      state.run.artifacts = [{ label: '查看活动话术成果', ref: state.details.scripts[0].ref, destination: { area: 'workbench', view: 'scripts', preparation_id: 'prep-A', room_id: 'room-A', resource_id: 'script-A1', script_id: 'script-A1', module_key: 'campaign', field_path: '/modules/campaign' } }];
      await ui(() => window.shengboWorkbench.assistant.refresh()); assert.equal(await ui(() => window.shengboWorkbench.state().view), before); await page.getByRole('button', { name: '查看活动话术成果', exact: true }).click(); await page.locator('textarea[data-module="campaign"]').waitFor(); assert.equal(await page.locator('dialog[open]').count(), 0); assert.equal(await ui(() => window.shengboWorkbench.state().view), 'scripts');
    });
    await check('话术模块可人工微调并按原文 hash 保存', async () => { await page.locator('textarea[data-module="campaign"]').fill('本场活动价一百二十八新台币。'); const button = page.locator('[data-save-module="campaign"]'); if (await button.count()) await button.click(); else await page.locator('textarea[data-module="campaign"]').locator('xpath=ancestor::section[1]').getByRole('button', { name: /保存/ }).first().click(); await page.waitForFunction(() => window.shengboWorkbench.editors.hasDirty() === false); assert.ok(state.calls.some(c => c.body?.command === 'patch_script' && c.body.args.module_key === 'campaign')); });
    await check('执行过程开关打开后展示参数，关闭不停止采集', async () => { await page.getByRole('switch', { name: '显示执行过程' }).check(); assert.ok(await page.locator('.sb-a-event').count() >= 1); assert.ok(await page.getByText('发送的参数', { exact: true }).count() >= 1); await page.getByRole('switch', { name: '显示执行过程' }).uncheck(); assert.equal(await page.locator('.sb-a-process').count(), 0); assert.ok((await ui(() => window.shengboWorkbench.assistant._inspect())).events >= 1); });
    await check('UX01/07 左侧滚动与未保存编辑跨导航保留', async () => { await openProduct('product-A'); await field('faq').fill('本机待恢复的 FAQ 草稿。'); await page.locator('#workspace-editor').evaluate(n => n.scrollTop = 180); const scroll = await page.locator('#workspace-editor').evaluate(n => n.scrollTop); await focus('chat'); await focus('work'); assert.equal(await page.locator('#workspace-editor').evaluate(n => n.scrollTop), scroll); await openProduct('product-B'); await openProduct('product-A'); const restore = page.getByRole('button', { name: '恢复本机未保存内容', exact: true }); if (await restore.count()) await restore.click(); assert.equal(await field('faq').inputValue(), '本机待恢复的 FAQ 草稿。'); });
    await check('RUN05 刷新后未保存缓冲需用户恢复，不自动写入服务', async () => { const saved = state.products[0].body.faq; const saves = state.calls.filter(c => c.body?.command === 'save_product').length; await page.reload(); await page.waitForFunction(() => !!window.shengboWorkbench); await ui(() => window.shengboWorkbench.ready); await openProduct('product-A'); const restore = page.getByRole('button', { name: '恢复本机未保存内容', exact: true }); await restore.waitFor(); assert.equal(state.products[0].body.faq, saved); assert.equal(state.calls.filter(c => c.body?.command === 'save_product').length, saves); await restore.click(); assert.equal(await field('faq').inputValue(), '本机待恢复的 FAQ 草稿。'); });
    await check('指定的未保存对象发送前须明确确认，取消保留输入且不建任务', async () => {
      await page.locator('[data-select-field="faq"]').click(); await chatInput().fill('只让常见问题更口语一点');
      const before = state.calls.filter(c => c.path.endsWith('/messages')).length;
      await page.getByRole('button', { name: '发送', exact: true }).click(); await page.locator('dialog.wb-confirm-dialog[open]').waitFor();
      assert.equal(state.calls.filter(c => c.path.endsWith('/messages')).length, before);
      await page.getByRole('button', { name: '返回编辑', exact: true }).click(); await page.waitForFunction(() => !document.querySelector('.sb-a-send').disabled);
      assert.equal(await chatInput().inputValue(), '只让常见问题更口语一点'); assert.equal(await field('faq').inputValue(), '本机待恢复的 FAQ 草稿。'); assert.equal(state.calls.filter(c => c.path.endsWith('/messages')).length, before);
    });
    await check('保存修改并继续后，发送使用新修订而非本地草稿或旧引用', async () => {
      const before = state.calls.filter(c => c.path.endsWith('/messages')).length;
      await page.getByRole('button', { name: '发送', exact: true }).click(); await Promise.all([page.waitForResponse(r => r.url().endsWith('/messages')), page.getByRole('button', { name: '保存修改并继续', exact: true }).click()]);
      await page.waitForFunction(() => !document.querySelector('.sb-a-send').disabled);
      assert.equal(state.calls.filter(c => c.path.endsWith('/messages')).length, before + 1);
      const message = state.calls.filter(c => c.path.endsWith('/messages')).at(-1); assert.equal(message.body.selection.target.object_ref.revision, state.products[0].ref.revision); assert.ok(message.body.observed_versions.some(r => r.id === 'product-A' && r.revision === state.products[0].ref.revision)); assert.equal(state.products[0].body.faq, '本机待恢复的 FAQ 草稿。'); assert.equal(Number(state.products[0].body.price), 188);
    });
    await check('UX07 自动跟随开启仍保护未保存内容', async () => {
      await openProduct('product-A'); await field('faq').fill('自动跟随不得覆盖的未保存内容。'); await page.getByRole('checkbox', { name: '自动跟随当前任务成果' }).check(); await focus('work');
      state.run.artifacts.push({ label: '新成果 B（dirty保护）', ref: ref('analysis', 'dirty-result'), destination: { view: 'products', product_id: 'product-B', preparation_id: 'prep-A' } }); await ui(() => window.shengboWorkbench.assistant.refresh());
      assert.equal(await ui(() => window.shengboWorkbench.state().context.product_id), 'product-A'); assert.equal(await field('faq').inputValue(), '自动跟随不得覆盖的未保存内容。'); assert.equal(await ui(() => window.shengboWorkbench.state().focusMode), 'work');
    });
    await check('UX07 自动跟随开启仍保护未发送指令', async () => {
      await ui(() => window.shengboWorkbench.editors.discardDirty()); await chatInput().fill('这条指令还没有发送。'); await page.getByRole('checkbox', { name: '自动跟随当前任务成果' }).uncheck(); await page.getByRole('checkbox', { name: '自动跟随当前任务成果' }).check();
      state.run.artifacts.push({ label: '新成果 B（输入保护）', ref: ref('analysis', 'input-result'), destination: { view: 'products', product_id: 'product-B', preparation_id: 'prep-A' } }); await ui(() => window.shengboWorkbench.assistant.refresh());
      assert.equal(await ui(() => window.shengboWorkbench.state().context.product_id), 'product-A'); assert.equal(await chatInput().inputValue(), '这条指令还没有发送。');
    });
    await check('自动跟随无阻碍时左侧定位但不抢右侧焦点', async () => {
      await chatInput().fill(''); await page.getByRole('checkbox', { name: '自动跟随当前任务成果' }).uncheck(); await page.getByRole('checkbox', { name: '自动跟随当前任务成果' }).check(); await focus('chat');
      state.run.artifacts.push({ label: '可自动查看的新成果 B', ref: ref('analysis', 'clean-result'), destination: { view: 'products', product_id: 'product-B', preparation_id: 'prep-A' } }); await ui(() => window.shengboWorkbench.assistant.refresh());
      await page.waitForFunction(() => window.shengboWorkbench.state().context.product_id === 'product-B'); assert.equal(await ui(() => window.shengboWorkbench.state().focusMode), 'chat');
    });
    await check('DATA05 并发点击/Enter 只出现一个前置确认，取消不发请求', async () => {
      await page.getByRole('checkbox', { name: '自动跟随当前任务成果' }).uncheck(); await ui(()=>shengboWorkbench.assistant.setSelection(null,{focus:false})); await openProduct('product-A'); await field('faq').fill('并发发送测试的本地编辑。'); await page.locator('[data-select-field="faq"]').click(); await chatInput().fill('只更新这个常见问题的表达');
      const before = state.calls.filter(c => c.path.endsWith('/messages')).length;
      await ui(() => { const form = document.querySelector('.sb-a-composer'), input = document.querySelector('.sb-a-input'); form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })); form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
      await page.locator('dialog.wb-confirm-dialog[open]').waitFor(); assert.equal(await page.locator('dialog.wb-confirm-dialog').count(), 1); assert.equal(state.calls.filter(c => c.path.endsWith('/messages')).length, before); assert.equal(await page.locator('.sb-a-send').isDisabled(), true);
      await page.getByRole('button', { name: '返回编辑', exact: true }).click(); await page.waitForFunction(() => !document.querySelector('.sb-a-send').disabled); assert.equal(state.calls.filter(c => c.path.endsWith('/messages')).length, before);
    });
    await check('DATA05 并发点击/Enter 只提交一条已保存目标指令', async () => {
      await ui(() => window.shengboWorkbench.editors.discardDirty()); const before = state.calls.filter(c => c.path.endsWith('/messages')).length;
      await Promise.all([page.waitForResponse(r => r.url().endsWith('/messages')), ui(() => { const form = document.querySelector('.sb-a-composer'), input = document.querySelector('.sb-a-input'); form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })); form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); })]);
      await page.waitForFunction(() => !document.querySelector('.sb-a-send').disabled); assert.equal(state.calls.filter(c => c.path.endsWith('/messages')).length, before + 1); assert.equal(await page.locator('dialog.wb-confirm-dialog').count(), 0);
    });
    await check('本场页头可暂停和继续，不丢失句子进度且不发起生成', async () => {
      await ui(() => window.shengboWorkbench.editors.discardDirty());
      state.playback={status:'playing',current:{id:'sentence-pause-test'},session_id:'session-pause-test',position:1.25,epoch:9,connected_player:true,output_source:'browser'};
      await navigate({view:'overview',room_id:'room-A',preparation_id:'prep-A'}); await focus('work');
      const control=page.locator('#pause-preparation');
      assert.equal(await control.isVisible(),true); assert.match(await control.innerText(),/暂停播出/);
      const before=state.calls.length;
      await control.click(); await page.waitForFunction(()=>document.querySelector('#pause-preparation').textContent.includes('继续播出')&&!document.querySelector('#pause-preparation').disabled);
      assert.equal(state.playback.status,'paused'); assert.equal(state.playback.position,1.25);
      await control.click(); await page.waitForFunction(()=>document.querySelector('#pause-preparation').textContent.includes('暂停播出')&&!document.querySelector('#pause-preparation').disabled);
      const calls=state.calls.slice(before), controls=calls.filter(c=>c.path.endsWith('/control'));
      assert.deepEqual(controls.map(c=>c.body.action),['pause','resume']);
      assert.ok(controls.every(c=>c.path==='/api/rooms/room-A/control'&&c.body.expected_session_id==='session-pause-test'&&c.body.action_id));
      assert.ok(calls.every(c=>!c.path.endsWith('/commands'))); assert.equal(state.playback.current.id,'sentence-pause-test');
      state.playback={status:'stopped',current:null};
    });
    await check('播放清单自动展示当前句与设备，暂停后不会误报正在播放', async () => {
      state.playback={status:'playing',current:{id:'audio-display',product_name:'测试保温杯',version:'第1版',ordinal:2,total_sentences:4,text:'三百毫升，方便携带。'},session_id:'audio-display-session',position:.5,epoch:2,connected_player:true,output_source:'browser'};
      await navigate({view:'playlist',room_id:'room-A',preparation_id:'prep-A'});await focus('work');
      await page.waitForFunction(()=>document.querySelector('#audio-output-device').textContent.includes('测试扬声器'));
      assert.equal(await page.locator('#playback-strip').isVisible(),true);
      assert.match(await page.locator('#audio-current-meta').innerText(),/测试保温杯.*第 2 \/ 4 句/);
      assert.equal(await page.locator('#audio-current-text').innerText(),'三百毫升，方便携带。');
      await page.locator('#pause-preparation').click();
      await page.waitForFunction(()=>document.querySelector('#audio-playback-status').textContent==='已暂停');
      assert.match(await page.locator('#audio-local-status').innerText(),/不会输出声音/);
      state.playback={status:'stopped',current:null};
    });
    await check('工作台音频连接可选，展开配置不启动声音或业务任务', async () => {
      if(!await page.locator('#audio-settings').evaluate(n=>n.open))await page.locator('#audio-settings > summary').click();
      assert.equal(await page.locator('#audio-route-controls [data-route-local]').getAttribute('aria-pressed'),'true');
      const before=state.calls.length;
      await page.locator('#audio-route-controls [data-route-external]').click();
      assert.equal(await page.locator('#audio-route-controls details').getAttribute('open'),'');
      assert.equal(await page.locator('#audio-route-controls [data-route-external]').getAttribute('aria-pressed'),'false');
      assert.equal(state.calls.slice(before).filter(c=>c.method==='POST').length,0);
      await page.locator('#audio-route-controls summary').click();
    });
    await check('待确认弹窗留在右侧，离开日志也可选择或自填', async () => {
      state.run.status = 'WAITING_USER';
      state.run.questions = [{ decision_id: 'catalog-fixture', kind: 'catalog_intake_approval', status: 'OPEN', revision: 1, allow_text: false,
        revision_text: { choice: 'revise', label: '提交修改意见', placeholder: '说明希望调整的规格或价格' },
        prompt: '已读完模拟商品资料。标准装 29.90 元，加量装 39.90 元。请选择入库范围。\n资料中的促销参考价只供记录。本场活动价需在直播间单独设置。',
        options: [{ value: 'save_all', label: '全部分别入库' }, { value: 'save_1', label: '只保存标准装' }, { value: 'skip', label: '暂不保存' }] }];
      const before = state.calls.length;
      await navigate({view:'products'}); await focus('chat');
      const processSwitch = page.getByRole('switch', { name: '显示执行过程' });
      if (!await processSwitch.isChecked()) await processSwitch.check();
      await ui(() => window.shengboWorkbench.assistant.refresh());
      const dialog = page.getByRole('dialog', { name: '确认商品入库方案' });
      await dialog.waitFor();
      assert.equal(await dialog.getByText('待确认，尚未入库', { exact: true }).count(), 1);
      const geometry = await ui(() => {
        const panel = document.querySelector('#chat-panel').getBoundingClientRect();
        const pop = document.querySelector('.sb-a-decision-dialog').getBoundingClientRect();
        return { left: panel.left, right: panel.right, top: panel.top, bottom: panel.bottom, x: pop.left, y: pop.top, end: pop.right, low: pop.bottom };
      });
      assert.ok(geometry.x >= geometry.left && geometry.end <= geometry.right && geometry.y >= geometry.top && geometry.low <= geometry.bottom);
      assert.equal(await page.locator('.sb-a-transcript [data-decision-id]').count(), 0);
      assert.equal(await dialog.getByRole('button', { name: '只保存标准装' }).count(), 1);
      assert.equal(await dialog.getByRole('textbox').count(), 1);
      await page.screenshot({ path: path.join(OUT, 'right-agent-confirmation.png') });
      await dialog.getByRole('button', { name: '稍后处理' }).click();
      assert.equal(await dialog.count(), 0);
      await page.setViewportSize({ width: 390, height: 844 });
      await page.locator('#mobile-work').click();
      assert.match(await page.locator('#mobile-chat').innerText(), /1 项待确认/);
      await page.locator('#mobile-chat').click();
      await page.getByRole('button', { name: '查看待确认' }).click();
      assert.ok(await dialog.isVisible());
      assert.equal(await ui(() => document.documentElement.scrollWidth > innerWidth), false);
      state.run.status = 'CANCELLED';
      await ui(() => window.shengboWorkbench.assistant.refresh());
      assert.equal(await dialog.count(), 0, 'cancelled tasks must not surface an old open question');
      assert.ok(state.calls.slice(before).every(call => !call.path.endsWith('/answers') && !call.path.endsWith('/commands')));
      await page.setViewportSize({ width: 1440, height: 1000 });
    });
    await check('无未处理浏览器异常与未定义模拟路由', async () => { assert.deepEqual(errors, []); assert.deepEqual(state.failures, []); });
  } finally {
    await page.screenshot({ path: path.join(OUT, 'workbench-last.png'), fullPage: true }).catch(() => {});
    const report = { scope: 'Independent Chromium private-mock UI consumer acceptance; not real backend/model/OBS testing', timestamp: new Date().toISOString(), passed: results.filter(r => r.result === 'PASS').length, failed: results.filter(r => r.result === 'FAIL').length, results, browser_errors: errors, fixture_errors: state.failures };
    fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report)); await context.close(); await browser.close(); await new Promise(resolve => server.close(resolve));
    if (report.failed) process.exitCode = 1;
  }
}
// Reuse the same private fixture server in the complementary parity acceptance.
// Direct execution retains the original acceptance entry point.
module.exports = { server, fixtures, getState: () => state, resetState: () => { state = fixtures(); return state; }, emit, ref, hash };
if (require.main === module) main().catch(error => { console.error(error.stack); server.close(); process.exitCode = 1; });
