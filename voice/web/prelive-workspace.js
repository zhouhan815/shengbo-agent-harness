/* Human review workspace. All authority remains in the voice v2 service. */
(function(global){
  'use strict';
  const VERSION='PRELIVE-2.3';
  const LABELS={opening_long:'长开场',opening_short:'短引入',product_intro:'商品介绍',selling_points:'卖点讲解',campaign:'本场活动',cta:'促单',transition:'衔接'};
  const STATES={draft:'准备中',ready_to_adopt:'候选已备好，待采用',candidate_requires_choice:'待确认商品版本差异'};
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
  const uuid=()=>crypto.randomUUID();
  let sessionPromise;
  async function request(path,body,retried=false){
    if(!sessionPromise)sessionPromise=fetch('/api/assistant/v2/session',{credentials:'same-origin'}).then(r=>{if(!r.ok)throw Error('无法建立本机操作会话');return r.json()}).catch(e=>{sessionPromise=null;throw e});
    const session=await sessionPromise;
    const r=await fetch(path,{method:body?'POST':'GET',credentials:'same-origin',headers:body?{'Content-Type':'application/json','X-CSRF-Token':session.csrf_token}:{},body:body?JSON.stringify(body):undefined});
    const v=await r.json();if(!r.ok){if(!retried&&[401,403].includes(r.status)&&['LOCAL_SESSION_REQUIRED','CSRF_REJECTED'].includes(v.error?.code)){sessionPromise=null;return request(path,body,true)}throw Error(v.error?.message||(typeof v.detail==='string'?v.detail:v.detail?.message)||'操作失败')}return v;
  }
  const query=(name,args)=>request('/api/prelive/v2/queries',{query:name,args});
  const command=(name,args)=>request('/api/prelive/v2/commands',{command:name,args,contract_version:VERSION,mutation:{operation_id:uuid()}});
  function details(title,value){const d=el('details');d.append(el('summary',title),el('pre',JSON.stringify(value,null,2),'prelive-json'));return d}
  function sorted(v){if(Array.isArray(v))return v.map(sorted);if(v&&typeof v==='object')return Object.fromEntries(Object.keys(v).sort().map(k=>[k,sorted(v[k])]));return v}
  async function hash(value){const data=new TextEncoder().encode(JSON.stringify(sorted(value)));const bytes=await crypto.subtle.digest('SHA-256',data);return 'sha256:'+Array.from(new Uint8Array(bytes),x=>x.toString(16).padStart(2,'0')).join('')}
  function mount(options){
    const dialog=document.getElementById('prelive-workspace');
    const body=document.getElementById('prelive-workspace-content');
    const feedback=document.getElementById('prelive-workspace-feedback');
    let preparationId=null,snapshot=null,items=[],preview=null,busy=false,orderDirty=false;
    const notice=(text)=>{feedback.textContent=text;feedback.hidden=!text};
    const btn=(text,fn,cls)=>{const b=el('button',text,cls);b.type='button';b.addEventListener('click',()=>guard(fn));return b};
    async function guard(fn){if(busy)return;busy=true;body.setAttribute('aria-busy','true');try{await fn()}catch(e){notice(e.message)}finally{busy=false;body.removeAttribute('aria-busy')}}
    async function open(destination={}){
      if(!dialog.open)dialog.showModal();body.replaceChildren();snapshot=null;items=[];preview=null;orderDirty=false;notice('正在读取本场准备…');
      try{
        if(destination.preparation_id)preparationId=destination.preparation_id;
        else{
          const id=options.getConversationId();
          if(!id)throw Error('请先在右侧助手发起本场准备任务。');
          const result=await request('/api/assistant/v2/workspace?conversation_id='+encodeURIComponent(id));
          preparationId=result.voice_snapshot?.preparation?.ref?.id;
          if(!preparationId)throw Error('当前对话还没有本场准备。先进入直播间，再新建对话并说明准备要求。');
        }
        await reload();
      }catch(e){notice(e.message)}
    }
    async function reload(){
      snapshot=await query('get_workspace_snapshot',{preparation_id:preparationId});
      const prep=snapshot.preparation;
      const data=snapshot.preparation_details||{};
      items=structuredClone(data.playlist?.body?.items||data.scripts?.map(s=>({occurrence_id:'occ_'+s.ref.id,product_id:s.body.product_id,script_revision_ref:s.ref}))||[]);
      preview=null;orderDirty=false;render();notice('');
    }
    const prep=()=>snapshot.preparation;
    const data=()=>snapshot.preparation_details||{};
    function itemName(item){const script=(data().scripts||[]).find(s=>s.ref.id===item.script_revision_ref.id);const product=snapshot.products?.items?.find?.(p=>(p.ref?.id||p.id)===item.product_id);return (product?.body?.name||product?.name||'商品 '+item.product_id.slice(-6))+' · 第'+(script?.body.version_number||'?')+'版'}
    function renderOrder(list){
      list.replaceChildren();items.forEach((item,index)=>{
        const card=el('article',undefined,'prelive-order-card');card.draggable=true;card.dataset.occurrence=item.occurrence_id;card.tabIndex=0;
        const heading=el('div');heading.append(el('strong',String(index+1).padStart(2,'0')+'. '+itemName(item)),el('small',index%5===0?'长开场':'短引入'));
        const moves=el('div');moves.append(btn('↑',()=>move(index,index-1)),btn('↓',()=>move(index,index+1)));
        card.append(el('span','⠿','prelive-grip'),heading,moves);
        card.addEventListener('dragstart',e=>{if(busy){e.preventDefault();return}e.dataTransfer.setData('text/plain',item.occurrence_id);e.dataTransfer.effectAllowed='move'});
        card.addEventListener('dragover',e=>{if(!busy)e.preventDefault()});card.addEventListener('drop',e=>{e.preventDefault();if(busy)return;const from=items.findIndex(v=>v.occurrence_id===e.dataTransfer.getData('text/plain'));if(from>=0)move(from,index)});
        list.append(card);
      });
      function move(from,to){if(to<0||to>=items.length)return;items.splice(to,0,items.splice(from,1)[0]);preview=null;orderDirty=true;renderOrder(list);notice('顺序尚未保存；保存后重新计算第1、6、11项长开场。')}
    }
    async function reorder(mode){
      if(orderDirty&&!preview)throw Error('请先保存手动调整的顺序，再预览重排。');const productOrder=data().playlist?.body?.product_group_order||[...new Set(items.map(i=>i.product_id))];
      if(data().playlist){
        // Server preview is authoritative and returns a hash binding this exact base.
        const spec={mode,product_group_order:productOrder,within_product:'keep',seed:uuid().replace(/-/g,''),algorithm_version:'reorder-v1'};
        const result=await query('preview_playlist_reorder',{preparation_id:preparationId,ref:data().playlist.ref,spec});
        items=structuredClone(result.proposed_items);preview={spec:result.resolved_spec,hash:result.preview_hash};orderDirty=true;
      }else{
        const groups=productOrder.map(id=>items.filter(i=>i.product_id===id));
        if(mode==='grouped')items=groups.flat();
        else if(mode==='interleave'){items=[];for(let i=0;groups.some(g=>i<g.length);i++)groups.forEach(g=>{if(g[i])items.push(g[i])})}
        else for(let i=items.length-1;i>0;i--){const j=crypto.getRandomValues(new Uint32Array(1))[0]%(i+1);[items[i],items[j]]=[items[j],items[i]]}
        preview=null;
      }
      renderOrder(body.querySelector('.prelive-order'));notice('已预览重排，点击保存后才会生效。');
    }
    async function saveOrder(){
      const receipt=await command('save_playlist',{preparation_id:preparationId,expected_revision:prep().ref.revision,items,loop_mode:data().playlist?.body?.loop_mode||'infinite',product_group_order:preview?.spec?.product_group_order||data().playlist?.body?.product_group_order||[...new Set(items.map(i=>i.product_id))],reorder_spec:preview?.spec||null,preview_hash:preview?.hash||null});
      await command('compile_playback_plan',{preparation_id:preparationId,expected_revision:receipt.data.preparation_ref.revision,playlist_ref:receipt.data.playlist_ref});
      await reload();notice('顺序已保存并重新编译。若文案整包发生变化，需重新批准后生成语音。');options.onChange?.();
    }
    async function patch(script,key,module,textarea){
      const sentences=textarea.value.split('\n').map(x=>x.trim()).filter(Boolean);if(!sentences.length)throw Error('至少保留一句话术');
      await command('patch_script',{preparation_id:preparationId,expected_revision:prep().ref.revision,script_ref:script.ref,module_key:key,selected_text_hash:await hash(module.sentences),sentences});
      await reload();notice('该模块已保存为新修订，旧版保留；请让助手继续核对整包并申请语音批准。');options.onChange?.();
    }
    async function publicationAction(start){
      const ref=prep().body.candidate_publication_ref;
      const review=await query('create_review',{preparation_id:preparationId,ref});
      if(review.currentness?.blockers?.length){notice('存在待处理差异或停售信息，请先检查差异。');body.append(details('采用前差异',review.currentness));return}
      if(start&&!review.adopted)throw Error('请先采用本场准备，再开播');
      const args={publication_ref:ref,review_hash:review.review_hash,expected_room_revision:review.room_ref.revision};if(start)args.action_id=uuid();
      const receipt=await command(start?'start_prepared_room':'adopt_snapshot',args);
      await reload();options.onChange?.();
      if(start){dialog.close();await options.onStarted?.(prep().body.room_id,receipt)}else notice('已采用到直播间，尚未开播。');
    }
    async function revisionChoice(item,choice){
      await command('record_product_revision_choice',{preparation_id:preparationId,expected_revision:prep().ref.revision,pinned_product_ref:item.pinned_ref||item.pinned_product_ref,observed_head_ref:item.head_ref||item.observed_head_ref,diff_hash:item.diff_hash,choice});await reload();
    }
    function render(){
      body.replaceChildren();const p=prep().body;
      const h=el('div',undefined,'toolbar');h.append(el('strong','本场准备 · '+p.goal),btn('刷新',reload));body.append(h,el('p','商品正本保存在商品库；本场引用精确修订，未采用前不会改变直播间。','muted'));
      const meta=el('p','状态：'+(STATES[p.status]||p.status)+' · 修订 '+prep().ref.revision+' · 生成批次 '+p.generation_epoch,'muted');body.append(meta);
      const orderHead=el('div',undefined,'toolbar');const ops=el('div',undefined,'row');ops.append(btn('同商品连续',()=>reorder('grouped')),btn('商品穿插',()=>reorder('interleave')),btn('随机重排',()=>reorder('full_shuffle')),btn('保存顺序',saveOrder,'primary'));orderHead.append(el('h3','商品 AI 脚本 · '+items.length+'项'),ops);body.append(orderHead);
      const order=el('div',undefined,'prelive-order');renderOrder(order);body.append(order);
      if(!items.length)body.append(el('p','尚无本场话术。可在右侧让助手先生成或更新话术。','muted'));
      for(const script of data().scripts||[]){const box=el('details',undefined,'prelive-script');box.append(el('summary',itemName({product_id:script.body.product_id,script_revision_ref:script.ref})+' · 修订'+script.body.script_revision));
        for(const [key,module] of Object.entries(script.modules||{})){if(!module)continue;const section=el('section');section.append(el('h4',LABELS[key]||key));const text=el('textarea');text.rows=3;text.value=module.sentences.map(s=>s.text||s.spoken_text||'').join('\n');text.setAttribute('aria-label',(LABELS[key]||key)+'逐句话术');section.append(text,btn('保存此模块',()=>patch(script,key,module,text)));box.append(section)}body.append(box)}
      if(data().draft)body.append(details('本场配置',data().draft.body.draft));
      if(data().asset_checklist)body.append(details('演示素材准备清单',data().asset_checklist.body));
      const footer=el('div',undefined,'prelive-actions');
      if(p.candidate_publication_ref){footer.append(btn('检查并采用到直播间',()=>publicationAction(false),'primary'),btn('采用后开播',()=>publicationAction(true),'primary'),btn('查看采用前差异',async()=>{const review=await query('create_review',{ref:p.candidate_publication_ref});body.append(details('当前差异与选择',review.currentness));for(const diff of review.currentness?.differences||[]){if(diff.diff_hash&&(diff.pinned_ref||diff.pinned_product_ref)&&(diff.observed_head_ref||diff.head_ref)){const row=el('div');row.append(btn('保留旧版',()=>revisionChoice(diff,'keep_pinned')),btn('刷新到新版',()=>revisionChoice(diff,'refresh')));body.append(row)}}}));}
      else footer.append(el('p','话术、编排与语音准备完成后，助手会生成可采用的候选。采用与开播由你手动操作。','muted'));
      body.append(footer,details('本场引用与依赖（调试）',p));
    }
    return {open,request,query};
  }
  global.ShengboPreliveWorkspace={mount,request,query,hash};
})(window);
