/* The workspace shows saved business data. A displayed result never retargets a conversation. */
(function (global) {
  'use strict';
  const $ = id => document.getElementById(id);
  const clone = value => value == null ? value : structuredClone(value);
  const VIEWS = { overview: ['准备概览', '已保存资料、准备进度与待处理事项。'], products: ['商品知识库', '维护商品资料，供所有直播间使用。'], insights: ['顾客与卖点', '查看研究依据、卖点建议和所需素材。'], session: ['本场配置', '本场主题、活动、语言、声音与新增话术要求。'], scripts: ['话术与排序', '新增、排列、试听和逐句修改本场话术。'], changes: ['变更记录', '查看已保存修改与版本差异。'], rooms: ['直播间', '选择要配置的直播间。'], 'room-config': ['直播间配置', '管理本场、商品话术、平台和播出。'] };
  const api = global.ShengboWorkbenchApi;
  let editors, execution, assistant, lastAssistantState, busyDialog = false, manualNavigation = 0;
  let context = { view: 'overview', room_id: null, preparation_id: null };
  let followResults = true, followPaused = false, lastRequestId = null;
  let latestArtifact = null, latestResultButton = null, resultOrigin = null;
  let headerSnapshot = null;
  try { followResults = localStorage.getItem('shengbo-workbench-follow-results-v2') !== 'false'; } catch {}
  const selectionItems=value=>value?(Array.isArray(value.items)?value.items:[value]):[];
  let headerPlayback = null, playbackHeaderPolling = false, playbackControlBusy = false;
  let outputDiagnostic = {}, outputPolling = false, outputReadAt = 0;
  const seenResults = new Set();
  const seenCompletions = new Set();
  Object.assign(VIEWS, { broadcast: ['播出监控', '当前播放、逐句控制、人工插播与 OBS 来源。'], platform: ['平台接入', '配置公开直播间来源，查看实际采集状态。'], comments: ['评论测试', '检索商品知识、测试回复并查看依据。'], jobs: ['任务与评测', '后台任务、失败重试与已有话术评测。'] });
  const EXECUTION_VIEWS = new Set(['broadcast', 'platform', 'comments', 'jobs']);
  const ROOM_SECTIONS = {
    basic: '直播间设置', preparations: '选择或创建本场', overview: '准备概览', products: '本场商品',
    session: '本场配置', scripts: '话术与排序', platform: '平台接入', broadcast: '声音与播出',
    insights: '顾客与卖点', changes: '变更记录', comments: '评论测试', jobs: '任务与评测'
  };
  const ROOM_DIALOG_SECTIONS = new Set(['preparations', 'products', 'insights', 'changes', 'comments', 'jobs']);
  let diagnostics, liveHistory, roomDialogOpener, roomDialogRequest, roomRefreshPending=false, lastRoomRevisionCheck=0;
  const roomProductDrafts = new Map();
  let roomProductBusy = false, roomProductPicker, platformExecution, selectedProductResult;
  function inRoom(value) {
    return !!(value.room_id || value.preparation_id) && value.view !== 'rooms';
  }
  function roomSection(value) {
    return inRoom(value) && (value.room_section || (value.view === 'room-config' || value.view === 'overview' ? null : value.view));
  }
  const isBoard = inRoom;
  const isToolSection = value => ['comments','jobs'].includes(value.view);
  const boardDestination = next => ({...next,view:'room-config',room_section:null,inline_section:roomSection(next)||null});
  function activateSurface(value) {
    const board = $('room-dashboard'), panel = $('work-panel');
    const workspace = $('workspace-editor'), run = $('execution-editor'), strip = $('playback-strip');
    const inline = isBoard(value);
    const monitoring = inline && value.view === 'broadcast';
    board.hidden = !inline || monitoring;
    $('room-monitor').hidden = !monitoring;
    panel.dataset.workspaceSurface = monitoring ? 'room-monitor' : inline ? 'room-config' :
      value.action === 'new_room' ? 'room-create' : value.view === 'products' ? 'catalog' : 'rooms';
    const move = (node, target) => { if (node.parentNode !== target) target.append(node); };
    if (inline) {
      move(workspace, $('room-defaults-editor'));
      move(run, $(monitoring ? 'room-monitor-editor' : 'room-broadcast-editor'));
      move(strip, $(monitoring ? 'room-monitor-audio' : 'room-audio-settings'));
    } else {
      const footer = panel.querySelector('.wb-panel-footer');
      for (const node of [workspace,run]) if (node.parentNode !== panel) panel.insertBefore(node,footer);
    }
    workspace.hidden = false;
    const active = execution?.getState();
    run.hidden = !inline || active?.view !== (monitoring ? 'broadcast' : 'broadcast-setup') ||
      active?.context.room_id !== value.room_id || active?.context.preparation_id !== value.preparation_id;
    workspace.setAttribute('aria-labelledby','workspace-title');
  }
  function closeRoomDialog({restoreFocus=true}={}) {
    const dialog=$('room-sheet');
    if(!dialog.open)return;
    const opener=roomDialogOpener;
    roomDialogOpener=null;
    roomDialogRequest=null;
    dialog.close();
    if(restoreFocus&&opener?.isConnected)opener.focus({preventScroll:true});
  }
  async function showRoomDialog(destination,opener) {
    if(isToolSection(destination)){
      closeRoomDialog({restoreFocus:false});
      return diagnostics?.open(destination,{opener});
    }
    diagnostics?.close({restoreFocus:false});
    if(destination.view==='products'&&destination.product_id){
      closeRoomDialog({restoreFocus:false});
      return editors.showRoomProductDetail(destination,opener);
    }
    const dialog=$('room-sheet');
    if(!dialog.open)roomDialogOpener=opener;
    $('room-sheet-title').textContent=ROOM_SECTIONS[roomSection(destination)];
    const body=$('room-sheet-body'), status=$('room-overlay-status');
    status.textContent='正在读取…';
    status.hidden=false;
    const request=document.createElement('div');
    roomDialogRequest=request;
    body.replaceChildren(request);
    if(!dialog.open)dialog.showModal();
    try {
      await editors.renderRoomOverlay(request,destination);
      if(dialog.open&&roomDialogRequest===request&&status.textContent==='正在读取…')status.hidden=true;
    } catch(error) {
      if(dialog.open&&roomDialogRequest===request){status.textContent=error.message;status.hidden=false;}
    }
  }
  function setText(id, value) { if ($(id).textContent !== value) $(id).textContent = value; }
  const productScopeKey = value => (value.room_id || '') + ':' + (value.preparation_id || '');
  const productDraft = () => roomProductDrafts.get(productScopeKey(context));
  const differentProducts = (picked, base) => picked.size !== base.size || [...picked.keys()].some(id => !base.has(id));
  const productSelectionDirty = draft => !!draft && differentProducts(draft.picked, draft.base);
  function roomProductsLocked() {
    return [...pendingEditorLocks, ...serverEditorLocks].some(item =>
      item.targets?.some(target => target.kind === 'product_selection' && target.id === context.preparation_id));
  }
  function paintRoomPickerActions() {
    if (!roomProductPicker) return;
    const draft = productDraft(), { picked, base } = roomProductPicker;
    const locked = roomProductBusy || roomProductsLocked();
    const status = draft?.stale ? ' · 请关闭后重新读取选品' : locked ? ' · 当前暂不能保存' : '';
    setText('room-picker-count', '已选 ' + picked.size + ' 件' + status);
    $('room-save-products').disabled = !draft || draft.stale || locked ||
      !differentProducts(picked, base) || (!context.preparation_id && !picked.size);
    $('room-product-options').querySelectorAll('input[type=checkbox]').forEach(input => {
      input.disabled = locked || (input.hasAttribute('data-unavailable') && !input.checked);
    });
  }
  function paintRoomDashboard() {
    if (!isBoard(context)) return;
    const current = headerSnapshot?.preparation?.ref.id === context.preparation_id ? headerSnapshot?.preparation : null;
    const draft = productDraft();
    const selected = draft?.picked.size ?? current?.body.selected_product_refs?.length ?? 0;
    const dirty = productSelectionDirty(draft);
    setText('room-board-content', !draft&&!current ? '正在读取选品…' : selected + ' 件已选' + (dirty ? ' · 尚未保存' :
      current ? ' · ' + (current.body.script_refs?.length || 0) + ' 版话术' : ' · 保存选品后可准备话术'));
    paintSelectedProducts(draft);
    $('room-dashboard').querySelectorAll('[data-needs-preparation], [data-room-section="session"]').forEach(button => { button.disabled = !context.preparation_id; });
    $('room-open-products').disabled = !draft || roomProductBusy;
    $('room-reload-products').hidden = !draft?.stale;
    $('room-retry-products').hidden = !dirty || !!draft?.stale;
    $('room-retry-products').disabled = roomProductBusy || roomProductsLocked();
    const feedback = $('room-products-feedback');
    feedback.hidden = !feedback.textContent || feedback.dataset.scope !== productScopeKey(context);
    paintRoomPickerActions();
  }
  const selectedProductPageSize = 3;
  const productRefKey = ref => JSON.stringify([ref.id,ref.revision,ref.content_hash]);
  const productImage = source => /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}$/.test(source || '') ? source : '';
  function paintSelectedProducts(draft) {
    const list = $('room-selected-products');
    const refs = [...(draft?.picked.values() || [])];
    const pages = Math.max(1, Math.ceil(refs.length / selectedProductPageSize));
    const resultId = selectedProductResult?.scope === productScopeKey(context) ? selectedProductResult.id : null;
    const targetIndex = refs.findIndex(ref => ref.id === resultId);
    if (targetIndex >= 0 && !selectedProductResult.revealed) {
      draft.page = Math.floor(targetIndex / selectedProductPageSize);
      selectedProductResult.revealed = true;
    }
    const page = Math.min(draft?.page || 0, pages - 1);
    if (draft) draft.page = page;
    const rows = refs.slice(page * selectedProductPageSize, (page + 1) * selectedProductPageSize).map(ref => {
      const summary = draft.rows.get(ref.id);
      const row = draft.exact.get(productRefKey(ref)) || (summary && productRefKey(summary.ref) === productRefKey(ref) ? summary : {});
      const override = draft.overrides[ref.id];
      const hasUpdate = draft.base.has(ref.id) && summary?.ref?.revision > ref.revision &&
        !summary.archived && summary.sale_status !== 'unavailable';
      const amount = value => [Number(value).toFixed(2),row.currency].filter(Boolean).join(' ');
      const offer = override?.mode === 'price' && override.price != null ? '本场活动价 ' + amount(override.price) :
        override?.mode === 'discount' && override.discount != null ?
          '本场折扣 ' + Number((override.discount * 10).toFixed(2)) + ' 折' : '';
      return {
        id: ref.id, name: row.name || ref.id, image: productImage(row.image || row.thumbnail_url),
        detail: [row.price == null ? '' : '日常价 ' + amount(row.price),row.sku_id].filter(Boolean).join(' · '),
        offer, saved: draft.base.has(ref.id), overridden: Object.hasOwn(draft.overrides,ref.id), result: ref.id === resultId,
        hasUpdate, refreshDisabled: roomProductBusy || roomProductsLocked() || anyDirty()
      };
    });
    const signature = JSON.stringify([productScopeKey(context),page,pages,
      rows.map(({image,...row})=>({...row,hasImage:!!image})),refs.slice(page*selectedProductPageSize,(page+1)*selectedProductPageSize).map(productRefKey)]);
    list.hidden = !rows.length;
    if (list._signature === signature) return;
    list._signature = signature;
    list.replaceChildren();
    for (const row of rows) {
      const item = document.createElement('li'), picture = document.createElement(row.image ? 'img' : 'span');
      picture.className = 'wb-room-product-thumb';
      if (row.result) item.dataset.productResult = '';
      if (row.image) { picture.src = row.image; picture.alt = ''; }
      else { picture.setAttribute('aria-hidden','true'); picture.textContent = '◇'; }
      const copy = document.createElement('div'), name = document.createElement('strong');
      name.textContent = row.name;
      name.title = row.name;
      copy.append(name);
      if (row.detail) {
        const info = document.createElement('small');
        info.textContent = row.detail; info.title = row.detail;
        copy.append(info);
      }
      if (row.offer) {
        const offer = document.createElement('small');
        offer.className = 'wb-room-product-offer';
        offer.textContent = row.offer; offer.title = row.offer;
        copy.append(offer);
      }
      const action = document.createElement('button');
      action.type = 'button'; action.className = 'wb-button';
      action.dataset.sessionProduct = row.id;
      action.dataset.overridden = String(row.overridden);
      action.textContent = '单品设置';
      action.title = '查看或修改该商品的本场优惠和发货';
      action.setAttribute('aria-label','本场设置 '+row.name+(row.overridden?'，已有设置':''));
      action.setAttribute('aria-haspopup','dialog');
      action.disabled = !row.saved;
      action.addEventListener('click',()=>Promise.resolve(editors.openSessionProductConfig(row.id,action)).catch(e=>notice(e.message)));
      const actions = document.createElement('div');
      actions.className = 'wb-room-product-actions';
      actions.append(action);
      if (row.hasUpdate) {
        const update = document.createElement('button');
        update.type = 'button'; update.className = 'wb-button';
        update.textContent = '采用新版';
        update.title = '商品库有新版。仅更新本场对这件商品的引用。';
        update.setAttribute('aria-label','本场采用商品新版 '+row.name);
        update.disabled = row.refreshDisabled;
        update.addEventListener('click',()=>void adoptLatestRoomProduct(row.id));
        actions.append(update);
      }
      item.append(picture,copy,actions);
      list.append(item);
    }
    const pager = $('room-selected-pager');
    pager.hidden = pages === 1;
    setText('room-selected-page',(page+1)+' / '+pages);
    $('room-selected-prev').disabled = page === 0;
    $('room-selected-next').disabled = page + 1 >= pages;
  }
  async function adoptLatestRoomProduct(productId) {
    const draft = productDraft(), target = {...context};
    const previous = draft?.base.get(productId);
    if (!previous || !draft.ref || roomProductBusy || roomProductsLocked() || anyDirty()) return;
    const sameRoom = () => isBoard(context) && productScopeKey(context) === productScopeKey(target);
    roomProductBusy = true; heading();
    try {
      const result = await editors.refreshRoomProductReference({preparationRef:draft.ref, previous});
      if (!sameRoom()) return;
      await navigate({view:'room-config',room_id:target.room_id,preparation_id:target.preparation_id,
        product_id:null,refresh:true});
      notice(result ? '本场已采用该商品的新版。已有话术和语音请重新核对。' : '本场已使用该商品的最新版本。');
    } catch(error) {
      if (!sameRoom()) return;
      if (['REVISION_CONFLICT','PINNED_REFERENCE_CHANGED'].includes(error.code)) {
        void syncHeader();
        notice('本场资料刚刚更新。请核对最新商品后再试。');
      } else notice(error.message);
    } finally { roomProductBusy = false; heading(); }
  }
  function renderRoomProducts(snapshot) {
    if (!isBoard(context)) return;
    const key = productScopeKey(context);
    const preparation = snapshot?.preparation, products = snapshot?.products?.items || [];
    const base = new Map((preparation?.body.selected_product_refs || []).map(ref => [ref.id,ref]));
    const previous = roomProductDrafts.get(key), dirty = productSelectionDirty(previous);
    const rows = new Map(previous?.rows);
    for (const row of products) rows.set(row.ref.id, row);
    const draft = {
      base: dirty ? previous.base : base, picked: dirty ? previous.picked : new Map(base),
      ref: dirty ? previous.ref : preparation?.ref, stale: dirty && previous.stale, rows,
      exact: previous?.exact || new Map(), pending: previous?.pending || new Set(), page: previous?.page || 0,
      overrides: snapshot?.preparation_details?.draft?.body?.draft?.commerce?.product_overrides || {}
    };
    roomProductDrafts.set(key,draft);
    paintRoomDashboard();
    for (const ref of draft.picked.values()) {
      if (!products.some(row => row.ref.id === ref.id)) {
        void api.query('read_product',{product_id:ref.id}).then(record => {
          if (!record?.ref || roomProductDrafts.get(key) !== draft) return;
          rows.set(ref.id,{...record.body,ref:record.ref});
          if (productScopeKey(context) === key) paintRoomDashboard();
        }).catch(()=>{});
      }
      const refKey = productRefKey(ref), row = rows.get(ref.id);
      if (draft.exact.has(refKey) || draft.pending.has(refKey) ||
          (row && productRefKey(row.ref) === refKey && productImage(row.thumbnail_url))) continue;
      draft.pending.add(refKey);
      void api.query('read_product',{ref}).then(record => {
        if (record?.body) draft.exact.set(refKey,record.body);
        if (productScopeKey(context) === key) paintRoomDashboard();
      }).catch(() => {}).finally(() => draft.pending.delete(refKey));
    }
  }
  function roomPickerPageSize() {
    return Number(getComputedStyle($('room-products-picker')).getPropertyValue('--wb-picker-page-size')) || 8;
  }
  function openRoomProductPicker() {
    const draft = productDraft();
    if (!draft || roomProductPicker) return;
    roomProductPicker = { base: draft.base, ref: draft.ref, picked: new Map(draft.picked),
      cursor: 0, next: null, size: roomPickerPageSize(), request: 0, timer: null };
    $('room-search-products').value = '';
    $('room-products-selected-only').checked = false;
    $('room-products-picker').showModal();
    $('room-search-products').focus();
    void loadRoomProductPage(0);
  }
  function closeRoomProductPicker() {
    if (!roomProductPicker) return;
    clearTimeout(roomProductPicker.timer);
    roomProductPicker = null;
    $('room-products-picker').close();
    $('room-open-products').focus({ preventScroll: true });
  }
  function setRoomProductStatus(message) {
    const status = $('room-product-options-status');
    setText(status.id, message);
    status.hidden = !message;
  }
  async function loadRoomProductPage(cursor, delayed = false) {
    const picker = roomProductPicker;
    if (!picker) return;
    const request = ++picker.request, size = picker.size;
    const query = $('room-search-products').value.trim();
    const selectedOnly = $('room-products-selected-only').checked;
    clearTimeout(picker.timer);
    if (delayed) picker.cursor = 0;
    $('room-products-prev').disabled = true;
    $('room-products-next').disabled = true;
    $('room-retry-product-search').hidden = true;
    setText('room-products-page', '');
    $('room-product-options').replaceChildren();
    setRoomProductStatus('正在读取商品…');
    paintRoomPickerActions();
    if (delayed) {
      picker.timer = setTimeout(() => void loadRoomProductPage(cursor), 180);
      return;
    }
    try {
      let page;
      if (selectedOnly) {
        const knownRows = productDraft()?.rows;
        const filter = query.toLocaleLowerCase();
        const picked = [...picker.picked.values()].map(ref => knownRows?.get(ref.id) || { ref, name: '已选商品（资料不可见）' });
        const matches = picked.filter(row => (row.name + ' ' + (row.sku_id || '')).toLocaleLowerCase().includes(filter));
        page = { items: matches.slice(cursor, cursor + size), next_cursor: matches.length > cursor + size ? cursor + size : null };
      } else {
        page = await api.query('search_products', { q: query, cursor, limit: size });
      }
      if (picker !== roomProductPicker || picker.request !== request) return;
      const draft = productDraft();
      if (!draft) return;
      picker.cursor = cursor;
      picker.next = page.next_cursor ?? null;
      $('room-product-options').replaceChildren(...page.items.map(row => {
        draft.rows.set(row.ref.id, row);
        const label = document.createElement('label'), input = document.createElement('input'), title = document.createElement('span');
        const unavailable = row.sale_status === 'unavailable';
        input.type = 'checkbox';
        input.checked = picker.picked.has(row.ref.id);
        input.toggleAttribute('data-unavailable', unavailable);
        title.textContent = row.name + (unavailable ? '（已停售）' : '');
        label.title = title.textContent + (row.sku_id ? ' · ' + row.sku_id : '');
        if (row.sku_id) input.setAttribute('aria-label', title.textContent + '，SKU ' + row.sku_id);
        input.addEventListener('change', () => {
          if (input.checked) picker.picked.set(row.ref.id, picker.base.get(row.ref.id) || row.ref);
          else picker.picked.delete(row.ref.id);
          paintRoomPickerActions();
        });
        label.append(input, title);
        return label;
      }));
      setRoomProductStatus(page.items.length ? '' : selectedOnly ? '没有符合条件的已选商品。' : '没有匹配的商品。');
      setText('room-products-page', '第 ' + (Math.floor(cursor / size) + 1) + ' 页');
      $('room-products-prev').disabled = cursor === 0;
      $('room-products-next').disabled = picker.next === null;
      paintRoomPickerActions();
    } catch {
      if (picker !== roomProductPicker || picker.request !== request) return;
      picker.cursor = cursor;
      setRoomProductStatus('商品读取失败，请重试。');
      $('room-retry-product-search').hidden = false;
      $('room-products-prev').disabled = cursor === 0;
    }
  }
  async function refreshRoomRevision(){
    const board=$('room-dashboard');
    const editor=$('workspace-editor');
    const canRefresh=()=>isBoard(context)&&!!context.preparation_id&&!board.hidden&&!anyDirty()&&
      !roomProductBusy&&!roomProductPicker&&!$('room-sheet').open&&!diagnostics?.isOpen()&&!liveHistory?.isOpen()&&
      !editors.hasOpenProductDialog?.()&&!editors.hasOpenSessionProductConfig?.()&&!editors.hasOpenScriptReader?.()&&
      editor.getAttribute('aria-busy')!=='true'&&!(editor.contains(document.activeElement)&&document.activeElement?.matches('input,textarea,select'));
    if(roomRefreshPending||Date.now()-lastRoomRevisionCheck<5000||!canRefresh())return;
    const generation=navigationGeneration, preparationId=context.preparation_id;
    lastRoomRevisionCheck=Date.now();
    roomRefreshPending=true;
    try{
      const head=await api.query('read_preparation',{preparation_id:preparationId});
      const saved=editors.getContext().preparation_ref;
      if(generation!==navigationGeneration||context.preparation_id!==preparationId||
        !canRefresh()||!head?.ref||head.ref.revision===saved?.revision)return;
      const top=board.scrollTop;
      await editors.open({...boardDestination(context),inline_section:null,action:null,script_id:null,
        resource_id:null,module_key:null,field_path:null,object_ref:null,refresh:true});
      if(generation!==navigationGeneration)return;
      board.scrollTop=top;
      context=workspaceLocation({...context,preparation_ref:editors.getContext().preparation_ref});
      assistant?.setContext(context);
      void syncHeader();
    }catch(_){/* The board keeps its current snapshot when a read fails. */}
    finally{roomRefreshPending=false;}
  }
  async function openRoomInline(value) {
    const opens = (surface, view, preparationId) => {
      if (!surface) return;
      const current = surface.getState();
      if (current.view !== view || current.context.room_id !== value.room_id || current.context.preparation_id !== preparationId)
        return surface.open({...value, view, preparation_id:preparationId});
    };
    const playbackView = value.view==='broadcast' ? 'broadcast' : 'broadcast-setup';
    const playback = opens(execution,playbackView,value.preparation_id);
    if (execution?.getState().view === playbackView) $('execution-editor').hidden = false;
    await Promise.allSettled([playback,opens(platformExecution,'platform',null)]);
  }
  let rooms = [], headerGeneration = 0, navigationGeneration = 0;
  let serverEditorLocks=[],pendingEditorLocks=[],lockPollBusy=false,selectionNoticeCount=null;

  function pendingLocks(request){
    if(!request)return[];const pid=request.scope?.preparation_id;let targets=[];
    for(const selected of selectionItems(request.selection)){const target=selected.target,ref=target?.object_ref;if(!ref)continue;const kind={product:'product',preparation:'session',script_revision:'scripts',analysis:'insights'}[ref.kind];if(kind)targets.push({kind,id:kind==='product'?ref.id:pid||'catalog',field_paths:target.field_path?[target.field_path]:null,module_keys:target.module_key?[target.module_key]:null,...(kind==='scripts'?{script_id:ref.id,product_ids:target.product_ref?[target.product_ref.id]:null}:{})});}
    if(!targets.length&&pid)targets=['session','scripts','insights','assets','playlist','product_selection'].map(kind=>({kind,id:pid}));
    else if(!targets.length)targets=[{kind:'product_create',id:'catalog'}];
    return[{run_id:'submitting:'+request.client_request_id,status:'SUBMITTING',reason:'正在核实任务受理状态',targets}];
  }
  function paintEditorLocks(){editors?.setEditorLocks({locks:[...serverEditorLocks,...pendingEditorLocks]});}
  async function refreshEditorLocks(){
    if(lockPollBusy)return;lockPollBusy=true;
    try{const value=await api.request('/api/assistant/v2/editor-locks');serverEditorLocks=value.locks||[];pendingEditorLocks=(assistant?.getState().pendingRequests||[]).flatMap(pendingLocks);paintEditorLocks();}
    catch(_){/* Keep known locks when the network is uncertain; a failed poll must not release them. */}
    finally{lockPollBusy=false;}
  }
  function anyDirty() { return !!editors?.hasDirty() || !!execution?.hasDirty?.() || !!platformExecution?.hasDirty?.() || !!diagnostics?.hasDirty?.() || productSelectionDirty(productDraft()); }
  // Room defaults affect future preparations, not the already adopted plan.
  function hasPlaybackBlockingChanges() { return productSelectionDirty(productDraft()) || !!editors?.getDirtySummary().some(draft => !draft.key.startsWith('room-defaults:')); }
  function rememberLocation() { try { sessionStorage.setItem('shengbo-workbench-location-v2', JSON.stringify({ view: context.view, room_section: context.room_section || null, product_id: context.product_id || null, preparation_id: context.preparation_id || null, room_id: context.room_id || null, action: null })); } catch (_) {} }
  function notice(message) { selectionNoticeCount=null;$('workbench-notice-message').textContent = message || ''; $('workbench-notice').hidden = !message; }
  function showSelectionNotice(count){notice(count?'已选择 '+count+' 项。在右侧输入修改要求后发送。':'已取消选择。');selectionNoticeCount=count;}
  $('dismiss-workbench-notice').addEventListener('click', () => notice(''));
  function focus(mode) {
    if (!['work', 'chat'].includes(mode)) return;
    document.body.dataset.focusMode = mode; document.body.dataset.mobilePanel = mode;
    for (const panel of ['work', 'chat']) {
      $('focus-' + panel).setAttribute('aria-pressed', String(panel === mode));
      $('mobile-' + panel).setAttribute('aria-selected', String(panel === mode));
    }
  }
  // Layout changes keep the original DOM, cursor, selection and scroll positions.
  for (const side of ['work', 'chat']) {
    const focusPanel = event => {
      // Keep the left dialog in place through the click when returning from Agent chat.
      if (side === 'work' && event.target.closest?.('.wb-product-dialog')) return;
      focus(side);
    };
    $(side + '-panel').addEventListener('pointerdown', focusPanel);
    $(side + '-panel').addEventListener('focusin', focusPanel);
    $('focus-' + side).addEventListener('click', () => focus(side));
    $('mobile-' + side).addEventListener('click', () => focus(side));
  }
  function normalizeDestination(raw) {
    const d = clone(raw || {});
    const aliases = { 'product-detail': 'products', 'product-list': 'products', catalog: 'products', 'session-config': 'session', preparation: 'overview', playlist:'scripts', audio: 'scripts', adopted: 'scripts', task: 'overview' };
    d.view = Object.hasOwn(aliases, d.view) ? aliases[d.view] : d.view || (d.product_id ? 'products' : 'overview');
    if (d.room_section != null && !Object.hasOwn(ROOM_SECTIONS, d.room_section)) throw Error('无法打开该直播间配置区。');
    if (d.view === 'room-config') {
      if (['config', 'room'].includes(d.object_ref?.kind)) {
        if (d.room_id && d.room_id !== d.object_ref.id) throw Error('成果与直播间不一致。');
        d.room_id = d.object_ref.id;
        d.room_section ??= 'basic';
      }
      if (d.room_section && !['basic', 'preparations'].includes(d.room_section)) d.view = d.room_section;
    }
    if (!Object.hasOwn(VIEWS, d.view)) throw Error('该成果没有可识别的工作区位置，请查看执行记录。');
    if (d.room_section && d.view !== 'room-config' && d.room_section !== d.view) throw Error('成果与配置区不一致，请从执行记录核对。');
    if (d.resource_id && d.view === 'products' && !d.product_id) d.product_id = d.resource_id;
    if (d.resource_id && d.view === 'scripts' && !d.script_id) d.script_id = d.resource_id;
    if(d.view==='scripts'&&!d.script_id&&!d.resource_id){d.script_id=null;d.resource_id=null;d.module_key=null;}
    // Destinations are data. Arbitrary model-supplied URLs are never executed.
    delete d.url; delete d.href;
    return d;
  }
  function workspaceLocation(value) {
    const { view, room_id = null, room_section = null, action = null, preparation_id = null, product_id = null, product_label, room_name, prep_label, preparation_ref, scope } = value;
    return { view, room_id, room_section, action: action === 'new_room' ? action : null, preparation_id, product_id, product_label, room_name, prep_label, preparation_ref, scope };
  }

  function heading() {
    const [title, subtitle] = VIEWS[context.view] || VIEWS.overview;
    const configured = inRoom(context), section = roomSection(context);
    setText('workspace-title', context.action === 'new_room' ? '新建直播间' : configured && context.view !== 'broadcast' ? '直播间配置' : title);
    setText('workspace-subtitle', configured ? (context.room_name || '当前直播间') : context.action === 'new_room' ? '填写直播间信息，保存后进入配置。' : subtitle);
    $('workspace-status').textContent = editors?.hasDirty() ? '有未保存修改' : execution?.hasDirty?.() ? '有调试草稿（不影响播出）' : '已读取';
    const liveState = headerPlayback?.room_id === context.room_id ? headerPlayback : null;
    paintRoomDashboard();
    $('review-preparation').hidden = true;
    $('review-preparation').textContent = '查看话术与采用';
    const canPause = !!liveState?.current && ['playing', 'ready', 'preparing', 'buffering', 'paused'].includes(liveState.status);
    const canStop=!!liveState&&!['stopped','completed'].includes(liveState.status);
    $('stop-preparation').hidden=true;
    $('stop-preparation').disabled=playbackControlBusy;
    $('restart-version-preparation').hidden=true;
    $('restart-version-preparation').disabled=playbackControlBusy;
    $('pause-preparation').hidden = true;
    $('pause-preparation').disabled = playbackControlBusy;
    $('pause-preparation').textContent = playbackControlBusy ? '正在处理…' : liveState?.status === 'paused' ? '继续播出' : '暂停播出';
    $('return-from-result').hidden = !resultOrigin;
    $('room-back-list').hidden = !isBoard(context) || context.view === 'broadcast';
    const catalog = context.view === 'products' && !configured;
    $('open-catalog').setAttribute('aria-current', catalog ? 'page' : 'false');
    $('open-rooms').setAttribute('aria-current', catalog ? 'false' : 'page');
    renderAudioOutput(liveState);
    execution?.refreshControls?.();
  }
  function renderAudioOutput(liveState) {
    const strip=$('playback-strip');
    strip.hidden=!isBoard(context);
    if(strip.hidden||!global.ShengboAudioOutput)return;
    const media=execution?.getAudioStatus?.()||{},route=media.routing;
    const externalRoute=route?.mode==='external'&&liveState?.output_source!=='obs';
    const scopedDiagnostic=externalRoute?{...outputDiagnostic,browser_sessions:(outputDiagnostic.browser_sessions||[]).filter(x=>x.device_name===route.label)}:outputDiagnostic;
    const value=global.ShengboAudioOutput.describe(liveState||{},media,scopedDiagnostic);
    if(externalRoute){
      value.device=route.label||'等待选择外部输出设备';
      value.recommendation=route.status==='ready'?'OBS 添加“'+route.receiver.type+'”，设备选择“'+route.receiver.name+'”。':route.message;
      if(route.status!=='ready')value.local='外部音频通道未就绪，已阻止播放';
      else if(!media.ownsPlayback&&liveState?.connected_player)value.local='声音来自其他窗口；在“播出监控”点击“开启本机声音”，让本页按所选设备播放。';
    }
    execution?.audioRouting?.setHostDevices(outputDiagnostic.devices||[]);
    for(const [id,key]of [['audio-playback-status','status'],['audio-local-status','local'],['audio-current-meta','meta'],['audio-current-text','text'],['audio-output-device','device'],['audio-output-advice','recommendation'],['audio-output-signal','signal']])$(id).textContent=value[key];
    $('audio-output-meter').value=value.peak;
    $('audio-output-details').textContent=JSON.stringify(outputDiagnostic,null,2);
    strip.dataset.playbackStatus=liveState?.status||'stopped';
    strip.dataset.view=context.view;
    $('audio-current-meta').hidden=!liveState?.current;
    if (liveState?.current && context.view !== 'broadcast') {
      $('audio-current-meta').textContent = '此直播间当前输出 · ' + value.meta;
    }
    // The monitor already shows the complete current sentence in its main card.
    $('audio-current-text').hidden=!liveState?.current||context.view==='broadcast';
    const routeFault=!!route&&(route.status==='unavailable'||(route.mode==='external'&&route.status!=='ready'));
    const deviceMuted=!!route?.deviceMuted;
    const playbackFault=['blocked','error'].includes(liveState?.status);
    const warning=$('audio-output-warning');
    warning.hidden=!routeFault&&!playbackFault&&!deviceMuted;
    warning.textContent=routeFault ? (route.message||'外部声音设备未就绪，请检查当前播放设备。') : playbackFault ? (liveState?.error||value.local||'声音输出需要检查，请检查当前播放设备。') : deviceMuted ? '所选播放设备在 Windows 中处于静音，请取消设备静音或选择其他设备。' : '';
  }
  async function refreshAudioOutput() {
    if(outputPolling||Date.now()-outputReadAt<3000)return;
    outputPolling=true;outputReadAt=Date.now();
    try{outputDiagnostic=await api.request('/api/audio/output-status');}
    catch(_){outputDiagnostic={...outputDiagnostic,stale:true,message:'设备检测暂不可用，请检查 Windows 音量混合器'};}
    finally{outputPolling=false;}
  }
  async function refreshPlaybackHeader() {
    const roomId = context.room_id;
    if (!roomId) { headerPlayback = null; heading(); return; }
    if (playbackHeaderPolling) return;
    playbackHeaderPolling = true;
    try {
      const [observed] = await Promise.all([api.request('/api/rooms/' + encodeURIComponent(roomId) + '/state'),refreshAudioOutput()]);
      if (roomId === context.room_id) { headerPlayback = { ...observed, room_id: roomId }; heading(); }
    } catch (_) { /* Keep the last session-bound control when a refresh fails. */ }
    finally { playbackHeaderPolling = false; }
  }
  async function navigate(raw, { automatic = false, result = false } = {}) {
    if (roomProductPicker) {
      if (automatic) return clone(context);
      throw Error('请先保存或关闭选品弹窗。');
    }
    const dest = normalizeDestination(raw);
    if (editors.hasOpenSessionProductConfig?.()) {
      if (automatic) return clone(context);
      throw Error('请先保存或关闭单品配置弹窗。');
    }
    const overrideMatch = dest.view === 'session' && /^\/draft\/commerce\/product_overrides\/([^/]+)(?:\/[^/]+)?$/.exec(dest.field_path || '');
    const overrideProduct = overrideMatch?.[1]?.replaceAll('~1','/').replaceAll('~0','~');
    if (overrideProduct && dest.product_id && overrideProduct !== dest.product_id) throw Error('成果与单品配置不一致。');
    const opensProductResult = result && dest.view === 'products' && !!dest.product_id;
    if (result && !automatic && dest.catalog_import_result) editors.closeCatalogImportDialog?.();
    if (editors.hasOpenProductDialog?.() && !opensProductResult) {
      if (automatic) return clone(context);
      throw Error('请先保存或关闭左侧商品弹窗。');
    }
    if (result && dest.product_id && editors.getDirtySummary().some(draft => draft.key === 'product:' + dest.product_id)) {
      throw Error('目标商品有未保存修改。请先保存或放弃草稿，再打开 Agent 成果。');
    }
    const generation = ++navigationGeneration;
    const opener = document.activeElement;
    const origin = resultOrigin || workspaceLocation(context);
    if (!automatic) {
      manualNavigation++;
      followPaused = true;
    }
    if (!result) resultOrigin = null;
    if (!automatic || window.matchMedia('(max-width: 800px)').matches) focus('work');
    const next = { ...workspaceLocation(context), action: null, room_section: null, ...dest, preserve_focus: automatic };
    if (dest.view === 'rooms' || dest.action === 'new_room') {
      next.room_id = null; next.preparation_id = null; next.product_id = null;
    } else if (dest.room_id && dest.room_id !== context.room_id && !Object.hasOwn(dest, 'preparation_id')) {
      next.preparation_id = null;
    }
    if (dest.view === 'overview' && dest.object_ref && !next.room_section) next.room_section = 'overview';
    if (inRoom(next) && !next.room_section && !['room-config', 'overview'].includes(next.view)) next.room_section = next.view;
    try {
      if (isBoard(next) && next.room_id && !next.preparation_id && editors.resolveDefaultRoomPreparation) {
        next.preparation_id = await editors.resolveDefaultRoomPreparation(next);
        if (generation !== navigationGeneration) return clone(context);
      }
      const dialogSection=ROOM_DIALOG_SECTIONS.has(roomSection(next));
      if(automatic&&dialogSection&&!(next.view==='products'&&next.product_id))return clone(context);
      const sameBoard=isBoard(context)&&context.room_id===next.room_id&&context.preparation_id===next.preparation_id;
      if(dialogSection&&sameBoard){
        await showRoomDialog(next,opener);
        return clone(context);
      }
      closeRoomDialog({restoreFocus:false});
      diagnostics?.close({restoreFocus:false});
      liveHistory?.close({restoreFocus:false});
      activateSurface(next);
      if (isBoard(next)) {
        const board=boardDestination(next);
        if(dialogSection){board.inline_section=null;board.object_ref=null;board.action=null;board.product_id=null;}
        await editors.open(board);
      } else await editors.open(next);
    } catch (error) {
      if (generation === navigationGeneration) { activateSurface(context); heading(); }
      throw error;
    }
    if (generation !== navigationGeneration) return clone(context);
    const dialogSection=ROOM_DIALOG_SECTIONS.has(roomSection(next));
    context = workspaceLocation({ ...next, ...editors.getContext(), view:dialogSection?'room-config':dest.view, room_section:dialogSection?null:next.room_section });
    activateSurface(context);
    selectedProductResult = overrideProduct ? {id:overrideProduct,scope:productScopeKey(context)} : null;
    if (result && (origin.view !== context.view || origin.product_id !== context.product_id ||
        origin.preparation_id !== context.preparation_id || origin.room_id !== context.room_id || origin.room_section !== context.room_section)) {
      resultOrigin = origin;
    }
    rememberLocation();
    assistant?.setContext(context);
    heading();
    // The left surface is ready; status queries should not hold its buttons busy.
    void Promise.allSettled([syncHeader(), refreshPlaybackHeader(), isBoard(context) && openRoomInline(context)]);
    if(dialogSection)await showRoomDialog({...next,room_id:context.room_id,preparation_id:context.preparation_id},opener);
    if (overrideProduct && !automatic) await editors.openSessionProductConfig(overrideProduct,opener,{fieldPath:next.field_path});
    const inlineTarget=isBoard(context)&&!dialogSection&&roomSection(next);
    if(inlineTarget && !automatic && !next.object_ref && !next.script_id && !next.action){
      const target=$('room-dashboard').querySelector('[data-inline-room-section="'+inlineTarget+'"]');
      if (inlineTarget === 'session') {
        const settings = target?.querySelector('[data-session-settings]');
        if (settings) settings.open = true;
      }
      if(target && inlineTarget!=='basic')target.scrollIntoView({block:'nearest'});
    }
    if (result && isBoard(context) && !automatic && roomSection(context)==='basic') {
      const target=$('room-defaults-editor').querySelector('input[name="room-name"]');target?.scrollIntoView({block:'nearest'});target?.focus({preventScroll:true});
    }
    return clone(context);
  }

  function openArtifact(destination, automatic = false) {
    return navigate({ ...destination, refresh: true }, { automatic, result: true }).catch(e => notice(e.message));
  }
  function isBusinessResult(artifact) {
    const destination = artifact.destination;
    return !!destination && (destination.object_ref || !['preparation', 'overview', 'task'].includes(destination.view));
  }
  function startBlock() {
    if (hasPlaybackBlockingChanges()) return '请先保存或放弃左侧未保存的商品或本场修改。';
    if (!context.preparation_id) return '请先保存选品，再准备可播内容。';
    return '';
  }
  function showStartBlockers(blockers, target, preparationRef) {
    const scripts = {view:'scripts',room_id:target.room_id,preparation_id:target.preparation_id};
    const session = {...scripts,view:'session'};
    const forScripts = (text, agentCanHelp=false) => ({text,agentCanHelp,destination:scripts,actionLabel:'打开话术与排序'});
    const forSession = text => ({text,agentCanHelp:true,destination:session,actionLabel:'打开本场配置'});
    const audioMissing = '本场语音尚未就绪。请生成或补齐缺失语音。';
    const copy = {
      START_PRODUCTS_REQUIRED: {text:'请先为本场添加要播出的商品。',destination:{...scripts,view:'products'},actionLabel:'打开本场商品'},
      START_SESSION_REQUIRED: forSession('请先保存本场配置。'),
      START_SCRIPTS_REQUIRED: forScripts('本场缺少正式话术。请为选中的商品准备话术。',true),
      START_PLAYLIST_REQUIRED: forScripts('本场还没有保存播出顺序。'),
      START_PUBLICATION_REQUIRED: forScripts('请生成本场播出整包，再检查并采用。',true),
      START_ADOPTION_REQUIRED: forScripts('本场播出内容已准备，请先由你检查并采用。'),
      PRODUCT_REVISION_CHOICE_REQUIRED: forScripts('商品资料有新版本。请核对差异，确认本场继续使用哪一版。'),
      CAMPAIGN_NOT_STARTED: forSession('本场活动还没开始。请核对活动开始时间，再开播。'),
      CAMPAIGN_EXPIRED: forSession('活动已经过期。请更新活动时间或移除过期活动。'),
      INVALID_CAMPAIGN_EXPIRY: forSession('活动截止时间无法核对。请填写正确的日期和时间。'),
      TEXT_APPROVAL_NOT_CURRENT: forScripts('请先审核并批准当前话术生成语音。'),
      TEXT_APPROVAL_REVOKED: forScripts('话术的语音批准已撤回。请重新审核。'),
      TEXT_APPROVAL_STALE: forScripts('话术或顺序已经变化。请重新审核当前版本。'),
      AUDIO_REQUIREMENTS_MISSING: forScripts('请核对并保存播出顺序，生成本场语音清单。',true),
      AUDIO_MANIFEST_MISSING: forScripts(audioMissing,true),
      AUDIO_COVERAGE_OR_FILE_INVALID: forScripts(audioMissing,true),
      AUDIO_MANIFEST_BINDING_MISMATCH: forScripts(audioMissing,true),
      AUDIO_NOT_READY: forScripts(audioMissing,true),
      BUSINESS_JOB_PENDING: forScripts('本场仍有任务在执行。等任务结束后再复查。'),
      BUSINESS_JOB_OUTCOME_UNKNOWN: forScripts('有任务结果尚未确认。请先核对原任务，再决定是否补做。'),
      PUBLICATION_SUPERSEDED: forScripts('本场播出整包已经更新。请检查并采用当前版本。'),
      PUBLICATION_STALE: forScripts('本场准备内容已有变化。请重新检查并采用。'),
      LIVE_SLOT_BUSY: {text:'还有场次正在启动或播出。请先确认原场次状态。'}
    };
    const productFields = {
      START_PRODUCT_NAME_REQUIRED:['缺少商品名称，请补充正确的名称。','/name'],
      START_PRODUCT_PRICE_REQUIRED:['缺少日常售价，请提供正确的金额。','/price'],
      START_PRODUCT_SELLING_POINTS_REQUIRED:['缺少核心卖点。请提供至少一条，或让 Agent 根据商品资料生成。','/core_selling_points'],
      PRODUCT_UNAVAILABLE_OR_WITHDRAWN:['当前不可售，请确认本场是否应移除该商品。'],
      PRODUCT_EXPLICITLY_WITHDRAWN:['已停止用于播出，请在本场更换该商品。']
    };
    const updatedProducts = new Set((blockers || []).filter(row=>row.code==='START_PRODUCT_VERSION_REQUIRED').map(row=>row.product_ref?.id));
    const issues = (blockers || []).filter(row=>row.code!=='PRODUCT_REVISION_CHOICE_REQUIRED'||!updatedProducts.has(row.product_id)).map(blocker => {
      if (blocker.message) return {text:blocker.message};
      if (blocker.code==='START_PRODUCT_VERSION_REQUIRED') {
        return {
          text:'「'+(blocker.product_name||'本场商品')+'」的新版已补上资料。改用新版后，需要核对受影响的话术和语音。',
          actionLabel:'将本场改用商品新版',
          action:async()=>{
            await api.command('record_product_revision_choice',{preparation_id:preparationRef.id,expected_revision:preparationRef.revision,
              pinned_product_ref:blocker.pinned_product_ref,observed_head_ref:blocker.product_ref,diff_hash:blocker.diff_hash,choice:'refresh'});
            await syncHeader();
            notice('本场商品版本已更新。请核对受影响的话术与语音，然后再开播。');
          }
        };
      }
      if (productFields[blocker.code]) {
        const productId=blocker.product_ref?.id;
        const [guidance, fieldPath] = productFields[blocker.code];
        const label = blocker.product_name || '本场商品';
        const destination = productId ? {view:'products',product_id:productId,room_id:null,preparation_id:null} : null;
        return {
          text:'「'+label+'」' + guidance,
          agentCanHelp:!!(productId && fieldPath),
          selection:productId && fieldPath ? {label,target:{object_ref:blocker.product_ref,field_path:fieldPath},destination} : null,
          destination,
          actionLabel:'查看商品资料'
        };
      }
      return copy[blocker.code] || forScripts('本场播出内容需要重新核对。请打开当前话术与排序，查看需要处理的事项。');
    });
    const unique = [...new Map(issues.map(issue=>[issue.text,issue])).values()];
    if (!unique.length) unique.push(forScripts('暂时无法确认本场已就绪，请刷新本场后再试。'));
    focus('chat');
    assistant.showStartAudit({issues:unique,context:target,onNavigate:destination=>openArtifact(destination)});
  }
  async function syncHeader() {
    const generation = ++headerGeneration, navigation = navigationGeneration;
    try {
      const snapshot = await api.query('get_workspace_snapshot', context.preparation_id ? { preparation_id: context.preparation_id } : {});
      if (generation !== headerGeneration || navigation !== navigationGeneration) return;
      headerSnapshot = snapshot;
      rooms = snapshot.rooms || [];
      const room = rooms.find(r => r.ref.id === context.room_id);
      const preparation = snapshot.preparation;
      const roomName = room?.body.name || '';
      const preparationName = preparation ? window.ShengboWorkspaceEditors.preparationTitle(preparation, snapshot.preparation_details?.draft) : '';
      context = { ...context, room_name: roomName, prep_label: preparationName };
      $('context-room').textContent = roomName || '全部工作区';
      $('context-preparation').textContent = isBoard(context) && (!preparationName || preparationName === roomName)
        ? '默认配置' : preparationName || (context.room_id ? '尚未选择本场' : context.view === 'products' ? '商品知识库' : '直播间');
      $('context-room').title = roomName || '全部工作区';
      $('service-status').textContent = '本机服务已连接';
      $('service-status').dataset.connected = 'true';
      renderRoomProducts(snapshot);
      heading();
      assistant?.setContext(context);
    } catch (e) {
      if (generation !== headerGeneration || navigation !== navigationGeneration) return;
      headerSnapshot = null;
      $('service-status').textContent = '连接需检查';
      delete $('service-status').dataset.connected;
      heading();
      notice(e.message);
    }
  }
  function choose(message, choices) {
    return new Promise(resolve => {
      busyDialog = true;
      const dialog = document.createElement('dialog'); dialog.className = 'wb-confirm-dialog';
      const title = document.createElement('h2'); title.textContent = '发送前确认';
      const text = document.createElement('p'); text.textContent = message;
      const actions = document.createElement('div'); actions.className = 'wb-form-actions';
      function finish(value) { busyDialog = false; dialog.close(); dialog.remove(); resolve(value); }
      for (const choice of choices) { const b = document.createElement('button'); b.type = 'button'; b.className = 'wb-button'; b.textContent = choice.label; b.addEventListener('click', () => finish(choice.value)); actions.append(b); }
      dialog.append(title, text, actions); document.body.append(dialog);
      dialog.addEventListener('cancel', e => { e.preventDefault(); finish('cancel'); }); dialog.showModal();
    });
  }
  async function beforeSend({ selection, conversation, source }) {
    if (source === 'catalog_points') {
      if (editors.hasDirty(selection)) throw Error('请先保存或放弃所选商品的未保存编辑，再生成核心卖点。');
      return { selection };
    }
    if (selection && editors.hasDirty(selection)) {
      const choice = await choose('选中的对象还有未保存修改。请先决定使用哪份内容；你的对话输入会保留。', [ { label: '保存修改并继续', value: 'save' }, { label: '放弃修改并继续', value: 'discard' }, { label: '返回编辑', value: 'cancel' } ]);
      if (choice === 'cancel') return false;
      if (choice === 'save') await editors.saveDirty(selection); else await editors.discardDirty(selection);
      if (editors.hasDirty(selection)) throw Error('仍有未保存内容，请先检查保存结果。');
    }
    if (selection) {
      selection = await editors.resolveSelection(selection);
      if (selection.stale) throw Error('选中的内容已出现新修订。请先查看差异，重新指定要修改的版本。输入已保留。');
      const preparations=[...new Set(selectionItems(selection).map(item=>item.preparation_id||item.destination?.preparation_id).filter(Boolean))];
      if(preparations.length>1)throw Error('一次任务只能修改同一本场中的内容，请移除属于其他本场的选中项。');
      const selectedPreparation=preparations[0];
      if (selectedPreparation && conversation && conversation.preparation_id !== selectedPreparation) {
        const choice = await choose('选中的内容属于另一本场。当前对话仍绑定原任务；是否为选中的本场新建对话后发送？', [ { label: '为选中本场新建对话并发送', value: 'new' }, { label: '取消发送', value: 'cancel' } ]);
        if (choice !== 'new') return false;
        const selectedDestination=selectionItems(selection).find(item=>(item.preparation_id||item.destination?.preparation_id)===selectedPreparation)?.destination;
        const next = await assistant.newConversation({ ...selectedDestination, preparation_id: selectedPreparation }, { moveDraft: true });
        if (!next) return false;
      }
    }
    return { selection };
  }
  function observed(selection) {
    const refs = selectionItems(selection).flatMap(item=>[item.target?.object_ref,item.target?.product_ref]);
    return refs.filter((r, i) => r && refs.findIndex(x => x?.kind === r.kind && x?.id === r.id && x?.revision === r.revision) === i).map(clone);
  }
  function assistantState(value) {
    lastAssistantState = value;
    const mobileChat = $('mobile-chat');
    const tabLabel = value.pendingDecisionCount ? 'Agent 对话（' + value.pendingDecisionCount + ' 项待确认）' : 'Agent 对话';
    if (mobileChat.textContent !== tabLabel) mobileChat.textContent = tabLabel;
    editors?.setSelected(value.selected);
    const selectedCount=selectionItems(value.selected).length;
    if(selectionNoticeCount!==null&&selectedCount!==selectionNoticeCount)showSelectionNotice(selectedCount);
    pendingEditorLocks = (value.pendingRequests || []).flatMap(pendingLocks);
    paintEditorLocks();refreshEditorLocks();
    for (const run of value.runs) {
      if (!['COMPLETED', 'READY', 'READY_FOR_REVIEW'].includes(run.status) || seenCompletions.has(run.run_id)) continue;
      seenCompletions.add(run.run_id);
      if (run.artifacts?.some(isBusinessResult) && !value.hydrating) {
        notice('Agent 已保存成果，可从右侧打开对应内容。');
        const pointProducts = new Set(run.artifacts
          .filter(artifact => artifact.destination?.field_path === '/core_selling_points')
          .map(artifact => artifact.destination.product_id).filter(Boolean));
        for (const productId of pointProducts) void assistant?.refreshCatalogRequirement(productId);
        if (isBoard(context) && run.artifacts.some(artifact => {
          const ref = artifact.destination?.object_ref || artifact.ref;
          return ref?.kind === 'product' && productDraft()?.base.has(ref.id);
        })) void syncHeader();
      }
    }
    const requestId = value.pendingRequest?.client_request_id;
    if (requestId && value.sending && !value.hydrating && requestId !== lastRequestId) {
      lastRequestId = requestId;
      followPaused = false;
    }
    const candidates = value.runs.flatMap(run => (run.artifacts || [])
      .filter(isBusinessResult)
      .map(artifact => ({ ...artifact, runId: run.run_id,
        directSellingPoints: run.input?.view_context?.selling_points_action === 'generate_and_save'
          && artifact.destination.object_ref?.kind === 'product' && artifact.destination.field_path === '/core_selling_points'
      })));
    latestArtifact = candidates.at(-1) || null;
    if (latestResultButton) {
      latestResultButton.hidden = !latestArtifact;
      latestResultButton.title = latestArtifact?.label || '查看 Agent 最近保存的成果';
    }
    let fresh = [];
    for (const artifact of candidates) {
      const key = JSON.stringify([value.conversation?.conversation_id, artifact.runId, artifact.artifact_id || [artifact.ref, artifact.destination]]);
      const reconciled = artifact.directSellingPoints && artifact.runId === value.reconciledCatalogPointRunId;
      if (seenResults.has(key) && !reconciled) continue;
      seenResults.add(key);
      fresh.push(artifact);
    }
    if (!fresh.length || value.hydrating || !followResults || followPaused ||
        value.pendingRequest || anyDirty() || busyDialog || $('room-sheet').open || diagnostics?.isOpen() || liveHistory?.isOpen() || editors.hasOpenSessionProductConfig?.() || editors.hasOpenScriptReader?.()) return;
    fresh = fresh.filter(a => (!value.hasPendingInput || a.directSellingPoints) &&
      (!a.destination.preparation_id || a.destination.preparation_id === context.preparation_id) &&
      (!editors.hasOpenProductDialog?.() || (a.destination.view === 'products' && a.destination.product_id)));
    const exact = fresh.filter(a => a.destination.object_ref);
    const target = exact.at(-1) || fresh.at(-1);
    if (target) openArtifact(target.destination, true);
  }
  editors = global.ShengboWorkspaceEditors.mount({
    container: $('workspace-editor'), api, roomCanvas:true, roomOverlay:$('room-sheet'),
    getAudioBlock: () => productSelectionDirty(productDraft()) ? '请先保存本场选品。' : '',
    onAudioElement:audio=>execution?.audioRouting?.registerMedia(audio),
    onSelect(selection,{focusChat=false}={}) {
      assistant.toggleSelection(selection,{focus:false});
      showSelectionNotice(selectionItems(assistant.getState().selected).length);
      if(focusChat){closeRoomDialog({restoreFocus:false});focus('chat');assistant.focusInput();}
      else focus('work');
    },
    onNavigate: dest => navigate(dest),
    onDirty: () => heading(),
    onUploadProductMaterials: async options => {
      const batch=await assistant.uploadProductMaterials(options);
      if(options.compose && batch?.composeAttached)focus('chat');
      return batch;
    },
    onSubmitProductMaterials: options => assistant.submitProductMaterials(options),
    onUploadRoomMaterials: options => assistant.uploadProductMaterials(options),
    onSubmitRoomMaterials: async options => {
      const receipt = await assistant.submitProductMaterials(options);
      focus('chat');
      return receipt;
    },
    onPrepareGeneration: async (text,taskContext) => { await assistant.prepareTask(text,taskContext); focus('chat'); notice('新增版本要求已放入右侧。检查后发送，Agent 将对照已有话术生成新稿。'); },
    onError: e => notice(e.message),
    onChange({ receipt, context: next }) {
      context = workspaceLocation({ ...context, ...next }); heading(); assistant?.setContext(context); syncHeader();
      const productId = receipt?.command === 'save_product' && receipt.data?.product_ref?.id;
      if (productId) void assistant?.refreshCatalogRequirement(productId);
    }
  });
  if (global.ShengboWorkbenchExecution) execution = global.ShengboWorkbenchExecution.mount({
    container: $('execution-editor'), api, inline: true, splitPlayback: true, showNavigation: false, getContext: () => clone(context),
    getStartBlock: startBlock, onStartBlocked: showStartBlockers, onError: e => notice(e.message), onNavigate: dest => navigate(dest),
    audioRouteContainer: $('audio-route-controls'), onAudioRouteChange: () => heading()
  });
  if(global.ShengboWorkbenchExecution) platformExecution=global.ShengboWorkbenchExecution.mount({
    container:$('room-platform-editor'),api,inline:true,passive:true,hidePlatformActivity:true,showNavigation:false,getContext:()=>clone(context)
  });
  if(global.ShengboWorkbenchExecution?.mountDiagnostics)diagnostics=global.ShengboWorkbenchExecution.mountDiagnostics({
    container:$('room-diagnostics-host'),api,getContext:()=>clone(context),sampleAudioRouting:execution?.audioRouting,onError:error=>notice(error.message),onNavigate:destination=>navigate(destination)
  });
  liveHistory = global.ShengboRoomLiveHistory?.mount({
    container:$('room-live-history-host'),api,getContext:()=>clone(context),onError:error=>notice(error.message)
  });
  for (const id of ['room-open-live-history','room-monitor-live-history']) {
    const opener = $(id);
    opener.disabled = !liveHistory;
    opener.addEventListener('click',()=>liveHistory?.open({opener}));
  }
  $('room-platform-editor').addEventListener('input',heading);
  $('room-open-products').addEventListener('click',openRoomProductPicker);
  $('room-close-products').addEventListener('click',closeRoomProductPicker);
  $('room-products-picker').addEventListener('cancel',event=>{
    event.preventDefault();
    closeRoomProductPicker();
  });
  $('room-search-products').addEventListener('input',()=>void loadRoomProductPage(0,true));
  $('room-products-selected-only').addEventListener('change',()=>void loadRoomProductPage(0));
  $('room-products-prev').addEventListener('click',()=>void loadRoomProductPage(Math.max(0,roomProductPicker.cursor-roomProductPicker.size)));
  $('room-products-next').addEventListener('click',()=>void loadRoomProductPage(roomProductPicker.next));
  $('room-retry-product-search').addEventListener('click',()=>void loadRoomProductPage(roomProductPicker.cursor));
  global.addEventListener('resize',()=>{
    if (!roomProductPicker) return;
    const size=roomPickerPageSize();
    if(size===roomProductPicker.size)return;
    roomProductPicker.size=size;
    void loadRoomProductPage(0);
  });
  $('room-reload-products').addEventListener('click',()=>{
    roomProductDrafts.delete(productScopeKey(context));
    setText('room-products-feedback','');
    heading();
    void syncHeader();
  });
  $('room-save-products').addEventListener('click',()=>{
    const draft=productDraft();
    if(!roomProductPicker||!draft)return;
    const {base,ref,picked}=roomProductPicker;
    Object.assign(draft,{base,ref,picked});
    closeRoomProductPicker();
    void saveRoomProducts();
  });
  $('room-retry-products').addEventListener('click',()=>void saveRoomProducts());
  async function saveRoomProducts(){
    const draft=productDraft(), roomId=context.room_id, preparationId=context.preparation_id, key=productScopeKey(context);
    if(roomProductBusy || !draft || draft.stale || roomProductsLocked() || !productSelectionDirty(draft))return;
    roomProductBusy=true;heading();
    const show=message=>{
      const element=$('room-products-feedback');
      setText(element.id,message);
      element.dataset.scope=productScopeKey(context);
      element.hidden=!message;
    };
    const isCurrent=scopeKey=>isBoard(context)&&productScopeKey(context)===scopeKey;
    show('正在保存选品…');
    try {
      const result=await editors.saveRoomProductSelection({roomId,preparationId,expectedPreparationRef:draft.ref,productRefs:[...draft.picked.values()]});
      roomProductDrafts.delete(key);
      const nextKey=productScopeKey({room_id:roomId,preparation_id:result.preparationId});
      roomProductDrafts.set(nextKey,{...draft,base:new Map(draft.picked),ref:result.preparationRef,stale:false});
      if (isCurrent(key)) await navigate({view:'room-config',room_id:roomId,preparation_id:result.preparationId,product_id:null,refresh:true});
      if (isCurrent(nextKey)) show(result.warning||'选品已保存。');
    } catch(error) {
      let errorKey=key;
      if(error.preparationId && error.preparationId!==preparationId){
        errorKey=productScopeKey({room_id:roomId,preparation_id:error.preparationId});
        if(error.preparationRef){
          roomProductDrafts.set(errorKey,{...draft,base:new Map(),ref:error.preparationRef});
          roomProductDrafts.delete(key);
        }
        if(isCurrent(key)) await navigate({view:'room-config',room_id:roomId,preparation_id:error.preparationId,product_id:null,refresh:true}).catch(()=>{});
      }
      const failedDraft=roomProductDrafts.get(errorKey);
      if(failedDraft)failedDraft.stale=error.code==='PREPARATION_STALE';
      if(isCurrent(errorKey))show(error.message);
    } finally {roomProductBusy=false;heading();}
  }
  $('execution-editor').addEventListener('input', heading);
  $('execution-editor').addEventListener('change', heading);
  assistant = global.ShengboAssistant.mount({
    container: $('assistant-container'), title: '播前 Agent', subtitle: '整理资料、修改内容、查看保存结果',
    storageKey: 'shengbo-agent-workbench-v2', getViewContext: () => clone(context),
    onNavigate: dest => navigate({ ...dest, refresh: true }).catch(e => notice(e.message)),
    onResult: dest => openArtifact(dest),
    beforeSend, getObservedVersions: observed, onState: assistantState
  });
  const toRoomList = () => navigate({ view: 'rooms', room_id: null, preparation_id: null, product_id: null }).catch(e => notice(e.message));
  $('room-back-list').addEventListener('click', toRoomList);
  $('room-monitor-back').addEventListener('click',()=>navigate({view:'room-config',product_id:null}).catch(e=>notice(e.message)));
  for (const [id,step] of [['room-selected-prev',-1],['room-selected-next',1]]) {
    $(id).addEventListener('click',()=>{const draft=productDraft(); if(draft){draft.page+=step;paintSelectedProducts(draft);}});
  }
  $('room-close-section').addEventListener('click',()=>closeRoomDialog());
  $('room-sheet').addEventListener('cancel',event=>{
    if(event.target!==$('room-sheet'))return;
    event.preventDefault();
    closeRoomDialog();
  });
  $('room-dashboard').querySelectorAll('[data-room-section]').forEach(button => button.addEventListener('click', () => {
    const section = button.dataset.roomSection;
    navigate({ view: 'room-config', room_section: section, product_id: null }).catch(e => notice(e.message));
  }));
  $('open-catalog').addEventListener('click', () => navigate({ view: 'products', product_id: null, preparation_id: null, room_id: null, action: null }).catch(e => notice(e.message)));
  $('open-rooms').addEventListener('click', toRoomList);
  $('review-preparation').addEventListener('click', () => navigate({ view: 'scripts', action: 'review' }).catch(e => notice(e.message)));
  $('return-from-result').addEventListener('click', () => {
    if (resultOrigin) navigate(resultOrigin).catch(e => notice(e.message));
  });
  $('pause-preparation').addEventListener('click', async () => {
    if (playbackControlBusy || !headerPlayback?.session_id || headerPlayback.room_id !== context.room_id) return;
    const observed = clone(headerPlayback), action = observed.status === 'paused' ? 'resume' : 'pause';
    playbackControlBusy = true; heading();
    try {
      // Pause the current sentence in this session, including across a sentence
      // boundary. An epoch fence here would make ordinary pause clicks fail.
      if (action === 'resume' && !observed.connected_player) await execution.resumeWithSound(clone(context));
      else await api.request('/api/rooms/' + encodeURIComponent(observed.room_id) + '/control', {
        action, action_id: api.uuid(), expected_session_id: observed.session_id
      });
      if (context.room_id === observed.room_id) {
        await refreshPlaybackHeader();
        if (context.view === 'broadcast') await execution?.refresh();
        notice(action === 'pause' ? '语音已暂停，点击“继续播出”从当前位置继续。' : '语音已继续播放。');
      }
    } catch (e) { notice(e.message || '操作未完成，请检查当前播放状态。'); }
    finally { playbackControlBusy = false; await refreshPlaybackHeader(); heading(); }
  });
  for(const [id,action] of [['stop-preparation','stop'],['restart-version-preparation','restart_version']]) $(id).addEventListener('click',async()=>{
    if(playbackControlBusy||!headerPlayback?.session_id||headerPlayback.room_id!==context.room_id)return;
    const observed=clone(headerPlayback);playbackControlBusy=true;heading();
    try{
      if(action==='restart_version')await execution.restartVersionWithSound(clone(context),observed);
      else await api.request('/api/rooms/'+encodeURIComponent(observed.room_id)+'/control',{action,action_id:api.uuid(),expected_session_id:observed.session_id});
      if(context.room_id===observed.room_id){
        await refreshPlaybackHeader();if(context.view==='broadcast')await execution?.refresh();
        notice(action==='stop'?'声播声音已停止。请到「平台接入」关闭采集，并在抖音端结束直播。':'当前版本已从第一句话重新播放，后续按原队列顺序播出。');
      }
    }catch(e){notice(e.message||'操作未完成，请核对播出状态。');}
    finally{playbackControlBusy=false;await refreshPlaybackHeader();heading();}
  });

  const followLabel = document.createElement('label'); followLabel.className = 'wb-follow';
  const follow = document.createElement('input'); follow.id = 'follow-results'; follow.type = 'checkbox'; follow.setAttribute('aria-label', '自动跟随当前任务成果');
  follow.checked = followResults;
  follow.addEventListener('change', () => {
    followResults = follow.checked;
    followPaused = false;
    try { localStorage.setItem('shengbo-workbench-follow-results-v2', String(followResults)); } catch {}
  });
  followLabel.append(follow, document.createTextNode('自动跟随成果'));
  document.querySelector('.wb-panel-footer').append(followLabel);
  latestResultButton=document.createElement('button');latestResultButton.type='button';latestResultButton.className='wb-button wb-latest-result';latestResultButton.textContent='查看最新成果';latestResultButton.hidden=!latestArtifact;latestResultButton.addEventListener('click',()=>{if(latestArtifact?.destination)openArtifact(latestArtifact.destination);});document.querySelector('.wb-panel-footer').append(latestResultButton);
  focus('chat');
  const editorLockTimer=setInterval(()=>{if(!document.hidden){refreshEditorLocks();refreshPlaybackHeader();void refreshRoomRevision();}},1500);
  global.addEventListener('pagehide',()=>clearInterval(editorLockTimer),{once:true});
  const ready = (async () => {
    await assistant.ready;
    if (manualNavigation) return;
    const conv = assistant.getState().conversation;
    let saved = null; try { saved = JSON.parse(sessionStorage.getItem('shengbo-workbench-location-v2') || 'null'); } catch (_) {}
    context = { ...context, room_id: conv?.room_id || null, preparation_id: conv?.preparation_id || null, ...(saved && VIEWS[saved.view] ? saved : {}) };
    if (!context.room_id && !context.preparation_id && context.view === 'overview') context.view = 'rooms';
    if (EXECUTION_VIEWS.has(context.view) && !execution) context.view = 'overview';
    activateSurface(context);
    if(isBoard(context) && context.room_id && !context.preparation_id && editors.resolveDefaultRoomPreparation)
      context.preparation_id=await editors.resolveDefaultRoomPreparation(context);
    const initial=context;
    const initialDialog=ROOM_DIALOG_SECTIONS.has(roomSection(initial));
    await editors.open(isBoard(initial)?boardDestination(initial):initial);
    context=workspaceLocation({...initial,...editors.getContext(),view:initialDialog?'room-config':initial.view,room_section:initialDialog?null:initial.room_section});
    activateSurface(context);
    heading(); assistant.setContext(context); await Promise.all([syncHeader(),refreshEditorLocks(),refreshPlaybackHeader(),isBoard(context)&&openRoomInline(context)]);
  })().catch(e => { notice('工作区读取失败：' + e.message); $('service-status').textContent = '连接需检查'; });
  global.shengboWorkbench = { ready, focus, navigate, editors, execution, assistant, refreshEditorLocks, state: () => ({ focusMode: document.body.dataset.focusMode, mobilePanel: document.body.dataset.mobilePanel, view: context.view, context: clone(context), hasDirty: anyDirty(), followResults,editorLocks:clone(serverEditorLocks) }) };
  global.addEventListener('beforeunload', e => { if (anyDirty() || lastAssistantState?.hasPendingInput) { e.preventDefault(); e.returnValue = ''; } });
})(window);
