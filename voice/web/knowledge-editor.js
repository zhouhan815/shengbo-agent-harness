// Product-owned documents remain in the shared product library.
let knowledgeDraft = [];
function renderKnowledgeEditor(docs) {
  knowledgeDraft = docs.map(d => ({...d})); paintKnowledgeEditor();
}
function paintKnowledgeEditor() {
  $('knowledge-documents').innerHTML = knowledgeDraft.map((d,i) => `<div class="knowledge-editor-row"><label>文档标题<input data-knowledge-title="${i}" value="${esc(d.title)}" maxlength="120" required></label><label>文档内容<textarea data-knowledge-content="${i}" rows="4" maxlength="15000" required>${esc(d.content)}</textarea></label><button type="button" data-remove-knowledge="${i}" class="danger">删除文档</button></div>`).join('');
  document.querySelectorAll('[data-knowledge-title]').forEach(el => el.oninput = () => knowledgeDraft[Number(el.dataset.knowledgeTitle)].title = el.value);
  document.querySelectorAll('[data-knowledge-content]').forEach(el => el.oninput = () => knowledgeDraft[Number(el.dataset.knowledgeContent)].content = el.value);
  document.querySelectorAll('[data-remove-knowledge]').forEach(el => el.onclick = () => { knowledgeDraft.splice(Number(el.dataset.removeKnowledge),1); paintKnowledgeEditor(); });
}
function readKnowledgeEditor() {
  if (knowledgeDraft.some(d => !d.title.trim() || !d.content.trim())) throw Error('请填写知识文档的标题与内容，或删除空文档');
  return knowledgeDraft.map(d => ({id:d.id,title:d.title.trim(),content:d.content.trim()}));
}
$('add-knowledge-document').onclick = () => { if (knowledgeDraft.length >= 20) return notify('每件商品最多20份知识文档'); knowledgeDraft.push({id:crypto.randomUUID(),title:'',content:''}); paintKnowledgeEditor(); };
$('import-knowledge-text').onchange = async e => {
  try { const file = e.target.files[0]; if (!file) return; if (knowledgeDraft.length >= 20) throw Error('每件商品最多20份知识文档'); if (file.size > 60000) throw Error('请使用60KB以内的 UTF-8 文本文件'); const text = await file.text(); if (!text.trim() || text.length > 15000) throw Error('文档内容须为1至15000字'); knowledgeDraft.push({id:crypto.randomUUID(),title:file.name.replace(/\.txt$/i,'').slice(0,120),content:text}); paintKnowledgeEditor(); }
  catch(e) { notify(e.message); } finally { e.target.value = ''; }
};
