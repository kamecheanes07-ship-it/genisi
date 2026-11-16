// Test the actual model menu and its selection callbacks, without a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/index.html', 'utf8');
const start = html.indexOf('  function selectRouteMode(mode){');
const end = html.indexOf('  async function discoverGradio(', start);
assert(start >= 0 && end > start, 'model menu not found');
let items, applied = 0, deepSyncs = 0;
const voiceModel = {innerHTML:''};
const storage = {};
const context = {
  state:{routeMode:'auto'}, S:{model:'auto'},
  ROUTE_NAMES:{auto:'Genisi 4 Atlas',carbon:'Genisi 4.1 Carbon'},
  ROUTE_DESCRIPTIONS:{auto:'Atlas'},
  localStorage:{setItem:(key,value)=>{storage[key]=value;}},
  syncDeep:()=>deepSyncs++, applyLang:()=>applied++,
  openPop:(_id,_anchor,menu)=>{items=menu;},
  I:{sliders:'',chev:'▼'}, $:()=>voiceModel, openSettings:()=>{}, modelLabel:null, openModelMenu:null,
};
vm.createContext(context);
vm.runInContext(html.slice(start,end), context);
context.openModelMenu({});
const carbon = items.find(item=>item.label === 'Genisi 4.1 Carbon');
assert(carbon && !carbon.disabled && typeof carbon.fn === 'function', 'Carbon must be selectable');
carbon.fn();
assert.equal(context.state.routeMode,'carbon');
assert.equal(context.S.model,'carbon');
assert.equal(storage.g3_route_mode,'carbon');
assert.equal(context.modelLabel(),'Genisi 4.1 Carbon');
assert(voiceModel.innerHTML.includes('Genisi 4.1 Carbon'));
context.openModelMenu({});
assert.equal(items.find(item=>item.label === 'Genisi 4.1 Carbon').sel,true);
items.find(item=>item.label === 'Genisi 4 Atlas').fn();
assert.equal(context.state.routeMode,'auto');
assert.equal(storage.g3_route_mode,'auto');
assert.equal(context.modelLabel(),'Genisi 4 Atlas');
assert(voiceModel.innerHTML.includes('Genisi 4 Atlas'));
assert.equal(applied,2);
assert.equal(deepSyncs,2);
assert(html.includes("state.routeMode = localStorage.getItem('g3_route_mode') === 'carbon' ? 'carbon' : 'auto'"), 'selection should be restored on reload');
console.log('Carbon model menu selection tests passed');
