/* Browser acceptance using only the private workbench server and synthetic uploads. */
'use strict';

const assert = require('node:assert/strict');
const { chromium } = require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fixture = require('./test_agent_workbench.cjs');
const { reusableDraft } = require('../web/workspace-editors.js');

const rightDraft = '原对话未发送的指令，不属于商品任务。';
const savedProductName = '隔离夹具手工商品';
const file = (name, text) => ({ name, mimeType: 'text/plain', buffer: Buffer.from(text) });

async function mockAgentUploads(page, state) {
  const conversations = new Map([[state.conversation.conversation_id, state.conversation]]);
  const uploads = [];
  const messages = [];
  const receipts = new Map();
  const mock = { conversations, uploads, messages, receipts, failNextReceipt: false, failUploadOnceFor: '' };

  await page.route(/\/live\/[^/?]+(?:\?.*)?$/, route => route.fulfill({
    contentType: 'text/html; charset=utf-8', body: '<!doctype html><title>Fixture preview</title>'
  }));
  await page.route(/\/api\/rooms\/[^/]+\/live-source\/events(?:\?.*)?$/, route => route.fulfill({
    json: { events: [], next_cursor: 0, has_more: false }
  }));
  await page.route('**/api/assistant/v2/conversations', async route => {
    if (route.request().method() === 'GET') {
      return route.fulfill({ json: { items: [...conversations.values()] } });
    }
    const body = route.request().postDataJSON();
    const id = 'fixture-catalog-' + conversations.size;
    const conversation = { ...body, id, conversation_id: id, title: '商品资料任务 ' + id, messages: [], runs: [] };
    conversations.set(id, conversation);
    return route.fulfill({ status: 201, json: conversation });
  });
  await page.route('**/api/assistant/v2/conversations/fixture-catalog-*', route => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1);
    const conversation = conversations.get(id);
    return conversation ? route.fulfill({ json: conversation }) : route.fallback();
  });
  await page.route('**/api/assistant/v2/materials', async route => {
    const request = route.request();
    const form = await new Request(request.url(), {
      method: 'POST',
      headers: { 'content-type': request.headers()['content-type'] },
      body: request.postDataBuffer()
    }).formData();
    const material = form.get('file');
    if (mock.failUploadOnceFor === material.name) {
      mock.failUploadOnceFor = '';
      return route.abort('failed');
    }
    const receipt = {
      name: material.name,
      material_ref: fixture.ref('source_manifest', 'fixture-upload-' + (uploads.length + 1)),
      status: 'uploaded'
    };
    uploads.push({
      conversationId: form.get('conversation_id'),
      scope: JSON.parse(form.get('scope')),
      text: await material.text(),
      receipt
    });
    return route.fulfill({ status: 201, json: receipt });
  });
  await page.route('**/api/assistant/v2/conversations/*/messages', async route => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2);
    const body = route.request().postDataJSON();
    messages.push({ id, body });
    const prior = receipts.get(body.client_request_id);
    if (prior) return route.fulfill({ status: 202, json: prior });
    const runId = 'fixture-upload-run-' + (receipts.size + 1);
    const run = { run_id: runId, id: runId, status: 'RUNNING', summary: '已受理隔离商品资料任务',
      artifacts: [], input: body };
    const message = { message_id: 'message-' + runId, role: 'user', text: body.text, run_id: runId };
    const receipt = { message, run, run_id: runId };
    receipts.set(body.client_request_id, receipt);
    const conversation = conversations.get(id);
    if (conversation) {
      conversation.messages.push(message);
      conversation.runs.push(run);
    }
    if (mock.failNextReceipt) {
      mock.failNextReceipt = false;
      return route.abort('failed');
    }
    return route.fulfill({ status: 202, json: receipt });
  });
  await page.route(/\/api\/assistant\/v2\/runs\/fixture-upload-run-[0-9]+(?:\/events(?:\?.*)?)?$/, route => {
    const pathname = new URL(route.request().url()).pathname;
    const runId = pathname.split('/')[5];
    const record = [...receipts.values()].find(value => value.run_id === runId);
    if (!record) return route.fallback();
    return route.fulfill({
      json: pathname.endsWith('/events') ? { events: [], next_cursor: 0, has_more: false } : record.run
    });
  });
  return mock;
}

function openedProduct(page, name) {
  return page.locator('#workspace-editor [data-catalog-product-id]')
    .filter({ hasText: name })
    .getByRole('button', { name: '编辑', exact: true });
}

function writeCalls(state) {
  return state.calls.filter(call => call.path.endsWith('/commands') || call.path.endsWith('/control') ||
    call.path.endsWith('/messages') || call.path.includes('/human/'));
}

async function setRightDraft(page) {
  const input = page.getByLabel('发送给播前助手的要求');
  await input.fill(rightDraft);
  await page.locator('.sb-a-file').setInputFiles(file('相同文件名.txt', '原对话专用附件。'));
  await page.locator('.sb-a-attachments').getByText('相同文件名.txt').waitFor();
  return input;
}

async function restoreRightDraft(page, input) {
  const picker = page.getByLabel('选择已有对话');
  await picker.selectOption('conversation-A');
  await page.locator('.sb-a-attachments').getByText('相同文件名.txt').waitFor();
  assert.equal(await input.inputValue(), rightDraft);
  assert.equal(await picker.inputValue(), 'conversation-A');
}

function assertTask(mock, index, mode, target) {
  const request = mock.messages[index];
  const conversation = mock.conversations.get(request.id);
  assert.notEqual(request.id, 'conversation-A');
  assert.deepEqual(conversation.scope, { kind: 'catalog', account_id: 'local' });
  assert.equal(conversation.room_id, null);
  assert.equal(conversation.preparation_id, null);
  assert.deepEqual(request.body.scope, conversation.scope);
  assert.equal(request.body.view_context.product_material_action, mode);
  assert.equal(request.body.view_context.catalog_create_room, mode === 'create' ? true : undefined);
  assert.equal(request.body.intent_mode, 'new_task');
  assert.ok(request.body.client_request_id);
  assert.ok(request.body.text.trim());
  assert.doesNotMatch(request.body.text, /原对话未发送的指令/);
  const scopedUploads = mock.uploads.filter(upload => upload.conversationId === request.id);
  assert.ok(scopedUploads.length > 0);
  assert.deepEqual(request.body.material_refs, scopedUploads.map(upload => upload.receipt.material_ref));
  for (const upload of scopedUploads) assert.deepEqual(upload.scope, conversation.scope);
  if (target) {
    assert.deepEqual(request.body.selection.target.object_ref, target);
    assert.ok(request.body.observed_versions.some(ref => ref.id === target.id && ref.revision === target.revision));
  } else {
    assert.equal(request.body.selection, null);
    assert.deepEqual(request.body.observed_versions, []);
  }
  return request;
}

