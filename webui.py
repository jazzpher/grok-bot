#!/usr/bin/env python3
"""Local chat and bounded tasks. Public exposure requires GROK_APP_PASSWORD."""
import argparse
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
import requests
from flask import Flask, Response, request, render_template_string, session, redirect, send_file
from agent_loop import Workspace, run_loop

API_URL = os.environ.get('GROK_API_URL', 'https://api.x.ai/v1/chat/completions')
DEFAULT_MODEL = os.environ.get('GROK_MODEL', 'grok-4-fast')
SYSTEM_PROMPT = 'You are a helpful assistant. Do not claim to be a particular provider or model unless verified. Be honest about available tools.'
ALLOWED_API_URLS = {
    'https://api.x.ai/v1/chat/completions',
    'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
}
# A local provider may be configured by the server owner, never through public settings.
if urlparse(API_URL).hostname in ('127.0.0.1', 'localhost'):
    ALLOWED_API_URLS.add(API_URL)

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>grok-bot · Chat and Tasks</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #0d0f12; color: #e8eaed;
         display: flex; flex-direction: column; height: 100vh; }
  header { display: flex; align-items: center; justify-content: space-between;
           padding: 10px 16px; border-bottom: 1px solid #22262c; }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header button { background: #1c2026; color: #e8eaed; border: 1px solid #2a2f36;
                  border-radius: 8px; padding: 6px 12px; cursor: pointer; font-size: 13px; }
  header button:hover { background: #242a31; }
  #chat { flex: 1; overflow-y: auto; padding: 16px; display: flex;
          flex-direction: column; gap: 12px; }
  .msg { max-width: 78%; padding: 10px 14px; border-radius: 14px; white-space: pre-wrap;
         line-height: 1.5; font-size: 15px; }
  .you { align-self: flex-end; background: #24486b; border-bottom-right-radius: 4px; }
  .grok { align-self: flex-start; background: #1a1e24; border: 1px solid #262b32;
          border-bottom-left-radius: 4px; }
  #bar { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid #22262c; }
  #in { flex: 1; background: #1a1e24; color: #e8eaed; border: 1px solid #2a2f36;
        border-radius: 12px; padding: 12px 14px; font-size: 15px; resize: none;
        font-family: inherit; }
  #in:focus { outline: none; border-color: #3d7bd9; }
  #send { background: #2f6fdb; color: #fff; border: 0; border-radius: 12px;
          padding: 0 20px; font-size: 15px; cursor: pointer; }
  #send:disabled { background: #24314a; cursor: default; }
  .hint { text-align: center; color: #6b7280; font-size: 13px; padding: 8px; }
  #settings { display: none; padding: 10px 16px; border-bottom: 1px solid #22262c;
              flex-wrap: wrap; gap: 8px; align-items: center; }
  #settings.open { display: flex; }
  #settings input { background: #1a1e24; color: #e8eaed; border: 1px solid #2a2f36;
                    border-radius: 8px; padding: 8px 10px; font-size: 13px; flex: 1;
                    min-width: 160px; }
  #settings input:focus { outline: none; border-color: #3d7bd9; }
  #settings button { background: #1c2026; color: #e8eaed; border: 1px solid #2a2f36;
                     border-radius: 8px; padding: 8px 12px; cursor: pointer; font-size: 13px; }
  #keystatus { color: #6b7280; font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>grok-bot</h1>
  <div>
    <button onclick="toggleSettings()">Settings</button>
    <button onclick="clearChat()">New chat</button>
  </div>
</header>
<div id="settings">
  <input id="keyin" type="password" placeholder="API key (Gemini, Grok, ...)" autocomplete="off">
  <input id="urlin" placeholder="API URL (optional, blank = auto)" autocomplete="off">
  <input id="modelin" placeholder="Model (optional, blank = auto)" autocomplete="off">
  <button onclick="saveKey()">Save</button>
  <span id="keystatus"></span>
</div>
<div style="padding:8px 16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
<label>Mode <select id="mode"><option value="chat">Chat</option><option value="task">Task loop</option></select></label>
<button id="stop" onclick="stopTask()" disabled>Stop</button>
<button onclick="showFiles()">Workspace files</button>
<span id="status" role="status" aria-live="polite">Ready</span>
</div>
<div id="chat"><div class="hint">Chat or run a task. Tasks can calculate and read/write small workspace files. No browsing or computer control.</div></div>
<div id="bar">
  <textarea id="in" rows="1" placeholder="Message grok-bot..." autofocus></textarea>
  <button id="send" onclick="send()">Send</button>
