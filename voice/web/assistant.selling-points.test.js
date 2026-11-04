/* Selling-point decision UI contract. Mocked services only; no product/model writes. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: fs.existsSync(chromium.executablePath()) ? chromium.executablePath() : 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  try {
    const page = await browser.newPage({ viewport: { width: 460, height: 1000 } });
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.setContent('<!doctype html><meta charset="utf-8"><div id="assistant" style="width:100%;height:960px"></div>');
    await page.addStyleTag({ content: fs.readFileSync(path.join(__dirname, 'assistant.css'), 'utf8') });
    await page.addScriptTag({ path: path.join(__dirname, 'assistant-decision-copy.js') });
    await page.addScriptTag({ path: path.join(__dirname, 'assistant.js') });
    await page.evaluate(() => {
      window.calls = []; window.sequence = 0;
      window.run = { run_id: 'r1', status: 'WAITING_USER', questions: [] };
      window.ask = async (kind, preview, options) => {
        window.run.questions = [{ decision_id: 'd' + ++window.sequence, revision: 1, kind, prompt: kind === 'selling_points_generation' ? '没有核心卖点，是否帮你整理？' : '请确认以下卖点后保存', preview, options, allow_text: true, input_placeholder: '可选：希望重点面向谁、什么场景或突出什么方向（不能补造性能）' }];
        await window.ui.refresh();
      };
      window.ui = ShengboAssistant.mount({
        container: document.getElementById('assistant'), storage: null, pollInterval: 60000,
        fetch: async (url, init = {}) => {
          const body = typeof init.body === 'string' ? JSON.parse(init.body) : null;
          window.calls.push({ url, method: init.method || 'GET', body });
          let value = {};
          if (url.endsWith('/session')) value = { csrf_token: 'fixture-csrf' };
          else if (url.endsWith('/conversations')) value = { items: [{ conversation_id: 'c1', scope: { kind: 'catalog', account_id: 'local' } }] };
          else if (url.endsWith('/conversations/c1')) value = { conversation_id: 'c1', runs: [window.run] };
          else if (url.endsWith('/runs/r1')) value = window.run;
          else if (url.includes('/events?')) value = { events: [], next_cursor: 0 };
          else if (url.endsWith('/answers')) { window.run.questions = []; value = { run: window.run }; }
          return { ok: true, status: 200, json: async () => structuredClone(value) };
        }
      });
    });
    await page.evaluate(() => window.ui.ready);
    const askGeneration = () => page.evaluate(() => window.ask('selling_points_generation', { product_name: '单人网球回弹训练器', product_ref: { id: 'p1', revision: 1 } }, [{ value: 'generate', label: '帮我整理核心卖点' }, { value: 'skip', label: '暂不生成' }]));
    const askConfirmation = () => page.evaluate(() => window.ask('selling_points_confirmation', {
      product_name: '单人网球回弹训练器', product_ref: { id: 'p1', revision: 1 }, proposal_id: 'proposal-1',
      items: [{ selling_point: '一个人也能练习回球 <img src=x onerror=alert(1)>', audience_need: '缺少陪练', use_scene: '个人练习', evidence: [{ fact: '弹力绳连接球体与底座', source_ref: { id: 'source-1' } }], limitations: ['需要合适的运动空间'] }]
    }, [{ value: 'approve', label: '确认并保存' }, { value: 'revise', label: '按意见修改' }, { value: 'reject', label: '暂不采用' }]));
    const answers = () => page.evaluate(() => window.calls.filter(c => c.url.endsWith('/answers')));

    await askGeneration();
    assert.equal(await page.locator('.sb-a-question .sb-a-json').count(), 0, 'generation prompt is a readable card');
    await page.getByRole('textbox', { name: '卖点生成方向（可选）' }).fill('面向刚入门、没有固定球友的人');
    await page.evaluate(() => window.ui.refresh());
    assert.equal(await page.getByRole('textbox', { name: '卖点生成方向（可选）' }).inputValue(), '面向刚入门、没有固定球友的人');
    await page.getByRole('button', { name: '帮我整理核心卖点', exact: true }).click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/decisions/d1/answers')));
    assert.deepEqual((await answers()).at(-1).body.answer, { choice: 'generate', text: '面向刚入门、没有固定球友的人' });
    assert.equal((await answers()).at(-1).body.decision_revision, 1);

    await askGeneration();
    await page.getByRole('button', { name: '暂不生成', exact: true }).click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/decisions/d2/answers')));
    assert.equal((await answers()).at(-1).body.answer, 'skip');

    await askConfirmation();
    assert.equal(await page.locator('.sb-a-benefit').count(), 1);
    assert.equal(await page.locator('.sb-a-benefit img').count(), 0, 'candidate strings cannot become HTML');
    assert.equal(await page.locator('.sb-a-question .sb-a-json').count(), 0, 'candidate is not a JSON dump');
    await page.getByText('顾客场景与商品依据', { exact: true }).click();
    assert.ok((await page.locator('.sb-a-benefit-basis').textContent()).includes('弹力绳连接球体与底座'));
    await page.evaluate(() => window.ui.refresh());
    assert.equal(await page.locator('.sb-a-benefit-basis').getAttribute('open'), '', 'expanded evidence survives polling');
    const approve = page.getByRole('button', { name: '确认并保存', exact: true });
    assert.equal(await approve.isEnabled(), true, 'server-bound proposal approval requires no business human_action');
    await approve.click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/decisions/d3/answers')));
    assert.equal((await answers()).at(-1).body.answer, 'approve');
    assert.equal(await page.evaluate(() => window.calls.some(c => c.url.endsWith('/commands'))), false);

    await askConfirmation();
    const beforeEmptyRevision = (await answers()).length;
    await page.getByRole('button', { name: '按意见修改', exact: true }).click();
    assert.equal((await answers()).length, beforeEmptyRevision, 'empty revision does not submit');
    assert.ok((await page.locator('.sb-a-decision-error').textContent()).includes('请先填写'));
    await page.getByRole('textbox', { name: '卖点修改意见（可选）' }).fill('强调自主练习，不承诺提高球技');
    assert.equal(await approve.isEnabled(), false, 'unsubmitted feedback cannot silently approve old content');
    await page.getByRole('textbox', { name: '卖点修改意见（可选）' }).fill('');
    assert.equal(await approve.isEnabled(), true, 'clearing unsubmitted feedback restores plain approval');
    await page.getByRole('textbox', { name: '卖点修改意见（可选）' }).fill('强调自主练习，不承诺提高球技');
    await page.evaluate(() => window.ui.refresh());
    assert.equal(await approve.isEnabled(), false);
    assert.equal(await page.getByRole('textbox', { name: '卖点修改意见（可选）' }).inputValue(), '强调自主练习，不承诺提高球技');
    await page.getByRole('button', { name: '按意见修改', exact: true }).click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/decisions/d4/answers')));
    assert.deepEqual((await answers()).at(-1).body.answer, { choice: 'revise', text: '强调自主练习，不承诺提高球技' });

    await askConfirmation();
    await page.getByRole('textbox', { name: '卖点修改意见（可选）' }).fill('这不是我的方向');
    await page.getByRole('button', { name: '暂不采用', exact: true }).click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/decisions/d5/answers')));
    assert.equal((await answers()).at(-1).body.answer, 'reject');

    await askGeneration();
    await page.locator('.sb-a-answer').fill('请先问我需要补充哪些事实');
    await page.getByRole('button', { name: '提交答复', exact: true }).click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/decisions/d6/answers')));
    assert.deepEqual((await answers()).at(-1).body.answer, { text: '请先问我需要补充哪些事实' });

    await page.evaluate(() => window.ask('audio_approval', null, [{ value: 'approve', label: '批准生成语音' }]));
    assert.equal(await page.getByRole('button', { name: '批准生成语音', exact: true }).isEnabled(), false, 'ordinary audio approval still requires its business receipt command');
    await page.evaluate(() => window.ask('selling_points_confirmation', { product_name: '测试商品', items: [] }, [{ value: 'approve', label: '受约束的确认', requires_human_action: true }]));
    await page.locator('.sb-a-answer').fill('修改意见');
    await page.locator('.sb-a-answer').fill('');
    assert.equal(await page.getByRole('button', { name: '受约束的确认', exact: true }).isEnabled(), false, 'editing feedback cannot bypass an explicit human-action requirement');
    await page.evaluate(async () => {
      window.run.questions = [{ decision_id: 'catalog-test', status: 'OPEN', revision: 1, kind: 'catalog_confirm',
        prompt: '模拟资料：标准装 29.90 元。日常售价→price；直播售价→promo_price。缺项 image、sales_platform。',
        options: [{ value: 'split_by_sku', label: '拆分保存，日常价=price、直播价=promo_price' },
          { value: 'skip_save', label: '暂不保存' }], allow_text: true }];
      await window.ui.refresh();
    });
    const dialog = page.getByRole('dialog', { name: '确认商品入库方案' });
    assert.ok(await dialog.isVisible());
    assert.equal(await page.locator('.sb-a-transcript [data-decision-id]').count(), 0);
    assert.doesNotMatch(await dialog.innerText(), /\b(?:price|promo_price|image|sales_platform)\b/);
    assert.match(await dialog.innerText(), /促销参考价/);
    await page.getByRole('button', { name: '稍后处理' }).click();
    await page.evaluate(() => window.ui.refresh());
    assert.equal(await dialog.count(), 0, 'a polling refresh must not reopen a deferred dialog');
    await page.getByRole('button', { name: '查看待确认' }).click();
    await dialog.getByRole('textbox').fill('只整理差异，等我确认后再写入');
    assert.equal(await dialog.getByRole('button', { name: /拆分保存/ }).isEnabled(), false);
    await dialog.getByRole('button', { name: '提交答复' }).click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/decisions/catalog-test/answers')));
    assert.deepEqual((await answers()).at(-1).body.answer, { text: '只整理差异，等我确认后再写入' });
    await page.evaluate(async () => {
      window.run.questions = [{ decision_id: 'legacy-catalog-options', status: 'OPEN', revision: 1, kind: 'catalog_confirm',
        prompt: '请选择要入库的方式',
        options: [{ save_both: '标准装与加量装分别入库' }, { save_standard: '只保存标准装' }, { skip: '暂不入库' }],
        allow_text: true }];
      await window.ui.refresh();
    });
    assert.deepEqual(await dialog.locator('.sb-a-question-actions button').allTextContents(),
      ['标准装与加量装分别入库', '只保存标准装', '暂不入库']);
    await dialog.getByRole('button', { name: '只保存标准装' }).click();
    await page.waitForFunction(() => window.calls.some(c => c.url.endsWith('/decisions/legacy-catalog-options/answers')));
    assert.equal((await answers()).at(-1).body.answer, 'save_standard');
    await page.evaluate(async () => {
      window.run.questions = [{ decision_id: 'ambiguous-options', status: 'OPEN', revision: 1, kind: 'catalog_confirm',
        prompt: '请选择要入库的方式', options: [{ save_both: '分别入库', skip: '暂不入库' }], allow_text: true }];
      await window.ui.refresh();
    });
    assert.equal(await dialog.locator('.sb-a-question-actions button').count(), 0);
    assert.match(await dialog.innerText(), /选项说明不完整/);
    assert.ok(await dialog.getByRole('textbox').isVisible());
    await page.evaluate(async () => {
      window.run.questions = [{ decision_id: 'retired-question', status: 'OPEN', revision: 1, kind: 'catalog_confirm', prompt: '已经停止的任务' }];
      window.run.status = 'CANCELLED'; await window.ui.refresh();
    });
    assert.equal(await page.getByRole('dialog').count(), 0);
    assert.equal(await page.getByRole('button', { name: '查看待确认' }).count(), 0);
    await page.evaluate(() => { window.run.status = 'WAITING_USER'; });
    await askConfirmation();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => { document.getElementById('assistant').style.height = '810px'; document.body.style.margin = '0'; });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    await page.evaluate(() => window.ui.destroy());
    console.log('selling-point UI contracts passed: generate/skip/directions/revise/approve/reject/free text, evidence, polling, existing audio approval, mobile; mocked services only.');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
