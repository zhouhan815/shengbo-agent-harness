// Product facts belong to the library; offers and presentation preferences belong to a room.
let commerceDraft=null,commerceRoomId=null,overrideProductId=null;
let productHiddenFields=new Set(),productImageDraft='',productImageLoading=false,productImageRevision=0;
let quoteGeneration=0,overrideQuoteGeneration=0,lastQuoteStamp=null;
const commerceCopy=value=>JSON.parse(JSON.stringify(value));
const currencyOptions=()=>[['CNY','¥ CNY'],['TWD','NT$ TWD'],['USD','$ USD'],['AUD','A$ AUD']].map(([v,t])=>`<option value="${v}">${t}</option>`).join('');
const emptyOverride=()=>({mode:'none',discount:null,price:null,exclusive:true,promotion:'',gifts:'',shipping_override:false,shipping_time:''});
const emptyCommerce=()=>({scene:'',coupon:{mode:'none',discount:null,amount:null,currency:'CNY',minimum_spend:null,condition:'',scope:'all',product_ids:[]},promotion:'',promotion_scope:'all',promotion_product_ids:[],knowledge_documents:[],product_overrides:{},hidden_sections:[]});
const productOptionLabels={material:'材质',ingredients:'成分',specifications:'规格属性',target_audience:'适用人群',storage_conditions:'储存条件',shelf_life:'保质期',shipping_origin:'商品发货地',shipping_time:'日常发货时效',return_policy:'特殊退换货条款',usage_instructions:'使用说明 / 安装指南',faq:'商品常见问答',knowledge:'知识库 / 注意事项',knowledge_documents:'独立知识文档'};
const roomSectionLabels={scene:'本场场景与口播要求',coupon:'全场优惠',promotion:'本场促销机制',knowledge_documents:'本场知识文档'};

