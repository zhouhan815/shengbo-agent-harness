// One persistent media element: the user's gesture unlocks the same element
// that will play every later sentence (including after async preparation).
class VoicePlayer {
  constructor(audio, report = () => {}, options = {}) {
    this.audio = audio; this.report = report; this.ws = null;
    this.state = null; this.identity = null; this.epoch = -1;
    this.enabled = false; this.starting = false; this.generation = 0;
    this.closed = true;
    this.beforePlay = options.beforePlay || (() => {});
    this.onPlaybackEvent = options.onPlaybackEvent || (() => {});
    this.playbackDiagnostics = []; this.eventSequence = 0; this.completedAttempt = null;
  }
  clock() { return typeof performance !== 'undefined' && typeof performance.now === 'function' ? performance.now() : Date.now(); }
  trace(event, extra = {}) {
    const current=this.state?.current, detail={schema_version:'PLAYBACK-BOUNDARY-1',sequence:++this.eventSequence,event,
      observed_at_ms:Date.now(),monotonic_ms:this.clock(),room_id:this.roomId||null,session_id:this.state?.session_id||null,
      epoch:this.state?.epoch??this.epoch,attempt_id:current?.attempt_id||null,sentence_id:current?.id||null,queue_id:current?.queue_id||null,
      entry_kind:current?.is_welcome?'welcome':current?.is_insertion?'insertion':'script',status:this.state?.status||null,
      position_seconds:Number(this.audio.currentTime)||0,...extra};
    // No text, nickname, URL, authorization or unbounded result body is recorded.
    this.playbackDiagnostics.push(detail);if(this.playbackDiagnostics.length>300)this.playbackDiagnostics.shift();
    if(event.startsWith('continuous_')&&this.ws?.readyState===1&&this.identity===this.state?.player_id){
      this.ws.send(JSON.stringify({type:'playback_diagnostic',event,epoch:this.state.epoch,attempt_id:current?.attempt_id,
        offset_seconds:extra.offset_seconds??extra.boundary_offset_seconds??null}));
    }
    try { this.onPlaybackEvent({...detail}); } catch (_) { /* Diagnostics cannot interrupt playback. */ }
    try { if(typeof CustomEvent==='function'&&typeof this.audio.dispatchEvent==='function')this.audio.dispatchEvent(new CustomEvent('shengbo:playback',{detail:{...detail},bubbles:true})); } catch (_) {}
    return detail;
  }
  getDiagnostics() { return this.playbackDiagnostics.map(item=>({...item})); }
  async unlock() {
    try {
      if(this.continuousEngine){await this.continuousEngine.unlock();this.enabled=true;return;}
      await this.beforePlay();
      if(this.completedAttempt?.attempt===this.state?.current?.attempt_id&&this.completedAttempt?.epoch===this.state?.epoch&&this.audio.ended&&this.state?.status==='paused'){this.enabled=true;this.trace('unlock_completed_attempt_without_replay');return;}
      if (!this.welcomeMayPlay()) this.reset();
      if (!this.state?.current || ['stopped','completed','error'].includes(this.state.status)) this.reset();
      if (!this.audio.getAttribute('src')) this.audio.src = '/assets/unlock.wav';
      await this.audio.play();
      if (!this.state?.current || this.state.status === 'paused') this.audio.pause();
      this.enabled = true;
      this.report('声音已开启，等待开始直播或待播音频就绪');
    } catch (error) {
      this.report(error.code === 'OUTPUT_DEVICE_UNAVAILABLE' ? error.message : '声音未开启，请再次点击开启声音；检查浏览器标签页是否静音');
      throw error;
    }
  }
  connect(roomId, source = 'browser') {
    if (this.ws?.readyState === WebSocket.OPEN && this.roomId === roomId) return Promise.resolve();
    this.disconnect(); this.closed = false; this.roomId = roomId; this.source = source;
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws?role=player&room=${encodeURIComponent(roomId)}&source=${source}&delta=1`);
      this.ws = ws;
      let settled=false;const settle=(error)=>{if(settled)return;settled=true;clearTimeout(timer);error?reject(error):resolve();};
      const timer = setTimeout(() => {this.trace('channel_timeout');settle(Error('声音通道连接超时，请重试'));}, 8000);
      ws.onmessage = e => {
        if (this.ws !== ws) return;
        const m = JSON.parse(e.data);
        if (m.type === 'welcome') { this.identity = m.id;this.trace('channel_connected');settle(); }
        if (m.type === 'state' || m.type === 'state_delta') { const state=m.type==='state_delta'?{...this.state,...m.state}:m.state;this.sync(state); if (this.onState) this.onState(state); }
      };
      ws.onerror = () => { this.trace('channel_error');settle(Error('声音通道连接失败')); };
      ws.onclose = () => {
        settle(Error('声音通道已断开，请重试连接'));
        if (this.ws !== ws || this.closed) return;
        this.trace('channel_closed');
        this.reset(); this.report('声音通道已断开，请点击开启声音重新连接');
        setTimeout(() => {
          if (!this.closed && this.ws === ws) this.connect(roomId, source).catch(error => this.report(error.message));
        }, 2000);
      };
    });
  }
  reset() {
    if(this.continuousEngine){this.continuousEngine.dispose();this.continuousEngine=null;}
    this.generation++; this.starting = false; this.epoch = -1;this.ack=null;this.completedAttempt=null;this.readyObservedAt=null;this.playRequestedAt=null;
    const a = this.audio;
    a.onplaying = a.onended = a.onwaiting = a.onerror = a.ontimeupdate = a.onloadedmetadata = null;
    a.pause(); a.removeAttribute('src'); a.load();
  }
  disconnect() {
    this.closed = true;
    if (this.ws) { this.ws.close(); this.ws = null; }
    this.reset(); this.identity = null; this.state = null;
  }
  claim() {
    if (this.state?.player_id === this.identity) return Promise.resolve();
    if (this.ws?.readyState !== 1) return Promise.reject(Error('声音来源尚未连接'));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.claimReady = null; reject(Error('声音接管超时，请重试')); }, 5000);
      this.claimReady = () => { clearTimeout(timer); this.claimReady = null; resolve(); };
      this.ws.send(JSON.stringify({type:'claim'}));
    });
  }
  async resume() {
    const response = await fetch(`/api/rooms/${encodeURIComponent(this.roomId)}/control`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'resume', action_id:crypto.randomUUID(),...(this.state?.session_id?{expected_session_id:this.state.session_id}:{})})});
    if (!response.ok) { const r = await response.json(); throw Error(r.detail || '恢复失败'); }
  }
  failOutput(message) {
    this.continuousEngine?.pause();
    this.audio.pause();
    if (this.state?.current && this.identity === this.state.player_id) this.ack?.('failed');
    this.report(message);
  }
  welcomeMayPlay() {
    const current = this.state?.current;
    if (!current?.is_welcome || current.welcome_started || !current.welcome_expires_at || Date.now() < current.welcome_expires_at * 1000) return true;
    this.audio.pause();
    if (this.expiredWelcomeEpoch !== this.state.epoch) {
      this.expiredWelcomeEpoch = this.state.epoch;
      this.ack?.('welcome_expired');
    }
    return false;
  }
  sync(s) {
    const previous=this.state;this.state = s;
    if(!previous||previous.epoch!==s.epoch||previous.status!==s.status||previous.session_id!==s.session_id||previous.player_id!==s.player_id)this.trace('state_received',{owns_playback:this.identity===s.player_id});
    if (s.player_id === this.identity && this.claimReady) this.claimReady();
    if (this.identity !== s.player_id || !s.current || ['stopped','completed','error'].includes(s.status)) {
      if(this.continuousEngine)this.reset();
      if (this.audio.getAttribute('src') && this.audio.getAttribute('src') !== '/assets/unlock.wav') this.reset();
      return;
    }
    if(s.audio?.continuous_track||(this.continuousEngine&&(s.current.is_insertion||!s.audio||s.status==='paused'))){
      if(!this.continuousEngine){
        this.reset();
        try{this.continuousEngine=new ContinuousTrackPlayer(this.audio,this.beforePlay,(event,detail)=>this.trace(event,detail),error=>{this.trace('continuous_play_failed',{error_name:error.name||'Error'});this.ack?.(error.name==='NotAllowedError'?'blocked':'failed');this.report(error.message||'连续音频播放失败');});}
        catch(error){this.report(error.message);return;}
      }
      this.epoch=s.epoch;const epoch=s.epoch,attempt=s.current.attempt_id;
      this.ack=(event,extra={})=>{if(this.ws?.readyState!==1||this.state?.epoch!==epoch||this.identity!==this.state.player_id)return;this.ws.send(JSON.stringify({type:'ack',event,epoch,attempt_id:attempt,...extra}));if(event!=='progress')this.trace('ack_sent',{ack_event:event});};
      this.continuousEngine.sync(s,this.ack);return;
    }
    if(this.continuousEngine)this.reset();
    if (this.epoch !== s.epoch) {
      this.reset(); this.epoch = s.epoch;
      const token = this.generation, epoch = s.epoch, attempt = s.current.attempt_id;this.trace('entry_selected');
      const ack = (event, extra={}) => {
        if (token !== this.generation || this.ws?.readyState !== 1 || this.identity !== this.state?.player_id) return;
        this.ws.send(JSON.stringify({type:'ack', event, epoch, attempt_id:attempt, ...extra}));
        if(event!=='progress')this.trace('ack_sent',{ack_event:event});
      };
      this.audio.onplaying = () => { if(token!==this.generation)return;this.enabled = true;this.trace('media_playing',{ready_to_playing_ms:this.readyObservedAt==null?null:this.clock()-this.readyObservedAt,play_request_to_playing_ms:this.playRequestedAt==null?null:this.clock()-this.playRequestedAt});ack('playing'); this.report(this.source === 'obs' ? 'OBS 来源正在播放声音' : '声播语音正在播放'); };
      this.audio.onended = () => {
        if(token!==this.generation||!this.audio.ended)return;
        this.completedAttempt={token,epoch,attempt,confirmed_after_resume:false};this.trace('media_ended');
        if(this.state?.status==='paused'){this.trace('ended_deferred_while_paused');return;}
        ack('ended');
      };
      this.audio.onwaiting = () => { if (token===this.generation&&this.audio.getAttribute('src')){this.trace('media_waiting');ack('waiting');} };
      this.audio.onerror = () => { if (token===this.generation&&this.audio.getAttribute('src')){this.trace('media_error',{media_error_code:this.audio.error?.code||null});ack('failed');} };
      let last = 0;
      this.audio.ontimeupdate = () => { if (Date.now()-last > 350) { last = Date.now(); ack('progress', {position:this.audio.currentTime}); } };
      this.ack = ack;
    }
    if (s.status === 'paused') { if(this.completedAttempt)this.completedAttempt.confirmed_after_resume=false;this.readyObservedAt=null;this.audio.pause(); return; }
    if (!s.audio) return;
    if(this.readyObservedAt==null&&['ready','buffering'].includes(s.status)){this.readyObservedAt=this.clock();this.trace('audio_ready_observed');}
    if (!this.welcomeMayPlay()) return;
    // A native end can be queued at the same instant as pause. The server
    // intentionally ignores ended while paused. On resume confirm that exact
    // finished attempt instead of restarting the clip from its beginning.
    const finished=this.completedAttempt;
    if(finished&&finished.token===this.generation&&finished.epoch===s.epoch&&finished.attempt===s.current.attempt_id&&this.audio.ended&&['ready','buffering','playing'].includes(s.status)){
      if(!finished.confirmed_after_resume&&s.status!=='playing'){finished.confirmed_after_resume=true;this.trace('completed_attempt_confirmed_without_replay');this.ack('playing');this.ack('ended');}
      return;
    }
    if (this.audio.getAttribute('src') !== s.audio.url) {
      this.audio.src = s.audio.url; this.audio.load();this.trace('source_assigned');
      // A reconnect creates a fresh media element. Restore the observed position
      // once metadata is available; ordinary pause/resume keeps the same element.
      const position=Number(s.position)||0, token=this.generation;
      if(position>0){
        const restore=()=>{if(token!==this.generation)return;try{this.audio.currentTime=Math.min(position,Number.isFinite(this.audio.duration)?Math.max(0,this.audio.duration-.02):position);}catch(_){}this.audio.onloadedmetadata=null;};
        if(this.audio.readyState>=1)restore();else this.audio.onloadedmetadata=restore;
      }
    }
    // A gesture can start audio before the resume HTTP request finishes.
    // In that case there is no second native `playing` event after status=ready.
    if (s.status === 'ready' && !this.audio.paused && this.audio.readyState >= 2) this.ack('playing');
    if (['ready','buffering'].includes(s.status) && this.audio.paused && !this.starting) {
      this.starting = true; const token = this.generation;
      Promise.resolve().then(() => this.beforePlay()).then(() => {
        if (token !== this.generation || this.identity !== this.state?.player_id || this.state?.status === 'paused' || !this.welcomeMayPlay()) return;
        this.playRequestedAt=this.clock();this.trace('play_requested');return this.audio.play();
      }).catch(error => {
        if (token === this.generation && error.name !== 'AbortError') {
          this.trace('play_rejected',{error_name:error.name||'Error',error_code:error.code||null});this.ack(error.name === 'NotAllowedError' ? 'blocked' : 'failed');
          this.report(error.code === 'OUTPUT_DEVICE_UNAVAILABLE' ? error.message : error.name === 'NotAllowedError' ? '声音被拦截，请点击开启声音' : '音频播放失败，请重试当前句');
        }
      }).finally(() => { if (token === this.generation) this.starting = false; });
    }
  }
}
