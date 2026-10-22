// Debounced server autosave plus a local draft to survive a close before response.
let roomRevision = 0, autosaveTimer, savingChain = Promise.resolve();
function roomPayload() {
  const f = $('config-form');
  return {...(typeof readRoomCommerce==='function'?{commerce:readRoomCommerce()}:{}),name:f.elements.name.value,product_ids:[...chosenProducts],version_count:Number(f.elements.version_count.value),
    loop:f.elements.loop.checked,plan_mode:'all',script_order:[...order],base_script_ids:[...(roomConfig.script_order || [])],
    removed_script_ids:[...removedScripts],selected_script_ids:roomScripts().map(s => s.id),...ttsForm()};
}
function rememberRoomDraft() {
  if (roomId && roomConfig) localStorage.setItem('shengbo-room-draft:' + roomId, JSON.stringify(roomPayload()));
}
function queueRoomSave() {
  roomRevision++; rememberRoomDraft(); clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(() => { if (dirty && roomId) saveRoom().catch(() => { $('save-status').textContent = '本地草稿已保存 · 尚未同步'; }); },700);
}
saveRoom = function() {
  const identity = roomId, revision = roomRevision, payload = roomPayload();
  if (!identity) return Promise.reject(Error('请先创建直播间'));
  clearTimeout(autosaveTimer);
  const task = savingChain.catch(() => {}).then(async () => {
    if (roomId === identity) $('save-status').textContent = '正在保存…';
    const result = await api(`/api/rooms/${identity}`,'PUT',payload);
    const index = library.rooms.findIndex(r => r.id === identity); if (index >= 0) library.rooms[index] = result;
    if (roomId === identity && roomRevision === revision) {
      roomConfig = result; order = [...result.script_order]; dirty = false; removedScripts.clear();
      localStorage.removeItem('shengbo-room-draft:' + identity); $('save-status').textContent = '已保存到本机';
      $('page-title').textContent = result.name; renderRoom();
    }
    return result;
  }); savingChain = task; return task;
};
const originalEnterRoom = enterRoom;
enterRoom = async function(id, monitor=false) {
  await originalEnterRoom(id,monitor); roomRevision++;
  const saved = localStorage.getItem('shengbo-room-draft:' + id); if (!saved) return;
  try {
    const draft = JSON.parse(saved), f = $('config-form');
    chosenProducts = new Set(draft.product_ids); order = draft.script_order; removedScripts = new Set(draft.removed_script_ids || []);
    f.elements.name.value = draft.name; f.elements.version_count.value = draft.version_count; f.elements.loop.checked = draft.loop;
    f.elements.tts_provider.value = draft.tts_provider; updateVoiceChoices(draft.tts_voice); f.elements.tts_model.value = draft.tts_model;
    if(typeof loadCommerceEditor==='function')loadCommerceEditor(draft.commerce||roomConfig.commerce);
    markDirty(); renderRoom(); notify('已恢复上次关闭前的配置草稿，正在保存到本机');
  } catch { notify('上次的配置草稿未能恢复，已加载最后保存的配置'); }
};
window.addEventListener('pagehide', () => { if (dirty) rememberRoomDraft(); });
document.querySelectorAll('[data-test-link]').forEach(el => el.onclick = async e => {
  e.preventDefault(); try { if (dirty && roomId) await saveRoom(); location.href = '/test' + (roomId ? '?room=' + encodeURIComponent(roomId) : ''); }
  catch(e) { notify(e.message); }
});
