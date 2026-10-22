/* Offline UI helpers: no browser, providers, credentials or database. */
const assert=require('node:assert/strict');
const {newVariantModuleKeys,playbackSummary}=require('../web/workspace-editors.js');
for(const position of [1,6,11])assert.deepEqual(newVariantModuleKeys(position),['opening_long','product_intro','selling_points','campaign','cta']);
for(const position of [2,3,4,5,7,10]){
  const keys=newVariantModuleKeys(position);
  assert.equal(keys[0],'opening_short');
  assert.equal(keys.length,5);
  assert.ok(!keys.includes('transition'));
  assert.ok(!keys.includes('opening_long'));
}
const review={texts:[{},{}],cost_scope:{characters:80},playback_statistics:{basis:'saved_queue_one_cycle',entry_count:3,sentence_count:12,spoken_characters:500}};
const label=playbackSummary(review);
assert.match(label,/实播一轮：3 项 · 12 句 · 500 字符/);
assert.match(label,/合成范围（去重）：2 句 · 80 字符/);
assert.match(playbackSummary({...review,playback_statistics:null}),/实播统计尚未就绪/);
console.log('opening statistics UI: passed');
