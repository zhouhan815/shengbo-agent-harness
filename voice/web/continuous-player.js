/* One physical audio source, many logical sentence bookmarks. */
class ContinuousTrackPlayer {
  static createContext(){
    const Context=globalThis.AudioContext||globalThis.webkitAudioContext;
    if(!Context)throw Error('当前浏览器不支持连续音频调度，请使用新版 Chrome 或 Edge');
    // This context renders into a MediaStream, never into a physical speaker.
    // A silent sink keeps its clock independent of Bluetooth/default-device resets.
    try{return new Context({sinkId:{type:'none'}});}catch(_){return new Context();}
  }
  now(){return typeof performance!=='undefined'?performance.now():Date.now();}
  configureOutput(){
    this.directOutput=typeof this.context.setSinkId==='function';
    this.destination=this.directOutput?this.context.destination:this.context.createMediaStreamDestination();
    this.gain=this.context.createGain?.();this.analyser=this.context.createAnalyser?.();
    if(this.analyser){this.analyser.fftSize=256;this.analyser.connect(this.destination);}
    if(this.gain)this.gain.connect(this.analyser||this.destination);
    this.output=this.gain||this.analyser||this.destination;
    const engine=this;
    this.outputRoute={get isConnected(){return !engine.closed;},pause(){engine.pause();},
      async setSinkId(id){if(engine.directOutput&&!engine.closed&&engine.context.sinkId!==id)await engine.context.setSinkId(id);}};
  }
  signalPeak(){if(!this.analyser)return null;const values=new Float32Array(this.analyser.fftSize);this.analyser.getFloatTimeDomainData(values);let peak=0;for(const value of values)peak=Math.max(peak,Math.abs(value));return peak;}
  diagnostic(){return {output_mode:this.directOutput?'web_audio_direct':'media_stream',graph_peak:this.signalPeak(),media_muted:!!this.media.muted,media_volume:this.media.volume??1,context_state:this.context.state||'unknown',context_time:this.context.currentTime,
    media_paused:!!this.media.paused,media_time:Number(this.media.currentTime)||0,
    media_ready_state:Number(this.media.readyState)||0,silent_clock:this.context.sinkId?.type==='none'};}
  tick(){
    const p=this.physical;if(this.closed||!p?.running||['paused','blocked','stopped','completed','error'].includes(this.view?.status))return;
    const wall=this.now(),clock=this.context.currentTime;
    if(clock>p.lastClock+.005){p.lastClock=clock;p.lastAdvanceWall=wall;}
    if(this.gain)this.gain.gain.value=this.media.muted?0:(this.media.volume??1);
    if(!p.confirmed&&clock>p.clock+.005&&(this.directOutput||!this.media.paused)){
      p.confirmed=true;this.trace('continuous_clock_confirmed',{offset_seconds:this.position(),...this.diagnostic()});
      this.ack?.('playing');
    }
    if(wall-p.lastAdvanceWall>3000||(!p.confirmed&&wall-p.startedWall>3000)){
      this.trace('continuous_clock_stalled',{offset_seconds:this.position(),...this.diagnostic()});
      this.clockFailed=true;this.pause();const error=Error('音频时钟未推进，已暂停以避免假播放。请点击“开启本机声音”重新连接。');error.name='AudioClockError';this.fail(error);return;
    }
    if(p.confirmed&&this.view?.status==='playing'&&wall-(this.lastProgressWall||0)>=350){
      this.lastProgressWall=wall;this.ack?.('progress',{position:Math.max(0,Math.min(this.end-this.begin,this.position()-this.begin))});
    }
  }

