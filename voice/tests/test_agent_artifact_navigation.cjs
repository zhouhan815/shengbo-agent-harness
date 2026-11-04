/* Browser acceptance for Agent result routing over the private workbench fixture. */
'use strict';

const assert = require('node:assert/strict');
const { chromium } = require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fixture = require('./test_agent_workbench.cjs');

const sharedDocumentId = 'doc-shared';
const doc = (id, content) => ({ id, title: '清洁说明', content });

function result(label, product, target = {}) {
  return {
    label,
    ref: product.ref,
    destination: {
      view: 'products',
      room_id: null,
      preparation_id: null,
      product_id: product.ref.id,
      object_ref: product.ref,
      field_path: '/knowledge_documents',
      knowledge_document_id: sharedDocumentId,
      ...target
    }
  };
}

function mutations(state) {
  return state.calls.filter(call => call.path.endsWith('/messages') ||
    call.path.endsWith('/commands') || call.path.endsWith('/control') ||
    call.path.includes('/human/') ||
    (call.method === 'POST' && call.path.endsWith('/conversations')));
}

async function mockDocumentSchema(page) {
  await page.route('**/api/prelive/v2/queries', async route => {
    if (route.request().postDataJSON().query !== 'get_business_schema') return route.continue();
    const response = await route.fetch();
    const json = await response.json();
    json.schema.product.properties.knowledge_documents.items = {
      type: 'object',
      properties: {
        id: { type: 'string' },
        title: { type: 'string', maxLength: 120 },
        content: { type: 'string', maxLength: 15000 }
      }
    };
    await route.fulfill({ response, json });
  });
}

