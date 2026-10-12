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
plans, uses a tool, sees its output and chooses the next step in a durable
background worker. [HARNESS.md](HARNESS.md) documents checks, resume, API,
budgets, persistence, retention and deployment limits. Completion requires
explicit supported artifact checks; a budget stop is never success.

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
to check it." Supply checks JSON `[{"type":"file_text","path":"answer.txt","text":"391"}]`.
Click Workspace files to download the result.

This is NOT a browser/computer-control agent. It cannot run shell commands,
install packages, browse the web, send messages, buy things, or reach your
computer's files. Completion verifies only the explicit supported byte checks,
not every possible semantic goal. Review generated content.

Files are limited to 25 per session, 32 KB per file and 256 KB total. Paths
cannot escape the workspace. Existing files in that workspace can be
replaced by a task. Download important results. New chat clears only your
conversation, not workspace files or task evidence. Last 24 chat entries and
task trajectories persist in SQLite. Set a stable GROK_SESSION_SECRET to keep
session access across restarts. Remove unused workspace folders manually when
the app is stopped. This demo is not a multi-user service.

Stop prevents the next action but may wait up to the current provider
request timeout before returning; it is cooperative, not a hard thread kill.
Tasks survive HTTP disconnects. Restart interrupts active tasks until explicit
Resume; counters/checkpoints are restored without blindly replaying writes.

## Private access and keys

Local-only is the default: passwordless access requires a loopback socket peer,
a loopback Host, and no Forwarded/X-Forwarded-* headers. Host and forwarded
headers are never proof of authentication. For **every** tunnel, reverse proxy
(including one that connects locally or strips headers), or cloud deployment,
explicitly select exposed mode and set an app password before starting:

```sh
export GROK_DEPLOYMENT_MODE=exposed
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
Grok model. xAI usage may cost money. The terminal bot.py keeps /new, /save, /help and /exit. Interrupted
streams return an InterruptedReply status to callers; the CLI stays interactive.
Partial text may already be visible, but neither the failed user turn nor partial
assistant output is saved as conversation history. Successful asks still return
strings. A stream ending without [DONE] is treated as interrupted.

## Tests

```sh
python -m unittest discover -v
python -m py_compile webui.py agent_loop.py bot.py
```

The tests use scripted provider responses, not live credentials. They check
loop completion, read-back checks, failures, budgets, Stop, file boundaries,
calculator restrictions, HTTP endpoints, auth and session isolation. For a
real Gemini smoke test, run the example task above with your own configured
free-tier key. --mock remains available for testing Chat without a key;
Task mode reports that it needs a real key rather than faking completion.

## Uploads and visible history (next slice)

Upload UTF-8 text/code, CSV/JSON, text PDFs or DOCX (1 MB, 20 PDF pages,
32 KB extracted text). Images, scanned PDFs/OCR, encrypted PDFs and larger
files are not supported. Originals are not kept: extracted text goes into a
read-only uploads/ workspace path. Repeated filenames get a suffix instead
of overwriting. Tasks can read the full extracted file; ordinary Chat receives
up to 12000 characters of document excerpts and must disclose incomplete context.

Only upload documents you want sent to the configured AI provider. Uploaded
content is untrusted data, not permission to run instructions embedded in it.
The app has no external-action tools. PDF/DOCX parsing is bounded but not a
hardened document sandbox; use trusted files and keep dependencies updated.

Reload now restores the last 24 conversation entries from this session.
Server restart retains SQLite history and tasks; a stable session secret is
required to retain access. Files are never reassigned to new sessions. No
cross-device history/account recovery. Metadata retention defaults to 30 days.

Exact copies use copy_file and compare_files to preserve and compare bytes,
including CRLF and lone CR. The deterministic completion guard recognizes the
narrow format `Copy SOURCE exactly to TARGET`, where paths have no spaces;
In durable Task mode SOURCE must be a literal workspace path.
TARGET must be separate and outside uploads/. Such a task cannot finish unless
that specific target exists and its bounded bytes match the source, even if no
write happened in this request. Ambiguous exact-copy wording asks for explicit
paths instead of guessing. This is not general semantic goal verification:
other transformations require explicit supported checks, or need clarification. Repeated
successful reads are allowed; consecutive identical failed tool arguments stop
after two failures, and all requests retain the model step budget.

Structured replies require every argument field (empty strings for unused fields).
Action-specific server validation rejects missing, blank or wrong-type arguments
before a tool executes, and supplies the exact correction within the step budget.
Empty write_file content remains valid. Live provider behavior still needs testing.
