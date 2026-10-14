const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync(__dirname+'/static/app.js','utf8');
function setup(fetch){const elements={};
 const mk=()=>({value:'',textContent:'',children:[],disabled:false,isConnected:true,style:{},className:'',
  classList:{_s:new Set(),add(...a){a.forEach(x=>this._s.add(x))},remove(...a){a.forEach(x=>this._s.delete(x))},toggle(x){this._s.has(x)?this._s.delete(x):this._s.add(x);return this._s.has(x)},contains(x){return this._s.has(x)}},
  appendChild(c){this.children.push(c)},append(...a){a.forEach(x=>this.children.push(x))},replaceChildren(){this.children=[]},focus(){},
  addEventListener(n,f){this[n]=f}});
 const el=id=>elements[id]||=mk();
 const ctx={console,Set,Map,JSON,Promise,TextDecoder,fetch,setTimeout:f=>f(),document:{getElementById:el,createElement:mk}};
 vm.createContext(ctx);for(const n of ['keyin','urlin','modelin','inp','wrap','chat'])ctx[n]=el(n);vm.runInContext(code.replace('restoreTasks();','').replace('restoreHistory();','').replace('loadPolicy();','').replace('refreshKeyStatus();\n',''),ctx);return {ctx,el}}
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
 // agent stream: NDJSON events split across chunks; retry notices go to the status line, never into the reply
 { const enc=new TextEncoder(); const lines=[JSON.stringify({type:'conversation',id:'c1',title:'hi'}),JSON.stringify({type:'retry',text:'Google is busy right now, retrying 1 of 3...'}),JSON.stringify({type:'retry',text:'retrying 2 of 3...'}),JSON.stringify({type:'text',delta:'Hel'}),JSON.stringify({type:'text',delta:'lo world'}),JSON.stringify({type:'done',status:'completed',steps:1,tool_calls:0,usage:{}})].join('\n')+'\n';
   const parts=[lines.slice(0,40),lines.slice(40,131),lines.slice(131)];
   let i=0; const s=setup(async(u)=>u==='/agent'?({ok:true,body:{getReader:()=>({read:async()=>i<parts.length?{done:false,value:enc.encode(parts[i++])}:{done:true}})}}):resp([]));
   s.ctx.TextEncoder=TextEncoder; const seen=[]; const st=s.el('status'); Object.defineProperty(st,'textContent',{get(){return this._t||''},set(v){this._t=v;seen.push(v)}});
   s.el('in').value='hi';
   await vm.runInContext('send()',s.ctx);
   const bubbles=s.el('chat').children.filter(c=>typeof c.className==='string'&&c.className.includes('grok'));
   const bub=bubbles[bubbles.length-1]; assert.equal(bub.textContent,'Hello world');
   assert(seen.includes('Google is busy right now, retrying 1 of 3...'),JSON.stringify(seen)); assert(seen.includes('retrying 2 of 3...')); assert.equal(seen[seen.length-1],'Ready');
   assert.equal(vm.runInContext('currentConversation',s.ctx),'c1','conversation id from the stream is adopted'); }
 // tool cards: tool_start/tool_end render a card with the command and its output; approval_required renders buttons that post the decision
 { const enc=new TextEncoder(); const events=[{type:'tool_start',id:'t1',name:'run_command',arguments:{command:'ls -la'}},{type:'tool_end',id:'t1',name:'run_command',ok:true,output:'total 0',duration:0.2},{type:'approval_required',id:'a1',tool:'write_file',arguments:{path:'x.txt'},reason:'Sandbox is read-only',always_pattern:'write_file:*'},{type:'text',delta:'done'},{type:'done',status:'completed'}];
   const body=events.map(e=>JSON.stringify(e)).join('\n')+'\n'; let sent=false; const posts=[];
   const s=setup(async(u,o)=>{ if(u==='/agent') return {ok:true,body:{getReader:()=>({read:async()=>sent?{done:true}:(sent=true,{done:false,value:enc.encode(body)})})}}; if(u==='/agent/approve'){posts.push(JSON.parse(o.body));return resp({ok:true})} return resp([]);});
   s.el('in').value='run it'; await vm.runInContext('send()',s.ctx);
   const kids=s.el('chat').children; const card=kids.find(c=>typeof c.className==='string'&&c.className.startsWith('toolcard'));
   assert(card,'tool card rendered'); assert(card.className.includes('ok'),card.className);
   const flat=n=>[n.textContent||'',...(n.children||[]).map(flat)].join('|');
   assert(flat(card).includes('ls -la'),flat(card)); assert(flat(card).includes('total 0'),flat(card));
   const approval=kids.find(c=>c.className==='approval'); assert(approval,'approval card rendered');
   const btns=approval.children.find(c=>c.className==='btns'); const allow=btns.children.find(b=>b.className==='allow');
   await allow.click(); assert.deepEqual(posts[0],{id:'a1',decision:'allow'}); }
 console.log('PASS key/test/resume UI');
})().catch(e=>{console.error(e);process.exitCode=1});
