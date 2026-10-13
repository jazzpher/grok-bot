// Execute the shipped inline UI with deterministic DOM/network/timer seams.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const code = fs.readFileSync(__dirname + '/webui.py', 'utf8').split('<script>')[1].split('</script>')[0];
function setup(fetch) {
  const elements = {}, timers = [];
  const el = id => elements[id] ||= {value:'', textContent:'', children:[], disabled:id === 'stop', isConnected:true,
    appendChild(c) {this.children.push(c); if(id === 'taskselect' && !this.value) this.value = c.value;},
    replaceChildren() {this.children = [];}, focus() {}, addEventListener(name, fn) {this[name] = fn;}};
  const ctx = {console, Set, Map, JSON, Promise, TextDecoder, fetch, setTimeout:fn => timers.push(fn),
    document:{getElementById:el, createElement:() => ({textContent:'', isConnected:true, style:{}})}};
  vm.createContext(ctx);
  vm.runInContext(code.replace('restoreTasks();','').replace('restoreHistory();',''), ctx);
  return {ctx, el, timers, run:expr => vm.runInContext(expr, ctx)};
}
const response = obj => ({ok:true, json:async() => obj, text:async() => JSON.stringify(obj)});
const tick = () => new Promise(resolve => setImmediate(resolve));
const tests = [];
function test(name, fn) {tests.push({name, fn});}
test('global Stop never targets selected history', async() => {
  const calls = [];
  const s = setup(async url => {calls.push(url); return response(url.includes('/events') ? [] : url === '/tasks' ? [] : {status:'completed', steps:1});});
  s.el('taskselect').value = 'old'; await s.el('taskselect').change();
  await s.run('stopTask()');
  assert.equal(calls.at(-1), '/stop');
});
for (const historical of ['completed', 'needs_input']) {
  test('Stop stays enabled viewing ' + historical + ' during another run', async() => {
    let live = 'running'; const posts = [];
    const rows = () => [{id:'old', status:historical, goal:'history'}, {id:'live', status:live, goal:'work'}];
    const s = setup(async(url, opts) => {
      if (opts?.method === 'POST') {posts.push(url); return response({requested:true});}
      return response(url === '/tasks' ? rows() : url.includes('/events') ? [] : {status:url.includes('old') ? historical : live, steps:1});
    });
    s.el('taskselect').value = 'old';
    await s.run('restoreTasks()'); await tick();
    assert.equal(s.el('stop').disabled, false, 'history must not disable Stop');
    await s.run('stopTask()'); assert.equal(posts.at(-1), '/stop');
    s.timers.shift()(); await tick();
    await s.el('taskselect').change();
    assert.equal(s.el('stop').disabled, false, 'poll then selection must not disable Stop');
    if (historical === 'needs_input') {
      await s.run('resumeTask()'); assert.equal(posts.at(-1), '/tasks/old/resume');
    }
    live = 'completed'; s.timers.shift()(); await tick();
    assert.equal(s.el('stop').disabled, true, 'last live run completion disables Stop');
  });
}
test('list refresh enables queued work without watching history', async() => {
  let rows = [{id:'queued', status:'queued', goal:'work'}];
  const s = setup(async() => response(rows));
  await s.run('restoreTasks(false)'); assert.equal(s.el('stop').disabled, false);
  rows = []; await s.run('restoreTasks(false)'); assert.equal(s.el('stop').disabled, true);
});
test('Stop reports inactive, HTTP, and network results truthfully', async() => {
  for (const [result, expected] of [
    [response({requested:false}), 'No running task or chat to stop'],
    [response({requested:true}), 'Stop requested; waiting for current request'],
    [{ok:false, text:async() => 'denied'}, 'Stop request failed: denied'],
    [new Error('offline'), 'Stop request failed: offline']
  ]) {
    const s = setup(async() => {if(result instanceof Error) throw result; return result;});
    await s.run('stopTask()'); assert.equal(s.el('status').textContent, expected);
  }
});
test('active chat remains stoppable across task refresh and selection', async() => {
  let release; const calls = [];
  const s = setup(async(url) => {
    calls.push(url);
    if(url === '/chat') return {ok:true, body:{getReader:() => ({read:() => new Promise(r => {release = r;})})}};
    return response(url === '/stop' ? {requested:true} : url === '/tasks' || url.includes('/events') ? [] : {status:'completed', steps:1});
  });
  s.el('in').value = 'hello'; s.el('mode').value = 'chat';
  const sending = s.run('send()'); await tick();
  assert.equal(s.el('stop').disabled, false, 'chat Stop enabled');
  await s.run('restoreTasks(false)'); s.el('taskselect').value = 'old'; await s.el('taskselect').change();
  assert.equal(s.el('stop').disabled, false, 'history cannot disable chat Stop');
  await s.run('stopTask()'); assert.equal(calls.at(-1), '/stop');
  release({done:true}); await sending; assert.equal(s.el('stop').disabled, true);
});
(async() => {
  let failed = 0;
  for (const {name, fn} of tests) {
    if (process.env.STOP_TEST && !name.includes(process.env.STOP_TEST)) continue;
    try {await fn(); console.log('PASS ' + name);} catch(e) {failed++; console.error('FAIL ' + name, e);}
  }
  if (failed) process.exitCode = 1;
})();
