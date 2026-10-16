/* grok-bot UI: unified agent stream, conversations, approvals, legacy durable tasks, settings, skills.
   Written defensively so the Node .cjs tests can execute it against a minimal fake DOM. */
const chat = document.getElementById('chat');
const wrap = document.getElementById('chatwrap');
const inp = document.getElementById('in');
const sendBtn = document.getElementById('send');
const raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (f => f());
const hasDOM = typeof window !== 'undefined' && typeof document.createElement === 'function' && typeof window.Node === 'function';
let currentConversation = '';
let turnRunning = false;
let chatRunning = false;           // legacy name used by stop-button logic
const taskStatuses = new Map();
let usageTotals = {prompt: 0, completion: 0};
let policy = null;

/* ---------------- rendering ---------------- */
function scrollChat() { if (wrap) wrap.scrollTop = wrap.scrollHeight; }
function el(tag, cls, text) {
  const d = document.createElement(tag);
  if (cls) d.className = cls;
  if (text !== undefined) d.textContent = text;
  return d;
}
function add(cls, text) {
  const d = el('div', 'msg ' + cls, text);
  chat.appendChild(d);
  scrollChat();
  return d;
}
function renderMarkdown(target, text) {
  if (!target) return;
  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined' || !hasDOM) { target.textContent = text; return; }
  try {
    const html = DOMPurify.sanitize(marked.parse(text || '', {gfm: true, breaks: true}), {USE_PROFILES: {html: true}, ADD_ATTR: ['target']});
    target.innerHTML = html;
    target.classList.add('md');
    target.querySelectorAll('a[href]').forEach(a => { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
    target.querySelectorAll('pre').forEach(pre => {
      if (pre.querySelector('.copy')) return;
      const b = el('button', 'copy', 'copy');
      b.addEventListener('click', () => {
        const code = pre.querySelector('code');
        const t = (code || pre).textContent;
        if (typeof navigator !== 'undefined' && navigator.clipboard) navigator.clipboard.writeText(t).then(() => { b.textContent = 'copied'; setTimeout(() => b.textContent = 'copy', 1200); });
      });
      pre.appendChild(b);
    });
  } catch (_) { target.textContent = text; }
}
function note(text, cls) { const d = el('div', 'sysnote' + (cls ? ' ' + cls : ''), text); chat.appendChild(d); scrollChat(); return d; }
function summarizeArgs(name, args) {
  if (!args || typeof args !== 'object') return '';
  if (args.command) return args.command;
  if (args.query) return args.query;
  if (args.url) return args.url;
  if (args.pattern) return args.pattern + (args.path ? ' in ' + args.path : '');
  if (args.path) return args.path + (args.offset ? ':' + args.offset : '');
  if (args.name) return args.name;
  if (args.expression) return args.expression;
  try { return JSON.stringify(args).slice(0, 120); } catch (_) { return ''; }
}
const toolCards = new Map();
function toolCard(ev) {
  const card = el('details', 'toolcard running');
  const sum = el('summary');
  sum.appendChild(el('b', '', ev.name));
  sum.appendChild(el('code', '', summarizeArgs(ev.name, ev.arguments)));
  const meta = el('span', 'tmeta', 'running');
  sum.appendChild(meta);
  card.appendChild(sum);
  const args = el('pre', 'targs');
  try { args.textContent = JSON.stringify(ev.arguments, null, 1); } catch (_) { args.textContent = ''; }
  card.appendChild(args);
  const out = el('pre', 'tout', '');
  card.appendChild(out);
  card._meta = meta; card._out = out;
  chat.appendChild(card);
  scrollChat();
  if (ev.id) toolCards.set(ev.id, card);
  return card;
}
function finishToolCard(ev) {
  const card = toolCards.get(ev.id);
  if (!card) return;
  card.className = 'toolcard ' + (ev.ok ? 'ok' : 'err');
  if (card._meta) card._meta.textContent = (ev.duration !== undefined ? ev.duration.toFixed(1) + 's' : '') + (ev.truncated ? ' · truncated' : '');
  if (card._out) card._out.textContent = ev.output || '';
  toolCards.delete(ev.id);
}
function approvalCard(ev) {
  const card = el('div', 'approval');
  card.appendChild(el('b', '', 'Approval needed: ' + ev.tool));
  card.appendChild(el('span', '', ' — ' + (ev.reason || '')));
  card.appendChild(el('code', '', summarizeArgs(ev.tool, ev.arguments) || JSON.stringify(ev.arguments)));
  const btns = el('div', 'btns');
  const mk = (label, decision, cls) => {
    const b = el('button', cls, label);
    b.addEventListener('click', async () => {
      for (const x of btns.children || []) x.disabled = true;
      try {
        const res = await fetch('/agent/approve', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: ev.id, decision})});
        if (!res.ok) throw new Error(await res.text());
        card.classList && card.classList.add('resolved');
        card.appendChild(el('div', 'muted', 'Decision: ' + decision));
      } catch (e) { card.appendChild(el('div', 'sysnote err', 'Could not send decision: ' + e.message)); for (const x of btns.children || []) x.disabled = false; }
    });
    return b;
  };
  btns.appendChild(mk('Allow once', 'allow', 'allow'));
  btns.appendChild(mk('Always allow ' + (ev.always_pattern || '').replace(/^[^:]+:/, '') , 'always', ''));
  btns.appendChild(mk('Deny', 'deny', 'deny'));
  card.appendChild(btns);
  chat.appendChild(card);
  scrollChat();
  return card;
}
function renderEntry(entry) {
  if (entry.role === 'user') { add('you', entry.content); return; }
  const events = entry.events || [];
  for (const ev of events) {
    if (ev.type === 'tool_start') toolCard(ev);
    else if (ev.type === 'tool_end') finishToolCard(ev);
    else if (ev.type === 'approval') note('Approval: ' + ev.tool + ' → ' + ev.decision + (ev.reason ? ' (' + ev.reason + ')' : ''));
    else if (ev.type === 'compaction') note('Context compacted (' + ev.method + '): ' + ev.before_tokens + ' → ' + ev.after_tokens + ' tokens');
    else if (ev.type === 'error') note(ev.text, 'err');
    else if (ev.type === 'subagent_start') subagentCard(ev);
    else if (ev.type === 'subagent_end') finishSubagent(ev);
    else if (ev.type === 'memory_saved') note('Memory saved: ' + ev.text, 'mem');
    else if (ev.type === 'schedule_created') note('Scheduled: ' + ev.title + ' · ' + ev.when, 'mem');
  }
  const bubble = add('grok', '');
  renderMarkdown(bubble, entry.content || '');
}

/* ---------------- context info, sub-agents ---------------- */
function contextInfoRow(ev) {
  const box = el('details', 'ctxinfo');
  const sum = el('summary');
  const bits = [];
  if ((ev.memories || []).length) bits.push(ev.memories.length + ' memor' + (ev.memories.length === 1 ? 'y' : 'ies'));
  if ((ev.skills || []).length) bits.push(ev.skills.length + ' skill' + (ev.skills.length === 1 ? '' : 's'));
  if (ev.project_file) bits.push(ev.project_file);
  if ((ev.uploads || []).length) bits.push(ev.uploads.length + ' upload' + (ev.uploads.length === 1 ? '' : 's'));
  if ((ev.mcp || []).length) bits.push('MCP: ' + ev.mcp.join(', '));
  bits.push((ev.tools || 0) + ' tools');
  sum.textContent = 'Context: ' + bits.join(' · ');
  box.appendChild(sum);
  const body = el('div', 'ctxbody');
  for (const m of ev.memories || []) body.appendChild(el('div', 'ctxitem', (m.pinned ? '📌 ' : '• ') + m.text));
  if ((ev.skills || []).length) body.appendChild(el('div', 'ctxitem', 'Skills in index: ' + ev.skills.join(', ')));
  if (ev.project_file) body.appendChild(el('div', 'ctxitem', 'Project instructions: ' + ev.project_file));
  if ((ev.uploads || []).length) body.appendChild(el('div', 'ctxitem', 'Uploads: ' + ev.uploads.join(', ')));
  box.appendChild(body);
  chat.appendChild(box); scrollChat();
  return box;
}
const subagents = new Map();
function subagentCard(ev) {
  const card = el('details', 'subagent running');
  card.open = true;
  const sum = el('summary');
  sum.appendChild(el('b', '', (ev.agent || 'sub-agent') + (ev.depth > 1 ? ' (depth ' + ev.depth + ')' : '')));
  sum.appendChild(el('code', '', (ev.task || '').slice(0, 120)));
  const meta = el('span', 'tmeta', 'working'); sum.appendChild(meta);
  card.appendChild(sum);
  const body = el('div', 'subbody'); card.appendChild(body);
  card._meta = meta; card._body = body; card._text = ''; card._bubble = null;
  chat.appendChild(card); scrollChat();
  if (ev.agent) subagents.set(ev.agent, card);
  return card;
}
function finishSubagent(ev) {
  const card = subagents.get(ev.agent); if (!card) return;
  card.className = 'subagent ' + (ev.status === 'completed' ? 'ok' : 'err');
  if (card._meta) card._meta.textContent = (ev.status || '') + ' · ' + (ev.steps || 0) + ' steps · ' + (ev.tool_calls || 0) + ' tools';
  card.open = false;
  subagents.delete(ev.agent);
}
function subagentEvent(ev) {
  const card = subagents.get(ev.agent); if (!card) return false;
  const host = card._body;
  if (ev.type === 'text') {
    if (!card._bubble) { card._bubble = el('div', 'subtext', ''); host.appendChild(card._bubble); }
    card._text += ev.delta; card._bubble.textContent = card._text;
  } else if (ev.type === 'tool_start') {
    host.appendChild(toolCard(ev));
  } else if (ev.type === 'tool_end') finishToolCard(ev);
  else if (ev.type === 'approval_required') approvalCard(ev);
  else if (ev.type === 'subagent_done') { /* summarised by subagent_end */ }
  return true;
}

