const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(__dirname+'/grokbot/templates/index.html','utf8');
const code=fs.readFileSync(__dirname+'/grokbot/static/app.js','utf8');
const elements={};
function el(id){return elements[id] ||= {value:'',textContent:'',children:[],disabled:false,appendChild(c){this.children.push(c)},focus(){},addEventListener(){}}}
let payload;
const ctx={console,Set,Map,JSON,Promise,TextDecoder,setTimeout,fetch:async(url,opts)=>{payload=JSON.parse(opts.body);return {ok:false,text:async()=> 'probe stopped'}},document:{getElementById:el,createElement:tag=>({tag,textContent:'',className:'',children:[],appendChild(c){this.children.push(c)}})}};
vm.createContext(ctx);vm.runInContext(code.replace('restoreTasks();','').replace('restoreHistory();','').replace('loadPolicy();','').replace('refreshKeyStatus();\n',''),ctx);
(async()=>{
el('in').value='Write invitation';el('background').checked=true;el('requirements').value='Tagalog; numbered list; warm';
await vm.runInContext('send()',ctx);
assert.equal(payload.requirements,'Tagalog; numbered list; warm');
assert(source.includes('Generation requirements'),'visible requirements label');
assert(source.includes('not independently verified'),'visible verification limit');
const rowOf=(name,arg)=>{ctx.__name=name;ctx.__arg=arg;return vm.runInContext('argRow(__name,__arg)',ctx)};
const anchors=row=>row.children[0].children.filter(c=>c.tag==='a');
for(const ok of ['http://example.com/a','https://example.com/a']){const a=anchors(rowOf('browse_open',ok));assert.equal(a.length,1,'http(s) url is a link');assert.equal(a[0].href,ok);assert.equal(a[0].rel,'noopener');}
for(const bad of ['javascript:alert(1)','JavaScript:alert(1)',' javascript:alert(1)','data:text/html,x','file:///etc/passwd','//evil.example/x','example.com']){const row=rowOf('browse_read',bad);assert.equal(anchors(row).length,0,'no link for '+bad);assert(row.children[0].children.some(c=>c.tag==='code'&&c.textContent===bad),'plain code text for '+bad);}
console.log('PASS browse url rows link only http(s)');
console.log('PASS user requirements reach the background task API and UI explains limits');
})().catch(e=>{console.error(e);process.exitCode=1});