function optionalField(key,content,cls='field-medium'){
  return `<div class="optional-field ${cls}" data-product-option="${key}"><div class="field-heading"><label for="product-${key}">${productOptionLabels[key]}</label><button type="button" class="remove-field" data-remove-product-field="${key}" aria-label="移除${productOptionLabels[key]}">移除</button></div>${content}</div>`;
}
function optionalInput(key,max=2000){return optionalField(key,`<input id="product-${key}" name="${key}" maxlength="${max}">`);}
function optionalText(key,max=4000,rows=2){return optionalField(key,`<textarea id="product-${key}" name="${key}" rows="${rows}" maxlength="${max}"></textarea>`,'field-wide');}
function numberStepper(name,value='',label='数值'){
  return `<div class="number-stepper"><button type="button" data-step="-1" aria-label="减少${label}">−</button><input name="${name}" aria-label="${label}" type="number" min="1" max="100000" step="1" value="${esc(value)}"><button type="button" data-step="1" aria-label="增加${label}">＋</button></div>`;
}
function paintProductFields(product={}){
  productHiddenFields=new Set(product.hidden_optional_fields||[]);const f=$('product-form');
  f.elements.sku_id.value=product.sku_id||product.id||'';f.elements.sale_status.value=product.sale_status||'available';
  $('product-extra').innerHTML=`<section class="editor-section"><div class="section-heading"><div><h3>规格与核心卖点</h3><p>卖点按顺序讲解；规格用于准确回答参数问题。</p></div></div><div data-product-option="specifications" class="optional-field"><div class="field-heading"><strong>规格属性</strong><div><button id="add-specification" type="button">＋ 添加规格</button><button type="button" class="remove-field" data-remove-product-field="specifications">移除</button></div></div><div id="specification-rows"></div></div><div class="field-heading core-heading"><strong>商品核心卖点 <span class="required-mark">*</span></strong><button id="add-core-point" type="button">＋ 添加卖点</button></div><div id="core-point-rows"></div></section><section class="editor-section"><div class="section-heading"><div><h3>商品属性与服务规则</h3><p>填写已确认的事实；不适用的配置项可移除。</p></div></div><div class="compact-grid">${optionalInput('material')}${optionalInput('ingredients')}${optionalInput('shipping_origin',500)}${optionalInput('shipping_time')}${optionalInput('storage_conditions')}${optionalField('shelf_life',`<div class="unit-input">${numberStepper('shelf_life_value',product.shelf_life_value??'','保质期数值')}<select name="shelf_life_unit" aria-label="保质期单位"><option value="day">天</option><option value="month">月</option><option value="year">年</option></select></div>`)}${optionalText('target_audience')}${optionalText('return_policy')}${optionalText('usage_instructions',15000,3)}${optionalText('faq',15000,3)}</div></section>`;
  for(const key of ['material','ingredients','target_audience','storage_conditions','shipping_origin','shipping_time','return_policy','usage_instructions','faq'])f.elements[key].value=product[key]||'';
  f.elements.shelf_life_unit.value=product.shelf_life_unit||'month';
  const specs=product.specifications||[];specs.forEach(addSpecification);if(!specs.length)addSpecification({name:'',value:''});
  (product.core_selling_points?.length?product.core_selling_points:[product.selling_points||'']).forEach(addCorePoint);
  $('add-specification').onclick=()=>addSpecification({name:'',value:''});$('add-core-point').onclick=()=>addCorePoint('');
  document.querySelectorAll('[data-remove-product-field]').forEach(b=>b.onclick=()=>{productHiddenFields.add(b.dataset.removeProductField);paintProductOptionVisibility();});
  $('restore-product-field').onclick=()=>{const key=$('product-optional-select').value;if(key){productHiddenFields.delete(key);paintProductOptionVisibility();document.querySelector(`[data-product-option="${key}"] input, [data-product-option="${key}"] textarea`)?.focus();}};
  $('product-extra').querySelectorAll('[data-step]').forEach(b=>b.onclick=()=>{const input=b.parentElement.querySelector('input');if(b.dataset.step==='1')input.stepUp();else input.stepDown();input.dispatchEvent(new Event('input',{bubbles:true}));});
  paintProductOptionVisibility();resetProductImage(product.image||'');
}
function paintProductOptionVisibility(){
  document.querySelectorAll('[data-product-option]').forEach(node=>{const hidden=productHiddenFields.has(node.dataset.productOption);node.hidden=hidden;node.querySelectorAll('input,select,textarea,button').forEach(input=>input.disabled=hidden);});
  const keys=[...productHiddenFields].filter(key=>productOptionLabels[key]);
  $('product-optional-select').innerHTML=keys.length?keys.map(key=>`<option value="${key}">${productOptionLabels[key]}</option>`).join(''):'<option value="">所有配置项均已展示</option>';
  $('restore-product-field').disabled=$('product-optional-select').disabled=!keys.length;
  $('product-removed-count').textContent=keys.length?`已移除 ${keys.length} 项；保存时会清空对应内容。`:'可选项可随时移除，再从这里添加回来。';
}
function addSpecification(spec){
  const row=document.createElement('div');row.className='config-row';row.innerHTML=`<input aria-label="规格名" placeholder="规格名，如尺码" value="${esc(spec.name)}" maxlength="60"><input aria-label="规格值" placeholder="规格内容，如36–37" value="${esc(spec.value)}" maxlength="500"><button type="button" class="remove-field" aria-label="删除这条规格">删除</button>`;row.querySelector('button').onclick=()=>row.remove();$('specification-rows').append(row);
}
function addCorePoint(text){
  const row=document.createElement('div');row.className='config-row core-point-row';row.innerHTML=`<textarea aria-label="核心卖点" rows="3" maxlength="4000" required>${esc(text)}</textarea><div class="point-actions"><button type="button" data-move="-1" aria-label="卖点上移">↑</button><button type="button" data-move="1" aria-label="卖点下移">↓</button><button type="button" class="remove-field" data-delete aria-label="删除这条卖点">删除</button></div>`;
  row.querySelector('[data-delete]').onclick=()=>row.remove();row.querySelectorAll('[data-move]').forEach(b=>b.onclick=()=>{if(b.dataset.move==='-1'&&row.previousElementSibling)row.before(row.previousElementSibling);else if(b.dataset.move==='1'&&row.nextElementSibling)row.after(row.nextElementSibling);});$('core-point-rows').append(row);
}
function readProductFields(){
  const f=$('product-form'),r={};for(const k of ['sku_id','sale_status','material','ingredients','target_audience','storage_conditions','shelf_life_unit','shipping_origin','shipping_time','return_policy','usage_instructions','faq'])r[k]=f.elements[k].value;
  r.shelf_life_value=f.elements.shelf_life_value.value===''?null:Number(f.elements.shelf_life_value.value);
  r.specifications=[...$('specification-rows').children].map(row=>{const a=row.querySelectorAll('input');return{name:a[0].value.trim(),value:a[1].value.trim()};}).filter(s=>s.name||s.value);
  if(!productHiddenFields.has('specifications')&&r.specifications.some(s=>!s.name||!s.value))throw Error('请填写完整的规格名和规格值，或删除空行');
  r.core_selling_points=[...$('core-point-rows').querySelectorAll('textarea')].map(t=>t.value.trim());if(!r.core_selling_points.length||r.core_selling_points.some(p=>!p))throw Error('请至少填写一条非空核心卖点');
  r.selling_points=r.core_selling_points.join('\n');r.hidden_optional_fields=[...productHiddenFields];
  for(const key of productHiddenFields){if(key==='shelf_life'){r.shelf_life_value=null;r.shelf_life_unit='month';}else if(key in r)r[key]=key==='specifications'?[]:'';}
  return r;
}
function resetProductImage(value){productImageRevision++;productImageDraft=value;productImageLoading=false;$('product-form').elements.image_file.value='';paintProductImage();}
function paintProductImage(){
  const image=$('product-image-preview');image.hidden=!productImageDraft;if(productImageDraft)image.src=productImageDraft;else image.removeAttribute('src');
  $('product-image-empty').hidden=!!productImageDraft;$('remove-product-image').hidden=!productImageDraft;$('product-image-help').textContent=productImageLoading?'正在读取图片…':'JPG、PNG、WebP，最大 3MB';
}
function readProductImage(){if(productImageLoading)throw Error('图片正在读取，请稍候再保存');return productImageDraft;}
$('product-image-file').onchange=async e=>{
  const file=e.target.files[0];if(!file)return;const revision=++productImageRevision;productImageLoading=true;paintProductImage();
  try{if(file.size>3000000)throw Error('商品图片请控制在3MB以内');if(!['image/png','image/jpeg','image/webp'].includes(file.type))throw Error('请上传 JPG、PNG 或 WebP 图片');const data=await fileAsData(file),probe=new Image();probe.src=data;await probe.decode();if(revision===productImageRevision)productImageDraft=data;}
  catch(error){if(revision===productImageRevision)notify(error.name==='EncodingError'?'无法读取这张图片，请更换文件':error.message);}
  finally{if(revision===productImageRevision){productImageLoading=false;paintProductImage();}}
};
$('remove-product-image').onclick=()=>resetProductImage('');