/* ---------------- conversations ---------------- */
async function loadConversations() {
  try {
    const res = await fetch('/conversations'); if (!res.ok) return;
    const rows = await res.json(); if (!Array.isArray(rows)) return;
    const list = document.getElementById('convlist'); if (!list) return;
    list.replaceChildren();
    for (const row of rows) {
      const item = el('div', 'conv' + (row.id === currentConversation ? ' active' : ''));
      item.dataset && (item.dataset.id = row.id);
      if (row.pinned) item.appendChild(el('span', 'pin', '★'));
      const title = el('span', 'title', row.title || 'New chat'); title.title = row.title || '';
      item.appendChild(title);
      const pin = el('button', 'act', row.pinned ? 'unpin' : 'pin'); pin.title = 'Pin';
      pin.addEventListener('click', async e => { e.stopPropagation(); await fetch('/conversations/' + row.id + '/pin', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pinned: !row.pinned})}); loadConversations(); });
      const ren = el('button', 'act', '✎'); ren.title = 'Rename';
      ren.addEventListener('click', async e => { e.stopPropagation(); const t = typeof prompt === 'function' ? prompt('Rename conversation', row.title || '') : null; if (t === null) return; await fetch('/conversations/' + row.id + '/rename', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({title: t})}); loadConversations(); if (row.id === currentConversation) setCrumb(t); });
      const del = el('button', 'act', '✕'); del.title = 'Delete';
      del.addEventListener('click', async e => { e.stopPropagation(); if (typeof confirm === 'function' && !confirm('Delete this conversation?')) return; await fetch('/conversations/' + row.id + '/delete', {method: 'POST'}); if (row.id === currentConversation) { currentConversation = ''; chat.replaceChildren(); } loadConversations(); });
      item.appendChild(pin); item.appendChild(ren); item.appendChild(del);
      item.addEventListener('click', () => openConversation(row.id));
      list.appendChild(item);
    }
  } catch (_) { /* sidebar is best-effort */ }
}
function setCrumb(text) { const c = document.getElementById('crumb'); if (c) c.textContent = text || 'Chat'; }
async function openConversation(id) {
  if (turnRunning) { document.getElementById('status').textContent = 'Wait for the current reply to finish.'; return; }
  try {
    const res = await fetch('/conversations/' + id + '/select', {method: 'POST'}); if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    currentConversation = data.id; setCrumb(data.title); setUrl(data.id);
    chat.replaceChildren(); toolCards.clear();
    for (const entry of data.entries || []) renderEntry(entry);
    if (!(data.entries || []).length) chat.appendChild(el('div', 'hint', 'Empty conversation.'));
    scrollChat(); loadConversations(); closeSidebar();
  } catch (e) { document.getElementById('status').textContent = 'Could not open conversation: ' + e.message; }
}
function setUrl(id) {
  try { if (hasDOM && window.history && window.history.replaceState) window.history.replaceState({cid: id}, '', id ? '/c/' + id : '/'); } catch (_) { /* sandboxed */ }
}
function exportConversation(fmt) {
  if (!currentConversation) { document.getElementById('status').textContent = 'Nothing to export yet.'; return; }
  const f = (typeof event !== 'undefined' && event && event.shiftKey) ? 'json' : (fmt || 'md');
  const a = document.createElement('a'); a.href = '/conversations/' + currentConversation + '/export?format=' + f; a.download = '';
  document.body.appendChild(a); a.click(); a.remove();
}
async function newConversation() {
  if (turnRunning) { document.getElementById('status').textContent = 'Wait for the current reply to finish.'; return; }
  try {
    const res = await fetch('/conversations', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    currentConversation = data.id; setCrumb('New chat'); setUrl(data.id);
    chat.replaceChildren(); chat.appendChild(el('div', 'hint', 'New chat. Workspace files and skills are kept.'));
    toolCards.clear(); loadConversations(); closeSidebar(); if (inp && inp.focus) inp.focus();
  } catch (e) { document.getElementById('status').textContent = e.message; }
}
let convSearchTimer;
function searchConversations() {
  const q = (document.getElementById('convsearch').value || '').trim();
  if (typeof clearTimeout === 'function') clearTimeout(convSearchTimer);
  convSearchTimer = setTimeout(async () => {
    if (!q) { loadConversations(); return; }
    try {
      const res = await fetch('/conversations/search?q=' + encodeURIComponent(q)); if (!res.ok) return;
      const rows = await res.json(); const list = document.getElementById('convlist'); if (!list || !Array.isArray(rows)) return;
      list.replaceChildren();
      if (!rows.length) list.appendChild(el('div', 'side-note', 'No matches.'));
      for (const r of rows) {
        const item = el('div', 'conv');
        item.appendChild(el('span', 'title', (r.title || 'chat') + ' — ' + (r.snippet || '').replace(/\[|\]/g, '')));
        item.addEventListener('click', () => openConversation(r.conv_id));
        list.appendChild(item);
      }
    } catch (_) { /* ignore */ }
  }, 250);
}
async function restoreHistory() {
  try {
    const res = await fetch('/conversations/current'); if (!res.ok) return;
    const data = await res.json();
    if (!data || !data.id) return;
    currentConversation = data.id; setCrumb(data.title); setUrl(data.id);
    if (Array.isArray(data.entries) && data.entries.length) { chat.replaceChildren(); for (const entry of data.entries) renderEntry(entry); }
    if (data.running) { note('A reply is still running in the background; reconnecting…'); attachCurrentTurn(); }
    loadConversations();
  } catch (_) { document.getElementById('status').textContent = 'Could not restore history'; }
}

/* ---------------- sending (unified agent) ---------------- */
function setRunning(on) {
  turnRunning = on; chatRunning = on; updateStopButton();
  sendBtn.disabled = on;
}
function slashCommand(text) {
  const m = text.match(/^\/(\w+)\s*(.*)$/s); if (!m) return false;
  const cmd = m[1].toLowerCase(), arg = m[2].trim();
  if (cmd === 'new') { newConversation(); return true; }
  if (cmd === 'compact') { compactNow(); return true; }
  if (cmd === 'skills') { toggleSkills(); return true; }
  if (cmd === 'files') { showFiles(); return true; }
  if (cmd === 'settings') { toggleSettings(); return true; }
  if (cmd === 'model') { if (arg) setModel(arg); else toggleSettings(); return true; }
  if (cmd === 'policy') { const [mode, approval] = arg.split(/\s+/); quickPolicy(mode, approval); return true; }
  if (cmd === 'memory' || cmd === 'memories') { toggleMemory(); return true; }
  if (cmd === 'remember' && arg) { addMemory(arg); return true; }
  if (cmd === 'schedule' || cmd === 'schedules' || cmd === 'automations') { toggleSchedules(); return true; }
  if (cmd === 'export') { exportConversation(arg === 'json' ? 'json' : 'md'); return true; }
  if (cmd === 'theme') { toggleTheme(arg); return true; }
  if (cmd === 'help') {
    add('grok', '').textContent = 'Commands: /new · /compact · /model <name> · /policy <read-only|workspace-write|full-access> [on-request|never] · /memory · /remember <fact> · /schedules · /skills · /files · /export [json] · /theme · /settings · /help';
    return true;
  }
  return false;
}
async function send() {
  const text = inp.value.trim();
  if (!text || sendBtn.disabled) return;
  if (text.startsWith('/') && slashCommand(text)) { inp.value = ''; return; }
  const background = !!(document.getElementById('background') && document.getElementById('background').checked);
  const images = attachments.splice(0, attachments.length); renderAttachments();
  const you = add('you', text);
  for (const img of images) { const im = document.createElement('img'); im.src = img.data_url; im.alt = img.name; im.className = 'thumb'; if (you.appendChild) you.appendChild(im); }
  inp.value = ''; if (inp.style) inp.style.height = 'auto';
  const status = document.getElementById('status');
  if (background) return sendBackgroundTask(text);
  setRunning(true);
  status.textContent = 'Thinking';
  let bubble = add('grok pending', '');
  let statusLine = null;
  if (hasDOM) { statusLine = el('div', 'tstatus running', 'orchestrating…'); chat.appendChild(statusLine); scrollChat(); }
  const dropStatusLine = () => { if (statusLine && typeof statusLine.remove === 'function') statusLine.remove(); statusLine = null; };
  let current = '';            // text in the active bubble
  let anyText = false, reasoningBox = null, pendingRender = false;
  const paint = () => { if (pendingRender) return; pendingRender = true; raf(() => { pendingRender = false; renderMarkdown(bubble, current); scrollChat(); }); };
  const finalizeBubble = () => { renderMarkdown(bubble, current); bubble.className = 'msg grok'; };
  const lastChild = () => chat.children[chat.children.length - 1];
  try {
    const res = await fetch('/agent', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text, conversation_id: currentConversation || undefined, images: images.length ? images : undefined})});
    if (!res.ok) throw new Error(await res.text());
    await consumeEventStream(res, ev => {
      if (ev.agent && ev.type !== 'subagent_start' && ev.type !== 'subagent_end' && subagentEvent(ev)) return;
      switch (ev.type) {
        case 'conversation': currentConversation = ev.id; if (ev.title) setCrumb(ev.title); setUrl(ev.id); break;
        case 'context_info': contextInfoRow(ev); break;
        case 'subagent_start': if (current) finalizeBubble(); subagentCard(ev); status.textContent = 'Sub-agent ' + ev.agent; break;
        case 'subagent_end': finishSubagent(ev); break;
        case 'memory_saved': note('Memory saved: ' + ev.text, 'mem'); refreshMemoryCount(); break;
        case 'schedule_created': note('Scheduled: ' + ev.title + ' · ' + ev.when, 'mem'); refreshScheduleCount(); break;
        case 'text':
          dropStatusLine();
          if (current && lastChild() !== bubble) { finalizeBubble(); bubble = add('grok pending', ''); current = ''; }
          current += ev.delta; anyText = true; paint(); status.textContent = 'Replying'; break;
        case 'reasoning':
          if (!reasoningBox) { reasoningBox = el('div', 'reasoning', ''); chat.appendChild(reasoningBox); }
          reasoningBox.textContent += ev.delta; break;
        case 'tool_start':
          if (current) finalizeBubble();
          toolCard(ev); status.textContent = 'Running ' + ev.name; break;
        case 'tool_end': finishToolCard(ev); break;
        case 'command_start': status.textContent = 'Running: ' + (ev.description || ev.command).slice(0, 60); break;
        case 'approval_required': approvalCard(ev); status.textContent = 'Waiting for your approval'; break;
        case 'approval': if (ev.id === undefined) note('Approval: ' + ev.tool + ' → ' + ev.decision + ' (' + (ev.reason || '') + ')'); break;
        case 'compaction': note('Context compacted (' + ev.method + '): ' + ev.before_tokens + ' → ' + ev.after_tokens + ' tokens'); break;
        case 'usage': updateMeter(ev); break;
        case 'retry': status.textContent = ev.text; break;
        case 'status': note(ev.text); break;
        case 'error': note(ev.text, 'err'); status.textContent = 'Error'; break;
        case 'done':
          if (ev.status === 'stopped') note('Stopped.');
          if (ev.status === 'max_steps') note('Step budget reached — say "continue" to keep going.');
          break;
        default: break;
      }
    });
    finalizeBubble();
    if (!anyText && !bubble.textContent) bubble.textContent = '';
    if (status.textContent !== 'Error') status.textContent = 'Ready';
  } catch (e) {
    note('Error: ' + e.message, 'err'); status.textContent = 'Error';
  } finally {
    dropStatusLine();
    bubble.className = 'msg grok';
    setRunning(false); if (inp.focus) inp.focus(); loadConversations();
  }
}
async function consumeEventStream(res, onEvent) {
  const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
  for (;;) {
    const {done, value} = await reader.read();
    if (done) break;
    buf += dec.decode(value, {stream: true});
    let pos;
    while ((pos = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, pos); buf = buf.slice(pos + 1);
      if (!line.trim()) continue;
      let ev; try { ev = JSON.parse(line); } catch (_) { continue; }
      onEvent(ev);
    }
  }
  if (buf.trim()) { try { onEvent(JSON.parse(buf)); } catch (_) {} }
}
async function attachCurrentTurn() {
  try {
    const res = await fetch('/agent/current'); if (!res.ok) return;
    const data = await res.json();
    if (!data.running) return;
    setRunning(true);
    for (const ev of data.events || []) if (ev.type === 'approval_required' && !ev.resolved) approvalCard(ev);
    const poll = async () => {
      const r = await fetch('/agent/current'); const d = await r.json();
      if (d.running) { setTimeout(poll, 1500); return; }
      setRunning(false); restoreHistory();
    };
    setTimeout(poll, 1500);
  } catch (_) { /* ignore */ }
}
async function sendBackgroundTask(text) {
  const status = document.getElementById('status');
  status.textContent = 'Task running';
  const bubble = add('grok tasklog', '');
  try {
    const checks = document.getElementById('checks').value.trim();
    const res = await fetch('/task', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text, requirements: document.getElementById('requirements').value.trim(), ...(checks ? {checks: JSON.parse(checks)} : {})})});
    if (!res.ok) throw new Error(await res.text());
    const task = await res.json();
    taskStatuses.set(task.id, 'queued'); updateStopButton();
    await watchTask(task.id, bubble);
  } catch (e) { bubble.textContent += '\n[Error: ' + e.message + ']'; status.textContent = 'Blocked'; }
  sendBtn.disabled = false; if (inp.focus) inp.focus();
}
async function compactNow() {
  const status = document.getElementById('status');
  status.textContent = 'Compacting…';
  try {
    const res = await fetch('/compact', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({conversation_id: currentConversation})});
    const data = await res.json(); if (!res.ok) throw new Error(data.error || 'failed');
    note('Context compacted (' + data.method + '): ' + data.before_tokens + ' → ' + data.after_tokens + ' tokens');
    status.textContent = 'Ready';
  } catch (e) { status.textContent = 'Compact failed: ' + e.message; }
}
function updateMeter(ev) {
  const m = document.getElementById('ctxmeter'); if (!m) return;
  if (typeof ev.prompt_tokens === 'number') usageTotals.prompt = ev.total_prompt_tokens || usageTotals.prompt;
  if (typeof ev.completion_tokens === 'number') usageTotals.completion = ev.total_completion_tokens || usageTotals.completion;
  const used = ev.prompt_tokens || ev.estimated_context || 0; const win = ev.context_tokens || 0;
  m.textContent = (win ? Math.round(100 * used / win) + '% ctx · ' : '') + (used ? used.toLocaleString() + ' in / ' + (ev.completion_tokens || 0).toLocaleString() + ' out' : '');
}

