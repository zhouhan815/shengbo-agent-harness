// Segment metadata stays separate from the text sent for playback.
const segmentLabels = {welcome:'开场欢迎',opening:'开场欢迎',pain:'场景引入',hook:'场景引入',intro:'场景引入',identity:'商品介绍',product:'商品介绍',overview:'卖点概览',benefit:'卖点讲解',scene:'使用场景',specification:'规格选购',spec_notice:'规格选购',price:'价格说明',promotion:'促销优惠',cta:'购买引导',engagement:'直播互动',care:'使用提醒',faq:'问题解答'};
let scriptEditor = null, speechProfiles = [], scriptOpenToken = 0;
function scriptSegments(script) {
  const groups = [], byId = new Map();
  for (const sentence of script.sentences || []) {
    const key = sentence.block_id || `legacy-${sentence.block_ordinal || sentence.stage || 'benefit'}`;
    let group = byId.get(key);
    if (!group) {
      const source = (script.blocks || []).find(b => b.id === key) || {};
      const paragraph = (script.paragraphs || script.paragraph_view || []).find(b => (b.block_id || b.id) === key) || {};
      group = {...source,...paragraph,id:key,stage:source.stage || sentence.stage || 'benefit',sentences:[]};
      groups.push(group); byId.set(key,group);
    }
    group.sentences.push(sentence);
  }
  return groups;
}
function styleForSegment(segment) {
  const supplied = segment.speech_style || segment.sentences[0]?.speech_style || {};
  const profile = speechProfiles.find(p => p.stage === segment.stage || p.stage === supplied.profile_id || p.stages?.includes(segment.stage)) || {};
  // Script detail defaults to MiniMax; the room may use the limited local voice.
  return supplied.provider && supplied.provider !== ttsForm().tts_provider ? profile : {...profile,...supplied};
}
const emotionLabels = {happy:'轻快、有笑意',neutral:'自然平和',fluent:'自然流畅',excited:'积极、有感染力',confident:'清晰自信',calm:'平和讲解',surprised:'惊喜',sad:'低落',angry:'强烈',disgusted:'厌恶',fearful:'紧张'};
function voiceDescription(style) {
  const voice = style.voice_setting || style, parts = [];
  if (style.description || style.delivery) parts.push(String(style.description || style.delivery));
  if (voice.emotion) parts.push(`情绪：${emotionLabels[voice.emotion] || voice.emotion}`);
  if (voice.speed != null) parts.push(`语速 ${voice.speed} 倍`);
  if (voice.pitch != null) parts.push(`音调 ${Number(voice.pitch) > 0 ? '+' : ''}${voice.pitch}`);
  if (voice.vol != null || voice.volume != null) parts.push(`音量 ${voice.vol ?? voice.volume}`);
  return parts.length ? parts.join(' · ') : '按当前音色自然播报；具体表达以生成后的试听为准。';
}
function renderScriptEditor() {
  const segments = scriptSegments(scriptEditor);
  $('script-segments').innerHTML = segments.map((segment,index) => {
    const style = styleForSegment(segment), goal = segment.focus || style.goal || style.intent || '围绕本段商品信息自然讲解';
    return `<section class="script-segment"><div class="segment-heading"><span class="segment-index">${index+1}</span><div><h3>${esc(segmentLabels[segment.stage] || '商品讲解')}</h3><p>${esc(goal)}</p></div><span class="badge">${segment.sentences.length} 句</span></div><div class="segment-voice"><strong>声音表达</strong><div><p>${esc(voiceDescription(style))}</p>${style.capability_note ? `<small>${esc(style.capability_note)}</small>` : ''}</div></div><div class="segment-sentences">${segment.sentences.map(sentence => `<label class="sentence-editor"><span>第 ${sentence.ordinal || scriptEditor.sentences.indexOf(sentence)+1} 句</span><textarea rows="2" data-sentence-edit="${scriptEditor.sentences.indexOf(sentence)}" maxlength="1500">${esc(sentence.text)}</textarea><button type="button" data-listen-sentence="${scriptEditor.sentences.indexOf(sentence)}">试听本句</button></label>`).join('')}</div></section>`;
  }).join('');
  document.querySelectorAll('[data-listen-sentence]').forEach(button => button.onclick = () => previewEditedSentence(Number(button.dataset.listenSentence),button));
}
function readScriptEditor() {
  if (!scriptEditor) return {lines:[],blocks:[],sentences:[]};
  const sentences = scriptEditor.sentences.map((sentence,index) => {
    const text = document.querySelector(`[data-sentence-edit="${index}"]`)?.value.trim() ?? sentence.text;
    return {...sentence,text,display_text:text,spoken_text:text === sentence.text ? (sentence.spoken_text || text) : text};
  });
  return {lines:sentences.map(s => s.text),blocks:scriptEditor.blocks.map(b => ({...b})),sentences};
}
async function previewEditedSentence(index,button) {
  if (button) button.disabled = true;
  try {
    if (currentState && ['playing','ready','preparing','buffering'].includes(currentState.status)) throw Error('请先暂停播出，避免试听与直播重叠');
    const sentence = readScriptEditor().sentences[index];
    if (!sentence?.text) throw Error('请先填写这句话');
    const segment = scriptSegments(scriptEditor).find(b => b.id === sentence.block_id) || {stage:sentence.stage,sentences:[sentence]};
    $('voice-audio').pause(); $('preview-audio').pause();
    const result = await api('/api/preview','POST',{text:sentence.spoken_text || sentence.text,stage:sentence.stage,speech_style:styleForSegment(segment),...ttsForm()});
    $('preview-audio').src = result.url; $('preview-audio').hidden = false; await $('preview-audio').play();
  } catch(error) { notify(error.message); }
  finally { if (button) button.disabled = false; }
}
async function openScript(id) {
  const token = ++scriptOpenToken;
  selectedScript = library.scripts.find(s => s.id === id);
  if (!selectedScript) return notify('这版话术已移出，请刷新列表');
  scriptEditor = structuredClone(selectedScript);
  try {
    const [detail,profileResult] = await Promise.allSettled([api(`/api/scripts/${id}`),api(`/api/tts/profiles?provider=${encodeURIComponent(ttsForm().tts_provider)}`)]);
    if (token !== scriptOpenToken) return;
    if (detail.status === 'fulfilled') scriptEditor = structuredClone(detail.value.script || detail.value);
    if (profileResult.status === 'fulfilled') speechProfiles = profileResult.value.profiles || [];
    else speechProfiles = [];
  } catch(error) { notify('声音表达信息暂不可用，仍可查看和编辑文字'); }
  if (token !== scriptOpenToken) return;
  scriptEditor.blocks ||= [];
  $('script-dialog-title').textContent = scriptName(scriptEditor);
  const audio = matchingAudio() ? audioInfo.scripts.find(s => s.script_id === id) : null;
  $('script-meta').textContent = `${scriptEditor.product_snapshot?.name || ''}｜${scriptSegments(scriptEditor).length} 段 · ${scriptEditor.sentences.length} 句｜${audio?.status === 'ready' ? '语音已生成' : '语音待生成或检查'}｜文字无需确认`;
  $('script-issues').textContent = [...(scriptEditor.issues || []),...(scriptEditor.missing_information || []),'可自行核对价格、币种、规格与优惠；不需要点击文本确认。'].join('；');
  renderScriptEditor(); $('preview-audio').hidden = true;
  if (!$('script-dialog').open) $('script-dialog').showModal();
}
$('save-script').onclick = async () => {
  const button = $('save-script'); button.disabled = true;
  try {
    const oldId = selectedScript.id, payload = readScriptEditor();
    if (payload.lines.some(line => !line)) throw Error('请补齐空白句子后再保存');
    const result = await api(`/api/scripts/${oldId}`,'PUT',payload), i = order.indexOf(oldId);
    if (i >= 0) { order[i] = result.id; removedScripts.add(oldId); }
    await refresh(); markDirty(); await saveRoom(); await checkAudio(); await openScript(result.id);
    notify('文本与段落已保存；无需确认，补齐语音后即可开播');
  } catch(error) { notify(error.message); }
  finally { button.disabled = false; }
};
$('listen-script').onclick = () => previewEditedSentence(0,$('listen-script'));

