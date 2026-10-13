const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync(__dirname+'/webui.py','utf8').split('<script>')[1].split('</script>')[0];
function setup(fetch){const elements={};function el(id){return elements[id] ||= {value:'',textContent:'',children:[],disabled:false,isConnected:true,appendChild(c){this.children.push(c);if(id==='taskselect'&&!this.value)this.value=c.value},replaceChildren(){this.children=[]},focus(){},addEventListener(name,fn){this[name]=fn}}}const ctx={console,Set,Map,JSON,Promise,TextDecoder,fetch,setTimeout:f=>f(),document:{getElementById:el,createElement:()=>({textContent:'',isConnected:true,style:{}})}};vm.createContext(ctx);vm.runInContext(code.replace('restoreTasks();','').replace('restoreHistory();',''),ctx);return {ctx,el}}
const response=obj=>({ok:true,json:async()=>obj});
(async()=>{
 for(const terminal of ['completed','needs_input']){let count=0;const s=setup(async url=>url.includes('/events')?response(++count===1?[]:[{seq:1,type:terminal==='completed'?'result':'needs_input',text:'TERMINAL_SENTINEL'}]):response(url==='/tasks'?[]:{status:terminal,steps:1}));s.ctx.bubble={textContent:''};await vm.runInContext("watchTask('race',bubble)",s.ctx);assert(s.ctx.bubble.textContent.includes('TERMINAL_SENTINEL'),'terminal event must be drained after terminal status');console.log('PASS terminal race '+terminal)}
 const rows=[{id:'paused',status:'needs_input',goal:'question'},{id:'done',status:'completed',goal:'artifact'}];
 const s=setup(async url=>response(url==='/tasks'?rows:url.includes('/events')?[{seq:1,type:url.includes('paused')?'needs_input':'result',text:url.includes('paused')?'QUESTION_SENTINEL':'RESULT_SENTINEL'}]:{status:url.includes('paused')?'needs_input':'completed',steps:1}));
 await vm.runInContext('restoreTasks()',s.ctx); await new Promise(r=>setImmediate(r));
 assert(s.el('chat').children.some(e=>e.textContent.includes('QUESTION_SENTINEL')),'reload must restore selected paused question');
 s.el('taskselect').value='done'; assert.equal(typeof s.el('taskselect').change,'function','selector change handler required'); await s.el('taskselect').change();
 assert(s.el('chat').children.some(e=>e.textContent.includes('RESULT_SENTINEL')),'selector must restore completed result');
 await s.el('taskselect').change();
 assert.equal(s.el('chat').children.filter(e=>e.textContent.includes('RESULT_SENTINEL')).length,1,'deduplicate task bubbles');
 console.log('PASS saved reload and selector deduplication');
 const calls=[];const r=setup(async(url,opts)=>{calls.push({url,opts});return {ok:false,text:async()=> 'stop probe'}});r.el('taskselect').value='paused';r.el('checks').value='[{"type":"file_text","path":"out","text":"ok"}]';await vm.runInContext('resumeTask()',r.ctx);assert.equal(JSON.parse(calls[0].opts.body).checks[0].text,'ok','checks must reach resume endpoint');console.log('PASS resume UI checks');
})().catch(e=>{console.error(e);process.exitCode=1});