/* ---------------- stop / legacy durable tasks ---------------- */
async function stopTask() {
  const status = document.getElementById('status');
  try {
    const res = await fetch('/stop', {method: 'POST'});
    if (!res.ok) throw new Error(await res.text());
    const result = await res.json();
    status.textContent = result.requested === true ? 'Stop requested; waiting for current request' : 'No running task or chat to stop';
  } catch (e) { status.textContent = 'Stop request failed: ' + e.message; }
}
function updateStopButton() {
  document.getElementById('stop').disabled = !chatRunning && !Array.from(taskStatuses.values()).some(status => ['queued', 'running'].includes(status));
}
const watching = new Set();
const taskViews = new Map();
function taskBubble(id, bubble) {
  let view = taskViews.get(id);
  if (!view || view.bubble.isConnected === false) {
    view = {bubble: bubble || add('grok tasklog', 'Saved task\n'), after: 0};
    taskViews.set(id, view);
  }
  return view;
}
function noteFile(view, e) {
  if (e.type === 'file' && typeof e.text === 'string') (view.files = view.files || new Set()).add(e.text);
}
function showFileChips(view) {
  if (!view.files || !view.files.size || view.chipsShown === view.files.size) return;
  view.chipsShown = view.files.size;
  const bubble = add('grok', 'Files ready to download:\n');
  for (const name of view.files) {
    const link = document.createElement('a'); link.textContent = name; link.className = 'chip';
    link.href = '/files/' + name.split('/').map(encodeURIComponent).join('/');
    bubble.appendChild(link);
  }
  scrollChat();
}
const ROW_VERBS = {
  browse_open: ['Fetched', 'url'], browse_read: ['Read', 'url'], browse_click: ['Clicked', 'code'],
  browse_type: ['Typed', 'code'], read_file: ['Read', 'path'], read_skill: ['Read', 'path'],
  write_file: ['Created', 'path'], write_docx: ['Created', 'path'], save_skill: ['Saved', 'path'],
  copy_file: ['Copied', 'path'], compare_files: ['Compared', 'path'], calculate: ['Calculated', 'code'],
  list_files: ['Listed', 'files'], list_skills: ['Listed', 'skills'], browse_close: ['Closed', 'browser'],
};
function iconSvg() {
  const s = el('span', 'aicon');
  s.innerHTML = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M4 1.5h5.5L13 5v9.5H4z"/><path d="M9 1.5V5h4"/></svg>';
  return s;
}
function argRow(name, arg) {
  const def = ROW_VERBS[name] || ['Used', 'code'];
  const row = el('details', 'arow running');
  const sum = el('summary');
  sum.appendChild(iconSvg());
  sum.appendChild(el('span', 'averb', def[0]));
  if (arg) {
    if (def[1] === 'url') {
      const a = document.createElement('a');
      a.className = 'alink'; a.href = arg; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = arg.length > 72 ? arg.slice(0, 72) + '\u2026' : arg;
      sum.appendChild(a);
    } else {
      sum.appendChild(el('code', 'aarg', arg.length > 80 ? arg.slice(0, 80) + '\u2026' : arg));
    }
  }
  row._out = el('pre', 'aout', '');
  row.appendChild(sum);
  row.appendChild(row._out);
  return row;
}
function taskViewInit(view, bubble) {
  if (view.state) return view.state;
  if (typeof bubble.appendChild !== 'function') return null;
  view.state = {raw: [], lastCard: null, lastMeta: null, status: null, rawEl: null, rawDet: null, btns: [], title: null};
  bubble.textContent = '';
  bubble.className = 'msg grok';
  return view.state;
}
function taskStatusLine(st, bubble, text, cls) {
  if (!st) return;
  if (!st.status) { st.status = el('div', 'tstatus ' + (cls || 'running'), ''); bubble.appendChild(st.status); }
  else st.status.className = 'tstatus ' + (cls || '');
  st.status.textContent = text;
}
function finishTaskStatus(st, bubble, status) {
  const labels = {completed: 'completed', needs_input: 'needs your input', blocked: 'paused', interrupted: 'interrupted', stopped: 'stopped', failed: 'failed'};
  taskStatusLine(st, bubble, labels[status] || status, status === 'completed' ? 'done' : '');
}
function appendRawLog(view, bubble) {
  const st = view.state; if (!st || !st.raw.length || st.rawDet) return;
  const det = el('details', 'rawlog');
  det.appendChild(el('summary', '', 'raw log'));
  st.rawEl = el('pre', '', st.raw.join('\n'));
  det.appendChild(st.rawEl);
  st.rawDet = det;
  bubble.appendChild(det);
}
function taskEvent(view, bubble, e) {
  if (e.seq <= view.after) return;
  view.after = e.seq; noteFile(view, e);
  if (e.type === 'progress') return;
  const st = taskViewInit(view, bubble);
  if (!st) { bubble.textContent += '[' + e.type + '] ' + e.text + '\n'; return; }
  st.raw.push('[' + e.type + '] ' + e.text);
  if (st.rawEl) st.rawEl.textContent = st.raw.join('\n');
  const text = e.text || '';
  switch (e.type) {
    case 'status': taskStatusLine(st, bubble, text === 'Running' ? '' : text); break;
    case 'plan': if (text) bubble.appendChild(el('div', 'tplan', text.length > 220 ? text.slice(0, 220) + '…' : text)); break;
    case 'action': {
      st.lastCard = null; st.lastMeta = null;
      const m = text.match(/Executing (\S+)/);
      const row = argRow(m ? m[1] : (text.slice(0, 40) || 'tool'), e.arg || '');
      st.lastCard = row; st.lastMeta = row._out;
      bubble.appendChild(row);
      break;
    }
    case 'tool': {
      const row = st.lastCard;
      if (!row) { bubble.appendChild(el('div', 'tplan', text.length > 220 ? text.slice(0, 220) + '…' : text)); break; }
      const failed = text.startsWith('Tool failed');
      row.className = 'arow ' + (failed ? 'err' : 'ok');
      if (st.lastMeta) st.lastMeta.textContent = text.length > 4000 ? text.slice(0, 4000) + '…' : text;
      st.lastCard = null; st.lastMeta = null;
      break;
    }
    case 'error': bubble.appendChild(el('div', 'terr', text.slice(0, 300))); break;
    case 'verification': taskStatusLine(st, bubble, 'verifying result…'); break;
    case 'needs_input': taskStatusLine(st, bubble, 'waiting for you'); break;
    default: break;
  }
}
function collapseLog(bubble) {
  if (!bubble.classList) return;
  bubble.classList.add('done');
  if (!bubble.collapseBound && bubble.addEventListener) {
    bubble.collapseBound = true;
    bubble.addEventListener('click', () => bubble.classList.toggle('open'));
  }
}
async function showSelectedTask() {
  const id = document.getElementById('taskselect').value;
  if (id) await watchTask(id, taskBubble(id).bubble);
}
document.getElementById('taskselect').addEventListener('change', showSelectedTask);
async function watchTask(id, bubble) {
  if (watching.has(id)) return;
  const view = taskBubble(id, bubble); bubble = view.bubble;
  watching.add(id);
  if (view.state) {
    if (view.state.rawDet && typeof view.state.rawDet.remove === 'function') { view.state.rawDet.remove(); view.state.rawDet = null; view.state.rawEl = null; }
    for (const b of view.state.btns || []) if (b && typeof b.remove === 'function') b.remove();
    view.state.btns = [];
  }
  let retryText = '';
  try {
    for (;;) {
      const er = await fetch('/tasks/' + id + '/events?after=' + view.after);
      if (!er.ok) throw new Error('Could not load task events');
      const events = await er.json();
      for (const e of events) { if (e.type === 'retry') retryText = e.text; else if (e.type !== 'progress') retryText = ''; taskEvent(view, bubble, e); }
      const sr = await fetch('/tasks/' + id); if (!sr.ok) throw new Error('Could not load task status');
      const row = await sr.json();
      if (view.state && !view.state.title && row.goal) {
        const t = el('div', 'ttitle', row.goal.length > 90 ? row.goal.slice(0, 90) + '…' : row.goal);
        view.state.title = t; bubble.insertBefore(t, bubble.firstChild);
      }
      document.getElementById('status').textContent = (row.status === 'running' && retryText) ? retryText : row.status + ' · step ' + row.steps + (row.attempts === undefined ? '' : ' · provider attempts ' + row.attempts + '/' + row.budgets.steps);
      taskStatuses.set(id, row.status); updateStopButton();
      if (!['queued', 'running'].includes(row.status)) {
        for (;;) {
          const finalRes = await fetch('/tasks/' + id + '/events?after=' + view.after);
          if (!finalRes.ok) throw new Error('Could not load final task events');
          const finalEvents = await finalRes.json();
          for (const e of finalEvents) taskEvent(view, bubble, e);
          if (finalEvents.length < 200) break;
        }
        if (view.state) { finishTaskStatus(view.state, bubble, row.status); appendRawLog(view, bubble); }
        if (view.state && row.elapsed) { const s = Math.round(row.elapsed); bubble.appendChild(el('div', 'tmetafoot', s >= 60 ? Math.floor(s / 60) + 'm ' + (s % 60) + 's' : s + 's')); }
        else collapseLog(bubble);
        showFileChips(view);
        if (['blocked', 'interrupted', 'stopped', 'needs_input'].includes(row.status)) addResumeButton(id, bubble, view);
        break;
      }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  } finally { watching.delete(id); await restoreTasks(false); }
}
async function restoreTasks(watch = true) {
  try {
    const res = await fetch('/tasks'); if (!res.ok) return;
    const rows = await res.json(); const select = document.getElementById('taskselect');
    if (!Array.isArray(rows)) return;
    taskStatuses.clear();
    for (const row of rows) taskStatuses.set(row.id, row.status);
    updateStopButton();
    const selected = select.value; select.replaceChildren();
    for (const row of rows) {
      const option = document.createElement('option'); option.value = row.id;
      option.textContent = row.status + ': ' + row.goal.slice(0, 60); select.appendChild(option);
    }
    if (rows.some(row => row.id === selected)) select.value = selected;
    if (watch) {
      for (const row of rows.filter(row => ['queued', 'running'].includes(row.status))) {
        watchTask(row.id, taskBubble(row.id).bubble).catch(e => add('grok', e.message));
      }
      await showSelectedTask();
    }
  } catch (e) { document.getElementById('status').textContent = e.message; }
}
function addResumeButton(id, bubble, view) {
  const btn = document.createElement('button'); btn.className = 'resume-btn'; btn.textContent = 'Resume task';
  if (typeof btn.addEventListener !== 'function') return;
  if (view && view.state) {
    for (const old of view.state.btns) if (old && typeof old.remove === 'function') old.remove();
    view.state.btns = [btn];
  }
  btn.addEventListener('click', async ev => { ev.stopPropagation(); btn.disabled = true; btn.textContent = 'Resuming...'; const ok = await resumeTask(id); if (!ok) { btn.disabled = false; btn.textContent = 'Resume task'; } });
  bubble.appendChild(btn);
}
async function resumeTask(oneClickId) {
  const id = oneClickId || document.getElementById('taskselect').value; if (!id) return false;
  const answer = oneClickId ? '' : inp.value.trim();
  try {
    const checksText = oneClickId ? '' : document.getElementById('checks').value.trim();
    const res = await fetch('/tasks/' + id + '/resume', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({answer, ...(checksText ? {checks: JSON.parse(checksText)} : {})})});
    if (!res.ok) { let m = await res.text(); try { m = JSON.parse(m).error || m; } catch (_) {} document.getElementById('status').textContent = 'Could not resume: ' + m; return false; }
    taskStatuses.set(id, 'queued'); updateStopButton();
    if (!oneClickId) inp.value = '';
    document.getElementById('status').textContent = 'Resuming...';
    const view = taskBubble(id); if (view.bubble.classList) view.bubble.classList.remove('done', 'open'); watchTask(id, view.bubble).catch(e => add('grok', e.message));
    return true;
  } catch (e) { document.getElementById('status').textContent = 'Resume failed: ' + e.message; return false; }
}