async function run() {
  const { server, resetState, ref } = fixture;
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
    const check = async (name, verify, setup) => {
      const state = resetState();
      const [productA, productB] = state.products;
      productA.body.knowledge_documents = [
        doc('doc-first', 'A 的第一条同名文档。'),
        doc(sharedDocumentId, 'A 的目标文档旧正文。')
      ];
      productB.body.knowledge_documents = [doc(sharedDocumentId, 'B 的同编号文档。')];
      const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      try {
        await page.route(/\/live\/[a-zA-Z0-9_-]+(?:\?.*)?$/, route => route.fulfill({
          contentType: 'text/html', body: '<!doctype html><title>隔离直播预览</title>'
        }));
        await mockDocumentSchema(page);
        if (setup) await setup(page, state);
        await page.goto(base);
        await page.evaluate(() => shengboWorkbench.ready);
        const navigate = (productId, prep = null) => page.evaluate(
          ({ productId, prep }) => shengboWorkbench.navigate({
            view: 'products',
            product_id: productId,
            room_id: prep ? 'room-A' : null,
            preparation_id: prep
          }),
          { productId, prep }
        );
        const configureRoom = () => page.evaluate(() => shengboWorkbench.navigate({
          view: 'room-config', room_id: 'room-A', room_section: null, preparation_id: 'prep-A', product_id: null
        }));
        const publish = async artifact => {
          state.run.artifacts.push(artifact);
          state.run.version++;
          await page.evaluate(() => shengboWorkbench.assistant.refresh());
          const card = page.getByRole('button', { name: artifact.label, exact: true });
          await card.waitFor();
          return card;
        };
        const detail = page.locator('[data-catalog-dialog="detail"]');
        const row = id => detail.locator('[data-product-document-id="' + id + '"]');
        const content = id => row(id).locator('[data-field="content"]');
        const field = name => detail.locator('[data-field="' + name + '"]').first();
        const located = page.locator('.wb-located');
        const waitProduct = id => page.waitForFunction(
          productId => shengboWorkbench.state().context.product_id === productId,
          id
        );
        await verify({ page, state, productA, productB, navigate, configureRoom, publish, detail, row, content, field, located, waitProduct });
        assert.deepEqual(errors, [], 'browser errors');
        assert.deepEqual(state.failures, [], 'mock route errors');
        assert.deepEqual(mutations(state), [], 'result navigation is read-only');
        console.log('PASS ' + name);
      } catch (error) {
        failures.push(name);
        console.error('FAIL ' + name, error);
      } finally {
        await context.close();
      }
    };

    await check('成果卡刷新已有缓存，按商品与文档 ID 定位同名文档并保留另一商品草稿',
      async ({ page, state, productA, productB, navigate, publish, detail, row, content, field, located, waitProduct }) => {
        await navigate(productA.ref.id);
        assert.equal(await content(sharedDocumentId).inputValue(), 'A 的目标文档旧正文。');
        await detail.getByRole('button', { name: '关闭', exact: true }).click();
        await waitProduct(null);
        await navigate(productB.ref.id);
        await content(sharedDocumentId).fill('B 的未保存文档草稿。');
        await detail.getByRole('button', { name: '关闭', exact: true }).click();
        await detail.getByRole('button', { name: '保留草稿并关闭', exact: true }).click();
        await waitProduct(null);
        await page.getByRole('button', { name: '上传资料新增商品', exact: true }).click();
        const upload = page.locator('[data-catalog-dialog="upload"]');
        await upload.waitFor();
        const artifact = result('查看 A 的清洁说明', productA);
        productA.ref = ref('product', productA.ref.id, 2);
        productA.body.knowledge_documents[1].content = 'A 的目标文档当前正文。';
        const card = await publish(artifact);
        const before = state.calls.length;
        await card.click();
        await waitProduct(productA.ref.id);
        assert.equal(await upload.count(), 0, 'a saved result replaces a clean upload dialog');
        await row(sharedDocumentId).and(located).waitFor();
        assert.equal(await located.count(), 1);
        assert.equal(await content(sharedDocumentId).inputValue(), 'A 的目标文档当前正文。');
        assert.equal(await field('name').inputValue(), productA.body.name);
        assert.equal(await content(sharedDocumentId).evaluate(el => el === document.activeElement), true);
        assert.ok(state.calls.slice(before).some(call => call.body?.query === 'read_product' &&
          (call.body.args.product_id || call.body.args.ref?.id) === productA.ref.id), 'refresh re-reads target product');
        await page.getByText(/当前版本.*原成果/).first().waitFor();
        await detail.getByRole('button', { name: '关闭', exact: true }).click();
        await waitProduct(null);
        await navigate(productB.ref.id);
        assert.equal(await content(sharedDocumentId).inputValue(), 'B 的未保存文档草稿。');
      });

    await check('文档移除和商品归档有明确降级，旧成果卡仍能打开对应商品字段',
      async ({ page, productA, productB, navigate, publish, row, field, located, waitProduct }) => {
        await navigate(productB.ref.id);
        const removed = result('查看已移除的知识文档', productA);
        productA.ref = ref('product', productA.ref.id, 2);
        productA.body.knowledge_documents.pop();
        await (await publish(removed)).click();
        await waitProduct(productA.ref.id);
        await page.locator('[data-product-field="/knowledge_documents"].wb-located').waitFor();
        assert.equal(await row('doc-first').and(located).count(), 0);
        assert.equal(await row(sharedDocumentId).count(), 0);
        await page.getByText(/知识文档.*已打开商品/).first().waitFor();

        const legacy = {
          label: '旧成果卡查看商品 B 常见问题',
          ref: productB.ref,
          destination: { view: 'products', product_id: productB.ref.id, field_path: '/faq' }
        };
        await (await publish(legacy)).click();
        await waitProduct(productB.ref.id);
        await page.locator('[data-product-field="/faq"].wb-located, [data-field="faq"].wb-located').waitFor();
        assert.equal(await field('name').inputValue(), productB.body.name);
        assert.equal(await field('faq').inputValue(), productB.body.faq);

        const archived = result('查看已归档商品的文档', productA);
        productA.body.archived = true;
        await (await publish(archived)).click();
        await waitProduct(null);
        await page.getByText(/成果关联的商品已归档.*已打开商品知识库/).first().waitFor();
        assert.equal(await row(sharedDocumentId).count(), 0);
        assert.equal(await page.locator('.wb-product-form').count(), 0);
      });

    await check('同一商品不同上下文的编辑草稿阻止成果刷新，草稿处理后可读取已保存正文',
      async ({ page, productA, navigate, publish, content, row, located }) => {
        await navigate(productA.ref.id, 'prep-A');
        await content(sharedDocumentId).fill('A 的未保存文档草稿。');
        const card = await publish(result('查看与编辑草稿同属商品的成果', productA));
        productA.ref = ref('product', productA.ref.id, 2);
        productA.body.knowledge_documents[1].content = 'Agent 已保存但尚未打开的正文。';
        await card.click();
        await page.locator('#workbench-notice').filter({ hasText: /未保存/ }).waitFor();
        assert.equal(await content(sharedDocumentId).inputValue(), 'A 的未保存文档草稿。');
        assert.equal(await page.evaluate(() => shengboWorkbench.state().context.preparation_id), 'prep-A');
        await page.evaluate(() => shengboWorkbench.editors.discardDirty());
        await card.click();
        await row(sharedDocumentId).and(located).waitFor();
        assert.equal(await content(sharedDocumentId).inputValue(), 'Agent 已保存但尚未打开的正文。');
      });

    await check('历史成果跳转保留原会话绑定，对话间不串草稿或精确选区',
      async ({ page, productA, productB, navigate, publish, row, content, located, waitProduct }) => {
        await navigate(productB.ref.id, 'prep-A');
        await publish(result('当前会话 B 的成果', productB));
        const conversations = page.getByLabel('选择已有对话');
        const input = page.getByLabel('发送给播前助手的要求');
        const selections = page.locator('.sb-a-selection-chip');
        await page.locator('[data-select-field="faq"]').click();
        await input.fill('当前会话的待发草稿。');
        assert.equal(await selections.count(), 1);

        await conversations.selectOption('conversation-history');
        await page.getByRole('button', { name: '历史会话 A 的知识文档', exact: true }).waitFor();
        assert.equal(await input.inputValue(), '');
        assert.equal(await selections.count(), 0);
        await input.fill('历史会话自己的草稿。');
        await page.getByRole('button', { name: '历史会话 A 的知识文档', exact: true }).click();
        await waitProduct(productA.ref.id);
        await row(sharedDocumentId).and(located).waitFor();
        assert.equal(await content(sharedDocumentId).inputValue(), 'A 的目标文档旧正文。');
        assert.equal(await conversations.inputValue(), 'conversation-history');
        assert.equal(await input.inputValue(), '历史会话自己的草稿。');
        const selected = await page.evaluate(() => shengboWorkbench.assistant.getState().conversation);
        assert.equal(selected.conversation_id, 'conversation-history');
        assert.equal(selected.preparation_id, 'prep-history');
        await page.locator('[data-scope-mismatch="true"]').waitFor();

        await conversations.selectOption('conversation-A');
        await page.getByRole('button', { name: '当前会话 B 的成果', exact: true }).waitFor();
        assert.equal(await input.inputValue(), '当前会话的待发草稿。');
        assert.equal(await selections.count(), 1);
        await conversations.selectOption('conversation-history');
        await page.getByRole('button', { name: '历史会话 A 的知识文档', exact: true }).waitFor();
        assert.equal(await input.inputValue(), '历史会话自己的草稿。');
        assert.equal(await selections.count(), 0);
      },
      async (page, state) => {
        const history = {
          conversation_id: 'conversation-history',
          id: 'conversation-history',
          title: '历史准备会话',
          room_id: 'room-B',
          preparation_id: 'prep-history',
          scope: { kind: 'preparation', preparation_id: 'prep-history', account_id: 'local' }
        };
        const run = {
          run_id: 'run-history',
          id: 'run-history',
          status: 'COMPLETED',
          summary: '历史商品资料已保存',
          artifacts: [result('历史会话 A 的知识文档', state.products[0])]
        };
        await page.route('**/api/assistant/v2/conversations', route => {
          if (route.request().method() !== 'GET') return route.continue();
          return route.fulfill({ json: { items: [state.conversation, history] } });
        });
        await page.route('**/api/assistant/v2/conversations/conversation-history', route =>
          route.fulfill({ json: {
            ...history,
            messages: [{ message_id: 'history-message', role: 'user', text: '整理商品资料', run_id: run.run_id }],
            runs: [run]
          } }));
        await page.route('**/api/assistant/v2/runs/run-history', route => route.fulfill({ json: run }));
        await page.route('**/api/assistant/v2/runs/run-history/events?*', route =>
          route.fulfill({ json: { events: [], next_cursor: 0, has_more: false } }));
      });

    await check('自动跟随命中具体文档并保留对话操作焦点',
      async ({ page, productA, navigate, publish, row, located, waitProduct }) => {
        await navigate(null);
        const follow = page.getByRole('checkbox', { name: '自动跟随当前任务成果' });
        await follow.uncheck();
        await follow.check();
        const input = page.getByLabel('发送给播前助手的要求');
        await input.focus();
        await publish(result('自动跟随 A 的知识文档', productA));
        await waitProduct(productA.ref.id);
        await row(sharedDocumentId).and(located).waitFor();
        assert.equal(await input.evaluate(el => el === document.activeElement), true);
      });
    await check('已保存话术成果打开对应版本与模块的阅读器',
      async ({ page, state, publish }) => {
        const script = state.details.scripts[0];
        const follow = page.getByRole('checkbox', { name: '自动跟随当前任务成果' });
        await follow.uncheck();
        await follow.check();
        const input = page.getByLabel('发送给播前助手的要求');
        await input.focus();
        const card = await publish({
          label: '查看已保存话术',
          ref: script.ref,
          destination: {
            authority: 'voice', object_ref: script.ref, view: 'scripts',
            room_id: 'room-A', preparation_id: 'prep-A',
            script_id: script.ref.id, module_key: 'campaign'
          }
        });
        const reader = page.locator('.wb-script-reader[data-script-id="script-A1"]');
        const row = page.locator('[data-inline-room-section="scripts"] [data-script-id="script-A1"]');
        await row.and(page.locator('.wb-located')).waitFor();
        assert.equal(await reader.count(), 0, 'automatic following reveals the row without opening a modal');
        assert.equal(await input.evaluate(element => element === document.activeElement), true);
        await card.click();
        await reader.waitFor();
        assert.equal(await reader.evaluate(element => element.matches('dialog:modal')), true);
        assert.equal(await page.locator('#room-dashboard').isVisible(), true, 'opening the version keeps the room canvas mounted');
        assert.equal(await row.count(), 1);
        const module = reader.locator('[data-module="campaign"].wb-located');
        await module.waitFor();
        assert.match(await module.innerText(), /本场活动价一百零七新台币/);
        assert.equal(await reader.locator('.wb-located').count(), 1);
        await reader.getByRole('button', { name: '关闭话术详情', exact: true }).click();
        assert.equal(await page.locator('#room-dashboard').isVisible(), true);
        await card.click();
        await module.locator('[data-select-module="campaign"]').click();
        await reader.waitFor({ state: 'detached' });
        assert.equal(await input.evaluate(element => element === document.activeElement), true, 'selecting a module returns focus to the Agent composer');
        const selection = await page.evaluate(() => shengboWorkbench.assistant.getState().selected);
        const [selected] = Array.isArray(selection) ? selection : [selection];
        assert.equal(selected.target.object_ref.id, script.ref.id);
        assert.equal(selected.target.module_key, 'campaign');
      });

    const researchRecords = ['research-other', 'research-target'].map(id => ({
      ref: ref('analysis', id),
      body: {
        preparation_id: 'prep-A',
        role: 'audience_scenes',
        accepted: true,
        report: { summary: id === 'research-target' ? '本场新保存的顾客建议。' : '另一份同类建议。', hypotheses: [] }
      }
    }));
    await check('已保存的顾客研究成果刷新本场，并打开对应建议卡',
      async ({ page, state, configureRoom, publish, located }) => {
        await page.getByRole('checkbox', { name: '自动跟随当前任务成果' }).uncheck();
        await configureRoom();
        await page.getByRole('button', { name: '顾客与卖点', exact: true }).click();
        const other = page.locator('[data-analysis-id="research-other"]');
        await other.waitFor();
        await page.locator('#room-close-section').click();

        const target = researchRecords[1];
        state.preparation.body.analysis_refs.push(target.ref);
        await (await publish({
          label: '查看已保存的顾客与卖点建议',
          ref: target.ref,
          destination: {
            authority: 'voice', object_ref: target.ref, view: 'insights',
            room_id: 'room-A', preparation_id: 'prep-A', product_id: null
          }
        })).click();
        const card = page.locator('[data-analysis-id="research-target"]').and(located);
        await card.waitFor();
        assert.equal(await card.getByLabel('分析摘要').inputValue(), target.body.report.summary);
        assert.equal(await other.and(located).count(), 0);
        assert.equal(await card.evaluate(el => el.contains(document.activeElement)), true);
      },
      async (page, state) => {
        state.preparation.body.analysis_refs = [researchRecords[0].ref];
        await page.route('**/api/prelive/v2/queries', route => {
          const request = route.request().postDataJSON();
          if (request.query !== 'read_analysis_report') return route.fallback();
          const record = researchRecords.find(item => item.ref.id === request.args.ref.id);
          assert.ok(record, 'read only the requested fixture research');
          return route.fulfill({ json: record });
        });
      });

    let savedDraft;
    await check('未配置本场从面板选品，配置后直接显示话术生成',
      async ({ page, state, productA, configureRoom }) => {
        const start = page.locator('#start-preparation');
        await configureRoom();
        assert.equal(await start.isVisible(), false);
        await page.locator('#room-open-products').click();
        const picker = page.locator('#room-products-picker');
        await picker.waitFor();
        await picker.getByRole('checkbox', { name: productA.body.name }).waitFor();
        assert.equal(await page.locator('#room-dashboard').isVisible(), true);

        state.preparation.body.selected_product_refs = [productA.ref];
        state.preparation.body.draft_ref = savedDraft.ref;
        state.details.draft = savedDraft;
        state.preparation.ref = ref('preparation', state.preparation.ref.id, 2);
        await page.reload();
        await page.evaluate(() => shengboWorkbench.ready);
        await configureRoom();
        assert.equal(await page.locator('#prep-next').count(), 0);
        assert.equal(await start.isVisible(), false);
        const generator = page.locator('[data-inline-room-section="scripts"] [data-script-generation]');
        await generator.waitFor();
        assert.equal(await generator.getByRole('checkbox', {
          name: '为此商品新增话术 ' + productA.body.name
        }).isChecked(), true);
        assert.equal(await generator.getByRole('button', {
          name: '将新增要求交给 Agent'
        }).isEnabled(), true);
      },
      (_page, state) => {
        savedDraft = state.details.draft;
        Object.assign(state.preparation.body, {
          selected_product_refs: [], draft_ref: null, script_refs: [], playlist_ref: null
        });
        Object.assign(state.details, { draft: null, scripts: [], playlist: null });
      });
    assert.deepEqual(failures, [], 'artifact navigation scenarios with failures');
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
