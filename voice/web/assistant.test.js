/* Standalone browser consumer-contract tests. No provider or live business API calls. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const assistant = require('./assistant.js');
assert.equal(assistant._test.redact({ api_key: 'test', nested: { refresh_token: 'hidden', count: 3 }, reasoning: 'private' }).api_key, '[已隐藏]');
assert.equal(assistant._test.redact({ reasoning_content: 'private' }).reasoning_content, '[不展示私有推理]');
assert.equal(assistant._test.statusName('FINALIZING'), '正在收拢后台工作');

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: fs.existsSync(chromium.executablePath()) ? chromium.executablePath() : 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const browserErrors = [];
    page.on('pageerror', e => browserErrors.push(e.message));
    await page.setContent('<!doctype html><meta charset="utf-8"><div id="assistant" style="width:460px;height:920px"></div>');
    await page.addStyleTag({ content: fs.readFileSync(path.join(__dirname, 'assistant.css'), 'utf8') });
    await page.addScriptTag({ path: path.join(__dirname, 'assistant.js') });
    await page.evaluate(() => {
      window.calls = []; window.failMessage = true;
      const events = [
        { event_id: 'e1', type: 'intent.assessed', data: { title: '识别本次任务', summary: '仅更新商品与研究，话术保留' } },
        { event_id: 'e2', type: 'tool.started', data: { title: '读取商品版本', tool_name: 'read_product', arguments: { product_id: 'p1', api_key: 'NEVER_SHOW', text: '<img src=x onerror=alert(1)>' }, result_preview: { id: 'p1', name: '拖鞋' }, payload_ref: 'opaque-01' } },
        { event_id: 'e3', type: 'middleware.checked', data: { middleware: 'M4 ToolPolicy', hook: 'before_tool', status: 'SUCCEEDED', reason_summary: '只允许原任务范围内的写入' } },
        ...['product_facts', 'session_config', 'audience_scenes'].map((role, i) => ({ event_id: 'e' + (4 + i), branch_task_id: 'b' + i, type: 'branch.progress', data: { role, title: role, status: 'RUNNING', progress: { completed: 1, total: 3 }, result_preview: { suggestion: '候选结果' } } })),
        { event_id: 'e7', type: 'tool.completed', data: { title: '资料更新回执', status: 'SUCCEEDED', result_preview: { receipt_id: 'receipt-1', revision: 2 } } }
      ].map((e, i) => ({ schema_version: 'PRELIVE-2.3', sequence: i + 1, run_id: 'r1', timestamp: '2026-10-16T08:00:00Z', ...e }));
      const run = { run_id: 'r1', status: 'RUNNING', version: 1, summary: '说明文字中出现“已完成”不改变真实状态', questions: [{ decision_id: 'd1', revision: 1, kind: 'clarification', prompt: '这是新商品还是已有商品？', allow_text: true, options: [{ value: 'new', label: '新建商品' }] }], branches: ['product_facts', 'session_config', 'audience_scenes'].map((role, i) => ({ branch_task_id: 'b' + i, role, status: 'RUNNING' })) };
      window.mockRun = run;
      window.mockFetch = async (url, init = {}) => {
        const method = init.method || 'GET'; const parsed = init.body && typeof init.body === 'string' ? JSON.parse(init.body) : null;
        window.calls.push({ url, method, body: parsed, csrf: init.headers && init.headers['X-CSRF-Token'] });
        let value = {};
        if (url.endsWith('/session')) value = { csrf_token: 'session-only-fixture' };
        else if (url.endsWith('/conversations') && method === 'POST') value = {conversation_id:'cB',scope:parsed.scope,room_id:parsed.room_id,preparation_id:parsed.preparation_id,messages:[],runs:[]};
        else if (url.endsWith('/conversations') && method === 'GET') value = { items: [{ conversation_id: 'c1', title: '测试准备', scope: { kind: 'catalog', account_id: 'local' } }] };
        else if (url.endsWith('/conversations/c1')) value = { conversation_id: 'c1', scope: { kind: 'catalog', account_id: 'local' }, messages: [{ message_id: 'm1', role: 'user', text: '更新商品', run_id: 'r1' }], runs: [run] };
        else if (url.includes('/events?')) value = { events: [...events, events[1]], next_cursor: 7 };
        else if (url.endsWith('/runs/r1')) value = run;
        else if (url.includes('/payloads/')) value = { payload: { request: { product: 'p1', authorization: 'NEVER_SHOW_FULL' }, reasoning_content: 'NEVER_PRIVATE', output: { ok: true } } };
        else if (url.endsWith('/messages')) {
          if (window.failMessage) { window.failMessage = false; throw Error('模拟传输中断'); }
          value = { message: { message_id: 'm2', role: 'user', text: parsed.text, run_id: 'r1' }, run };
        } else if (url.endsWith('/commands')) value = { receipt_ref: 'human-receipt-01' };
        else if (url.endsWith('/human/product-identity')) value = { identity_decision_ref: { kind: 'human_decision', id: 'proof-1', revision: 1, content_hash: 'sha256:fixture' } };
        else if (url.includes('/answers')) { run.questions = []; value = { run }; }
        return { ok: true, status: 200, json: async () => structuredClone(value) };
      };
      window.ui = ShengboAssistant.mount({ container: document.getElementById('assistant'), fetch: window.mockFetch, storage: null, pollInterval: 60000, getViewContext: () => window.taskView || ({ page: 'products' }) });
    });
    await page.evaluate(() => window.ui.ready);
    assert.equal(await page.locator('[role=switch]').isChecked(), false);
    assert.equal(await page.locator('.sb-a-process').count(), 0);
    assert.equal((await page.evaluate(() => window.ui._inspect())).events, 7, 'hidden process still receives and deduplicates events');
    assert.equal(await page.locator('.sb-a-run-status').textContent(), '正在执行', 'natural language does not declare completion');
    await page.locator('[role=switch]').check();
    assert.equal(await page.locator('.sb-a-branch').count(), 3, 'parallel branches remain separate');
    assert.equal(await page.locator('.sb-a-event').count(), 7);
    assert.equal(await page.locator('.sb-a-event img').count(), 0, 'tool text cannot become HTML');
    assert.ok(!(await page.locator('#assistant').textContent()).includes('NEVER_SHOW'));
    await page.getByRole('button', { name: '展开完整安全记录' }).click();
    assert.ok(!(await page.locator('#assistant').textContent()).includes('NEVER_PRIVATE'));
    assert.ok(!(await page.locator('#assistant').textContent()).includes('NEVER_SHOW_FULL'));
    await page.locator('.sb-a-answer').fill('保留这个补充，不应被进度刷新清除');
    await page.evaluate(() => window.ui.refresh());
    assert.equal(await page.locator('.sb-a-answer').inputValue(), '保留这个补充，不应被进度刷新清除');
    assert.equal(await page.locator('.sb-a-details summary').filter({ hasText: '完整安全记录' }).count(), 1, 'full record remains available after refresh');
    await page.locator('[role=switch]').uncheck();
    assert.equal(await page.locator('.sb-a-process').count(), 0);
    assert.equal((await page.evaluate(() => window.ui._inspect())).events, 7);
    await page.locator('.sb-a-input').fill('只更新价格');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('.sb-a-send').disabled);
    assert.equal(await page.locator('.sb-a-input').inputValue(), '只更新价格', 'failed send retains text');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('.sb-a-send').disabled);
    const messages = await page.evaluate(() => window.calls.filter(c => c.url.endsWith('/messages')));
    assert.equal(messages.length, 2);
    assert.equal(messages[0].body.client_request_id, messages[1].body.client_request_id, 'uncertain send retries are idempotent');
    assert.ok(messages.every(m => !!m.csrf), 'mutations carry the bootstrap CSRF token');
    assert.equal(await page.locator('.sb-a-input').inputValue(), '');
    await page.evaluate(() => { window.mockRun.questions = [{ decision_id: 'd2', revision: 2, kind: 'audio_approval', prompt: '批准生成本场语音？', allow_text: false, options: [{ value: 'approve', label: '批准生成语音', human_action: { command: { command: 'approve_text_for_audio', operation_id: 'approval-op', args: { review_ref: 'review-2' } } } }] }]; return window.ui.refresh(); });
    await page.getByRole('button', { name: '批准生成语音' }).click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/decisions/d2/answers')));
    const approval = await page.evaluate(() => window.calls.filter(c => c.url.endsWith('/commands') || c.url.endsWith('/decisions/d2/answers')));
    assert.equal(approval[0].url, '/api/prelive/v2/commands');
    assert.equal(approval[1].body.human_receipt, 'human-receipt-01');
    await page.evaluate(() => { window.mockRun.messages = [{ id: 'final-message', role: 'assistant', content: '这是一条实际持久化的最终分析', run_id: 'r1' }]; return window.ui.refresh(); });
    await page.evaluate(() => window.ui.refresh());
    assert.equal(await page.locator('.sb-a-message-assistant').filter({ hasText: '这是一条实际持久化的最终分析' }).count(), 1, 'persisted final answer is shown once');
    await page.evaluate(() => { window.mockRun.questions = [{ decision_id: 'd3', revision: 1, kind: 'product_identity', prompt: '确认不同商品新建？', allow_text: false, options: [{ value: 'create_distinct', label: '确认不同商品', requires_human_action: true, human_action: { kind: 'product_identity', url: 'https://never-fetch.invalid', body: { target_name: '测试拖鞋', candidate_refs: [{ id: 'p1' }] } } }] }]; return window.ui.refresh(); });
    await page.getByRole('button', { name: '确认不同商品', exact: true }).click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/decisions/d3/answers')));
    const identity = await page.evaluate(() => window.calls.filter(c => c.url.endsWith('/human/product-identity') || c.url.endsWith('/decisions/d3/answers')));
    assert.equal(identity[0].url, '/api/prelive/v2/human/product-identity', 'only fixed identity endpoint can be called');
    assert.equal(identity[1].body.human_receipt.identity_decision_ref.id, 'proof-1');
    assert.equal(await page.evaluate(() => window.calls.some(c => c.url.startsWith('https:'))), false);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => { document.getElementById('assistant').style.cssText = 'width:100%;height:810px'; document.body.style.margin = '0'; });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    assert.equal(overflow, false, 'mobile panel does not overflow horizontally');
    assert.deepEqual(browserErrors, []);
    await page.evaluate(()=>{ui.setInput('',{replace:true});ui.setSelection({label:'旧稿模块',target:{object_ref:{kind:'script_revision',id:'old',revision:1},module_key:'cta'}},{focus:false});return ui.prepareTask('为当前商品新增两版完整话术');});
    assert.equal(await page.evaluate(()=>ui.getState().selected),null,'preparing generation explicitly clears stale module targets');
    assert.equal(await page.getByLabel('任务提交方式',{exact:true}).inputValue(),'new_task','generation never inherits an old follow-up edit mode');
    assert.equal(await page.evaluate(()=>ui.getState().inputText),'为当前商品新增两版完整话术');
    const refused=await page.evaluate(async()=>{try{await ui.prepareTask('不得覆盖草稿');return false;}catch(error){return error.message.includes('尚未发送');}});
    assert.equal(refused,true,'new task preparation preserves unsent user input');
    const oldConversation=await page.evaluate(()=>ui.getState().conversation.conversation_id);
    await page.evaluate(async()=>{ui.setInput('',{replace:true});window.taskView={view:'session',room_id:'roomB',preparation_id:'prepB'};await ui.prepareTask('仅为B本场新增版本',{room_id:'roomB',preparation_id:'prepB'});});
    assert.equal(oldConversation,'c1');
    assert.equal(await page.evaluate(()=>ui.getState().conversation.conversation_id),'cB','explicit generation uses a separate conversation for the selected preparation');
    assert.equal(await page.evaluate(()=>ui.getState().conversation.scope.preparation_id),'prepB');
    const messagesBefore=await page.evaluate(()=>calls.filter(call=>call.url.endsWith('/messages')).length);
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await page.waitForFunction(count=>calls.filter(call=>call.url.endsWith('/messages')).length>count,messagesBefore);
    const scopeSubmission=await page.evaluate(()=>calls.filter(call=>call.url.endsWith('/messages')).at(-1));
    assert.equal(scopeSubmission.url,'/api/assistant/v2/conversations/cB/messages');
    assert.equal(scopeSubmission.body.scope.preparation_id,'prepB','text JSON does not substitute for real request authorization scope');
    assert.equal(scopeSubmission.body.selection,null);
    assert.equal(await page.evaluate(()=>calls.filter(call=>call.url.endsWith('/conversations/c1/messages')).length),messagesBefore,'A conversation history receives no B message');
    await page.evaluate(() => window.ui.destroy());
    console.log('assistant consumer contract assertions passed; all API calls mocked; no model/TTS/business calls.');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
