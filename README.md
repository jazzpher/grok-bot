# grok-bot

A lightweight local chat app with the same core experience as the Grok bot:
you chat, it remembers the conversation, replies stream in live.

It uses the official xAI (Grok) API, so replies come from Grok itself.

## Setup (one time)

1. Get an API key at https://console.x.ai (the API is a paid service).
2. Install the one dependency: `pip3 install requests`
3. Set your key: `export GROK_API_KEY="xai-..."`

## Run it

```
python3 bot.py
```

Test without a key (fake replies, good for checking the app works):

```
python3 bot.py --mock
```

Optional: pick a model with `python3 bot.py --model grok-4-fast` or by
setting `GROK_MODEL` in your environment.

## Commands

- `/new` clear memory and start a fresh conversation
- `/save` save this conversation to a text file
- `/help` list commands
- `/exit` quit
