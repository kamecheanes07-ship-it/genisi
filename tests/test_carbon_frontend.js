// Exercise the actual browser streaming functions with mocked local API responses.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/index.html', 'utf8');
const start = html.indexOf('  async function carbonImageData(file){');
const end = html.indexOf('  runTurn=async function(', start);
assert(start >= 0 && end > start, 'Carbon frontend functions not found');
let requests = [], snapshots = [], mode = 'ok';
const encoder = new TextEncoder();
const context = {
  AbortController, TextDecoder,
  ai: {busy:false, received:'', controller:null}, S: {ctrl:null},
  FileReader: class { readAsDataURL(file) { this.result=file.data; this.onload(); } },
  buildQwenData: text => [text, null, null, null, [{role:'user', content:'prior'}], 'system', 1, .95, 20],
  onText: (answer, reasoning) => snapshots.push([answer, reasoning]),
  api: async (path, opts) => {
    requests.push({path, opts});
    const events = mode === 'partial'
      ? [{type:'answer_delta',data:'partial'}]
      : mode === 'snapshot'
      ? [{type:'snapshot',answer:'قديم',reasoning:'تفكير'}, {type:'snapshot',answer:'جديد',reasoning:'تفكير جديد'}, {type:'done'}]
      : [{type:'reasoning_delta',data:'أفكر'}, {type:'answer_delta',data:'مرح'}, {type:'answer_delta',data:'باً'}, {type:'done'}];
    const bytes = encoder.encode(events.map(e => `data: ${JSON.stringify(e)}\n\n`).join(''));
    let pos=0;
    return {ok:true, body:{getReader:()=>({read:async()=>{
      if(pos>=bytes.length) return {done:true};
      const value=bytes.slice(pos,pos+3); pos+=3; return {done:false,value};
    }})}};
  }
};
vm.createContext(context);
vm.runInContext(html.slice(start,end) + '\nthis.carbonProvider=runCarbonProvider; this.carbonOnce=carbonRequest;', context);
(async()=>{
  const result = await context.carbonProvider('hello', {image:{size:8,data:'data:image/png;base64,abc'}}, 'first');
  assert.equal(result, 'مرحباً');
  assert.deepEqual(snapshots.at(-1), ['مرحباً','أفكر']);
  assert.equal(requests[0].path, '/api/models/carbon/stream');
  const payload=JSON.parse(requests[0].opts.body);
  assert.equal(payload.image, 'data:image/png;base64,abc');
  assert.equal(payload.text, 'hello');
  assert.equal(context.ai.busy, false);
  await assert.rejects(context.carbonProvider('hello', {audio:{}}, 'first'), /الصوت/);
  mode='snapshot';
  assert.equal(await context.carbonOnce({text:'hello'}, undefined, context.onText), 'جديد');
  assert.deepEqual(snapshots.at(-1), ['جديد','تفكير جديد']);
  mode='partial';
  await assert.rejects(context.carbonProvider('hello', {}, 'first'), /انقطع بث Carbon/);
  assert.equal(context.ai.busy, false);
  console.log('Carbon frontend streaming tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
