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

API_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("GROK_MODEL", "grok-4-fast")

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
</style>
</head>
<body>
<header>
  <h1>grok-bot</h1>
  <button onclick="clearChat()">New chat</button>
</header>
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


def stream_reply(user_text, api_key, model, mock):
    """Yield reply text chunks as they arrive, and remember the exchange."""
    state["history"].append({"role": "user", "content": user_text})

    if mock:
        reply = f"(mock) got your message: {user_text}"
        for word in reply.split(" "):
            yield word + " "
        state["history"].append({"role": "assistant", "content": reply})
        return

    payload = {"model": model, "messages": state["history"], "stream": True}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    chunks = []
    try:
        resp = requests.post(API_URL, headers=headers, json=payload, stream=True, timeout=180)
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
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
    except requests.RequestException as e:
        status = getattr(e.response, "status_code", None) if e.response is not None else None
        detail = f" (HTTP {status})" if status else ""
        yield f"[Connection error to the Grok API{detail}]"
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
        mimetype="text/plain",
    )


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