  constructor(media, beforePlay, trace, fail, primedContext=null) {
    const Context=globalThis.AudioContext||globalThis.webkitAudioContext;
    if(!Context)throw Error('当前浏览器不支持连续音频调度，请使用新版 Chrome 或 Edge');
    this.media=media;this.beforePlay=beforePlay;this.trace=trace;this.fail=fail;
    this.context=primedContext||ContinuousTrackPlayer.createContext();this.configureOutput();
    this.cache=new Map();this.revision=0;this.closed=false;this.physical=null;this.logical=null;
    this.progress=setInterval(()=>this.tick(),100);
  }
  async unlock(gesture=false){
    if(gesture&&(this.clockFailed||(!this.physical&&this.context.state==='suspended'))){
      this.pause();this.physical=null;this.logical=null;this.clockFailed=false;
      const Context=globalThis.AudioContext||globalThis.webkitAudioContext;
      this.context.close().catch(()=>{});this.context=ContinuousTrackPlayer.createContext();this.configureOutput();
    }
    const resumed=this.context.resume();await this.beforePlay();
    if(this.directOutput){const target=this.media.sinkId||'';if(this.context.sinkId!==target)await this.context.setSinkId(target);}
    if(this.context.state==='suspended'){
      let timer;
      try{await Promise.race([resumed,new Promise((_,reject)=>{timer=setTimeout(()=>{const error=Error('连续音频等待浏览器声音授权（'+this.context.state+'），请再次点击启用声音');error.name='NotAllowedError';reject(error);},gesture?5000:500);})]);}
      finally{clearTimeout(timer);}
    }else await resumed;
    if(this.closed)return;
    if(this.directOutput)return;
    if(this.media.srcObject!==this.destination.stream)this.media.srcObject=this.destination.stream;
    // A MediaStream play() promise may wait for its FIRST produced sample.
    // Waiting for it before starting the source would deadlock both sides.
    if(this.media.paused)this.media.play().catch(error=>{if(!this.closed&&error.name!=='AbortError'&&!['paused','stopped','completed'].includes(this.view?.status)){this.pause();this.fail(error);}});
  }
  position(){const p=this.physical;return p?p.offset+(p.running?this.context.currentTime-p.clock:0):0;}
  load(url){
    if(!/^\/(?:audio\/[a-f0-9]{64}\.wav|room-audio\/[A-Za-z0-9_-]+\/[a-f0-9]{64}\.wav)$/.test(url))return Promise.reject(Error('连续音频地址无效'));
    if(!this.cache.has(url)){
      const pending=fetch(url).then(r=>{if(!r.ok)throw Error('连续音频读取失败');return r.arrayBuffer();}).then(bytes=>this.context.decodeAudioData(bytes));
      this.cache.set(url,pending);pending.catch(()=>this.cache.delete(url));
      if(this.cache.size>3){const old=[...this.cache.keys()].find(key=>key!==url&&key!==this.physical?.url);if(old)this.cache.delete(old);}
    }
    return this.cache.get(url);
  }
  cancelAlarm(){if(this.alarm){this.alarm.onended=null;try{this.alarm.stop();}catch(_){}this.alarm.disconnect();this.alarm=null;}}
  halt(){this.cancelAlarm();const p=this.physical;if(p){const offset=this.position();p.node.onended=null;try{p.node.stop();}catch(_){}p.node.disconnect();p.offset=offset;p.running=false;}this.cut=null;}
  pause(){++this.revision;this.halt();this.media.pause();}
  dispose(){this.closed=true;clearInterval(this.progress);this.pause();this.cache.clear();this.media.srcObject=null;this.context.close().catch(()=>{});}
  finish(epoch){
    if(this.closed||this.view?.epoch!==epoch||this.view.status==='paused'||this.finished===epoch)return;
    this.finished=epoch;this.trace('continuous_sentence_boundary',{boundary_offset_seconds:this.end,planned_interaction:!!(this.cut?.epoch===epoch)});
    this.ack('ended',{boundary_choice:this.cut?.epoch===epoch?this.cut.queueId:null});
  }
  arm(epoch){
    this.cancelAlarm();const remaining=this.end-this.position();
    if(remaining<=0){queueMicrotask(()=>this.finish(epoch));return;}
    const alarm=this.context.createBufferSource();alarm.buffer=this.context.createBuffer(1,1,this.context.sampleRate);
    // Silent scheduled audio event, not a background-throttled timeupdate poll.
    alarm.connect(this.output);alarm.onended=()=>{alarm.disconnect();if(this.alarm===alarm)this.alarm=null;this.finish(epoch);};
    this.alarm=alarm;alarm.start(this.context.currentTime+remaining);
  }
  async prepareCut(s){
    const pending=s.insertion?.pending,track=s.audio?.continuous_track;
    if(!track||!pending?.prepared_audio||!this.physical?.running||this.cut)return;
    const epoch=s.epoch,id=pending.queue_id;
    try{await this.load(pending.prepared_audio.url);}catch(_){return;}
    if(this.closed||this.view?.epoch!==epoch||this.view.insertion?.pending?.queue_id!==id||!this.physical?.running||this.cut)return;
    const remaining=this.end-this.position();if(remaining<.10)return;
    this.cut={epoch,queueId:id};this.physical.node.stop(this.context.currentTime+remaining);
    this.trace('continuous_interaction_scheduled',{boundary_offset_seconds:this.end});
  }
  async sync(s,ack){
    this.view=s;this.ack=ack;
    if(s.status==='paused'||s.status==='blocked'){this.pause();return;}
    if(!s.audio){if(this.logical&&this.logical.seekRevision!==(s.playback_seek_revision||0)){++this.revision;this.halt();}return;}
    const url=s.audio.url,timing=s.audio.continuous_track;
    const begin=timing?.start||0,end=timing?.end||s.audio.duration;
    const follows=timing&&this.logical?.url===url&&Math.abs(this.logical.end-begin)<.001;
    this.begin=begin;this.end=end;
    if(this.logical?.epoch===s.epoch&&this.physical?.running){this.prepareCut(s);return;}
    const revision=++this.revision;
    try{
      const buffer=await this.load(url);if(this.closed||revision!==this.revision||this.view?.epoch!==s.epoch)return;
      await this.unlock();if(this.closed||revision!==this.revision)return;
      const keep=!!timing&&this.logical?.seekRevision===(s.playback_seek_revision||0)&&this.physical?.url===url&&this.physical.running&&
        ((this.position()>=begin-.05&&this.position()<end)||(follows&&this.position()>=end));
      if(!keep){
        const pausedOffset=this.physical?.url===url&&!this.physical.running&&this.logical?.epoch===s.epoch?this.physical.offset:null;
        this.halt();const node=this.context.createBufferSource();node.buffer=buffer;node.connect(this.output);
        const offset=Math.min(buffer.duration-.001,Math.max(begin,pausedOffset??(begin+(Number(s.position)||0))));
        const physical={url,node,offset,clock:this.context.currentTime,running:true,confirmed:false,lastClock:this.context.currentTime,lastAdvanceWall:this.now(),startedWall:this.now()};this.physical=physical;
        node.onended=()=>{if(this.physical===physical){physical.offset=this.position();physical.running=false;}};
        node.start(physical.clock,offset);this.trace('continuous_source_started',{offset_seconds:offset,...this.diagnostic()});
      }else this.trace('continuous_source_retained',{offset_seconds:this.position()});
      this.logical={url,epoch:s.epoch,end,seekRevision:s.playback_seek_revision||0};this.finished=null;
      if(this.physical.confirmed)this.ack('playing');this.arm(s.epoch);this.prepareCut(this.view);
    }catch(error){if(!this.closed&&revision===this.revision)this.fail(error);}
  }
}
