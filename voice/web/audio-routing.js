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
    let devices=[],status=pref.mode==='external'?'checking':'local',message='',hostDevices=[],busy=false,disposed=false,revision=0,container=null,operation=null,refreshAfterApply=false;
    const supported=typeof audio.setSinkId==='function';
    function save(){try{storage?.setItem(KEY,JSON.stringify(pref));}catch(_){message='设备已切换，但浏览器无法保存选择，下次需重新选择。';}}
    function getStatus(){return {mode:pref.mode,status,supported,deviceId:pref.mode==='external'?pref.deviceId:'',label:pref.mode==='external'?pref.label:'浏览器默认设备',message,busy:busy||!!operation,devices:devices.map(x=>({...x})),receiver:receiver(pref.label)};}
    function emit(){if(disposed)return;render();options.onChange?.(getStatus());}
    function fail(messageText){status='unavailable';message=messageText;audio.pause();options.onFault?.(fault(messageText));emit();}
    async function apply(mode,device){
      if(busy)throw fault('正在切换输出设备，请稍候。');
      if(mode==='external'&&(!supported||!device?.deviceId||!device.label))throw fault('请先授权并选择一个可用的声音输出设备。');
      const wasPlaying=!audio.paused;busy=true;emit();audio.pause();
      const previous={...pref};
      try{
        if(supported)await audio.setSinkId(mode==='external'?device.deviceId:'');
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
        else{status='local';message='没有切换输出：'+(error.name==='NotAllowedError'?'未获得设备权限。':error.message||'设备不可用。');if(wasPlaying&&options.mayPlay?.())await audio.play().catch(()=>{});}
        throw fault(message);
      }finally{busy=false;emit();if(refreshAfterApply&&!disposed){refreshAfterApply=false;await refresh();}}
    }
    async function refresh(){
      const token=++revision;
      try{
        const list=media?.enumerateDevices?await media.enumerateDevices():[];
        if(disposed||token!==revision)return;
        devices=list.filter(d=>d.kind==='audiooutput'&&d.deviceId&&!['default','communications'].includes(d.deviceId)).map(d=>({deviceId:d.deviceId,label:d.label||''}));
        if(busy)refreshAfterApply=true;
        if(pref.mode==='external'&&!busy){
          let selected=devices.find(d=>d.deviceId===pref.deviceId&&d.label);
          // Browsers can rotate origin IDs. Recover by name only if unambiguous.
          if(!selected&&pref.label){const matches=devices.filter(d=>d.label===pref.label);if(matches.length===1)selected=matches[0];}
          if(!selected)fail('已选输出设备未连接或未授权，声音已暂停；请重新选择设备，或切回本机播放。');
          else if(audio.sinkId!==selected.deviceId||status!=='ready')await apply('external',selected);
        }
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
    function run(task){if(operation)return operation;const current=Promise.resolve().then(task);operation=current;emit();current.catch(error=>{message=error.message;emit();}).finally(()=>{if(operation===current)operation=null;emit();});return current;}
    function element(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
    function render(){
      if(!container)return;
      const state=getStatus(),local=container.querySelector('[data-route-local]'),external=container.querySelector('[data-route-external]');
      local.setAttribute('aria-pressed',String(pref.mode==='local'));external.setAttribute('aria-pressed',String(pref.mode==='external'));
      local.disabled=external.disabled=state.busy;container.dataset.routeStatus=status;
      container.querySelector('[data-route-state]').textContent=state.busy?'正在更新声音设备…':message||(pref.mode==='external'?(status==='ready'?'外部输出已启用：'+pref.label+'；OBS 接收端待验证。':'外部通道未就绪，已暂停。'):'本机播放；未启用外部设备输出。');
      const list=container.querySelector('[data-route-devices]');
      const signature=JSON.stringify([devices,state.busy,pref.mode,pref.deviceId]);
      if(list.dataset.signature!==signature){list.dataset.signature=signature;list.replaceChildren();for(const d of devices.filter(x=>x.label)){
        const b=element('button',d.label,'wb-route-device');b.type='button';b.dataset.routeDevice=d.deviceId;b.disabled=state.busy||!supported;b.setAttribute('aria-pressed',String(pref.mode==='external'&&pref.deviceId===d.deviceId));b.onclick=()=>run(()=>apply('external',d));list.append(b);
      }if(!list.childElementCount)list.append(element('p','设备名称尚未开放，请使用下面的授权按钮读取。','wb-muted'));}
      const auth=container.querySelector('[data-route-authorize]');auth.disabled=state.busy||!supported;auth.textContent=media?.selectAudioOutput?'打开系统设备选择器':'授权读取完整设备列表';
      container.querySelector('[data-route-refresh]').disabled=state.busy;
      container.querySelector('[data-route-permission]').textContent=media?.selectAudioOutput?'选择器只用于选择声音输出设备。':'Chrome / Edge 首次可能请求麦克风权限以显示设备名；只在你点击后请求，读取后立即关闭，不录音、不上传。';
      const cableInstalled=[...devices,...hostDevices.map(d=>({label:d.name}))].some(d=>/\bCABLE(?:-[A-D])?\s+Input\b/i.test(d.label||''));
      container.querySelector('[data-route-driver-hint]').textContent=cableInstalled?'已发现 VB-CABLE。选择 CABLE Input 后，OBS 添加“音频输入采集”，设备选择 CABLE Output。':'可使用已有扬声器供 OBS 采集。若需要独立虚拟麦克风，可首次安装 VB-CABLE，安装后刷新设备。';
      const guide=container.querySelector('[data-route-guide]');guide.hidden=pref.mode!=='external'||status!=='ready';
      if(!guide.hidden){const r=receiver(pref.label);guide.querySelector('[data-route-receiver]').textContent='OBS → 来源 ＋ → '+r.type+' → 设备选择“'+r.name+'”。';guide.querySelector('[data-route-receiver-name]').textContent=r.name;}
      const unsupported=container.querySelector('[data-route-unsupported]');unsupported.hidden=supported;
    }
    function mount(target){
      container=target;container.replaceChildren();const heading=element('strong','音频连接（可选）');
      const modes=element('div',undefined,'wb-route-modes');
      const local=element('button','不连接 · 本机播放','wb-route-mode'),external=element('button','连接 OBS / 直播伴侣','wb-route-mode');local.type=external.type='button';local.dataset.routeLocal='';external.dataset.routeExternal='';
      const config=element('details');config.className='wb-route-config';config.append(element('summary','选择输出设备与接入方法'));
      local.onclick=()=>run(()=>apply('local'));
      external.onclick=()=>{config.open=true;const old=devices.find(d=>d.deviceId===pref.deviceId&&d.label);if(old)run(()=>apply('external',old));else{message='请在下面选一个输出设备，选定后启用外部输出。';emit();run(refresh);}};
      modes.append(local,external);const state=element('p',undefined,'wb-route-state');state.dataset.routeState='';state.setAttribute('role','status');
      const list=element('div',undefined,'wb-route-device-list');list.dataset.routeDevices='';
      const actions=element('div',undefined,'wb-row');const auth=element('button',undefined,'wb-button');auth.type='button';auth.dataset.routeAuthorize='';auth.onclick=()=>run(authorize);
      const reload=element('button','刷新设备','wb-button');reload.type='button';reload.dataset.routeRefresh='';reload.onclick=()=>run(refresh);actions.append(auth,reload);
      const permission=element('small');permission.dataset.routePermission='';
      const hint=element('p',undefined,'wb-muted');hint.dataset.routeDriverHint='';const install=element('a','VB-CABLE 官方安装说明');install.href='https://vb-audio.com/Cable/';install.target='_blank';install.rel='noopener noreferrer';
      const unsupported=element('p','当前浏览器不支持指定输出设备，请用新版 Chrome 或 Edge 打开声播。本机默认播放可继续使用。','wb-inline-alert');unsupported.dataset.routeUnsupported='';
      config.append(list,permission,actions,hint,install,element('p','想单独开关声播，请使用专用输出设备，并让 OBS 只采集该音源；如果 OBS 还在采集本机扬声器，本机播放也可能被收进去。','wb-muted'),unsupported);
      const guide=element('div',undefined,'wb-route-guide');guide.dataset.routeGuide='';const receiverText=element('p');receiverText.dataset.routeReceiver='';const name=element('code');name.dataset.routeReceiverName='';guide.append(receiverText,name,element('small','播放声播后查看 OBS 该音源电平。不要同时重复采集同一设备，以免重音；OBS 需首次手动添加音源并保存场景。'));
      const modeRow=element('div',undefined,'wb-route-mode-row');modeRow.append(heading,modes);
      container.append(modeRow,state,guide,config);render();return controller;
    }
    const changed=()=>{refresh().catch(()=>{});};media?.addEventListener?.('devicechange',changed);
    const controller={ready,ensureReady,refresh,authorize,setLocal:()=>run(()=>apply('local')),selectDevice:id=>{const device=devices.find(d=>d.deviceId===id);return run(()=>apply('external',device));},getStatus,mount,setHostDevices(list){hostDevices=list||[];render();},dispose(){disposed=true;media?.removeEventListener?.('devicechange',changed);}};
    return controller;
  }
  global.ShengboAudioRouting={create,receiver};
})(typeof window==='undefined'?globalThis:window);