/* ---------------- uploads & files ---------------- */
async function uploadFile(input) {
  const file = input.files[0]; if (!file) return;
  const status = document.getElementById('status');
  if (file.size > 1048576) { status.textContent = 'File too large (1 MB maximum)'; input.value = ''; return; }
  status.textContent = 'Reading upload…';
  const form = new FormData(); form.append('file', file);
  try {
    const res = await fetch('/upload', {method: 'POST', body: form});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Upload failed');
    add('grok', 'Uploaded ' + data.path + ' (' + data.characters + ' characters). The agent can read it with read_file.');
    status.textContent = 'Upload ready';
  } catch (e) { status.textContent = e.message; }
  input.value = '';
}
async function showFiles() {
  const res = await fetch('/files');
  if (!res.ok) { document.getElementById('status').textContent = 'Could not list files'; return; }
  const names = await res.json(); const bubble = add('grok', names.length ? 'Workspace files:\n' : 'No workspace files yet.');
  for (const name of names) {
    const link = document.createElement('a'); link.textContent = name; link.className = 'chip';
    link.href = '/files/' + name.split('/').map(encodeURIComponent).join('/');
    bubble.appendChild(link);
  }
  scrollChat();
}

/* ---------------- skills ---------------- */
let skillRevision = '';
let skillPinned = false;
function newSkill() {
  for (const id of ['skillname', 'skilldescription', 'skillcontent', 'skillselect']) document.getElementById(id).value = '';
  document.getElementById('skillname').disabled = false;
  skillRevision = ''; skillPinned = false; document.getElementById('skillstatus').textContent = 'New workflow';
}
async function refreshSkills() {
  try {
    const res = await fetch('/skills'); if (!res.ok) throw new Error(await res.text());
    const rows = await res.json(); const sel = document.getElementById('skillselect');
    const selected = sel.value; sel.replaceChildren();
    const blank = document.createElement('option'); blank.value = ''; blank.textContent = 'New skill'; sel.appendChild(blank);
    for (const row of rows) { const op = document.createElement('option'); op.value = row.name; op.textContent = row.name + ' [' + row.status + '] - ' + row.description; sel.appendChild(op); }
    sel.value = rows.some(r => r.name === selected) ? selected : '';
    document.getElementById('skillstatus').textContent = rows.filter(r => r.status !== 'archived').length + '/500 non-archived, ' + rows.filter(r => r.status === 'archived').length + ' archived';
  } catch (e) { document.getElementById('skillstatus').textContent = e.message; }
}
async function toggleSkills() {
  const panel = document.getElementById('skillsPanel'); const open = panel.hidden; showPanel('skillsPanel', open);
  if (open) await refreshSkills();
  closeSidebar();
}
async function loadSkill() {
  const name = document.getElementById('skillselect').value; if (!name) { newSkill(); return; }
  try {
    const res = await fetch('/skills/' + encodeURIComponent(name)); if (!res.ok) throw new Error(await res.text());
    const row = await res.json();
    document.getElementById('skillname').value = row.name; document.getElementById('skillname').disabled = true;
    document.getElementById('skilldescription').value = row.description; document.getElementById('skillcontent').value = row.content;
    skillRevision = row.revision; skillPinned = !!row.pinned; document.getElementById('skillstatus').textContent = 'Loaded workflow (' + row.status + ', ' + (row.created_by || 'legacy') + ')';
  } catch (e) { document.getElementById('skillstatus').textContent = e.message; }
}
async function saveSkill() {
  try {
    const res = await fetch('/skills', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
      name: document.getElementById('skillname').value, description: document.getElementById('skilldescription').value,
      content: document.getElementById('skillcontent').value, revision: skillRevision})});
    if (!res.ok) throw new Error(await res.text());
    const row = await res.json(); skillRevision = row.revision; await refreshSkills();
    document.getElementById('skillselect').value = row.name; await loadSkill();
    document.getElementById('skillstatus').textContent = 'Saved and read back: ' + row.name;
  } catch (e) { document.getElementById('skillstatus').textContent = e.message; }
}
async function restoreSkill() {
  const name = document.getElementById('skillselect').value; if (!name) return;
  try {
    const res = await fetch('/skills/' + encodeURIComponent(name) + '/restore', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({revision: skillRevision})});
    if (!res.ok) throw new Error(await res.text()); await refreshSkills();
    document.getElementById('skillstatus').textContent = 'Restored: ' + name;
  } catch (e) { document.getElementById('skillstatus').textContent = e.message; }
}
async function togglePinSkill() {
  const name = document.getElementById('skillselect').value; if (!name || !skillRevision) return;
  try {
    const res = await fetch('/skills/' + encodeURIComponent(name) + '/pin', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({revision: skillRevision, pinned: !skillPinned})});
    if (!res.ok) throw new Error(await res.text());
    const row = await res.json(); skillRevision = row.revision; skillPinned = !!row.pinned;
    await refreshSkills(); document.getElementById('skillselect').value = name;
    document.getElementById('skillstatus').textContent = (skillPinned ? 'Pinned: ' : 'Unpinned: ') + name;
  } catch (e) { document.getElementById('skillstatus').textContent = e.message; }
}
async function deleteSkill() {
  const name = document.getElementById('skillselect').value;
  if (!name || (typeof confirm === 'function' && !confirm('Delete saved skill ' + name + '?'))) return;
  try {
    const res = await fetch('/skills/' + encodeURIComponent(name) + '/delete', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({revision: skillRevision})});
    if (!res.ok) throw new Error(await res.text()); newSkill(); await refreshSkills();
  } catch (e) { document.getElementById('skillstatus').textContent = e.message; }
}

