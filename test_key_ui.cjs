const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync(__dirname+'/webui.py','utf8').split('<script>')[1].split('</script>')[0];
function setup(fetch){const elements={};
 const mk=()=>({value:'',textContent:'',children:[],disabled:false,isConnected:true,style:{},className:'',
  classList:{_s:new Set(),add(...a){a.forEach(x=>this._s.add(x))},remove(...a){a.forEach(x=>this._s.delete(x))},toggle(x){this._s.has(x)?this._s.delete(x):this._s.add(x);return this._s.has(x)},contains(x){return this._s.has(x)}},
  appendChild(c){this.children.push(c)},append(...a){a.forEach(x=>this.children.push(x))},replaceChildren(){this.children=[]},focus(){},
  addEventListener(n,f){this[n]=f}});
 const el=id=>elements[id]||=mk();
 const ctx={console,Set,Map,JSON,Promise,TextDecoder,fetch,setTimeout:f=>f(),document:{getElementById:el,createElement:mk}};
 vm.createContext(ctx);for(const n of ['keyin','urlin','modelin','inp','wrap','chat'])ctx[n]=el(n);vm.runInContext(code.replace('restoreTasks();','').replace('restoreHistory();',''),ctx);return {ctx,el}}
const resp=(obj,ok=true)=>({ok,json:async()=>obj,text:async()=>typeof obj==='string'?obj:JSON.stringify(obj)});
(async()=>{
 // Test button: shows OK / failure text, key never shown, button restored
 for(const [r,expectOk,frag] of [[{ok:true,status:200,message:'Key works. Model m replied.'},true,'OK: Key works'],[{ok:false,status:404,message:'No such model on your key.'},false,'Failed (404): No such model'],[{ok:false,status:503,message:'Google is busy right now.'},false,'Failed (503): Google is busy']]){
  const s=setup(async()=>resp(r)); await vm.runInContext('testKey()',s.ctx);
  assert(s.el('keystatus').textContent.includes(frag),s.el('keystatus').textContent);
  assert.equal(s.el('keystatus').className,expectOk?'ok':'bad'); assert.equal(s.el('testbtn').disabled,false); assert.equal(s.el('testbtn').textContent,'Test');
 }
 { const s=setup(async()=>{throw new Error('offline')}); await vm.runInContext('testKey()',s.ctx); assert(s.el('keystatus').textContent.includes('Could not run the test: offline')); assert.equal(s.el('testbtn').disabled,false)}
 // mask display
 { const s=setup(async()=>resp({configured:true,masked:'AIza\u2026x9k',source:'saved',model:'gemini-flash-latest',provider:'Google Gemini'})); await vm.runInContext('refreshKeyStatus()',s.ctx);
   const box=s.el('keymask'); assert(box.children.some(c=>c.textContent==='AIza\u2026x9k')); }
 { const s=setup(async()=>resp({configured:false})); await vm.runInContext('refreshKeyStatus()',s.ctx); assert.equal(s.el('keymask').textContent,'No key saved yet.') }
 // save clears the input and refreshes mask, never echoes
 { const calls=[];const s=setup(async(u,o)=>{calls.push(u);return u==='/set_key'?{ok:true,text:async()=>'Saved in memory only.'}:resp({configured:true,masked:'AIza\u2026x9k',source:'saved',model:'m',provider:''})});
   s.el('keyin').value='AIzaFAKEFAKEFAKE';await vm.runInContext('saveKey()',s.ctx);assert.equal(s.el('keyin').value,'');assert(calls.includes('/key_status')); }
 // blocked task shows one-click Resume; clicking posts with no answer/checks
 { const calls=[];let status='blocked';
   const s=setup(async(u,o)=>{calls.push({u,o});
     if(u.includes('/resume'))return resp({id:'t1',status:'queued'});
     if(u.includes('/events'))return resp([{seq:1,type:'blocked',text:'Google is busy right now. Task paused, nothing was lost. Press Resume to continue.'}]);
     return resp({status,steps:2});});
   s.ctx.bubble=s.el('b1'); s.el('b1').textContent=''; await vm.runInContext("watchTask('t1',bubble)",s.ctx);
   const btn=s.el('b1').children.find(c=>c.className==='resume-btn'); assert(btn,'resume button for blocked task');
   s.el('in').value='should not be sent'; status='needs_input';
   await btn.click({stopPropagation(){}}); const rc=calls.find(c=>c.u==='/tasks/t1/resume'); assert(rc,'resume called');
   assert.deepEqual(JSON.parse(rc.o.body),{answer:''}); }
 // no button for completed task
 { const s=setup(async(u)=>u.includes('/events')?resp([]):resp({status:'completed',steps:1})); s.ctx.bubble=s.el('b2'); await vm.runInContext("watchTask('t2',bubble)",s.ctx); assert(!s.el('b2').children.some(c=>c.className==='resume-btn')) }
 // live retry status on topbar while running
 { let n=0; const seen=[]; const s=setup(async(u)=>{ if(u.includes('/events')) return resp(n===0?[{seq:1,type:'retry',text:'Google is busy right now, retrying 2 of 3...'}]:[]);
     n++; const st=n>=2?'completed':'running'; return resp({status:st,steps:1}); });
   const orig=s.el('status'); Object.defineProperty(orig,'textContent',{get(){return this._t||''},set(v){this._t=v;seen.push(v)}});
   s.ctx.bubble=s.el('b3'); await vm.runInContext("watchTask('t3',bubble)",s.ctx);
   assert(seen.includes('Google is busy right now, retrying 2 of 3...'),JSON.stringify(seen)); }
 // chat stream: retry notices split across chunks never leak into the reply, status shows them
 { const enc=new TextEncoder(); const parts=['\x1eGoogle is bu','sy right now, retrying 1 of 3...\x1f','\x1eretrying 2 of 3...\x1f','Hel','lo \x1e','x\x1fworld'];
   let i=0; const s=setup(async(u)=>({ok:true,body:{getReader:()=>({read:async()=>i<parts.length?{done:false,value:enc.encode(parts[i++])}:{done:true}})}}));
   s.ctx.TextEncoder=TextEncoder; const seen=[]; const st=s.el('status'); Object.defineProperty(st,'textContent',{get(){return this._t||''},set(v){this._t=v;seen.push(v)}});
   s.el('mode').value='chat'; s.el('in').value='hi';
   await vm.runInContext('send()',s.ctx);
   const bub=s.el('chat').children[s.el('chat').children.length-1]; assert.equal(bub.textContent,'Hello world'); assert(!/[\x1e\x1f]/.test(bub.textContent));
   assert(seen.includes('Google is busy right now, retrying 1 of 3...'),JSON.stringify(seen)); assert(seen.includes('retrying 2 of 3...')); assert.equal(seen[seen.length-1],'Ready'); }
 console.log('PASS key/test/resume UI');
})().catch(e=>{console.error(e);process.exitCode=1});
