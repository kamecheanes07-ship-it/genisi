// Regression checks for the sign-in gate and model showcase without a browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/index.html', 'utf8');
for(const title of ['Genisi 1 Speak','Genisi 2 Sona','Genisi 2 Art','Genisi 1 Music','Genisi 4 Atlas','Genisi 4.1 Carbon']) assert(html.includes(title),title);
assert(html.includes('id="authOverlay" role="dialog"'));
assert(html.includes('class="overlay on auth-loading"'));
assert(html.includes('.overlay.on:not(#authOverlay)'));
assert(html.includes('#sidebar .sb-scroll{flex:0 0 auto;min-height:0;overflow:visible}'));
assert(html.includes("$('#acctAvatar').style.background = '#df3d4e'"));
assert(!html.includes('<button id="toolsBtn"'));
assert(!html.includes("$('#toolsBtn').onclick"));
assert(html.includes("/api/auth/challenge") && html.includes('solveAtlasProof') && html.includes("/api/auth/register"));
assert(html.includes('/static/genisi-auth-art.jpg'));
// Run the actual async session bootstrap against authenticated, guest and
// network-error states. The account screen must only stay open for guests.
let start=html.indexOf('(async function bootSecureAccount(){');
let end=html.indexOf('setTimeout(() => detect(false)',start);
assert(start>=0 && end>start);
let source=html.slice(start,end).replace('(async function bootSecureAccount(){','async function bootSecureAccount(){').replace(/\}\)\(\);\s*$/, '}')+'\nthis.runBoot=bootSecureAccount;';
async function check(status,throws=false){
  const calls=[], classes={remove:(...x)=>calls.push(['remove',...x])};
  const ctx={S:{user:null},fetch:async()=>{if(throws)throw Error('offline');return {ok:status===200,json:async()=>({user:{username:'Amine'}})}},
    $:()=>({classList:classes}),setAuth:(_token,user)=>{ctx.S.user=user;calls.push(['auth',!!user])},
    openAuth:mode=>calls.push(['open',mode]),setEntryGate:()=>{},loadPreferences:async()=>{},loadHistory:async()=>{},loadWorkspace:async()=>{},loadAgent:async()=>{},
    syncPendingSessionMedia:()=>{},renderSync:()=>{},renderAvatar:()=>{},renderHomeHero:()=>{},renderSessions:()=>{},renderGallery:()=>{},renderPlanBadge:()=>{}};
  vm.createContext(ctx);vm.runInContext(source,ctx);await ctx.runBoot();return calls;
}
(async()=>{
  let c=await check(200);assert(c.some(x=>x[0]==='remove'&&x.includes('on')));assert(!c.some(x=>x[0]==='open'));
  c=await check(401);assert(c.some(x=>x[0]==='open'));
  c=await check(0,true);assert(c.some(x=>x[0]==='open'));
  console.log('Welcome screen and session-gate tests passed');
})().catch(e=>{console.error(e);process.exitCode=1});