/* ---------------- settings: provider, model discovery, policy ---------------- */
let providerList = [];
let currentProviderId = '';
async function loadProviders() {
  try {
    const res = await fetch('/providers'); if (!res.ok) return;
    providerList = await res.json();
    const sel = document.getElementById('providersel');
    const current = sel.value; sel.replaceChildren();
    for (const p of providerList) {
      const op = document.createElement('option'); op.value = p.id; op.textContent = p.label; sel.appendChild(op);
    }
    const custom = document.createElement('option'); custom.value = 'custom'; custom.textContent = 'Custom URL (advanced)'; sel.appendChild(custom);
    const wanted = current || currentProviderId;
    if (providerList.some(p => p.id === wanted) || wanted === 'custom') sel.value = wanted;
    providerChanged();
  } catch (_) { /* leave the picker empty; Save still accepts a typed key */ }
}
function providerChanged() {
  const sel = document.getElementById('providersel');
  const custom = sel.value === 'custom';
  document.getElementById('urlin').style.display = custom ? '' : 'none';
  const p = providerList.find(x => x.id === sel.value);
  const mi = document.getElementById('modelin');
  mi.placeholder = p && p.model_hint ? 'Model (blank = ' + p.model_hint + ')' : 'Model (optional, blank = auto)';
  document.getElementById('modelsurl').style.display = custom ? '' : 'none';
  discoveryChanged();
}
let discoveryVersion = 0;
let discoveryTimer;
let discoveredDetails = {};
function clearModelChoices() {
  const sel = document.getElementById('modelsel');
  sel.replaceChildren();
  const op = document.createElement('option'); op.value = ''; op.textContent = 'Choose a listed model (optional)'; sel.appendChild(op);
  sel.value = '';
}
function keyLooksComplete(key) {
  // Avoid sending partial keys to the provider while the user is still typing (PR #17 review item A).
  return !key || (key.length >= 20 && !/\s/.test(key));
}
function discoveryChanged() {
  discoveryVersion++;
  clearModelChoices();
  document.getElementById('modelstatus').textContent = 'Model list changed. Manual entry always works.';
  if (typeof clearTimeout === 'function') clearTimeout(discoveryTimer);
  if (!keyLooksComplete(document.getElementById('keyin').value)) return;
  discoveryTimer = setTimeout(() => refreshModels(false), 600);
}
function selectDiscoveredModel() {
  const sel = document.getElementById('modelsel');
  if (sel.value) {
    document.getElementById('modelin').value = sel.value;
    const d = discoveredDetails[sel.value];
    if (d && d.context_length && document.getElementById('ctxin')) document.getElementById('ctxin').value = d.context_length;
  }
}
async function refreshModels(force = true) {
  const version = ++discoveryVersion;
  const sel = document.getElementById('providersel');
  const custom = sel.value === 'custom';
  const st = document.getElementById('modelstatus');
  const btn = document.getElementById('refreshmodels');
  clearModelChoices();
  btn.disabled = true; st.textContent = 'Loading model list...';
  try {
    const res = await fetch('/models', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: custom ? '' : sel.value, key: document.getElementById('keyin').value,
        url: custom ? document.getElementById('urlin').value : '',
        models_url: custom ? document.getElementById('modelsurl').value : '', refresh: force})});
    const r = await res.json();
    if (version !== discoveryVersion) return;
    st.textContent = r.message || 'Model listing unavailable. Enter a model manually.';
    if (!res.ok) return;
    discoveredDetails = r.details || {};
    for (const id of r.models || []) {
      const op = document.createElement('option'); op.value = id;
      const d = discoveredDetails[id];
      op.textContent = id + (d && d.context_length ? ' · ' + Math.round(d.context_length / 1000) + 'k ctx' : '');
      document.getElementById('modelsel').appendChild(op);
    }
    document.getElementById('modelsel').value = '';
  } catch (_) {
    if (version === discoveryVersion) st.textContent = 'Could not load models. Enter a model manually.';
  } finally {
    if (version === discoveryVersion) btn.disabled = false;
  }
}
async function toggleSettings() {
  loadCapabilities();
  const open = document.getElementById('settings').classList.toggle('open');
  if (open) { await refreshKeyStatus(); loadProviders(); loadPolicy(); }
  closeSidebar();
}
function closeSidebar() { const side = document.getElementById('sidebar'); if (side && side.classList && side.classList.contains('open')) side.classList.remove('open'); }
function setKeyStatus(text, cls, busy) {
  const e = document.getElementById('keystatus');
  e.textContent = text; e.className = (cls || '') + (busy ? ' dots' : '');
}
async function refreshKeyStatus() {
  const box = document.getElementById('keymask');
  try {
    const res = await fetch('/key_status'); if (!res.ok) return;
    const s = await res.json();
    currentProviderId = s.provider_id || '';
    const pill = document.getElementById('modelpill'); if (pill) pill.textContent = (s.model || 'model?') + (s.provider ? ' · ' + s.provider : '');
    const ctxin = document.getElementById('ctxin'); if (ctxin && s.context_length && !ctxin.value) ctxin.value = s.context_length;
    box.textContent = '';
    if (!s.configured) { box.textContent = 'No key saved yet.'; return; }
    const b = document.createElement('b'); b.textContent = s.masked;
    box.append('Key in use: ', b, ' (' + s.source + ') \u00b7 ' + s.model + (s.provider ? ' \u00b7 ' + s.provider : '') + (s.context_length ? ' \u00b7 ' + s.context_length + ' ctx' : ''));
  } catch (_) { /* leave previous text */ }
}
async function testKey() {
  const btn = document.getElementById('testbtn');
  btn.disabled = true; btn.textContent = 'Testing'; btn.classList.add('dots');
  setKeyStatus('Sending one tiny request', '', true);
  try {
    const res = await fetch('/test_key', {method: 'POST'});
    const r = await res.json();
    setKeyStatus((r.ok ? 'OK: ' : (r.status ? 'Failed (' + r.status + '): ' : 'Failed: ')) + r.message, r.ok ? 'ok' : 'bad');
  } catch (e) { setKeyStatus('Could not run the test: ' + e.message, 'bad'); }
  btn.disabled = false; btn.textContent = 'Test'; btn.classList.remove('dots');
}
function toggleSidebar() {
  const side = document.getElementById('sidebar');
  if (side.classList) side.classList.toggle('open');
}
async function saveKey() {
  const sel = document.getElementById('providersel');
  const custom = sel && sel.value === 'custom';
  const ctxin = document.getElementById('ctxin');
  const body = {key: keyin.value, url: custom ? urlin.value : '', model: modelin.value, provider: custom ? '' : (sel ? sel.value : '')};
  if (ctxin && ctxin.value && /^\d+$/.test(ctxin.value)) body.context_length = parseInt(ctxin.value, 10);
  const saveRes = await fetch('/set_key', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const st = await saveRes.text();
  setKeyStatus(st, saveRes.ok ? 'ok' : 'bad');
  if (saveRes.ok) { keyin.value = ''; await refreshKeyStatus(); discoveryChanged(); }
}
async function setModel(name) {
  try {
    const res = await fetch('/set_model', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({model: name})});
    const t = await res.text(); note(t, res.ok ? '' : 'err'); refreshKeyStatus();
  } catch (e) { note('Could not switch model: ' + e.message, 'err'); }
}
async function loadPolicy() {
  try {
    const res = await fetch('/policy'); if (!res.ok) return;
    const p = await res.json(); policy = p;
    const sm = document.getElementById('sandboxmode'), am = document.getElementById('approvalmode'), nm = document.getElementById('networkmode');
    if (sm) sm.value = p.mode; if (am) am.value = p.approval; if (nm) nm.checked = !!p.network;
    const badge = document.getElementById('policybadge'); if (badge) badge.textContent = 'policy: ' + p.mode + ' · ' + p.approval + (p.network ? ' · net' : '');
    const rules = document.getElementById('allowrules'); if (rules) rules.textContent = (p.allow && p.allow.length) ? 'Saved allow rules: ' + p.allow.join(', ') : 'No saved allow rules.';
    const info = document.getElementById('sandboxinfo');
    if (info && p.sandbox) info.textContent = 'Execution backend: ' + p.sandbox.backend + (p.sandbox.isolated ? ' (isolated container)' : ' (host subprocess — not a security boundary; approvals guard risky commands)') + ' · search: ' + (p.search_backend || 'off') + ' · tools: ' + (p.tools || []).join(', ');
  } catch (_) { /* ignore */ }
}
async function savePolicy() {
  const body = {mode: document.getElementById('sandboxmode').value, approval: document.getElementById('approvalmode').value, network: !!document.getElementById('networkmode').checked};
  try {
    const res = await fetch('/policy', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    document.getElementById('policystatus').textContent = res.ok ? 'Policy saved.' : await res.text();
    await loadPolicy();
  } catch (e) { document.getElementById('policystatus').textContent = e.message; }
}
async function clearAllowRules() {
  try {
    const res = await fetch('/policy', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({allow: []})});
    document.getElementById('policystatus').textContent = res.ok ? 'Allow rules cleared.' : await res.text();
    await loadPolicy();
  } catch (e) { document.getElementById('policystatus').textContent = e.message; }
}
async function quickPolicy(mode, approval) {
  const body = {}; if (mode) body.mode = mode; if (approval) body.approval = approval;
  const res = await fetch('/policy', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  note(res.ok ? 'Policy updated.' : await res.text(), res.ok ? '' : 'err'); loadPolicy();
}
async function clearChat() { return newConversation(); }
function toggleAdvanced() { const a = document.getElementById('advanced'); if (a) a.hidden = !a.hidden; }

/* ---------------- composer events & init ---------------- */
inp.addEventListener('input', () => { if (inp.style) { inp.style.height = 'auto'; inp.style.height = Math.min(inp.scrollHeight || 0, 200) + 'px'; } });
inp.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  if (e.key === 'Escape' && turnRunning) { stopTask(); }
});
/* ---------------- attachments (images), drag-drop, paste ---------------- */
const attachments = [];
function renderAttachments() {
  const box = document.getElementById('attachments'); if (!box || typeof box.replaceChildren !== 'function') return;
  box.replaceChildren();
  box.hidden = attachments.length === 0;
  attachments.forEach((img, i) => {
    const chip = el('span', 'chip');
    const im = document.createElement('img'); im.src = img.data_url; im.alt = img.name; chip.appendChild(im);
    chip.appendChild(el('span', '', img.name));
    const x = el('button', 'x', '×'); x.title = 'Remove'; x.addEventListener('click', () => { attachments.splice(i, 1); renderAttachments(); });
    chip.appendChild(x); box.appendChild(chip);
  });
}
function attachImages(files) {
  const status = document.getElementById('status');
  for (const file of Array.from(files || [])) {
    if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) { status.textContent = 'Only PNG, JPEG, WebP or GIF images can be attached.'; continue; }
    if (file.size > 2 * 1024 * 1024) { status.textContent = file.name + ' is over 2 MB.'; continue; }
    if (attachments.length >= 4) { status.textContent = 'At most 4 images per message.'; break; }
    const reader = new FileReader();
    reader.onload = () => { attachments.push({name: file.name, data_url: String(reader.result)}); renderAttachments(); };
    reader.readAsDataURL(file);
  }
}
function handleDroppedFiles(files) {
  const images = [], docs = [];
  for (const f of Array.from(files || [])) (f.type && f.type.startsWith('image/') ? images : docs).push(f);
  if (images.length) attachImages(images);
  for (const f of docs) uploadFile({files: [f], value: ''});
}
if (hasDOM) {
  const composer = document.getElementById('composer');
  if (composer) {
    ['dragenter', 'dragover'].forEach(t => composer.addEventListener(t, e => { e.preventDefault(); composer.classList.add('drop'); }));
    ['dragleave', 'drop'].forEach(t => composer.addEventListener(t, e => { e.preventDefault(); composer.classList.remove('drop'); }));
    composer.addEventListener('drop', e => { if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) handleDroppedFiles(e.dataTransfer.files); });
  }
  inp.addEventListener('paste', e => {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    const files = []; for (const it of items) if (it.kind === 'file' && it.type.startsWith('image/')) files.push(it.getAsFile());
    if (files.length) { e.preventDefault(); attachImages(files); }
  });
}

