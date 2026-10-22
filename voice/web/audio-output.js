/* Present observations without treating a connected socket as audible sound. */
(function(global){
  'use strict';
  function describe(s={},media={},diagnostic={}){
    const labels={paused:'已暂停',playing:'正在播出',ready:'等待播放',preparing:'准备音频',buffering:'缓冲中',blocked:'声音被拦截',error:'播出异常',stopped:'未播出',completed:'本轮已完成'};
    const localOwns=!!media.ownsPlayback;
    let local='本页尚未连接声音';
    if(s.output_source==='obs'&&s.connected_player)local='声音由 OBS 输出';
    else if(s.status==='paused')local='语音已暂停，当前不会输出声音';
    else if(localOwns){
      local=media.muted||media.volume===0?'本页播放器已静音':media.paused?'本页播放器未播放':media.hasAudio?'本页正在播放语音':'本页等待音频';
    }else if(s.connected_player)local='声音来源在其他窗口，本页仅显示状态';
    const sessions=diagnostic.browser_sessions||[],active=sessions.filter(x=>x.active);
    const candidates=active.length?active:sessions;
    const names=[...new Set(candidates.map(x=>x.device_name).filter(Boolean))];
    const fallback=(diagnostic.devices||[]).find(x=>x.default);
    const device=names.length===1?names[0]:names.length>1?'检测到多个浏览器输出设备，展开详情核对':fallback?'Windows 默认：'+fallback.name:'正在检测输出设备…';
    const peak=Math.max(0,...active.map(x=>Number(x.peak)||0));
    const recommendation=diagnostic.stale?'设备检测已过期，请等待重新读取':names.length===1?'直播伴侣“主扬声器”请选择同名设备':names.length>1?'多个浏览器可能同时发声，请按下方会话列表核对':diagnostic.available?'尚未检测到浏览器会话；播放后自动更新':diagnostic.message||'设备名称由本机 Windows 只读检测，不会修改系统设置';
    return {status:labels[s.status]||'等待播出',local,
      meta:s.current?`${s.current.product_name||'商品'} · ${s.current.version||'版本'} · 第 ${s.current.ordinal||'插播'} / ${s.current.total_sentences||'—'} 句`:'尚未播放商品',
      text:s.current?.text||'选择本场并开始播出后，这里自动显示当前口播。',
      device,recommendation,peak,
      signal:diagnostic.stale?'声音信号检测已过期':peak>.001?'电脑端检测到浏览器声音信号（不代表伴侣已采集）':s.status==='paused'?'已暂停，当前没有声播信号':diagnostic.available?'本次采样未检测到浏览器声音，请检查是否暂停或静音':'等待声音信号检测'};
  }
  global.ShengboAudioOutput={describe};
})(typeof window==='undefined'?globalThis:window);
