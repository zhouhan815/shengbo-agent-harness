/* Isolated browser acceptance for the room list, creation, and configuration. */
'use strict';

const assert = require('node:assert/strict');
const { chromium } = require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fixture = require('./test_agent_workbench.cjs');

const rightDraft = '保留在原本场对话的未发送要求。';

async function mockRoomBusiness(page, state) {
  const writes = [];
  const freshPreparations = new Map();
  await page.route('**/api/prelive/v2/queries', route => {
    const { query, args } = route.request().postDataJSON();
    const fresh = freshPreparations.get(args?.preparation_id);
    if (query === 'search_products') {
      const { q = '', cursor = 0, limit = 200 } = args || {};
      const matches = state.products.filter(({ body }) => (body.name + ' ' + body.sku_id).toLowerCase().includes(q.toLowerCase()));
      return route.fulfill({ json: { items: matches.slice(cursor, cursor + limit).map(p => ({ ref: p.ref, ...p.body })),
        next_cursor: cursor + limit < matches.length ? cursor + limit : null } });
    }
    if (query === 'read_session_history') {
      return route.fulfill({ json: { preparations: [state.preparation, ...(state.history || []), ...freshPreparations.values()].map(item => item.preparation || item) } });
    }
    if (query === 'find_audience_research') return route.fulfill({ json: { items: [] } });
    if (query === 'read_preparation' && fresh) return route.fulfill({ json: fresh.preparation });
    if (query === 'get_workspace_snapshot' && fresh) {
      return route.fulfill({ json: {
        voice_event_cursor: 1,
        products: { items: state.products.map(p => ({ ref: p.ref, ...p.body })) },
        rooms: state.rooms,
        preparation: fresh.preparation,
        preparation_details: { scripts: [], draft: fresh.draft || null }
      } });
    }
    return route.fallback();
  });
  await page.route(/\/api\/rooms\/[^/]+\/live-source(?:\?summary_only=true)?$/, route => {
    assert.equal(route.request().method(), 'GET');
    return route.fulfill({ json: { config: {}, status: { state: 'disabled', connected: false }, events: [] } });
  });
  await page.route(/\/api\/rooms\/[^/]+\/live-source\/events(?:\?.*)?$/, route => {
    assert.equal(route.request().method(), 'GET');
    return route.fulfill({ json: { events: [], next_cursor: 0 } });
  });
  await page.route(/\/api\/rooms\/[^/]+\/reply-(knowledge|sessions)$/, route => {
    assert.equal(route.request().method(), 'GET');
    return route.fulfill({ json: { products: [], documents: [], sessions: [] } });
  });
  await page.route('**/api/jobs?room_id=**', route => {
    assert.equal(route.request().method(), 'GET');
    return route.fulfill({ json: { jobs: [] } });
  });
  await page.route(/\/live\/[^/]+(?:\?.*)?$/, route => {
    assert.equal(route.request().method(), 'GET');
    return route.fulfill({ contentType: 'text/html', body: '<title>隔离测试直播预览</title>' });
  });
  await page.route('**/api/prelive/v2/commands', route => {
    const body = route.request().postDataJSON();
    const args = body.args;
    const committed = data => route.fulfill({ json: { command: body.command, status: 'committed', data } });
    if (body.command === 'create_preparation') {
      assert.ok(body.mutation.operation_id);
      assert.equal(args.room_revision, state.rooms.find(room => room.ref.id === args.room_id).ref.revision);
      writes.push(body);
      const id = 'prep-created-' + (freshPreparations.size + 1);
      const preparation = {
        ref: fixture.ref('preparation', id),
        body: { id, room_id: args.room_id, goal: args.goal, status: 'draft', selected_product_refs: [],
          script_refs: [], analysis_refs: [], source_refs: [], recap_refs: [], draft_ref: null }
      };
      freshPreparations.set(id, { preparation });
      return committed({ preparation_ref: preparation.ref });
    }
    const fresh = freshPreparations.get(args?.preparation_id);
    if (fresh && ['update_preparation_products', 'save_session_draft'].includes(body.command)) {
      const preparation = fresh.preparation;
      assert.ok(body.mutation.operation_id);
      assert.equal(args.expected_revision, preparation.ref.revision);
      writes.push(body);
      if (body.command === 'update_preparation_products' && state.rejectFirstSelection) {
        state.rejectFirstSelection = false;
        return route.fulfill({ status: 409, json: { error: { code: 'FIXTURE_REJECTED', message: '测试选品暂未保存' } } });
      }
      preparation.ref = fixture.ref('preparation', preparation.ref.id, preparation.ref.revision + 1);
      if (body.command === 'update_preparation_products') {
        preparation.body.selected_product_refs = args.selected_product_refs;
        return committed({ preparation_ref: preparation.ref });
      }
      const draftRef = fixture.ref('draft', 'draft-' + preparation.ref.id);
      preparation.body.draft_ref = draftRef;
      fresh.draft = { ref: draftRef, body: { preparation_id: preparation.ref.id, draft: args.draft } };
      return committed({ preparation_ref: preparation.ref, draft_ref: draftRef });
    }
    if (body.command !== 'create_room') return route.fallback();
    assert.ok(body.mutation.operation_id);
    assert.equal(body.contract_version, 'PRELIVE-2.3');
    writes.push(body);
    const { name, initial_config } = body.args;
    const id = 'room-new-fixture';
    const room = {
      ref: fixture.ref('config', id),
      body: { id, name, product_ids: [], script_order: [], commerce: {}, loop: true, ...initial_config }
    };
    state.rooms.push(room);
    return committed({ room_ref: room.ref });
  });
  await page.route('**/api/prelive/v2/human/room-settings', route => {
    const body = route.request().postDataJSON();
    const room = state.rooms.find(item => item.ref.id === body.room_id);
    assert.equal(body.expected_revision, room.ref.revision);
    assert.ok(body.operation_id);
    writes.push(body);
    room.body = {
      ...room.body,
      name: body.name,
      version_count: body.version_count,
      default_tts_selection: body.tts_selection
    };
    room.ref = fixture.ref('config', room.ref.id, room.ref.revision + 1);
    return route.fulfill({ json: { room_ref: room.ref } });
  });
  return writes;
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
    const check = async (name, verify, setup) => {
      const state = resetState();
      if (setup) setup(state);
      const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      await context.route('**/*', route => {
        assert.equal(new URL(route.request().url()).origin, base, 'fixture sends no off-origin requests');
        return route.continue();
      });
      const page = await context.newPage();
      page.setDefaultTimeout(6000);
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      try {
        const writes = await mockRoomBusiness(page, state);
        await page.goto(base);
        await page.evaluate(() => shengboWorkbench.ready);
        const board = page.locator('#room-dashboard');
        const sheet = page.locator('#room-sheet');
        const rows = page.locator('#workspace-editor .wb-room-list [data-room-id]');
        const openList = async () => {
          await page.locator('#open-rooms').click();
          await rows.first().waitFor();
        };
        const enter = async roomId => {
          await page.locator('.wb-room-list [data-room-id="' + roomId + '"]').getByRole('button', { name: /进入直播间/ }).click();
          await page.waitForFunction(id => shengboWorkbench.state().context.room_id === id, roomId);
          await board.waitFor();
          await board.locator('[data-room-defaults]').waitFor();
        };
        const section = name => board.locator('[data-room-section="' + name + '"]');
        const openPicker = async () => {
          await board.locator('#room-open-products').click();
          const dialog = page.getByRole('dialog', { name: '选择要讲解的商品' });
          await dialog.waitFor();
          return dialog;
        };
        const input = page.getByLabel('发送给播前助手的要求');
        await input.fill(rightDraft);
        await verify({ page, state, writes, board, sheet, rows, openList, enter, section, openPicker, input });
        assert.deepEqual(state.failures, [], 'no unexpected mock API calls');
        assert.deepEqual(errors, [], 'no browser errors');
        assert.equal(await input.inputValue(), rightDraft);
        assert.equal(await page.getByLabel('选择已有对话').inputValue(), 'conversation-A');
        assert.equal(state.calls.filter(call => call.path.endsWith('/messages') || call.path.endsWith('/control')).length, 0);
        console.log('PASS ' + name);
      } finally {
        await context.close();
      }
    };

    await check('列表、新建配置、创建后直播间面板互相分开',
      async ({ page, state, writes, board, sheet, rows, openList }) => {
        await openList();
        assert.equal(await rows.count(), 2);
        assert.equal(await page.locator('#workspace-editor .wb-room-settings').count(), 0);
        assert.equal(await page.getByRole('button', { name: '创建本场准备', exact: true }).count(), 0);
        assert.equal(await board.isVisible(), false);
        assert.equal(await sheet.isVisible(), false);
        assert.deepEqual(writes, []);
        await page.setViewportSize({ width: 1366, height: 740 });
        await page.getByRole('button', { name: '新建直播间', exact: true }).click();
        const name = page.getByLabel('直播间名称', { exact: true });
        await name.waitFor();
        assert.equal(await name.evaluate(el => el === document.activeElement), true);
        assert.equal(await rows.count(), 0);
        assert.equal(await board.isVisible(), false);
        assert.equal(await page.evaluate(() => shengboWorkbench.state().context.room_id), null);
        assert.equal(await page.getByRole('heading', { name: '新建直播间', exact: true }).count(), 1);
        const desktopGeometry = await page.evaluate(() => {
          const editor = document.getElementById('workspace-editor');
          const submit = editor.querySelector('.wb-room-create button[type=submit]').getBoundingClientRect();
          const provider = editor.querySelector('[aria-label="默认语音供应商"]').getBoundingClientRect();
          const voice = editor.querySelector('[aria-label="默认音色"]').getBoundingClientRect();
          return {
            submitVisible: submit.bottom <= editor.getBoundingClientRect().bottom + 1,
            voicesSideBySide: Math.abs(provider.top - voice.top) <= 1 && voice.left > provider.left
          };
        });
        assert.deepEqual(desktopGeometry, { submitVisible: true, voicesSideBySide: true });
        await name.fill('隔离测试直播间');
        assert.equal(await page.getByLabel('默认话术版本数', { exact: true }).count(), 0);
        await page.getByRole('button', { name: '返回直播间列表', exact: true }).click();
        await rows.first().waitFor();
        assert.deepEqual(writes, []);
        await page.getByRole('button', { name: '新建直播间', exact: true }).click();
        await name.waitFor();
        assert.equal(await name.inputValue(), '隔离测试直播间');
        await page.getByRole('button', { name: '创建直播间', exact: true }).click();
        await page.waitForFunction(() => shengboWorkbench.state().context.room_id === 'room-new-fixture');
        await board.waitFor();
        assert.equal(await rows.count(), 0);
        await board.locator('[data-room-defaults]').waitFor();
        assert.equal(await name.inputValue(), '隔离测试直播间');
        const location = await page.evaluate(() => shengboWorkbench.state().context);
        assert.equal(location.view, 'room-config');
        assert.equal(location.room_id, 'room-new-fixture');
        assert.equal(location.preparation_id, null);
        assert.equal(writes.length, 1);
        assert.equal(writes[0].command, 'create_room');
        assert.equal(writes[0].args.name, '隔离测试直播间');
        assert.equal(writes[0].args.initial_config.version_count, 2);
        assert.equal(state.rooms.at(-1).body.tts_provider, 'system');
        await page.locator('#room-back-list').click();
        await rows.first().waitFor();
        assert.equal(await rows.count(), 3);
        assert.match(await rows.last().innerText(), /隔离测试直播间/);
      });

    await check('准备概况、话术和本场配置同屏编辑，历史弹窗不替换面板',
      async ({ page, state, writes, board, sheet, rows, openList, enter, section }) => {
        const oldDraft = structuredClone(state.details.draft);
        await openList();
        await enter('room-A');
        assert.equal(await rows.count(), 0);
        assert.equal(await page.evaluate(() => shengboWorkbench.state().context.preparation_id), 'prep-A');
        const scripts = board.locator('[data-inline-room-section="scripts"]');
        const session = board.locator('[data-inline-room-section="session"]');
        const overview = board.locator('[data-inline-room-section="overview"]');
        const theme = session.getByLabel('本场主题', { exact: true });
        await overview.getByText('准备概况', { exact: true }).waitFor();
        await scripts.getByRole('button', { name: '将新增要求交给 Agent', exact: true }).waitFor();
        const summary = session.locator('details[data-session-settings] > summary');
        assert.equal(await theme.isVisible(), false);
        assert.ok(await session.evaluate((section, selector) =>
          section.getBoundingClientRect().top < document.querySelector(selector).getBoundingClientRect().top,
          '[data-inline-room-section="scripts"]'));
        await summary.click();
        await theme.waitFor();
        assert.equal(await theme.inputValue(), state.details.draft.body.draft.theme);
        assert.equal(await section('scripts').count(), 0);
        assert.equal(await section('session').count(), 0);
        assert.equal(await sheet.isVisible(), false);
        for (const slot of ['room-defaults-editor', 'room-platform-editor', 'room-broadcast-editor']) {
          await board.locator('#' + slot + ' > *').first().waitFor();
        }
        assert.equal(await board.locator('#room-open-products').isVisible(), true);
        assert.equal(await board.getByLabel('默认话术版本数', { exact: true }).count(), 0);
        assert.equal(await board.locator('#room-platform-tile [data-source-metrics]').count(), 0);
        await board.getByRole('combobox', { name: '播放设备', exact: true }).waitFor();
        const cells = await board.locator('.wb-room-tile').evaluateAll(items =>
          items.map(item => [item.offsetTop,item.offsetHeight]));
        assert.equal(cells.length, 4);
        assert.ok(cells.every(([top,height]) => top === cells[0][0] && height === cells[0][1]));
        assert.equal(await board.locator('#room-products-picker').isVisible(), false);
        assert.deepEqual(writes, []);
        assert.equal(state.calls.filter(call => call.path.endsWith('/commands')).length, 0);
        const defaults = board.locator('#room-defaults-editor');
        const name = defaults.getByLabel('直播间名称', { exact: true });
        assert.equal(await name.inputValue(), state.rooms[0].body.name);
        const provider = defaults.getByLabel('默认语音供应商', { exact: true });
        const feeNotice = defaults.locator('[data-tone="warning"]').getByText(/MiniMax 试听计入接口用量/);
        await provider.selectOption('minimax');
        assert.equal(await feeNotice.isVisible(), true);
        await provider.selectOption('system');
        assert.equal(await feeNotice.count(), 0);
        await theme.fill('暂存于本场的主题');
        await name.fill('重新命名的直播间 A');
        const history = section('preparations');
        await history.scrollIntoViewIfNeeded();
        const beforeOverlay = await board.evaluate(node => node.scrollTop);
        assert.equal(beforeOverlay, 0, 'the utility opener stays at the top');
        await history.click();
        await sheet.waitFor();
        await sheet.locator('[data-preparation-id="prep-A"]').waitFor();
        assert.equal(await board.isVisible(), true);
        await page.keyboard.press('Escape');
        await sheet.waitFor({ state: 'hidden' });
        assert.equal(await history.evaluate(node => node === document.activeElement), true);
        assert.equal(await board.evaluate(node => node.scrollTop), beforeOverlay);
        assert.deepEqual(writes, []);
        assert.equal(await name.inputValue(), '重新命名的直播间 A');
        assert.equal(await theme.inputValue(), '暂存于本场的主题');
        await defaults.getByRole('button', { name: '保存默认设置', exact: true }).click();
        await page.getByText('直播间默认设置已保存，已有本场保持原配置。', { exact: true }).waitFor();
        assert.equal(writes.length, 1);
        assert.equal(writes[0].room_id, 'room-A');
        assert.equal(writes[0].expected_revision, 1);
        assert.deepEqual(state.details.draft, oldDraft);
        await section('preparations').click();
        await sheet.locator('[data-preparation-id="prep-A"]').getByRole('button', { name: '进入本场', exact: true }).click();
        await page.waitForFunction(() => shengboWorkbench.state().context.preparation_id === 'prep-A');
        await board.waitFor();
        await sheet.waitFor({ state: 'hidden' });
        assert.equal(await page.evaluate(() => shengboWorkbench.state().context.preparation_id), 'prep-A');
        assert.equal(await theme.inputValue(), '暂存于本场的主题');
        assert.equal(await scripts.getByLabel('本次新增版本数 ' + state.products[0].body.name).inputValue(), '1');
        assert.equal(await scripts.getByLabel('本次新增版本数 ' + state.products[1].body.name).inputValue(), '3');
        assert.equal(await sheet.isVisible(), false);
      },
      state => {
        state.rooms[0].body.version_count = 3;
        state.details.draft.body.draft.requested_variant_counts = { [state.products[0].ref.id]: 1 };
      });

    await check('顾客建议、变更记录和测试评测各自在弹窗读取，关闭恢复面板位置',
      async ({ page, state, writes, board, sheet, openList, enter, section }) => {
        await openList();
        await enter('room-A');
        const execution = board.locator('#room-broadcast-editor audio').first();
        await execution.waitFor({ state: 'attached' });
        await execution.evaluate(node => { node.dataset.fixturePersistentAudio = 'yes'; });
        const before = await page.evaluate(() => shengboWorkbench.state().context);
        for (const [view, heading] of [['insights', '顾客、场景与卖点建议'], ['changes', '变更记录与引用']]) {
          const opener = section(view);
          await opener.scrollIntoViewIfNeeded();
          const scroll = await board.evaluate(node => node.scrollTop);
          await opener.click();
          await sheet.getByRole('heading', { name: heading, exact: true }).waitFor();
          assert.equal(await board.isVisible(), true);
          await sheet.getByRole('button', { name: '关闭', exact: true }).click();
          await sheet.waitFor({ state: 'hidden' });
          assert.equal(await opener.evaluate(node => node === document.activeElement), true);
          assert.equal(await board.evaluate(node => node.scrollTop), scroll);
        }
        const opener = section('comments');
        await opener.scrollIntoViewIfNeeded();
        const scroll = await board.evaluate(node => node.scrollTop);
        await opener.click();
        const diagnostics = page.getByRole('dialog', { name: '互动与诊断' });
        await diagnostics.getByRole('tab', { name: '评论测试', selected: true }).waitFor();
        await diagnostics.getByRole('tab', { name: '任务与评测' }).click();
        await diagnostics.getByText('当前直播间暂无后台生成任务。', { exact: true }).waitFor();
        await page.keyboard.press('Escape');
        await diagnostics.waitFor({ state: 'hidden' });
        assert.equal(await opener.evaluate(node => node === document.activeElement), true);
        assert.equal(await board.evaluate(node => node.scrollTop), scroll);
        assert.equal(await execution.getAttribute('data-fixture-persistent-audio'), 'yes');
        assert.deepEqual(await page.evaluate(() => shengboWorkbench.state().context), before);
        assert.deepEqual(writes, []);
        assert.equal(state.calls.filter(call => call.path.endsWith('/commands')).length, 0);
      });

    await check('单品列表和 Agent 成果定位到同一件本场商品，生成请求绑定已保存草案',
      async ({ page, state, writes, board, sheet, openList, enter }) => {
        await openList();
        await enter('room-A');
        const product = state.products[1];
        const rows = board.locator('#room-selected-products li');
        assert.equal(await rows.count(), 2);
        const row = rows.filter({ hasText: product.body.name });
        await row.getByRole('button', { name: '本场设置 ' + product.body.name, exact: true }).click();
        const modal = page.locator('dialog[data-session-product-id][open]');
        assert.equal(await modal.getAttribute('data-session-product-id'), product.ref.id);
        await page.keyboard.press('Escape');
        await modal.waitFor({ state: 'hidden' });
        const draft = state.details.draft.ref;
        state.run.artifacts = [{
          label: '查看指定商品本场设置',
          ref: draft,
          destination: { view: 'session', room_section: 'session', room_id: 'room-A', preparation_id: 'prep-A',
            product_id: product.ref.id, object_ref: draft, field_path: '/draft/commerce/product_overrides/' + product.ref.id }
        }];
        state.run.version++;
        await page.evaluate(() => shengboWorkbench.assistant.refresh());
        await page.getByRole('button', { name: '查看指定商品本场设置', exact: true }).click();
        await modal.waitFor();
        assert.equal(await modal.getAttribute('data-session-product-id'), product.ref.id);
        assert.equal(await board.locator('[data-session-settings]').evaluate(node => node.open), false);
        assert.equal(await sheet.isVisible(), false);
        await page.keyboard.press('Escape');
        const scripts = board.locator('[data-inline-room-section="scripts"]');
        const variants = scripts.getByLabel('本次新增版本数 ' + product.body.name);
        await variants.fill('4');
        const input = page.locator('.sb-a-input');
        assert.equal(await input.inputValue(), rightDraft);
        await input.fill('');
        await scripts.getByRole('button', { name: '将新增要求交给 Agent', exact: true }).click();
        await page.waitForFunction(() => document.querySelector('.sb-a-input')?.value?.includes('"draft_ref"'));
        const manifest = JSON.parse((await input.inputValue()).split('当前明确范围与已有稿引用：\n').at(-1));
        assert.deepEqual(manifest.draft_ref, draft);
        assert.equal(manifest.product_counts.find(item => item.product_ref.id === product.ref.id).count, 4);
        await input.fill(rightDraft);
        assert.deepEqual(writes, []);
      });

    await check('Agent 的直播间设置成果定位到对应房间的行内默认设置',
      async ({ page, state, writes, board, sheet, openList }) => {
        await openList();
        const room = state.rooms[1];
        state.run.artifacts = [{
          label: '查看直播间 B 的设置',
          ref: room.ref,
          destination: { view: 'room-config', room_section: 'basic', room_id: room.ref.id, preparation_id: null, object_ref: room.ref }
        }];
        state.run.version++;
        await page.evaluate(() => shengboWorkbench.assistant.refresh());
        await page.getByRole('button', { name: '查看直播间 B 的设置', exact: true }).click();
        await page.waitForFunction(id => shengboWorkbench.state().context.room_id === id, room.ref.id);
        const name = board.locator('#room-defaults-editor').getByLabel('直播间名称', { exact: true });
        await name.waitFor();
        await page.waitForFunction(() => document.activeElement?.name === 'room-name');
        assert.equal(await sheet.isVisible(), false);
        assert.equal(await name.inputValue(), room.body.name);
        const location = await page.evaluate(() => shengboWorkbench.state().context);
        assert.equal(location.room_id, room.ref.id);
        assert.equal(location.preparation_id, null);
        assert.equal(location.room_section, 'basic');
        assert.deepEqual(writes, []);
      });

    await check('首选商品须显式保存，连续点击只创建一次并从标准语音配置起步',
      async ({ page, state, writes, board, sheet, openList, enter, openPicker }) => {
        const room = state.rooms[1];
        await openList();
        await enter(room.ref.id);
        assert.deepEqual(writes, []);
        assert.equal(state.calls.filter(call => call.path.endsWith('/commands')).length, 0);
        assert.equal(await page.evaluate(() => shengboWorkbench.state().context.preparation_id), null);
        const defaults = board.locator('#room-defaults-editor');
        assert.equal(await defaults.getByLabel('默认语音供应商', { exact: true }).inputValue(), 'system');
        const dialog = await openPicker();
        const save = dialog.getByRole('button', { name: '保存选品', exact: true });
        assert.equal(await save.isDisabled(), true);
        const product = dialog.getByRole('checkbox', { name: state.products[1].body.name });
        await product.check();
        assert.deepEqual(writes, []);
        await save.evaluate(button => { button.click(); button.click(); });
        await page.locator('#room-products-feedback').getByText('选品已保存。').waitFor();
        assert.deepEqual(writes.map(write => write.command), ['create_preparation', 'update_preparation_products', 'save_session_draft']);
        assert.equal(new Set(writes.map(write => write.mutation.operation_id)).size, 3);
        assert.equal(writes[0].args.room_id, room.ref.id);
        assert.deepEqual(writes[1].args.selected_product_refs, [state.products[1].ref]);
        assert.deepEqual(writes[2].args.draft.tts_selection, room.body.default_tts_selection);
        assert.deepEqual(writes[2].args.draft.commerce, {});
        assert.deepEqual(writes[2].args.draft.requested_variant_counts, { [state.products[1].ref.id]: 4 });
        assert.equal(await page.evaluate(() => shengboWorkbench.state().context.preparation_id), 'prep-created-1');
        assert.equal(await sheet.isVisible(), false);
        assert.equal(await dialog.isVisible(), false);
        await board.locator('#room-selected-products').getByText(state.products[1].body.name).waitFor();
        await openPicker();
        await product.waitFor();
        assert.equal(await product.isChecked(), true);
        assert.equal(await save.isDisabled(), true);
        await page.keyboard.press('Escape');
      },
      state => Object.assign(state.rooms[1].body, {
        tts_provider: 'minimax', tts_voice: 'female-shaonv', tts_model: 'speech-2.8-turbo',
        default_tts_selection: { provider: 'system', voice: 'Huihui', model: 'system' },
        version_count: 4,
        commerce: { scene: '上一场活动', promotion: '不应带入默认设置' }
      }));

    await check('默认进入仍有效的已采用场次，历史入口和冲突保护保持独立',
      async ({ page, state, writes, openList, enter, board, openPicker }) => {
        await openList();
        await enter('room-A');
        assert.equal(await page.evaluate(() => shengboWorkbench.state().context.preparation_id), 'prep-A');
        const explicit = await page.evaluate(() => shengboWorkbench.editors.resolveDefaultRoomPreparation({
          room_id: 'room-A', preparation_id: 'prep-newer'
        }));
        assert.equal(explicit, 'prep-newer');
        const adoptedRoom = state.rooms[0];
        delete adoptedRoom.body.adopted_publication_ref;
        const fallback = await page.evaluate(() => shengboWorkbench.editors.resolveDefaultRoomPreparation({ room_id: 'room-A' }));
        assert.equal(fallback, 'prep-newer');
        state.preparation.ref = fixture.ref('preparation', 'prep-A', 2);
        const dialog = await openPicker();
        const product = dialog.getByRole('checkbox', { name: state.products[1].body.name });
        await product.uncheck();
        await board.locator('#room-save-products').click();
        await page.locator('#room-products-feedback').getByText('本场已更新，请重新读取选品后核对。').waitFor();
        assert.match(await board.locator('#room-board-content').innerText(), /1 件已选.*尚未保存/);
        await board.locator('#room-reload-products').click();
        await board.locator('#room-reload-products').waitFor({ state: 'hidden' });
        await openPicker();
        await product.waitFor();
        assert.equal(await product.isChecked(), true);
        assert.equal(await board.locator('#room-save-products').isDisabled(), true);
        await page.keyboard.press('Escape');
        assert.deepEqual(writes, []);
        assert.equal(state.calls.filter(call => call.path.endsWith('/commands')).length, 0);
      },
      state => {
        Object.assign(state.rooms[0].body, {
          adopted_publication_ref: fixture.ref('publication', 'old-publication'),
          adopted_preparation_id: 'prep-A'
        });
        state.history = [
          { ref: fixture.ref('preparation', 'prep-newer'), body: { room_id: 'room-A', status: 'draft' } },
          { ref: fixture.ref('preparation', 'prep-archived'), body: { room_id: 'room-A', status: 'archived' } }
        ];
      });

    await check('创建成功但选品暂未保存时保留选择，重试沿用同一场次',
      async ({ page, state, writes, board, openList, enter, openPicker }) => {
        await openList();
        await enter('room-B');
        const dialog = await openPicker();
        const product = dialog.getByRole('checkbox', { name: state.products[1].body.name });
        const save = dialog.getByRole('button', { name: '保存选品', exact: true });
        const feedback = page.locator('#room-products-feedback');
        await product.check();
        await save.click();
        await feedback.getByText('测试选品暂未保存').waitFor();
        assert.equal(await page.evaluate(() => shengboWorkbench.state().context.preparation_id), 'prep-created-1');
        assert.match(await board.locator('#room-selected-products').innerText(), new RegExp(state.products[1].body.name));
        await board.locator('#room-retry-products').click();
        await feedback.getByText('选品已保存。').waitFor();
        assert.deepEqual(writes.map(write => write.command), [
          'create_preparation', 'update_preparation_products', 'update_preparation_products', 'save_session_draft'
        ]);
        assert.deepEqual(writes[2].args, writes[1].args);
        assert.equal(await board.locator('#room-retry-products').isVisible(), false);
      },
      state => { state.rejectFirstSelection = true; });

    await check('选品弹窗支持全库搜索、跨页勾选、窄屏分页和键盘取消',
      async ({ page, writes, board, openList, enter, openPicker }) => {
        await openList();
        await enter('room-B');
        const opener = board.locator('#room-open-products');
        const dialog = await openPicker();
        const search = dialog.getByRole('searchbox');
        const options = dialog.getByRole('group', { name: '本页商品' });
        const pager = dialog.getByRole('navigation', { name: '选品分页' });
        assert.equal(await search.evaluate(input => input === document.activeElement), true);
        await pager.getByText('第 1 页', { exact: true }).waitFor();
        assert.equal(await options.getByRole('checkbox').count(), 8);
        assert.equal(await options.evaluate(list => list.scrollHeight <= list.clientHeight + 1), true);
        await options.getByRole('checkbox', { name: '足弓支撑拖鞋 A' }).check();
        await pager.getByRole('button', { name: '下一页' }).click();
        await pager.getByText('第 2 页', { exact: true }).waitFor();
        assert.equal(await options.getByRole('checkbox').count(), 5);
        await search.fill('SKU-13');
        const matched = options.getByRole('checkbox', { name: '分页测试商品 13' });
        await matched.check();
        assert.equal(await options.getByRole('checkbox').count(), 1);
        await search.fill('');
        await dialog.getByRole('checkbox', { name: '仅看已选' }).check();
        assert.equal(await options.getByRole('checkbox', { checked: true }).count(), 2);
        await dialog.getByRole('checkbox', { name: '仅看已选' }).uncheck();
        await page.setViewportSize({ width: 390, height: 600 });
        await page.waitForFunction(() => document.querySelectorAll('#room-product-options input').length === 5);
        const geometry = await dialog.evaluate(node => {
          const rect = node.getBoundingClientRect(), list = node.querySelector('#room-product-options');
          return { fits: rect.left >= 0 && rect.right <= innerWidth && rect.bottom <= innerHeight,
            shellFits: node.scrollHeight <= node.clientHeight + 1, rowsFit: list.scrollHeight <= list.clientHeight + 1 };
        });
        assert.deepEqual(geometry, { fits: true, shellFits: true, rowsFit: true });
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.keyboard.press('Escape');
        assert.equal(await dialog.isVisible(), false);
        assert.equal(await opener.evaluate(button => button === document.activeElement), true);
        assert.match(await board.locator('#room-board-content').innerText(), /0 件已选/);
        assert.deepEqual(writes, []);
      },
      state => {
        for (let index = 3; index <= 13; index++) {
          const id = 'product-page-' + index;
          state.products.push({ ref: fixture.ref('product', id),
            body: { ...state.products[0].body, id, name: '分页测试商品 ' + index, sku_id: 'SKU-' + index } });
        }
      });

    await check('窄屏直播间列表每页最多五项，不横向溢出',
      async ({ page, writes, rows, openList }) => {
        await page.setViewportSize({ width: 390, height: 760 });
        await page.locator('#mobile-work').click();
        await openList();
        assert.equal(await rows.count(), 5);
        const pager = page.getByRole('navigation', { name: '直播间分页' });
        const previous = pager.getByRole('button', { name: '上一页' });
        const next = pager.getByRole('button', { name: '下一页' });
        assert.equal(await previous.isDisabled(), true);
        await next.click();
        assert.equal(await rows.count(), 2);
        assert.equal(await next.isDisabled(), true);
        await previous.click();
        assert.equal(await rows.count(), 5);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        assert.deepEqual(writes, []);
      },
      state => {
        for (let index = 3; index <= 7; index++) {
          const id = 'room-' + index;
          state.rooms.push({ ref: fixture.ref('config', id), body: { id, name: '列表测试直播间 ' + index } });
        }
      });
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
