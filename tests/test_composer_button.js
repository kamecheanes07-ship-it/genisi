// Exercise the actual composer state and click handlers without a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/index.html', 'utf8');
const cut=(a,b)=>{const x=html.indexOf(a),y=html.indexOf(b,x);assert(x>=0&&y>x);return html.slice(x,y)};
let started=0, stopped=0, sends=0, stops=0, toasts=[];
const button={className:'',innerHTML:'',title:'',setAttribute(k,v){this[k]=v;}};
const classes=new Set();
const ctx={
  S:{streaming:false,attachments:[],rec:null,lang:'en'}, input:{value:''},
  I:{stop:'STOP',up:'SEND',mic:'MIC'},
  T:()=>({stopGen:'Stop',send:'Send',dictate:'Voice dictation',stopDictation:'Stop voice dictation',micUnavailable:'Unavailable',micError:'Microphone error',listening:'Listening'}),
  $:()=>button, refreshVoiceBarHook:null, toast:(msg)=>toasts.push(msg),
  document:{body:{classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}}},
  localStorage:{getItem:()=>null}, Intl, syncWritingDirection:()=>{},
  send:()=>sends++, stopStream:()=>stops++,
  window:{SpeechRecognition:class {start(){started++;} stop(){stopped++;this.onend();}}}
};
vm.createContext(ctx);
vm.runInContext(cut('function refreshSend(){','\n\n/* ═══════════════════════════ attachments')+'\n'+cut('function stopComposerDictation(){','\n\n/* ═══════════════════════════ markdown pipeline')+'\n'+cut("$('#sendBtn').onclick = () => {", "\ninput.addEventListener('keydown'")+'\nthis.autoGrow=()=>refreshSend(); this.refresh=refreshSend;',ctx);
ctx.refresh();assert.equal(button.innerHTML,'MIC');assert.equal(button['aria-label'],'Voice dictation');
button.onclick();assert.equal(started,1);assert.equal(button.className,'recording');assert.equal(button['aria-label'],'Stop voice dictation');
const rec=ctx.S.rec;rec.onresult({results:[[{transcript:'hello'}]]});assert.equal(ctx.input.value,'hello');assert.equal(button.innerHTML,'SEND');assert.equal(button['aria-label'],'Send');
button.onclick();assert.equal(stopped,1);assert.equal(sends,1);ctx.input.value='';ctx.refresh();rec.onresult({results:[[{transcript:'late'}]]});assert.equal(ctx.input.value,'');assert.equal(button.innerHTML,'MIC');
ctx.S.attachments.push({});ctx.refresh();assert.equal(button.innerHTML,'SEND');button.onclick();assert.equal(sends,2);
ctx.S.streaming=true;ctx.refresh();assert.equal(button.innerHTML,'STOP');button.onclick();assert.equal(stops,1);
ctx.S.streaming=false;ctx.S.attachments=[];ctx.window.SpeechRecognition=null;ctx.refresh();button.onclick();assert(toasts.includes('Unavailable'));
assert(html.includes("['ar','en','fr'].includes(localStorage.getItem('g3_lang')) ? localStorage.getItem('g3_lang') : 'en'"));
assert(!html.includes('id="micBtn"'));
console.log('Composer microphone, send, attachment and stop tests passed');