/* ---------------- memory panel ---------------- */
function showPanel(id, on) {
  for (const pid of ['memoryPanel', 'schedulePanel', 'skillsPanel']) { const p = document.getElementById(pid); if (p) p.hidden = pid === id ? !on : true; }
}
async function refreshMemoryCount() {
  try { const res = await fetch('/memories'); if (!res.ok) return; const rows = await res.json(); const c = document.getElementById('memcount'); if (c) c.textContent = rows.length ? String(rows.length) : ''; } catch (_) { /* ignore */ }
}
async function toggleMemory() {
  const panel = document.getElementById('memoryPanel'); if (!panel) return;
  const open = panel.hidden; showPanel('memoryPanel', open); closeSidebar();
  if (open) refreshMemories();
}
function memoryRow(m) {
  const row = el('div', 'item' + (m.pinned ? ' pinned' : ''));
  const text = el('div', 'itext', (m.pinned ? '📌 ' : '') + m.text);
  const meta = el('div', 'imeta', (m.tags || []).join(', ') + (m.source ? ' · ' + m.source : ''));
  const acts = el('div', 'iacts');
  const mk = (label, fn) => { const b = el('button', '', label); b.addEventListener('click', fn); acts.appendChild(b); };
  mk(m.pinned ? 'Unpin' : 'Pin', async () => { await fetch('/memories/' + m.id, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pinned: !m.pinned})}); refreshMemories(); });
  mk('Edit', async () => {
    const next = typeof prompt === 'function' ? prompt('Edit memory', m.text) : null; if (next === null || !next.trim()) return;
    const res = await fetch('/memories/' + m.id, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: next})});
    document.getElementById('memstatus').textContent = res.ok ? 'Updated.' : await res.text(); refreshMemories();
  });
  mk('Forget', async () => { await fetch('/memories/' + m.id + '/delete', {method: 'POST'}); refreshMemories(); });
  row.append(text, meta, acts);
  return row;
}
async function refreshMemories() {
  const list = document.getElementById('memlist'); if (!list) return;
  const q = (document.getElementById('memsearch') || {}).value || '';
  try {
    const res = await fetch('/memories' + (q ? '?q=' + encodeURIComponent(q) : '')); if (!res.ok) throw new Error(await res.text());
    const rows = await res.json();
    list.replaceChildren();
    if (!rows.length) list.appendChild(el('div', 'muted', q ? 'No matches.' : 'No memories yet. Tell the agent "remember that …" or add one above.'));
    for (const m of rows) list.appendChild(memoryRow(m));
    const c = document.getElementById('memcount'); if (c && !q) c.textContent = rows.length ? String(rows.length) : '';
  } catch (e) { document.getElementById('memstatus').textContent = e.message; }
}
async function addMemory(textArg) {
  const textEl = document.getElementById('memtext'), tagsEl = document.getElementById('memtags');
  const text = (textArg || (textEl && textEl.value) || '').trim(); if (!text) return;
  const tags = ((tagsEl && tagsEl.value) || '').split(',').map(t => t.trim()).filter(Boolean);
  const res = await fetch('/memories', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text, tags})});
  const st = document.getElementById('memstatus');
  if (!res.ok) { st.textContent = await res.text(); return; }
  if (textEl) textEl.value = ''; if (tagsEl) tagsEl.value = '';
  st.textContent = 'Remembered.'; if (textArg) note('Memory saved: ' + text, 'mem');
  const panel = document.getElementById('memoryPanel'); if (panel && !panel.hidden) refreshMemories(); else refreshMemoryCount();
}

