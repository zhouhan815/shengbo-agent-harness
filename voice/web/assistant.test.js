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
    await page.addScriptTag({ path: path.join(__dirname, 'assistant-decision-copy.js') });
    await page.addScriptTag({ path: path.join(__dirname, 'assistant.js') });
    await page.evaluate(() => {
      window.calls = []; window.failMessage = true;
      const events = [
        { event_id: 'e1', type: 'intent.assessed', data: { title: '识别本次任务', summary: '仅更新商品与研究，话术保留' } },
        { event_id: 'e2', type: 'tool.started', data: { title: '读取商品版本', tool_name: 'read_product', arguments: { product_id: 'p1', api_key: 'NEVER_SHOW', text: '<img src=x onerror=alert(1)>' }, result_preview: { id: 'p1', name: '拖鞋' }, payload_ref: 'opaque-01' } },
        { event_id: 'e3', type: 'middleware.checked', data: { middleware: 'M4 ToolPolicy', hook: 'before_tool', status: 'SUCCEEDED', reason_summary: '只允许原任务范围内的写入' } },
        ...['product_facts', 'session_config', 'audience_scenes'].map((role, i) => ({ event_id: 'e' + (4 + i), branch_task_id: 'b' + i, type: 'branch.progress', data: { role, title: role, status: 'RUNNING', progress: { completed: 1, total: 3 }, result_preview: { suggestion: '候选结果' } } })),
        { event_id: 'e7', type: 'tool.completed', data: { title: '资料更新回执', status: 'SUCCEEDED', result_preview: { receipt_id: 'receipt-1', revision: 2 } } },
        { event_id: 'e8', type: 'tool.failed', data: { title: '工具返回', status: 'INTERRUPTED', tool_name: 'request_user_decision', result_preview: 'GraphInterrupt' } },
        { event_id: 'e9', type: 'tool.interrupted', data: { title: '工具已暂停', status: 'INTERRUPTED', tool_name: 'request_user_decision' } },
        { event_id: 'e10', type: 'tool.failed', data: { title: '工具返回', status: 'FAILED' } }
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
        else if (url.includes('/events?')) value = { events: [...events, events[1]], next_cursor: 10 };
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
    assert.equal((await page.evaluate(() => window.ui._inspect())).events, 10, 'hidden process still receives and deduplicates events');
    assert.equal(await page.locator('.sb-a-run-status').textContent(), '正在执行', 'natural language does not declare completion');
    assert.equal(await page.getByRole('dialog').isVisible(), true, 'pending decision opens outside transcript');
    assert.equal(await page.locator('.sb-a-transcript .sb-a-question').count(), 0);
    await page.getByRole('button', { name: '稍后处理' }).click();
    await page.locator('[role=switch]').check();
    assert.equal(await page.locator('.sb-a-branch').count(), 3, 'parallel branches remain separate');
    assert.equal(await page.locator('.sb-a-event').count(), 10);
    await page.locator('.sb-a-raw-events > summary').click();
    assert.match(await page.locator('[data-event-id="e8"] .sb-a-badge').innerText(), /等待人工答复/);
    assert.match(await page.locator('[data-event-id="e9"] .sb-a-badge').innerText(), /等待人工答复/);
    assert.match(await page.locator('[data-event-id="e10"] .sb-a-badge').innerText(), /执行失败/);
    assert.equal(await page.locator('.sb-a-event img').count(), 0, 'tool text cannot become HTML');
    assert.ok(!(await page.locator('#assistant').textContent()).includes('NEVER_SHOW'));
    await page.getByRole('button', { name: '展开完整安全记录' }).click();
    assert.ok(!(await page.locator('#assistant').textContent()).includes('NEVER_PRIVATE'));
    assert.ok(!(await page.locator('#assistant').textContent()).includes('NEVER_SHOW_FULL'));
    await page.getByRole('button', { name: '查看待确认' }).click();
    await page.locator('.sb-a-answer').fill('保留这个补充，不应被进度刷新清除');
    await page.evaluate(() => window.ui.refresh());
    assert.equal(await page.locator('.sb-a-answer').inputValue(), '保留这个补充，不应被进度刷新清除');
    assert.equal(await page.locator('.sb-a-details summary').filter({ hasText: '完整安全记录' }).count(), 1, 'full record remains available after refresh');
    await page.locator('.sb-a-answer').press('Escape');
    assert.equal(await page.getByRole('dialog').count(), 0, 'Escape defers without answering');
    await page.locator('[role=switch]').uncheck();
    assert.equal(await page.locator('.sb-a-process').count(), 0);
    assert.equal((await page.evaluate(() => window.ui._inspect())).events, 10);
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
    await page.waitForFunction(() => !document.querySelector('.sb-a-send').disabled);
    const formTask = {preparation_id:'prepB',draft_ref:{kind:'draft',id:'draft-b',revision:1,content_hash:'sha256:b'},
      product_counts:[{product_ref:{kind:'product',id:'sku-b',revision:2,content_hash:'sha256:p'},count:2}],
      target_duration_seconds:{min:480,max:600}};
    const notes='双十一预写稿，语气自然，别把两个款式的优惠说混。';
    await page.evaluate(task=>ui.prepareTask('按本场配置新增两篇话术。',{
      room_id:'roomB',preparation_id:'prepB',script_generation:task,script_generation_label:'测试保冷袋 · 2 版'
    }),formTask);
    assert.equal(await page.getByText('新增话术 · 测试保冷袋 · 2 版',{exact:true}).count(),1);
    assert.equal(await page.locator('.sb-a-input').inputValue(),'','typed scope remains separate from editable notes');
    assert.equal(await page.getByLabel('任务提交方式',{exact:true}).isDisabled(),true);
    await page.locator('.sb-a-input').fill(notes);
    await page.evaluate(()=>window.failMessage=true);
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('.sb-a-send').disabled);
    assert.equal(await page.locator('.sb-a-input').inputValue(),notes);
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('.sb-a-send').disabled);
    const [typedAttempt,typedRetry]=await page.evaluate(()=>calls.filter(call=>call.url.endsWith('/messages')).slice(-2));
    assert.deepEqual(typedAttempt.body,typedRetry.body,'both typed task and notes are frozen across an uncertain send');
    assert.deepEqual(typedRetry.body.script_generation,{...formTask,writing_notes:notes});
    assert.equal(typedRetry.body.intent_mode,'new_task');
    assert.ok(typedRetry.body.text.endsWith(notes));
    assert.equal(await page.getByText('新增话术 · 测试保冷袋 · 2 版',{exact:true}).count(),0);
    await page.evaluate(() => {
      window.mockRun.status='COMPLETED';
      window.mockRun.summary='已保存，但篇幅未达明确最低要求。';
      window.mockRun.delivery_record={delivery_quality:'partial'};
      return ui.refresh();
    });
    assert.equal(await page.locator('.sb-a-run-status').filter({hasText:'任务部分达成'}).count(),1);


    await page.evaluate(() => window.ui.destroy());
    await page.evaluate(() => {
      const base = {run_id:'original', status:'BLOCKED', error:{code:'DUPLICATE_VARIANT_ID'}};
      const ref = id => ({kind:'script_revision', id, revision:1, content_hash:'sha256:' + id});
      const artifact = id => ({label:'查看已生成话术', ref:ref(id), destination:{view:'scripts',object_ref:ref(id)}});
      const followup = {
        run_id:'continued', status:'BLOCKED', input:{related_run_id:'original'},
        error:{code:'TEXT_SELECTION_QUERY_INVALID'},
        artifacts:[artifact('script-one'), artifact('script-two'), artifact('script-two'),
          {label:'查看本场准备', destination:{view:'preparation'}}],
        receipts:[
          {command:'save_script_plan',receipt:{status:'committed'}},
          {command:'submit_script_change',receipt:{status:'accepted',data:{job_id:'job-fixture'}}},
          {command:'submit_script_change',receipt:{status:'accepted',data:{job_id:'another-job'}}},
          {command:'submit_script_change',receipt:{status:'accepted',data:{job_id:'another-job'}}}
        ],
      };
      const event = (id, type, data, extra={}) => ({event_id:id,type,data,...extra});
      const failed = (id, name, code, invocation) => event(id,'tool.failed',
        {tool_name:name,status:'FAILED',title:'工具返回失败',result_preview:JSON.stringify({reason_code:code})},
        {invocation_id:invocation});
      const correction = (id, code, invocation) => event(id,'middleware.check',
        {hook:'correctable_tool_input',status:'CORRECTING',reason_code:code},{invocation_id:invocation});
      const trace = {
        original:[
          failed('duplicate','save_script_change_plan','DUPLICATE_VARIANT_ID','first'),
          event('original-error','run.error',{error_code:'DUPLICATE_VARIANT_ID',status:'FAILED'}),
          event('original-close','run.closed',{status:'BLOCKED',title:'任务已收尾'})
        ],
        continued:[
          failed('initial-failure','save_task_assessment','INTENT_EVIDENCE_INVALID','assessment'),
          correction('initial-correction','INTENT_EVIDENCE_INVALID','assessment'),
          event('retry-start','tool.started',{tool_name:'save_task_assessment'},{invocation_id:'assessment'}),
          event('retry-result','tool.completed',{tool_name:'save_task_assessment',status:'SUCCEEDED'},
            {invocation_id:'assessment',parent_event_id:'retry-start'}),
          ...Array.from({length:230},(_,i)=>event('mid-'+i,'middleware.check',{})),
          failed('context-error','read_context','WRONG_ARTIFACT_KIND','shared'),
          correction('context-correction','WRONG_ARTIFACT_KIND','shared'),
          event('different-tool','tool.completed',{tool_name:'read_run_history',status:'SUCCEEDED'},{invocation_id:'shared'}),
          failed('final-tool','read_script','TEXT_SELECTION_QUERY_INVALID','shared'),
          event('final-error','run.error',{error_code:'TEXT_SELECTION_QUERY_INVALID',status:'FAILED'}),
          event('final-close','run.closed',{status:'BLOCKED',title:'任务已收尾'})
        ]
      };
      for (const [runId,events] of Object.entries(trace)) {
        trace[runId] = events.map((item,i)=>({schema_version:'PRELIVE-2.3',run_id:runId,sequence:i+1,...item}));
      }
      const conversation = {conversation_id:'history',runs:[base,followup]};
      const fixtureFetch = async url => {
        let data;
        if (url.endsWith('/session')) data={csrf_token:'fixture'};
        else if (url.endsWith('/conversations')) data={items:[conversation]};
        else if (url.endsWith('/conversations/history')) data=conversation;
        else if (url.endsWith('/runs/original')) data=base;
        else if (url.endsWith('/runs/continued')) data=followup;
        else {
          const runId = url.match(/runs\/([^/]+)/)[1];
          const events = trace[runId];
          const params = new URL(url,'http://fixture.test').searchParams;
          const offset = Number(params.get('after') || 0);
          const limit = Number(params.get('limit') || 200);
          data={events:events.slice(offset,offset+limit),next_cursor:Math.min(events.length,offset+limit)};
        }
        return {ok:true,status:200,json:async()=>structuredClone(data)};
      };
      window.ui = ShengboAssistant.mount({container:document.getElementById('assistant'),fetch:fixtureFetch,storage:null,pollInterval:60000});
      return window.ui.ready;
    });
    const previousRun = page.locator('[data-run-id="original"]');
    const continuedRun = page.locator('[data-run-id="continued"]');
    assert.match(await previousRun.locator('.sb-a-run-summary').innerText(), /多个方案使用了同一个编号/);
    assert.match(await previousRun.innerText(), /后续任务已保存 2 版话术，仍有步骤未完成/);
    await previousRun.getByRole('button',{name:'查看后续任务'}).click();
    assert.equal(await continuedRun.evaluate(el=>el===document.activeElement),true);
    assert.equal(await continuedRun.locator('.sb-a-run-status').getAttribute('data-status'),'BLOCKED');
    assert.match(await continuedRun.locator('.sb-a-run-summary').innerText(), /已保存 2 版话术.*读取范围未通过校验/);
    await page.locator('[role=switch]').check();
    assert.equal(await continuedRun.getByText('已保存话术方案',{exact:true}).count(),1);
    assert.equal(await continuedRun.getByText('已提交 2 次文字生成',{exact:true}).count(),1);
    assert.equal(await continuedRun.getByText('核对本次任务要求：已修正并重试成功',{exact:true}).count(),1);
    assert.equal(await continuedRun.getByText('读取话术生成依据：进入修正，未记录重试',{exact:true}).count(),1);
    assert.equal(await continuedRun.locator('[data-event-id="retry-result"]').count(),0,'verified correction is visible before expanding older pages');
    await continuedRun.locator('.sb-a-raw-events > summary').click();
    assert.equal(await continuedRun.locator('[data-event-id="context-error"]').isVisible(),true,'an earlier independent failure remains visible');
    assert.equal(await continuedRun.locator('[data-event-id="final-tool"]').isVisible(),false);
    await continuedRun.getByText('本次结束的详细记录',{exact:true}).click();
    assert.equal(await continuedRun.locator('[data-event-id="final-tool"]').isVisible(),true);
    assert.equal(await continuedRun.locator('[data-event-id="final-close"]').isVisible(),true);
    await continuedRun.getByRole('button',{name:'查看接续前的任务'}).click();
    assert.equal(await previousRun.evaluate(el=>el===document.activeElement),true);
    await page.evaluate(() => window.ui.destroy());
    await page.evaluate(() => {
      const run = { run_id: 'catalog-progress', conversation_id: 'catalog', scope: {kind: 'catalog'}, status: 'RUNNING', branches: [
        { branch_task_id: 'old-attempt', role: 'product_facts', status: 'CANCELLED' }
      ] };
      const conversation = { conversation_id: 'catalog', runs: [run] };
      const events = [];
      function append(type, data = {}, branch_task_id) {
        const sequence = events.length + 1;
        events.push({ schema_version: 'PRELIVE-2.3', run_id: run.run_id, sequence,
          event_id: 'catalog-' + sequence, timestamp: '2026-11-04T08:06:25Z',
          type, data, branch_task_id });
      }
      append('branch.completed', { title: '旧的读取方式结束', status: 'CANCELLED' }, 'old-attempt');
      append('catalog_intake.fallback', { title: '资料需要进一步核对', status: 'RUNNING' });
      window.catalogFixture = { append, run, events, disconnected: false };
      const fetch = async (url, init = {}) => {
        let data;
        if (url.endsWith('/materials')) {
          window.catalogFixture.restoredUpload = { conversation: init.body.get('conversation_id'), file: init.body.get('file').name };
          data = { material_ref: { kind: 'source_manifest', id: 'restored', revision: 1, content_hash: 'sha256:fixture' } };
        } else if (url.endsWith('/decisions/recover-test/answers')) {
          window.catalogFixture.restoreAnswer = JSON.parse(init.body);
          run.status = 'RUNNING'; run.questions = []; run.recovery_pending = { state: 'resuming' };
          append('runtime.recovery.resuming', { title: '正在继续原任务', status: 'RECOVERING' });
          data = {run};
        } else if (url.endsWith('/session')) data = { csrf_token: 'fixture' };
        else if (url.endsWith('/conversations')) data = { items: [conversation] };
        else if (url.endsWith('/conversations/catalog')) data = conversation;
        else if (url.includes('/events?')) {
          if (window.catalogFixture.disconnected) throw Error('模拟进度连接中断');
          const params = new URL(url, 'http://fixture.test').searchParams;
          const after = Number(params.get('after'));
          const limit = Number(params.get('limit'));
          const cursor = Math.min(after + limit, events.length);
          const page = events.slice(after, cursor);
          data = { events: page.length ? [...page, page[0]] : [], next_cursor: cursor, has_more: cursor < events.length };
        } else data = run;
        return { ok: true, status: 200, json: async () => structuredClone(data) };
      };
      window.ui = ShengboAssistant.mount({ container: document.getElementById('assistant'),
        fetch, storage: null, pollInterval: 60000 });
      return window.ui.ready;
    });
    await page.locator('[role=switch]').check();
    const catalogRun = page.locator('[data-run-id="catalog-progress"]');
    const currentProgress = catalogRun.locator('.sb-a-current-progress');
    assert.match(await currentProgress.innerText(), /正在换一种方式读取资料/);
    assert.equal(await catalogRun.locator('.sb-a-branch').isVisible(), false, 'old branch is available in folded details');
    await page.evaluate(() => {
      const { append } = window.catalogFixture;
      append('workflow.progress', { workflow: 'catalog_intake', step_id: 'read', status: 'RECOVERING',
        title: '继续读取文档正文', summary: '商品照片没有可识别的文字，已改用正文和图片。' });
      append('model.request');
      append('model.response');
      append('model.request', {}, 'new-attempt');
      append('model.completed', {}, 'new-attempt');
      append('workflow.progress', { workflow: 'catalog_intake', step_id: 'write', status: 'RUNNING',
        title: '正在保存两款商品' });
      for (let index = 0; index < 205; index += 1) append('middleware.check');
      return window.ui.refresh();
    });
    assert.match(await currentProgress.innerText(), /正在保存两款商品/);
    assert.match(await currentProgress.innerText(), /Agent 模型请求 2 次（主 Agent 1，子任务 1）/);
    assert.match(await catalogRun.locator('.sb-a-progress-history').innerText(), /继续读取文档正文/);
    await catalogRun.locator('.sb-a-raw-events > summary').click();
    assert.match(await catalogRun.locator('.sb-a-branch').first().innerText(), /商品资料分析 · 已取消/);
    assert.equal(await catalogRun.locator('[data-event-id="catalog-2"]').count(), 0, 'raw history remains paged');
    await page.evaluate(() => {
      window.catalogFixture.disconnected = true;
      return window.ui.refresh();
    });
    assert.match(await currentProgress.innerText(), /正在保存两款商品/);
    await page.evaluate(() => {
      const { append } = window.catalogFixture;
      window.catalogFixture.disconnected = false;
      append('workflow.progress', { workflow: 'catalog_intake', step_id: 'verify', status: 'RUNNING',
        title: '正在核对两款商品的保存回执' });
      return window.ui.refresh();
    });
    assert.match(await currentProgress.innerText(), /正在核对两款商品的保存回执/);
    await page.evaluate(() => {
      window.catalogFixture.run.status = 'COMPLETED';
      window.catalogFixture.run.summary = '已保存并核对两款商品';
      return window.ui.refresh();
    });
    assert.equal(await currentProgress.getAttribute('data-status'), 'COMPLETED');
    assert.match(await currentProgress.innerText(), /已保存并核对两款商品/);
    await page.evaluate(() => {
      const { run, append } = window.catalogFixture;
      run.status = 'WAITING_USER';
      run.summary = '暂时读不到商品资料的原件';
      run.questions = [{kind:'runtime_recovery', decision_id:'recover-test', revision:1, title:'任务需要你协助继续',
        prompt:'卡在商品资料核对。暂时读不到商品资料的原件。', allow_text:true,
        options:[{value:'retry', label:'按原资料继续核对'}], context:{can_restore_source:true}}];
      append('runtime.recovery.waiting', {title:'已保留任务，等待你回复', status:'WAITING_USER'});
      return window.ui.refresh();
    });
    const recoveryDialog = page.getByRole('dialog');
    assert.match(await recoveryDialog.innerText(), /暂时读不到商品资料的原件/);
    assert.equal(await recoveryDialog.getByRole('button',{name:'按原资料继续核对'}).count(), 1);
    await recoveryDialog.getByRole('button',{name:'稍后处理'}).click();
    await page.getByRole('button',{name:'查看待确认'}).click();
    await recoveryDialog.getByRole('textbox',{name:'补充原文位置或说明'}).fill('SKU 在表格第二行');
    await recoveryDialog.locator('input[type=file]').setInputFiles({
      name:'商品原件.txt', mimeType:'text/plain', buffer:Buffer.from('原文件')});
    await page.waitForFunction(() => !!window.catalogFixture.restoreAnswer);
    const restored = await page.evaluate(() => ({ upload:window.catalogFixture.restoredUpload, answer:window.catalogFixture.restoreAnswer.answer }));
    assert.equal(restored.upload.conversation, 'catalog');
    assert.equal(restored.answer.source_ref.id, 'restored');
    assert.equal(restored.answer.text, 'SKU 在表格第二行');
    await page.waitForFunction(() => document.querySelector('[data-run-id="catalog-progress"] .sb-a-current-progress').textContent.includes('正在继续原任务'));
    await page.evaluate(() => window.ui.destroy());
    console.log('assistant consumer contract assertions passed; all API calls mocked; no model/TTS/business calls.');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
