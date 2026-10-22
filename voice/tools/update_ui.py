from pathlib import Path
root=Path(__file__).resolve().parents[1]
p=root/'web/index.html'
s=p.read_text(encoding='utf-8')
s=s.replace('<label>播报音色<input value="Huihui 普通话 · 本机免费" disabled></label>', '''<label>声音服务<select name="tts_provider" id="tts-provider"><option value="system">本机普通话 · 免费</option><option value="minimax">MiniMax · 在线合成</option></select></label><label>播报音色<select name="tts_voice" id="tts-voice"></select></label><label id="tts-model-label" hidden>MiniMax 模型<select name="tts_model"><option value="speech-2.8-turbo">Speech 2.8 Turbo · 默认</option><option value="speech-2.8-hd">Speech 2.8 HD</option></select></label><p class="muted" id="tts-note"></p><div class="row"><button type="button" id="preview-voice">试听所选音色</button><audio id="voice-audio" controls hidden></audio></div><p class="muted">保存后用于下一次开始播出；当前场次保持原音色。</p>''')
p.write_text(s,encoding='utf-8')
p=root/'web/control.js';s=p.read_text(encoding='utf-8')
s=s.replace("f.elements.loop.checked=library.config.loop;", "f.elements.loop.checked=library.config.loop;f.elements.tts_provider.value=library.config.tts_provider;updateVoiceChoices(library.config.tts_voice);f.elements.tts_model.value=library.config.tts_model;")
s=s.replace("loop:f.elements.loop.checked});", "loop:f.elements.loop.checked,...ttsForm()});")
s=s.replace("'POST',{text});", "'POST',{text,...ttsForm()});")
s=s.replace("const ids=order.filter(id=>selected.has(id));", "$('preview-audio').pause();$('voice-audio').pause();const ids=order.filter(id=>selected.has(id));")
s=s.replace("const c=state.current;$('current-location')", "const c=state.current;$('current-location')")
s=s.replace("· ${esc(statuses[state.status])}</small>", "· 段落${c.block_ordinal||'—'} · ${esc(state.tts?.provider==='minimax'?'MiniMax':'本机普通话')} · ${esc(statuses[state.status])}</small>")
s=s.replace("c.ordinal?`第${c.ordinal}/${c.total_sentences}句`:'插播'", "c.ordinal?`段落${c.block_ordinal} · 第${c.ordinal}/${c.total_sentences}句`:'插播'")
s=s.replace("<button data-cancel=\"${g.product_id}\">取消商品</button></div></div>", "<button data-cancel=\"${g.product_id}\">取消商品</button></div><div class=\"row\"><select aria-label=\"替换待播版本${i+1}\" data-replacement=\"${i}\"><option value=\"\">选择已确认的新稿</option>${library.scripts.filter(x=>x.product_id===g.product_id&&x.status==='approved'&&x.id!==g.key).map(x=>`<option value=\"${x.id}\">${esc(x.label)} / 修订${x.revision}</option>`).join('')}</select><button data-replace=\"${i}\">替换待播稿</button></div></div>")
s=s.replace("$('event-log').innerHTML=", "document.querySelectorAll('[data-replace]').forEach(el=>el.onclick=()=>{const i=Number(el.dataset.replace),script_id=document.querySelector(`[data-replacement=\"${i}\"]`).value;if(!script_id)return notify('请先选择已确认的新稿');command('replace_pending',{script_id,target_script_id:groupedPending(currentState)[i].key})});\n $('event-log').innerHTML=")
s += '''
function ttsForm(){const f=$('config-form');return {tts_provider:f.elements.tts_provider.value,tts_voice:f.elements.tts_voice.value,tts_model:f.elements.tts_model.value}}
function updateVoiceChoices(chosen){const provider=$('tts-provider').value;const voices=provider==='system'?[['Huihui','慧慧 · 本机普通话']]:[['Wise_Woman','智慧女声'],['Deep_Voice_Man','沉稳男声'],['Friendly_Person','亲切声线'],['sweet_girl','甜美女声']];$('tts-voice').innerHTML=voices.map(([id,label])=>`<option value="${id}">${label}</option>`).join('');if(voices.some(([id])=>id===chosen))$('tts-voice').value=chosen;$('tts-model-label').hidden=provider!=='minimax';$('tts-note').textContent=provider==='system'?'无需在线TTS费用，使用本机已安装的普通话音色。':(library?.service.minimax_configured?'MiniMax Key 已配置。首次合成调用在线接口，相同内容与音色命中缓存后复用。':'MiniMax Key 尚未配置。');}
$('tts-provider').onchange=()=>updateVoiceChoices();
$('preview-voice').onclick=async()=>{const button=$('preview-voice');button.disabled=true;try{if(currentState&&['playing','ready','preparing','buffering'].includes(currentState.status))throw Error('请先暂停播出，避免试听与直播重叠');$('preview-audio').pause();const result=await api('/api/preview','POST',{text:'欢迎来到声音直播间，接下来为你介绍商品的卖点和使用场景。',...ttsForm()});$('voice-audio').src=result.url;$('voice-audio').hidden=false;await $('voice-audio').play();notify('当前试听：'+(ttsForm().tts_provider==='minimax'?'MiniMax':'本机普通话'))}catch(error){notify(error.message)}finally{button.disabled=false}};
$('script-dialog').addEventListener('close',()=>{$('preview-audio').pause()});
'''
p.write_text(s,encoding='utf-8')
p=root/'web/player.js';s=p.read_text(encoding='utf-8').replace("${c.version} / 修订 ${c.revision} ·", "${c.version} / 修订 ${c.revision} · 段落 ${c.block_ordinal||'—'} ·")
s=s.replace("$('stage').style.transformOrigin='top center';", "$('stage').style.transformOrigin='top center';$('stage').style.position='absolute';$('stage').style.left='50%';$('stage').style.marginLeft='-270px';document.body.style.overflow='hidden';")
p.write_text(s,encoding='utf-8')
# CSS margin shorthand must not override the centered stage position.
p=root/'web/style.css';s=p.read_text(encoding='utf-8');s+='\n.stage{margin-left:-270px!important}.queue-item select{min-width:0;max-width:180px;font-size:11px}.queue-item .row{margin-top:8px;flex-wrap:wrap}\n';p.write_text(s,encoding='utf-8')
p=root/'.env';s=p.read_text(encoding='utf-8-sig').replace('https://api.minimaxi.com/v1/t2a_v2','https://api.minimax.io/v1/t2a_v2');p.write_text(s,encoding='utf-8')
p=root/'.env.example';s=p.read_text(encoding='utf-8');s+='\nMINIMAX_API_KEY=\nMINIMAX_BASE_URL=https://api.minimax.io/v1/t2a_v2\n';p.write_text(s,encoding='utf-8')
print('UI and provider configuration updated')
