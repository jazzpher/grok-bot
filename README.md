# grok-bot

A local chat app with the same core experience as the Grok bot: a web chat
interface, live-streaming replies, and conversation memory.

It uses the official xAI (Grok) API, so replies come from Grok itself.
Made for low-end devices: it is two small Python files, no heavy stack.

## Setup (one time)

1. Get an API key at https://console.x.ai (the API is a paid service).
2. Install dependencies: `pip3 install -r requirements.txt`
3. Set your key: `export GROK_API_KEY="xai-..."`

## Run it

Web interface (recommended):

```
python3 webui.py
```

Then open http://127.0.0.1:5000 in your browser. Use "New chat" to clear
the conversation memory. Pick a port with `--port 8080`.

Terminal version (no browser needed):

```
python3 bot.py
```

Test without a key (fake replies, good for checking things work):

```
python3 webui.py --mock
python3 bot.py --mock
```

Optional: choose a model with `--model grok-4-fast` or by setting
`GROK_MODEL` in your environment.

## Commands (terminal version)

- `/new` clear memory and start a fresh conversation
- `/save` save this conversation to a text file
- `/help` list commands
- `/exit` quit

## Using Gemini (free, AI Studio) instead of Grok

Gemini has an OpenAI-compatible endpoint, so you can plug it in without changing any code:

```bash
export GROK_API_KEY="your-gemini-api-key"
export GROK_API_URL="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
export GROK_MODEL="gemini-2.0-flash"
python webui.py
```

Then open http://127.0.0.1:5000. You can swap GROK_MODEL for any Gemini model (e.g. gemini-2.5-flash).