</div>
<script>
const chat = document.getElementById('chat');
const inp = document.getElementById('in');
const sendBtn = document.getElementById('send');

function add(cls, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.textContent = text;
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
  return d;
}

async function send() {
  const text = inp.value.trim();
  if (!text || sendBtn.disabled) return;
  const isTask = document.getElementById('mode').value === 'task';
  add('you', text); inp.value = ''; sendBtn.disabled = true;
  document.getElementById('stop').disabled = !isTask;
  const status = document.getElementById('status');
  status.textContent = isTask ? 'Task running' : 'Replying';
  const bubble = add('grok', ''); let full = ''; let pending = '';
  try {
    const res = await fetch(isTask ? '/task' : '/chat', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text})
    });
    if (!res.ok) throw new Error(await res.text());
    const reader = res.body.getReader(); const dec = new TextDecoder();
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      const chunk = dec.decode(value, {stream: true});
      if (isTask) {
        pending += chunk;
        let pos;
        while ((pos = pending.indexOf('\n')) >= 0) {
          const line = pending.slice(0, pos); pending = pending.slice(pos + 1);
          if (!line.trim()) continue;
          const event = JSON.parse(line);
          status.textContent = 'Step ' + event.step + ': ' + event.type;
          if (event.type === 'progress') continue;
          full += '[' + event.type + '] ' + event.text + '\n\n';
          bubble.textContent = full;
        }
      } else { full += chunk; bubble.textContent = full; }
      chat.scrollTop = chat.scrollHeight;
    }
    if (!full) bubble.textContent = '[Empty reply]';
    if (!isTask) status.textContent = 'Ready';
  } catch (e) { bubble.textContent += '\n[Error: ' + e.message + ']'; status.textContent = 'Blocked'; }
  sendBtn.disabled = false; document.getElementById('stop').disabled = true; inp.focus();
}

async function stopTask() {
  const res = await fetch('/stop', {method: 'POST'});
  document.getElementById('status').textContent = res.ok ? 'Stop requested; waiting for current request' : 'Stop request failed';
}

async function showFiles() {
  const res = await fetch('/files');
  if (!res.ok) { document.getElementById('status').textContent = 'Could not list files'; return; }
  const names = await res.json(); const bubble = add('grok', names.length ? 'Workspace files:\n' : 'No workspace files yet.');
  for (const name of names) {
    const link = document.createElement('a'); link.textContent = name;
    link.href = '/files/' + name.split('/').map(encodeURIComponent).join('/');
    link.style.color = '#9ac6ff'; bubble.appendChild(link); bubble.appendChild(document.createElement('br'));
  }
}

function toggleSettings() {
  document.getElementById('settings').classList.toggle('open');
}

async function saveKey() {
  const saveRes = await fetch('/set_key', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key: keyin.value, url: urlin.value, model: modelin.value})
  });
  const st = await saveRes.text();
  document.getElementById('keystatus').textContent = st;
  if (saveRes.ok) keyin.value = '';
}

async function clearChat() {
  const res = await fetch('/clear', {method: 'POST'});
  if (!res.ok) { document.getElementById('status').textContent = await res.text(); return; }
  chat.innerHTML = '<div class="hint">New chat. Memory cleared.</div>';
}

