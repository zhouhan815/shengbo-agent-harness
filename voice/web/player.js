const $=id=>document.getElementById(id);
const roomId=decodeURIComponent(location.pathname.split('/').pop()), preview=new URLSearchParams(location.search).has('preview');
let state, ws;
const labels={stopped:'等待开播',preparing:'准备音频',ready:'等待播放',playing:'正在直播',paused:'已暂停',buffering:'正在缓冲',blocked:'声音被拦截',error:'播放失败',completed:'本场结束'};
const phases={welcome:'开场',identity:'商品介绍',benefit:'卖点讲解',overview:'卖点概览',pain:'需求场景',scene:'使用场景',specification:'规格选择',care:'注意事项',promotion:'优惠说明',cta:'购买引导',engagement:'互动',price:'价格',faq:'常见问题'};
const player=new VoicePlayer($('broadcast-audio'),message=>{$('stage-instruction').textContent=message});
function time(v){return `${Math.floor(v/60).toString().padStart(2,'0')}:${Math.floor(v%60).toString().padStart(2,'0')}`}
function display(s){state=s;const c=s.current;$('stage-status').textContent=labels[s.status]||s.status;$('stage-name').textContent=c?.product_name||'等待商品播出';$('stage-version').textContent=c?`${c.version} · ${c.ordinal?`第 ${c.ordinal} / ${c.total_sentences} 句`:'插播'}`:'版本 — · 当前句 —';$('stage-phase').textContent=c?(phases[c.stage]||c.stage):'待机';$('stage-text').textContent=c?.text||s.error||(s.status==='completed'?'本场播出结束':'等待控制台开始直播');$('stage-source').textContent='';const art=$('stage-art');if(c?.image){if(art.dataset.image!==c.image){art.textContent='';const img=document.createElement('img');img.src=c.image;img.alt='当前商品';art.append(img);art.dataset.image=c.image}}else{art.textContent='声音直播';art.dataset.image=''};$('stage-progress').style.width=s.audio?Math.min(100,s.position/s.audio.duration*100)+'%':'0';$('stage-timing').textContent=`${time(s.position||0)} / ${time(s.audio?.duration||0)}`;
 const owner=s.player_id===player.identity;$('stage-enable').hidden=preview||!['blocked','error'].includes(s.status);$('enable-audio').hidden=!owner;$('claim-player').hidden=owner;$('stage-instruction').textContent=s.error||'若没有声音，请点击启用声音';
}
player.onState=display;
$('enable-audio').onclick=async()=>{try{await player.unlock();if(['blocked','paused'].includes(state?.status))await player.resume();$('stage-instruction').textContent='声音已启用，正在恢复播放';$('enable-audio').textContent='声音已启用'}catch(e){$('stage-instruction').textContent='声音未能开启，请检查音频设备后重试'}};
$('claim-player').onclick=()=>player.claim().catch(e=>{$('stage-instruction').textContent=e.message});
if(preview){$('stage-enable').hidden=true;function connect(){ws=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws?role=monitor&room=${roomId}`);ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.type==='state')display(m.state)};ws.onclose=()=>{$('stage-status').textContent='连接断开';setTimeout(connect,2000)}}connect();}
else player.connect(roomId,'obs').catch(e=>{$('stage-enable').hidden=false;$('stage-instruction').textContent=e.message});
function scaleStage(){const scale=Math.min(innerWidth/540,innerHeight/960);$('stage').style.width='540px';$('stage').style.minHeight='960px';$('stage').style.transform=`scale(${scale})`;$('stage').style.transformOrigin='top center';$('stage').style.position='absolute';$('stage').style.left='50%';$('stage').style.marginLeft='-270px';document.body.style.overflow='hidden';}
addEventListener('resize',scaleStage);scaleStage();