async function main() {
  const { server, resetState } = fixture;
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    try {
      browser = await chromium.launch({ headless: true });
    } catch {
      browser = await chromium.launch({ channel: 'msedge', headless: true });
    }
    const base = 'http://127.0.0.1:' + server.address().port;
    const failures = [];
    const check = async (name, verify) => {
      const state = resetState();
      const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const blocked = [];
      await context.route('**/*', route => {
        if (new URL(route.request().url()).origin === base) return route.continue();
        blocked.push(route.request().url());
        return route.abort();
      });
      const page = await context.newPage();
      page.setDefaultTimeout(5500);
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      try {
        const mock = await mockAgentUploads(page, state);
        await page.goto(base);
        await page.evaluate(() => shengboWorkbench.ready);
        await verify({ page, state, mock });
        assert.deepEqual(blocked, [], 'UI made no off-origin requests');
        assert.deepEqual(state.failures, [], 'no unexpected fixture requests');
        assert.deepEqual(errors, [], 'no page exceptions');
        console.log('PASS ' + name);
      } catch (error) {
        failures.push(name);
        console.error('FAIL ' + name, error);
        console.error(await page.evaluate(() => ({
          surface: document.querySelector('#work-panel')?.dataset.workspaceSurface,
          workspaceNotice: document.querySelector('#workbench-notice-message')?.textContent,
          editorNotice: document.querySelector('#workspace-editor [role=status]')?.textContent,
          dialogNotice: document.querySelector('dialog[open] .wb-product-dialog-message')?.textContent,
          leftFilesNotice: document.querySelector('dialog[open] .wb-product-material-flow [role=status]')?.textContent,
          catalogRows: [...document.querySelectorAll('[data-catalog-product-id]')].map(row=>[row.dataset.catalogProductId,row.className])
        })));
        if (state.failures.length) console.error('Mock route errors', state.failures);
      } finally {
        await context.close();
      }
    };

    await check('商品库进入详情与返回只更换左侧，保留右侧对话、草稿、附件及输入控件',
      async ({ page, state, mock }) => {
        const input = await setRightDraft(page);
        const picker = page.getByLabel('选择已有对话');
        await input.evaluate(el => { window.fixtureComposer = el; });
        await page.evaluate(() => shengboWorkbench.navigate({view:'products',room_id:'room-A',preparation_id:'prep-A'}));
        const roomProducts = page.locator('#room-sheet[open]');
        await roomProducts.locator('[data-catalog-product-id]').filter({hasText:state.products[0].body.name})
          .getByRole('button',{name:'编辑',exact:true}).click();
        await page.locator('dialog[data-catalog-dialog="detail"] [data-select-field="faq"]').click();
        const selection = await page.evaluate(() => shengboWorkbench.assistant.getState().selected);
        assert.equal(selection.target.object_ref.id, state.products[0].ref.id);
        await page.locator('dialog[data-catalog-dialog="detail"]').getByRole('button', { name: '关闭', exact: true }).click();
        await roomProducts.waitFor({state:'hidden'});
        await page.locator('#open-catalog').click();
        const product = state.products[1];
        await openedProduct(page, product.body.name).click();
        const detail = page.locator('dialog[data-catalog-dialog="detail"][open]');
        const name = detail.locator('.wb-product-form [data-field="name"]').first();
        await name.waitFor();
        assert.equal(await name.inputValue(), product.body.name);
        await detail.getByRole('button', { name: '关闭', exact: true }).click();
        await openedProduct(page, product.body.name).waitFor();
        assert.equal(await input.inputValue(), rightDraft);
        assert.equal(await input.evaluate(el => el === window.fixtureComposer), true);
        assert.equal(await picker.inputValue(), 'conversation-A');
        assert.deepEqual(await page.evaluate(() => shengboWorkbench.assistant.getState().selected), selection);
        assert.equal(await page.locator('.sb-a-attachments .sb-a-attachment').count(), 1);
        assert.equal(mock.conversations.size, 1);
        assert.deepEqual(mock.messages, []);
        assert.deepEqual(writeCalls(state), []);
      });

    await check('编辑商品关闭时固定面板，区分真实改动和撤回，并保留未填完的卖点', async ({page,state}) => {
      const input=await setRightDraft(page);
      await page.locator('#open-catalog').click();
      const opener=openedProduct(page,state.products[0].body.name);
      const dialog=page.locator('dialog[data-catalog-dialog="detail"][open]');
      const confirmation=page.getByRole('dialog',{name:'关闭商品编辑？',exact:true});
      const close=dialog.getByRole('button',{name:'关闭',exact:true});
      const name=dialog.locator('[data-field="name"]').first();
      const faq=dialog.getByLabel('常见问题',{exact:true});
      await opener.click();
      const originalName=await name.inputValue();
      await name.fill(originalName+'临时输入');
      await name.fill(originalName);
      assert.deepEqual(await page.evaluate(()=>shengboWorkbench.editors.getDirtySummary()),[]);
      await input.click();
      await page.waitForFunction(()=>document.body.dataset.focusMode==='chat');
      await page.waitForTimeout(300);
      const target=await close.boundingBox();
      await page.mouse.move(target.x+target.width/2,target.y+target.height/2);
      await page.mouse.down();
      await page.waitForTimeout(280);
      assert.equal((await close.boundingBox()).x,target.x);
      await page.mouse.up();
      await dialog.waitFor({state:'hidden'});

      await opener.click();
      await faq.fill('尚未保存的商品常见问题');
      await faq.focus();
      const scroll=dialog.locator('.wb-product-dialog-scroll').first();
      const originalScroll=await scroll.evaluate(node=>node.scrollTop);
      assert.ok(originalScroll>0);
      await close.click();
      await confirmation.waitFor();
      assert.equal(await confirmation.getByRole('button',{name:'继续编辑'}).evaluate(node=>node===document.activeElement),true);
      await page.keyboard.press('Escape');
      await confirmation.waitFor({state:'hidden'});
      assert.equal(await faq.evaluate(node=>node===document.activeElement),true);
      assert.equal(await scroll.evaluate(node=>node.scrollTop),originalScroll);

      const rows=dialog.locator('.wb-core-point-row');
      const count=await rows.count();
      await dialog.getByRole('button',{name:'添加卖点',exact:true}).click();
      assert.equal(await rows.count(),count+1);
      await close.click();
      await confirmation.getByRole('button',{name:'保留草稿并关闭',exact:true}).click();
      await dialog.waitFor({state:'hidden'});
      await opener.click();
      await rows.last().waitFor();
      assert.equal(await rows.count(),count+1);
      assert.equal(await rows.last().getByRole('textbox').last().inputValue(),'');
      assert.equal(await faq.inputValue(),'尚未保存的商品常见问题');
      await close.click();
      await confirmation.getByRole('button',{name:'放弃草稿并关闭',exact:true}).click();
      await dialog.waitFor({state:'hidden'});
      await opener.click();
      await name.waitFor();
      assert.equal(await faq.inputValue(),state.products[0].body.faq);
      assert.equal(await rows.count(),count);
      await close.click();
      await dialog.waitFor({state:'hidden'});
      assert.equal(await input.inputValue(),rightDraft);
      assert.deepEqual(writeCalls(state),[]);
    });

    await check('左侧新建弹窗支持关闭、草稿保留和手工保存，窄屏可达',
      async ({ page, state, mock }) => {
        const input = await setRightDraft(page);
        await page.locator('#open-catalog').click();
        const opener = page.getByRole('button', { name: '新建商品', exact: true });
        await opener.click();
        const dialog = page.locator('#work-panel dialog.wb-product-dialog[open]');
        const name = dialog.locator('[data-field="name"]').first();
        await name.waitFor();
        assert.equal(await name.evaluate(el => el === document.activeElement), true);
        await page.waitForFunction(() => {
          const box = document.querySelector('dialog.wb-product-dialog[open]')?.getBoundingClientRect();
          const panel = document.querySelector('#work-panel').getBoundingClientRect();
          return box && box.left >= panel.left - 1 && box.right <= panel.right + 1 &&
            box.top >= panel.top - 1 && box.bottom <= panel.bottom + 1;
        });
        await input.click();
        assert.equal(await input.evaluate(el => el === document.activeElement), true, 'desktop right composer remains interactive');
        assert.equal(await input.inputValue(), rightDraft);
        await name.focus();
        await page.keyboard.press('Escape');
        await dialog.waitFor({ state: 'hidden' });
        assert.equal(await opener.evaluate(el => el === document.activeElement), true);

        await opener.click();
        await name.fill('保留的左侧草稿');
        await page.keyboard.press('Escape');
        const keep = dialog.getByRole('button', { name: '保留草稿并关闭', exact: true });
        await keep.waitFor();
        assert.deepEqual(writeCalls(state), []);
        await dialog.getByRole('button', { name: '继续编辑', exact: true }).click();
        assert.equal(await name.inputValue(), '保留的左侧草稿');
        await page.keyboard.press('Escape');
        await keep.click();
        await dialog.waitFor({ state: 'hidden' });
        await opener.click();
        await name.waitFor();
        assert.equal(await name.inputValue(), '保留的左侧草稿');
        await name.fill('将被放弃的左侧草稿');
        await page.keyboard.press('Escape');
        await dialog.getByRole('button', { name: '放弃草稿并关闭', exact: true }).click();
        await dialog.waitFor({ state: 'hidden' });
        await opener.click();
        await name.waitFor();
        assert.equal(await name.inputValue(), '');

        await page.setViewportSize({ width: 390, height: 780 });
        await page.waitForFunction(() => {
          const box = document.querySelector('dialog.wb-product-dialog[open]')?.getBoundingClientRect();
          return box && box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight;
        });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        await name.fill(savedProductName);
        await dialog.locator('[data-field="price"]').first().fill('168');
        await dialog.getByRole('button', { name: '保存商品', exact: true }).click();
        await dialog.waitFor({ state: 'hidden' });
        const writes = writeCalls(state);
        assert.equal(writes.length, 1);
        assert.equal(writes[0].body.command, 'save_product');
        assert.equal(writes[0].body.args.fields.name, savedProductName);
        assert.equal(Number(writes[0].body.args.fields.price), 168);
        assert.ok(state.products.some(product => product.body.name === savedProductName), 'fixture stores submitted product');
        const savedRow = page.locator('[data-catalog-product-id].wb-located').filter({ hasText: savedProductName });
        await savedRow.waitFor();
        assert.equal(await savedRow.evaluate(el => el.contains(document.activeElement)), true);
        assert.equal(await input.inputValue(), rightDraft);
        assert.deepEqual(mock.messages, []);
      });

    await check('选择文件后一键新增，重复文件名使用本批精确回执',
      async ({ page, state, mock }) => {
        const input = await setRightDraft(page);
        await page.locator('#open-catalog').click();
        await page.locator('[data-open-product-upload]').click();
        const flow = page.locator('dialog[data-catalog-dialog="upload"] [data-product-material-flow="create"]');
        const submit = flow.getByRole('button', { name: '交给 Agent 新增商品', exact: true });
        await submit.waitFor();
        assert.equal(await submit.isDisabled(), true);
        await flow.locator('input[type=file]').setInputFiles([
          file('相同文件名.txt', '新增资料一。'),
          file('相同文件名.txt', '新增资料二。')
        ]);
        assert.equal(await submit.isEnabled(), true);
        assert.deepEqual(mock.messages, []);
        assert.equal(await input.inputValue(), rightDraft);
        assert.deepEqual(mock.uploads.map(upload => upload.text), ['原对话专用附件。']);
        await submit.evaluate(button => {
          button.click();
          button.click();
        });
        await page.getByText('已受理隔离商品资料任务', { exact: true }).waitFor();
        assert.equal(mock.messages.length, 1);
        const request = assertTask(mock, 0, 'create');
        assert.match(request.body.text, /本次只整理并保存基础商品资料（基础信息、规格、商品知识库和常见问答），暂不开展顾客研究，不生成直播话术或语音。/);
        assert.equal(request.body.material_refs.length, 2);
        assert.deepEqual(mock.uploads.map(upload => upload.text), ['原对话专用附件。', '新增资料一。', '新增资料二。']);
        const run = mock.receipts.get(request.body.client_request_id).run;
        run.status = 'COMPLETED';
        run.document_import_result = {room_id:'room-A', room_name:'测试直播间 A', preparation_id:'prep-A',
          product_ids:['product-A'], activity_found:false, missing_fields:[], conflicts:[]};
        await page.evaluate(() => shengboWorkbench.assistant.refresh());
        await page.getByRole('button',{name:'打开新直播间',exact:true}).click();
        await page.waitForFunction(()=>shengboWorkbench.editors.getContext().preparation_id==='prep-A');
        assert.equal(await flow.count(),0,'successful import overlay closes when opening the created room');
        await restoreRightDraft(page, input);
        assert.deepEqual(writeCalls(state), []);
      });

    await check('可先加入独立对话补充要求，仍限定新增范围和同一批资料',
      async ({ page, state, mock }) => {
        const input = await setRightDraft(page);
        await page.locator('#open-catalog').click();
        await page.locator('[data-open-product-upload]').click();
        const flow = page.locator('dialog[data-catalog-dialog="upload"] [data-product-material-flow="create"]');
        await flow.locator('input[type=file]').setInputFiles(file('本批资料.txt', '只给这次新增任务使用。'));
        await flow.getByRole('button', { name: '上传并加入对话', exact: true }).click();
        await page.locator('.sb-a-attachments').getByText('本批资料.txt', { exact: true }).waitFor();
        assert.deepEqual(mock.messages, []);
        assert.equal(await input.inputValue(), '');
        assert.deepEqual(await page.locator('.sb-a-attachments .sb-a-attachment').allTextContents(), ['本批资料.txt']);
        await input.fill('请优先整理规格。若存在同名商品也请覆盖旧数据。');
        await page.locator('.sb-a-send').click();
        await page.getByText('已受理隔离商品资料任务', { exact: true }).waitFor();
        assert.equal(mock.messages.length, 1);
        const request = assertTask(mock, 0, 'create');
        assert.match(request.body.text, /在上述商品范围内的补充要求：请优先整理规格。/);
        assert.match(request.body.text, /禁止修改已有商品/);
        await restoreRightDraft(page, input);
        assert.deepEqual(writeCalls(state), []);
      });

    await check('部分上传失败时不提交，重试只补上传未成功的文件',
      async ({ page, state, mock }) => {
        await page.locator('#open-catalog').click();
        await page.locator('[data-open-product-upload]').click();
        const flow = page.locator('dialog[data-catalog-dialog="upload"] [data-product-material-flow="create"]');
        const submit = flow.getByRole('button', { name: '交给 Agent 新增商品', exact: true });
        await flow.locator('input[type=file]').setInputFiles([
          file('成功资料.txt', '成功资料。'), file('需重试资料.txt', '失败后补传。')
        ]);
        mock.failUploadOnceFor = '需重试资料.txt';
        await submit.click();
        await flow.getByText(/部分资料尚未上传成功/).waitFor();
        assert.deepEqual(mock.messages, []);
        assert.deepEqual(mock.uploads.map(upload => upload.receipt.name), ['成功资料.txt']);
        await submit.click();
        await page.getByText('已受理隔离商品资料任务', { exact: true }).waitFor();
        assert.deepEqual(mock.uploads.map(upload => upload.receipt.name), ['成功资料.txt', '需重试资料.txt']);
        assert.equal(mock.messages.length, 1);
        assert.equal(assertTask(mock, 0, 'create').body.material_refs.length, 2);
        assert.deepEqual(writeCalls(state), []);
      });

    await check('本场资料只送往选定场次，保留原对话并可核实原提交', async ({page,state,mock}) => {
      const input = await setRightDraft(page);
      await page.evaluate(() => shengboWorkbench.navigate({view:'session',room_id:'room-A',preparation_id:'prep-A'}));
      const flow = page.locator('[data-room-material-flow]');
      if (!await flow.isVisible()) await page.locator('[data-session-settings] > summary').click();
      const submit = flow.getByRole('button',{name:'导入并整理',exact:true});
      assert.equal(await submit.isDisabled(),true);
      await flow.locator('input[type=file]').setInputFiles(file('本场商品与活动.txt','这份资料只导入 A。'));
      await flow.getByLabel('本场资料补充说明（选填）',{exact:true}).fill('满减只适用于文件中的两款。');
      mock.failNextReceipt = true;
      await submit.click();
      await flow.getByText('暂未确认任务是否受理。再次点击会核实同一请求。').waitFor();
      const original = mock.messages[0];
      assert.deepEqual(mock.conversations.get(original.id).scope,state.scope);
      assert.equal(mock.conversations.get(original.id).room_id,'room-A');
      assert.notEqual(original.id,'conversation-A');
      assert.deepEqual(original.body.scope,state.scope);
      assert.deepEqual(original.body.view_context,{view:'session',room_id:'room-A',preparation_id:'prep-A',room_material_action:'import'});
      assert.deepEqual(original.body.observed_versions,[]);
      assert.equal(original.body.selection,null);
      assert.equal(original.body.related_run_id,null);
      assert.match(original.body.text,/满减只适用于文件中的两款/);
      assert.doesNotMatch(original.body.text,/原对话未发送的指令/);
      const uploaded = mock.uploads.find(item=>item.conversationId===original.id);
      assert.deepEqual(uploaded.scope,state.scope);
      assert.deepEqual(original.body.material_refs,[uploaded.receipt.material_ref]);
      await flow.getByRole('button',{name:'核实原提交',exact:true}).click();
      await flow.getByText('Agent 已受理本场导入，可在右侧查看进度和成果。').waitFor();
      assert.deepEqual(mock.messages[1],original,'lost receipt retries exact frozen request and does not upload again');
      assert.equal(mock.uploads.filter(item=>item.conversationId===original.id).length,1);
      const run = mock.receipts.get(original.body.client_request_id).run;
      run.status='COMPLETED';
      run.document_import_result={room_id:'room-A',room_name:'测试直播间 A',preparation_id:'prep-A',
        product_ids:['product-A'],activity_found:true,missing_fields:['campaign_ends_at'],
        conflicts:[{field:'promotion',current:'旧活动',incoming:'满129减15'}]};
      await page.evaluate(() => shengboWorkbench.assistant.refresh());
      const report=page.locator('.sb-a-document-import-report');
      assert.match(await report.innerText(),/活动截止时间/);
      assert.match(await report.innerText(),/旧活动.*满129减15/);
      await report.getByRole('button',{name:'查看本场配置',exact:true}).click();
      assert.equal(await page.evaluate(()=>shengboWorkbench.editors.getContext().preparation_id),'prep-A');
      await restoreRightDraft(page,input);
      assert.deepEqual(writeCalls(state),[]);
    });

    await check('直播间没有本场时沿用现有创建入口，资料随后只上传一次', async ({page,state,mock}) => {
      const created=[];
      await page.route('**/api/prelive/v2/commands',async route=>{
        const body=route.request().postDataJSON();
        if(body.command==='save_session_draft'&&body.args.preparation_id==='prep-B'){
          assert.equal(body.args.expected_revision,state.preparation.ref.revision);
          state.preparation.ref=fixture.ref('preparation','prep-B',state.preparation.ref.revision+1);
          state.preparation.body.draft_ref=state.details.draft.ref;
          state.details.draft.body.draft=body.args.draft;
          return route.fulfill({json:{schema_version:'PRELIVE-2.3',command:body.command,
            operation_id:body.mutation.operation_id,status:'committed',
            data:{preparation_ref:state.preparation.ref,draft_ref:state.details.draft.ref},
            changed_paths:[],invalidated_refs:[],event_cursor:1}});
        }
        if(body.command!=='create_preparation')return route.fallback();
        created.push(body.args);
        assert.equal(body.args.room_id,'room-B');
        state.preparation={...state.preparation,ref:fixture.ref('preparation','prep-B'),
          body:{...state.preparation.body,id:'prep-B',room_id:'room-B',selected_product_refs:[],script_refs:[]}};
        state.details={...state.details,scripts:[],draft:{...state.details.draft,
          ref:fixture.ref('draft','draft-B'),body:{...state.details.draft.body,preparation_id:'prep-B'}},playlist:null};
        return route.fulfill({json:{schema_version:'PRELIVE-2.3',command:body.command,
          operation_id:body.mutation.operation_id,status:'committed',data:{preparation_ref:state.preparation.ref},
          changed_paths:[],invalidated_refs:[],event_cursor:1}});
      });
      await page.evaluate(() => shengboWorkbench.navigate({view:'room-config',room_id:'room-B',preparation_id:null}));
      const flow=page.locator('[data-room-material-flow]');
      await flow.getByText(/导入时会在当前直播间创建本场/).waitFor();
      await flow.locator('input[type=file]').setInputFiles(file('B场资料.txt','本场 B 首次导入。'));
      await flow.getByRole('button',{name:'导入并整理',exact:true}).click();
      await page.getByText('已受理隔离商品资料任务',{exact:true}).waitFor();
      assert.equal(created.length,1);
      assert.equal(mock.messages.length,1);
      assert.equal(mock.uploads.length,1);
      assert.deepEqual(mock.messages[0].body.scope,{kind:'preparation',preparation_id:'prep-B',account_id:'local'});
      assert.equal(mock.messages[0].body.view_context.room_id,'room-B');
      assert.equal(mock.uploads[0].conversationId,mock.messages[0].id);
    });

    await check('本场活动开始和截止时间分别保存，错误时间不写入', async ({page,state}) => {
      await page.evaluate(() => shengboWorkbench.navigate({view:'session',room_id:'room-A',preparation_id:'prep-A'}));
      const session=page.locator('[data-session-settings]');
      const start=session.locator('[data-field=campaign_starts_at]');
      const end=session.locator('[data-field=campaign_ends_at]');
      if(!await start.isVisible())await session.locator('summary').first().click();
      await start.fill('2030-11-11T21:00:00+08:00');
      await end.fill('2030-11-11T20:00:00+08:00');
      const save=session.getByRole('button',{name:'保存本场配置',exact:true});
      await save.click();
      await page.locator('#workbench-notice-message').filter({hasText:'活动截止时间必须晚于活动开始时间。'}).waitFor();
      assert.equal(writeCalls(state).length,0);
      await end.fill('2030-11-11T23:59:00+08:00');
      await save.click();
      await page.waitForFunction(()=>!shengboWorkbench.editors.hasDirty());
      const saved=state.details.draft.body.draft;
      assert.equal(saved.campaign_starts_at,'2030-11-11T21:00:00+08:00');
      assert.equal(saved.campaign_ends_at,'2030-11-11T23:59:00+08:00');
      const now=Date.parse('2030-11-11T22:00:00+08:00');
      assert.equal(reusableDraft(saved,{now}).campaign_starts_at,undefined);
      assert.equal(reusableDraft(saved,{now,includeCampaign:true}).campaign_starts_at,saved.campaign_starts_at);
    });

    await check('更新绑定当前商品修订，回执丢失可核实，商品变更后拦截旧批次',
      async ({ page, state, mock }) => {
        const input = await setRightDraft(page);
        const product = state.products[1];
        await page.locator('#open-catalog').click();
        await openedProduct(page, product.body.name).click();
        await page.locator('dialog[data-catalog-dialog="detail"] [data-open-product-update]').click();
        const flow = page.locator('dialog[data-catalog-dialog="detail"] [data-product-material-flow="update"]');
        const submit = flow.getByRole('button', { name: '交给 Agent 更新此商品', exact: true });
        await flow.locator('input[type=file]').setInputFiles(file('相同文件名.txt', '只用于更新商品 B。'));
        mock.failNextReceipt = true;
        await submit.click();
        await flow.getByText(/受理结果待核实/).waitFor();
        assert.equal(mock.messages.length, 1);
        const request = assertTask(mock, 0, 'update', product.ref);
        assert.doesNotMatch(request.body.text, /暂不开展顾客研究/);
        assert.equal(request.body.material_refs.length, 1);
        assert.equal(await input.inputValue(), '');
        await submit.click();
        await page.getByText('已受理隔离商品资料任务', { exact: true }).waitFor();
        assert.equal(mock.messages.length, 2);
        assert.deepEqual(mock.messages[1], request, 'retry preserves catalog conversation, id, product revision, and files');
        assert.equal(mock.receipts.size, 1);
        await restoreRightDraft(page, input);

        await flow.locator('input[type=file]').setInputFiles(file('第二批.txt', '这份资料绑定商品的旧修订。'));
        product.ref = fixture.ref('product', product.ref.id, product.ref.revision + 1);
        await submit.click();
        await flow.getByText(/选中的内容已出现新修订/).waitFor();
        assert.equal(mock.messages.length, 2, 'stale revision never reaches Agent');
        await restoreRightDraft(page, input);
        assert.deepEqual(writeCalls(state), []);
      });

    await check('入库报告逐商品展示字段、选填建议和精确入口，不重复保存', async ({ page, state, mock }) => {
      Object.assign(state.conversation, { scope: { kind: 'catalog', account_id: 'local' }, room_id: null, preparation_id: null });
      await page.reload();
      await page.evaluate(() => shengboWorkbench.ready);
      const [a, b] = state.products;
      state.run.status = 'WAITING_USER';
      state.run.questions = [{ decision_id:'price-fixture', kind:'catalog_intake_price', status:'OPEN', allow_text:true,
        prompt:'请补充日常售价。', input_placeholder:'输入日常售价金额，例如 89.90',
        options:[{value:'later',label:'稍后补价格（暂不入库）'}],
        preview:{type:'catalog_intake_price', product_name:b.body.name, sku_id:'B-002', currency:'CNY'} }];
      await page.evaluate(() => shengboWorkbench.assistant.refresh());
      const price = page.getByRole('dialog', { name:'补充商品日常售价' });
      await price.getByRole('textbox', {name:'日常售价金额'}).waitFor();
      assert.equal(await price.locator('.sb-a-question-required').count(), 1);
      assert.equal(await price.getByRole('textbox', {name:'日常售价金额'}).getAttribute('inputmode'), 'decimal');
      assert.equal(await price.getByText('待确认，尚未入库', {exact:true}).count(), 1);

      state.run.questions = [{ decision_id: 'approval-fixture', kind: 'catalog_intake_approval', status: 'OPEN',
        prompt: '请核对这次要新增的商品和日常售价。', options: [{value:'save_all', label:'全部分别入库'}] }];
      await page.evaluate(() => shengboWorkbench.assistant.refresh());
      const pending = page.getByRole('dialog', { name: '确认商品入库方案' });
      await pending.getByText('待确认，尚未入库', { exact: true }).waitFor();
      assert.equal(await page.getByRole('region', { name: '商品入库报告' }).count(), 0);
      state.run.questions = [];
      a.body.knowledge_documents = [{ content: '测试知识正文', title: '安装说明', id: 'doc-test-order' }];
      const savedB = structuredClone(b.ref);
      b.ref = fixture.ref('product', b.ref.id, 7);
      b.body.core_selling_points = [];
      b.body.selling_points = '';
      Object.assign(state.run, {
        status: 'COMPLETED',
        summary: '旧摘要不应重复商品实际字段值 178.35',
        messages: [{ id: 'fixture-old-summary', role: 'assistant', run_id: state.run.run_id, content: '旧摘要还包含实际字段值 178.35' }],
        artifacts: [
          { label: '重复的基础字段链接', destination: { view: 'products', product_id: a.ref.id, object_ref: a.ref } },
          { label: '重复的常见问题链接', destination: { view: 'products', product_id: a.ref.id, object_ref: a.ref, field_path: '/faq' } },
          { label: '保留其他工作区成果', destination: { view: 'scripts', preparation_id: 'prep-A' } }
        ],
        catalog_intake_result: { products: [
          { product_id: a.ref.id, product_ref: a.ref, name: a.body.name, sku_id: 'A-001',
            configured_groups: [{label: '基础资料', items: ['商品名称', '日常售价']}, {label: '知识库', items: ['常见问答']}],
            missing_required: [], recommendations: [{ label: '商品主图', reason: '方便在商品库中识别。' }] },
          { product_id: savedB.id, product_ref: savedB, name: b.body.name,
            configured_groups: [{label: '基础资料', items: ['商品名称']}],
            missing_required: ['核心卖点'], required_actions: [{id:'core_selling_points', label:'核心卖点', reason:'至少填写 1 条。'}],
            recommendations: [{label:'常见问答', reason:'可补充顾客最常问的问题。'}] }
        ] }
      });
      await page.evaluate(() => shengboWorkbench.assistant.refresh());
      const report = page.getByRole('region', { name: '商品入库报告' });
      await report.waitFor();
      assert.match(await report.innerText(), /已保存 2 件商品 · 1 件待补必填信息/);
      assert.match(await report.innerText(), /已配置 3 项[\s\S]*基础资料 · 2 项[\s\S]*商品名称、日常售价[\s\S]*必填信息齐全/);
      assert.match(await report.innerText(), /必填信息待补 1 项[\s\S]*核心卖点[\s\S]*选填建议/);
      const backgrounds = await report.locator('.sb-a-catalog-product').first().locator('.sb-a-catalog-group')
        .evaluateAll(groups => groups.map(group => getComputedStyle(group).backgroundColor));
      assert.equal(new Set(backgrounds).size, 3);
      assert.equal(await report.locator('.sb-a-catalog-group--optional').getByText('核心卖点',{exact:true}).count(), 0);
      assert.match(await report.innerText(), /不影响本次入库，也不会单独阻止开播。补充后可让直播讲解和顾客问答更完整/);
      assert.equal(await page.getByText('重复的基础字段链接', { exact: true }).count(), 0);
      assert.equal(await page.getByText('重复的常见问题链接', { exact: true }).count(), 0);
      assert.equal(await page.getByText('保留其他工作区成果', { exact: true }).count(), 1);
      assert.equal(await page.getByText(/178\.35/).count(), 0);
      assert.equal(await report.getByRole('button', {name: /打开商品详情/}).count(), 2);
      await report.getByRole('button', {name: '打开商品详情 · ' + a.body.name}).click();
      const detail = page.locator('dialog[data-catalog-dialog="detail"]');
      await detail.waitFor();
      assert.equal(await detail.getByRole('textbox', { name: '商品名称', exact: true }).inputValue(), a.body.name);
      await detail.getByRole('button', {name: '保存修改', exact: true}).click();
      await page.getByText('没有需要保存的修改。', {exact:true}).waitFor();
      assert.deepEqual(writeCalls(state), []);
      await detail.waitFor({state:'detached'});
      assert.equal((await page.evaluate(() => shengboWorkbench.editors.getContext())).product_id, null);

      const before = mock.messages.length;
      const required = report.locator('.sb-a-catalog-product').filter({has: page.getByRole('heading',{name:b.body.name,exact:true})})
        .locator('.sb-a-catalog-group--required');
      assert.equal(await required.getByRole('button').count(), 3);
      await required.getByRole('button', {name:'稍后去商品库填写 · ' + b.body.name}).click();
      await required.getByText('已选择稍后填写，核心卖点仍待补。',{exact:true}).waitFor();
      assert.match(await required.innerText(), /必填信息待补 1 项/);
      assert.equal(mock.messages.length,before);
      assert.deepEqual(writeCalls(state), []);
      await required.getByRole('button',{name:'查看填写选项 · ' + b.body.name}).click();

      await required.getByRole('button',{name:'自己填写 · ' + b.body.name}).click();
      await detail.waitFor();
      const currentPoints = detail.locator('[data-product-field="/core_selling_points"].wb-located');
      await currentPoints.waitFor();
      assert.equal(await detail.getByRole('textbox',{name:'商品名称',exact:true}).inputValue(),b.body.name);
      assert.equal(await currentPoints.getByRole('button',{name:'添加卖点',exact:true}).evaluate(el=>el===document.activeElement),true);
      await detail.getByRole('button',{name:'关闭',exact:true}).click();
      await detail.waitFor({state:'detached'});
      assert.deepEqual(writeCalls(state), []);

      const input = page.locator('.sb-a-input');
      await input.fill('保留我的另一条普通草稿。');
      await Promise.all([
        page.waitForResponse(response => response.url().endsWith('/messages')),
        required.getByRole('button', {name: '生成并保存核心卖点 · ' + b.body.name}).click()
      ]);
      assert.equal(mock.messages.length, before + 1);
      assert.equal(await input.inputValue(), '保留我的另一条普通草稿。');
      assert.equal((await page.evaluate(() => shengboWorkbench.assistant.getState())).selected, null);
      const sent = mock.messages.at(-1).body;
      assert.equal(sent.intent_mode, 'new_task');
      assert.equal(sent.selection.target.field_path, '/core_selling_points');
      assert.deepEqual(sent.observed_versions, [b.ref]);
      assert.deepEqual(sent.material_refs, []);
      assert.deepEqual(sent.view_context, {
        view:'products', product_id:b.ref.id, room_id:null, preparation_id:null, selling_points_action:'generate_and_save'
      });
      assert.match(sent.text, /优先复用已经验收且适用于所选商品的顾客研究/);

      await required.getByRole('button',{name:'自己填写 · ' + b.body.name}).click();
      await detail.getByRole('button',{name:'添加卖点',exact:true}).click();
      await detail.getByRole('textbox',{name:'卖点内容 1'}).fill('经核实可用于衣物表面清洁。');
      await page.route('**/api/prelive/v2/commands', async route => {
        const response = await route.fetch();
        if (response.ok() && route.request().postDataJSON().command === 'save_product') {
          const saved = state.run.catalog_intake_result.products[1];
          saved.missing_required = [];
          saved.required_actions = [];
          saved.configured_groups.push({label:'讲解与问答',items:['核心卖点']});
        }
        await route.fulfill({response});
      }, {times:1});
      await detail.getByRole('button',{name:'保存修改',exact:true}).click();
      await detail.waitFor({state:'detached'});
      await required.getByText('必填信息齐全',{exact:true}).waitFor();
      assert.equal(await required.getByRole('button').count(), 0);
      assert.doesNotMatch(await report.innerText(), /件待补必填信息/);
      assert.deepEqual(state.products[1].body.core_selling_points,['经核实可用于衣物表面清洁。']);
    });

    await check('报告一键提交、丢回执后核实同一请求，实际保存后定位卖点', async ({page, state, mock}) => {
      Object.assign(state.conversation, {scope:{kind:'catalog',account_id:'local'},room_id:null,preparation_id:null});
      await page.reload();
      await page.evaluate(() => shengboWorkbench.ready);
      const [first, second] = state.products;
      const configured = structuredClone(first);
      configured.ref = fixture.ref('product','product-C',1);
      configured.body.id = configured.ref.id;
      configured.body.name = '已有卖点的款式';
      state.products.push(configured);
      const reportItem = product => ({product_id:product.ref.id, product_ref:product.ref, name:product.body.name,
        configured_groups:[], missing_required:['核心卖点'], required_actions:[{id:'core_selling_points',label:'核心卖点'}], recommendations:[]});
      const items = [reportItem(first), reportItem(second),
        {...reportItem(configured),missing_required:[],required_actions:[]}];
      for (const [index, product] of [first, second].entries()) {
        product.ref = fixture.ref('product',product.ref.id,index+3);
        product.body.core_selling_points = [];
        product.body.selling_points = '';
      }
      Object.assign(state.run,{status:'COMPLETED',catalog_intake_result:{products:items}});
      await page.evaluate(() => shengboWorkbench.assistant.refresh());
      const report = page.getByRole('region',{name:'商品入库报告'});
      const batch = report.getByRole('button',{name:'一起生成核心卖点',exact:true});
      const input = page.locator('.sb-a-input');
      await page.locator('#open-catalog').click();
      mock.failNextReceipt = true;
      await batch.evaluate(button => { button.click(); button.click(); });
      const retry = report.getByRole('button',{name:'核实卖点任务',exact:true});
      await retry.waitFor();
      assert.equal(mock.messages.length,1,'one click action and a double click do not create two requests');
      assert.equal(await input.inputValue(),'');
      assert.equal(await page.locator('.sb-a-selection').isVisible(),false);
      assert.equal(await page.getByText(/点击左侧字段名或模块标题/).count(),0);
      const original = mock.messages[0].body;
      assert.deepEqual(original.observed_versions,[first.ref,second.ref]);
      assert.deepEqual(original.selection.items.map(item=>item.target.field_path),['/core_selling_points','/core_selling_points']);
      assert.deepEqual(original.view_context,{view:'products',product_id:null,room_id:null,preparation_id:null,selling_points_action:'generate_and_save'});
      assert.equal(original.intent_mode,'new_task');
      assert.equal(original.related_run_id,null);
      assert.deepEqual(original.material_refs,[]);
      assert.match(original.text,/研究任务只绑定本次选中的商品/);
      assert.match(original.text,/生成并保存核心卖点/);

      await page.reload();
      await page.evaluate(() => shengboWorkbench.ready);
      await Promise.all([page.waitForResponse(response => response.url().endsWith('/messages')), retry.click()]);
      await retry.waitFor({state:'detached'});
      assert.equal(mock.messages.length,2);
      assert.deepEqual(mock.messages[1],mock.messages[0]);
      assert.equal(mock.receipts.size,1);
      const detail = page.locator('dialog[data-catalog-dialog="detail"]');
      assert.equal(await detail.count(),0,'queue acceptance does not open a field before the actual save');

      await batch.click();
      await page.getByText('已有卖点任务在处理这些商品，请在对话中查看进度。',{exact:true}).waitFor();
      assert.equal(mock.messages.length,2,'an overlapping active request cannot be started again');
      const old = mock.receipts.get(original.client_request_id).run;
      old.input = {...original,view_context:{...original.view_context,selling_points_action:'generate'}};
      old.status = 'WAITING_USER';
      old.questions = [{decision_id:'old-preview-fixture',kind:'selling_points_confirmation',status:'OPEN',
        prompt:'旧任务候选等待确认。', preview:{product_ref:second.ref},
        options:[{value:'approve',label:'确认旧候选'},{value:'reject',label:'不保存旧候选'}]}];
      await page.evaluate(() => shengboWorkbench.assistant.refresh());
      const oldDialog = page.getByRole('dialog',{name:'确认商品卖点'});
      await oldDialog.waitFor();
      await Promise.all([
        page.waitForResponse(response => response.url().endsWith('/messages')),
        batch.evaluate(button => button.click())
      ]);
      await oldDialog.waitFor({state:'hidden'});
      await page.getByRole('button',{name:'查看待确认'}).waitFor();
      assert.equal(mock.messages.length,3);
      const newest = mock.messages[2].body;
      assert.notEqual(newest.client_request_id, original.client_request_id);
      assert.equal(newest.view_context.selling_points_action,'generate_and_save');
      assert.equal(old.status,'WAITING_USER');
      assert.equal(old.questions[0].status,'OPEN');
      const accepted = mock.receipts.get(newest.client_request_id).run;
      await input.fill('这条普通草稿在查看左侧结果时也保留。');
      second.ref = fixture.ref('product', second.ref.id, second.ref.revision + 1);
      second.body.core_selling_points = ['经核实可用于衣物表面清洁。'];
      accepted.artifacts = [{artifact_id:'fixture-saved-points',label:'查看核心卖点',ref:second.ref,destination:{
        authority:'voice',view:'products',product_id:second.ref.id,preparation_id:null,room_id:null,
        object_ref:second.ref,field_path:'/core_selling_points'
      }}];
      await page.evaluate(() => shengboWorkbench.assistant.refresh());
      const field = detail.locator('[data-product-field="/core_selling_points"].wb-located');
      await field.waitFor();
      assert.equal(await detail.getByRole('textbox',{name:'商品名称',exact:true}).inputValue(),second.body.name);
      assert.equal(await field.getByRole('textbox',{name:'卖点内容 1'}).inputValue(),'经核实可用于衣物表面清洁。');
      assert.equal(await input.inputValue(),'这条普通草稿在查看左侧结果时也保留。');
      assert.deepEqual(writeCalls(state),[]);
    });

    await check('商品详情保存成功后关闭并停在列表，失败保留输入，连点只写一次', async ({page, state}) => {
      const product = state.products[0];
      await page.locator('#open-catalog').click();
      await openedProduct(page, product.body.name).click();
      const detail = page.locator('dialog[data-catalog-dialog="detail"]');
      const save = detail.getByRole('button', {name: '保存修改', exact: true});
      const amount = detail.locator('[data-field=price]');
      await amount.fill('188');
      await page.route('**/api/prelive/v2/commands', route => route.fulfill({
        status: 422, json: { error: {code:'TEST_VALIDATION', message:'模拟保存校验失败'} }
      }), {times:1});
      await save.click();
      await detail.getByText('模拟保存校验失败', {exact:true}).waitFor();
      assert.equal(await amount.inputValue(), '188');
      assert.equal(await page.evaluate(() => shengboWorkbench.editors.hasDirty()), true);
      assert.equal(writeCalls(state).length, 0);

      await save.evaluate(button => { button.click(); button.click(); });
      await detail.waitFor({state:'detached'});
      await page.getByText('商品已保存。', {exact:true}).waitFor();
      await page.evaluate(() => shengboWorkbench.editors.refresh({force:true}));
      assert.equal(await detail.count(), 0);
      assert.equal(await page.evaluate(() => shengboWorkbench.editors.getContext().product_id), null);
      assert.equal(state.products[0].body.price, 188);
      assert.equal(state.products[0].ref.revision, 2);
      assert.equal(writeCalls(state).filter(call => call.body?.command === 'save_product').length, 1);
    });

    assert.deepEqual(failures, [], 'catalog journey scenarios with failures');
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
