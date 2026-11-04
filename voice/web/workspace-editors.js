/* Agent workbench: inline human editors over PRELIVE-2.3. No legacy globals. */
(function (global) {
  'use strict';
  const MODULES = {opening_long:'长开场',opening_short:'短引入',product_intro:'商品介绍',selling_points:'卖点讲解',campaign:'本场活动',cta:'促单',transition:'衔接'};
  const FIELD_LABELS = {name:'商品名称',sku_id:'商品内部编号 / SKU',sale_status:'在售状态',sales_platform:'销售平台',price:'日常售价',currency:'币种',promo_price:'历史促销价（商品库）',selling_points:'商品卖点',core_selling_points:'核心卖点',promotion:'商品长期促销说明',knowledge:'商品知识库',knowledge_documents:'知识文档',specifications:'规格参数',material:'材质',ingredients:'成分',target_audience:'目标顾客',storage_conditions:'储存条件',shelf_life_value:'保质期数值',shelf_life_unit:'保质期单位',shelf_life:'保质期',shipping_origin:'发货地',shipping_time:'发货时效',return_policy:'售后政策',usage_instructions:'使用方法',faq:'常见问题',image:'商品图片',hidden_optional_fields:'不适用的可选字段'};
  const PLATFORM_LABELS={unspecified:'未指定',douyin:'抖音',tiktok:'TikTok',kuaishou:'快手',other:'其他平台'};
  const selectionItems=value=>value?(Array.isArray(value.items)?value.items:[value]):[];
  function selectionKey(item){const t=item?.target||{},r=t.object_ref||{};return JSON.stringify([r.kind,r.id,t.field_path||'',t.module_key||'',t.sentence_ids||[]]);}
  function unpackCorePoint(value){const text=String(value??''),match=/^([^\r\n：:]{1,120})[：:]\s*([\s\S]*)$/.exec(text);return match?{name:match[1].trim(),content:match[2],original:text}:{name:'',content:text,original:text};}
  function packCorePoint(value){const name=String(value.name||'').trim(),content=String(value.content||'').trim(),original=unpackCorePoint(value.original||'');if(name===original.name&&content===original.content.trim())return value.original||content;return name?name+'：'+content:content;}
  const OPTIONAL_PRODUCT_FIELDS={material:['material'],ingredients:['ingredients'],specifications:['specifications'],target_audience:['target_audience'],storage_conditions:['storage_conditions'],shelf_life:['shelf_life_value','shelf_life_unit'],shipping_origin:['shipping_origin'],shipping_time:['shipping_time'],return_policy:['return_policy'],usage_instructions:['usage_instructions'],faq:['faq'],knowledge:['knowledge'],knowledge_documents:['knowledge_documents']};
  function activeProductFields(value){const result=JSON.parse(JSON.stringify(value));for(const group of result.hidden_optional_fields||[])for(const key of OPTIONAL_PRODUCT_FIELDS[group]||[])result[key]=key==='shelf_life_value'?null:key==='shelf_life_unit'?'month':['specifications','knowledge_documents'].includes(key)?[]:'';return result;}
  function applyOptionalProductVisibility(fields){const hidden=new Set(fields.hidden_optional_fields?.read()||[]);for(const[group,keys]of Object.entries(OPTIONAL_PRODUCT_FIELDS))for(const key of keys)if(fields[key]){fields[key].box.hidden=hidden.has(group);fields[key].box.inert=hidden.has(group);}return hidden;}
  const STATES={draft:'准备中',ready_to_adopt:'语音候选已备好，待采用',candidate_requires_choice:'待确认商品版本差异'};
  function preparationTitle(preparation,draft){const body=preparation?.body||{},theme=String(draft?.body?.draft?.theme||'').trim();const firstLine=String(theme||body.goal||'本场准备').split(/\r?\n/).find(line=>line.trim())||'本场准备';const title=firstLine.replace(/\s+/g,' ').trim(),characters=Array.from(title);return characters.length>48?characters.slice(0,48).join('')+'…':title;}
  function preparationAdopted(preparation,room){const candidate=preparation?.body?.candidate_publication_ref,adopted=room?.body?.adopted_publication_ref;return !!candidate&&!!adopted&&room.body.adopted_preparation_id===preparation.ref.id&&['kind','id','revision','content_hash'].every(key=>candidate[key]!==undefined&&candidate[key]===adopted[key]);}
  function preparationStateLabel(preparation,room){return preparationAdopted(preparation,room)?'当前直播间已采用（未代表开播）':STATES[preparation?.body?.status]||preparation?.body?.status||'准备中';}
  const copy=v=>v===undefined?undefined:JSON.parse(JSON.stringify(v));
  const equal=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
  const canonicalProductField=value=>JSON.stringify(value,(_,item)=>
    item && typeof item==='object' && !Array.isArray(item)
      ? Object.fromEntries(Object.keys(item).sort().map(key=>[key,item[key]])) : item);
  const E=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;};
  const uid=()=>global.ShengboWorkbenchApi?.uuid?.()||crypto.randomUUID();
  const jsonDetails=(label,value)=>{const d=E('details',undefined,'wb-evidence');d.append(E('summary',label),E('pre',JSON.stringify(value,null,2),'wb-mono'));return d;};
  const refName=r=>r?'修订 '+r.revision+' · '+r.id:'尚未保存';
  const safeUrl=url=>typeof url==='string'&&/^\/(?:audio|room-audio)\/[A-Za-z0-9_./:-]+$/.test(url)&&!url.includes('..')?url:null;
  const newVariantModuleKeys=position=>[position%5===1?'opening_long':'opening_short','product_intro','selling_points','campaign','cta'];
  const SCRIPT_MODULE_ORDER=['opening_long','opening_short','product_intro','selling_points','campaign','cta','transition'];
  function scriptTextMetrics(script,position){
    const modules=script.modules||{},preferred=position===undefined?null:(position%5===1?'opening_long':'opening_short');
    const opening=preferred&&modules[preferred]?preferred:modules.opening_long?'opening_long':modules.opening_short?'opening_short':null;
    const keys=[...SCRIPT_MODULE_ORDER.filter(key=>key===opening||!key.startsWith('opening_')),...Object.keys(modules).filter(key=>!SCRIPT_MODULE_ORDER.includes(key))].filter(key=>modules[key]);
    const playbackKeys=keys.filter(key=>['opening_long','opening_short','product_intro','selling_points','campaign','cta'].includes(key));
    const text=playbackKeys.flatMap(key=>(modules[key].sentences||[]).map(sentence=>sentence.spoken_text||sentence.text||'')).join('\n');
    const characters=(text.match(/[\p{L}\p{N}]/gu)||[]).length;
    return {keys,playback_keys:playbackKeys,text,characters,estimated_minutes:Math.round(characters/240*10)/10,opening,requested_opening:preferred};
  }
  function currentScriptVariants(scripts=[]){const variants=new Map();for(const script of scripts){const key=script.body.product_id+':'+(script.body.variant_id||script.body.version_number||script.ref.id),old=variants.get(key);if(!old||Number(script.body.script_revision||script.ref.revision)>=Number(old.body.script_revision||old.ref.revision))variants.set(key,script);}return [...variants.values()];}
  function playbackSummary(review){
    const statistics=review.playback_statistics;
    const synthesis='本场合成范围（去重）：'+review.texts.length+' 句 · '+review.cost_scope.characters+' 字符';
    if(statistics?.basis==='saved_queue_one_cycle')return '按保存顺序实播一轮：'+statistics.entry_count+' 项 · '+statistics.sentence_count+' 句 · '+statistics.spoken_characters+' 字符；每项计入实际选用的一种开场。'+synthesis;
    return synthesis+'；实播统计尚未就绪，未将长短开场合计为实播长度。';
  }
  function matchingLocks(locks,region){return (locks||[]).flatMap(lock=>(lock.targets||[]).filter(target=>target.kind===region.kind&&target.id===region.id&&(!target.script_id||!region.script_id||target.script_id===region.script_id)&&(!target.product_ids?.length||!region.product_id||target.product_ids.includes(region.product_id))&&(!target.module_keys?.length||!region.module_key||target.module_keys.includes(region.module_key))).map(target=>({...target,run_id:lock.run_id,status:lock.status,reason:lock.reason})));}
  function schemaDefault(schema,defs){if(schema.$ref)return schemaDefault(defs[schema.$ref.split('/').pop()]||{},defs);if('default'in schema)return copy(schema.default);if(schema.anyOf){const s=schema.anyOf.find(v=>v.type!=='null')||{};return 'default'in schema?copy(schema.default):s.type==='number'?null:schemaDefault(s,defs);}if(schema.type==='array')return [];if(schema.type==='object')return {};if(schema.type==='number'||schema.type==='integer')return 0;if(schema.type==='boolean')return false;return '';}
  function effectiveSchema(schema,defs){if(schema.$ref)return effectiveSchema(defs[schema.$ref.split('/').pop()]||{},defs);if(schema.anyOf)return {...effectiveSchema(schema.anyOf.find(v=>v.type!=='null')||{},defs),nullable:schema.anyOf.some(v=>v.type==='null')};return schema;}
  function reusableDraft(source={}, options={}) {
    const draft=copy(source), commerce=draft.commerce||{};
    const expiry=Date.parse(draft.campaign_ends_at||'');
    const campaignAllowed=options.includeCampaign===true&&Number.isFinite(expiry)&&expiry>(options.now||Date.now());
    draft.theme='';draft.objective=options.goal||'准备本场直播';draft.scheduled_at='';draft.loop_mode='infinite';
    if(!campaignAllowed)delete draft.campaign_starts_at;
    draft.campaign_ends_at=campaignAllowed?draft.campaign_ends_at:'';
    const excludedDocuments=[];
    if(!campaignAllowed){commerce.coupon={mode:'none',currency:commerce.coupon?.currency||'CNY',scope:'all',product_ids:[]};commerce.promotion='';commerce.promotion_scope='all';commerce.promotion_product_ids=[];for(const[id,value]of Object.entries(commerce.product_overrides||{}))commerce.product_overrides[id]={...value,mode:'none',price:null,discount:null,promotion:'',gifts:''};commerce.knowledge_documents=(commerce.knowledge_documents||[]).filter(doc=>{const timed=/活动|限时|促销|折扣|赠品|今天|明天|本场|20\d{2}[-/年]|\d{1,2}月\d{1,2}日/.test((doc.title||'')+' '+(doc.content||''));if(timed)excludedDocuments.push({id:doc.id,title:doc.title});return !timed;});}
    draft.commerce=commerce;draft.inheritance_decisions=[...(draft.inheritance_decisions||[]),{kind:'human_copy_reusable_config',campaign:campaignAllowed?'explicitly_included':'not_inherited',excluded_documents:excludedDocuments,copied_at:new Date(options.now||Date.now()).toISOString()}];
    return draft;
  }
  function trimCommerceReferences(source={},refs=[]){const value=copy(source),ids=new Set(refs.map(r=>r.id));value.product_overrides=Object.fromEntries(Object.entries(value.product_overrides||{}).filter(([id])=>ids.has(id)));if(value.coupon){value.coupon.product_ids=(value.coupon.product_ids||[]).filter(id=>ids.has(id));if(value.coupon.scope==='selected'&&!value.coupon.product_ids.length)value.coupon={mode:'none',currency:value.coupon.currency||'CNY',scope:'all',product_ids:[]};}value.promotion_product_ids=(value.promotion_product_ids||[]).filter(id=>ids.has(id));if(value.promotion_scope==='selected'&&!value.promotion_product_ids.length){value.promotion='';value.promotion_scope='all';}return value;}
  function mount(options={}) {
    if(!options.container)throw Error('业务工作区缺少容器');
    const api=options.api||global.ShengboWorkbenchApi;
    if(!api)throw Error('业务工作区 API 尚未初始化');
    const container=options.container;const storage=options.storage===null?null:(options.storage||global.localStorage);const prefix='shengbo.workbench.buffer.v2:';
    const buffers=new Map(),cache=new Map(),replacements=new Map(),operations=new Map(),productMaterialFlows=new Map(),catalogViewStates=new Map();
    const pendingRoomCreations=new Map(), roomMaterialFlows=new Map();
    let unknownOperation=null;try{const pending=JSON.parse(storage?.getItem(prefix+'pending-operation')||'null');if(pending?.operationId&&pending?.fingerprint)unknownOperation=pending;}catch{}
    let current={view:'overview'},snapshot=null,schema=null,voiceOptions=null,openGeneration=0,busy=false,disposed=false,scriptReader=null,productDialog=null;
    let editorLocks=[],selectedKeys=new Set();const lockedControls=new Map();
    const status=E('div',undefined,'wb-inline-alert');status.hidden=true;status.setAttribute('role','status');
    const content=E('div',undefined,'wb-editor-content');container.replaceChildren(status,content);
    let catalogResizeObserver=null, activeRoomOverlay=null;
    const editorTargets=selector=>new Set([...container.querySelectorAll(selector),...(options.roomOverlay?.querySelectorAll(selector)||[]),...(productDialog?.dialog.querySelectorAll(selector)||[])]);
    function routedAudio(node){Promise.resolve(options.onAudioElement?.(node)).catch(e=>notice(e.message||'试听输出设备未就绪，请重新选择播放设备。'));return node;}
    function region(host,descriptor){host.dataset.agentRegion=JSON.stringify(descriptor);return host;}
    function applyEditorLocks(){
      if(disposed)return;const desired=new Map();
      for(const host of editorTargets('[data-agent-region]')){
        const descriptor=JSON.parse(host.dataset.agentRegion),matches=matchingLocks(editorLocks,descriptor);let count=0;
        for(const control of host.querySelectorAll('input,textarea,select,button,[draggable]')){
          if(control.dataset.readOnlyAction==='true'||control.closest('[data-product-material-flow]'))continue;
          const field=control.closest('[data-agent-field]')?.dataset.agentField;
          const blocked=matches.some(target=>!target.field_paths?.length||control.tagName==='BUTTON'&&!control.closest('[data-agent-field]')||field&&target.field_paths.some(path=>field===path||field.startsWith(path+'/')||path.startsWith(field+'/')));
          if(blocked){desired.set(control,matches[0]);count++;}
        }
        host.classList.toggle('wb-agent-locked',count>0);host.dataset.agentLocked=String(count>0);
        let note=Array.from(host.children).find(n=>n.classList?.contains('wb-agent-lock-note'));
        if(count&&!note){note=E('p',undefined,'wb-agent-lock-note');note.setAttribute('role','status');host.prepend(note);}
        if(note){note.hidden=!count;if(count){const text='Agent 正在处理此区域（'+matches[0].status+'）。相关输入暂时锁定；已有草稿保留，其他区域仍可编辑。';if(note.textContent!==text)note.textContent=text;}}
      }
      for(const[control,original]of lockedControls)if(!desired.has(control)){control.disabled=original.disabled;if(original.draggable!==null)control.setAttribute('draggable',original.draggable);control.removeAttribute('aria-disabled');control.removeAttribute('data-agent-input-locked');lockedControls.delete(control);}
      for(const[control,lock]of desired){if(!lockedControls.has(control))lockedControls.set(control,{disabled:!!control.disabled,draggable:control.getAttribute('draggable')});control.disabled=true;if(control.hasAttribute('draggable'))control.draggable=false;control.setAttribute('aria-disabled','true');control.dataset.agentInputLocked=lock.run_id;}
    }
    function setEditorLocks(value){editorLocks=copy(value?.locks||value||[]);applyEditorLocks();}
    function setSelected(value){selectedKeys=new Set(selectionItems(value).map(selectionKey));for(const n of editorTargets('[data-selection-key]')){const active=selectedKeys.has(n.dataset.selectionKey);n.classList.toggle('is-agent-selected',active);n.setAttribute('aria-pressed',String(active));}}
    function selectable(node,fn,item){const key=selectionKey(item);node.classList.add('wb-select-target');node.dataset.selectionKey=key;if(node.tagName==='LABEL')node.removeAttribute('for');node.setAttribute('role','button');node.tabIndex=0;node.title='选中此项，在右侧说明修改要求；再次点击取消';const activate=event=>{event.preventDefault();event.stopPropagation();guard(fn);};node.addEventListener('click',activate);node.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){activate(event);}});node.classList.toggle('is-agent-selected',selectedKeys.has(key));node.setAttribute('aria-pressed',String(selectedKeys.has(key)));return node;}
    const lockObserver=new MutationObserver(()=>applyEditorLocks());lockObserver.observe(content,{childList:true,subtree:true});
    function preventLockedEdit(event){if(event.target.closest?.('[data-read-only-action="true"]'))return;if(event.target.closest?.('[data-agent-input-locked]')){event.preventDefault();event.stopImmediatePropagation();}}
    container.addEventListener('click',preventLockedEdit,true);
    options.roomOverlay?.addEventListener('click',preventLockedEdit,true);
    function closeScriptReader({restoreFocus=true,force=false}={}){if(!scriptReader)return true;if(!force&&[...buffers.values()].some(b=>b.dirty&&scriptReader.contains(b.host))){readerNotice('请先保存或取消正在编辑的句子，再关闭或切换话术。','warning');return false;}const origin=scriptReader._origin,backScrollTop=scriptReader._backScrollTop||0;for(const item of scriptReader._underlay||[]){item.node.inert=item.inert;if(item.ariaHidden===null)item.node.removeAttribute('aria-hidden');else item.node.setAttribute('aria-hidden',item.ariaHidden);}scriptReader._panel?.classList.remove('wb-script-reader-open');scriptReader.remove();scriptReader=null;content.inert=false;content.removeAttribute('aria-hidden');container.classList.remove('wb-has-script-reader');container.scrollTop=backScrollTop;if(restoreFocus)(origin?.isConnected?origin:container).focus({preventScroll:true});return true;}
    function readerNotice(message,tone='info'){if(!scriptReader)return;let alert=scriptReader.querySelector('[data-sentence-save-status]');if(!alert){alert=E('p',undefined,'wb-inline-alert');alert.dataset.sentenceSaveStatus='';alert.setAttribute('role','status');scriptReader.querySelector('.wb-script-reader-heading')?.after(alert);}alert.textContent=message;alert.dataset.tone=tone;}
    function readerKeydown(event){if(event.key==='Escape'&&scriptReader){event.preventDefault();closeScriptReader();}}
    document.addEventListener('keydown',readerKeydown);
    function showProductCloseConfirmation(state){
      if(state.closeConfirmation?.open)return;
      const {dialog,editFocus}=state;
      const confirm=E('dialog',undefined,'wb-product-close-confirm');
      const title=E('h3','关闭商品编辑？');
      const explanation=E('p','此商品还有未保存修改。请选择如何处理草稿。');
      title.id='wb-product-close-title-'+uid();
      explanation.id=title.id+'-description';
      confirm.setAttribute('aria-labelledby',title.id);
      confirm.setAttribute('aria-describedby',explanation.id);
      const resume=()=>{
        confirm.close();
        confirm.remove();
        state.closeConfirmation=null;
        if(editFocus?.isConnected)editFocus.focus({preventScroll:true});
      };
      const continueEditing=button('继续编辑',resume,false,{readOnlyAction:'true'});
      const actions=E('div',undefined,'wb-product-close-actions');
      actions.append(continueEditing);
      if(unknownOperation?.command!=='save_product'){
        const discard=button('放弃草稿并关闭',()=>dismissProductDialog({discardDraft:true}),false,{readOnlyAction:'true'});
        discard.classList.add('wb-catalog-danger');
        actions.append(discard);
      }
      actions.append(button('保留草稿并关闭',()=>dismissProductDialog({keepDraft:true}),true,{readOnlyAction:'true'}));
      confirm.append(title,explanation,actions);
      confirm.addEventListener('cancel',event=>{event.preventDefault();resume();});
      dialog.append(confirm);
      state.closeConfirmation=confirm;
      confirm.showModal();
      continueEditing.focus({preventScroll:true});
    }
    function closeProductDialog({restoreFocus=true,keepDraft=false,discardDraft=false}={}){
      if(!productDialog)return true;
      const {dialog,backdrop,origin,draftKey,productId}=productDialog;
      const draft=draftKey&&buffers.get(draftKey);
      if(draft?.dirty&&!keepDraft&&!discardDraft){
        showProductCloseConfirmation(productDialog);
        return false;
      }
      if(discardDraft&&draft)clearBuffer(draft);
      if(productDialog.closeConfirmation?.open)productDialog.closeConfirmation.close();
      productDialog=null;
      dialog.close();
      dialog.remove();
      backdrop.remove();
      lockObserver.disconnect();
      lockObserver.observe(content,{childList:true,subtree:true});
      content.inert=false;
      applyEditorLocks();
      if(restoreFocus){
        const row=!origin?.isConnected&&productId?content.firstElementChild?._showCatalogProduct?.(productId,{clearSearch:false}):null;
        const target=origin?.isConnected?origin:row?.querySelector('[data-open-product]')||content.querySelector(productId?'input[type="search"]':'[data-new-product]');
        target?.focus({preventScroll:true});
      }
      return true;
    }
    function closeCatalogImportDialog(){
      if(productDialog?.dialog.dataset.catalogDialog!=='upload')return false;
      const flow=productMaterialFlows.get('create');
      if(flow?.pending||flow?.batch||productDialog.dialog.querySelector('input[type=file]')?.files.length)return false;
      return closeProductDialog({restoreFocus:false});
    }
    function dismissProductDialog(options){
      const productId=productDialog?.productId;
      if(!closeProductDialog(options))return;
      if(productId&&current.product_id===productId)return navigate({view:'products',product_id:null,action:null});
    }
    async function openProductDialog(origin,{record=null,upload=false,copyFrom=null,archiveProduct=null,preserveFocus=false}={}){
      if(productDialog)return;
      const roomModal=options.roomOverlay?.open;
      const panel=roomModal?options.roomOverlay:container.closest('.wb-work-panel')||container;
      const previousFocus=document.activeElement;
      const backdrop=E('div',undefined,'wb-product-dialog-backdrop');
      backdrop.setAttribute('aria-hidden','true');
      const dialog=E('dialog',undefined,'wb-product-dialog');
      const productId=record?.ref.id||null;
      dialog.dataset.catalogDialog=archiveProduct?'archive':upload?'upload':productId?'detail':'create';
      const title=archiveProduct?'删除商品':upload?'上传资料新增商品':record?'编辑商品 · '+record.body.name:copyFrom?'复制为新商品':'新建商品';
      const heading=E('h2',title);
      heading.id='wb-product-dialog-title-'+uid();
      dialog.setAttribute('aria-labelledby',heading.id);
      dialog.setAttribute('aria-modal',String(!!roomModal));
      if(roomModal)dialog.classList.add('wb-room-product-detail');
      const head=E('div',undefined,'wb-product-dialog-head');
      const headActions=E('div',undefined,'wb-product-dialog-head-actions');
      headActions.append(button('关闭',()=>dismissProductDialog(),false,{readOnlyAction:'true'}));
      head.append(heading,headActions);
      const feedback=E('div',undefined,'wb-product-dialog-message wb-inline-alert');
      feedback.setAttribute('role','status');
      feedback.hidden=true;
      const body=E('div',undefined,'wb-product-dialog-body');
      body._productHeading=heading;
      dialog.append(head,feedback,body);
      const draftKey=upload||archiveProduct?null:copyFrom?'product:new:copy:'+copyFrom.ref.id:'product:'+(productId||'new');
      const state=productDialog={dialog,backdrop,origin,body,productId,draftKey,status:feedback};
      dialog.addEventListener('focusin',event=>{if(body.contains(event.target))state.editFocus=event.target;});
      dialog.addEventListener('keydown',event=>{
        if(event.key!=='Escape'||state.closeConfirmation?.open)return;
        event.preventDefault();
        if(!busy)guard(()=>dismissProductDialog());
      });
      dialog.addEventListener('click',preventLockedEdit,true);
      content.inert=true;
      if(!roomModal)panel.append(backdrop);
      panel.append(dialog);
      lockObserver.observe(dialog,{childList:true,subtree:true});
      if(roomModal)dialog.showModal();else dialog.show();
      if(preserveFocus&&previousFocus?.isConnected)previousFocus.focus({preventScroll:true});
      try{
        if(archiveProduct){
          dialog.classList.add('wb-product-dialog--confirm');
          const scroll=E('div',undefined,'wb-product-dialog-scroll');
          scroll.append(E('p','将「'+archiveProduct.name+'」从商品知识库移除。历史商品资料和话术会保留。引用该商品的本场需要重新核对选品。'));
          const actions=E('div',undefined,'wb-product-dialog-actions wb-form-actions');
          const cancel=button('取消',()=>dismissProductDialog());
          const confirm=button('确认删除',async()=>{
            if(hasDirty())throw Error('请先保存或放弃未保存编辑，再删除商品。');
            const result=await humanMutation('/api/prelive/v2/human/product-archive',{product_ref:archiveProduct.ref});
            changedBusiness({data:result});
            closeProductDialog({restoreFocus:false});
            try{
              await navigate({view:'products',product_id:null,action:null,refresh:true});
              content.querySelector('input[type="search"]')?.focus({preventScroll:true});
              notice('商品已从商品知识库移除，历史记录仍可查看。');
            }catch(error){notice('商品已移除。请重新打开商品知识库刷新列表。','warning');}
          },true);
          confirm.classList.add('wb-catalog-danger');
          actions.append(cancel,confirm);
          body.append(scroll,actions);
          cancel.focus({preventScroll:true});
          return body;
        }
        if(upload){
          const scroll=E('div',undefined,'wb-product-dialog-scroll');
          body.append(scroll);
          renderProductMaterials(scroll);
          if(!preserveFocus)scroll.querySelector('input[type="file"]')?.focus({preventScroll:true});
          return body;
        }
        await renderProduct(body,productId,record,{draftKey,copyFrom,onSaved:async (ref,{unchanged=false}={})=>{
          if(productDialog?.dialog!==dialog){
            if(!unchanged)cache.clear();
            return;
          }
          closeProductDialog({restoreFocus:false});
          const message=unchanged?'没有需要保存的修改。':productId?'商品已保存。':'商品已保存到商品知识库。';
          try{
            await navigate({view:'products',product_id:null,action:null,refresh:!unchanged});
            const catalog=content.firstElementChild;
            const row=catalog?._showCatalogProduct?.(ref?.id||productId,{highlight:true});
            if(row)markLocation(content,row,row.querySelector('[data-open-product]'));
            else content.querySelector(productId?'input[type="search"]':'[data-new-product]')?.focus({preventScroll:true});
            notice(message);
          }catch(error){
            notice(unchanged?message:'商品已保存。请重新打开商品知识库查看最新资料。','warning');
          }
        }});
      }catch(error){closeProductDialog();throw error;}
      const form=body.querySelector('.wb-product-form');
      const actions=form.querySelector('.wb-product-dialog-actions');
      const scroll=E('div',undefined,'wb-product-dialog-scroll');
      if(copyFrom)scroll.append(E('p','已复制「'+copyFrom.body.name+'」的商品资料。请核对名称和信息后保存为新商品。','wb-muted'));
      scroll.append(...[...body.children].filter(child=>child!==form),...[...form.children].filter(child=>child!==actions));
      form.prepend(scroll);
      if(record){
        const updatePane=E('div',undefined,'wb-product-dialog-scroll wb-product-update-pane');
        updatePane.hidden=true;
        body.append(updatePane);
        const update=button('上传资料更新此商品',()=>{
          if(!updatePane.childElementCount)renderProductMaterials(updatePane,record);
          form.hidden=true;
          updatePane.hidden=false;
          heading.textContent='上传资料更新此商品';
          update.hidden=true;
          back.hidden=false;
          updatePane.querySelector('input[type="file"]')?.focus({preventScroll:true});
        },false,{openProductUpdate:''});
        const showEditor=()=>{
          updatePane.hidden=true;
          form.hidden=false;
          heading.textContent=title;
          back.hidden=true;
          update.hidden=false;
        };
        const back=button('返回编辑',()=>{showEditor();update.focus({preventScroll:true});});
        back.hidden=true;
        headActions.prepend(update,back);
      }
      applyEditorLocks();
      if(!preserveFocus){
        const name=form.querySelector('[data-field="name"]');
        if(name&&!name.disabled)name.focus({preventScroll:true});
      }
      return body;
    }
    function bufferRegion(key,baseRef){const[prefix,id,...parts]=key.split(':');if(prefix==='product')return{kind:id==='new'?'product_create':'product',id:id==='new'?'catalog':id};if(prefix==='room-defaults')return{kind:'room',id};if(prefix==='module'||prefix==='sentence'){const script=details().scripts?.find(s=>s.ref.id===baseRef?.id);return{kind:'scripts',id:prepId(),script_id:baseRef?.id,product_id:script?.body.product_id,module_key:prefix==='sentence'?parts[0]:parts.join(':')};}return{kind:({'session':'session','insight':'insights','assets':'assets','playlist':'playlist','product-selection':'product_selection'})[prefix]||prefix,id:prefix==='insight'?prepId()||'catalog':id};}
    const activeStatus=()=>productDialog?.status||(options.roomOverlay?.open&&options.roomOverlay.querySelector('[data-room-overlay-status]'))||status;
    function notice(message,tone='info',details){const output=activeStatus();output.replaceChildren(E('span',message));output.hidden=!message;output.dataset.tone=tone;if(details)output.append(jsonDetails('查看详情',details));}
    function button(text,fn,primary=false,attrs={}){const b=E('button',text,'wb-button'+(primary?' wb-button--primary':''));b.type='button';for(const[k,v]of Object.entries(attrs))b.dataset[k]=v;b.addEventListener('click',()=>guard(()=>fn(b)));return b;}
    async function guard(fn){if(busy)return;busy=true;container.setAttribute('aria-busy','true');try{return await fn();}catch(error){notice(error.message||'操作失败，输入已保留。','danger',error.details);if((error.unknown||error.code==='OUTCOME_UNKNOWN')&&unknownOperation){notice('提交结果暂不确定。请先核实原操作，勿重复提交。','warning',{operation_id:error.operationId});activeStatus().append(button('核实原操作结果',verifyUnknownOperation));}throwSafe(error);}finally{busy=false;container.removeAttribute('aria-busy');}}
    async function verifyUnknownOperation(){if(!unknownOperation)return null;const result=await api.query('read_operation_receipt',{operation_id:unknownOperation.operationId});if(result.state!=='committed'){notice('原操作仍未查到确定回执。可能仍在处理中，输入与操作号已保留；暂不重发。','warning',{operation_id:unknownOperation.operationId});activeStatus().append(button('核实原操作结果',verifyUnknownOperation));return result;}operations.set(unknownOperation.signature||'fingerprint:'+unknownOperation.fingerprint,{operationId:unknownOperation.operationId,receipt:result.receipt});unknownOperation=null;storage?.removeItem(prefix+'pending-operation');notice('已核实原操作已提交。再次执行相同保存会读取该回执，不重复写入。');return result;}
    async function command(name,args){const signature=name+':'+JSON.stringify(args);const fingerprint=await api.hash({command:name,args});if(unknownOperation){await verifyUnknownOperation();if(unknownOperation)throw Object.assign(Error('请等待原操作结果确定后再提交修改。'),{code:'OUTCOME_UNKNOWN',unknown:true,operationId:unknownOperation.operationId});}let op=operations.get(signature)||operations.get('fingerprint:'+fingerprint);if(op?.receipt){operations.delete(signature);operations.delete('fingerprint:'+fingerprint);return copy(op.receipt);}if(!op){op={operationId:uid()};operations.set(signature,op);}try{const receipt=await api.command(name,args,{operationId:op.operationId});operations.delete(signature);return receipt;}catch(error){if(error.unknown||error.code==='OUTCOME_UNKNOWN'){unknownOperation={operationId:error.operationId||op.operationId,signature,fingerprint,command:name};try{storage?.setItem(prefix+'pending-operation',JSON.stringify({operationId:unknownOperation.operationId,fingerprint,command:name}));}catch{}error.operationId=unknownOperation.operationId;}else operations.delete(signature);throw error;}}
    function throwSafe(error){options.onError?.(error);}
    async function humanMutation(path,body){
      const signature=path+':'+JSON.stringify(body),fingerprint=await api.hash({path,body});
      if(unknownOperation){await verifyUnknownOperation();if(unknownOperation)throw Object.assign(Error('请先核实上次提交结果。'),{code:'OUTCOME_UNKNOWN',unknown:true,operationId:unknownOperation.operationId});}
      let op=operations.get(signature)||operations.get('fingerprint:'+fingerprint);
      if(op?.receipt){operations.delete(signature);operations.delete('fingerprint:'+fingerprint);return copy(op.receipt.data||op.receipt);}
      if(!op){op={operationId:uid()};operations.set(signature,op);}
      try{const result=await api.request(path,{...body,operation_id:op.operationId});operations.delete(signature);return result;}
      catch(error){if(error.unknown||['NETWORK_UNAVAILABLE','OUTCOME_UNKNOWN'].includes(error.code)){unknownOperation={operationId:op.operationId,signature,fingerprint,command:path};try{storage?.setItem(prefix+'pending-operation',JSON.stringify({operationId:op.operationId,fingerprint,command:path}));}catch{}error.unknown=true;error.operationId=op.operationId;error.code='OUTCOME_UNKNOWN';}else operations.delete(signature);throw error;}
    }
    async function voicePreview(selection){
      const body={tts_selection:copy(selection)},signature=await api.hash(body),key=prefix+'voice-preview:'+signature;
      let attempt=operations.get(key);if(!attempt){try{attempt=JSON.parse(storage?.getItem(key)||'null');}catch{}attempt||={operationId:uid(),state:'new'};operations.set(key,attempt);}
      const remember=()=>{try{storage?.setItem(key,JSON.stringify(attempt));}catch{}};remember();
      if(attempt.state==='unknown'){
        const receipt=await api.query('read_operation_receipt',{operation_id:attempt.operationId});
        if(receipt.state==='committed'){attempt.state='succeeded';remember();return receipt.receipt?.data||receipt.receipt;}
        throw Object.assign(Error('上次试听结果仍需核实；未再次请求语音生成。原操作号：'+attempt.operationId),{code:'PREVIEW_OUTCOME_UNKNOWN',details:{operation_id:attempt.operationId}});
      }
      try{const result=await api.request('/api/prelive/v2/human/voice-preview',{...body,operation_id:attempt.operationId});attempt.state='succeeded';remember();return result;}
      catch(error){if(['NETWORK_UNAVAILABLE','OUTCOME_UNKNOWN','PREVIEW_IN_PROGRESS','PREVIEW_OUTCOME_UNKNOWN','PREVIEW_AUDIO_UNAVAILABLE'].includes(error.code)||error.unknown){attempt.operationId=error.details?.original_operation_id||error.details?.operation_id||attempt.operationId;attempt.state='unknown';remember();error.message+='；原试听编号已保留，再次点击只核实原操作，不重新生成。';}throw error;}
    }
    const prep=()=>snapshot?.preparation;
    const details=()=>snapshot?.preparation_details||{};
    const prepId=()=>current.preparation_id||null;
    const scope=()=>prepId()?'preparation':'catalog';
    function header(title,subtitle,actions=[]){const box=E('div',undefined,'wb-card-header');const h=E('div');h.append(E('h2',title,'wb-card-title'));if(subtitle)h.append(E('p',subtitle,'wb-muted'));box.append(h,...actions);return box;}
    function section(title){const n=E('section',undefined,'wb-card');n.append(E('h3',title,'wb-section-heading'));return n;}
    function empty(title,message,actions=[]){const box=E('div',undefined,'wb-empty');box.append(E('h3',title),E('p',message),...actions);return box;}
    function renderProductMaterials(node,record=null){
      const updating=!!record;
      const flowKey=updating?'update:'+record.ref.id:'create';
      const flow=productMaterialFlows.get(flowKey)||{batch:null,product:null,pending:false,unknown:false};
      productMaterialFlows.set(flowKey,flow);
      const title=updating?'上传资料更新此商品':'上传资料新增商品';
      const box=section(title);
      box.classList.add('wb-product-material-flow');
      box.dataset.productMaterialFlow=updating?'update':'create';
      box.dataset.bufferIgnore='true';
      box.append(E('p',updating
        ?'目标商品：'+record.body.name+'。选好资料后可直接交给 Agent 更新这件商品。'
        :'提交新商品的文档或截图后，Agent 会整理新增商品，并为这批商品新建直播间和本场。','wb-muted'));
      box.append(E('p','需要补充特别要求时，可先上传并加入对话，再在右侧填写并发送。','wb-muted'));
      const upload=E('input');
      upload.type='file';
      upload.multiple=true;
      upload.accept='.txt,.md,.csv,.json,.pdf,.doc,.docx,.rtf,.xlsx,.png,.jpg,.jpeg,.webp';
      upload.setAttribute('aria-label',title);
      const feedback=E('p',undefined,'wb-muted');
      feedback.setAttribute('aria-live','polite');
      feedback.setAttribute('aria-atomic','true');
      feedback.hidden=true;
      const filesAdded=E('ul',undefined,'wb-product-material-files');
      filesAdded.hidden=true;
      function message(text){
        flow.message=text;
        updateFlow();
      }
      function targetChanged(){
        return updating&&flow.product&&(flow.product.ref.revision!==record.ref.revision||
          flow.product.ref.content_hash!==record.ref.content_hash);
      }
      function currentProduct(){return updating?{ref:copy(record.ref),name:record.body.name}:null;}
      async function uploadBatch(files,compose){
        if(!options.onUploadProductMaterials)throw Error('商品资料入口暂未连接，请刷新后重试。');
        const product=flow.batch?flow.product:currentProduct();
        message('正在上传资料…');
        const batch=await options.onUploadProductMaterials({files,product,batch:flow.batch,compose});
        flow.batch=batch;
        flow.product=product;
        if(batch.failed?.length){
          throw Error('部分资料尚未上传成功：'+batch.failed.map(file=>file.name).join('、')+'。已保留成功的资料，可重试。');
        }
        if(!batch.materialRefs?.length)throw Error('尚未有资料上传成功，请检查文件后重试。');
        upload.value='';
        return batch;
      }
      function checkTarget(){
        if(targetChanged())throw Error('该商品版本已更新。请清空这批资料后，按当前版本重新上传。');
        if(updating&&buffers.get('product:'+record.ref.id)?.dirty)throw Error('请先保存或放弃这件商品的未保存修改，再交给 Agent。');
      }
      const add=button('上传并加入对话',async()=>{
        const files=Array.from(upload.files||[]);
        if((!files.length&&!flow.batch?.materialRefs?.length)||flow.pending||flow.unknown)return;
        flow.pending=true;
        paint();
        try{
          checkTarget();
          const batch=await uploadBatch(files,true);
          if(!batch.composeAttached)throw Error('资料已上传，请重试打开右侧对话。');
          flow.batch=null;
          flow.product=null;
          message('已在右侧对话加入 '+batch.materialRefs.length+' 份资料。可补充要求后点击发送。');
        }catch(error){
          message(error.message||'资料上传未完成。请检查文件后重试。');
        }finally{flow.pending=false;updateFlow();}
      });
      const submit=button(updating?'交给 Agent 更新此商品':'交给 Agent 新增商品',async()=>{
        const files=Array.from(upload.files||[]);
        if((!files.length&&!flow.batch?.materialRefs?.length)||flow.pending)return;
        flow.pending=true;
        paint();
        try{
          if(!options.onSubmitProductMaterials)throw Error('商品资料任务入口暂未连接，请刷新后重试。');
          if(!flow.unknown)checkTarget();
          if(!flow.unknown&&files.length)await uploadBatch(files,false);
          if(!flow.unknown&&flow.batch?.failed?.length)throw Error('请先重试未上传成功的资料。');
          message('正在提交给 Agent…');
          await options.onSubmitProductMaterials({batch:flow.batch,product:flow.product});
          flow.batch=null;
          flow.product=null;
          flow.unknown=false;
          upload.value='';
          message('Agent 已受理。可在右侧查看任务进度和保存成果。');
        }catch(error){
          flow.unknown=flow.unknown||!!error.unknown||error.code==='OUTCOME_UNKNOWN';
          message(flow.unknown?'这批任务的受理结果待核实。再次点击同一按钮将核实这批请求。':error.message||'任务未受理，已保留本批资料。');
        }finally{flow.pending=false;updateFlow();}
      },true);
      const reset=button('清空这批资料',()=>{
        flow.batch=null;
        flow.product=null;
        upload.value='';
        message('已清空待提交的这批资料。');
      });
      const actions=E('div',undefined,'wb-product-material-actions');
      actions.append(submit,add,reset);
      function paint(){
        const stale=targetChanged();
        const hasFiles=!!upload.files?.length;
        const hasBatch=!!flow.batch?.materialRefs?.length&&!flow.batch?.failed?.length;
        upload.disabled=flow.pending||flow.unknown||stale;
        add.disabled=upload.disabled||(!hasFiles&&!hasBatch);
        submit.disabled=flow.pending||(!hasFiles&&!hasBatch)||(stale&&!flow.unknown);
        reset.hidden=(!flow.batch&&!hasFiles)||flow.unknown;
        reset.disabled=flow.pending;
        const text=stale&&!flow.unknown&&!flow.pending?'该商品版本已更新。请清空这批资料后重新上传。':flow.message||'';
        feedback.textContent=text;
        feedback.hidden=!text;
        const names=(flow.batch?.materials||[]).map(material=>material.name).filter(Boolean);
        filesAdded.hidden=!names.length;
        filesAdded.replaceChildren(...names.map(name=>E('li',name)));
      }
      function updateFlow(){paint();if(flow.paint!==paint)flow.paint?.();}
      node._refreshProductMaterials=()=>{flow.paint=paint;paint();};
      upload.addEventListener('change',paint);
      box.append(upload,actions,feedback,filesAdded);
      node._refreshProductMaterials();
      node.append(box);
    }
    async function importFile(file){if(!file||!file.size)throw Error('请选择非空资料文件');if(file.size>10*1024*1024)throw Error('单份资料不能超过10 MB');const bytes=new Uint8Array(await file.arrayBuffer());const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));const result=await command('import_blob',{content_base64:btoa(binary),expected_sha256:'sha256:'+hash,mime:file.type||'text/plain',filename:file.name});return result.data.blob_ref;}
    function field(label,control){const box=E('div',undefined,'wb-field');const id='wb-field-'+uid();const l=E('label',label);l.htmlFor=id;control.id=id;box.append(l,control);return box;}
    function control(type,value,label){const c=E(type==='textarea'?'textarea':'input');if(type!=='textarea')c.type=type;c.value=value??'';c.setAttribute('aria-label',label);if(type==='textarea')c.rows=3;return c;}
    function select(value,entries,label){const c=E('select');c.setAttribute('aria-label',label);for(const [v,t]of entries)c.append(new Option(t,v));c.value=value??'';return c;}
    function suggestionEditor(value,label){
      const labels={context:'适用背景',market:'目标市场',language:'语言',target_audience:'目标顾客',customer:'目标顾客',audience:'顾客',use_scene:'使用场景',scene:'场景',concern:'顾虑',needs:'顾客需求',selling_points:'顾客能理解的卖点',recommended_focus:'讲解重点',priority:'优先级',reason:'依据说明',summary:'观点说明'};
      if(Array.isArray(value)){const box=E('div',undefined,'wb-list');if(value.every(v=>typeof v==='string')){const c=control('textarea',value.join('\n'),label);box.append(field(label+'（每行一条）',c));return{node:box,read:()=>c.value.split('\n').filter(Boolean),write:v=>c.value=(v||[]).join('\n')};}const children=value.map((v,i)=>suggestionEditor(v,label+' '+(i+1)));children.forEach(c=>box.append(c.node));return{node:box,read:()=>children.map(c=>c.read()),write:v=>children.forEach((c,i)=>c.write(v?.[i]))};}
      if(value&&typeof value==='object'){const box=section(label),children={};for(const[k,v]of Object.entries(value)){children[k]=suggestionEditor(v,labels[k]||k);box.append(children[k].node);}return{node:box,read:()=>Object.fromEntries(Object.entries(children).map(([k,c])=>[k,c.read()])),write:v=>Object.entries(children).forEach(([k,c])=>c.write(v?.[k]))};}
      const c=typeof value==='boolean'?select(String(value),[['true','是'],['false','否']],label):control(typeof value==='number'?'number':'textarea',value,label);return{node:field(label,c),read:()=>typeof value==='number'?Number(c.value):typeof value==='boolean'?c.value==='true':c.value,write:v=>c.value=v??''};
    }
    function localRead(key){try{const val=JSON.parse(storage?.getItem(prefix+key)||'null');return val?.value!==undefined?val:null;}catch{return null;}}
    function persist(buffer){try{if(buffer.dirty)storage?.setItem(prefix+buffer.key,JSON.stringify({base_ref:buffer.base_ref,value:buffer.read(),label:buffer.label,saved_at:new Date().toISOString()},(key,value)=>key==='image'?undefined:value));else storage?.removeItem(prefix+buffer.key);}catch{notice('本机恢复缓存不可用；当前输入仍保留在页面中。','warning');}}
    function changed(buffer){
      const baseline=buffer.cleanSnapshot;
      const dirty=!baseline||!equal(buffer.base_ref,baseline.ref)||!equal(buffer.read(),baseline.value);
      if(!dirty&&!buffer.dirty)return;
      buffer.dirty=dirty;
      buffer.host?.classList.toggle('wb-dirty',dirty);
      persist(buffer);
      options.onDirty?.(getDirtySummary());
    }
    function registerBuffer(key,label,baseRef,read,write,save,host,cleanSnapshot){
      const old=buffers.get(key);if(old?.dirty){write(old.read());}
      const buffer={key,label,base_ref:copy(old?.dirty?old.base_ref:baseRef),read,write,cleanSnapshot,save:()=>{if(matchingLocks(editorLocks,bufferRegion(key,baseRef)).length)throw Error('Agent 正在处理此区域，请等待任务完成后再保存。你的输入已保留。');return save();},host,dirty:old?.dirty||false};buffers.set(key,buffer);region(host,bufferRegion(key,baseRef));host.classList.toggle('wb-dirty',buffer.dirty);
      const stored=!old?.dirty?localRead(key):null;
      if(stored){const restore=E('div',undefined,'wb-inline-alert');restore.append(E('span','发现本机未保存内容'+(!equal(stored.base_ref,baseRef)?'；服务端基础版本已变化，请对比后再保存。':'。')),
        button('恢复本机未保存内容',()=>{write(stored.value);buffer.base_ref=stored.base_ref;changed(buffer);restore.remove();if(!equal(stored.base_ref,baseRef))notice('已恢复本机输入。服务端已有新版本，保存会进行版本检查；请先查看差异。','warning',{buffer_base:stored.base_ref,current:baseRef});}),
        button('放弃本机恢复内容',()=>{storage?.removeItem(prefix+key);restore.remove();}));host.prepend(restore);}
      const track=e=>{if(buffers.get(key)===buffer&&!e.target.closest?.('[data-buffer-ignore]'))changed(buffer);};host.addEventListener('input',track);host.addEventListener('change',track);return buffer;
    }
    function clearBuffer(buffer){buffer.dirty=false;buffer.host?.classList.remove('wb-dirty');storage?.removeItem(prefix+buffer.key);buffers.delete(buffer.key);options.onDirty?.(getDirtySummary());}
    function matches(buffer,selection){if(!selection)return true;if(buffer.key.startsWith('product:new:copy:'))return false;return selectionItems(selection).some(item=>{const target=item.target?.object_ref;if(!target)return false;return buffer.base_ref?.id===target.id||buffer.key.includes(target.id);});}
    function hasDirty(selection){return [...buffers.values()].some(b=>b.dirty&&matches(b,selection));}
    function getDirtySummary(){return [...buffers.values()].filter(b=>b.dirty).map(b=>({key:b.key,label:b.label,base_ref:copy(b.base_ref)}));}
    async function saveDirty(selection){for(const b of [...buffers.values()])if(b.dirty&&matches(b,selection))await b.save();return !hasDirty(selection);}
    function discardDirty(selection){for(const b of [...buffers.values()])if(b.dirty&&matches(b,selection)){b.dirty=false;storage?.removeItem(prefix+b.key);buffers.delete(b.key);cache.clear();}options.onDirty?.(getDirtySummary());return refresh({force:true});}
    function selected(label,target,destination,buffer){
      const reader=scriptReader?.matches('dialog[open]')&&scriptReader.contains(document.activeElement);
      const overlay=options.roomOverlay?.open&&!productDialog&&options.roomOverlay.contains(document.activeElement);
      if(reader&&!closeScriptReader({restoreFocus:false}))return;
      options.onSelect?.({label,scope:scope(),...(prepId()?{preparation_id:prepId()}:{}),
        target:copy(target),destination:copy(destination),dirty:!!buffer?.dirty},{focusChat:reader||overlay});
    }
    function changedBusiness(receipt){options.onChange?.({receipt,context:getContext()});}
    async function readSnapshot(){const requested=prepId();const value=await api.query('get_workspace_snapshot',requested?{preparation_id:requested,limit:200}:{limit:200});if(prepId()!==requested)return value;snapshot=value;if(prep())current.room_id=prep().body.room_id;return value;}
    async function ensureSchema(){if(!schema)schema=await api.query('get_business_schema',{});return schema;}
    let lastLocation='';
    function markLocation(node,target,focusTarget){
      for(let ancestor=target.parentElement;ancestor&&ancestor!==node;ancestor=ancestor.parentElement)if(ancestor.tagName==='DETAILS')ancestor.open=true;
      node.querySelectorAll('.wb-located').forEach(item=>item.classList.remove('wb-located'));
      target.classList.add('wb-located');
      if(focusTarget){
        if(!focusTarget.matches('button,input,textarea,select,a[href],[tabindex]'))focusTarget.tabIndex=-1;
        focusTarget.focus({preventScroll:true});
      }
      (focusTarget||target).scrollIntoView({block:'nearest'});
    }
    function locateProductResult(node,{object_ref:ref,field_path,knowledge_document_id:documentId,preserve_focus:preserveFocus}){
      const rendered=node._productRef;
      const heading=node._productHeading||node.querySelector('.wb-card-title');
      const fieldPath=documentId?'/knowledge_documents':field_path;
      const visible=element=>element&&!element.closest('[hidden],[inert]');
      const field=[...node.querySelectorAll('[data-product-field]')].find(item=>item.dataset.productField===fieldPath);
      const section=fieldPath==='/knowledge_documents'?node.querySelector('.wb-product-knowledge'):heading;
      const matchingDocuments=documentId?[...node.querySelectorAll('[data-product-document-id]')].filter(item=>item.dataset.productDocumentId===documentId):[];
      const target=documentId?(matchingDocuments.length===1?matchingDocuments[0]:null):fieldPath?field:heading;
      const revisionChanged=ref.revision!==rendered.revision||ref.content_hash!==rendered.content_hash;
      if(!visible(target)){
        const fallback=visible(field)?field:section||heading;
        markLocation(node,fallback,!preserveFocus&&(fallback.querySelector('h3')||fallback));
        notice(documentId?'当前商品中找不到这份知识文档，已打开商品知识库。请核对最新资料。':'当前商品中无法定位此字段，已打开商品资料。','warning');
        return;
      }
      const selector=documentId?'[data-field="content"]':fieldPath==='/core_selling_points'
        ?'.wb-core-points textarea,[data-add-core-point]':'[data-field]';
      const control=target.querySelector(selector);
      markLocation(node,target,!preserveFocus&&(visible(control)&&!control.disabled?control:target));
      if(revisionChanged)notice('已打开商品的当前版本。该版本与原成果记录的版本不同。','warning');
    }
    const preparationResults={
      analysis:{view:'insights',label:'顾客与场景建议',buffer:'insight'},
      draft:{view:'session',label:'本场配置',buffer:'session'},
      playlist:{view:'scripts',label:'播放顺序',buffer:'playlist'},
      audio_manifest:{view:'scripts',label:'本场语音'},
      publication:{view:'scripts',label:'播出内容'},
      asset_checklist:{view:'overview',label:'展示材料清单',buffer:'assets'}
    };
    function markResultSection(node,kind,ref){
      node.dataset.resultSection=kind;
      node._resultRef=ref;
      return node;
    }
    function preparationResult(view,ref){
      const descriptor=Object.hasOwn(preparationResults,ref?.kind)?preparationResults[ref.kind]:null;
      return descriptor?.view===view?descriptor:null;
    }
    function locatePreparationResult(node,{object_ref:ref,preparation_id:preparationId,preserve_focus:preserveFocus,field_path:fieldPath},descriptor){
      const samePreparation=preparationId&&prep()?.ref.id===preparationId;
      const sections=samePreparation?[...node.querySelectorAll('[data-result-section]')].filter(item=>item.dataset.resultSection===ref.kind):[];
      const target=sections.find(item=>{
        const saved=item._resultRef;
        return saved?.id===ref.id&&saved.revision===ref.revision&&saved.content_hash===ref.content_hash;
      });
      const section=target||(ref.kind==='analysis'?null:sections[0]);
      const fallback=section||(samePreparation&&ref.kind==='publication'?node.querySelector('[data-audio-review]'):null);
      const heading=fallback?.querySelector(':scope > h3')||node.querySelector('.wb-card-title');
      if(!(ref.kind==='draft' && /^\/draft\/commerce\/product_overrides\//.test(fieldPath || '')))
        markLocation(node,fallback||heading,!preserveFocus&&heading);
      if(!target)notice(section?._resultRef?'已打开当前'+descriptor.label+'。该版本与原成果记录的版本不同。':'本场中找不到这份已保存的'+descriptor.label+'。请核对当前本场。','warning');
    }
    function locate(node,destination,effectiveView=current.view){
      if(effectiveView==='products'&&destination.object_ref?.kind==='product'&&node._productRef){locateProductResult(node,destination);return;}
      const descriptor=preparationResult(effectiveView,destination.object_ref);
      if(descriptor){locatePreparationResult(node,destination,descriptor);return;}
      if(effectiveView==='scripts'&&destination.action==='script_generation'){node._showScriptGeneration?.(!destination.preserve_focus);return;}
      if(effectiveView==='scripts'&&destination.action==='review'){
        const target=node.querySelector('[data-result-section="publication"]')||node.querySelector('[data-audio-review]');
        if(target)markLocation(node,target,!destination.preserve_focus&&target.querySelector('h3'));
        return;
      }
      const id=destination.script_id||destination.resource_id;
      const key=[id||'',destination.module_key||'',destination.field_path||''].join(':');
      if(key==='::'||key===lastLocation&&scriptReader)return;
      let target=id?[...node.querySelectorAll('[data-script-id]')].find(n=>n.dataset.scriptId===id):null;
      if(target?._openScriptReader&&effectiveView==='scripts'){
        if(options.roomCanvas&&current.view==='room-config'&&destination.preserve_focus)markLocation(node,target);
        else target._openScriptReader(destination.module_key,destination.preserve_focus);
        lastLocation=key;return;
      }
      if(destination.module_key)target=(target||node).querySelector('[data-module="'+destination.module_key.replace(/[^a-z_]/g,'')+'"]')||target;
      if(destination.field_path){const name=destination.field_path.split('/').pop();target=[...node.querySelectorAll('[data-field]')].find(n=>n.dataset.field===name)||target;}
      if(target){markLocation(node,target);lastLocation=key;}
    }
    async function open(destination={}){
      const resultRef=destination.object_ref;
      const next={...current,...destination};
      delete next.object_ref;
      delete next.knowledge_document_id;
      delete next.preserve_focus;
      if(['script_generation','review','new_product'].includes(next.action))next.action=null;
      next.view=({'product-detail':'products','product-list':'products',insights:'insights','session-config':'session',playlist:'scripts',audio:'scripts',adopted:'scripts',task:'overview'}[next.view]||next.view);
      const productId=next.view==='products'?next.product_id:null;
      const productResult=next.view==='products'&&resultRef?.kind==='product';
      const preparationDescriptor=preparationResult(destination.inline_section||next.view,resultRef);
      if(productResult&&resultRef.id!==productId)throw Error('成果的商品与打开目标不一致，请从商品知识库核对。');
      if(productResult&&buffers.get('product:'+productId)?.dirty)throw Error('此商品有未保存编辑。请保存或放弃后再查看成果。');
      if(productId&&productDialog?.productId===productId&&next.preparation_id===current.preparation_id&&next.room_id===current.room_id&&buffers.get('product:'+productId)?.dirty){
        notice('当前商品有未保存编辑，已保留输入。请保存或放弃后刷新。','warning');
        return getContext();
      }
      if(preparationDescriptor?.buffer){
        const bufferId=resultRef.kind==='analysis'?resultRef.id:next.preparation_id;
        if(buffers.get(preparationDescriptor.buffer+':'+bufferId)?.dirty)throw Error(preparationDescriptor.label+'有未保存编辑。请保存或放弃后再查看成果。');
      }
      if(!closeProductDialog({restoreFocus:false}))throw Error('请先处理商品弹窗中的未保存修改。');
      if(!closeScriptReader({restoreFocus:false}))throw Error('请先保存或取消正在编辑的句子，再切换工作区。');
      const generation=++openGeneration;
      let productRecord;
      if(productId){
        notice('正在读取商品资料…');
        try{productRecord=await api.query('read_product',{product_id:productId});}
        catch(error){
          if(openGeneration!==generation)return;
          if(error.code!=='NOT_FOUND'){notice(error.message,'danger',error.details);throw error;}
        }
        if(openGeneration!==generation)return;
        if(productResult&&buffers.get('product:'+productId)?.dirty){
          const message='此商品有未保存编辑。请保存或放弃后再查看成果。';
          notice(message,'warning');
          throw Error(message);
        }
      }
      current=next;
      catalogResizeObserver?.disconnect();
      const key=[current.view,current.preparation_id||'',current.room_id||'',current.action||'',current.view==='room-config'?current.room_section||'':''].join(':');
      // A room has one editable defaults buffer across its preparations.
      if(current.view==='room-config'&&current.room_id&&!current.room_section)cache.delete(key);
      notice('');
      let protectedEdits=false;
      if((destination.refresh||preparationDescriptor)&&cache.has(key)){
        const cached=cache.get(key);
        protectedEdits=[...buffers.values()].some(b=>b.dirty&&cached.contains(b.host));
        if(!protectedEdits)cache.delete(key);
      }
      async function present(node){
        node._activateCatalog?.();
        if(productId){
          if(!productRecord||productRecord.ref.id!==productId||productRecord.body.archived){
            current.product_id=null;
            const heading=node.querySelector('.wb-card-title');
            markLocation(node,heading,!destination.preserve_focus&&heading);
            notice(productResult?'成果关联的商品已归档或无法找到，已打开商品知识库。':'该商品已归档或无法找到，已打开商品知识库。','warning');
          }else{
            const row=[...node.querySelectorAll('[data-catalog-product-id]')].find(item=>item.dataset.catalogProductId===productId)||
              (productResult?node._showCatalogProduct?.(productId,{clearSearch:false}):null);
            const body=await openProductDialog(row?.querySelector('[data-open-product]'),{record:productRecord,preserveFocus:destination.preserve_focus});
            if(body)locate(body,destination);
          }
        }else{
          const inline=destination.inline_section&&node.querySelector('[data-inline-room-section="'+destination.inline_section+'"]');
          if(inline)locate(inline,destination,destination.inline_section);
          else locate(node,destination);
          const add=node.querySelector('[data-new-product]');
          if(destination.action==='new_product'&&add)await openProductDialog(add);
        }
        if(protectedEdits&&!productDialog)notice([status.textContent,'当前页面有未保存编辑，已保留输入。请保存或放弃后刷新对比。'].filter(Boolean).join(' '),'warning');
        return getContext();
      }
      try{
        if(cache.has(key)){
          const node=cache.get(key);
          snapshot=copy(node._snapshot);
          if(content.firstElementChild!==node)content.replaceChildren(node);
          return await present(node);
        }
        notice('正在读取资料…');
        await readSnapshot();
        await ensureSchema();
        if(openGeneration!==generation)return;
        const node=E('div',undefined,'wb-view');
        node.dataset.view=current.view;
        content.replaceChildren(node);
        await render(node,destination);
        if(openGeneration!==generation)return;
        node._snapshot=copy(snapshot);
        cache.set(key,node);
        notice('');
        return await present(node);
      }catch(e){if(openGeneration!==generation)return;notice(e.message,'danger',e.details);throw e;}
    }
    async function navigate(dest){return options.onNavigate?options.onNavigate(dest):open(dest);}
    async function refresh({force=false}={}){
      if(hasDirty()&&!force){notice('资料有新变化。未保存输入已保留；保存或放弃后可刷新。','warning');return {dirty:true};}
      cache.clear();
      const overlay=options.roomOverlay?.open?activeRoomOverlay:null;
      const result=await open({...current});
      if(overlay&&options.roomOverlay?.open&&overlay===activeRoomOverlay)
        await renderRoomOverlay(overlay.host,{...overlay.destination,refresh:false,object_ref:null});
      return result;
    }
    function getContext(){const room=(snapshot?.rooms||[]).find(r=>r.ref.id===(prep()?.body.room_id||current.room_id));return {...copy(current),room_name:room?.body.name||'',prep_label:prep()?preparationTitle(prep(),details().draft):'',product_label:current.product_id?productDialog?.body._productLabel||'':'',...(prep()?{preparation_ref:copy(prep().ref),preparation_id:prep().ref.id,room_id:prep().body.room_id}:{}),scope:prepId()?{kind:'preparation',account_id:'local',preparation_id:prepId()}:{kind:'catalog',account_id:'local'}};}
    async function resolveSelection(selection){if(Array.isArray(selection?.items)){const items=[];for(const item of selection.items)items.push(await resolveSelection(item));return{label:'已选 '+items.length+' 项',items,...(items.some(item=>item.stale)?{stale:true}:{})};}const result=copy(selection);let ref=result?.target?.object_ref;if(!ref)return result;let known=replacements.get(ref.id);const seen=new Set();while(known&&!seen.has(known.id)){seen.add(known.id);result.target.object_ref=known;known=replacements.get(known.id);}if(seen.size){ref=result.target.object_ref;if(result.target.module_key){const s=await api.query('read_script',{ref});result.target.selected_text_hash=await api.hash(s.modules[result.target.module_key].sentences);}}
      if(ref.kind==='product'){const head=await api.query('read_product',{product_id:ref.id});if(!equal(head.ref,ref))return {...result,stale:true,current_ref:head.ref};}else if(ref.kind==='preparation'&&result.preparation_id){const head=await api.query('read_preparation',{preparation_id:result.preparation_id});if(!equal(head.ref,ref))return {...result,stale:true,current_ref:head.ref};}else if(ref.kind==='analysis'&&result.preparation_id){const record=await api.query('read_analysis_report',{ref});if(record.body.preparation_id!==result.preparation_id)return {...result,stale:true,current_ref:null,reason:'该研究尚未保存为当前本场建议'};}else if(ref.kind==='script_revision'&&result.preparation_id){const head=await api.query('read_preparation',{preparation_id:result.preparation_id});if(!head.body.script_refs.some(r=>equal(r,ref)))return {...result,stale:true,current_ref:null};}return result;}
    async function render(node,destination){if(current.view==='products')return renderProducts(node);if(current.view==='rooms')return renderRooms(node);if(current.view==='room-config')return renderRoomConfig(node);if(!prep())return renderOverview(node);if(current.view==='session')return renderSession(node);if(current.view==='insights')return renderInsights(node);if(current.view==='scripts')return renderScripts(node);if(current.view==='playlist')return renderPlaylist(node);if(current.view==='changes')return renderChanges(node);return renderOverview(node);}
    function renderOverview(node){node.append(header('本场播前准备','把需求交给右侧 Agent；左侧用于核对、补充和微调。'));
      if(!prep()){if(current.room_id){const room=(snapshot.rooms||[]).find(r=>r.ref.id===current.room_id);node.append(empty('已选择：'+(room?.body.name||'直播间'),'创建本场准备，或继续此直播间的已有本场。',[button('新建本场准备',()=>createPreparation(current.room_id),true),button('查看已有本场',()=>navigate({...roomWorkspace(current.room_id),room_section:'preparations'}))]));}else node.append(empty('先选择直播间','创建或选择一个直播间，然后进入本场准备。商品库在所有直播间之间共用。',[button('选择直播间',returnToRoomList,true),button('管理商品库',()=>navigate({view:'products',product_id:null}))]));return;}
      const p=prep().body,room=(snapshot.rooms||[]).find(r=>r.ref.id===p.room_id);const summary=section(preparationTitle(prep(),details().draft));summary.classList.add('wb-overview-summary');summary.append(E('p',preparationStateLabel(prep(),room),'wb-muted'));if(p.goal)summary.append(jsonDetails('查看本场原始任务',p.goal));const grid=E('div',undefined,'wb-grid');for(const [view,title,info]of [['session','本场配置',p.draft_ref?'已保存':'待配置'],['products','引用商品',p.selected_product_refs.length+' 个'],['insights','顾客与卖点',p.analysis_refs.length+' 份建议'],['scripts','话术、顺序与语音',p.script_refs.length+' 版 · '+(preparationAdopted(prep(),room)?'当前已采用':p.candidate_publication_ref?'已生成候选':'待准备')]]){const row=section(title);row.append(E('p',info,'wb-muted'),button('查看与微调',()=>navigate({view,product_id:null})));grid.append(row);}summary.append(grid);node.append(summary);if(details().asset_checklist)renderAssets(node,details().asset_checklist);}
    async function renderProducts(node){
      const pid=prepId(),initialPreparation=copy(prep());
      const stateKey=pid||'catalog';
      const pageState=catalogViewStates.get(stateKey)||{query:'',page:1,size:6};
      catalogViewStates.set(stateKey,pageState);
      node.classList.add('wb-catalog-view');
      const add=E('section',undefined,'wb-catalog-add');
      const addActions=E('div',undefined,'wb-catalog-add-actions');
      addActions.append(button('新建商品',openProductDialog,true,{newProduct:''}));
      if(!pid)addActions.append(button('上传资料新增商品',origin=>openProductDialog(origin,{upload:true}),false,{openProductUpload:''}));
      add.append(E('h2','添加商品'),addActions);
      const inventory=E('section',undefined,'wb-catalog-inventory');
      const inventoryHead=E('div',undefined,'wb-catalog-inventory-head');
      const heading=E('h2',pid?'选择本场引用商品':'商品列表','wb-card-title');
      const search=control('search',pageState.query,'搜索商品');
      search.placeholder='商品名称或 SKU';
      inventoryHead.append(heading,search);
      const window=E('div',undefined,'wb-catalog-list-window');
      const list=E('ul',undefined,'wb-catalog-list');
      const rowProbe=E('div',undefined,'wb-catalog-size-probe');
      rowProbe.setAttribute('aria-hidden','true');
      const emptyText=E('p',undefined,'wb-catalog-empty');
      window.append(rowProbe,list,emptyText);
      const footer=E('div',undefined,'wb-catalog-footer');
      const count=E('p',undefined,'wb-catalog-count');
      count.setAttribute('aria-live','polite');
      const pager=E('nav',undefined,'wb-catalog-pager');
      pager.setAttribute('aria-label','商品列表页码');
      footer.append(count,pager);
      const columns=E('div',undefined,'wb-catalog-columns');
      if(pid)columns.dataset.hasSelection='';
      columns.append(E('span','商品与资料'),E('span','促销 / 物流','wb-catalog-fulfillment-heading'),
        E('span','日常售价','wb-catalog-price-heading'),E('span','在售状态','wb-catalog-status-heading'),E('span','操作','wb-catalog-actions-heading'));
      inventory.append(inventoryHead,columns,window,footer);
      node.append(add,inventory);
      const products=copy(snapshot.products.items||[]);
      let cursor=snapshot.products.next_cursor;
      while(cursor!==null&&cursor!==undefined){
        const page=await api.query('search_products',{cursor,limit:200});
        products.push(...page.items);
        cursor=page.next_cursor;
      }

      const selectedRefs=initialPreparation?.body.selected_product_refs||[];
      const picked=new Map(selectedRefs.map(ref=>[ref.id,ref]));
      let selectionBuffer;
      const filtered=()=>{
        const query=pageState.query.trim().toLowerCase();
        return products.filter(product=>(product.name+' '+(product.sku_id||'')).toLowerCase().includes(query));
      };
      function thumbnail(product){
        const frame=E('span',undefined,'wb-catalog-thumbnail');
        const placeholder=E('span','无图');
        placeholder.setAttribute('aria-hidden','true');
        const source=product.thumbnail_url;
        const raster=typeof source==='string'&&source.length<=16464&&/^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}$/i.test(source);
        const local=typeof source==='string'&&/^\/(?:assets\/(?:product-images|product-thumbnails)|product-images|api\/prelive\/v2\/product-thumbnail)\/[a-zA-Z0-9_-]+\.(?:png|jpe?g|webp)$/i.test(source);
        if(raster||local){
          const image=E('img');
          image.alt='';
          image.loading='lazy';
          image.decoding='async';
          image.addEventListener('error',()=>frame.replaceChildren(placeholder),{once:true});
          image.src=source;
          frame.append(image);
        }else frame.append(placeholder);
        return frame;
      }
      function productRow(product){
        const row=E('li',undefined,'wb-catalog-row');
        row.dataset.catalogProductId=product.ref.id;
        row.classList.toggle('wb-located',pageState.highlightedProduct===product.ref.id);
        if(initialPreparation){
          const check=E('input');
          check.type='checkbox';
          check.checked=picked.has(product.ref.id);
          check.setAttribute('aria-label','本场引用 '+product.name);
          check.addEventListener('change',()=>{
            if(check.checked)picked.set(product.ref.id,selectedRefs.find(ref=>ref.id===product.ref.id)||product.ref);
            else picked.delete(product.ref.id);
            changed(selectionBuffer);
          });
          const holder=region(E('span',undefined,'wb-catalog-checkbox'),{kind:'product_selection',id:pid});
          holder.append(check);
          row.append(holder);
        }
        const text=E('div',undefined,'wb-catalog-product-text');
        const name=E('strong',product.name);
        name.title=product.name;
        const retained=picked.has(product.ref.id)&&!equal(picked.get(product.ref.id),product.ref);
        const platform=product.sales_platform!=='unspecified'&&PLATFORM_LABELS[product.sales_platform];
        const specs=Number.isInteger(product.specification_count)?product.specification_count+' 项规格':null;
        const knowledge=product.knowledge_document_count>0?product.knowledge_document_count+' 份知识文档':product.has_knowledge||product.has_faq?'有知识资料':null;
        const info=[retained?'本场引用旧修订':null,platform,specs,knowledge].filter(Boolean).join(' · ');
        const caption=info||[product.sku_id?'SKU '+product.sku_id:null,product.display_number?'编号 '+product.display_number:null].filter(Boolean).join(' · ');
        text.append(name);
        if(caption){
          const metadata=E('span',caption,'wb-catalog-product-info');
          metadata.title=caption;
          text.append(metadata);
        }
        const points=Array.isArray(product.selling_points_preview)?product.selling_points_preview.filter(point=>typeof point==='string'&&point.trim()).slice(0,3):[];
        if(!points.length&&typeof product.selling_point_preview==='string'&&product.selling_point_preview.trim())points.push(product.selling_point_preview);
        if(points.length){
          const highlights=E('ul',undefined,'wb-catalog-selling-points');
          highlights.setAttribute('aria-label','核心卖点');
          for(const point of points){
            const item=E('li',point,'wb-catalog-selling-point');
            item.title=point;
            highlights.append(item);
          }
          text.append(highlights);
        }
        const details=E('dl',undefined,'wb-catalog-fulfillment');
        for(const [label,value] of [['商品库促销',product.promotion_preview],['发货时效',product.shipping_time_preview]]){
          const description=E('dd',value||'未填写');
          if(value)description.title=value;
          details.append(E('dt',label),description);
        }
        const summary=E('div',undefined,'wb-catalog-summary');
        summary.append(text,details);
        const price=E('div',undefined,'wb-catalog-price');
        price.append(E('span','日常价','wb-catalog-cell-label'),E('strong',product.price==null?'未标价':product.price+(product.currency?' '+product.currency:'')));
        const available=product.sale_status!=='unavailable';
        const status=E('div',undefined,'wb-catalog-status');
        const badge=E('span',available?'在售':'停售','wb-catalog-status-value');
        badge.dataset.available=String(available);
        status.append(E('span','状态','wb-catalog-cell-label'),badge);
        const actions=E('div',undefined,'wb-catalog-row-actions');
        actions.append(button('编辑',()=>navigate({view:'products',product_id:product.ref.id,action:null}),false,{openProduct:''}));
        if(!pid){
          actions.append(button('复制',async origin=>{
            const source=await api.query('read_product',{product_id:product.ref.id});
            if(!source||source.body.archived)throw Error('原商品已移除，请刷新商品列表。');
            if(origin.isConnected)await openProductDialog(origin,{copyFrom:source});
          },false,{copyProduct:''}));
          const remove=button('删除',origin=>openProductDialog(origin,{archiveProduct:product}),false,{deleteProduct:''});
          remove.classList.add('wb-catalog-danger');
          actions.append(remove);
        }
        row.append(thumbnail(product),summary,price,status,actions);
        return row;
      }
      function paint(){
        const matches=filtered();
        const pages=Math.max(1,Math.ceil(matches.length/pageState.size));
        pageState.page=Math.max(1,Math.min(pageState.page,pages));
        const start=(pageState.page-1)*pageState.size;
        list.replaceChildren(...matches.slice(start,start+pageState.size).map(productRow));
        emptyText.hidden=!!matches.length;
        emptyText.textContent=products.length?'没有匹配的商品。试试商品名称或 SKU。':'商品知识库还没有商品。可在上方添加。';
        count.textContent='共 '+matches.length+' 件商品';
        pager.replaceChildren();
        pager.hidden=!matches.length;
        if(!matches.length)return;
        function go(page,focusKey){
          pageState.page=page;
          pageState.highlightedProduct=null;
          paint();
          const target=[...pager.querySelectorAll('button')].find(item=>item.dataset.catalogPage===focusKey);
          (target&&!target.disabled?target:pager.querySelector('[aria-current="page"]'))?.focus({preventScroll:true});
        }
        const previous=button('上一页',()=>go(pageState.page-1,'previous'),false,{catalogPage:'previous'});
        previous.disabled=pageState.page===1;
        pager.append(previous);
        const numbers=[...new Set([1,pageState.page-1,pageState.page,pageState.page+1,pages])].filter(page=>page>0&&page<=pages).sort((a,b)=>a-b);
        let last=0;
        for(const page of numbers){
          if(page-last>1){const gap=E('span','…');gap.setAttribute('aria-hidden','true');pager.append(gap);}
          const tab=button(String(page),()=>go(page,String(page)),false,{catalogPage:String(page)});
          tab.setAttribute('aria-label','第 '+page+' 页');
          if(page===pageState.page)tab.setAttribute('aria-current','page');
          pager.append(tab);
          last=page;
        }
        const next=button('下一页',()=>go(pageState.page+1,'next'),false,{catalogPage:'next'});
        next.disabled=pageState.page===pages;
        pager.append(next);
      }
      search.addEventListener('input',()=>{pageState.query=search.value;pageState.page=1;pageState.highlightedProduct=null;paint();});
      function fit(){
        const height=window.clientHeight,rowHeight=rowProbe.getBoundingClientRect().height;
        if(!height||!rowHeight)return;
        const nextSize=Math.max(1,Math.floor((height+.25)/rowHeight));
        if(nextSize===pageState.size)return;
        const active=document.activeElement?.closest('[data-catalog-product-id]');
        const activeId=list.contains(active)?active.dataset.catalogProductId:null;
        const anchor=activeId||pageState.highlightedProduct;
        const anchorIndex=anchor?filtered().findIndex(product=>product.ref.id===anchor):-1;
        const first=anchorIndex>=0?anchorIndex:(pageState.page-1)*pageState.size;
        pageState.size=nextSize;
        pageState.page=Math.floor(first/nextSize)+1;
        paint();
        if(activeId)[...list.children].find(row=>row.dataset.catalogProductId===activeId)?.querySelector('[data-open-product]')?.focus({preventScroll:true});
      }
      node._activateCatalog=()=>{
        catalogResizeObserver?.disconnect();
        fit();
        if(typeof ResizeObserver==='function'){
          catalogResizeObserver=new ResizeObserver(fit);
          catalogResizeObserver.observe(window);
          catalogResizeObserver.observe(rowProbe);
        }
      };
      node._showCatalogProduct=(id,{clearSearch=true,highlight=false}={})=>{
        let index=filtered().findIndex(product=>product.ref.id===id);
        if(index<0&&clearSearch&&products.some(product=>product.ref.id===id)){
          pageState.query='';
          search.value='';
          index=products.findIndex(product=>product.ref.id===id);
        }
        if(index<0)return null;
        pageState.page=Math.floor(index/pageState.size)+1;
        if(highlight)pageState.highlightedProduct=id;
        paint();
        return [...list.children].find(row=>row.dataset.catalogProductId===id);
      };
      if(initialPreparation){
        const actions=E('div',undefined,'wb-catalog-selection-actions');
        const save=async()=>{
          const r=await command('update_preparation_products',{preparation_id:pid,expected_revision:selectionBuffer.base_ref.revision,selected_product_refs:[...picked.values()]});
          clearBuffer(selectionBuffer);
          changedBusiness(r);
          await refresh({force:true});
          notice('本场商品引用已保存。');
        };
        selectionBuffer=registerBuffer('product-selection:'+pid,'本场商品引用',initialPreparation.ref,()=>[...picked.values()],refs=>{
          picked.clear();
          for(const ref of refs)picked.set(ref.id,ref);
          paint();
        },save,actions);
        actions.append(button('保存本场引用',save,true));
        footer.append(actions);
      }
      paint();
    }
    function productControl(key,descriptor,value,defs,customLabel){const schema=effectiveSchema(descriptor,defs);const label=customLabel||FIELD_LABELS[key]||descriptor.title||key;let c,read,write,validate;
      if(key==='core_selling_points'){
        c=E('div',undefined,'wb-core-points');let rows=[];
        const add=value=>{const parsed=unpackCorePoint(value),row=E('div',undefined,'wb-core-point-row');const name=control('text',parsed.name,'卖点名称'),text=control('textarea',parsed.content,'卖点内容');name.placeholder='名称（选填，如足弓支撑）';name.maxLength=120;text.placeholder='填写有依据的具体特点与用途';text.maxLength=4000;text.rows=2;const record={row,name,text,original:parsed.original};rows.push(record);const actions=E('div',undefined,'wb-core-point-actions');actions.append(button('上移',()=>{const i=rows.indexOf(record);if(i>0){rows.splice(i,1);rows.splice(i-1,0,record);paint();notify();}}),button('下移',()=>{const i=rows.indexOf(record);if(i<rows.length-1){rows.splice(i,1);rows.splice(i+1,0,record);paint();notify();}}),button('删除卖点',()=>{rows=rows.filter(r=>r!==record);paint();notify();}));row.append(name,text,actions);return record;};
        const list=E('div');const notify=()=>c.dispatchEvent(new Event('input',{bubbles:true}));const paint=()=>{list.replaceChildren(...rows.map((r,i)=>{r.name.setAttribute('aria-label','卖点名称 '+(i+1));r.text.setAttribute('aria-label','卖点内容 '+(i+1));return r.row;}));};
        const pack=row=>packCorePoint({name:row.name.value,content:row.text.value,original:row.original});
        read=()=>rows.map(pack);
        validate=()=>{
          if(rows.some(row=>!row.text.value.trim()))throw Error('每条核心卖点都需要填写内容，或删除空行');
          if(rows.some(row=>{const value=pack(row);return value.length>4000&&value!==row.original;}))
            throw Error('单条卖点名称与内容合计不能超过4000字；可拆分为多条卖点');
        };
        write=value=>{rows=[];for(const item of value||[])add(item);paint();};write(value||[]);c.append(list,button('添加卖点',()=>{add('');paint();notify();},false,{addCorePoint:''}));
      }
      else if(key==='specifications'&&schema.type==='array'){
        c=E('div',undefined,'wb-specification-editor');const list=E('div',undefined,'wb-specification-list');let rows=[];
        const heading=E('div',undefined,'wb-specification-header');heading.setAttribute('aria-hidden','true');heading.append(E('span','规格名称'),E('span','规格内容'),E('span','操作'));
        const notify=()=>c.dispatchEvent(new Event('input',{bubbles:true}));
        const paint=()=>list.replaceChildren(...rows.map((r,i)=>{r.name.setAttribute('aria-label','规格名称 '+(i+1));r.value.setAttribute('aria-label','规格内容 '+(i+1));r.remove.setAttribute('aria-label','删除规格 '+(i+1));return r.row;}));
        const add=item=>{const row=E('div',undefined,'wb-specification-row');row.dataset.specificationRow='';const name=control('text',item?.name||'','规格名称'),value=control('text',item?.value||'','规格内容');name.dataset.field='name';value.dataset.field='value';name.maxLength=60;value.maxLength=500;name.placeholder='如尺码、材质';value.placeholder='填写准确规格';const record={row,name,value};record.remove=button('删除',()=>{rows=rows.filter(r=>r!==record);paint();notify();});row.append(name,value,record.remove);rows.push(record);};
        read=()=>rows.map(r=>({name:r.name.value,value:r.value.value}));write=value=>{rows=[];for(const item of value||[])add(item);paint();};write(value||[]);c.append(heading,list,button('添加规格',()=>{add({name:'',value:''});paint();notify();}));
      }
      else if(schema.enum){c=select(value,schema.enum.map(v=>[v,key==='sales_platform'?(PLATFORM_LABELS[v]||v):v==='available'?'在售':v==='unavailable'?'停售':v]),label);read=()=>c.value;write=v=>c.value=v??'';}
      else if(schema.type==='boolean'){c=E('input');c.type='checkbox';c.checked=!!value;read=()=>c.checked;write=v=>c.checked=!!v;}
      else if(schema.type==='number'||schema.type==='integer'){c=control('number',value,label);c.step=schema.type==='integer'?'1':'any';if(schema.minimum!==undefined)c.min=schema.minimum;if(schema.maximum!==undefined)c.max=schema.maximum;read=()=>{if(c.value==='')return schema.nullable?null:0;const n=Number(c.value);if(!Number.isFinite(n))throw Error(label+'须填写数字');return n;};write=v=>c.value=v??'';}
      else if(schema.type==='array'&&effectiveSchema(schema.items||{},defs).type!=='object'){const item=effectiveSchema(schema.items||{},defs);if(item.enum){c=E('div',undefined,'wb-checklist');const checks=item.enum.map(v=>{const check=E('input');check.type='checkbox';check.value=v;check.checked=(value||[]).includes(v);if(key==='hidden_optional_fields')check.setAttribute('aria-label','不适用：'+(FIELD_LABELS[v]||v));const row=E('label');row.append(check,E('span',FIELD_LABELS[v]||v));c.append(row);return check;});read=()=>checks.filter(x=>x.checked).map(x=>x.value);write=v=>checks.forEach(x=>x.checked=(v||[]).includes(x.value));}else{c=control('textarea',(value||[]).join('\n'),label);read=()=>c.value.split('\n').map(x=>x.trim()).filter(Boolean);write=v=>c.value=(v||[]).join('\n');}}
      else if(schema.type==='array'){c=E('div',undefined,'wb-list');const listNode=c;const itemSchema=effectiveSchema(schema.items||{},defs);let rows=[];const add=(item={})=>{const row=E('div',undefined,'wb-card');const fields={};if(key==='knowledge_documents'&&item.id)row.dataset.productDocumentId=item.id;for(const [k,desc]of Object.entries(itemSchema.properties||{})){if(k==='id'&&!item[k])item[k]='doc_'+uid().replace(/-/g,'');const child=productControl(k,desc,item[k]??schemaDefault(desc,defs),defs,({name:'规格名称',value:'规格内容',id:'文档编号',title:'文档标题',content:'文档内容'})[k]||k);fields[k]=child;row.append(child.box);}const record={row,fields};rows.push(record);row.append(button('删除此条',()=>{rows=rows.filter(x=>x!==record);row.remove();c.dispatchEvent(new Event('input',{bubbles:true}));}));listNode.append(row);};for(const item of value||[])add(copy(item));const actions=E('div',undefined,'wb-form-actions');actions.append(button('添加一条',()=>{add();c.dispatchEvent(new Event('input',{bubbles:true}));}));const wrap=E('div');wrap.append(c,actions);c=wrap;read=()=>rows.map(r=>Object.fromEntries(Object.entries(r.fields).map(([k,f])=>[k,f.read()])));write=v=>{rows=[];listNode.replaceChildren();for(const x of v||[])add(copy(x));};}
      else{c=control(['name','sku_id','currency','material','shipping_origin','shelf_life_unit'].includes(key)?'text':'textarea',value,label);if(schema.maxLength)c.maxLength=schema.maxLength;read=()=>c.value;write=v=>c.value=v??'';}
      c.dataset.field=key;const box=field(label,c);box.dataset.agentField='/'+key;return {box,c,read,write,validate};}
    async function renderProduct(node,productId,productRecord,{onSaved,draftKey='product:'+(productId||'new'),copyFrom}={}){const record=productRecord||(productId?await api.query('read_product',{product_id:productId}):null);node._productRef=record?.ref;node._productLabel=record?.body.name||'';const productSchema=schema.schema.product;const defs=productSchema.$defs||{};const fields={};const initial={};
      const copied=copyFrom?{...copy(copyFrom.body),
        name:Array.from(copyFrom.body.name).slice(0,56).join('')+'（副本）',
        sku_id:'',promo_price:null,promotion:'',
        knowledge_documents:(copyFrom.body.knowledge_documents||[]).map(doc=>({...doc,id:'doc_'+uid().replace(/-/g,'')}))
      }:null;
      for(const[k,s]of Object.entries(productSchema.properties)){initial[k]=record?.body[k]??copied?.[k]??schemaDefault(s,defs);fields[k]=productControl(k,s,initial[k],defs);}const initialPoints=copy(initial.core_selling_points?.length?initial.core_selling_points:String(initial.selling_points||'').split('\n').filter(v=>v.trim()));fields.core_selling_points?.write(initialPoints);if(!onSaved)node.append(header(record?'编辑商品':'新建共享商品',record?refName(record.ref):'本场优惠请在「本场配置」中设置。',[button('返回商品知识库',()=>navigate({view:'products',product_id:null,action:null}))]));const form=E('form',undefined,'wb-product-form');form.addEventListener('submit',e=>e.preventDefault());
      const first=['name','sku_id','sale_status','sales_platform','price','currency','image'];const managed=['selling_points','core_selling_points','specifications','faq','knowledge','knowledge_documents','shipping_time','return_policy','promo_price','promotion'];const rest=Object.keys(fields).filter(k=>!first.includes(k)&&!managed.includes(k));const grid=E('div',undefined,'wb-grid');for(const k of first)if(fields[k])grid.append(fields[k].box);form.append(grid);if(fields.core_selling_points)form.append(fields.core_selling_points.box);else if(fields.selling_points)form.append(fields.selling_points.box);
      if(fields.specifications){fields.specifications.box.classList.add('wb-product-specifications');form.append(fields.specifications.box);}
      if(fields.faq){fields.faq.box.classList.add('wb-product-faq');fields.faq.c.rows=5;fields.faq.c.placeholder='逐条写清常见问题与回答，例如：问：如何选尺码？答：按已确认的尺码说明选择。';fields.faq.box.append(E('p','顾客常问的问题与回答保存在这里，与补充知识文档分别管理。','wb-muted'));form.append(fields.faq.box);}
      const knowledge=section('知识库与补充文档');knowledge.classList.add('wb-product-knowledge');knowledge.append(E('p','补充使用说明、注意事项和商品资料；常见问答请填写在上方「常见问题」。','wb-muted'));for(const key of ['knowledge','knowledge_documents'])if(fields[key])knowledge.append(fields[key].box);form.append(knowledge);
      const fulfillment=section('履约资料');fulfillment.classList.add('wb-product-fulfillment');for(const key of ['shipping_time','return_policy'])if(fields[key])fulfillment.append(fields[key].box);form.append(fulfillment);
      const more=E('details');more.open=false;more.append(E('summary','其他可选商品资料'));const extras=E('div',undefined,'wb-grid');for(const k of rest)extras.append(fields[k].box);more.append(extras);form.append(more);
      let docBox,paintPlatform=()=>{};const paintOptional=()=>{const hidden=applyOptionalProductVisibility(fields);if(docBox)docBox.hidden=hidden.has('knowledge_documents');};
      fields.hidden_optional_fields?.box.append(E('p','标记不适用后该项不用于生成和检索；保存会清空旧值，重新添加需填写。保存前取消勾选可恢复当前输入。','wb-muted'));
      fields.hidden_optional_fields?.c.addEventListener('change',paintOptional);paintOptional();
      const read=()=>{const value=activeProductFields(Object.fromEntries(Object.entries(fields).map(([k,f])=>[k,['promo_price','promotion'].includes(k)?copy(initial[k]):f.read()])));if(fields.core_selling_points){if(equal(value.core_selling_points,initialPoints)){value.core_selling_points=copy(initial.core_selling_points||[]);value.selling_points=initial.selling_points||'';}else value.selling_points=value.core_selling_points.join('\n');}return value;};const write=value=>{Object.entries(fields).forEach(([k,f])=>f.write(k==='core_selling_points'?(value[k]?.length?value[k]:String(value.selling_points||'').split('\n').filter(v=>v.trim())):value[k]===undefined?initial[k]:value[k]));paintOptional();paintPlatform();};let buffer;const save=async()=>{fields.core_selling_points?.validate();let value=read();if(!value.name?.trim())throw Error('请填写商品名称');const checked=await api.query('validate_product_assembly',{schema_hash:schema.schema_hash,fields:value});if(!checked.valid)throw Object.assign(Error('商品资料有缺项或格式不正确'),{details:checked.errors});value=checked.normalized_fields;const canonicalChanged=!equal(value.core_selling_points,initial.core_selling_points);const patches=Object.keys(value).filter(k=>!(canonicalChanged&&k==='selling_points')&&canonicalProductField(value[k])!==canonicalProductField(initial[k])).map(k=>({path:'/'+k,op:'set',value:value[k]}));
        if(record&&!patches.length){
          if(onSaved){
            clearBuffer(buffer);
            await onSaved(record.ref,{unchanged:true});
          }else{
            buffer.dirty=false;form.classList.remove('wb-dirty');persist(buffer);options.onDirty?.(getDirtySummary());
            notice('没有需要保存的修改。');
          }
          return true;
        }
        let result;try{result=await command('save_product',{product_id:record?.ref.id||null,expected_revision:record?buffer.base_ref.revision:null,schema_hash:schema.schema_hash,patches,fields:value,source_manifest_refs:record?.body.source_manifest_refs||[]});}catch(error){if(error.code==='PRODUCT_IDENTITY_CONFIRMATION_REQUIRED'){showIdentity(error,value);return false;}if(['REVISION_CONFLICT','VERSION_CONFLICT'].includes(error.code)){const head=await api.query('read_product',{product_id:record.ref.id});notice('商品已被另一个操作更新；你的输入已保留。请对比后明确重新应用。','warning');const conflict=section('商品版本冲突');conflict.append(jsonDetails('原基础版本',initial),jsonDetails('当前服务端版本',head.body),jsonDetails('我的未保存内容',value),button('基于新版重新应用我的修改',async()=>{const own=read();const updated={...head.body};for(const p of patches)updated[p.path.slice(1)]=own[p.path.slice(1)];if(canonicalChanged)updated.selling_points=(updated.core_selling_points||[]).join('\n');for(const k of Object.keys(fields))initial[k]=head.body[k]??schemaDefault(productSchema.properties[k],defs);buffer.base_ref=head.ref;record.ref=head.ref;write(updated);persist(buffer);conflict.remove();notice('已把你的字段修改应用到新版基础；请再次点击保存修改。','warning');}));(form.querySelector('.wb-product-dialog-scroll')||form).append(conflict);conflict.scrollIntoView({block:'nearest'});return false;}throw error;}return finishSave(result);};
      async function finishSave(result){
        const newRef=result.data.product_ref||result.data.record_ref;
        clearBuffer(buffer);
        if(record&&newRef)replacements.set(record.ref.id,newRef);
        if(onSaved){
          changedBusiness(result);
          await onSaved(newRef);
          return true;
        }
        current.product_id=newRef?.id||record?.ref.id;
        current.action=null;
        changedBusiness(result);
        cache.clear();
        await open({...current});
        notice('商品已保存。相关历史话术不会自动改写。');
        return true;
      }
      function showIdentity(error,value){form.querySelector('[data-product-identity-choice]')?.remove();const choices=section('已有同名商品，请明确区分');choices.dataset.productIdentityChoice='';choices.append(E('p','可以更新已有商品，或确认这是不同的新商品。'));for(const ref of error.details?.candidate_refs||[])choices.append(button('查看已有商品 '+ref.id,()=>{if(onSaved)closeProductDialog({restoreFocus:false,keepDraft:true});return navigate({view:'products',product_id:ref.id,action:null});}));choices.append(button('确认是不同商品，新增',async()=>{const proof=await api.request('/api/prelive/v2/human/product-identity',{target_name:value.name,candidate_refs:error.details.candidate_refs});const result=await command('save_product',{product_id:null,expected_revision:null,schema_hash:schema.schema_hash,patches:[],fields:value,identity_decision_ref:proof.identity_decision_ref});if(onSaved)return finishSave(result);clearBuffer(buffer);changedBusiness(result);await navigate({view:'products',product_id:result.data.product_ref.id,action:null});},true));const scroll=form.querySelector('.wb-product-dialog-scroll');if(scroll){scroll.prepend(choices);scroll.scrollTop=0;}else (form.querySelector('.wb-product-dialog-scroll')||form).append(choices);choices.scrollIntoView({block:'nearest'});notice('未覆盖任何已有商品，请先选择。','warning');}
      const baseRef=record?.ref||null;
      buffer=registerBuffer(draftKey,'商品：'+(record?.body.name||copied?.name||'新商品'),baseRef,read,write,save,form,{ref:copy(baseRef),value:read()});
      for(const[k,f]of Object.entries(fields))if(record&&form.contains(f.box)){f.box.dataset.productField='/'+k;const name=f.box.querySelector(':scope > label');if(name){name.dataset.selectField=k;selectable(name,()=>selected('商品库 / '+record.body.name+' / '+(FIELD_LABELS[k]||k),{object_ref:record.ref,field_path:'/'+k},{view:'products',product_id:productId,field_path:'/'+k},buffer),{target:{object_ref:record.ref,field_path:'/'+k}});}}
      if(fields.sales_platform){
        const template=E('div',undefined,'wb-platform-guidance'),hint=E('p',undefined,'wb-muted'),notes=E('ul',undefined,'wb-platform-notes'),sources=E('div',undefined,'wb-platform-sources'),shippingChoices=E('div',undefined,'wb-platform-shipping-choices'),confirmation=E('div',undefined,'wb-platform-confirmation');
        const autoApplied=new Map(),policies=schema.platform_policies||{},policy=()=>policies[fields.sales_platform.read()];
        const defaultText=(p,key)=>typeof p?.defaults?.[key]==='string'?p.defaults[key].trim():'';
        const legacyNeutral=text=>/^按商品或订单页(?:已确认的)?(?:约定的时间发货|售后约定处理)[。.]?$/.test(text);
        const replaceable=(key,text)=>!text||text===autoApplied.get(key)||legacyNeutral(text)||Object.values(policies).some(p=>text===defaultText(p,key)||(p.replaceable_defaults?.[key]||[]).includes(text));
        const restore=button('恢复平台默认规则',()=>applyPolicy({confirmCustom:true}));
        function paintPolicy(){const p=policy();confirmation.replaceChildren();notes.replaceChildren();sources.replaceChildren();shippingChoices.replaceChildren();const name=PLATFORM_LABELS[fields.sales_platform.read()]||'所选平台';restore.disabled=!p||!['shipping_time','return_policy'].some(key=>defaultText(p,key));hint.textContent=p?(p.scope_label||name+'平台规则')+(p.version?' · 版本 '+p.version:'')+(p.checked_at?' · 核对 '+p.checked_at:'')+'；请结合实际订单与商品类目核对。':name+'：当前没有已确认的默认履约规则，保留现有填写。';for(const text of p?.notes||[])notes.append(E('li',text));for(const source of p?.sources||[]){let url;try{url=new URL(source.url);}catch{continue;}if(url.protocol!=='https:'||url.username||url.password)continue;const link=E('a',source.title||'规则来源');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';sources.append(link);}for(const choice of Array.isArray(p?.shipping_options)?p.shipping_options:[]){if(typeof choice.text!=='string'||!choice.text.trim())continue;shippingChoices.append(button(choice.label||'采用所选发货承诺',()=>applyShippingChoice(choice),false,{shippingOption:choice.id||''}));} }
        function applyShippingChoice(choice,{force=false}={}){const f=fields.shipping_time,text=choice.text.trim();if(!f||!text)return;if(f.c.disabled||f.c.hasAttribute('data-agent-input-locked')){notice('发货时效正被 Agent 处理，未改动。');return;}const old=f.read().trim();confirmation.replaceChildren();if(!force&&old!==text&&!replaceable('shipping_time',old)){confirmation.append(E('p','已有人工填写的发货说明。确认后仅替换发货承诺，售后等其他内容保持原样。','wb-muted'),button('确认替换发货承诺',()=>applyShippingChoice(choice,{force:true})),button('保留人工发货说明',()=>confirmation.replaceChildren()));return;}if(old!==text){f.write(text);autoApplied.delete('shipping_time');const hidden=(fields.hidden_optional_fields?.read()||[]).filter(key=>key!=='shipping_time');fields.hidden_optional_fields?.write(hidden);paintOptional();changed(buffer);}notice('已采用所选发货承诺，保存后生效；请确认实际履约能力。');}
        function applyPolicy({force=false,confirmCustom=false}={}){const p=policy(),written=[],custom=[],locked=[];for(const key of ['shipping_time','return_policy']){const f=fields[key],value=defaultText(p,key);if(!f||!value)continue;if(f.c.disabled||f.c.hasAttribute('data-agent-input-locked')){locked.push(FIELD_LABELS[key]);continue;}const old=f.read().trim();if(!force&&!replaceable(key,old)){custom.push(FIELD_LABELS[key]);continue;}if(old!==value){f.write(value);written.push(key);}autoApplied.set(key,value);}
          if(written.length){const hidden=(fields.hidden_optional_fields?.read()||[]).filter(key=>!written.includes(key));fields.hidden_optional_fields?.write(hidden);paintOptional();changed(buffer);}confirmation.replaceChildren();
          if(confirmCustom&&custom.length){confirmation.append(E('p','已保留人工填写的'+custom.join('、')+'。确认后才用平台默认规则替换这些内容。','wb-muted'),button('确认替换人工填写的履约内容',()=>applyPolicy({force:true})),button('保留人工填写',()=>confirmation.replaceChildren()));}
          const messages=[];if(written.length)messages.push('已按平台规则填入'+written.map(key=>FIELD_LABELS[key]).join('、')+'，保存后生效');if(custom.length)messages.push('保留人工填写的'+custom.join('、'));if(locked.length)messages.push(locked.join('、')+'正被 Agent 处理，未改动');if(!messages.length)messages.push(p?'当前默认项无需修改':'此平台没有已确认的默认规则，未填入内容');notice(messages.join('；')+'。');
        }
        fields.sales_platform.c.addEventListener('change',()=>{paintPolicy();applyPolicy();});for(const key of ['shipping_time','return_policy'])fields[key]?.c.addEventListener('input',()=>{if(fields[key].read().trim()!==autoApplied.get(key))autoApplied.delete(key);});
        template.append(hint,notes,sources,shippingChoices,restore,confirmation);fulfillment.prepend(template);paintPlatform=paintPolicy;paintPolicy();
      }
      if(fields.image){
        fields.image.c.hidden=true;
        const preview=E('img',undefined,'wb-product-image-preview');preview.alt='商品主图预览';preview.style.maxWidth='180px';preview.style.maxHeight='180px';
        const emptyImage=E('p','添加商品主图，方便在共享商品库识别。','wb-muted');
        const paintImage=()=>{const value=fields.image.read();const allowed=/^data:image\/(?:png|jpeg|webp);base64,/i.test(value)||/^\/[^/]/.test(value);preview.hidden=!allowed;emptyImage.hidden=allowed;preview.src=allowed?value:'';};paintImage();
        const upload=E('input');upload.type='file';upload.accept='image/png,image/jpeg,image/webp';upload.dataset.bufferIgnore='';upload.setAttribute('aria-label','上传商品图片');upload.addEventListener('change',()=>{const file=upload.files[0];if(!file)return;if(file.size>3000000||!['image/png','image/jpeg','image/webp'].includes(file.type)){notice('请上传小于3 MB的 JPG、PNG 或 WebP 图片。','warning');return;}const reader=new FileReader();reader.onload=()=>{if(!form.isConnected||buffers.get(draftKey)!==buffer)return;fields.image.write(String(reader.result));paintImage();changed(buffer);};reader.readAsDataURL(file);});
        fields.image.box.append(preview,emptyImage,upload,button('移除商品图片',()=>{fields.image.write('');paintImage();changed(buffer);}));
      }
      const actions=E('div',undefined,'wb-form-actions');if(onSaved)actions.classList.add('wb-product-dialog-actions');actions.append(button(record?'保存修改':'保存商品',save,true));form.append(actions);const docFile=E('input');docFile.type='file';docFile.dataset.bufferIgnore='';docFile.accept='.txt,.md,.csv,.json';docFile.setAttribute('aria-label','导入商品知识文档');docBox=section('从文本文件补充商品知识库');docBox.append(docFile,button('导入为知识文档',async()=>{const f=docFile.files?.[0];if(!f)throw Error('请先选择文本文件');if(f.size>60000)throw Error('单份知识文档请控制在15000字以内');const text=await f.text();if(text.length>15000)throw Error('知识文档内容不能超过15000字');const docs=fields.knowledge_documents.read();docs.push({id:'doc_'+uid().replace(/-/g,''),title:f.name.slice(0,120),content:text});fields.knowledge_documents.write(docs);changed(buffer);docFile.value='';notice('文档已填入表单，保存商品后才生效。');}));knowledge.append(docBox);paintOptional();node.append(form);}
    function roomWorkspace(roomId=null,action=null){
      return {view:'room-config',room_id:roomId,preparation_id:null,product_id:null,room_section:null,action};
    }
    function returnToRoomList(){
      return navigate({view:'rooms',room_id:null,preparation_id:null,product_id:null,room_section:null,action:null});
    }
    function renderRooms(node){
      const rooms=snapshot.rooms||[];
      const create=()=>navigate(roomWorkspace(null,'new_room'));
      const heading=header('直播间列表',rooms.length?'共 '+rooms.length+' 个直播间':'还没有直播间',[
        button('新建直播间',create,true,{newRoom:''})
      ]);
      heading.classList.replace('wb-card-header','wb-room-list-header');
      node.append(heading);
      if(!rooms.length){node.append(empty('从新建直播间开始','设置直播间名称和默认播报声音后，就可以准备第一场。'));return;}
      const list=E('ul',undefined,'wb-room-list');
      const pageSize=5,totalPages=Math.ceil(rooms.length/pageSize);
      let page=1;
      const previous=button('上一页',()=>{page--;paint();if(previous.disabled)next.focus({preventScroll:true});});
      const next=button('下一页',()=>{page++;paint();if(next.disabled)previous.focus({preventScroll:true});});
      const count=E('span',undefined,'wb-muted');
      count.setAttribute('aria-live','polite');
      function paint(){
        list.replaceChildren();
        for(const room of rooms.slice((page-1)*pageSize,page*pageSize)){
          const row=E('li',undefined,'wb-room-card wb-room-list-row');
          row.dataset.roomId=room.ref.id;
          const info=E('div',undefined,'wb-room-list-info');
          const voice=(room.body.tts_provider||room.body.voice_provider)==='minimax'?'MiniMax':'本机普通话';
          info.append(E('h3',room.body.name),E('p','默认声音 '+voice+(room.body.adopted_publication_ref?' · 已采用播出内容':'')));
          const enter=button('进入直播间',()=>navigate(roomWorkspace(room.ref.id)));
          enter.setAttribute('aria-label','进入直播间 '+room.body.name);
          row.append(info,enter);
          list.append(row);
        }
        count.textContent='第 '+page+' / '+totalPages+' 页';
        previous.disabled=page===1;
        next.disabled=page===totalPages;
      }
      node.append(list);
      if(totalPages>1){
        const pager=E('nav',undefined,'wb-room-list-pager');
        pager.setAttribute('aria-label','直播间分页');
        pager.append(previous,count,next);
        node.append(pager);
      }
      paint();
    }
    function renderRoomMaterialImport(host,room,preparation){
      if(!options.onUploadRoomMaterials||!options.onSubmitRoomMaterials)return;
      const roomId=room.ref.id;
      const flowKey=id=>roomId+':'+(id||'new');
      const key=flowKey(preparation?.ref.id);
      const flow=roomMaterialFlows.get(key)||{files:[],note:'',batch:null,target:null,pending:false,unknown:false};
      roomMaterialFlows.set(key,flow);
      const box=section('导入资料到本场');
      box.classList.add('wb-product-material-flow');
      box.dataset.roomMaterialFlow='';
      box.append(E('p',preparation
        ?'商品资料会归入共享商品库并关联当前本场，活动和优惠只进入当前本场配置。'
        :'导入时会在当前直播间创建本场。商品资料归入共享商品库，活动和优惠只进入新本场。','wb-muted'));
      const files=E('input');
      files.type='file';
      files.multiple=true;
      files.accept='.txt,.md,.csv,.json,.pdf,.doc,.docx,.rtf,.xlsx,.png,.jpg,.jpeg,.webp';
      files.setAttribute('aria-label','选择本场导入资料');
      const note=control('textarea',flow.note,'本场资料补充说明（选填）');
      note.rows=2;
      note.maxLength=4000;
      const fileList=E('ul',undefined,'wb-product-material-files');
      const status=E('p',undefined,'wb-muted');
      status.setAttribute('role','status');
      const bind=ref=>{
        flow.target={room_id:roomId,preparation_id:ref.id,preparation_ref:copy(ref),room_name:room.body.name};
        roomMaterialFlows.set(flowKey(ref.id),flow);
        if(key!==flowKey(ref.id))roomMaterialFlows.delete(key);
      };
      const submit=button('导入并整理',async()=>{
        if(flow.pending||(!flow.files.length&&!flow.batch))return;
        flow.pending=true;
        update('正在准备本场资料…');
        try{
          if(!flow.unknown){
            const currentPreparation=prep();
            if((currentPreparation?.body.room_id||current.room_id)!==roomId
                || preparation&&currentPreparation?.ref.id!==preparation.ref.id)
              throw Error('当前本场已切换，请在要导入的场次重新提交。');
            if(currentPreparation&&buffers.get('session:'+currentPreparation.ref.id)?.dirty)
              throw Error('请先保存或放弃当前本场的未保存配置，再导入资料。');
            if(!flow.batch&&currentPreparation)bind(currentPreparation.ref);
            if(!flow.target){
              const existingId=await resolveDefaultRoomPreparation({room_id:roomId});
              if(existingId){
                const existing=await api.query('read_preparation',{preparation_id:existingId});
                if(existing.body.room_id!==roomId)throw Error('此本场不属于当前直播间。');
                bind(existing.ref);
                await navigate({...roomWorkspace(roomId),preparation_id:existingId});
              }else await createPreparation(roomId,{onPrepared:bind});
            }
            if(!flow.target)throw Error('暂未取得本场编号，资料尚未提交。');
            if(!flow.batch||flow.batch.failed?.length){
              update('正在上传资料…');
              flow.batch=await options.onUploadRoomMaterials({files:flow.files,batch:flow.batch,room:flow.target});
              if(flow.batch.failed?.length)throw Error('部分资料尚未上传成功：'+flow.batch.failed.map(item=>item.name).join('、')+'。请重试。');
              if(!flow.batch.materialRefs?.length)throw Error('尚未有资料上传成功，请检查文件后重试。');
            }
          }
          update('正在提交本场导入…');
          await options.onSubmitRoomMaterials({batch:flow.batch,room:flow.target,instructions:flow.note});
          flow.batch=null;
          flow.files=[];
          flow.note='';
          flow.unknown=false;
          update('Agent 已受理本场导入，可在右侧查看进度和成果。');
        }catch(error){
          if(flow.batch&&error.unknown)flow.unknown=true;
          update(flow.unknown?'暂未确认任务是否受理。再次点击会核实同一请求。':error.message||'导入未提交，已保留资料。');
        }finally{flow.pending=false;update();}
      },true);
      const clear=button('清空资料',()=>{
        flow.batch=null;flow.files=[];flow.note='';flow.target=null;update('');
      });
      function paint(){
        files.disabled=flow.pending||flow.unknown||!!flow.batch;
        note.disabled=flow.pending||flow.unknown;
        if(!flow.files.length)files.value='';
        if(note.value!==flow.note)note.value=flow.note;
        submit.textContent=flow.unknown?'核实原提交':'导入并整理';
        submit.disabled=flow.pending||(!flow.files.length&&!flow.batch);
        clear.hidden=flow.unknown||(!flow.files.length&&!flow.batch);
        clear.disabled=flow.pending;
        status.textContent=flow.message||'';
        status.hidden=!status.textContent;
        fileList.replaceChildren(...flow.files.map(file=>E('li',file.name)));
        fileList.hidden=!flow.files.length;
      }
      function update(message){
        if(message!==undefined)flow.message=message;
        paint();
        if(flow.paint!==paint)flow.paint?.();
      }
      files.addEventListener('change',()=>{
        flow.files=Array.from(files.files||[]);
        update('');
      });
      note.addEventListener('input',()=>{flow.note=note.value;});
      const actions=E('div',undefined,'wb-product-material-actions');
      actions.append(submit,clear);
      box.append(files,fileList,field('补充说明（选填）',note),actions,status);
      flow.paint=paint;
      paint();
      host.append(box);
    }
    async function renderRoomConfig(node){
      const location=current;
      if(location.action==='new_room'&&!location.room_id)return renderRoomCreate(node);
      if(!location.room_id){
        node.append(empty('请先选择直播间','从直播间列表选择要配置的直播间。',[button('返回直播间列表',returnToRoomList,true)]));
        return;
      }
      if(location.room_section&&!['basic','preparations'].includes(location.room_section))return;
      const room=(snapshot.rooms||[]).find(item=>item.ref.id===location.room_id)||
        await api.query('read_room',{room_id:location.room_id});
      if(current!==location)return;
      if(location.room_section==='preparations')return renderRoomPreparations(node,room);
      if(!options.roomCanvas)return renderRoomSettings(node,room);

      node.classList.add('wb-room-editor-grid');
      const defaults=section('直播间默认设置');
      defaults.id='room-defaults-tile';
      defaults.classList.add('wb-room-tile','wb-room-inline-slot');
      defaults.dataset.inlineRoomSection='basic';
      node.append(defaults);
      await renderRoomSettings(defaults,room);
      if(current!==location)return;

      const overview=section('准备概况');
      overview.classList.add('wb-room-canvas-section');
      overview.dataset.inlineRoomSection='overview';
      const preparation=prep();
      node.append(overview);
      if(!preparation){
        overview.append(E('p','选择并保存商品后，可在这里查看本场准备进度并配置话术。','wb-muted'));
        renderRoomMaterialImport(overview,room,null);
        return;
      }
      const body=preparation.body;
      const facts=E('ul',undefined,'wb-room-overview-facts');
      for(const [label,value] of [
        ['本场状态',preparationStateLabel(preparation,room)],
        ['已选商品',body.selected_product_refs.length+' 件'],
        ['话术版本',currentScriptVariants(details().scripts||[]).length+' 版'],
        ['本场设置',body.draft_ref?'已保存':'待填写']
      ]){
        const fact=E('li');fact.append(E('span',label),E('strong',value));facts.append(fact);
      }
      overview.append(facts);
      if(details().asset_checklist)renderAssets(overview,details().asset_checklist);

      const scripts=E('section',undefined,'wb-room-canvas-section');
      scripts.dataset.inlineRoomSection='scripts';
      scripts.append(E('h3','话术与排序','wb-room-section-title'));
      const session=E('section',undefined,'wb-room-canvas-section');
      session.dataset.inlineRoomSection='session';
      node.append(session,scripts);
      await renderSession(session,{inline:true});
      if(current===location)await renderScripts(scripts);
    }
    async function showRoomProductDetail(destination,opener){
      if(!closeProductDialog({restoreFocus:false}))return;
      const location=current;
      const record=await api.query('read_product',{product_id:destination.product_id});
      if(current!==location)return;
      if(record.body.archived)throw Error('该商品已归档。');
      if(destination.object_ref?.kind==='product'&&destination.object_ref.id!==record.ref.id)
        throw Error('成果的商品与打开目标不一致。');
      const body=await openProductDialog(opener,{record,preserveFocus:destination.preserve_focus});
      if(body)locate(body,destination,'products');
    }
    async function renderRoomOverlay(host,destination){
      const location=current, overlay={host,destination};
      activeRoomOverlay=overlay;
      if(destination.refresh)await readSnapshot();
      if(current!==location || !host.isConnected || overlay!==activeRoomOverlay || !options.roomOverlay?.open)return;
      const view=destination.room_section||destination.view;
      const node=E('div',undefined,'wb-view');
      node.dataset.view=view;
      host.replaceChildren(node);
      if(view==='preparations'){
        const room=(snapshot.rooms||[]).find(item=>item.ref.id===location.room_id);
        if(!room)throw Error('找不到当前直播间。');
        await renderRoomPreparations(node,room);
      }else if(view==='insights'){
        if(!prep())throw Error('请先保存本场选品。');
        await renderInsights(node);
      }else if(view==='changes'){
        if(!prep())throw Error('请先保存本场选品。');
        await renderChanges(node);
      }else if(view==='products'){
        await renderProducts(node);
      }
      if(current!==location||overlay!==activeRoomOverlay||!options.roomOverlay?.open)return;
      applyEditorLocks();
      if(view!=='products')locate(node,destination,view);
    }
    async function renderRoomCreate(node){
      const form=await renderRoomSettings(node);
      form?.querySelector('input[name="room-name"]')?.focus({preventScroll:true});
    }
    function roomDefaultVoice(room){
      const defaults=room.body;
      return copy(defaults.default_tts_selection||{
        provider:defaults.tts_provider||'system',
        voice:defaults.tts_voice||'Huihui',
        model:defaults.tts_model||'speech-2.8-turbo'
      });
    }
    function roomDefaultDraft(room,goal,productRefs=[]){
      return {
        theme:'',objective:goal||'准备本场直播',scheduled_at:'',timezone:'Asia/Shanghai',
        campaign_ends_at:'',language:'zh-CN',commerce:{},tts_selection:roomDefaultVoice(room),
        loop_mode:'infinite',requested_variant_counts:Object.fromEntries(
          productRefs.map(ref=>[ref.id,room.body.version_count||2])
        ),unknowns:[]
      };
    }
    async function renderRoomSettings(node,room){
      const location=current;
      voiceOptions=voiceOptions||await api.query('voice_options',{});
      if(current!==location)return;
      const form=E('form',undefined,'wb-room-settings wb-card');
      const creating=!room;
      form.classList.toggle('wb-room-create',creating);
      if(room){
        form.dataset.roomDefaults='';
        form.dataset.roomId=room.ref.id;
        form.setAttribute('aria-label','直播间默认设置');
      }
      form.addEventListener('submit',event=>event.preventDefault());
      const defaults=room?.body||{};
      const name=control('text',defaults.name||'','直播间名称');
      name.name='room-name';
      name.placeholder='例如：日用好物直播间';
      name.maxLength=80;
      name.required=true;
      const versions=options.roomCanvas?null:control('number',defaults.version_count||2,'默认话术版本数');
      if(versions){versions.min=1;versions.max=6;versions.step=1;}
      const voices=voiceEditor(roomDefaultVoice({body:defaults}),'默认');
      voices.node.classList.add('wb-room-voice');
      const basics=E('div',undefined,'wb-room-basics');
      basics.append(field('直播间名称',name));
      if(versions)basics.append(field('默认版本数 / 商品',versions));
      form.append(basics,E('p','默认设置用于新本场。已有本场配置保持原样。','wb-muted'),voices.node);
      const read=()=>({name:name.value.trim(),version_count:versions?Number(versions.value):defaults.version_count||2,loop_mode:'infinite',tts_selection:voices.read()});
      const write=value=>{name.value=value.name;if(versions)versions.value=value.version_count;voices.write(value.tts_selection);};
      const save=async()=>{
        const value=read();
        if(!value.name||value.name.length>80)throw Error('请输入 1 至 80 个字符的直播间名称。');
        if(!Number.isInteger(value.version_count)||value.version_count<1||value.version_count>6)throw Error('默认话术版本数须为 1 至 6 的整数。');
        if(creating){
          const voice=value.tts_selection;
          const result=await command('create_room',{name:value.name,initial_config:{
            version_count:value.version_count,tts_provider:voice.provider,
            ...(voice.voice?{tts_voice:voice.voice}:{}),...(voice.model?{tts_model:voice.model}:{})
          }});
          const roomId=result.data.room_ref.id;
          clearBuffer(buffer);
          cache.clear();
          changedBusiness(result);
          await navigate(roomWorkspace(roomId));
          notice('直播间已创建。可在面板选择商品并保存。');
          return;
        }
        const result=await humanMutation('/api/prelive/v2/human/room-settings',{
          room_id:room.ref.id,expected_revision:buffer.base_ref.revision,...value
        });
        clearBuffer(buffer);
        changedBusiness({data:result});
        await refresh({force:true});
        notice('直播间默认设置已保存，已有本场保持原配置。');
      };
      const buffer=registerBuffer('room-defaults:'+(room?.ref.id||'new'),creating?'新直播间':'直播间默认设置',room?.ref||null,read,write,save,form);
      const actions=E('div',undefined,'wb-form-actions wb-room-form-actions');
      const submit=button(creating?'创建直播间':options.roomCanvas?'保存默认设置':'保存直播间默认设置',()=>buffer.save(),true);
      submit.type='submit';
      if(creating)actions.append(button('返回直播间列表',returnToRoomList));
      actions.append(submit);
      form.append(actions);
      node.append(form);
      return form;
    }
    async function renderRoomPreparations(node,room){
      const head=section('本场记录');
      head.classList.add('wb-room-history');
      head.append(E('p','创建一场新的直播准备，或继续已有本场。','wb-muted'));
      const title=control('text','','新本场名称');
      title.placeholder='例如：周末居家好物';
      const toolbar=E('div',undefined,'wb-room-history-toolbar');
      toolbar.append(field('本场名称（选填）',title),button('创建本场准备',()=>createPreparation(room.ref.id,{goal:title.value.trim()||'准备本场直播'}),true));
      head.append(toolbar);
      const history=await api.query('read_session_history',{room_id:room.ref.id,limit:200});
      const rows=E('div',undefined,'wb-list');
      for(const preparation of history.preparations||history.items||[]){
        const row=E('article',undefined,'wb-list-item wb-preparation-card');
        row.dataset.preparationId=preparation.ref.id;
        row.classList.toggle('is-selected',current.preparation_id===preparation.ref.id);
        const info=E('div');
        const knownDraft=prep()?.ref.id===preparation.ref.id?details().draft:null;
        info.append(E('strong',preparationTitle(preparation,knownDraft)),
          E('p',preparationStateLabel(preparation,room)+' · '+(preparation.body.selected_product_refs?.length||0)+' 件商品 · '+(preparation.body.script_refs?.length||0)+' 版话术','wb-muted'),
          E('small',preparation.body.updated_at||preparation.body.created_at||refName(preparation.ref),'wb-record-meta'));
        if(preparation.body.goal)info.append(jsonDetails('查看本场原始任务',preparation.body.goal));
        const actions=E('div',undefined,'wb-form-actions');
        actions.append(button('进入本场',()=>navigate({
          view:'overview',room_id:room.ref.id,preparation_id:preparation.ref.id,product_id:null,room_section:null,action:null
        }),true),button('复制可复用配置',()=>showCopyPreparation(row,room,preparation)));
        row.append(info,actions);
        rows.append(row);
      }
      if(!rows.childElementCount)rows.append(empty('还没有本场','创建本场后，从共享商品库选品，再设置主题和声音。'));
      head.append(rows);
      node.append(head);
    }
    async function showCopyPreparation(host,room,source){
      host.querySelector('.wb-copy-preparation')?.remove();const sourceSnapshot=await api.query('get_workspace_snapshot',{preparation_id:source.ref.id});const old=sourceSnapshot.preparation_details?.draft?.body?.draft;
      const box=section('复制前核对');box.classList.add('wb-copy-preparation');const goal=control('text',(source.body.goal||'本场准备')+' · 新一场','复制后的本场名称');const include=E('input');include.type='checkbox';include.setAttribute('aria-label','明确复用尚未到期的活动');const expiry=Date.parse(old?.campaign_ends_at||'');const active=Number.isFinite(expiry)&&expiry>Date.now();include.disabled=!active;const line=E('label');line.append(include,E('span',active?'明确复用仍有效的活动（截至 '+old.campaign_ends_at+'）':'活动已过期或缺少截止时间，不继承优惠、赠品和活动说明。'));
      box.append(E('p','复制商品引用、使用场景、长期知识文档和声音设置。未明确复用有效活动时，优惠、赠品和含活动日期的文档不复制。新本场不复制话术、语音、文本批准或播出候选；主题和排期需要重新确认。','wb-muted'),field('新本场名称',goal),line,button('确认复制并创建新本场',async()=>{const result=await createPreparation(room.ref.id,{goal:goal.value.trim()||'准备本场直播',copyFrom:source.ref.id,expectedSourceRef:sourceSnapshot.preparation.ref,includeCampaign:include.checked});notice('已创建新本场并复制可复用配置。请检查商品修订、主题、活动和知识文档中的日期后继续。','warning');return result;},true),button('取消复制',()=>box.remove()));host.append(box);
    }
    async function resolveDefaultRoomPreparation({room_id,preparation_id}){
      if(preparation_id)return preparation_id;
      if(!room_id)return null;
      const [room,history]=await Promise.all([
        api.query('read_room',{room_id}),
        api.query('read_session_history',{room_id,limit:1})
      ]);
      const available=(history.preparations||history.items||[]).filter(item=>
        item.body.room_id===room_id&&!item.body.archived&&item.body.status!=='archived'
      );
      const adopted=room?.body?.adopted_publication_ref
        ?available.find(item=>item.ref.id===room.body.adopted_preparation_id)
        :null;
      return adopted?.ref.id||available.at(-1)?.ref.id||null;
    }
    async function saveRoomProductSelection({roomId,preparationId,expectedPreparationRef,productRefs}){
      if(!roomId||!Array.isArray(productRefs)||productRefs.some(ref=>ref?.kind!=='product'||!ref.id)||
        new Set(productRefs.map(ref=>ref.id)).size!==productRefs.length)throw Error('请核对直播间和所选商品。');
      if(!preparationId&&!productRefs.length)throw Error('请至少选择一个商品，再创建本场。');
      if(busy)throw Error('左侧有正在处理的操作，请稍候。');
      busy=true;
      let head, preparationRef;
      try{
        const room=await api.query('read_room',{room_id:roomId});
        let creation=pendingRoomCreations.get(roomId);
        if(preparationId){
          head=await api.query('read_preparation',{preparation_id:preparationId});
          if(head.body.room_id!==roomId)throw Error('此本场不属于当前直播间。');
          const expected=expectedPreparationRef||prep()?.ref;
          if(!expected||!equal(head.ref,expected))throw Object.assign(Error('本场已更新，请重新读取选品后核对。'),{code:'PREPARATION_STALE'});
        }else{
          if(!creation){
            creation={args:{room_id:roomId,room_revision:room.ref.revision,goal:'准备本场直播',source_refs:[]}};
            pendingRoomCreations.set(roomId,creation);
          }
          if(!creation.ref){
            try{creation.ref=(await command('create_preparation',creation.args)).data.preparation_ref;}
            catch(error){
              if(!error.unknown&&error.code!=='OUTCOME_UNKNOWN')pendingRoomCreations.delete(roomId);
              throw error;
            }
            head={ref:creation.ref,body:{goal:creation.args.goal,selected_product_refs:[]}};
          }else{
            preparationRef=creation.ref;
            head=await api.query('read_preparation',{preparation_id:creation.ref.id});
            if(head.body.room_id!==roomId||!equal(head.ref,creation.ref))throw Error('新本场已更新，请进入该场核对后保存。');
          }
        }
        preparationRef=head.ref;
        if(matchingLocks(editorLocks,{kind:'product_selection',id:head.ref.id}).length){
          throw Error('Agent 正在处理本场选品，请等待任务完成后再保存。');
        }
        const pinned=new Map((head.body.selected_product_refs||[]).map(ref=>[ref.id,ref]));
        const selected=productRefs.map(ref=>pinned.get(ref.id)||ref);
        if(!equal(head.body.selected_product_refs,selected)){
          try{
            const result=await command('update_preparation_products',{
              preparation_id:head.ref.id,expected_revision:head.ref.revision,selected_product_refs:selected
            });
            preparationRef=result.data.preparation_ref;
          }catch(error){
            if(error.code==='REVISION_CONFLICT')throw Object.assign(
              Error('本场已更新，请重新读取选品后核对。',{cause:error}),{code:'PREPARATION_STALE'}
            );
            throw error;
          }
        }
        if(creation?.ref?.id===preparationRef.id)pendingRoomCreations.delete(roomId);
        cache.clear();
        const outcome={preparationId:preparationRef.id,preparationRef,defaultDraftSaved:false};
        if(selected.length&&!head.body.draft_ref){
          try{
            const result=await command('save_session_draft',{
              preparation_id:preparationRef.id,expected_revision:preparationRef.revision,
              draft:roomDefaultDraft(room,head.body.goal,selected)
            });
            outcome.preparationRef=result.data.preparation_ref;
            outcome.defaultDraftSaved=true;
          }catch(error){
            outcome.warning='本场选品已保存。'+(error.unknown||error.code==='OUTCOME_UNKNOWN'
              ?'默认配置结果待核实，请刷新当前本场后查看。'
              :'默认配置未保存，请在本场特殊设置核对后保存。')+' '+error.message;
          }
        }
        return outcome;
      }catch(error){
        if(preparationRef){
          cache.clear();
          error.preparationId=preparationRef.id;
          error.preparationRef=preparationRef;
        }
        throw error;
      }finally{busy=false;}
    }
    async function refreshRoomProductReference({preparationRef,previous}){
      if(busy||hasDirty())throw Error('请先处理左侧尚未完成的编辑。');
      if(!equal(prep()?.ref,preparationRef))throw Object.assign(Error('本场已更新。'),{code:'REVISION_CONFLICT'});
      if(matchingLocks(editorLocks,{kind:'product_selection',id:preparationRef.id}).length){
        throw Error('Agent 正在处理本场选品，请等待任务完成。');
      }
      busy=true;
      try{
        const latest=await api.query('read_product',{product_id:previous.id});
        if(latest.ref.id!==previous.id||latest.body.archived||latest.body.sale_status==='unavailable'){
          throw Error('该商品的最新资料目前无法用于本场。');
        }
        if(latest.ref.revision<=previous.revision)return null;
        const receipt=await command('refresh_product_reference',{preparation_id:preparationRef.id,
          expected_revision:preparationRef.revision,expected_old_product_ref:previous,new_product_ref:latest.ref});
        cache.clear();
        return receipt;
      }finally{busy=false;}
    }
    async function createPreparation(roomId,opts={}){
      const room=await api.query('read_room',{room_id:roomId});let sourceSnapshot=null;
      if(opts.copyFrom){sourceSnapshot=await api.query('get_workspace_snapshot',{preparation_id:opts.copyFrom});if(opts.expectedSourceRef&&!equal(opts.expectedSourceRef,sourceSnapshot.preparation.ref))throw Error('源本场配置已经变化，请重新查看复制前核对信息后再创建。');}
      let result=await command('create_preparation',{room_id:roomId,room_revision:room.ref.revision,goal:opts.goal||'准备本场直播',source_refs:[]});
      const id=result.data.preparation_ref.id;
      if(!sourceSnapshot){
        let warning='';
        try{
          result=await command('save_session_draft',{preparation_id:id,expected_revision:result.data.preparation_ref.revision,
            draft:roomDefaultDraft(room,opts.goal)});
        }catch(error){
          warning=error.unknown||error.code==='OUTCOME_UNKNOWN'
            ?'新本场已创建。默认配置结果待核实，请刷新当前本场后查看。'
            :'新本场已创建。默认配置尚未保存，可在本场特殊设置核对后保存。';
        }
        const target={...roomWorkspace(roomId),preparation_id:id};
        if(opts.onPrepared){
          const ref=result.data.preparation_ref;
          if(ref?.id!==id)throw Error('保存本场时返回了其他场次，资料尚未提交。');
          opts.onPrepared(ref);
        }
        cache.clear();
        changedBusiness(result);
        await navigate(target);
        if(warning)notice(warning,'warning');
        return result;
      }
      try{if(sourceSnapshot){const source=sourceSnapshot.preparation;const selected=source.body.selected_product_refs||[];if(selected.length)result=await command('update_preparation_products',{preparation_id:id,expected_revision:result.data.preparation_ref.revision,selected_product_refs:selected});const draft=sourceSnapshot.preparation_details?.draft?.body?.draft;if(draft)result=await command('save_session_draft',{preparation_id:id,expected_revision:result.data.preparation_ref.revision,draft:reusableDraft(draft,{goal:opts.goal,includeCampaign:opts.includeCampaign})});}}
      catch(error){current={view:'session',room_id:roomId,preparation_id:id};cache.clear();changedBusiness(result);await navigate({...current});throw Object.assign(Error('新本场已创建，部分配置未复制：'+error.message+'。请在当前本场补齐，不必重复创建。'),{code:error.code,details:error.details});}
      current={view:'session',room_id:roomId,preparation_id:id};cache.clear();changedBusiness(result);await navigate({...current});return result;
    }
    function voiceEditor(initial={},prefix=''){
      const node=section(prefix?'默认播报声音':'本场播报声音'), voices=voiceOptions||{};const availability=E('p',undefined,'wb-muted');availability.setAttribute('aria-live','polite');const voiceDescription=E('p',undefined,'wb-muted');let previewButton=null;
      const provider=select(initial.provider||'system',(voices.providers||['system','minimax']).map(v=>[v,v==='system'?(options.roomCanvas&&prefix?'本机普通话':'本机免费普通话'):'MiniMax']),prefix+'语音供应商');const voice=select(initial.voice,[],prefix+'音色');const model=select(initial.model,[],prefix+'语音模型');
      const describeVoice=()=>{voiceDescription.textContent=voices.voice_details?.[provider.value]?.[voice.value]?.description||'';voiceDescription.hidden=!voiceDescription.textContent;};
      const paint=value=>{
        const entries=voices.voices?.[provider.value]||[];
        voice.replaceChildren(...entries.map(v=>{const id=typeof v==='string'?v:v.id,detail=voices.voice_details?.[provider.value]?.[id]||v,option=new Option(detail.name||id,id);option.title=detail.description||'';return option;}));
        voice.value=value?.voice||voice.options[0]?.value||'';
        describeVoice();
        const models=voices.models?.[provider.value]||['speech-2.8-turbo'];
        model.replaceChildren(...models.map(v=>new Option(v,v)));
        model.value=value?.model||models[0];
        const minimax=provider.value==='minimax';
        model.closest('.wb-field')?.toggleAttribute('hidden',!minimax);
        const state=voices.provider_status?.[provider.value];
        availability.textContent=state?.available===false?(state.reason||'当前语音供应商不可用，请检查服务配置。'):(provider.value==='system'?(prefix?'本机普通话可用。':'本机免费普通话，可用于首版功能验证。'):state?.available===true?'MiniMax 已配置，试听和生成会产生接口用量。':'MiniMax 音色支持情况会在试听时检查；不会自动切换到其他供应商。');
        availability.dataset.tone=state?.available===false?'warning':'info';
        if(prefix){
          previewNote.textContent=minimax?'试听固定示例。MiniMax 试听计入接口用量。':'试听固定示例。';
          previewNote.dataset.tone=minimax?'warning':'info';
        }
        if(previewButton)previewButton.disabled=state?.available===false;
      };
      provider.addEventListener('change',()=>paint());voice.addEventListener('change',describeVoice);node.append(field('语音供应商',provider),field('音色',voice),voiceDescription,field('语音模型',model),availability);
      const audio=routedAudio(E('audio'));audio.controls=true;audio.preload='none';audio.hidden=true;const previewNote=E('p',prefix?'试听固定示例。MiniMax 试听计入接口用量。':'固定示例用于比较音色；MiniMax 会产生语音接口用量。同一声音设置会复用试听；结果不明时先核实。本场稿件试听仍需先批准整包文本。','wb-muted');previewButton=button('试听'+(prefix?'默认':'当前')+'音色',async()=>{const result=await voicePreview(read());const url=safeUrl(result.url||result.asset?.url);if(!url)throw Error('试听未返回可播放的本机音频');audio.src=url;audio.hidden=false;try{await options.onAudioElement?.(audio);await audio.play();}catch{notice('试听已生成，点击音频播放器开始播放。');}});node.append(previewButton,audio,previewNote);paint(initial);
      const read=()=>({provider:provider.value,voice:voice.value,model:model.value});return{node,read,write:value=>{provider.value=value?.provider||'system';paint(value);}};
    }
    function commerceEditor(initial={},products=[]){
      const base=copy(initial), node=E('div',undefined,'wb-commerce-editor'), controls={}, groups={};
      let overrides=copy(base.product_overrides||{});
      const scene=control('textarea',base.scene||'','本场受众与场景');groups.scene=section('受众、场景与口播要求');groups.scene.append(field('本场受众与场景',scene));
      const promotion=control('textarea',base.promotion||'','本场活动说明');const promotionScope=select(base.promotion_scope||'all',[['all','全部本场商品'],['selected','指定商品']],'活动适用范围');
      scene.rows=promotion.rows=2;
      function picks(label,chosen){const box=E('div',undefined,'wb-checklist'), checks=[];for(const product of products){const c=E('input');c.type='checkbox';c.value=product.ref.id;c.checked=(chosen||[]).includes(c.value);c.setAttribute('aria-label',label+' '+product.body.name);const row=E('label');row.append(c,E('span',product.body.name));box.append(row);checks.push(c);}if(!checks.length)box.append(E('p','请先在商品库选择本场引用商品。','wb-muted'));return{node:box,read:()=>checks.filter(c=>c.checked).map(c=>c.value),write:v=>checks.forEach(c=>c.checked=(v||[]).includes(c.value))};}
      const promotionProducts=picks('参与本场活动',base.promotion_product_ids);groups.promotion=section('本场活动说明');groups.promotion.append(field('活动适用范围',promotionScope),promotionProducts.node,field('本场活动说明',promotion));
      const coupon=base.coupon||{mode:'none',currency:'CNY',scope:'all',product_ids:[]};groups.coupon=section('优惠（由你决定）');const mode=select(coupon.mode,[['none','不设置优惠'],['coupon','满减 / 抵扣券'],['discount','全场折扣']],'优惠方式');const amount=control('number',coupon.amount,'抵扣金额');const minimum=control('number',coupon.minimum_spend,'满额门槛');const discount=control('number',coupon.discount==null?'':Number(coupon.discount)*10,'折扣（八折填8）');const currency=select(coupon.currency||'CNY',['CNY','TWD','USD','AUD'].map(v=>[v,v]),'优惠币种');const condition=control('text',coupon.condition||'','优惠条件说明');const couponScope=select(coupon.scope||'all',[['all','全部本场商品'],['selected','指定商品']],'优惠适用范围');const couponProducts=picks('参与全场优惠',coupon.product_ids);
      amount.step=minimum.step=discount.step='0.01';amount.min='0.01';minimum.min='0';discount.min='0.01';discount.max='9.99';
      const couponFields={amount:field('抵扣金额',amount),minimum:field('满额门槛',minimum),discount:field('折扣（八折填8）',discount),currency:field('优惠币种',currency),condition:field('优惠条件说明',condition),scope:field('适用商品范围',couponScope)};
      groups.coupon.append(field('优惠方式',mode),...Object.values(couponFields),couponProducts.node,E('p','按单件金额计算。会员资格、领券条件等未确认时，不把条件优惠说成人人可得。','wb-muted'));
      const documents=productControl('knowledge_documents',{type:'array',items:{$ref:'#/$defs/Document'}},base.knowledge_documents||[],{Document:{type:'object',properties:{id:{type:'string'},title:{type:'string',maxLength:120},content:{type:'string',maxLength:15000}}}},'本场知识文档');groups.knowledge_documents=section('本场知识库');groups.knowledge_documents.append(documents.box);const documentFile=E('input');documentFile.type='file';documentFile.accept='.txt,.md,.csv,.json';documentFile.setAttribute('aria-label','导入本场知识文档');groups.knowledge_documents.append(documentFile,button('导入为本场知识文档',async()=>{const file=documentFile.files?.[0];if(!file)throw Error('请选择文本文件');if(file.size>60000)throw Error('知识文档请控制在15000字以内');const value=await file.text();if(value.length>15000)throw Error('文档不能超过15000字');const docs=documents.read();if(docs.length>=20)throw Error('本场最多20份知识文档');docs.push({id:'doc_'+uid().replace(/-/g,''),title:file.name.slice(0,120),content:value});documents.write(docs);documentFile.value='';node.dispatchEvent(new Event('input',{bubbles:true}));}));
      for(const box of Object.values(groups))node.append(box);
      const visibility=E('details',undefined,'wb-optional-settings');visibility.append(E('summary','显示 / 移除可选配置'));const hidden=new Set(base.hidden_sections||[]);for(const[key,box]of Object.entries(groups)){const check=E('input');check.type='checkbox';check.checked=!hidden.has(key);check.setAttribute('aria-label','显示'+({scene:'受众场景',coupon:'全场优惠',promotion:'活动说明',knowledge_documents:'本场知识库'})[key]);controls[key]=check;const label=E('label');label.append(check,E('span',({scene:'受众与场景',coupon:'全场优惠',promotion:'本场活动说明',knowledge_documents:'本场知识库'})[key]));check.addEventListener('change',()=>box.hidden=!check.checked);box.hidden=!check.checked;visibility.append(label);}visibility.append(E('p','移除后该部分不在本场生效，可随时重新添加。','wb-muted'));node.append(visibility);
      const paintModes=()=>{couponFields.amount.hidden=mode.value!=='coupon';couponFields.discount.hidden=mode.value!=='discount';for(const k of ['minimum','currency','condition','scope'])couponFields[k].hidden=mode.value==='none';couponProducts.node.hidden=mode.value==='none'||couponScope.value!=='selected';promotionProducts.node.hidden=promotionScope.value!=='selected';};for(const c of [mode,couponScope,promotionScope])c.addEventListener('change',paintModes);paintModes();
      const read=()=>{const value=copy(base);value.scene=scene.value;value.promotion=promotion.value;value.promotion_scope=promotionScope.value;value.promotion_product_ids=promotionProducts.read();value.coupon={...coupon,mode:mode.value,amount:mode.value==='coupon'?Number(amount.value):null,minimum_spend:minimum.value===''?null:Number(minimum.value),discount:mode.value==='discount'?Number(discount.value)/10:null,currency:currency.value,condition:condition.value,scope:couponScope.value,product_ids:couponProducts.read()};value.knowledge_documents=documents.read();value.hidden_sections=Object.entries(controls).filter(([,c])=>!c.checked).map(([k])=>k);value.product_overrides=copy(overrides);return value;};
      const write=value=>{scene.value=value.scene||'';promotion.value=value.promotion||'';promotionScope.value=value.promotion_scope||'all';promotionProducts.write(value.promotion_product_ids);const q=value.coupon||{};mode.value=q.mode||'none';amount.value=q.amount??'';minimum.value=q.minimum_spend??'';discount.value=q.discount?Number(q.discount)*10:'';currency.value=q.currency||'CNY';condition.value=q.condition||'';couponScope.value=q.scope||'all';couponProducts.write(q.product_ids);documents.write(value.knowledge_documents||[]);for(const[k,c]of Object.entries(controls)){c.checked=!(value.hidden_sections||[]).includes(k);groups[k].hidden=!c.checked;}overrides=copy(value.product_overrides||{});paintModes();};
      function writeOverride(id,value){if(value===undefined)delete overrides[id];else overrides[id]=copy(value);}
      return{node,read,write,writeOverride};
    }
    function sessionProductEditor(product,initial,fieldPath,onSelect){
      const name=product.body.name,editor=E('div',undefined,'wb-session-product-fields');
      const mode=select(initial.mode||'none',[['none','沿用日常价'],['price','本场活动价'],['discount','本场折扣']],'单品优惠方式 '+name);
      const price=control('number',initial.price,'本场活动价 '+name);
      const discount=control('number',initial.discount==null?'':initial.discount*10,'单品折扣 '+name);
      const promotion=control('textarea',initial.promotion||'','单品活动说明 '+name);
      const gifts=control('textarea',initial.gifts||'','本场赠品 '+name);
      const shipping=control('text',initial.shipping_time||'','本场发货时效 '+name);
      const exclusive=E('input'), shippingOverride=E('input');
      exclusive.type=shippingOverride.type='checkbox';
      exclusive.setAttribute('aria-label','单品优惠不叠加全场优惠 '+name);
      shippingOverride.setAttribute('aria-label','本场覆盖发货时效 '+name);
      price.min='0.01';price.step=discount.step='0.01';discount.min='0.01';discount.max='9.99';
      promotion.rows=gifts.rows=2;
      const productField=(key,label,input)=>{
        const box=field(label,input),path=fieldPath+'/'+key;
        box.dataset.agentField=path;
        selectable(box.querySelector('label'),()=>onSelect(label,path),{target:{object_ref:prep().ref,field_path:path}});
        return box;
      };
      const priceField=productField('price','本场活动价',price);
      const discountField=productField('discount','单品折扣（八折填8）',discount);
      const shippingField=productField('shipping_time','本场特殊发货时效',shipping);
      function paint(){
        priceField.hidden=mode.value!=='price';discountField.hidden=mode.value!=='discount';
        shippingField.hidden=!shippingOverride.checked;
      }
      function write(value){
        mode.value=value.mode||'none';price.value=value.price??'';discount.value=value.discount==null?'':value.discount*10;
        promotion.value=value.promotion||'';gifts.value=value.gifts||'';shipping.value=value.shipping_time||'';
        exclusive.checked=value.exclusive!==false;shippingOverride.checked=!!value.shipping_override;paint();
      }
      function read(){return {...copy(initial),mode:mode.value,price:mode.value==='price'?Number(price.value):null,
        discount:mode.value==='discount'?Number(discount.value)/10:null,promotion:promotion.value,gifts:gifts.value,
        exclusive:exclusive.checked,shipping_override:shippingOverride.checked,shipping_time:shipping.value};}
      function validate(){
        if(mode.value==='price'&&(!price.value||!price.checkValidity()))throw Error('请填写有效的本场活动价。');
        if(mode.value==='discount'&&(!discount.value||!discount.checkValidity()))throw Error('请填写有效的单品折扣，八折填8。');
      }
      const exclusiveField=productField('exclusive','优先单品优惠，不叠加全场优惠',exclusive);
      const shippingToggle=productField('shipping_override','覆盖本场发货时效',shippingOverride);
      exclusiveField.classList.add('wb-session-product-toggle');shippingToggle.classList.add('wb-session-product-toggle');
      mode.addEventListener('change',paint);shippingOverride.addEventListener('change',paint);
      editor.append(productField('mode','单品优惠方式',mode),priceField,discountField,exclusiveField,
        productField('promotion','单品活动说明',promotion),productField('gifts','本场赠品与加赠条件',gifts),shippingToggle,shippingField);
      write(initial);
      return {editor,read,write,validate,focus:mode};
    }
    async function openSessionProductConfig(productId,opener,location={}){
      if(location.preserveFocus)return;
      const form=content.querySelector('.wb-session-form');
      if(!form?._openProductOverride)throw Error('请先保存本场选品，再配置单品。');
      return form._openProductOverride(productId,opener,location);
    }
    function openSessionProductDialog(node,product,{opener,location,initial,preparation,buffer,commerce,commit}){
      if(!product)throw Error('该商品不在本场已保存的选品中。');
      if(buffer.dirty)throw Error('请先保存或放弃尚未保存的本场设置，再配置单品。');
      if(node.querySelector('dialog[data-session-product-id][open]'))return;
      const id=product.ref.id,pid=preparation.ref.id,fieldPath='/draft/commerce/product_overrides/'+id;
      const original=initial.commerce.product_overrides[id];
      const dialog=region(E('dialog',undefined,'wb-session-product-dialog'),{kind:'session',id:pid,product_id:id});
      dialog.dataset.sessionProductId=id;
      const title=E('h3','单品配置 · '+product.body.name);
      title.id='wb-session-product-'+uid();dialog.setAttribute('aria-labelledby',title.id);
      const head=E('header',undefined,'wb-session-product-head');
      const body=E('div',undefined,'wb-session-product-body');
      const actions=E('div',undefined,'wb-session-product-actions');
      const feedback=E('div',undefined,'wb-inline-alert');feedback.setAttribute('role','status');feedback.hidden=true;
      let working=original;
      function close(restoreFocus=true){
        dialog.close();dialog.remove();
        if(restoreFocus&&opener?.isConnected)opener.focus({preventScroll:true});
      }
      function message(text){feedback.replaceChildren(E('p',text));feedback.hidden=false;}
      function update(value){
        if(equal(working,value))return;
        working=value;commerce.writeOverride(id,value);
        if(equal(value,original)){
          buffer.dirty=false;buffer.host.classList.remove('wb-dirty');persist(buffer);options.onDirty?.(getDirtySummary());
        }else changed(buffer);
        if(!saveButton.dataset.agentInputLocked)saveButton.disabled=!buffer.dirty;
      }
      function dismiss(){
        if(!buffer.dirty){close();return;}
        message('这件商品还有未保存的本场配置。');
        const keep=button('继续编辑',()=>{feedback.hidden=true;editor.focus.focus();},false,{readOnlyAction:'true'});
        const discard=button('放弃修改并关闭',()=>{update(original);close();},false,{readOnlyAction:'true'});
        feedback.append(keep,discard);keep.focus();
      }
      const editor=sessionProductEditor(product,original||{},fieldPath,(label,path)=>{
        if(buffer.dirty){message('请先保存或放弃单品修改，再交给右侧 Agent。');return;}
        close(false);
        options.onSelect?.({label:'仅本场 / '+product.body.name+' / '+label,scope:scope(),preparation_id:pid,
          target:{object_ref:copy(preparation.ref),field_path:path},destination:{view:'session',preparation_id:pid,field_path:path},dirty:false},{focusChat:true});
      });
      const originalValues=editor.read();
      const onInput=()=>{
        const value=editor.read();
        update(equal(value,originalValues)?original:value);
      };
      editor.editor.addEventListener('input',onInput);editor.editor.addEventListener('change',onInput);
      const reset=button('恢复商品默认',()=>{editor.write({});update(undefined);});
      const saveButton=button('保存单品配置',async()=>{
        try{
          editor.validate();
          const draft=copy(initial);
          if(working===undefined)delete draft.commerce.product_overrides[id];
          else draft.commerce.product_overrides[id]=copy(working);
          await commit(draft,()=>close(false));
        }catch(error){
          message(error.message||'保存失败，输入已保留。');
          if(error.unknown)feedback.append(button('核实原操作结果',async()=>{
            const receipt=await verifyUnknownOperation();
            message(receipt?.state==='committed'?'已核实保存成功。再点保存会读取原回执。':'暂未核实结果，请保留当前输入。');
          },false,{readOnlyAction:'true'}));
        }
      },true);
      saveButton.disabled=true;
      head.append(title,button('关闭',dismiss,false,{readOnlyAction:'true'}));
      const price=product.body.price;
      body.append(E('p',price==null?'日常售价未填写。':'商品库日常价 '+price+' '+product.body.currency+'。此处设置仅用于本场。','wb-muted'),feedback,editor.editor);
      actions.append(reset,saveButton);dialog.append(head,body,actions);
      dialog.addEventListener('cancel',event=>{event.preventDefault();if(!busy)guard(dismiss);});
      node.append(dialog);dialog.showModal();applyEditorLocks();
      const target=location?.fieldPath&&[...editor.editor.querySelectorAll('[data-agent-field]')].find(item=>item.dataset.agentField===location.fieldPath);
      if(target&&!target.hidden)markLocation(dialog,target,target.querySelector('input,textarea,select'));
      else if(location?.fieldPath&&location.fieldPath!==fieldPath){
        markLocation(dialog,editor.focus.closest('.wb-field'),editor.focus);
        message('当前配置中没有可编辑的目标字段，已打开此商品的本场配置。');
      }
      else editor.focus.focus({preventScroll:true});
      return dialog;
    }
    async function renderSession(node,{inline=false}={}){
      const pid=prepId(),p=copy(prep()),old=details().draft?.body?.draft,room=await api.query('read_room',{room_id:p.body.room_id});
      const initial=copy(old||roomDefaultDraft(room,p.body.goal,p.body.selected_product_refs));
      initial.commerce=trimCommerceReferences(initial.commerce||{},p.body.selected_product_refs);
      initial.tts_selection||=roomDefaultVoice(room);
      const settings=inline?E('details',undefined,'wb-session-settings'):node;
      if(inline){
        settings.dataset.sessionSettings='';
        const summary=E('summary','本场配置');
        summary.append(E('span','选填 · 主题、口播要求与活动','wb-session-hint'));
        settings.append(summary);
        node.append(settings);
      }else node.append(header('本场配置','配置只属于当前本场，商品日常事实仍由共享商品库维护。',[
        button('选择引用商品',()=>navigate({view:'products',product_id:null,action:null})),
        button('新增话术版本',()=>navigate({view:'scripts',product_id:null,script_id:null,resource_id:null,module_key:null,action:'script_generation'}),false,{readOnlyAction:'true'})
      ]));
      renderRoomMaterialImport(settings,room,p);
      const form=E('form',undefined,'wb-session-form');form.addEventListener('submit',event=>event.preventDefault());
      if(inline)form.append(E('h3','本场配置','wb-sr-only'));
      const grid=E('div',undefined,'wb-grid'),controls={};let buffer;
      for(const [key,label,type] of [['theme','本场主题','text'],['objective','本场目标','textarea'],['scheduled_at','计划时间（带时区 ISO 8601）','text'],['campaign_starts_at','活动开始时间（带时区 ISO 8601）','text'],['campaign_ends_at','活动截止时间（带时区 ISO 8601）','text']]){
        const c=control(type,initial[key],label);c.dataset.field=key;controls[key]=c;if(type==='textarea')c.rows=2;
        const isTime=['scheduled_at','campaign_starts_at','campaign_ends_at'].includes(key);
        if(inline&&isTime){c.placeholder='2026-11-04T20:00:00+08:00';c.title='时间需包含时区';}
        const f=field(inline&&isTime?label.split('（')[0]:label,c),path='/draft/'+key;f.dataset.agentField=path;
        const name=f.querySelector('label');name.dataset.selectField=key;
        selectable(name,()=>selected('仅本场 / '+label,{object_ref:p.ref,field_path:path},{view:'session',preparation_id:pid,field_path:path},buffer),{target:{object_ref:p.ref,field_path:path}});
        grid.append(f);
      }
      form.append(grid);
      const products=[];
      for(const ref of p.body.selected_product_refs)products.push(await api.query('read_product',{ref}));
      const commerce=commerceEditor(initial.commerce,products);
      commerce.node.dataset.agentField='/draft/commerce';form.append(commerce.node);
      const read=()=>{
        const value=copy(initial);
        for(const [key,c] of Object.entries(controls)){
          if(key!=='campaign_starts_at'||c.value||Object.hasOwn(initial,key))value[key]=c.value;
        }
        value.commerce=commerce.read();
        return value;
      };
      const write=value=>{
        for(const [key,c] of Object.entries(controls))c.value=value[key]||'';
        commerce.write(value.commerce||{});
      };
      async function commit(draft,afterSave){
        const result=await command('save_session_draft',{preparation_id:pid,expected_revision:buffer.base_ref.revision,draft});
        clearBuffer(buffer);afterSave?.();replacements.set(p.ref.id,result.data.preparation_ref);changedBusiness(result);
        try{await refresh({force:true});notice('本场配置已保存。受影响的话术和语音需要重新检查。');}
        catch(error){notice('本场配置已保存。请刷新查看最新内容。','warning');}
        return result;
      }
      const save=()=>{
        const draft=read();
        for(const key of ['scheduled_at','campaign_starts_at','campaign_ends_at']){
          if(draft[key]&&(!/T.*(?:Z|[+-]\d\d:\d\d)$/.test(draft[key])||!Number.isFinite(Date.parse(draft[key]))))
            throw Error('时间请填写带时区 ISO 8601，例如 2026-10-16T20:00:00+08:00');
        }
        if(draft.campaign_starts_at&&draft.campaign_ends_at
          &&Date.parse(draft.campaign_starts_at)>=Date.parse(draft.campaign_ends_at))
          throw Error('活动截止时间必须晚于活动开始时间。');
        return commit(draft);
      };
      buffer=registerBuffer('session:'+pid,'本场配置',p.ref,read,write,save,form);
      form.append(button('保存本场配置',save,true));
      settings.append(markResultSection(form,'draft',details().draft?.ref));
      form._openProductOverride=(id,opener,location)=>openSessionProductDialog(node,products.find(product=>product.ref.id===id),{
        opener,location,initial,preparation:p,buffer,commerce,commit
      });
      if(!inline&&products.length){
        const shortcuts=E('div',undefined,'wb-form-actions');
        for(const product of products)shortcuts.append(button('单品配置 '+product.body.name,opener=>form._openProductOverride(product.ref.id,opener)));
        node.append(shortcuts);
      }
    }
    async function renderInsights(node){const pid=prepId();const initialPreparation=copy(prep());node.append(header('顾客、场景与卖点建议','分析建议不等于真实验证；采纳用于内容，也不会变成已验证的客户事实。'));const references=initialPreparation.body.analysis_refs||[];let found=false;for(const ref of references){const record=await api.query('read_analysis_report',{ref});if(record.body.role!=='audience_scenes')continue;found=true;renderInsight(node,record);}if(!found){for(const ref of initialPreparation.body.selected_product_refs){const records=await api.query('find_audience_research',{ref});for(const research of records.items||[]){found=true;node.append(E('p',research.compatibility==='exact'?'可复用的历史建议':'历史建议需结合本次事实复核','wb-badge wb-badge--warning'));renderHistoricalInsight(node,{ref:research.research_ref,body:{role:'audience_scenes',report:research.report,product_refs:research.product_refs,readset:[]}},research.compatibility);}}}if(!found)node.append(empty('尚无顾客与场景建议','可在右侧要求 Agent 根据商品资料分析。也可以先记录你已知的顾客与使用场景。'));
      const manual=region(section('补充人工顾客与场景建议'),{kind:'insights',id:pid});const summary=control('textarea','','人工建议');manual.append(field('你的顾客与场景判断',summary),button('保存为待验证建议',async()=>{if(!summary.value.trim())throw Error('请填写建议');const report={summary:summary.value,product_ids:initialPreparation.body.selected_product_refs.map(r=>r.id),fields:{},insights:[],evidence:[],hypotheses:['商家输入，待真实顾客验证'],conflicts:[],missing_fields:[],asset_suggestions:[],source_refs:[],dependencies:initialPreparation.body.selected_product_refs};const r=await command('accept_analysis_report',{role:'audience_scenes',report,report_hash:await api.hash(report),preparation_id:pid,expected_revision:initialPreparation.ref.revision,product_refs:initialPreparation.body.selected_product_refs,readset:[]});changedBusiness(r);await refresh({force:true});},true));node.append(manual);if(details().asset_checklist)renderAssets(node,details().asset_checklist);}
    function renderHistoricalInsight(node,record,compatibility){
      const pid=prepId(), initialPreparation=copy(prep());
      const box=region(section('历史顾客与场景建议 · 先复核'),{kind:'insights',id:pid});
      box.dataset.historicalAnalysis=record.ref.id;
      box.append(E('span','历史建议 · 尚未用于本场 · 尚未真实验证','wb-badge wb-badge--warning'),E('p',record.body.report.summary),jsonDetails('查看历史观点、依据与原始依赖',record.body.report),E('p',compatibility==='exact'?'商品修订一致，仍需检查本场适用性。':'商品资料或适用背景有差异，请复核后再决定是否用于本场。','wb-muted'));
      const reviewed=E('input');reviewed.type='checkbox';reviewed.setAttribute('aria-label','已复核历史建议对本场的适用性');
      const acknowledgement=E('label');acknowledgement.append(reviewed,E('span','我已复核商品、顾客场景与本场适用性；保存后仍属于待验证建议。'));box.append(acknowledgement);
      box.append(button('复核后保存为本场建议',async()=>{
        if(!reviewed.checked)throw Error('请先复核历史观点及本场适用性，再勾选确认。');
        const ids=[...new Set([...(record.body.report.product_ids||[]),...(record.body.product_refs||[]).map(r=>r.id)])];
        const selectedProducts=new Map(initialPreparation.body.selected_product_refs.map(r=>[r.id,r]));
        if(!ids.length||ids.some(id=>!selectedProducts.has(id)))throw Error('这份历史报告含有本场未选择的商品；请先明确本场商品范围，不能自动扩大或删减报告范围。');
        const currentProductRefs=ids.map(id=>selectedProducts.get(id));
        const report=copy(record.body.report);
        report.fields={...report.fields,historical_origin:{analysis_ref:record.ref,original_product_refs:record.body.product_refs||[],original_dependencies:report.dependencies||[],original_source_refs:report.source_refs||[],reviewed_for_preparation_id:pid,applicability:'merchant_reviewed_hypothesis_not_customer_verified'}};
        report.product_ids=ids;report.dependencies=currentProductRefs;report.source_refs=[];
        report.hypotheses=[...(report.hypotheses||[]),'历史分析经商家明确复核用于本场，尚未完成真实顾客验证'];
        const r=await command('accept_analysis_report',{role:'audience_scenes',report,report_hash:await api.hash(report),preparation_id:pid,expected_revision:initialPreparation.ref.revision,product_refs:currentProductRefs,readset:currentProductRefs});
        changedBusiness(r);await refresh({force:true});notice('历史建议已由你明确保存为本场待验证建议。现在可指定该本场建议让 Agent 修改。');
      },true));
      node.append(box);
    }
    function renderInsight(node,record){
      const pid=prepId(),initialPreparation=copy(prep()),box=section('顾客与使用场景');
      if(record.body.accepted===true&&record.body.preparation_id===pid){
        box.dataset.analysisId=record.ref.id;
        markResultSection(box,'analysis',record.ref);
      }
      box.append(E('span','分析建议 · 尚未真实验证','wb-badge wb-badge--warning'));const initial=copy(record.body.report);const summary=control('textarea',initial.summary,'分析摘要');const hypotheses=control('textarea',(initial.hypotheses||[]).join('\n'),'待验证假设');box.append(field('分析摘要',summary),field('待验证假设（每行一条）',hypotheses));const insightFields=suggestionEditor(initial.fields||{},'顾客与场景分析内容');const insightRows=suggestionEditor(initial.insights||[],'具体观点');box.append(insightFields.node,insightRows.node);box.append(jsonDetails('查看依据',initial.evidence||[]),jsonDetails('完整建议与依赖',initial));let buffer;const read=()=>({...copy(initial),summary:summary.value,hypotheses:hypotheses.value.split('\n').filter(Boolean),fields:insightFields.read(),insights:insightRows.read()});const save=async()=>{const report=read();const r=await command('accept_analysis_report',{role:'audience_scenes',report,report_hash:await api.hash(report),preparation_id:pid,expected_revision:buffer.base_ref.revision,product_refs:record.body.product_refs||[],readset:record.body.readset||[]});clearBuffer(buffer);if(r.data.analysis_ref)replacements.set(record.ref.id,r.data.analysis_ref);changedBusiness(r);await refresh({force:true});notice('已保存商家修订的建议，仍标记为待验证。');};buffer=registerBuffer('insight:'+record.ref.id,'顾客与场景建议',initialPreparation.ref,read,v=>{summary.value=v.summary;hypotheses.value=(v.hypotheses||[]).join('\n');insightFields.write(v.fields);insightRows.write(v.insights);},save,box);box.append(button('保存修订建议',save,true));selectable(box.querySelector(':scope > h3'),()=>selected('本场 / 顾客与使用场景建议',{object_ref:record.ref},{view:'insights',preparation_id:pid},buffer),{target:{object_ref:record.ref}});node.append(box);
    }
    function renderAssets(node,record){
      const pid=prepId(), initialPreparation=copy(prep());
      const checklist=record.body.checklist||record.body;
      const box=section('展示材料准备清单');
      box.append(E('p','可在这里查看视频拍摄、商品展示与直播间布置建议，也可绑定图片或文档。准备状态不会影响声音播出。','wb-muted'));
      if(checklist.notes?.length){
        const notes=E('ul');for(const note of checklist.notes)notes.append(E('li',note));box.append(notes);
      }
      const rows=[];
      for(const original of checklist.items||[]){
        const item=copy(original), row=section(item.purpose);const entry={item,state:null};
        row.append(E('p',item.recommended_content));
        const state=select(item.status,[['missing','待准备'],['available','已有材料'],['not_needed','本场不需要']],'材料状态 '+item.purpose);
        const upload=E('input');upload.type='file';upload.accept='.txt,.md,.csv,.json,.pdf,.doc,.docx,.rtf,.xlsx,.png,.jpg,.jpeg,.webp';upload.setAttribute('aria-label','材料文件 '+item.purpose);
        const fileStatus=E('p',item.blob_ref?'已绑定资料文件':'尚未绑定资料文件','wb-muted');
        row.append(field('准备状态',state),upload,button('上传并绑定此材料',async()=>{entry.item.blob_ref=await importFile(upload.files?.[0]);state.value='available';fileStatus.textContent='已上传文件，保存清单后生效';changed(buffer);}),fileStatus);
        entry.state=state;rows.push(entry);box.append(row);
      }
      let buffer;
      const read=()=>({...copy(checklist),items:rows.map(r=>({...r.item,status:r.state.value}))});
      const write=value=>{for(let i=0;i<rows.length;i++){const item=value.items?.find(v=>v.item_id===rows[i].item.item_id);if(item){rows[i].item=copy(item);rows[i].state.value=item.status;}}};
      const save=async()=>{const r=await command('save_asset_checklist',{preparation_id:pid,expected_revision:buffer.base_ref.revision,checklist:read()});clearBuffer(buffer);changedBusiness(r);await refresh({force:true});notice('展示材料状态已保存。');};
      buffer=registerBuffer('assets:'+pid,'展示材料准备清单',initialPreparation.ref,read,write,save,box);
      if(rows.length)box.append(button('保存材料准备状态',save,true));else box.append(E('p','暂无材料建议，可请 Agent 分析本商品需要哪些演示。'));
      node.append(markResultSection(box,'asset_checklist',record.ref));
    }

    async function renderScriptAudioPreviews(host,script){
      const requested=script.ref.id;
      try{
        const result=await api.query('list_script_audio_previews',{preparation_id:prepId(),ref:script.ref,limit:20});
        if(!host.isConnected||scriptReader?.dataset.scriptId!==requested)return;
        for(const item of result.items||[]){
          const url=safeUrl(item.url);if(!url||!item.playable)continue;
          const card=E('article',undefined,'wb-script-module-card wb-script-audio-preview');
          card.append(E('h4',(item.resolved_style?.profile_id==='continuous_host'?'新版统一主播 · 完整连续音频':'连续语音试听')+' · '+(item.module_keys||[]).map(key=>MODULES[key]||key).join('＋')));
          const voice=item.tts_selection||{},style=item.resolved_style||item.style||{};
          card.append(E('p',(voice.provider||item.provider||'')+' / '+(voice.model||item.model||'')+' · '+(voice.voice||item.voice||'')+' · '+Math.floor(item.duration/60)+'分'+Math.round(item.duration%60)+'秒','wb-muted'));
          const audio=routedAudio(E('audio'));audio.controls=true;audio.preload='metadata';audio.src=url;audio.style.width='100%';audio.setAttribute('aria-label','连续试听 '+(item.module_keys||[]).map(key=>MODULES[key]||key).join('和'));card.append(audio);
          const spokenKeys=Object.keys(script.modules||{}).filter(key=>key!=='opening_short'&&key!=='transition'&&(script.modules[key]?.sentences||[]).length),isWhole=spokenKeys.length>0&&spokenKeys.every(key=>(item.module_keys||[]).includes(key));
          card.append(E('p',isWhole?'本版全部有效模块已包含在这段连续试听中。试听不会启动直播。':'这段音频按模块顺序一次合成。试听不会启动直播；其他模块未包含在这段试听中。','wb-muted'),jsonDetails('查看实际音色与表达参数',{voice,style,text_characters:item.text_characters,usage_characters:item.usage_characters,cache_hit:item.cache_hit,ordered_text_hash:item.ordered_text_hash,preview_approval_ref:item.preview_approval_ref}));
          host.append(card);
        }
      }catch(error){if(host.isConnected&&scriptReader?.dataset.scriptId===requested)host.append(E('p','暂时无法读取已生成试听：'+error.message,'wb-muted'));}
    }
    function showScriptReader(script,origin,position,{preserveFocus=false}={}){
      const pid=prepId(),scripts=currentScriptVariants(details().scripts||[]),backScrollTop=scriptReader?scriptReader._backScrollTop:container.scrollTop;if(!closeScriptReader({restoreFocus:false}))return;
      const modal=options.roomCanvas&&current.view==='room-config';
      const reader=E(modal?'dialog':'section',undefined,modal?'wb-script-reader wb-room-script-dialog':'wb-script-reader');reader.setAttribute('role','dialog');reader.setAttribute('aria-modal',String(!!modal));reader.setAttribute('aria-label','话术版本详情');reader.tabIndex=-1;reader._origin=origin;reader._backScrollTop=backScrollTop;scriptReader=reader;
      // The reader uses the complete result panel; its underlying toolbar and
      // tabs return unchanged on close, while the Agent pane stays available.
      reader._panel=modal?null:container.closest('.wb-work-panel');reader._underlay=[];
      if(reader._panel){
        reader._panel.classList.add('wb-script-reader-open');
        // The editor can live inside the room configuration section. Keep its ancestors active.
        for(let surface=container;surface&&surface!==reader._panel;surface=surface.parentElement){
          for(const node of surface.parentElement.children){
            if(node===surface)continue;
            reader._underlay.push({node,inert:node.inert,ariaHidden:node.getAttribute('aria-hidden')});
            node.inert=true;
            node.setAttribute('aria-hidden','true');
          }
        }
      }
      const productName=id=>snapshot.products.items.find(product=>product.ref.id===id)?.name||id;
      function paint(active,activePosition){
        if([...buffers.values()].some(b=>b.dirty&&reader.contains(b.host))){readerNotice('请先保存或取消正在编辑的句子，再切换版本。','warning');return;}
        const focusKey=reader.contains(document.activeElement)?document.activeElement.dataset.readerFocus:null;if(activePosition===undefined){const savedPosition=(details().playlist?.body.items||[]).findIndex(item=>item.script_revision_ref.id===active.ref.id);if(savedPosition>=0)activePosition=savedPosition+1;}
        reader.replaceChildren();reader.dataset.scriptId=active.ref.id;const index=scripts.findIndex(value=>value.ref.id===active.ref.id),metrics=scriptTextMetrics(active,activePosition);
        const top=E('div',undefined,'wb-script-reader-heading'),identity=E('div');identity.append(E('span','完整话术 · 支持逐句编辑','wb-eyebrow'),E('h3',productName(active.body.product_id)+' · 第'+active.body.version_number+'版'),E('p',metrics.characters+' 字 · 按240字/分估算约 '+metrics.estimated_minutes+' 分钟','wb-muted'));
        top.append(identity,button('关闭话术详情',()=>closeScriptReader(),false,{closeScriptReader:'',readOnlyAction:'true'}));reader.append(top);
        const versions=E('nav',undefined,'wb-script-version-switch');versions.setAttribute('aria-label','切换话术版本');
        for(const value of scripts){const chip=button(productName(value.body.product_id)+' · 第'+value.body.version_number+'版',()=>paint(value),false,{readOnlyAction:'true'});chip.dataset.switchScript=value.ref.id;chip.dataset.readerFocus='version:'+value.ref.id;chip.setAttribute('aria-current',String(value.ref.id===active.ref.id));versions.append(chip);}reader.append(versions);
        const navigation=E('div',undefined,'wb-script-reader-navigation'),previous=button('上一版本',()=>paint(scripts[index-1]),false,{readerFocus:'previous',readOnlyAction:'true'}),next=button('下一版本',()=>paint(scripts[index+1]),false,{readerFocus:'next',readOnlyAction:'true'});previous.disabled=index<=0;next.disabled=index<0||index>=scripts.length-1;navigation.append(previous,E('span','第 '+(index+1)+' / '+scripts.length+' 版','wb-muted'),next);reader.append(navigation);
        const body=E('div',undefined,'wb-script-reader-body');body.tabIndex=0;
        const previews=E('div',undefined,'wb-script-audio-previews');body.append(previews);
        body.append(E('p','点击每句话的「编辑」即可手动修改并保存本句；也可点模块标题交给右侧 Agent。为避免丢稿，请逐句保存或取消后再编辑下一句；模块顺序保持不变。','wb-script-reader-help'));
        if(metrics.estimated_minutes<8||metrics.estimated_minutes>10)body.append(E('p','当前保存稿估算约 '+metrics.estimated_minutes+' 分钟，与本次8–10分钟目标有差异；生成语音后的实测时长为准。','wb-inline-alert'));
        if(metrics.requested_opening&&metrics.opening!==metrics.requested_opening)body.append(E('p','这个播放位置需要'+MODULES[metrics.requested_opening]+'；当前展示已保存的'+(MODULES[metrics.opening]||'正文')+'。保存编排时会检查缺少的开场，正文不受影响。','wb-muted'));
        let ordinal=0;for(const key of metrics.keys){const module=active.modules[key];if(!(module.sentences||[]).length)continue;
          const part=region(E('article',undefined,'wb-script-module-card'),{kind:'scripts',id:pid,script_id:active.ref.id,product_id:active.body.product_id,module_key:key});part.dataset.module=key;
          const title=E('h4',String(++ordinal).padStart(2,'0')+' · '+(MODULES[key]||key)+(key==='transition'?'（历史保留，不参与播出）':''));title.dataset.selectModule=key;
          selectable(title,async()=>selected(productName(active.body.product_id)+' / 第'+active.body.version_number+'版 / '+(MODULES[key]||key),{object_ref:active.ref,product_ref:active.body.product_ref,module_key:key,selected_text_hash:await api.hash(module.sentences)},{view:'scripts',preparation_id:pid,resource_id:active.ref.id,module_key:key}),{target:{object_ref:active.ref,module_key:key}});
          part.append(title);for(const [sentenceIndex,sentence] of (module.sentences||[]).entries()){
            const row=E('div',undefined,'wb-script-sentence');row.dataset.sentenceId=sentence.id;
            const paragraph=E('p',sentence.spoken_text||sentence.text||'','wb-script-paragraph'),editBox=E('div',undefined,'wb-script-sentence-edit');editBox.hidden=true;
            const input=control('textarea',sentence.spoken_text||sentence.text||'',(MODULES[key]||key)+'第'+(sentenceIndex+1)+'句');input.maxLength=2500;input.rows=3;input.dataset.sentenceEdit=sentence.id;
            const saveState=E('span','未修改','wb-script-sentence-status');saveState.setAttribute('role','status');let buffer=null;
            const endEdit=()=>{if(buffer){clearBuffer(buffer);buffer=null;}editBox.hidden=true;paragraph.hidden=false;edit.hidden=false;saveState.textContent='已取消修改';};
            const saveSentence=async()=>{
              const text=input.value.trim();if(!text)throw Error('本句不能为空；请填写后保存，或取消修改。');
              if(text===(sentence.spoken_text||sentence.text||'')){endEdit();readerNotice('本句没有变化，未提交保存。');return;}
              saveState.textContent='正在保存…';editBox.inert=true;
              let receipt;try{receipt=await command('patch_script',{preparation_id:pid,expected_revision:prep().ref.revision,script_ref:buffer.base_ref,module_key:key,sentence_ids:[sentence.id],selected_text_hash:await api.hash([sentence]),sentences:[text]});}
              catch(error){editBox.inert=false;saveState.textContent=error.unknown?'结果待核实，输入已保留':'保存未完成，输入已保留';readerNotice(error.message||'保存未完成；输入已保留，请核对最新版本。','warning');throw error;}
              const nextRef=receipt.data?.script_ref;if(!nextRef)throw Error('服务缺少保存回执，请先核实原操作，勿重复提交。');
              replacements.set(active.ref.id,nextRef);clearBuffer(buffer);buffer=null;changedBusiness(receipt);closeScriptReader({restoreFocus:false});cache.clear();
              try{await refresh({force:true});const fresh=details().scripts?.find(item=>item.ref.id===nextRef.id)||await api.query('read_script',{ref:nextRef});const nextOrigin=[...content.querySelectorAll('[data-script-id]')].find(item=>item.dataset.scriptId===nextRef.id)||container;showScriptReader(fresh,nextOrigin,activePosition);readerNotice('本句已保存为新修订；其他句子保持不变。修改后的语音、整包批准与播出候选需重新核对。');const saved=scriptReader?.querySelector('[data-module="'+key+'"]');saved?.scrollIntoView({block:'nearest'});}
              catch(error){notice('本句已保存，但最新稿暂未读取完成。请刷新查看最新成果，勿重复提交。','warning',{script_ref:nextRef});}
              return receipt;
            };
            const edit=button('编辑',()=>{
              if([...buffers.values()].some(b=>b.dirty&&reader.contains(b.host))){readerNotice('请先保存或取消正在编辑的句子，再编辑下一句。','warning');return;}
              if(reader._activeSentenceEditor&&reader._activeSentenceEditor!==endEdit)reader._activeSentenceEditor();
              buffer=registerBuffer('sentence:'+active.ref.id+':'+key+':'+sentence.id,(MODULES[key]||key)+'第'+(sentenceIndex+1)+'句',active.ref,()=>input.value,value=>{input.value=value??'';},saveSentence,row);
              input.value=sentence.spoken_text||sentence.text||'';reader._activeSentenceEditor=endEdit;paragraph.hidden=true;edit.hidden=true;editBox.hidden=false;saveState.textContent='编辑中，尚未保存';applyEditorLocks();input.focus();
            },false,{editSentence:sentence.id});
            const actions=E('div',undefined,'wb-script-sentence-actions');actions.append(button('保存本句',()=>buffer.save(),true,{saveSentence:sentence.id}),button('取消',endEdit,false,{cancelSentence:sentence.id}),saveState);input.addEventListener('input',()=>{saveState.textContent='有修改，尚未保存';});
            editBox.append(input,actions);row.append(paragraph,edit,editBox);part.append(row);
          }body.append(part);
        }
        if(!ordinal)body.append(empty('这版暂无模块正文','可在右侧让 Agent 重新检查这份稿件。'));
        reader.append(body,E('p','估时只用于文字核对；不代表已生成语音或已批准播出。','wb-script-reader-footer'));renderScriptAudioPreviews(previews,active);applyEditorLocks();if(focusKey){const nextFocus=[...reader.querySelectorAll('[data-reader-focus]')].find(element=>element.dataset.readerFocus===focusKey&&!element.disabled)||reader.querySelector('[data-close-script-reader]');nextFocus.focus({preventScroll:true});}container.scrollTop=0;
      }
      if(!modal){content.inert=true;content.setAttribute('aria-hidden','true');container.classList.add('wb-has-script-reader');container.scrollTop=0;}
      container.append(reader);paint(script,position);
      if(modal)reader.showModal();
      if(modal||!preserveFocus)reader.querySelector('[data-close-script-reader]').focus({preventScroll:true});
    }
    function renderScriptGeneration(){
      const pid=prepId(),initialPreparation=copy(prep()),scripts=currentScriptVariants(details().scripts||[]),generate=region(section('新增话术版本'),{kind:'scripts',id:pid});generate.dataset.scriptGeneration='';
      generate.append(E('p','按已保存的本场设置生成，每件可选 1–6 版。','wb-muted'));
      const roomVersionCount=snapshot.rooms?.find(room=>room.ref.id===initialPreparation.body.room_id)?.body.version_count||2;
      const rows=[];for(const ref of initialPreparation.body.selected_product_refs){const product=snapshot.products.items.find(value=>value.ref.id===ref.id),name=product?.name||ref.id,existing=scripts.filter(value=>value.body.product_id===ref.id),row=E('div',undefined,'wb-generation-product');
        const chosen=E('input');chosen.type='checkbox';chosen.checked=true;chosen.setAttribute('aria-label','为此商品新增话术 '+name);const quantity=control('number',details().draft?.body?.draft?.requested_variant_counts?.[ref.id]||roomVersionCount,'本次新增版本数 '+name);quantity.min='1';quantity.max='6';quantity.step='1';
        const label=E('label'),description=E('div');description.append(E('strong',name),E('p','已有 '+existing.length+' 版','wb-muted'));label.append(chosen,description);row.append(label,field('新增版数',quantity));generate.append(row);rows.push({ref,name,chosen,quantity,existing});
      }
      if(!rows.length)generate.append(E('p','请先选择这场直播要卖的商品。','wb-muted'),button('选择本场商品',()=>navigate({view:'products',product_id:null,action:null}),false,{readOnlyAction:'true'}));
      else if(!initialPreparation.body.draft_ref)generate.append(E('p','保存本场配置后即可提交新增要求。','wb-muted'),button('查看本场配置',()=>navigate({view:'session',product_id:null,action:null}),false,{readOnlyAction:'true'}));
      const send=button('将新增要求交给 Agent',async()=>{
        const chosen=rows.filter(row=>row.chosen.checked);if(!chosen.length)throw Error('请至少选择一个商品');if(!initialPreparation.body.draft_ref)throw Error('请先保存本场配置');if(hasDirty())throw Error('请先保存或放弃未保存的配置，再生成话术');
        for(const row of chosen)if(!Number.isInteger(Number(row.quantity.value))||Number(row.quantity.value)<1||Number(row.quantity.value)>6)throw Error('每件商品每次可新增1至6版');
        const prepareGeneration=options.onPrepareGeneration||options.onPrepareMessage;if(!prepareGeneration)throw Error('右侧 Agent 对话尚未连接，请刷新后重试');
        const request={preparation_id:pid,draft_ref:copy(initialPreparation.body.draft_ref),product_counts:chosen.map(row=>({product_ref:row.ref,count:Number(row.quantity.value),existing_script_refs:row.existing.map(value=>value.ref)})),target_duration_seconds:{min:480,max:600},prior_script_refs:copy(initialPreparation.body.script_refs||[])};
        const lines=chosen.map(row=>'- '+row.name+'：新增 '+Number(row.quantity.value)+' 版；本场已有 '+row.existing.length+' 版。');
        await prepareGeneration('请为当前本场新增完整话术版本：\n'+lines.join('\n')+'\n请先读取并对照已有版本，保留真实商品事实，在切入角度、顾客场景和表达上形成差异，不复制已有稿。每一个版本都要单独完整生成，普通话播报目标8–10分钟，按240有效口播字/分约1920–2400字；整版一次生成，再按模块存储。只生成文字。新稿保存后会进入本场播放顺序末尾，请勿手动调整已有顺序，也勿生成语音、批准或播出。按本场实际配置处理活动，没有活动不要编造。\n当前明确范围与已有稿引用：\n'+JSON.stringify(request,null,2),{kind:'script_generation',focus:true,preparation_id:pid,room_id:initialPreparation.body.room_id,
          script_generation:{preparation_id:pid,draft_ref:request.draft_ref,
            product_counts:request.product_counts.map(({product_ref,count})=>({product_ref,count})),
            target_duration_seconds:request.target_duration_seconds},
          script_generation_label:chosen.map(row=>row.name+' · '+Number(row.quantity.value)+' 版').join('、')});
        notice('检查右侧的商品和版数，可以补充写作要求，再发送给 Agent。');
      },true);send.disabled=!rows.length||!initialPreparation.body.draft_ref;generate.append(send);return generate;
    }
    async function renderScripts(node){return renderPlaylist(node);}
    async function renderPlaylist(node){
      const pid=prepId(),initialPreparation=copy(prep());
      const records=details().scripts||[];
      const scripts=currentScriptVariants(records);
      const scriptsById=new Map(records.map(script=>[script.ref.id,script]));
      const selected=new Map((initialPreparation.body.selected_product_refs||[]).map(ref=>[ref.id,ref]));
      const eligible=script=>{
        const product=script&&selected.get(script.body.product_id), pinned=script?.body.product_ref;
        return !!product&&(!pinned||product.revision===pinned.revision&&product.content_hash===pinned.content_hash);
      };
      const inline=node.dataset.inlineRoomSection==='scripts';
      const generator=renderScriptGeneration();
      generator.id='wb-script-generation-'+uid();
      generator.hidden=!inline;
      const showGenerator=button('新增话术版本',()=>{
        if(generator.hidden)node._showScriptGeneration(true);
        else{generator.hidden=true;showGenerator.setAttribute('aria-expanded','false');}
      },scripts.length===0,{readOnlyAction:'true'});
      showGenerator.setAttribute('aria-controls',generator.id);
      showGenerator.setAttribute('aria-expanded','false');
      node._showScriptGeneration=focus=>{
        generator.hidden=false;
        showGenerator.setAttribute('aria-expanded','true');
        const heading=generator.querySelector('h3');
        markLocation(node,heading,focus&&heading);
      };

      const saved=details().playlist;
      const savedItems=saved?.body.items||[];
      const scriptFor=item=>scriptsById.get(item.script_revision_ref.id);
      const activeItem=item=>{
        const script=scriptFor(item);
        return eligible(script)&&item.product_id===script.body.product_id&&
          item.script_revision_ref.revision===script.ref.revision&&item.script_revision_ref.content_hash===script.ref.content_hash;
      };
      const newOccurrence=script=>({occurrence_id:'occ_'+uid(),product_id:script.body.product_id,script_revision_ref:script.ref});
      let items=copy(savedItems).filter(activeItem);
      const queued=new Set(items.map(item=>item.script_revision_ref.id));
      items.push(...scripts.filter(script=>eligible(script)&&!queued.has(script.ref.id)).map(newOccurrence));
      const inactive=scripts.filter(script=>!eligible(script));
      let preview=null,buffer,discardOrder;
      const order=region(section('本场话术版本'),{kind:'playlist',id:pid});
      order.classList.add('wb-script-queue');
      const list=E('div',undefined,'wb-playlist');
      const summary=E('p',undefined,'wb-version-summary');
      const playlistLocked=()=>matchingLocks(editorLocks,{kind:'playlist',id:pid}).length>0;
      const read=()=>({items:copy(items),loop_mode:'infinite',preview:copy(preview)});
      const write=value=>{items=copy(value.items||[]);preview=copy(value.preview);paint();queueMicrotask(syncOrderActions);};
      const productName=id=>snapshot.products.items.find(product=>product.ref.id===id)?.name||id;
      const scriptName=script=>productName(script.body.product_id)+' · 第'+script.body.version_number+'版';
      function dirty(){preview=null;changed(buffer);}
      function syncOrderActions(){if(discardOrder)discardOrder.hidden=!buffer?.dirty;}
      function move(from,to){
        if(to<0||to>=items.length||from===to)return;
        items.splice(to,0,items.splice(from,1)[0]);
        dirty();
        paint();
      }
      const confirm=E('section',undefined,'wb-script-delete-confirm');
      confirm.hidden=true;
      confirm.setAttribute('aria-label','删除话术版本确认');
      function askDelete(script,origin){
        if(buffer.dirty)throw Error('请先保存或放弃尚未保存的播放顺序，再删除版本。');
        region(confirm,{kind:'scripts',id:pid,script_id:script.ref.id,product_id:script.body.product_id});
        const cancel=button('取消',()=>{confirm.hidden=true;origin.focus({preventScroll:true});},false,{readOnlyAction:'true'});
        const remove=button('确认删除',async()=>{
          if(buffer.dirty)throw Error('请先保存或放弃尚未保存的播放顺序，再删除版本。');
          const result=await humanMutation('/api/prelive/v2/human/script-delete',{
            preparation_id:pid,expected_revision:initialPreparation.ref.revision,script_ref:script.ref
          });
          confirm.hidden=true;
          changedBusiness({data:result});
          try{
            await refresh({force:true});
            notice('话术版本已从本场删除。请重新检查播放顺序及播出内容。');
          }catch(error){notice('话术版本已删除。请刷新查看最新播放顺序。','warning');}
        });
        remove.classList.add('wb-script-delete-button');
        const actions=E('div',undefined,'wb-form-actions');
        actions.append(cancel,remove);
        confirm.replaceChildren(E('p','删除「'+scriptName(script)+'」将移除它在本场的所有播放位置。此前采用的内容需要重新检查。'),actions);
        confirm.hidden=false;
        confirm.scrollIntoView({block:'nearest'});
        cancel.focus({preventScroll:true});
      }
      function rowFor(script,item,index,repeats){
        const ordered=!!item;
        const position=ordered?index+1:undefined;
        const row=E('article',undefined,'wb-playlist-item wb-version-card');
        row.dataset.scriptId=script.ref.id;
        row.dataset.readOnlyAction='true';
        if(ordered)row.dataset.occurrence=item.occurrence_id;
        row.draggable=ordered;
        row.tabIndex=0;
        row.setAttribute('aria-label',scriptName(script)+(ordered?'，播放位置'+position:'，需核对本场选品'));
        const ordinal=E('span',ordered?String(position).padStart(2,'0'):'—','wb-queue-position');
        ordinal.setAttribute('aria-hidden','true');
        const info=E('div',undefined,'wb-version-card-info');
        const badges=E('div',undefined,'wb-version-card-badges');
        const metrics=scriptTextMetrics(script,position);
        badges.append(E('span',ordered?(index%5===0?'长开场':'短引入'):'需核对本场选品','wb-badge'),
          E('span',metrics.characters+' 字 · 约 '+metrics.estimated_minutes+' 分钟','wb-version-metrics'));
        info.append(E('strong',scriptName(script)),badges);

        const openReader=(origin,{moduleKey,preserveFocus}={})=>{
          showScriptReader(script,origin,position,{preserveFocus});
          const module=moduleKey&&[...(scriptReader?.querySelectorAll('[data-module]')||[])].find(value=>value.dataset.module===moduleKey);
          if(module)markLocation(scriptReader,module);
        };
        const view=button(inline?'查看全文':'查看完整话术',()=>openReader(view),false,{readOnlyAction:'true',openScript:script.ref.id});
        if(inline)view.setAttribute('aria-label','查看完整话术');
        const actions=E('div',undefined,'wb-version-card-actions');
        actions.append(view);
        if(ordered){
          const up=button('上移',()=>move(index,index-1));
          const down=button('下移',()=>move(index,index+1));
          up.disabled=index===0;
          down.disabled=index===items.length-1;
          const duplicate=button(inline?'重复':'重复此播放项',()=>{
            items.splice(index+1,0,newOccurrence(script));
            dirty();paint();
          });
          if(inline)duplicate.setAttribute('aria-label','重复此播放项');
          actions.append(up,down,duplicate);
          if(repeats>1)actions.append(button('移除此重复项',()=>{
            items.splice(index,1);
            dirty();paint();
          }));
          row.addEventListener('dragstart',event=>{
            if(playlistLocked()){event.preventDefault();return;}
            event.dataTransfer.setData('text/plain',item.occurrence_id);
            event.dataTransfer.effectAllowed='move';
          });
          row.addEventListener('dragover',event=>{if(!playlistLocked())event.preventDefault();});
          row.addEventListener('drop',event=>{
            event.preventDefault();
            if(playlistLocked())return;
            const from=items.findIndex(value=>value.occurrence_id===event.dataTransfer.getData('text/plain'));
            if(from>=0)move(from,index);
          });
        }
        const deletion=region(E('span'),{kind:'scripts',id:pid,script_id:script.ref.id,product_id:script.body.product_id});
        const remove=button('删除版本',origin=>askDelete(script,origin),false,{deleteScript:script.ref.id});
        remove.classList.add('wb-script-delete-button');
        deletion.append(remove);
        actions.append(deletion);
        row.append(ordinal,info,actions);
        row._openScriptReader=(moduleKey,preserveFocus)=>openReader(row,{moduleKey,preserveFocus});
        row.addEventListener('click',event=>{if(!event.target.closest('button'))openReader(row);});
        row.addEventListener('keydown',event=>{
          if(event.target===row&&['Enter',' '].includes(event.key)){event.preventDefault();openReader(row);}
        });
        return row;
      }
      let updateAudioOrder;
      function paint(){
        confirm.hidden=true;
        syncOrderActions();
        list.replaceChildren();
        const validItems=items.filter(activeItem);
        summary.textContent='共 '+scripts.length+' 个版本 · '+validItems.length+' 个播放项'+
          (equal(items,savedItems)?'':' · 顺序待保存');
        const repetitions=new Map();
        for(const item of validItems)repetitions.set(item.script_revision_ref.id,(repetitions.get(item.script_revision_ref.id)||0)+1);
        items.forEach((item,index)=>{
          if(activeItem(item))list.append(rowFor(scriptFor(item),item,index,repetitions.get(item.script_revision_ref.id)));
        });
        for(const script of inactive)list.append(rowFor(script,null,undefined,0));
        if(!scripts.length)list.append(empty('尚无本场话术','点「新增话术版本」，选择商品和数量，再交给右侧 Agent 生成。'));
        updateAudioOrder?.();
      }
      async function reorder(mode){
        if(!items.length||!items.every(activeItem))throw Error('请先核对本场选品和话术版本，再调整播放顺序。');
        const productOrder=[...new Set([...(saved?.body.product_group_order||[]),...items.map(item=>item.product_id)])];
        if(saved&&!buffer.dirty&&equal(items,savedItems)){
          const spec={mode,product_group_order:productOrder,within_product:'keep',seed:uid().replace(/-/g,''),algorithm_version:'reorder-v1'};
          const result=await api.query('preview_playlist_reorder',{preparation_id:pid,ref:saved.ref,spec});
          items=copy(result.proposed_items);
          preview={spec:result.resolved_spec,hash:result.preview_hash};
        }else{
          const groups=productOrder.map(id=>items.filter(item=>item.product_id===id));
          if(mode==='grouped')items=groups.flat();
          else if(mode==='interleave'){
            items=[];
            for(let i=0;groups.some(group=>i<group.length);i++)groups.forEach(group=>{if(group[i])items.push(group[i]);});
          }else for(let i=items.length-1;i>0;i--){
            const j=crypto.getRandomValues(new Uint32Array(1))[0]%(i+1);
            [items[i],items[j]]=[items[j],items[i]];
          }
          preview=null;
        }
        changed(buffer);paint();notice('已预览整版顺序，保存后才会生效。');
      }
      const save=async()=>{
        if(!items.length)throw Error('需要至少一版可用话术。');
        if(!items.every(activeItem))throw Error('播放顺序含有已变化的版本。请核对最新本场并重新排序。');
        const result=await command('save_playlist',{
          preparation_id:pid,expected_revision:buffer.base_ref.revision,items,loop_mode:'infinite',
          product_group_order:preview?.spec.product_group_order||[...new Set(items.map(item=>item.product_id))],
          reorder_spec:preview?.spec||null,preview_hash:preview?.hash||null
        });
        let compiled;
        try{
          compiled=await command('compile_playback_plan',{preparation_id:pid,expected_revision:result.data.preparation_ref.revision,playlist_ref:result.data.playlist_ref});
        }catch(error){
          clearBuffer(buffer);changedBusiness(result);await refresh({force:true});
          notice('顺序已保存，编排检查未完成。'+error.message+' 请让右侧 Agent 按新位置补齐缺失开场。');
          return;
        }
        clearBuffer(buffer);changedBusiness(compiled);await refresh({force:true});
        notice('顺序已保存。第1、6、11…项使用长开场，其余短引入。');
      };
      buffer=registerBuffer('playlist:'+pid,'播放顺序',initialPreparation.ref,read,write,save,order);
      const toolbar=region(E('div',undefined,'wb-toolbar wb-version-toolbar wb-script-tools'),{kind:'playlist',id:pid});
      const saveOrder=button('保存顺序并检查',save,true);
      saveOrder.disabled=!items.length;
      discardOrder=button('放弃顺序修改',async()=>{clearBuffer(buffer);await refresh({force:true});notice('已恢复本场保存的播放顺序。');});
      const orderActions=[['同商品连续','grouped'],['商品穿插','interleave'],['随机重排','full_shuffle']].map(([label,mode])=>{
        const action=button(label,()=>reorder(mode));
        action.disabled=!items.length;
        return action;
      });
      if(!inline)toolbar.append(showGenerator);
      toolbar.append(...orderActions,saveOrder,discardOrder);
      order.append(summary,confirm,list);
      if(!inline)node.append(header('话术与排序','查看本场版本、调整顺序并准备声音。'));
      if(inline){
        const layout=E('div',undefined,'wb-inline-script-layout');
        const queue=E('div',undefined,'wb-inline-script-order');
        queue.append(toolbar,markResultSection(order,'playlist',saved?.ref));
        layout.append(generator,queue);
        node.append(layout);
      }else node.append(toolbar,generator,markResultSection(order,'playlist',saved?.ref));
      paint();
      if(inline){
        const top=E('div',undefined,'wb-inline-script-header');
        top.append(node.querySelector('.wb-room-section-title'));
        node.prepend(top);
        updateAudioOrder=await renderRoomAudio(top,initialPreparation,()=>{
          if(!items.length)return '先生成至少一版可播话术。';
          if(!saved?.ref||!equal(items,savedItems))return '请先保存播放顺序。';
          return '';
        });
        return;
      }
      const audio=E('details',undefined,'wb-script-audio-details');
      audio.open=true;
      audio.append(E('summary','整包检查与语音'));
      node.append(audio);
      await renderAudio(audio);
    }
    async function renderRoomAudio(node,initialPreparation,orderBlock){
      const pid=initialPreparation.ref.id, location=current;
      const box=region(E('section',undefined,'wb-room-audio-action'),{kind:'playlist',id:pid});
      box.dataset.audioReview='';
      markResultSection(box,'audio_manifest',initialPreparation.body.audio_manifest_ref);
      const status=E('p',undefined,'wb-muted');status.setAttribute('role','status');
      const heading=E('h3','本场语音');
      const scopeInfo=E('details',undefined,'wb-room-audio-scope');
      const actions=E('div',undefined,'wb-room-audio-buttons');
      const publication=E('div',undefined,'wb-room-audio-publication');
      const row=E('div',undefined,'wb-room-audio-row');
      const copyBox=E('div');copyBox.append(heading,status);
      row.append(copyBox,actions);
      box.append(row,scopeInfo,publication);node.append(box);
      let observation, review, reviewError, approval, retry, polling=false;
      const observe=()=>api.query('read_completion_observation',{preparation_id:pid});
      const currentJob=()=>{
        const body=initialPreparation.body;
        if(!body.requirements_ref)return null;
        return (observation?.jobs||[]).filter(({body:job})=>job.kind==='audio'&&
          job.generation_epoch===body.generation_epoch&&equal(job.requirements_ref,body.requirements_ref)&&
          equal(job.approval_ref,body.approval_ref)).sort((a,b)=>String(b.body.created_at).localeCompare(String(a.body.created_at)))[0];
      };
      const active=job=>['queued','running'].includes(job?.body.status);
      const uncertain=item=>['timeout_unknown','outcome_unknown'].includes(item.call_state)||
        item.provider_attempts?.some(attempt=>['timeout_unknown','outcome_unknown'].includes(attempt.state));
      const isEditing=()=>busy||hasDirty()||options.getAudioBlock?.()||productDialog||scriptReader||
        options.roomOverlay?.open||content.querySelector('dialog[data-session-product-id][open]')||
        (container.contains(document.activeElement)&&document.activeElement?.matches('input,textarea,select'));
      function assertCanGenerate(){
        if(current!==location)throw Error('直播间配置已切换，请在当前场次重新选择。');
        const blocked=options.getAudioBlock?.()||orderBlock();
        if(blocked)throw Error(blocked);
        if(hasDirty())throw Error('请先保存或放弃未保存的编辑。');
      }
      const generate=button('生成语音',async()=>{
        assertCanGenerate();
        let expected=initialPreparation.ref;
        const saved=initialPreparation.body;
        const latest=await api.query('read_preparation',{preparation_id:pid});
        assertCanGenerate();
        if(!equal(expected,latest.ref))throw Error('本场配置已有更新，请读取最新内容后再生成语音。');
        const retrying=retry;
        if(!saved.compiled_plan_ref||!saved.requirements_ref){
          try{
            const compiled=await command('compile_playback_plan',{
              preparation_id:pid,expected_revision:expected.revision,playlist_ref:saved.playlist_ref
            });
            expected=compiled.data.preparation_ref;
            changedBusiness(compiled);
          }catch(error){
            if(error.code==='MISSING_MODULE')throw Object.assign(Error('播放顺序中的话术还缺少模块。请在右侧请 Agent 补齐，再生成语音。'),{code:error.code,details:error.details});
            throw error;
          }
        }
        const [boundReview,boundApproval]=await Promise.all([
          api.query('create_text_review',{preparation_id:pid}),api.query('read_text_approval',{preparation_id:pid})
        ]);
        assertCanGenerate();
        if(review&&review.review_hash!==boundReview.review_hash)
          throw Error('文稿或音色已经变化，请读取最新内容后再生成语音。');
        if(boundReview.voice.provider==='minimax'){
          const count=boundReview.cost_scope.characters.toLocaleString('zh-CN');
          const voice=boundReview.voice_display_name||boundReview.voice.voice;
          const action=retrying?'仅重试本次已知失败的语音':'确认当前文稿并生成语音';
          if(!global.confirm(action+'？本场合成范围为 '+count+' 字符，使用 MiniMax · '+voice+'，按供应商实际用量计费。')){
            if(!equal(expected,initialPreparation.ref))await refresh({force:true});
            return;
          }
        }
        let approvalRef=boundApproval.status==='current'?boundApproval.approval_ref:null;
        if(!approvalRef){
          const approved=await command('approve_text_for_audio',{
            preparation_id:pid,expected_revision:expected.revision,review_hash:boundReview.review_hash
          });
          approvalRef=approved.data.approval_ref;
          expected=approved.data.preparation_ref;
          changedBusiness(approved);
          assertCanGenerate();
        }
        const receipt=retrying?await command('retry_failed_items',{job_id:retrying.ref.id}):
          await command('prepare_required_audio',{
            preparation_id:pid,expected_revision:expected.revision,approval_ref:approvalRef,
            review_hash:boundReview.review_hash,expected_generation_epoch:boundReview.generation_epoch
          });
        changedBusiness(receipt);
        if(current===location&&!hasDirty())await refresh({force:true});
        notice(retrying?'语音失败项已重新排队。':'本场语音已提交生成。');
      },true,{generateRoomAudio:'true'});
      actions.append(generate);
      function paint(){
        const job=currentJob(), items=job?.body.items||[], unknown=items.some(uncertain);
        const pending=active(job)||observation?.pending_job_ids?.length;
        const blocked=orderBlock();
        const ready=initialPreparation.body.audio_manifest_ref&&approval?.status==='current';
        retry=!unknown&&!pending&&job&&['failed','partially_failed','interrupted'].includes(job.body.status)&&
          items.some(item=>['failed','interrupted'].includes(item.status))?job:null;
        generate.hidden=!!(ready||pending||unknown);
        if(!generate.dataset.agentInputLocked)generate.disabled=!!blocked||!!reviewError;
        generate.textContent=retry?'重试语音失败项':'生成语音';
        status.textContent=ready?'当前语音已就绪。':
          pending?(active(job)?'正在生成语音 · '+items.filter(item=>item.status==='succeeded').length+' / '+items.length:'正在等待本场任务完成。'):
          unknown?'部分语音请求结果待核对。可在「测试与评测」查看任务。':
          retry?'部分语音生成失败，可重试失败项。':
          blocked||reviewError||(review?'按已保存的文稿、顺序和音色生成。':'使用已保存的播放顺序，先检查话术再生成。');
        if(review?.voice.provider==='minimax'&&!ready&&!pending&&!blocked&&!reviewError)
          status.textContent+=' MiniMax 按实际用量计费。';
      }
      async function poll(){
        if(polling||!observation?.pending_job_ids?.length)return;
        polling=true;
        while(box.isConnected&&current===location&&observation.pending_job_ids?.length){
          await new Promise(resolve=>global.setTimeout(resolve,2500));
          if(!box.isConnected||current!==location)return;
          if(document.hidden)continue;
          try{observation=await observe();}
          catch{continue;}
          if(!box.isConnected||current!==location)return;
          paint();
          if(!observation.pending_job_ids?.length){
            if(!isEditing())await refresh();
            else{
              status.textContent='本场任务已结束。处理当前编辑后可刷新语音状态。';
              actions.append(button('刷新状态',()=>refresh(),false,{readOnlyAction:'true'}));
            }
          }
        }
      }
      try{
        observation=await observe();
        if(initialPreparation.body.compiled_plan_ref&&initialPreparation.body.requirements_ref){
          [review,approval]=await Promise.all([
            api.query('create_text_review',{preparation_id:pid}),api.query('read_text_approval',{preparation_id:pid})
          ]);
        }
      }catch(error){
        reviewError=error.code==='CURRENTNESS_BLOCKED'?'有商品资料需要核对，请先确认本场选品和对应话术。':
          error.code==='COMPILED_PLAN_STALE'?'编排已变化，请重新保存播放顺序。':'暂时无法读取语音状态，请刷新后重试。';
        actions.append(button('刷新状态',()=>refresh(),false,{readOnlyAction:'true'}));
      }
      scopeInfo.hidden=!review;
      if(review){
        scopeInfo.append(E('summary','查看合成范围'),E('p',(review.voice_display_name||review.voice.voice)+' · '+playbackSummary(review),'wb-muted'));
        scopeInfo.addEventListener('toggle',()=>{
          if(scopeInfo.open&&!scopeInfo.querySelector('ol')){
            const lines=E('ol');
            for(const item of review.texts)lines.append(E('li',item.spoken_text));
            scopeInfo.append(lines);
          }
        },{passive:true});
      }
      paint();
      const ready=initialPreparation.body.audio_manifest_ref&&approval?.status==='current';
      if(ready){
        try{await renderAudioPublication(publication,pid,initialPreparation,approval);}
        catch{
          status.textContent='当前播出内容需要核对。请检查选品和播放顺序，或刷新状态。';
          actions.append(button('刷新状态',()=>refresh(),false,{readOnlyAction:'true'}));
        }
      }
      if(observation?.pending_job_ids?.length)global.setTimeout(()=>void poll(),1000);
      return paint;
    }
    async function renderAudio(node){const pid=prepId();const initialPreparation=copy(prep());const observation=await api.query('read_completion_observation',{preparation_id:pid});const box=region(section('整包检查与语音'),{kind:'playlist',id:pid});box.append(E('p','先检查当前文本、顺序和音色；你批准整包后才能提交试听或生成语音。','wb-muted'));let review=null,approval=null;try{review=await api.query('create_text_review',{preparation_id:pid});approval=await api.query('read_text_approval',{preparation_id:pid});}catch(e){box.append(E('p','尚不能检查：'+e.message,'wb-inline-alert'));}if(review){const summary=E('div',undefined,'wb-record-meta');summary.append(E('p',playbackSummary(review)+' · '+review.voice.provider+' / '+review.voice.voice),jsonDetails('查看本场整包文本与开场位置',{texts:review.texts,plan_summary:review.plan_summary,voice:review.voice,cost_scope:review.cost_scope,playback_statistics:review.playback_statistics}));box.append(summary);const currentApproval=approval?.status==='current';box.append(E('span',currentApproval?'当前整包已批准':'尚未批准当前整包','wb-badge '+(currentApproval?'wb-badge--success':'wb-badge--warning')));if(!currentApproval)box.append(button('批准当前整包文本',async()=>{if(hasDirty())throw Error('请先保存或放弃未保存编辑，再批准整包。');const r=await command('approve_text_for_audio',{preparation_id:pid,expected_revision:initialPreparation.ref.revision,review_hash:review.review_hash});changedBusiness(r);await refresh({force:true});notice('当前整包文本已批准，尚未调用 TTS。');},true));else{box.append(button('生成本场所需语音',async()=>{if(hasDirty())throw Error('请先保存或放弃未保存编辑。');const r=await command('prepare_required_audio',{preparation_id:pid,expected_revision:initialPreparation.ref.revision,approval_ref:approval.approval_ref,review_hash:review.review_hash,expected_generation_epoch:initialPreparation.body.generation_epoch});changedBusiness(r);await refresh({force:true});notice('语音任务已受理。仅生成本场实际需要的句子；请查看任务进度。');},true));const choice=select(review.texts[0]?.requirement_id,review.texts.map(t=>[t.requirement_id,t.spoken_text.slice(0,45)]),'试听句子');box.append(field('选一句试听（会调用当前 TTS）',choice),button('生成选中一句的试听',async()=>{const r=await command('preview_audio',{preparation_id:pid,expected_revision:initialPreparation.ref.revision,approval_ref:approval.approval_ref,review_hash:review.review_hash,expected_generation_epoch:initialPreparation.body.generation_epoch,requirement_id:choice.value});changedBusiness(r);await refresh({force:true});}),button('撤销文本批准',async()=>{const r=await command('revoke_text_approval',{approval_ref:approval.approval_ref,reason:'用户在工作台撤销'});changedBusiness(r);await refresh({force:true});}));}}
      for(const job of observation.jobs||[]){const jobBox=section((job.body.kind==='scripts'?'话术':job.body.kind==='preview'?'试听':'语音')+'任务 · '+job.body.status);let failed=false;for(const item of job.body.items||[]){jobBox.append(E('p',item.status+' · '+(item.input?.spoken_text||item.input?.product_ref?.id||item.item_id),'wb-muted'));if(['failed','interrupted'].includes(item.status))failed=true;const url=safeUrl(item.output?.asset?.url);if(url){const audio=routedAudio(E('audio'));audio.controls=true;audio.preload='none';audio.src=url;jobBox.append(audio);}if(item.error)jobBox.append(jsonDetails('此项错误',item.error));}if(failed)jobBox.append(button('仅重试失败项',async()=>{const r=await command('retry_failed_items',{job_id:job.ref.id});changedBusiness(r);await refresh({force:true});}));if(['queued','running'].includes(job.body.status))jobBox.append(button('取消此任务',async()=>{const r=await command('cancel_job',{job_id:job.ref.id});changedBusiness(r);await refresh({force:true});}));box.append(jobBox);}
      box.dataset.audioReview='';
      markResultSection(box,'audio_manifest',initialPreparation.body.audio_manifest_ref);
      box.append(button('刷新任务与语音状态',()=>refresh()));
      await renderAudioPublication(box,pid,initialPreparation,approval);
      node.append(box);
    }
    async function renderAudioPublication(box,pid,initialPreparation,approval){
      const publication=initialPreparation.body.candidate_publication_ref;
      if(initialPreparation.body.audio_manifest_ref&&approval?.status==='current'&&!publication)box.append(button('生成本场播出候选',async()=>{const r=await command('publish_preparation',{preparation_id:pid,expected_revision:initialPreparation.ref.revision,approval_ref:approval.approval_ref});changedBusiness(r);await refresh({force:true});},true));
      if(publication){
        const review=await api.query('create_review',{ref:publication});
        const candidate=markResultSection(E('section'),'publication',publication);
        candidate.append(E('h3','本场播出内容','wb-section-heading'),E('p',review.adopted?'当前候选已采用，尚未代表开播。':'候选已生成，采用前将再次检查最新版本。','wb-badge'));
        if(review.currentness?.differences?.length){
          candidate.append(jsonDetails('商品新旧差异',review.currentness));
          for(const diff of review.currentness.differences){
            if(!diff.diff_hash)continue;
            const choices=section('商品修订有变化');
            choices.append(jsonDetails('查看本次差异',diff));
            for(const[choice,label]of [['keep_pinned','保留旧版'],['refresh','刷新到新版']])choices.append(button(label,async()=>{const r=await command('record_product_revision_choice',{preparation_id:pid,expected_revision:initialPreparation.ref.revision,pinned_product_ref:diff.pinned_ref||diff.pinned_product_ref,observed_head_ref:diff.head_ref||diff.observed_head_ref,diff_hash:diff.diff_hash,choice});changedBusiness(r);await refresh({force:true});}));
            candidate.append(choices);
          }
        }
        if(review.currentness?.blockers?.length)candidate.append(jsonDetails('采用前需要处理的问题',review.currentness.blockers));
        else if(!review.adopted)candidate.append(button('确认采用本场',async()=>{if(hasDirty())throw Error('请先保存或放弃未保存编辑，再采用。');const r=await command('adopt_snapshot',{publication_ref:publication,review_hash:review.review_hash,expected_room_revision:review.room_ref.revision});changedBusiness(r);await refresh({force:true});notice('本场播出版本已采用。当前没有启动直播。');},true));
        box.append(candidate);
      }
    }
    async function renderChanges(node){
      const pid=prepId();node.append(header('变更记录与引用','按最新优先展示本场及共享商品库的实际记录，可继续加载更早记录。'));node.append(jsonDetails('本场引用与版本',copy(prep())));
      const list=E('div',undefined,'wb-change-list'),seen=new Set();let cursor=0;node.append(list);
      const more=button('加载更早记录',load);node.append(more);
      async function load(){const page=await api.query('read_events',{cursor,limit:50,scope_id:pid,include_catalog:true,order:'desc'});for(const event of page.events||page.items||[]){const id=event.event_id||event.sequence||JSON.stringify(event);if(seen.has(id))continue;seen.add(id);list.append(jsonDetails((event.type||event.event_type||'业务变更')+' · '+(event.timestamp||event.occurred_at||''),event));}cursor=page.next_cursor;more.hidden=!page.has_more;if(!seen.size&&!list.childElementCount)list.append(E('p','当前本场和共享商品库暂无变更记录。','wb-muted'));}
      await load();
    }
    function dispose(){closeProductDialog({restoreFocus:false,keepDraft:true});closeScriptReader({restoreFocus:false,force:true});document.removeEventListener('keydown',readerKeydown);disposed=true;lockObserver.disconnect();lockedControls.clear();cache.clear();productMaterialFlows.clear();roomMaterialFlows.clear();catalogViewStates.clear();catalogResizeObserver?.disconnect();}
    return {open,renderRoomOverlay,showRoomProductDetail,openSessionProductConfig,closeCatalogImportDialog,getContext,hasOpenProductDialog:()=>!!productDialog,hasOpenScriptReader:()=>!!scriptReader,hasOpenSessionProductConfig:()=>!!content.querySelector('dialog[data-session-product-id][open]'),setEditorLocks,setSelected,hasDirty,getDirtySummary,saveDirty,discardDirty,resolveSelection,refresh,createPreparation,resolveDefaultRoomPreparation,saveRoomProductSelection,refreshRoomProductReference,verifyUnknownOperation,dispose,_inspect:()=>({current:copy(current),dirty:getDirtySummary(),cache:cache.size,disposed,unknownOperation:copy(unknownOperation)})};
  }
  global.ShengboWorkspaceEditors={mount,preparationTitle,preparationAdopted,_test:{schemaDefault,effectiveSchema,safeUrl,reusableDraft,trimCommerceReferences,matchingLocks,newVariantModuleKeys,scriptTextMetrics,currentScriptVariants,playbackSummary,activeProductFields,applyOptionalProductVisibility,unpackCorePoint,packCorePoint,preparationTitle,preparationAdopted,preparationStateLabel}};
  if(typeof module!=='undefined'&&module.exports)module.exports={schemaDefault,effectiveSchema,safeUrl,reusableDraft,trimCommerceReferences,matchingLocks,newVariantModuleKeys,scriptTextMetrics,currentScriptVariants,playbackSummary,activeProductFields,applyOptionalProductVisibility,unpackCorePoint,packCorePoint,preparationTitle,preparationAdopted,preparationStateLabel};
})(typeof window!=='undefined'?window:globalThis);