function loadCommerceEditor(value=roomConfig.commerce){commerceRoomId=roomId;commerceDraft=commerceCopy(value||emptyCommerce());commerceDraft.hidden_sections||=[];quoteGeneration++;paintRoomCommerce();}
function readRoomCommerce(){
  if(commerceRoomId!==roomId||!commerceDraft)return commerceCopy(roomConfig.commerce||emptyCommerce());
  const r=commerceCopy(commerceDraft);for(const pid of Object.keys(r.product_overrides))if(!chosenProducts.has(pid))delete r.product_overrides[pid];
  r.coupon.product_ids=r.coupon.product_ids.filter(pid=>chosenProducts.has(pid));r.promotion_product_ids=r.promotion_product_ids.filter(pid=>chosenProducts.has(pid));
  if(r.coupon.scope==='selected'&&commerceDraft.coupon.product_ids.length&&!r.coupon.product_ids.length)r.coupon=emptyCommerce().coupon;
  if(r.promotion_scope==='selected'&&commerceDraft.promotion_product_ids.length&&!r.promotion_product_ids.length){r.promotion='';r.promotion_scope='all';}return r;
}
function roomEditorBlock(key,subtitle,content){return `<section class="editor-block" data-room-section="${key}"><div class="section-heading"><div><h3>${roomSectionLabels[key]}</h3><p>${subtitle}</p></div><button type="button" class="remove-field" data-remove-room-section="${key}">移除</button></div>${content}</section>`;}
function invalidateRoomQuote(){quoteGeneration++;lastQuoteStamp=null;if($('room-quote-state')){$('room-quote-state').textContent='配置已修改，请重新计算';$('room-quote-state').className='quote-stale';$('room-quote-results').innerHTML='<p class="quote-empty">点击“计算本场金额”，查看每件商品当前的单件报价。</p>';}}
function syncRoomQuoteContext(){if(lastQuoteStamp&&quoteStamp()!==lastQuoteStamp)invalidateRoomQuote();}
function commerceChanged(){invalidateRoomQuote();markDirty();}
function paintRoomCommerce(){
  if(!commerceDraft)return;const c=commerceDraft,q=c.coupon;
  $('room-commerce-fields').innerHTML=roomEditorBlock('coupon','先设置优惠规则，再计算本场各商品的到手价。',`<div class="offer-grid"><label class="offer-control">优惠模式<select data-coupon="mode"><option value="none">不设置全场优惠</option><option value="discount">折扣</option><option value="coupon">优惠券</option></select></label><label class="offer-control currency" data-offer-active>币种<select data-coupon="currency">${currencyOptions()}</select></label><label class="offer-control" data-coupon-mode="discount">折扣比率<input name="coupon_discount" type="number" step="0.01" min="0.01" max="0.99" value="${esc(q.discount??'')}" placeholder="如 0.85"><small>0.85 即原价的 85%</small></label><label class="offer-control" data-coupon-mode="coupon">优惠券面额<input name="coupon_amount" type="number" step="0.01" min="0.01" value="${esc(q.amount??'')}" placeholder="如 20"></label><label class="offer-control" data-offer-active>金额门槛<input name="coupon_minimum" type="number" step="0.01" min="0" value="${esc(q.minimum_spend??'')}" placeholder="0 为无门槛"></label><label class="offer-control" data-offer-active>适用范围<select data-coupon="scope"><option value="all">全场商品</option><option value="selected">部分商品</option></select></label></div><div id="coupon-products" class="config-checks"></div><label class="offer-condition" data-offer-active>补充优惠条件<input data-coupon="condition" value="${esc(q.condition)}" maxlength="1000" placeholder="如满100减10；会员等条件需另行确认"></label>`)+
    `<section class="quote-preview-card"><div class="section-heading"><div><h3>本场金额预览 <span id="room-quote-state" class="quote-stale">尚未计算</span></h3><p>包含单品优惠与全场优惠的叠加规则；按单件计算。</p></div><button type="button" class="primary" id="calculate-room-prices">计算本场金额</button></div><div id="room-quote-results"><p class="quote-empty">计算后显示日常售价、本场到手价及优惠是否生效。</p></div></section>`+
    roomEditorBlock('promotion','活动说明与适用商品一起保存，不影响商品库日常资料。',`<div class="offer-grid"><label class="offer-control">适用范围<select data-commerce="promotion_scope"><option value="all">全场商品</option><option value="selected">部分商品</option></select></label></div><div id="promotion-products" class="config-checks"></div><label>本场促销说明<textarea data-commerce="promotion" rows="2" maxlength="4000" placeholder="例如：本场购买两件赠收纳袋，具体条件请写完整。">${esc(c.promotion)}</textarea></label>`)+
    roomEditorBlock('scene','用于本场脚本的表达和场景背景。',`<label>场景与口播要求<textarea data-commerce="scene" rows="3" maxlength="6000" placeholder="例如：周末生活用品专场，语气亲切，先讲使用场景再讲优惠。">${esc(c.scene)}</textarea></label>`)+
    roomEditorBlock('knowledge_documents','供本场问答检索使用，可添加多份文档。',`<div id="room-document-rows"></div><button type="button" id="add-room-document">＋ 添加知识文档</button>`)+
    `<div class="optional-add-bar"><select id="room-optional-select" aria-label="可添加的直播间配置项"></select><button type="button" id="restore-room-section">＋ 添加配置项</button><small id="room-removed-count"></small></div>`;
  document.querySelectorAll('[data-commerce]').forEach(el=>{const k=el.dataset.commerce;if(el.tagName==='SELECT')el.value=c[k];el.oninput=()=>{c[k]=el.value;commerceChanged();if(k==='promotion_scope')paintCommerceScopes();};});
  document.querySelectorAll('[data-coupon]').forEach(el=>{const k=el.dataset.coupon;if(el.tagName==='SELECT')el.value=q[k];el.oninput=()=>{q[k]=el.value;if(k==='mode'){q.discount=null;q.amount=null;document.querySelector('[name="coupon_discount"]').value='';document.querySelector('[name="coupon_amount"]').value='';paintCouponMode();paintCommerceScopes();}commerceChanged();if(k==='scope')paintCommerceScopes();};});
  for(const[name,key]of[['coupon_discount','discount'],['coupon_amount','amount'],['coupon_minimum','minimum_spend']])document.querySelector(`[name="${name}"]`).oninput=e=>{q[key]=e.target.value===''?null:Number(e.target.value);commerceChanged();};
  $('add-room-document').onclick=()=>{if(c.knowledge_documents.length>=20)return notify('本场最多20份文档');c.knowledge_documents.push({id:crypto.randomUUID(),title:'',content:''});paintRoomDocuments();commerceChanged();};
  document.querySelectorAll('[data-remove-room-section]').forEach(b=>b.onclick=()=>{const key=b.dataset.removeRoomSection;c.hidden_sections=[...new Set([...c.hidden_sections,key])];if(key==='coupon')c.coupon=emptyCommerce().coupon;else if(key==='promotion'){c.promotion='';c.promotion_scope='all';c.promotion_product_ids=[];}else c[key]=key==='knowledge_documents'?[]:'';commerceChanged();paintRoomCommerce();});
  $('restore-room-section').onclick=()=>{const key=$('room-optional-select').value;if(key){c.hidden_sections=c.hidden_sections.filter(k=>k!==key);commerceChanged();paintRoomCommerce();}};
  $('calculate-room-prices').onclick=calculateRoomPrices;paintRoomDocuments();paintCommerceScopes();paintCouponMode();paintRoomSectionVisibility();
}
function paintRoomSectionVisibility(){
  const hidden=commerceDraft.hidden_sections||[];document.querySelectorAll('[data-room-section]').forEach(node=>node.hidden=hidden.includes(node.dataset.roomSection));
  $('room-optional-select').innerHTML=hidden.length?hidden.map(k=>`<option value="${k}">${roomSectionLabels[k]}</option>`).join(''):'<option value="">所有配置项均已展示</option>';
  $('restore-room-section').disabled=$('room-optional-select').disabled=!hidden.length;$('room-removed-count').textContent=hidden.length?`已移除 ${hidden.length} 项，移除的内容不再生效。`:'不需要的配置项可以移除，再添加回来。';
}
function paintCouponMode(){
  const mode=commerceDraft.coupon.mode;document.querySelectorAll('[data-coupon-mode]').forEach(el=>el.hidden=el.dataset.couponMode!==mode);document.querySelectorAll('[data-offer-active]').forEach(el=>el.hidden=mode==='none');
  $('coupon-mode-hint')?.remove();const hint=document.createElement('p');hint.id='coupon-mode-hint';hint.className='muted';hint.textContent=mode==='none'?'未设置全场优惠；仍可计算单品优惠价。':mode==='discount'?'折扣在单品优惠后计算，受单品互斥开关控制。':'优惠券须满足金额门槛，额外资格条件需确认。';document.querySelector('[data-room-section="coupon"]').append(hint);
}
function paintRoomDocuments(){
  const c=commerceDraft;$('room-document-rows').innerHTML=c.knowledge_documents.map((d,i)=>`<div class="knowledge-editor-row"><label>文档标题<input data-room-title="${i}" value="${esc(d.title)}" maxlength="120"></label><label>文档内容<textarea data-room-content="${i}" rows="3" maxlength="15000">${esc(d.content)}</textarea></label><button type="button" class="remove-field" data-room-remove="${i}">删除文档</button></div>`).join('');
  for(const k of['title','content'])document.querySelectorAll(`[data-room-${k}]`).forEach(el=>el.oninput=()=>{c.knowledge_documents[Number(el.getAttribute(`data-room-${k}`))][k]=el.value;commerceChanged();});document.querySelectorAll('[data-room-remove]').forEach(el=>el.onclick=()=>{c.knowledge_documents.splice(Number(el.dataset.roomRemove),1);commerceChanged();paintRoomDocuments();});
}
function paintCommerceScopes(){
  if(!commerceDraft)return;for(const[target,owner,key,show]of[['coupon-products',commerceDraft.coupon,'product_ids',commerceDraft.coupon.scope==='selected'&&commerceDraft.coupon.mode!=='none'],['promotion-products',commerceDraft,'promotion_product_ids',commerceDraft.promotion_scope==='selected']]){
    const el=$(target);if(!el)continue;el.hidden=!show;el.innerHTML=library.products.filter(p=>chosenProducts.has(p.id)).map(p=>`<label class="check"><input type="checkbox" value="${p.id}" ${owner[key].includes(p.id)?'checked':''}>${esc(p.name)}</label>`).join('');el.querySelectorAll('input').forEach(input=>input.onchange=()=>{owner[key]=input.checked?[...new Set([...owner[key],input.value])]:owner[key].filter(id=>id!==input.value);commerceChanged();});
  }
}
function quoteStamp(){return JSON.stringify({room:roomId,commerce:readRoomCommerce(),products:library.products.filter(p=>chosenProducts.has(p.id)).map(p=>({id:p.id,price:p.price,currency:p.currency,updated_at:p.updated_at}))});}
function quoteResultMarkup(rows){return `<div class="quote-table-scroll"><table class="quote-result-table"><thead><tr><th>商品</th><th>日常售价</th><th>本场单件到手价</th><th>计算说明</th></tr></thead><tbody>${rows.map(({product,result,error})=>`<tr><td><strong>${esc(product.name)}</strong><small>${esc(product.sku_id||product.id)}</small></td><td>${esc(product.price)} ${esc(product.currency)}</td><td class="quote-amount">${error?'计算失败':result.confirmed?`${esc(result.final_price)} <small>${esc(result.currency)}</small>`:'待确认'}</td><td>${esc(error||result.reason)}</td></tr>`).join('')}</tbody></table></div>`;}
async function calculateRoomPrices(){
  const products=library.products.filter(p=>chosenProducts.has(p.id));if(!products.length){$('room-quote-results').innerHTML='<p class="quote-empty">请先在“商品库选品”中选择本场商品。</p>';return;}
  const identity=roomId,stamp=quoteStamp(),generation=++quoteGeneration,button=$('calculate-room-prices');button.disabled=true;button.textContent='计算中…';
  try{
    if([...chosenProducts].some(id=>!roomConfig.product_ids.includes(id)))throw Error('请先保存本场选品，再计算金额。');
    const c=readRoomCommerce(),results=await Promise.allSettled(products.map(p=>api(`/api/rooms/${identity}/price-preview`,'POST',{product_id:p.id,commerce:c})));
    if(roomId!==identity||quoteGeneration!==generation||quoteStamp()!==stamp)return;
    const rows=results.map((r,i)=>({product:products[i],...(r.status==='fulfilled'?{result:r.value}:{error:r.reason.message})}));$('room-quote-results').innerHTML=quoteResultMarkup(rows);lastQuoteStamp=stamp;
    const pending=rows.some(r=>r.error||!r.result.confirmed);$('room-quote-state').textContent=pending?'部分结果需核对':'已计算';$('room-quote-state').className=pending?'quote-stale':'quote-current';
  }catch(error){if(roomId===identity&&quoteGeneration===generation){$('room-quote-results').innerHTML=`<p class="quote-empty">${esc(error.message)}</p>`;$('room-quote-state').textContent='计算未完成';}}
  finally{if(button.isConnected){button.disabled=false;button.textContent='计算本场金额';}}
}
function openRoomProduct(pid){
  overrideProductId=pid;overrideQuoteGeneration++;const p=library.products.find(p=>p.id===pid),o=commerceDraft.product_overrides[pid]||emptyOverride();$('override-name').textContent=`${p.name} · SKU ${p.sku_id||p.id} · 日常售价 ${p.price} ${p.currency}`;
  const f=$('override-form');f.reset();for(const k of['mode','discount','price','promotion','gifts','shipping_time'])f.elements[k].value=o[k]??'';f.elements.exclusive.checked=o.exclusive;f.elements.shipping_override.checked=o.shipping_override;$('override-quote').textContent='日常发货：'+(p.shipping_time||'未配置');paintOverrideMode();$('override-dialog').showModal();
}
function paintOverrideMode(){const f=$('override-form');document.querySelectorAll('[data-override-mode]').forEach(el=>{el.hidden=el.dataset.overrideMode!==f.elements.mode.value;el.querySelector('input').disabled=el.hidden;});$('override-shipping-input').hidden=!f.elements.shipping_override.checked;f.elements.shipping_time.disabled=!f.elements.shipping_override.checked;}
function readOverride(){const f=$('override-form'),o=emptyOverride();for(const k of['mode','promotion','gifts','shipping_time'])o[k]=f.elements[k].value;for(const k of['price','discount'])o[k]=f.elements[k].value===''?null:Number(f.elements[k].value);if(o.mode!=='discount')o.discount=null;if(o.mode!=='price')o.price=null;o.exclusive=f.elements.exclusive.checked;o.shipping_override=f.elements.shipping_override.checked;return o;}
$('override-form').oninput=()=>{overrideQuoteGeneration++;$('override-quote').textContent='配置已修改，请重新计算券后价';paintOverrideMode();};
$('override-form').onsubmit=e=>{e.preventDefault();commerceDraft.product_overrides[overrideProductId]=readOverride();commerceChanged();renderRoom();$('override-dialog').close();notify('本场单品配置已更新，正在保存');};
$('clear-override').onclick=()=>{delete commerceDraft.product_overrides[overrideProductId];commerceChanged();renderRoom();$('override-dialog').close();};
$('calculate-price').onclick=async()=>{
  const identity=roomId,pid=overrideProductId,generation=++overrideQuoteGeneration,button=$('calculate-price');button.disabled=true;
  try{if(!roomConfig.product_ids.includes(pid))throw Error('请先保存本场选品，再计算金额。');const c=readRoomCommerce();c.product_overrides[pid]=readOverride();const r=await api(`/api/rooms/${identity}/price-preview`,'POST',{product_id:pid,commerce:c});if(roomId===identity&&overrideProductId===pid&&overrideQuoteGeneration===generation)$('override-quote').textContent=(r.confirmed?`本场单件到手价 ${r.final_price} ${r.currency}`:'最终价待确认')+'；'+r.reason;}
  catch(error){if(roomId===identity&&overrideQuoteGeneration===generation)$('override-quote').textContent=error.message;}finally{button.disabled=false;}
};
$('save-commerce').onclick=async()=>{try{await saveRoom();notify('直播间业务配置已保存');}catch(error){notify(error.message);}};
