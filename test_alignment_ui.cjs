const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(__dirname+'/templates/index.html','utf8');
const code=fs.readFileSync(__dirname+'/static/app.js','utf8');
const elements={};
function el(id){return elements[id] ||= {value:'',textContent:'',children:[],disabled:false,appendChild(c){this.children.push(c)},focus(){},addEventListener(){}}}
let payload;
const ctx={console,Set,Map,JSON,Promise,TextDecoder,setTimeout,fetch:async(url,opts)=>{payload=JSON.parse(opts.body);return {ok:false,text:async()=> 'probe stopped'}},document:{getElementById:el,createElement:()=>({textContent:''})}};
vm.createContext(ctx);vm.runInContext(code.replace('restoreTasks();','').replace('restoreHistory();','').replace('loadPolicy();','').replace('refreshKeyStatus();\n',''),ctx);
(async()=>{
el('in').value='Write invitation';el('background').checked=true;el('requirements').value='Tagalog; numbered list; warm';
await vm.runInContext('send()',ctx);
assert.equal(payload.requirements,'Tagalog; numbered list; warm');
assert(source.includes('Generation requirements'),'visible requirements label');
assert(source.includes('not independently verified'),'visible verification limit');
console.log('PASS user requirements reach the background task API and UI explains limits');
})().catch(e=>{console.error(e);process.exitCode=1});
