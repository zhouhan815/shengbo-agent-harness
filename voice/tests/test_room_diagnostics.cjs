/* Private browser acceptance. Every backend response and playback hook is a local fixture. */
'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

async function main() {
  const browser = await chromium.launch({ headless: true }).catch(() => chromium.launch({ channel: 'msedge', headless: true }));
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.setDefaultTimeout(6000);
    const origin = 'http://127.0.0.1:41891';
    const browserErrors = [], escapedRequests = [];
    page.on('pageerror', error => browserErrors.push(error.message));
    await page.route('**/*', route => {
      if (route.request().url() === origin + '/') {
        return route.fulfill({ contentType: 'text/html; charset=utf-8', body: '<main><button id="opener">互动与诊断</button><div id="broadcast"></div><div id="diagnostics"></div></main>' });
      }
      if (route.request().url() === origin + '/live/A?preview=1') {
        return route.fulfill({ contentType: 'text/html; charset=utf-8', body: '<title>隔离预览</title>' });
      }
      escapedRequests.push(route.request().url());
      return route.abort();
    });
    await page.goto(origin);
    const web = path.resolve(__dirname, '../web');
    await page.addStyleTag({ path: path.join(web, 'room-diagnostics.css') });
    await page.addScriptTag({ path: path.join(web, 'workbench-execution.js') });
    await page.evaluate(async () => {
      const calls = [], media = { routers: 0, samples: [], paused: [], players: 0 };
      const room = id => ({ room_id: id, room_name: '测试直播间 ' + id, preparation_id: 'prep-' + id });
      let location = room('A');
      window.diagFixture = {
        calls, media, get location() { return location; },
        switchRoom(id) { location = room(id); }
      };
      window.VoicePlayer = class { constructor() { media.players++; } };
      window.ShengboAudioRouting = { create() {
        media.routers++;
        return { registerMedia: async audio => media.samples.push(audio.getAttribute('src')), dispose() {} };
      } };
      HTMLMediaElement.prototype.pause = function () { media.paused.push(this.getAttribute('src')); };
      const api = {
        async request(url, body, options = {}) {
          calls.push({ url, body, method: options.method || (body === undefined ? 'GET' : 'POST') });
          if (url === '/api/library') return { rooms: [{ id: 'A' }, { id: 'B' }], scripts: [] };
          if (url.endsWith('/state')) return { status: 'stopped', queue: [], current: null };
          if (url === '/api/copywriting/evaluation') return { methodology: {}, rubric: {}, runs: [],
            audio_samples: [{ label: '隔离声音样本', url: '/tts-samples/fixture.wav' }] };
          const legacy = /^\/api\/jobs\?room_id=(\w+)$/.exec(url);
          if (legacy) return { jobs: [{ id: 'legacy-' + legacy[1], kind: 'scripts', status: 'completed', items: [] }] };
          const test = /^\/api\/rooms\/(\w+)\/reply-(knowledge|sessions)$/.exec(url);
          if (test) {
            const [, id, resource] = test;
            if (resource === 'knowledge') return { products: [{ id: 'product-' + id, name: '房间商品 ' + id }], documents: [] };
            return { sessions: [1, 2].map(index => ({ id: 'session-' + id + index, title: '测试记录 ' + id + index })) };
          }
          const session = /^\/api\/reply-sessions\/(session-(\w+?)(\d))(\/comments)?$/.exec(url);
          if (session) {
            if (session[4]) return { id: 'sent' };
            if (window.diagFixture.delaySession === session[1]) {
              window.diagFixture.delaySession = null;
              await new Promise(resolve => window.diagFixture.release = resolve);
            }
            return { id: session[1], turns: [{ id: 'turn-' + session[1], username: '测试观众',
              comment: '只属于记录 ' + session[2] + session[3], answer: '隔离回复', status: 'completed' }] };
          }
          throw Error('Unexpected API request ' + url);
        },
        async query(name, args) {
          calls.push({ name, args, method: 'GET' });
          assertFixture(name === 'read_completion_observation', 'unexpected query ' + name);
          return { jobs: [{ job_id: 'job-' + args.preparation_id, kind: 'scripts', status: 'completed', items: [] }] };
        }
      };
      function assertFixture(condition, message) { if (!condition) throw Error(message); }
      const broadcast = ShengboWorkbenchExecution.mount({ container: document.querySelector('#broadcast'), api, showNavigation: false });
      await broadcast.open({ ...location, view: 'broadcast' });
      window.diagFixture.mainAudio = document.querySelector('#broadcast audio');
      window.diagFixture.tool = ShengboWorkbenchExecution.mountDiagnostics({ container: document.querySelector('#diagnostics'),
        api, getContext: () => location, sampleAudioRouting: broadcast.audioRouting,
        onError: error => calls.push({ error: error.message }) });
      document.getElementById('opener').addEventListener('click', () => void window.diagFixture.tool.open({ view: 'comments' }));
    });

    const opener = page.getByRole('button', { name: '互动与诊断', exact: true });
    const dialog = page.getByRole('dialog', { name: '互动与诊断' });
    const commentsTab = dialog.getByRole('tab', { name: '评论测试' });
    const jobsTab = dialog.getByRole('tab', { name: '任务与评测' });
    const commentPanel = dialog.getByRole('tabpanel', { name: '评论测试' });
    const jobsPanel = dialog.getByRole('tabpanel', { name: '任务与评测' });
    const question = dialog.getByLabel('模拟评论内容');
    await opener.click();
    await dialog.getByText('当前直播间 · 测试直播间 A').waitFor();
    assert.equal(await commentsTab.evaluate(tab => tab === document.activeElement), true);
    await commentPanel.getByText('只属于记录 A1').waitFor();
    assert.equal(await dialog.locator('audio').count(), 0);
    await commentPanel.getByRole('button', { name: /测试记录 A2/ }).click();
    await commentPanel.getByText('只属于记录 A2').waitFor();
    await question.fill('A 的未发评论');
    await commentPanel.getByRole('radio', { name: '房间商品 A' }).check();
    await commentsTab.focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await jobsTab.evaluate(tab => tab === document.activeElement), true);
    assert.equal(await commentsTab.getAttribute('aria-selected'), 'false');
    await jobsPanel.locator('[data-jobs-list]').waitFor();
    await page.waitForFunction(() => diagFixture.calls.some(call => call.args?.preparation_id === 'prep-A'));
    assert.equal(await commentPanel.isVisible(), false);
    await jobsPanel.getByRole('button', { name: '全局话术评测与语音样本' }).click();
    await jobsPanel.getByRole('heading', { name: '隔离声音样本' }).waitFor();
    assert.deepEqual(await page.evaluate(() => diagFixture.media.samples), ['/tts-samples/fixture.wav']);
    await commentsTab.click();
    assert.equal(await question.inputValue(), 'A 的未发评论');
    assert.equal(await commentPanel.getByRole('radio', { name: '房间商品 A' }).isChecked(), true);
    assert.equal(await commentPanel.getByRole('button', { name: /测试记录 A2/ }).getAttribute('aria-pressed'), 'true');
    assert.deepEqual(await page.evaluate(() => diagFixture.media.paused), ['/tts-samples/fixture.wav']);
    await page.keyboard.press('Escape');
    assert.equal(await dialog.isVisible(), false);
    assert.equal(await opener.evaluate(button => button === document.activeElement), true);
    const diagnosticReads = () => page.evaluate(() => diagFixture.calls.filter(call => /reply-|\/api\/jobs/.test(call.url || '')).length);
    const beforeHidden = await diagnosticReads();
    await page.waitForTimeout(1600);
    assert.equal(await diagnosticReads(), beforeHidden, 'closed modal stops background diagnostic polling');

    await page.evaluate(() => {
      diagFixture.delaySession = 'session-A2';
      void diagFixture.tool.open({ view: 'comments' });
    });
    await page.waitForFunction(() => !!diagFixture.release);
    await page.evaluate(async () => {
      diagFixture.tool.close();
      diagFixture.switchRoom('B');
      if (diagFixture.tool.hasDirty()) throw Error('comment draft leaked from room A');
      await diagFixture.tool.open({ view: 'jobs' }, { opener: document.getElementById('opener') });
    });
    await dialog.getByText('当前直播间 · 测试直播间 B').waitFor();
    await page.waitForFunction(() => diagFixture.calls.some(call => call.args?.preparation_id === 'prep-B'));
    await commentsTab.click();
    await commentPanel.getByText('只属于记录 B1').waitFor();
    await page.evaluate(() => diagFixture.release());
    assert.equal(await commentPanel.getByText('只属于记录 A2').count(), 0);
    assert.equal(await question.inputValue(), '');

    await page.setViewportSize({ width: 390, height: 540 });
    assert.equal(await dialog.evaluate(node => {
      const rect = node.getBoundingClientRect();
      return rect.left >= 0 && rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight;
    }), true);
    assert.equal(await dialog.getByRole('button', { name: '关闭', exact: true }).isVisible(), true);
    assert.equal(await jobsTab.isVisible(), true);
    await dialog.getByRole('button', { name: '关闭', exact: true }).click();
    await page.evaluate(() => diagFixture.switchRoom('A'));
    assert.equal(await page.evaluate(() => diagFixture.tool.hasDirty()), true);
    await opener.click();
    assert.equal(await question.inputValue(), 'A 的未发评论');
    await commentPanel.getByText('只属于记录 A1').waitFor();
    await commentPanel.getByRole('button', { name: /测试记录 A2/ }).click();
    await commentPanel.getByText('只属于记录 A2').waitFor();
    await commentPanel.getByRole('button', { name: '发送测试评论', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('[name="comment"]').value);
    const result = await page.evaluate(() => ({
      writes: diagFixture.calls.filter(call => call.method !== 'GET'),
      sameAudio: diagFixture.mainAudio === document.querySelector('#broadcast audio'),
      media: diagFixture.media
    }));
    assert.equal(result.writes.length, 1, JSON.stringify(result.writes));
    assert.equal(result.writes[0].url, '/api/reply-sessions/session-A2/comments');
    assert.equal(result.writes[0].body.product_id, 'product-A');
    assert.equal(result.writes[0].body.text, 'A 的未发评论');
    assert.equal(result.sameAudio, true);
    assert.equal(result.media.routers, 1);
    assert.equal(result.media.players, 0);
    assert.deepEqual(escapedRequests, []);
    assert.deepEqual(browserErrors, []);
    console.log('PASS diagnostics tabs, keyboard and focus, exact room/preparation isolation, hidden polling, desktop/mobile fit, single output owner, one explicit mock text action');
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
