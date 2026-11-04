/* Device IDs here are browser-origin IDs, never Windows endpoint IDs. */
(function(global){
  'use strict';
  const KEY='shengbo-audio-routing-v1';
  const fault=message=>Object.assign(new Error(message),{name:'AudioRouteError',code:'OUTPUT_DEVICE_UNAVAILABLE'});
  function receiver(label){
    const cable=/\bCABLE(?:-[A-D])?\s+Input\b/i.test(label||'');
    return {type:cable?'音频输入采集':'音频输出采集',name:cable?label.replace(/\bInput\b/i,'Output'):label};
  }
  function create(options){
    const audio=options.audio,media=options.mediaDevices||global.navigator?.mediaDevices;
    let storage;try{storage=options.storage||global.localStorage;}catch(_){}
    let pref={version:1,mode:'local',deviceId:'',label:''};
    try{const value=JSON.parse(storage?.getItem(KEY)||'null');if(value?.version===1&&['local','external'].includes(value.mode)&&typeof value.deviceId==='string'&&typeof value.label==='string')pref=value;}catch(_){}
    let devices=[],status=pref.mode==='external'?'checking':'local',message='',hostDevices=[],busy=false,disposed=false,revision=0,container=null,operation=null,refreshAfterApply=false,defaultEntry=null,hostDefaultSignature='';
    const supported=typeof audio.setSinkId==='function',outputs=new Set([audio]);
    async function setSinks(id){for(const node of outputs){if(node!==audio&&node.isConnected===false){outputs.delete(node);continue;}if(typeof node.setSinkId==='function')await node.setSinkId(id);}}
    function save(){try{storage?.setItem(KEY,JSON.stringify(pref));}catch(_){message='设备已切换，但浏览器无法保存选择，下次需重新选择。';}}
    function systemTarget(){
      const host=hostDevices.find(d=>d.default);
      if(host){const matches=devices.filter(d=>d.label===host.name);if(matches.length===1)return matches[0];return null;}
      if(defaultEntry?.groupId){const matches=devices.filter(d=>d.groupId===defaultEntry.groupId&&d.label);if(matches.length===1)return matches[0];}
      return null;
    }
    function getStatus(){
      const target=systemTarget(),label=pref.mode==='external'?pref.label:target?.label||'浏览器默认设备';
      const host=hostDevices.find(d=>d.name===label);
      return {mode:pref.mode,status,supported,deviceId:pref.mode==='external'?pref.deviceId:'',label,
        followsSystem:pref.mode==='local',resolvedDeviceId:pref.mode==='external'?pref.deviceId:target?.deviceId||'',
        defaultResolved:!!target,deviceMuted:host?.muted??null,message,busy:busy||!!operation,
        devices:devices.map(x=>({...x})),receiver:receiver(label)};
    }
    function emit(){if(disposed)return;render();options.onChange?.(getStatus());}
    function fail(messageText){status='unavailable';message=messageText;for(const node of outputs)node.pause();options.onFault?.(fault(messageText));emit();}
    async function apply(mode,device){
      if(busy)throw fault('正在切换输出设备，请稍候。');
      if(mode==='external'&&(!supported||!device?.deviceId||!device.label))throw fault('请先授权并选择一个可用的声音输出设备。');
      const wasPlaying=!audio.paused;busy=true;emit();audio.pause();
      const previous={...pref};
      try{
        if(supported)await setSinks(mode==='external'?device.deviceId:systemTarget()?.deviceId||'');
        else if(mode==='external')throw fault('此浏览器不支持选择输出设备，请用新版 Chrome 或 Edge 打开声播。');
        if(disposed)return;
        pref={version:1,mode,deviceId:mode==='external'?device.deviceId:previous.deviceId,label:mode==='external'?device.label:previous.label};
        status=mode==='external'?'ready':'local';message='';save();
        if(wasPlaying&&options.mayPlay?.()){
          try{await audio.play();}catch(_){message='输出设备已切换，请点击继续播出或重播当前句。';}
        }
      }catch(error){
        // A failed selection must never be presented as connected. Keep the
        // last committed route; no silent fallback from an external device.
        pref=previous;
        if(mode==='local'&&previous.mode==='external')fail('切回本机输出失败，原外部通道已暂停，请重新选择。');
        else if(previous.mode==='external')fail('输出设备切换失败，已暂停；请重新选择可用设备。');
        else{status='unavailable';message='没有切换输出：'+(error.name==='NotAllowedError'?'未获得设备权限。':error.message||'设备不可用。');if(wasPlaying&&options.mayPlay?.())await audio.play().catch(()=>{});}
        throw fault(message);
      }finally{busy=false;emit();if(refreshAfterApply&&!disposed){refreshAfterApply=false;await refresh();}}
    }
    async function refresh(){
      const token=++revision;
      try{
        const list=media?.enumerateDevices?await media.enumerateDevices():[];
        if(disposed||token!==revision)return;
        defaultEntry=list.find(d=>d.kind==='audiooutput'&&d.deviceId==='default')||null;
        devices=list.filter(d=>d.kind==='audiooutput'&&d.deviceId&&!['default','communications'].includes(d.deviceId)).map(d=>({deviceId:d.deviceId,label:d.label||'',groupId:d.groupId||''}));
        if(busy)refreshAfterApply=true;
        if(pref.mode==='external'&&!busy){
          let selected=devices.find(d=>d.deviceId===pref.deviceId&&d.label);
          // Browsers can rotate origin IDs. Recover by name only if unambiguous.
          if(!selected&&pref.label){const matches=devices.filter(d=>d.label===pref.label);if(matches.length===1)selected=matches[0];}
          if(!selected)fail('已选输出设备未连接或未授权，声音已暂停；请重新选择设备，或切回本机播放。');
          else if(audio.sinkId!==selected.deviceId||status!=='ready')await apply('external',selected);
        }
        if(pref.mode==='local'&&!busy&&supported){const wanted=systemTarget()?.deviceId||'';if(audio.sinkId!==wanted||status==='unavailable')await apply('local');}
        emit();
      }catch(error){
        if(disposed||token!==revision)return;
        if(pref.mode==='external')fail('无法读取已选声音设备，已暂停。请检查浏览器设备权限后重试。');
        else{message='暂时无法读取设备列表，本机默认播放仍可使用。';emit();}
      }
    }
    const ready=refresh();
    async function ensureReady(){
      await ready;if(operation)await operation;
      if(status==='unavailable')throw fault(message||'播放设备未就绪，请重新选择。');
      if(pref.mode==='external'&&(status!=='ready'||!supported||audio.sinkId!==pref.deviceId))throw fault('外部声音通道尚未就绪；请重新选择设备或切回本机播放。');
    }
    async function authorize(){
      if(busy)return;
      try{
        if(media?.selectAudioOutput){const selected=await media.selectAudioOutput();await apply('external',{deviceId:selected.deviceId,label:selected.label});await refresh();return;}
        if(!media?.getUserMedia)throw fault('此浏览器不支持设备授权，请使用新版 Chrome 或 Edge。');
        // Chromium requires microphone permission to expose all output labels.
        // No stream is connected to the app or uploaded; stop all tracks at once.
        const stream=await media.getUserMedia({audio:true,video:false});
        stream.getTracks().forEach(track=>track.stop());await refresh();
        message='设备列表已更新，请点选声音要发送到的设备。';emit();
      }catch(error){message=error.name==='NotAllowedError'?'设备选择未获授权，原输出设置保留；可切回本机播放。':error.message||'设备读取失败';emit();}
    }
    function run(task){if(operation)return operation;const current=Promise.resolve().then(task);operation=current;emit();current.catch(error=>{message=error.message;emit();}).finally(()=>{if(operation===current)operation=null;emit();if(refreshAfterApply&&!busy&&!disposed){refreshAfterApply=false;run(refresh).catch(()=>{});}});return current;}
    function element(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
    function render(){
      if(!container)return;
      const state=getStatus();container.dataset.routeStatus=status;
      const select=container.querySelector('[data-route-select]');
      const host=hostDevices.find(d=>d.default),signature=JSON.stringify([devices,state.busy,pref.mode,pref.deviceId,host?.name]);
      if(select.dataset.signature!==signature){
        select.dataset.signature=signature;select.replaceChildren();
        const automatic=element('option','跟随系统默认（推荐）'+(host?.name?' · '+host.name:''));automatic.value='';select.append(automatic);
        for(const d of devices.filter(d=>d.label)){const option=element('option',d.label);option.value=d.deviceId;select.append(option);}
        if(pref.mode==='external'&&!devices.some(d=>d.deviceId===pref.deviceId&&d.label)){const unavailable=element('option',pref.label+'（未连接）');unavailable.value=pref.deviceId;select.append(unavailable);}
        select.value=pref.mode==='external'?pref.deviceId:'';
      }
      select.disabled=state.busy||!supported;
      const fallback=pref.mode==='local'&&!state.defaultResolved&&host?'默认设备待匹配。可读取设备列表。':'';
      container.querySelector('[data-route-state]').textContent=state.busy?'正在切换播放设备…':message||(status==='unavailable'?'播放设备未就绪，已暂停。':fallback||(pref.mode==='local'?'跟随系统默认 · '+state.label:'已选择 · '+state.label))+(state.deviceMuted?'；该设备在 Windows 中已静音，请取消静音。':'');
      const auth=container.querySelector('[data-route-authorize]');auth.disabled=state.busy||!supported;
      auth.textContent=media?.selectAudioOutput?'选择其他设备':'读取设备列表';
      container.querySelector('[data-route-refresh]').disabled=state.busy;
      container.querySelector('[data-route-permission]').textContent=media?.selectAudioOutput?'可通过系统选择器授权其他输出设备。':'若设备列表不全，点击“读取设备列表”；浏览器可能请求麦克风权限，仅用于显示设备名，读取后立即关闭，不录音、不上传。';
      const r=state.receiver;
      container.querySelector('[data-route-guide]').textContent=pref.mode==='local'?'OBS“桌面音频”也选择“默认”，或明确选择与这里相同的设备。':r.type==='音频输入采集'?'OBS 添加“音频输入采集”，选择“'+r.name+'”。':'OBS“桌面音频”或“音频输出采集”选择“'+state.label+'”。';
      container.querySelector('[data-route-unsupported]').hidden=supported;
    }
    function mount(target){
      container=target;container.replaceChildren();
      const label=element('label','播放设备','wb-route-label'),select=element('select',undefined,'wb-route-select');
      select.dataset.routeSelect='';select.setAttribute('aria-label','播放设备');label.append(select);
      select.onchange=()=>{const id=select.value;run(()=>id?apply('external',devices.find(d=>d.deviceId===id)):apply('local'));};
      const auth=element('button','读取设备列表','wb-button');auth.type='button';auth.dataset.routeAuthorize='';auth.onclick=()=>run(authorize);
      const reload=element('button','刷新设备','wb-button');reload.type='button';reload.dataset.routeRefresh='';reload.onclick=()=>run(refresh);
      const row=element('div',undefined,'wb-route-mode-row');row.append(label,auth,reload);
      const state=element('p',undefined,'wb-route-state');state.dataset.routeState='';state.setAttribute('role','status');
      const guide=element('p',undefined,'wb-route-guide');guide.dataset.routeGuide='';
      const detail=element('details',undefined,'wb-route-config');detail.append(element('summary','设备权限说明'));
      const permission=element('small');permission.dataset.routePermission='';detail.append(permission);
      const unsupported=element('p','此浏览器不支持指定播放设备，将使用浏览器默认输出；可用新版 Chrome 或 Edge 打开声播选择设备。','wb-inline-alert');unsupported.dataset.routeUnsupported='';
      container.append(row,state,guide,detail,unsupported);render();return controller;
    }
    const changed=()=>{refresh().catch(()=>{});};media?.addEventListener?.('devicechange',changed);
    const controller={ready,ensureReady,refresh,authorize,setLocal:()=>run(()=>apply('local')),selectDevice:id=>{const device=devices.find(d=>d.deviceId===id);return run(()=>apply('external',device));},getStatus,mount,async registerMedia(node){await ready;if(operation)await operation;if(disposed)return;outputs.add(node);if(typeof node.setSinkId==='function')await node.setSinkId(pref.mode==='external'?pref.deviceId:systemTarget()?.deviceId||'');},setHostDevices(list){
      hostDevices=list||[];const host=hostDevices.find(d=>d.default),signature=JSON.stringify([host?.id,host?.name]);
      if(signature!==hostDefaultSignature){hostDefaultSignature=signature;if(pref.mode==='local'){if(operation||busy)refreshAfterApply=true;else run(refresh).catch(()=>{});}}
      render();
    },dispose(){disposed=true;outputs.clear();media?.removeEventListener?.('devicechange',changed);}};
    return controller;
  }
  global.ShengboAudioRouting={create,receiver};
})(typeof window==='undefined'?globalThis:window);
