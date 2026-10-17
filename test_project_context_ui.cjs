const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync(__dirname+'/grokbot/static/app.js','utf8');
function setup(fetch){const elements={};function el(id){return elements[id] ||= {value:'',textContent:'',children:[],disabled:false,isConnected:true,appendChild(c){this.children.push(c);if(id==='taskselect'&&!this.value)this.value=c.value},replaceChildren(){this.children=[]},focus(){},addEventListener(name,fn){this[name]=fn}}}const ctx={console,Set,Map,JSON,Promise,TextDecoder,fetch,setTimeout:f=>f(),document:{getElementById:el,createElement:()=>({textContent:'',isConnected:true,style:{},appendChild(c){(this.kids=this.kids||[]).push(c)},addEventListener(){}})}};vm.createContext(ctx);vm.runInContext(code.replace('restoreTasks();','').replace('restoreHistory();','').replace('loadPolicy();','').replace('refreshKeyStatus();\n',''),ctx);return {ctx,el}}
const response=obj=>({ok:true,json:async()=>obj});
function fakeBubble(){return {textContent:'',kids:[],isConnected:true,className:'',appendChild(c){this.kids.push(c)},insertBefore(c){this.kids.unshift(c)},addEventListener(){},classList:{add(){},remove(){},toggle(){},contains:()=>false}}}
(async()=>{
  const s=setup(async url=>url.includes('/events')?response([{seq:1,type:'context',text:'Loaded project instructions from AGENTS.md; they cannot grant new permissions.'},{seq:2,type:'result',text:'DONE'}]):response(url==='/tasks'?[]:{status:'completed',steps:1,attempts:1,budgets:{steps:12},elapsed:2}));
  s.ctx.bubble=fakeBubble();
  await vm.runInContext("watchTask('p1',bubble)",s.ctx);
  const shown=s.ctx.bubble.kids.map(k=>k.textContent).join('\n');
  assert(shown.includes('Loaded project instructions from AGENTS.md'),'context event visible in task view: '+shown);
  console.log('PASS task view shows project-instruction load event');
})().catch(e=>{console.error(e);process.exitCode=1});
