const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync(__dirname+'/grokbot/static/app.js','utf8');
function setup(){const elements={};function el(id){return elements[id] ||= {value:'',textContent:'',children:[],disabled:false,isConnected:true,appendChild(c){this.children.push(c)},replaceChildren(){this.children=[]},focus(){},addEventListener(n,f){this[n]=f}}}const ctx={console,Set,Map,JSON,Promise,TextDecoder,fetch:async()=>({ok:true,json:async()=>[]}),setTimeout:f=>f(),document:{getElementById:el,createElement:(tag)=>{if(tag==='img')return {src:'',alt:'',className:'',children:[],appendChild(c){this.children.push(c)}};return {textContent:'',isConnected:true,style:{},children:[],appendChild(c){this.children.push(c)},addEventListener(){}}}}};vm.createContext(ctx);vm.runInContext(code.replace('restoreTasks();','').replace('restoreHistory();','').replace('loadPolicy();','').replace('refreshKeyStatus();\n',''),ctx);return {ctx,el}}
const s=setup();
// A preview_image event renders the screenshot inside a tool card.
s.ctx.previewImageCard({name:'preview-index.html',data_url:'data:image/png;base64,QUJD'});
const chat=s.el('chat');
assert.equal(chat.children.length,1,'one card appended');
const imgs=[];(function walk(n){if(n&&n.src!==undefined&&n.className==='previewshot')imgs.push(n);for(const c of (n&&n.children)||[])walk(c)})({children:chat.children});
assert.equal(imgs.length,1,'screenshot img rendered');assert.equal(imgs[0].src,'data:image/png;base64,QUJD');assert.equal(imgs[0].alt,'preview-index.html');
// A non-image or missing data_url must NOT become an img (no javascript:/blob injection into src).
const chat2=setup();chat2.ctx.previewImageCard({name:'x',data_url:'javascript:alert(1)'});chat2.ctx.previewImageCard({name:'y'});
const imgs2=[];(function walk(n){if(n&&n.className==='previewshot')imgs2.push(n);for(const c of (n&&n.children)||[])walk(c)})({children:chat2.el('chat').children});
assert.equal(imgs2.length,0,'unsafe or missing data_url renders no img');
console.log('PASS preview image card rendering');
