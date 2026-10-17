const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync(__dirname+'/static/app.js','utf8');
function setup(fetch){const elements={};function el(id){return elements[id] ||= {value:'',textContent:'',children:[],disabled:false,isConnected:true,appendChild(c){this.children.push(c);if(id==='taskselect'&&!this.value)this.value=c.value},replaceChildren(){this.children=[]},focus(){},addEventListener(name,fn){this[name]=fn}}}const ctx={console,Set,Map,JSON,Promise,TextDecoder,fetch,setTimeout:f=>f(),document:{getElementById:el,createElement:()=>({textContent:'',isConnected:true,style:{},appendChild(c){(this.kids=this.kids||[]).push(c)},addEventListener(){}})}};vm.createContext(ctx);vm.runInContext(code.replace('restoreTasks();','').replace('restoreHistory();','').replace('loadPolicy();','').replace('refreshKeyStatus();\n',''),ctx);return {ctx,el}}
const response=obj=>({ok:true,json:async()=>obj});
function fakeBubble(){return {textContent:'',kids:[],isConnected:true,className:'',appendChild(c){this.kids.push(c)},insertBefore(c){this.kids.unshift(c)},addEventListener(){},classList:{add(){},remove(){},toggle(){},contains:()=>false}}}
(async()=>{
  // session totals accumulate in the context meter across calls
  const s=setup(async()=>response([]));
  await vm.runInContext("updateMeter({prompt_tokens:100,completion_tokens:5,total_prompt_tokens:100,total_completion_tokens:5,context_tokens:1000})",s.ctx);
  await vm.runInContext("updateMeter({prompt_tokens:150,completion_tokens:7,total_prompt_tokens:250,total_completion_tokens:12,context_tokens:1000})",s.ctx);
  const meter=s.el('ctxmeter').textContent;
  assert(meter.includes('150 in / 7 out'),'meter shows last-call tokens: '+meter);
  assert(meter.includes('session 250 in / 12 out'),'meter shows session totals: '+meter);
  console.log('PASS meter session totals');
  // durable task footer shows recorded usage
  const done=setup(async url=>url.includes('/events')?response([{seq:1,type:'result',text:'RESULT_SENTINEL'}]):response(url==='/tasks'?[]:{status:'completed',steps:3,attempts:3,budgets:{steps:12},elapsed:65,usage:{prompt_tokens:1234,completion_tokens:56}}));
  done.ctx.bubble=fakeBubble();
  await vm.runInContext("watchTask('u1',bubble)",done.ctx);
  const foot=done.ctx.bubble.kids.map(k=>k.textContent).join('\n');
  assert(foot.includes('1m 5s'),'footer keeps elapsed: '+foot);
  assert(foot.includes('tokens 1,234 in / 56 out'),'footer shows recorded usage: '+foot);
  console.log('PASS task footer usage');
  // footer is honest when the provider reported nothing
  const silent=setup(async url=>url.includes('/events')?response([{seq:1,type:'result',text:'RESULT_SENTINEL'}]):response(url==='/tasks'?[]:{status:'completed',steps:2,attempts:2,budgets:{steps:12},elapsed:3,usage:null}));
  silent.ctx.bubble=fakeBubble();
  await vm.runInContext("watchTask('u2',bubble)",silent.ctx);
  const foot2=silent.ctx.bubble.kids.map(k=>k.textContent).join('\n');
  assert(foot2.includes('token counts unavailable'),'footer admits unknown usage: '+foot2);
  assert(!foot2.includes('tokens 0 in / 0 out'),'no fake zero totals: '+foot2);
  console.log('PASS task footer unknown usage');
})().catch(e=>{console.error(e);process.exitCode=1});
