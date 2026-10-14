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
const PRESETS=[{id:'gemini',label:'Google Gemini',url:'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',model_hint:'gemini-flash-latest'},
 {id:'nvidia',label:'NVIDIA NIM',url:'https://integrate.api.nvidia.com/v1/chat/completions',model_hint:''}];
(async()=>{
 // picker fills from /providers and gets a Custom option
 { const s=setup(async(u)=>u==='/providers'?resp(PRESETS):resp({configured:false}));
   await vm.runInContext('loadProviders()',s.ctx);
   const sel=s.el('providersel');
   assert.deepEqual(sel.children.map(c=>c.value),['gemini','nvidia','custom']);
   assert.equal(sel.children[1].textContent,'NVIDIA NIM');
   assert.equal(s.el('urlin').style.display,'none');
   assert(s.el('modelin').placeholder.includes('optional'),s.el('modelin').placeholder); }
 // gemini hint shows in the model placeholder
 { const s=setup(async(u)=>u==='/providers'?resp(PRESETS):resp({configured:false}));
   await vm.runInContext('loadProviders()',s.ctx); s.el('providersel').value='gemini';
   await vm.runInContext('providerChanged()',s.ctx);
   assert(s.el('modelin').placeholder.includes('gemini-flash-latest'),s.el('modelin').placeholder); }
 // custom reveals the URL input
 { const s=setup(async(u)=>u==='/providers'?resp(PRESETS):resp({configured:false}));
   await vm.runInContext('loadProviders()',s.ctx); s.el('providersel').value='custom';
   await vm.runInContext('providerChanged()',s.ctx);
   assert.equal(s.el('urlin').style.display,''); }
 // save with a preset sends provider id and no url
 { const bodies=[]; const s=setup(async(u,o)=>{if(u==='/set_key'){bodies.push(JSON.parse(o.body));return {ok:true,text:async()=>'Saved in memory only. NVIDIA NIM selected.'}}
     return u==='/providers'?resp(PRESETS):resp({configured:true,masked:'nvapi\u2026001',source:'saved',model:'m',provider:'NVIDIA NIM'})});
   await vm.runInContext('loadProviders()',s.ctx); s.el('providersel').value='nvidia';
   s.el('keyin').value='nvapi-FAKE'; await vm.runInContext('saveKey()',s.ctx);
   assert.deepEqual(bodies[0],{key:'nvapi-FAKE',url:'',model:'',provider:'nvidia'});
   assert.equal(s.el('keyin').value,''); }
 // save with custom sends the typed url and no provider
 { const bodies=[]; const s=setup(async(u,o)=>{if(u==='/set_key'){bodies.push(JSON.parse(o.body));return {ok:true,text:async()=>'Saved in memory only. x (custom URL) selected.'}}
     return u==='/providers'?resp(PRESETS):resp({configured:true,masked:'k\u2026001',source:'saved',model:'m',provider:'api.example-llm.test'})});
   await vm.runInContext('loadProviders()',s.ctx); s.el('providersel').value='custom'; s.el('urlin').value='https://api.example-llm.test/v1/chat/completions';
   s.el('keyin').value='fake'; await vm.runInContext('saveKey()',s.ctx);
   assert.deepEqual(bodies[0],{key:'fake',url:'https://api.example-llm.test/v1/chat/completions',model:'',provider:''}); }
 // save failure keeps the typed key
 { const s=setup(async(u)=>u==='/set_key'?{ok:false,text:async()=>'Custom endpoints must use https.'}:resp({configured:false}));
   s.el('keyin').value='fake'; await vm.runInContext('saveKey()',s.ctx);
   assert.equal(s.el('keyin').value,'fake');
   assert.equal(s.el('keystatus').textContent,'Custom endpoints must use https.');
   assert.equal(s.el('keystatus').className,'bad'); }
 // opening Settings closes an open sidebar (mobile overlap fix)
 { const s=setup(async(u)=>u==='/providers'?resp(PRESETS):resp({configured:false}));
   s.el('sidebar').classList.add('open');
   await vm.runInContext('toggleSettings()',s.ctx);
   assert(s.el('settings').classList.contains('open'));
   assert(!s.el('sidebar').classList.contains('open'),'sidebar must close when Settings opens'); }
 // provider label from key_status renders in the mask line
 { const s=setup(async()=>resp({configured:true,masked:'nvapi\u2026001',source:'saved',model:'nvidia/some-model',provider:'NVIDIA NIM'}));
   await vm.runInContext('refreshKeyStatus()',s.ctx);
   assert(s.el('keymask').children.some(c=>typeof c==='string'&&c.includes('NVIDIA NIM')),JSON.stringify(s.el('keymask').children)); }
 // picker preselects the provider reported by key_status
 { const s=setup(async(u)=>u==='/providers'?resp(PRESETS):resp({configured:true,masked:'nvapi\u2026001',source:'saved',model:'m',provider:'NVIDIA NIM',provider_id:'nvidia'}));
   await vm.runInContext('refreshKeyStatus()',s.ctx);
   await vm.runInContext('loadProviders()',s.ctx);
   assert.equal(s.el('providersel').value,'nvidia'); }
 // a manually chosen provider survives a reload of the list
 { const s=setup(async(u)=>u==='/providers'?resp(PRESETS):resp({configured:true,masked:'nvapi\u2026001',source:'saved',model:'m',provider:'NVIDIA NIM',provider_id:'nvidia'}));
   await vm.runInContext('refreshKeyStatus()',s.ctx);
   await vm.runInContext('loadProviders()',s.ctx); s.el('providersel').value='gemini';
   await vm.runInContext('loadProviders()',s.ctx);
   assert.equal(s.el('providersel').value,'gemini'); }
 // discovery never chooses a returned model or overwrites a manual one
 { const s=setup(async(u)=>u==='/models'?resp({models:['first','second'],message:'Listed != quota'}):resp({configured:false}));
   s.el('modelin').value='manual'; s.el('providersel').value='gemini';
   await vm.runInContext('refreshModels(true)',s.ctx);
   assert.equal(s.el('modelin').value,'manual'); assert.equal(s.el('modelsel').value,'');
   s.el('modelsel').value='second'; vm.runInContext('selectDiscoveredModel()',s.ctx);
   assert.equal(s.el('modelin').value,'second'); }
 // provider changes preserve even a previously hinted model, and stale results are ignored
 { let done; const s=setup(async(u)=>u==='/models'?new Promise(r=>{done=r}):resp({configured:false}));
   s.el('modelin').value='gemini-flash-latest'; s.el('providersel').value='gemini';
   const pending=vm.runInContext('refreshModels()',s.ctx);
   vm.runInContext('discoveryVersion++',s.ctx); done(resp({models:['stale'],message:'stale'})); await pending;
   assert.equal(s.el('modelsel').children.length,1); assert.equal(s.el('modelin').value,'gemini-flash-latest'); }
 // errors leave manual entry available and render strings as text, not markup
 { const s=setup(async(u)=>resp({message:'<error> enter manually',models:[]},false));
   s.el('modelin').value='keep'; await vm.runInContext('refreshModels()',s.ctx);
   assert.equal(s.el('modelin').value,'keep'); assert.equal(s.el('modelstatus').textContent,'<error> enter manually');
   assert.equal(s.el('refreshmodels').disabled,false); }
 console.log('PASS provider picker UI');
})().catch(e=>{console.error(e);process.exitCode=1});
