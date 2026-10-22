/* Pure editor projections, no browser, server, database or provider. */
const assert=require('node:assert/strict');
const {activeProductFields,applyOptionalProductVisibility}=require('../web/workspace-editors.js');
const source={name:'保留商品',price:178,currency:'CNY',material:'旧材质',knowledge:'旧说明',knowledge_documents:[{id:'a',content:'旧资料'}],specifications:[{name:'规格',value:'旧规格'}],shelf_life_value:12,shelf_life_unit:'year',hidden_optional_fields:['material','knowledge','knowledge_documents','specifications','shelf_life']};
const before=structuredClone(source),value=activeProductFields(source);
assert.equal(value.material,'');assert.equal(value.knowledge,'');assert.deepEqual(value.knowledge_documents,[]);assert.deepEqual(value.specifications,[]);assert.equal(value.shelf_life_value,null);assert.equal(value.shelf_life_unit,'month');assert.equal(value.name,'保留商品');assert.equal(value.price,178);assert.deepEqual(source,before);
let flags=['material','shelf_life','knowledge_documents'];
const makeField=()=>({box:{hidden:false,inert:false}});
const fields={material:makeField(),shelf_life_value:makeField(),shelf_life_unit:makeField(),knowledge_documents:makeField(),name:makeField(),hidden_optional_fields:{read:()=>flags}};
applyOptionalProductVisibility(fields);
for(const key of ['material','shelf_life_value','shelf_life_unit','knowledge_documents']){assert.equal(fields[key].box.hidden,true);assert.equal(fields[key].box.inert,true);}
assert.equal(fields.name.box.hidden,false);
flags=[];applyOptionalProductVisibility(fields);for(const key of ['material','shelf_life_value','shelf_life_unit','knowledge_documents']){assert.equal(fields[key].box.hidden,false);assert.equal(fields[key].box.inert,false);}
console.log('2 optional product editor projection and visibility checks passed');
