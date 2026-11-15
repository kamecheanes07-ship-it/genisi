const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('static/index.html','utf8');
function take(start,end){const a=html.indexOf(start),b=html.indexOf(end,a);assert(a>=0&&b>a,start);return html.slice(a,b);}
const ctx={state:{routeMode:'carbon',temp:0.5,topP:0.8,topK:20,deep:false,history:[],system:'',toolsOn:false},
  ai:{turnRoute:{id:'carbon',profile:'code',think:'force'},turn:1,windows12PromptInjectedTurn:-1},
  clampNum:(n,a,b)=>Math.max(a,Math.min(b,n)),
  profileClassifier:()=>({profile:'code',reason:'code'}),prevExchange:()=>({}),
  PROFILES:{code:{think:true,t:0.3,p:0.8,k:20},general:{think:false}},
  routeProfile:()=>({id:'carbon'}),
  baseSys:()=> 'sys', thinkNote:(p,mode)=>`NOTE ${p} ${mode}`,
  ANSWER_NOW_NOTE:'ANSWER NOW',currentTurnNeedsWindows12Prompt:()=>false};
vm.createContext(ctx);
vm.runInContext(take('  function resolveTurnRoute(', '  function showRouteDecision(')+'\nthis.resolve=resolveTurnRoute;',ctx);
assert.equal(ctx.resolve('program',[]).think,'force');
ctx.profileClassifier=()=>({profile:'code',reason:'followup',inherited:true});
assert.equal(ctx.resolve('change it',[]).think,'soft');
ctx.profileClassifier=()=>({profile:'general'});assert.equal(ctx.resolve('hi',[]).think,'');
ctx.state.deep=true;assert.equal(ctx.resolve('hi',[]).think,'force');
vm.runInContext(take('  function buildQwenData(', '  function qwenAssistantText(')+'\nthis.build=buildQwenData;',ctx);
assert.match(ctx.build('hello',{},'first')[0],/NOTE code force/);
assert.match(ctx.build('result',{},'followup')[0],/NOTE code followup/);
assert.match(ctx.build('answer',{},'answer')[0],/ANSWER NOW/);
ctx.ai.turnRoute.think='';assert.equal(ctx.build('hello',{},'first')[0],'hello');
const prompt={ai:{turnRoute:{id:'carbon',profile:'code'}},state:{toolsOn:false,system:''},
  ATLAS_CORE:'Genisi 4 Atlas',THINK_PROTOCOL:'THINK',PLAYBOOKS:{code:'code'},
  OUTPUT_QUALITY_GUARD:'quality',timeBlock:()=>'time',FILE_PROMPT:'file',
  QWEN_NO_TOOLS_PROMPT:'no tools',QWEN_CHAT_TOOLS_PROMPT:'tools',baseSys:null};
vm.createContext(prompt);
vm.runInContext(take('  baseSys = function(){','  const ROUTE_NAMES')+'\nthis.get=baseSys;',prompt);
const s=prompt.get();assert.match(s,/CREATIVE ENGINEERING/);assert.match(s,/mature libraries/);assert.match(s,/THINK/);
prompt.ai.turnRoute.id='qwen_omni';assert.doesNotMatch(prompt.get(),/CREATIVE ENGINEERING/);
console.log('Automatic reasoning and creative engineering tests passed');