/* ---------------- schedules panel ---------------- */
async function refreshScheduleCount() {
  try { const res = await fetch('/schedules'); if (!res.ok) return; const rows = await res.json(); const c = document.getElementById('schedcount'); if (c) c.textContent = rows.length ? String(rows.length) : ''; } catch (_) { /* ignore */ }
}
async function toggleSchedules() {
  const panel = document.getElementById('schedulePanel'); if (!panel) return;
  const open = panel.hidden; showPanel('schedulePanel', open); closeSidebar();
  if (open) refreshSchedules();
}
function fmtTime(ts) { if (!ts) return '—'; try { return new Date(ts * 1000).toLocaleString(); } catch (_) { return String(ts); } }
function scheduleRow(j) {
  const row = el('div', 'item' + (j.enabled ? '' : ' off'));
  row.appendChild(el('div', 'itext', (j.enabled ? '⏰ ' : '⏸ ') + j.title + ' — ' + j.when));
  row.appendChild(el('div', 'imeta', 'next ' + (j.enabled ? fmtTime(j.next_run) : 'paused') + ' · last ' + (j.last_status || 'never') + (j.last_run ? ' (' + fmtTime(j.last_run) + ')' : '') + ' · runs ' + (j.runs || 0)));
  const pr = el('div', 'iprompt', j.prompt); row.appendChild(pr);
  if (j.last_result) row.appendChild(el('div', 'imeta', 'last result: ' + j.last_result.slice(0, 200)));
  const acts = el('div', 'iacts');
  const mk = (label, fn) => { const b = el('button', '', label); b.addEventListener('click', fn); acts.appendChild(b); };
  mk('Run now', () => scheduleAction(j.id, 'run'));
  mk(j.enabled ? 'Pause' : 'Resume', () => scheduleAction(j.id, 'toggle'));
  mk('Delete', () => scheduleAction(j.id, 'delete'));
  row.appendChild(acts);
  return row;
}
async function refreshSchedules() {
  const list = document.getElementById('schedlist'); if (!list) return;
  try {
    const res = await fetch('/schedules'); if (!res.ok) throw new Error(await res.text());
    const rows = await res.json();
    list.replaceChildren();
    if (!rows.length) list.appendChild(el('div', 'muted', 'No automations yet.'));
    for (const j of rows) list.appendChild(scheduleRow(j));
    const c = document.getElementById('schedcount'); if (c) c.textContent = rows.length ? String(rows.length) : '';
  } catch (e) { document.getElementById('schedstatus').textContent = e.message; }
}
async function addSchedule() {
  const when = (document.getElementById('schedwhen') || {}).value || '', prompt_ = (document.getElementById('schedprompt') || {}).value || '', title = (document.getElementById('schedtitle') || {}).value || '';
  const st = document.getElementById('schedstatus');
  const res = await fetch('/schedules', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({when, prompt: prompt_, title})});
  if (!res.ok) { st.textContent = await res.text(); return; }
  st.textContent = 'Scheduled.'; document.getElementById('schedprompt').value = ''; refreshSchedules();
}
async function scheduleAction(id, action) {
  const st = document.getElementById('schedstatus');
  const res = await fetch('/schedules/' + id + '/' + action, {method: 'POST'});
  st.textContent = res.ok ? (action === 'run' ? 'Started — the result opens as a new ⏰ conversation.' : 'Done.') : await res.text();
  refreshSchedules();
}
let notifyTimer;
async function pollNotifications() {
  clearTimeout(notifyTimer);
  try {
    if (!turnRunning) {
      const res = await fetch('/notifications');
      if (res.ok) {
        const rows = await res.json();
        for (const n of rows || []) {
          const d = note('⏰ ' + n.title + ' → ' + n.status + (n.preview ? ': ' + n.preview.slice(0, 120) : ''), 'mem');
          if (n.conversation_id) { const b = el('button', 'linkbtn', 'open'); b.addEventListener('click', () => openConversation(n.conversation_id)); d.appendChild(b); }
        }
        if ((rows || []).length) { loadConversations(); refreshScheduleCount(); }
      }
    }
  } catch (_) { /* offline */ }
  notifyTimer = setTimeout(pollNotifications, 45000);
}

