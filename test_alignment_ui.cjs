const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(__dirname+'/webui.py','utf8');
const code=source.split('<script>')[1].split('</script>')[0];
const elements={};
function el(id){return elements[id] ||= {value:'',textContent:'',children:[],disabled:false,appendChild(c){this.children.push(c)},focus(){},addEventListener(){}}}
let payload;
const ctx={console,Set,Map,JSON,Promise,TextDecoder,setTimeout,fetch:async(url,opts)=>{payload=JSON.parse(opts.body);return {ok:false,text:async()=> 'probe stopped'}},document:{getElementById:el,createElement:()=>({textContent:''})}};
vm.createContext(ctx);vm.runInContext(code.replace('restoreTasks();','').replace('restoreHistory();',''),ctx);
(async()=>{
el('in').value='Write invitation';el('mode').value='task';el('requirements').value='Tagalog; numbered list; warm';
await vm.runInContext('send()',ctx);
assert.equal(payload.requirements,'Tagalog; numbered list; warm');
assert(source.includes('Generation requirements'),'visible requirements label');
assert(source.includes('not independently verified'),'visible verification limit');
console.log('PASS user requirements reach task API and UI explains limits');
})().catch(e=>{console.error(e);process.exitCode=1});
