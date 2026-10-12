#!/usr/bin/env python3
"""Grok-style local chat bot.

A lightweight terminal chat app that talks to the xAI (Grok) API,
with conversation memory, streaming replies, and session commands.

Usage:
    export GROK_API_KEY="xai-..."     # get one at https://console.x.ai
    python3 bot.py                    # chat with Grok
    python3 bot.py --mock             # no key needed, fake replies for testing

Commands inside the chat:
    /new    start a fresh conversation (clears memory)
    /save   save this conversation to a file
    /help   show commands
    /exit   quit
"""

import argparse
import json
import os
import sys
import datetime

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run: pip3 install requests")

API_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("GROK_MODEL", "grok-4-fast")

SYSTEM_PROMPT = (
    "You are Grok, a helpful assistant with a bit of wit. "
    "Answer directly and honestly."
)


class Chat:
    def __init__(self, api_key, model, mock=False):
        self.api_key = api_key
        self.model = model
        self.mock = mock
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]

    def ask(self, user_text):
        """Send one message, stream the reply, and remember it."""
        self.history.append({"role": "user", "content": user_text})

        if self.mock:
            reply = self._mock_reply(user_text)
        else:
            reply = self._stream_reply()

        self.history.append({"role": "assistant", "content": reply})
        return reply

    def _stream_reply(self):
        payload = {
            "model": self.model,
            "messages": self.history,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        chunks = []
        try:
            resp = requests.post(API_URL, headers=headers, json=payload, stream=True, timeout=120)
            resp.raise_for_status()
        except requests.HTTPError as e:
            body = ""
            try:
                body = e.response.text[:300]
            except Exception:
                pass
            return f"[API error {e.response.status_code}] {body}"
        except requests.RequestException as e:
            return f"[Connection error] {e}"

        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data.strip() == "[DONE]":
                    break
                try:
                    piece = json.loads(data)
                    delta = piece["choices"][0].get("delta", {})
                    text = delta.get("content", "")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if text:
                    chunks.append(text)
                    print(text, end="", flush=True)
            print()
        finally:
            resp.close()

        return "".join(chunks) or "[Empty reply from the API]"

    def _mock_reply(self, user_text):
        print(f"(mock) you said: {user_text}")
        return f"(mock) got your message: {user_text}"

    def new_conversation(self):
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]

    def save(self, path=None):
        path = path or f"grok-chat-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
        with open(path, "w", encoding="utf-8") as f:
            for msg in self.history:
                role = msg["role"]
                if role == "system":
                    continue
                f.write(f"{'You' if role == 'user' else 'Grok'}: {msg['content']}\n\n")
        return path


def main():
    parser = argparse.ArgumentParser(description="Local Grok-style chat bot")
    parser.add_argument("--mock", action="store_true", help="run without an API key (fake replies)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Grok model (default: {DEFAULT_MODEL})")
    args = parser.parse_args()

    api_key = os.environ.get("GROK_API_KEY", "")
    if not args.mock and not api_key:
        sys.exit(
            "No API key found. Get one at https://console.x.ai then:\n"
            '  export GROK_API_KEY="xai-..."\n'
            "Or test the app without a key: python3 bot.py --mock"
        )

    chat = Chat(api_key, args.model, mock=args.mock)
    print(f"grok-bot ready ({'mock mode' if args.mock else 'model: ' + args.model}). "
          "Type /help for commands.\n")

    while True:
        try:
            user_text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye!")
            break

        if not user_text:
            continue

        if user_text.lower() in ("/exit", "/quit", "exit"):
            print("bye!")
            break
        if user_text.lower() == "/new":
            chat.new_conversation()
            print("* new conversation, memory cleared\n")
            continue
        if user_text.lower() == "/help":
            print("/new  /save  /exit  (chat anything else)\n")
            continue
        if user_text.lower() == "/save":
            path = chat.save()
            print(f"* saved to {path}\n")
            continue

        print("grok> ", end="", flush=True)
        chat.ask(user_text)
        print()


if __name__ == "__main__":
    main()
