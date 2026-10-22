/* Isolated DOM acceptance. No app server, credentials or synthesis. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('C:/Users/lizhe/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const catalog = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  let browser;
  try { browser = await chromium.launch({ headless: true }); }
  catch { browser = await chromium.launch({ channel: 'msedge', headless: true }); }
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setContent('<!doctype html><meta charset="utf-8"><main id="editor"></main>');
    await page.addScriptTag({ path: path.resolve(__dirname, '../web/workspace-editors.js') });
    await page.evaluate(async catalog => {
      const room = {ref: {kind: 'config', id: 'isolated-room', revision: 1, content_hash: 'sha256:test'}, body: {name: '隔离音色验证', tts_provider: 'minimax', tts_voice: 'Wise_Woman'}};
      window.saved = [];
      window.ShengboWorkbenchApi = { uuid: () => 'isolated-operation', hash: async value => JSON.stringify(value),
        query: async name => {
          if (name === 'get_workspace_snapshot') return {rooms: [room]};
          if (name === 'get_business_schema') return {};
          if (name === 'read_room') return room;
          if (name === 'read_session_history') return {preparations: []};
          if (name === 'voice_options') return catalog;
          throw Error('Unexpected query: ' + name);
        },
        request: async (url, body) => {
          if (!url.endsWith('/room-settings')) throw Error('Unexpected mutation: ' + url);
          window.saved.push(structuredClone(body));
          room.body.tts_voice = body.tts_selection.voice;
          room.ref.revision++;
          return {room_ref: structuredClone(room.ref)};
        }};
      window.ui = ShengboWorkspaceEditors.mount({container: document.getElementById('editor'), storage: null});
      await ui.open({view: 'rooms', room_id: room.ref.id});
    }, catalog);
    const voice = page.getByLabel('默认音色', { exact: true });
    assert.equal(await voice.inputValue(), 'Wise_Woman');
    assert.equal(await voice.locator('option').count(), 7);
    for (const [id, name] of [
      ['Chinese (Mandarin)_Wise_Women', '成熟女声'],
      ['Chinese (Mandarin)_Sweet_Lady', '甜美女声'],
      ['Chinese (Mandarin)_Reliable_Executive', '沉稳男声'],
    ]) {
      assert.equal(await voice.locator('option').filter({hasText: name}).evaluateAll((nodes, id) => nodes.find(n => n.value === id)?.textContent, id), name);
      await voice.selectOption(id);
      await page.getByText(catalog.voice_details.minimax[id].description, {exact: true}).waitFor();
      await page.getByRole('button', {name: '保存直播间默认设置', exact: true}).click();
      await page.getByText('直播间默认设置已保存，已有本场保持原配置。', {exact: true}).waitFor();
      assert.equal(await page.evaluate(() => saved.at(-1).tts_selection.voice), id);
      assert.equal(await voice.inputValue(), id);
    }
    assert.deepEqual(errors, []);
    const legacy = await browser.newPage();
    await legacy.setContent('<select id="tts-provider"><option value="system">system</option><option value="minimax" selected>minimax</option></select><select id="tts-voice"></select><label id="tts-model-label"></label><p id="tts-note"></p>');
    const controlSource = fs.readFileSync(path.resolve(__dirname, '../web/control.js'), 'utf8');
    const legacyChoiceFunction = controlSource.slice(controlSource.indexOf('function updateVoiceChoices('), controlSource.indexOf("$('tts-provider').onchange"));
    await legacy.addScriptTag({content: 'const $=id=>document.getElementById(id);const library=' + JSON.stringify({service: {voice_options: catalog, minimax_configured: true}}) + ';' + legacyChoiceFunction});
    await legacy.evaluate(() => updateVoiceChoices('Chinese (Mandarin)_Wise_Women'));
    assert.equal(await legacy.locator('#tts-voice').inputValue(), 'Chinese (Mandarin)_Wise_Women');
    assert.equal(await legacy.locator('#tts-voice option').count(), 7);
    assert.equal(await legacy.locator('#tts-voice option:checked').textContent(), '成熟女声');
    await legacy.selectOption('#tts-provider', 'system');
    await legacy.evaluate(() => updateVoiceChoices('Huihui'));
    assert.equal(await legacy.locator('#tts-voice option:checked').textContent(), '慧慧 · 本机普通话');
    console.log(JSON.stringify({status: 'passed', voices: 7, verified_new_selections_saved: 3, legacy_selector: 'passed', tts_calls: 0}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