const evaluationLabels = {program_points:'规则检查（40分）',judge_points:'模型评审（60分）',selling_point_coverage:'卖点覆盖',flow:'结构完整',speakability:'适合口播',length_fit:'篇幅适配',fact_references:'事实引用',natural_live_speech:'自然直播表达',benefit_scene_expansion:'卖点与场景展开',coherent_flow:'讲解连贯',non_repetition:'避免重复',purchase_guidance:'购买引导',thresholds:'通过阈值',hard_gates:'必须满足',cautions:'解释与限制',total:'总分',score:'分数',scores:'各项评分',program_scores:'程序各项评分',semantic_scores:'语义各项评分',versions:'各版本结果',version:'版本',passed:'通过检查',pass:'通过检查',hard_gate:'全部硬门槛通过',copy_pass:'版本重复检查通过',review_type:'评审方式',unsupported_claims:'缺少依据的断言',rule_issues:'规则发现的问题',knowledge_leaks:'知识库信息混入',main_gaps:'主要差距',quote:'原句',reason:'判断依据',rubric:'量表版本',index:'版本序号',score_reasons:'评分理由',price_and_terms_correct:'价格与条件正确',issues:'问题',failures:'未通过原因',failure:'失败原因',notes:'备注',semantic:'模型评审得分',program:'规则得分',pair_diversity:'两版差异',diversity:'版本差异',coverage:'卖点覆盖率',manual_review:'人工复核',human_review:'人工复核',judge:'模型评审',methodology:'评测方法',case:'测试商品',run:'实验轮次',model:'评审模型',limitations:'限制',total_min_each_version:'每版最低总分',semantic_min:'模型评审最低分',pair_diversity_min_0_to_5:'版本差异最低分（满分5）',max_shared_narrative_sentences:'最多重复叙述句',near_sentence_similarity:'相似句判定阈值',near_copy_ratio_max:'最高近似重复占比',coverage_min:'最低覆盖率'};
function evaluationValue(value,depth=0) {
  if (value == null) return '<span class="muted">未记录</span>';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value !== 'object') return esc(value);
  if (Array.isArray(value)) return `<ol class="evaluation-values">${value.map(item => `<li>${evaluationValue(item,depth+1)}</li>`).join('')}</ol>`;
  return `<dl class="evaluation-fields">${Object.entries(value).map(([key,item]) => `<div><dt>${esc(evaluationLabels[key] || key)}</dt><dd>${evaluationValue(item,depth+1)}</dd></div>`).join('')}</dl>`;
}
function scoreSummary(scorecard) {
  if (!scorecard) return '未产出有效评分';
  const versions = scorecard.versions || scorecard.results || [], values = Array.isArray(versions) ? versions.map(v => v.total ?? v.score).filter(v => typeof v === 'number') : [];
  return values.length ? `各版总分：${values.map(v => Number(v).toFixed(1)).join(' / ')}` : '已记录评测，展开查看各项结果';
}
function renderAudioSamples(samples) {
  const groups = new Map();
  for (const sample of samples || []) {
    if (!groups.has(sample.stage)) groups.set(sample.stage,[]);
    groups.get(sample.stage).push(sample);
  }
  return `<section class="audio-ab-section"><h3 class="evaluation-heading">语音 A/B 试听 <span class="badge">尚未完成人耳验收</span></h3><p class="muted">同一段文字对比原参数与分段参数，请分别听唱腔、背景杂音与感染力。波形正常不代表无杂音，也不代表听感合格。</p>${groups.size ? [...groups].map(([stage,items]) => `<article class="audio-ab-group"><h4>${esc(segmentLabels[stage] || stage || '自然讲述')}</h4><p>${esc(items[0].text)}</p><div class="audio-ab-pair">${items.map(sample => {
    const label = ({baseline:'原参数',original:'原参数',default:'原参数',plain:'原参数',styled:'分段参数',segmented:'分段参数',stage_profile:'分段参数'})[sample.version] || sample.version || '试听样本';
    const w = sample.waveform || {}, ready = !!sample.url && !sample.error;
    return `<div class="audio-ab-item"><strong>${esc(label)}</strong>${ready ? `<audio controls preload="none" src="${esc(sample.url)}" aria-label="${esc(segmentLabels[stage] || stage)} ${esc(label)}试听"></audio>` : `<p class="task-error">${esc(sample.error || (sample.status === 'running' ? '正在生成' : '尚无可试听文件'))}</p>`}<small>${Object.entries(sample.listening || {唱腔:'未试听',背景杂音:'未试听',感染力:'未试听'}).map(([key,value]) => `${esc(key)}：${esc(value)}`).join(' · ')}</small>${Object.keys(w).length ? `<details><summary>音频文件检查（不评价听感）</summary><p>时长 ${Number(w.duration || 0).toFixed(2)} 秒 · 峰值 ${Number(w.peak || 0).toFixed(3)} · RMS ${Number(w.rms || 0).toFixed(3)} · 削波 ${(Number(w.clip_ratio || 0)*100).toFixed(3)}%</p></details>` : ''}</div>`;
  }).join('')}</div></article>`).join('') : '<p class="empty-state">暂无语音对比样本。生成后会显示在这里。</p>'}</section>`;
}
$('show-evaluation').onclick = async () => {
  $('evaluation-dialog').showModal(); $('evaluation-content').innerHTML = '<p class="muted">正在读取评测记录…</p>';
  try {
    const result = await api('/api/copywriting/evaluation'), runs = result.runs || [];
    $('evaluation-content').innerHTML = `<details class="evaluation-method" open><summary>评分标准与通过条件</summary>${evaluationValue(result.rubric)}</details><details class="evaluation-method"><summary>评测方法、模型与人工复核的区别</summary>${evaluationValue(result.methodology)}</details>${renderAudioSamples(result.audio_samples)}<h3 class="evaluation-heading">实际测试 · ${runs.length} 轮记录</h3><p class="muted">不同商品、提示词与评测口径的结果需分别比较；没有分数的失败不按零分处理。</p>${runs.map(run => `<details class="evaluation-run"><summary><strong>${esc(run.run)}</strong>${run.generation ? `<span>${esc(run.generation)}</span>` : ''}<span>${esc(({slippers:'拖鞋',bag:'收纳包',mug:'马克杯',notebook:'笔记本'})[run.case] || run.case || '测试商品')} · 完成 ${run.completed_versions ?? '—'} / ${run.requested_versions ?? '—'} 版</span></summary><p class="evaluation-score">${esc(scoreSummary(run.scorecard))}</p>${run.failure ? `<div class="task-error"><strong>生成失败</strong>${evaluationValue(run.failure)}</div>` : ''}${run.judge_failure ? `<div class="task-error"><strong>模型评审失败（不可据此评分）</strong>${evaluationValue(run.judge_failure)}</div>` : ''}${run.adjudication ? `<details class="evaluation-review" open><summary>AI 复核裁决（保留原始评分）</summary>${evaluationValue(run.adjudication)}</details>` : ''}${run.review_history?.length ? `<details><summary>评审校准历史（不可直接与新口径比较）</summary>${evaluationValue(run.review_history)}</details>` : ''}${run.speech_peer_review ? `<details><summary>独立 AI 朗读文本复核（未试听音频）</summary>${evaluationValue(run.speech_peer_review)}</details>` : ''}${run.independent_reviews ? `<details class="evaluation-review"><summary>独立 AI 复核（非真人测试）</summary>${evaluationValue(run.independent_reviews)}</details>` : ''}${run.reviews ? `<details><summary>各角色 AI 评审原始意见</summary>${evaluationValue(run.reviews)}</details>` : ''}${run.manual_review ? `<div class="evaluation-review"><h4>人工复核</h4>${evaluationValue(run.manual_review)}</div>` : ''}${run.scorecard ? `<details><summary>各项得分与问题明细</summary>${evaluationValue(run.scorecard)}</details>` : ''}</details>`).join('') || '<p class="empty-state">尚无实际测试结果</p>'}`;
  } catch(error) { $('evaluation-content').innerHTML = `<p class="task-error">${esc(error.message)}，评测记录暂时未能读取。</p>`; }
};

// A/B comparisons must not play over each other or the live output.
$('evaluation-content').addEventListener('play',event => {
  if (event.target.tagName !== 'AUDIO') return;
  if (currentState && ['playing','ready','preparing','buffering'].includes(currentState.status)) { event.target.pause(); notify('请先暂停直播，再进行语音 A/B 试听'); return; }
  $('voice-audio').pause(); $('preview-audio').pause();
  $('evaluation-content').querySelectorAll('audio').forEach(audio => { if (audio !== event.target) audio.pause(); });
},true);
$('evaluation-dialog').addEventListener('close',() => $('evaluation-content').querySelectorAll('audio').forEach(audio => audio.pause()));
