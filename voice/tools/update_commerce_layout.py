"""One-time layout migration; application behavior lives in web/commerce-editor.js."""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
page = root/'web/index.html'
text = page.read_text(encoding='utf-8')
if '/assets/commerce-editor.css' not in text:
    text = text.replace('<link rel="stylesheet" href="/assets/test.css">', '<link rel="stylesheet" href="/assets/test.css"><link rel="stylesheet" href="/assets/commerce-editor.css">')
start = text.index('<dialog id="product-dialog">')
end = text.index('<dialog id="script-dialog">', start)
text = text[:start] + '''<dialog id="product-dialog"><form id="product-form" class="product-editor-shell">
<div class="dialog-head"><div><h2 id="product-dialog-title">添加商品</h2><p class="editor-heading">统一维护商品资料，供各直播间选择调用</p></div><button type="button" data-close="product-dialog" aria-label="关闭商品编辑">×</button></div>
<section class="editor-section"><div class="section-heading"><div><h3>基础信息</h3><p>名称、状态与日常售价；本场优惠在直播间单独配置。</p></div></div>
<div class="product-identity-grid"><div><label class="image-upload-tile" for="product-image-file"><img id="product-image-preview" alt="商品主图预览" hidden><span id="product-image-empty" class="image-upload-empty"><span aria-hidden="true">＋</span><strong>上传商品主图</strong><small>点击上传或更换</small></span><input id="product-image-file" type="file" name="image_file" accept="image/png,image/jpeg,image/webp"></label><p id="product-image-help" class="image-help">JPG、PNG、WebP，最大 3MB</p><div class="image-actions"><button type="button" id="remove-product-image" hidden>移除图片</button></div></div>
<div class="product-main-fields"><label>商品名称 <span class="required-mark">*</span><input name="name" required maxlength="60" placeholder="输入商品名称，最多60字"></label><div class="product-identifiers"><label>SKU ID<input name="sku_id" maxlength="80" placeholder="留空自动分配"></label><label class="product-status">商品状态<select name="sale_status"><option value="available">可售</option><option value="unavailable">不可售</option></select></label></div><div class="money-field"><label for="product-price">日常售价 <span class="required-mark">*</span></label><div class="money-input-group"><select name="currency" aria-label="售价币种"><option value="CNY">¥ CNY</option><option value="TWD">NT$ TWD</option><option value="USD">$ USD</option><option value="AUD">A$ AUD</option></select><input id="product-price" name="price" aria-label="日常售价" type="number" min="0" max="10000000" step="0.01" required placeholder="0.00"></div></div></div></div></section>
<div id="product-extra"></div>
<section class="editor-section" data-product-option="knowledge"><div class="field-heading"><label for="product-knowledge">知识库 / 注意事项</label><button type="button" class="remove-field" data-remove-product-field="knowledge">移除</button></div><textarea id="product-knowledge" name="knowledge" rows="3" maxlength="15000" placeholder="每条一行：使用提醒、物流、常见问题等"></textarea></section>
<section class="editor-section knowledge-editor" data-product-option="knowledge_documents"><div class="section-heading"><div><h3>独立知识文档</h3><p>用于评论问答检索，如尺码说明、售后政策。</p></div><button type="button" class="remove-field" data-remove-product-field="knowledge_documents">移除</button></div><div class="document-actions"><button type="button" id="add-knowledge-document">＋ 添加文档</button><label class="text-import-label">导入 TXT<input id="import-knowledge-text" type="file" accept=".txt,text/plain"></label></div><div id="knowledge-documents"></div></section>
<div class="optional-add-bar"><select id="product-optional-select" aria-label="可添加的商品配置项"></select><button type="button" id="restore-product-field">＋ 添加配置项</button><small id="product-removed-count"></small></div>
<div class="dialog-footer"><small class="muted">* 为必填项。移除的可选内容在保存后清空。</small><button type="button" data-close="product-dialog">取消</button><button type="submit" class="primary">保存商品</button></div></form></dialog>
''' + text[end:]
start = text.index('<dialog id="override-dialog">')
end = text.index('<audio id="local-broadcast-audio"', start)
text = text[:start] + '''<dialog id="override-dialog"><form id="override-form"><div class="dialog-head"><div><h2>本场单商品配置</h2><p class="editor-heading">只影响当前直播间，商品库日常资料保持独立。</p></div><button type="button" data-close="override-dialog" aria-label="关闭单品配置">×</button></div><p id="override-name" class="muted"></p>
<section class="editor-section"><div class="offer-grid"><label class="offer-control">单品优惠模式<select name="mode"><option value="none">不设置单品特惠</option><option value="discount">折扣</option><option value="price">优惠价</option></select></label><label class="offer-control" data-override-mode="discount">折扣比率<input name="discount" type="number" step="0.01" min="0.01" max="0.99" placeholder="如0.85"></label><label class="offer-control" data-override-mode="price">本场优惠价<input name="price" type="number" min="0" step="0.01" placeholder="0.00"></label></div><label class="check"><input type="checkbox" name="exclusive" checked>优先单品价，不叠加全场优惠</label><div class="quote-preview-card"><div class="section-heading"><div><h3>本场金额</h3><p>同时考虑全场优惠与单品互斥规则。</p></div><button id="calculate-price" type="button" class="primary">计算券后价</button></div><p id="override-quote" class="config-quote" role="status"></p></div></section>
<section class="editor-section"><label>本场单品促销机制<textarea name="promotion" rows="2" maxlength="4000"></textarea></label><label>本场赠品 / 加赠规则<textarea name="gifts" rows="2" maxlength="4000" placeholder="赠品名称、数量与领取条件"></textarea></label><label class="check"><input type="checkbox" name="shipping_override">覆写本场发货时效</label><label id="override-shipping-input">本场特殊发货时效<input name="shipping_time" maxlength="2000" placeholder="例如：本场现货12小时内发出"></label></section>
<div class="dialog-footer"><button id="clear-override" type="button">恢复商品默认</button><button type="submit" class="primary">保存本场单品配置</button></div></form></dialog>
''' + text[end:]
page.write_text(text,encoding='utf-8')
control=root/'web/control.js'
text=control.read_text(encoding='utf-8')
text=text.replace("renderKnowledgeEditor(editingProduct?.knowledge_documents||[]);$('product-dialog').showModal()", "renderKnowledgeEditor(editingProduct?.knowledge_documents||[]);paintProductOptionVisibility();$('product-dialog').showModal()")
text=text.replace("const file=f.elements.image_file.files[0];if(file&&file.size>3000000)throw Error('图片请控制在3MB以内');",'')
text=text.replace("knowledge:f.elements.knowledge.value,knowledge_documents:readKnowledgeEditor(),image:file?await fileAsData(file):editingProduct?.image||''", "knowledge:productHiddenFields.has('knowledge')?'':f.elements.knowledge.value,knowledge_documents:productHiddenFields.has('knowledge_documents')?[]:readKnowledgeEditor(),image:readProductImage()")
control.write_text(text,encoding='utf-8')
print('product and room offer layouts updated')
