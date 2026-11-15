const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const html=fs.readFileSync('static/index.html','utf8');
const start=html.indexOf('  async function describeExtraImages(');
const end=html.indexOf('  async function runQwenProvider(',start);
assert(start>=0&&end>start);
let requests=[], fail=false, inflight=0, peak=0;
const context={
  toast:()=>{},
  uploadFilesToSpace: async (_meta, files, signal)=>({image:{path:'/remote/'+files.image.name,meta:{_type:'gradio.FileData'}}}),
  fetch:async (_url,opts)=>{
    const payload=JSON.parse(opts.body); requests.push(payload); inflight++; peak=Math.max(peak,inflight);
    await new Promise(r=>setTimeout(r,5)); inflight--;
    const path=payload.data[2].path;
    return {ok:!(fail&&path.endsWith('b.png')), json:async()=>({text:'details '+path})};
  }
};
vm.createContext(context);
vm.runInContext(html.slice(start,end)+'\nthis.describe=describeExtraImages;this.frame=extraImageContext;',context);
(async()=>{
  assert.equal(await context.describe([], 'x', undefined),'');
  assert.equal(requests.length,0);
  const out=await context.describe([{name:'a.png'},{name:'b.png'},{name:'c.png'}],'compare',undefined);
  assert.match(out,/\[Image 2\][\s\S]*a.png[\s\S]*\[Image 3\][\s\S]*b.png[\s\S]*\[Image 4\][\s\S]*c.png/);
  assert.equal(peak,2); assert.equal(requests.length,3);
  assert.equal(requests[0].data.length,9);
  assert.equal(requests[0].data[4].length,0);
  assert.match(requests[0].data[0],/User question: compare/);
  assert.match(context.frame(out),/untrusted image data/);
  fail=true;
  await assert.rejects(context.describe([{name:'a.png'},{name:'b.png'}],'x'),/تعذر تحليل الصورة 3/);
  const a=html.indexOf('  attsToFiles=async function(atts){');
  const b=html.indexOf('  ai.discoverGradio =',a);
  assert(a>=0&&b>a);
  class File { constructor(name){this.name=name;} }
  const conv={File,attsToFiles:null,toast:()=>{},T:()=>({}),mediaGetOrFetch:async()=>null};
  vm.createContext(conv);vm.runInContext(html.slice(a,b),conv);
  const result=await conv.attsToFiles(['one','two','three'].map(name=>({mime:'image/png',file:new File(name)})));
  assert.equal(result.files.image.name,'one');
  assert.deepEqual(Array.from(result.files.extraImages,x=>x.name),['two','three']);
  await assert.rejects(conv.attsToFiles([{mime:'image/png',id:'missing'}]),/تعذر تحميل إحدى الصور/);
  // Exercise the real Atlas provider with a direct first image and text
  // descriptions of later images; they must not be uploaded as a bogus array.
  const pa=html.indexOf('  async function runQwenProvider(');
  const pb=html.indexOf('  /* v58.64+ — نداء واحد',pa);
  assert(pa>=0&&pb>pa);
  let sentFiles, sentText, body;
  const provider={AbortController,TextDecoder,live:{userMsg:{}},ai:{},S:{},
    describeExtraImages:async(images)=>images.length?'[Image 2] description':'',
    extraImageContext:()=> ' EXTRA DESCRIPTION',referenceFileForPrompt:()=>null,
    uploadFilesToSpace:async(_x,files)=>{sentFiles=files;return {image:{path:'/image-1',meta:{_type:'gradio.FileData'}}};},
    buildQwenData:(text,files)=>{sentText=text;return [text,null,files.image,null,[],'sys',0.7,0.8,20]},
    fetch:async(_path,opts)=>{body=JSON.parse(opts.body);let done=false;return {ok:true,body:{getReader:()=>({read:async()=>{
      if(done)return {done:true};done=true;return {done:false,value:new TextEncoder().encode('event: complete\ndata: value\n\n')};
    }})}}},
    handleQwenSse:()=>{provider.ai.received='answer';return true;}
  };
  vm.createContext(provider);vm.runInContext(html.slice(pa,pb)+'\nthis.run=runQwenProvider;',provider);
  assert.equal(await provider.run({endpoint:'chat_predict',name:'Atlas'},'compare',{image:{name:'one'},extraImages:[{name:'two'}]},null,'first'),'answer');
  assert.equal(sentFiles.image.name,'one');assert.equal(sentFiles.extraImages,undefined);
  assert.match(sentText,/EXTRA DESCRIPTION/);assert.equal(body.data[2].path,'/image-1');
  assert.match(provider.live.userMsg.vision,/\[Image 2\] description/);
  console.log('Multiple image tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
