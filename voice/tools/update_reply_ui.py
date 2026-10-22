"""One-off, checked transformations for the existing compact HTML/JS files."""
from pathlib import Path

root = Path(__file__).resolve().parents[1]


def edit(file, before, after):
    path = root / file
    text = path.read_text(encoding='utf-8')
    assert text.count(before) == 1, (file, before[:100], text.count(before))
    path.write_text(text.replace(before, after), encoding='utf-8')


edit('web/index.html', '<link rel="icon"', '<link rel="stylesheet" href="/assets/test.css"><link rel="icon"')
edit('web/index.html', 'data-view="products">商品库</button></nav>', 'data-view="products">商品库</button><a class="nav-link" href="/test" data-test-link>评论测试台</a></nav>')
path = root / 'web/index.html'
text = path.read_text(encoding='utf-8')
start = text.index('<form id="test-event-form">')
end = text.index('<div id="audience-feed"', start)
text = text[:start] + '<p class="muted">这里只查看观众事件。模拟评论与智能回复请前往独立测试台。</p><a class="button-link" href="/test" data-test-link>打开评论测试台</a>' + text[end:]
text = text.replace('<span class="badge">模拟测试 · 未接平台插件</span>', '<span class="badge">保留历史 · 未接平台插件</span>')
text = text.replace('<div class="dialog-footer"><button type="button" data-close="product-dialog">', '<section class="knowledge-editor"><div class="toolbar"><h2>独立知识文档</h2><button type="button" id="add-knowledge-document">＋ 新增文档</button></div><p class="muted">例如物流政策、尺码说明、售后规则。评论回复按文档检索并展示来源，资料不加入常规话术生成。</p><label>导入 UTF-8 文本文件<input id="import-knowledge-text" type="file" accept=".txt,text/plain"></label><div id="knowledge-documents"></div></section><div class="dialog-footer"><button type="button" data-close="product-dialog">')
text = text.replace('<script src="/assets/preparation.js"></script>', '<script src="/assets/knowledge-editor.js"></script><script src="/assets/preparation.js"></script><script src="/assets/persistence.js"></script>')
path.write_text(text, encoding='utf-8')
edit('web/control.js', "function markDirty(){dirty=true;$('save-status').textContent='未保存'}", "function markDirty(){dirty=true;$('save-status').textContent='正在保存';if(typeof queueRoomSave==='function')queueRoomSave()}")
edit('web/control.js', "f.elements[name].value=editingProduct[name]??'';$('product-dialog').showModal()", "f.elements[name].value=editingProduct[name]??'';renderKnowledgeEditor(editingProduct?.knowledge_documents||[]);$('product-dialog').showModal()")
edit('web/control.js', 'knowledge:f.elements.knowledge.value,image:', 'knowledge:f.elements.knowledge.value,knowledge_documents:readKnowledgeEditor(),image:')
path = root / 'web/preparation.js'
text = path.read_text(encoding='utf-8')
start = text.index("$('test-event-type').onchange")
end = text.index('boot().then(pollJobs);', start)
text = text[:start] + text[end:]
text = text.replace('· 模拟事件</small>', "· ${e.source === 'simulated' ? '模拟事件' : '观众事件'} · ${esc(e.time.slice(0,10))}</small>")
text = text.replace('暂无事件。可发送模拟评论、点赞或进房事件。', '暂无观众事件。模拟输入请前往评论测试台。')
text = text.replace('<button data-monitor="${r.id}">直播监控</button>', '<button data-monitor="${r.id}">直播监控</button><a class="button-link" href="/test?room=${encodeURIComponent(r.id)}">评论测试</a>')
# Defer boot until persistence.js has installed autosave and draft recovery.
text = text.replace('boot().then(pollJobs);', 'window.addEventListener(\'DOMContentLoaded\', () => boot().then(pollJobs));')
path.write_text(text, encoding='utf-8')
path = root / 'web/style.css'
with path.open('a', encoding='utf-8') as f:
    f.write('\n.knowledge-editor{border-top:1px solid var(--border);margin-top:20px;padding-top:20px}.knowledge-editor .toolbar{margin-bottom:12px}.knowledge-editor h2{margin:0}.knowledge-editor>p{margin-bottom:16px}.knowledge-editor-row{border:1px solid var(--border);background:#f7f9fc;padding:16px;border-radius:8px;margin-bottom:14px}.knowledge-editor-row label{margin-bottom:12px}.room-card .product-actions{flex-wrap:wrap}.room-card .product-actions .button-link{font-size:12px;text-align:center;padding:8px}\n')
print('Updated product documents, autosave entry points, and separate test navigation.')