inp.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
</script>
</body>
</html>"""

app = Flask(__name__)
app.secret_key = os.environ.get('GROK_SESSION_SECRET') or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=40000, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Strict',
                  SESSION_COOKIE_SECURE=os.environ.get('GROK_SECURE_COOKIE') == '1',
                  API_KEY=os.environ.get('GROK_API_KEY', ''), MODEL=DEFAULT_MODEL, MOCK=False)
PASSWORD = os.environ.get('GROK_APP_PASSWORD', '')
WORKSPACES = Path(os.environ.get('GROK_WORKSPACE_DIR', 'workspaces')).resolve()
SETTINGS_FILE = Path(__file__).resolve().parent / 'settings.json'
settings = {}
# Migration: read existing settings, but reject unsafe endpoints. No new key persistence.
try:
    old = json.loads(SETTINGS_FILE.read_text())
    if old.get('url_override', API_URL) in ALLOWED_API_URLS:
        settings.update({k: v for k, v in old.items() if k in ('key_override', 'url_override', 'model_override')})
except (OSError, ValueError):
    pass
clients = {}
clients_lock = threading.Lock()
provider_lock = threading.Lock()
login_failures = {}
LOGIN = """<!doctype html><html><meta name="viewport" content="width=device-width,initial-scale=1"><title>grok-bot login</title><body style="font-family:system-ui;max-width:400px;margin:80px auto;padding:24px"><h1>grok-bot</h1><p>This app is private.</p><form method="post"><input type="password" name="password" placeholder="App password" required autocomplete="current-password"><button>Log in</button></form><p>{{ error }}</p></body></html>"""

@app.before_request
def protect():
    if request.endpoint == 'login':
        return None
    if PASSWORD and not session.get('authenticated'):
        return redirect('/login') if request.method == 'GET' else Response('Log in first.', status=401)
    # Without a password only literal loopback host access is allowed; tunnels fail closed.
    if not PASSWORD and request.host.split(':')[0] not in ('127.0.0.1', 'localhost', '[::1]'):
        return Response('Public access disabled. Set GROK_APP_PASSWORD before using a tunnel.', status=403)
    if request.method == 'POST':
        origin = request.headers.get('Origin')
        if origin and urlparse(origin).netloc != request.host:
            return Response('Cross-origin writes denied.', status=403)
    if 'sid' not in session:
        session['sid'] = secrets.token_hex(16)

@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
    return response

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not PASSWORD:
        return redirect('/')
    error = ''
    if request.method == 'POST':
        ip = request.remote_addr or 'unknown'
        now = time.monotonic()
        recent = [t for t in login_failures.get(ip, []) if now - t < 300]
        if len(recent) >= 5:
            return Response('Too many attempts. Try again in five minutes.', status=429)
        if hmac.compare_digest(request.form.get('password', ''), PASSWORD):
            session.clear(); session['authenticated'] = True
            return redirect('/')
        recent.append(now); login_failures[ip] = recent; error = 'Wrong password.'
    return render_template_string(LOGIN, error=error)


def client():
    sid = session['sid']
    with clients_lock:
        if sid not in clients:
            if len(clients) >= 100:
                # Only inactive oldest sessions are evicted.
                inactive = [k for k, v in clients.items() if not v['busy'].locked()]
                if not inactive:
                    raise RuntimeError('All sessions busy.')
                del clients[inactive[0]]
            clients[sid] = {'history': [], 'busy': threading.Lock(), 'stop': threading.Event(),
                            'workspace': Workspace(WORKSPACES / sid)}
        return clients[sid]


def config_snapshot():
    with provider_lock:
        return (settings.get('key_override') or app.config['API_KEY'],
                settings.get('url_override') or API_URL,
                settings.get('model_override') or app.config['MODEL'])


def model_reply(messages, cfg, stream=False):
    key, url, model = cfg
    if url not in ALLOWED_API_URLS:
        raise ValueError('Unsupported provider URL.')
    res = requests.post(url, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
                        json={'model': model, 'messages': messages, 'stream': stream},
                        stream=stream, timeout=(10, 45))
    res.raise_for_status()
    return res


@app.route('/')
def index():
    return render_template_string(PAGE)

@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json(silent=True) or {}
    text = data.get('message')
    if not isinstance(text, str) or not text.strip() or len(text) > 12000:
        return Response('Send a message of 1–12000 characters.', status=400)
    c = client()
    if not c['busy'].acquire(False):
        return Response('This session is already running.', status=409)
    cfg = config_snapshot()
    def generate():
        chunks = []
        try:
            messages = [{'role': 'system', 'content': SYSTEM_PROMPT}, *c['history'][-24:], {'role': 'user', 'content': text}]
            if app.config['MOCK'] and not cfg[0]:
                answer = '(mock) got your message: ' + text
                chunks.append(answer); yield answer
            else:
                with model_reply(messages, cfg, stream=True) as res:
                    for raw in res.iter_lines():
                        line = raw.decode('utf-8')
                        if not line.startswith('data: '): continue
                        if line[6:].strip() == '[DONE]': break
                        try: piece = json.loads(line[6:])['choices'][0].get('delta', {}).get('content', '')
                        except (ValueError, KeyError, IndexError): continue
                        if piece: chunks.append(piece); yield piece
            c['history'].extend([{'role': 'user', 'content': text}, {'role': 'assistant', 'content': ''.join(chunks)}])
            c['history'] = c['history'][-24:]
        except (requests.RequestException, ValueError):
            yield '[Provider error or interrupted reply. Check key, model and quota.]'
        finally:
            c['busy'].release()
    return Response(generate(), content_type='text/plain; charset=utf-8')

@app.route('/task', methods=['POST'])
def task():
    data = request.get_json(silent=True) or {}
    goal = data.get('message')
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 12000:
        return Response('Send a task of 1–12000 characters.', status=400)
    c = client()
    if not c['busy'].acquire(False):
        return Response('This session is already running.', status=409)
    c['stop'].clear(); cfg = config_snapshot()
    def call(messages):
        with model_reply(messages, cfg) as res:
            return res.json()['choices'][0]['message']['content']
    def generate():
        try:
            if app.config['MOCK'] and not cfg[0]:
                yield json.dumps({'type': 'blocked', 'text': 'Task mode needs a real API key. Mock mode cannot perform tasks.', 'step': 0}) + '\n'
                return
            last = None
            for event in run_loop(goal, c['history'], c['workspace'], call, c['stop'].is_set):
                last = event
                yield json.dumps(event, ensure_ascii=False) + '\n'
            if last:
                c['history'].extend([{'role': 'user', 'content': goal}, {'role': 'assistant', 'content': last['type'] + ': ' + last['text']}])
                c['history'] = c['history'][-24:]
        finally:
            c['busy'].release()
    return Response(generate(), content_type='application/x-ndjson; charset=utf-8')

@app.route('/stop', methods=['POST'])
def stop():
    client()['stop'].set()
    return Response('Stop requested.', content_type='text/plain')

@app.route('/clear', methods=['POST'])
def clear():
    c = client()
    if not c['busy'].acquire(False):
        return Response('Stop the running task first.', status=409)
    try: c['history'] = []
    finally: c['busy'].release()
    return 'ok'

@app.route('/files', methods=['GET'])
def files():
    return Response(client()['workspace'].execute({'action': 'list_files'}), content_type='application/json')

@app.route('/files/<path:name>', methods=['GET'])
def download(name):
    try:
        path = client()['workspace'].path(name)
        if not path.is_file(): return Response('Not found.', status=404)
        return send_file(path, as_attachment=True)
    except ValueError:
        return Response('Invalid file path.', status=400)

@app.route('/set_key', methods=['POST'])
def set_key():
    data = request.get_json(silent=True) or {}
    key, url, model = (data.get(k, '') for k in ('key', 'url', 'model'))
    if not all(isinstance(v, str) for v in (key, url, model)):
        return Response('Invalid settings.', status=400)
    key, url, model = key.strip(), url.strip(), model.strip()
    gemini = key.startswith(('AIza', 'AQ.'))
    if gemini and not url:
        url = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    if url and url not in ALLOWED_API_URLS:
        return Response('Only configured, trusted provider endpoints are allowed.', status=400)
    with provider_lock:
        current_url = settings.get('url_override') or API_URL
        # Never forward a stored credential to a different endpoint.
        if url and url != current_url and not key:
            return Response('Changing provider requires a new key in the same save.', status=400)
        if key: settings['key_override'] = key
        if url: settings['url_override'] = url
        if model: settings['model_override'] = model
        elif gemini: settings['model_override'] = 'gemini-flash-latest'
    return Response('Saved in memory only. Gemini selected.' if gemini else 'Saved in memory only.', content_type='text/plain')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mock', action='store_true')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--port', type=int, default=5000)
    args = parser.parse_args()
    app.config.update(MOCK=args.mock, MODEL=args.model)
    if not PASSWORD: print('Local-only access. Set GROK_APP_PASSWORD before sharing a tunnel.')
    if SETTINGS_FILE.exists(): print('Legacy settings.json loaded. Remove it after moving your key to an environment variable; new settings are memory-only.')
    app.run(host='127.0.0.1', port=args.port, debug=False, threaded=True)

if __name__ == '__main__':
    main()