/* ---------------- skills import ---------------- */
async function importSkills(input) {
  const file = input.files && input.files[0]; if (!file) return;
  const st = document.getElementById('skillstatus');
  const fd = new FormData(); fd.append('file', file);
  try {
    const res = await fetch('/skills/import', {method: 'POST', body: fd});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    st.textContent = 'Imported ' + data.imported.length + ' skill(s)' + (data.errors.length ? '; ' + data.errors.length + ' failed: ' + data.errors.join('; ') : '.');
    refreshSkills();
  } catch (e) { st.textContent = 'Import failed: ' + e.message; }
  input.value = '';
}

/* ---------------- quick model switch ---------------- */
async function quickModelMenu() {
  const sel = document.getElementById('modelquick'), pill = document.getElementById('modelpill');
  if (!sel) return toggleSettings();
  if (!sel.hidden) { sel.hidden = true; return; }
  if (!currentProviderId) return toggleSettings();
  sel.replaceChildren(); sel.appendChild(new Option('Loading models…', '')); sel.hidden = false;
  try {
    const res = await fetch('/models', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({provider: currentProviderId, key: '', url: '', models_url: '', refresh: false})});
    const data = await res.json();
    if (!res.ok || !Array.isArray(data.models) || !data.models.length) throw new Error(data.message || 'No models listed');
    discoveredDetails = data.details || {};
    sel.replaceChildren(); sel.appendChild(new Option('Switch model…', ''));
    for (const m of data.models.slice(0, 400)) sel.appendChild(new Option(m, m));
  } catch (e) { sel.hidden = true; document.getElementById('status').textContent = e.message + ' — use Settings to type a model.'; toggleSettings(); }
}
async function quickModelPicked() {
  const sel = document.getElementById('modelquick'); if (!sel || !sel.value) return;
  const name = sel.value; sel.hidden = true;
  const d = discoveredDetails[name] || {};
  const res = await fetch('/set_model', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({model: name, context_length: d.context_length || undefined})});
  document.getElementById('status').textContent = res.ok ? 'Model: ' + name : await res.text();
  refreshKeyStatus();
}

/* ---------------- theme, voice, capabilities ---------------- */
function applyTheme(theme) {
  if (!hasDOM) return;
  if (theme === 'light' || theme === 'dark') document.documentElement.setAttribute('data-theme', theme); else document.documentElement.removeAttribute('data-theme');
  const b = document.getElementById('themebtn'); if (b) b.textContent = '◐ Theme: ' + (theme || 'auto');
  try { localStorage.setItem('grokbot-theme', theme || ''); } catch (_) { /* storage blocked */ }
}
function toggleTheme(force) {
  let cur = ''; try { cur = localStorage.getItem('grokbot-theme') || ''; } catch (_) { /* ignore */ }
  const next = force !== undefined && force !== '' ? force : (cur === '' ? 'light' : cur === 'light' ? 'dark' : '');
  applyTheme(next === 'auto' ? '' : next);
}
let recognizer = null;
function toggleMic() {
  const SR = hasDOM && (window.SpeechRecognition || window.webkitSpeechRecognition); const mic = document.getElementById('mic');
  if (!SR) return;
  if (recognizer) { recognizer.stop(); recognizer = null; if (mic) mic.classList.remove('on'); return; }
  recognizer = new SR(); recognizer.continuous = true; recognizer.interimResults = false; recognizer.lang = (navigator.language || 'en-US');
  recognizer.onresult = e => { let t = ''; for (let i = e.resultIndex; i < e.results.length; i++) if (e.results[i].isFinal) t += e.results[i][0].transcript; if (t) { inp.value = (inp.value ? inp.value + ' ' : '') + t.trim(); inp.dispatchEvent(new Event('input')); } };
  recognizer.onend = () => { recognizer = null; if (mic) mic.classList.remove('on'); };
  recognizer.onerror = () => { recognizer = null; if (mic) mic.classList.remove('on'); };
  recognizer.start(); if (mic) mic.classList.add('on');
}
async function loadCapabilities(force) {
  const box = document.getElementById('mcpinfo'), info = document.getElementById('integrationinfo'); if (!box) return;
  try {
    const res = await fetch('/capabilities'); if (!res.ok) return;
    const c = await res.json();
    box.replaceChildren();
    const servers = c.mcp || [];
    if (!servers.length) box.textContent = 'MCP servers: none configured.';
    else for (const s of servers) box.appendChild(el('div', '', 'MCP ' + s.name + ' (' + s.transport + '): ' + s.state + (s.tools ? ' · ' + s.tools + ' tools' : '') + (s.error ? ' · ' + s.error : '')));
    if (info) info.textContent = 'Telegram: ' + (c.telegram ? 'on' : 'off') + ' · plugins: ' + ((c.plugins || []).join(', ') || 'none') + ' · local provider: ' + (c.local_provider ? 'yes' : 'no') + ' · memories: ' + ((c.memory || {}).count || 0) + ' · automations: ' + ((c.schedules || {}).count || 0) + (c.sandbox ? ' · sandbox: ' + c.sandbox.backend : '');
  } catch (_) { /* ignore */ }
}
async function reloadMcp() {
  const box = document.getElementById('mcpinfo'); if (box) box.textContent = 'Reconnecting MCP servers…';
  try { await fetch('/mcp/reload', {method: 'POST'}); } catch (_) { /* ignore */ }
  setTimeout(() => loadCapabilities(true), 1500);
}

function boot() {
  try { applyTheme(localStorage.getItem('grokbot-theme') || 'dark'); } catch (_) { /* ignore */ }
  const mic = document.getElementById('mic'); if (mic && (window.SpeechRecognition || window.webkitSpeechRecognition)) mic.hidden = false;
  restoreTasks();
  const initial = document.body && document.body.dataset ? document.body.dataset.cid : '';
  if (initial) openConversation(initial).then(() => attachCurrentTurn()); else restoreHistory();
  loadPolicy();
  refreshKeyStatus();
  refreshMemoryCount();
  refreshScheduleCount();
  notifyTimer = setTimeout(pollNotifications, 20000);
  window.addEventListener('popstate', e => { const cid = (e.state && e.state.cid) || (location.pathname.startsWith('/c/') ? location.pathname.slice(3) : ''); if (cid && cid !== currentConversation) openConversation(cid); });
}
if (hasDOM) boot();
