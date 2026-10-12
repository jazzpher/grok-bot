# grok-bot

A small Python web chat with a bounded task agent. Use Gemini or xAI through
an OpenAI-compatible endpoint. The name does not mean it runs the Grok model:
the provider and model you configure determine what answers.

## Run locally

```sh
pip install -r requirements.txt
export GROK_API_KEY='your-key'
export GROK_API_URL='https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
export GROK_MODEL='gemini-flash-latest'
python webui.py
```

Open http://127.0.0.1:5000. Chat mode streams replies as before. Task loop mode
plans, uses a tool, sees its output and chooses the next step. A task stops on
completion, a missing-input question, provider failure, repeated action,
Stop, or the 12-step budget. It never labels a budget stop as success.

Gemini can work with an eligible free-tier API key, subject to your account's
model availability and quota. Each step is a separate model request, so tasks
use more quota than ordinary chat. No paid services are added by this code.
Do not enable billing if you want to keep usage free. Provider errors do not
silently switch to other models or a mock answer.

## What tasks can actually do

- Calculate arithmetic with +, -, *, /, // and %.
- List, read and write small UTF-8 files in this session's workspace.
- Read back written files before saying the task is done.
- Ask you for missing information or report unsupported work.

Example: "Calculate 17 x 23, save the result in answer.txt, and read it back
to check it." Click Workspace files to download the result.

This is NOT a browser/computer-control agent. It cannot run shell commands,
install packages, browse the web, send messages, buy things, or reach your
computer's files. A completion message is the model's assessment, not a
proof that every possible goal is met. Review generated content.

Files are limited to 25 per session, 32 KB per file and 256 KB total. Paths
cannot escape the workspace. Existing files in that workspace can be
replaced by a task. Download important results. New chat clears only your
conversation, not workspace files. Reloading clears the visible transcript
but retains server-side session memory. A restart resets conversations and
sessions; existing workspace folders stay on disk. Remove unused folders
manually when the app is stopped. This demo is not a multi-user service.

Stop prevents the next action but may wait up to the current provider
request timeout (45 seconds) before returning. Tasks run only while their
request remains connected; no background scheduler or resume checkpoints.

## Private access and keys

Local-only is the default. A public tunnel is rejected unless you set an app
password before starting:

```sh
export GROK_APP_PASSWORD='a-long-unique-password'
export GROK_SESSION_SECRET='a-long-random-session-secret'
# For an HTTPS tunnel, also set:
export GROK_SECURE_COOKIE=1
python webui.py
```

Do not commit secrets. Use environment variables or the authenticated Settings
form. Settings entered in the form are memory-only and disappear on restart.
Legacy settings.json is still loaded for compatibility: it contains plaintext
keys, so migrate the key into an environment variable, delete that file, and
rotate a previously exposed key. settings.json and workspaces are ignored by git.

Sessions have separate chat histories/workspaces. The provider settings are
still shared for this single-owner app: do not distribute the password or
use it as a public service. Settings only allow xAI and Gemini endpoints (or a
loopback provider configured by the server owner). Switching endpoints
requires supplying a new key, preventing forwarding the previous saved key.
The old unauthenticated /debug endpoint is removed. Login has a small failed
attempt limit; this is basic protection, not a full production auth system.

xAI setup: use https://api.x.ai/v1/chat/completions and your selected supported
Grok model. xAI usage may cost money. The terminal bot.py is unchanged; its
/new, /save, /help and /exit commands still work.

## Tests

```sh
python -m unittest -v test_agent
python -m py_compile webui.py agent_loop.py bot.py
```

The tests use scripted provider responses, not live credentials. They check
loop completion, read-back checks, failures, budgets, Stop, file boundaries,
calculator restrictions, HTTP endpoints, auth and session isolation. For a
real Gemini smoke test, run the example task above with your own configured
free-tier key. --mock remains available for testing Chat without a key;
Task mode reports that it needs a real key rather than faking completion.
