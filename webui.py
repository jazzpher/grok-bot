#!/usr/bin/env python3
"""Grok-style local chat bot with a web interface.

Runs a small local web app (Flask). Open http://127.0.0.1:5000 in your
browser and chat: replies stream in live, and the server remembers the
conversation until you clear it.

Usage:
    export GROK_API_KEY="xai-..."     # get one at https://console.x.ai
    python3 webui.py                  # open http://127.0.0.1:5000
    python3 webui.py --mock           # no key needed, fake replies
    python3 webui.py --port 8080      # different port
"""

import argparse
import json
import os

try:
    import requests
    from flask import Flask, Response, request, render_template_string
except ImportError:
    import sys
    sys.exit("Missing dependencies. Run: pip3 install requests flask")

API_URL = os.environ.get("GROK_API_URL", "https://api.x.ai/v1/chat/completions")
DEFAULT_MODEL = os.environ.get("GROK_MODEL", "grok-4-fast")

# Tried in order when a model is rejected (e.g. retired by the provider).
FALLBACK_MODELS = ["gemini-flash-latest", "gemini-3.8-flash", "gemini-3.5-flash-lite"]

SYSTEM_PROMPT = (
    "You are Grok, a helpful assistant with a bit of wit. "
    "Answer directly and honestly."
)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>grok-bot</title>
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
<div id="chat"><div class="hint">Chat anything. Replies come from Grok.</div></div>
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
  add('you', text);
  inp.value = '';
  sendBtn.disabled = true;
  const bubble = add('grok', '');
  let full = '';
  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text})
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      full += dec.decode(value, {stream: true});
      bubble.textContent = full;
      chat.scrollTop = chat.scrollHeight;
    }
    if (!full) bubble.textContent = '[Empty reply]';
  } catch (e) {
    bubble.textContent = '[Error: ' + e + ']';
  }
  sendBtn.disabled = false;
  inp.focus();
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
  keyin.value = '';
}

async function clearChat() {
  await fetch('/clear', {method: 'POST'});
  chat.innerHTML = '<div class="hint">New chat. Memory cleared.</div>';
}

inp.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
</script>
</body>
</html>"""

app = Flask(__name__)
state = {"history": [{"role": "system", "content": SYSTEM_PROMPT}]}

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


def save_settings():
    data = {k: state[k] for k in ("key_override", "url_override", "model_override") if k in state}
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f)
        os.chmod(SETTINGS_FILE, 0o600)
    except OSError:
        pass


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        for k in ("key_override", "url_override", "model_override"):
            if data.get(k):
                state[k] = data[k]
    except (OSError, ValueError):
        pass


load_settings()


def stream_reply(user_text, api_key, model, mock):
    """Yield reply text chunks as they arrive, and remember the exchange."""
    state["history"].append({"role": "user", "content": user_text})

    if mock and not state.get("key_override"):
        reply = f"(mock) got your message: {user_text}"
        for word in reply.split(" "):
            yield word + " "
        state["history"].append({"role": "assistant", "content": reply})
        return

    api_key = state.get("key_override") or api_key
    model = state.get("model_override") or model
    api_url = state.get("url_override") or API_URL
    candidates = [model] + [m for m in FALLBACK_MODELS if m != model]
    chunks = []
    resp = None
    last_status = None
    for candidate in candidates:
        payload = {"model": candidate, "messages": state["history"], "stream": True}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            resp = requests.post(api_url, headers=headers, json=payload, stream=True, timeout=180)
            resp.raise_for_status()
            model = candidate
            break
        except requests.RequestException as e:
            last_status = getattr(e.response, "status_code", None) if e.response is not None else None
            if hasattr(resp, "close"):
                resp.close()
            resp = None
    if resp is None:
        detail = f" (HTTP {last_status})" if last_status else ""
        yield f"[Connection error to the AI API{detail}]"
        return
    try:
        for raw_line in resp.iter_lines():
            line = raw_line.decode("utf-8", errors="ignore") if isinstance(raw_line, bytes) else raw_line
            if not line or not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data.strip() == "[DONE]":
                break
            try:
                piece = json.loads(data)
                text = piece["choices"][0].get("delta", {}).get("content", "")
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            if text:
                chunks.append(text)
                yield text
    except requests.RequestException:
        pass
    finally:
        if hasattr(resp, "close"):
            resp.close()

    state["history"].append({"role": "assistant", "content": "".join(chunks)})


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/chat", methods=["POST"])
def chat():
    text = (request.json or {}).get("message", "").strip()
    if not text:
        return Response("Say something first.", mimetype="text/plain")
    return Response(
        stream_reply(text, app.config["API_KEY"], app.config["MODEL"], app.config["MOCK"]),
        mimetype="text/plain; charset=utf-8",
    )


@app.route("/set_key", methods=["POST"])
def set_key():
    d = request.json or {}
    key = (d.get("key") or "").strip()
    url = (d.get("url") or "").strip()
    model = (d.get("model") or "").strip()
    import re
    m = re.search(r"AIza[0-9A-Za-z_\-]{30,}", key)
    if m:
        key = m.group(0)
    if key:
        state["key_override"] = key
    is_gem = key.startswith("AIza") or key.startswith("AQ.")
    if url:
        state["url_override"] = url
    elif key and is_gem:
        state["url_override"] = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    if model:
        state["model_override"] = model
    elif key and is_gem and not state.get("model_override"):
        state["model_override"] = "gemini-flash-latest"
    if key:
        if is_gem:
            msg = "saved: Gemini key detected, endpoint and model auto-set"
        else:
            msg = "saved key, but no API URL set. Fill the API URL field for non-Gemini keys."
    elif url or model:
        msg = "saved endpoint/model settings"
    else:
        msg = "no key found in the field. Paste the full key (starts with AIza) then Save."
    save_settings()
    return Response(msg, mimetype="text/plain")


@app.route("/debug", methods=["GET"])
def debug_state():
    key = state.get("key_override", "")
    return Response(json.dumps({
        "key_set": bool(key),
        "key_prefix": key[:4] + "..." if key else "",
        "url_override": state.get("url_override", ""),
        "model_override": state.get("model_override", ""),
        "default_url": API_URL,
        "default_model": app.config.get("MODEL", ""),
    }), mimetype="application/json")


@app.route("/clear", methods=["POST"])
def clear():
    state["history"] = [{"role": "system", "content": SYSTEM_PROMPT}]
    return Response("ok", mimetype="text/plain")


def main():
    parser = argparse.ArgumentParser(description="Grok-style local chat bot with a web UI")
    parser.add_argument("--mock", action="store_true", help="run without an API key (fake replies)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Grok model (default: {DEFAULT_MODEL})")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    api_key = os.environ.get("GROK_API_KEY", "")
    if not args.mock and not api_key:
        import sys
        sys.exit(
            "No API key found. Get one at https://console.x.ai then:\n"
            '  export GROK_API_KEY="xai-..."\n'
            "Or test the app without a key: python3 webui.py --mock"
        )

    app.config.update(API_KEY=api_key, MODEL=args.model, MOCK=args.mock)
    print(f"grok-bot web UI ready on http://127.0.0.1:{args.port} "
          f"({'mock mode' if args.mock else 'model: ' + args.model})")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
