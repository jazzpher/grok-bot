# grok-bot

A local, tool-using chat/coding agent with a web UI and a CLI, for any
OpenAI-compatible provider (Gemini, xAI, NVIDIA NIM, OpenRouter, Groq, Cerebras,
Mistral, DeepSeek, or a custom https endpoint). The name does not mean it runs
the Grok model: the provider and model you configure determine what answers.

**What the agent can do (0.9):** read, search and edit files in a per-session
workspace; run shell commands in a sandbox with a Codex-style policy
(`read-only` / `workspace-write` / `full-access` × `on-request` / `never`
approvals, persisted "always allow" rules); fetch pages and search the web;
**remember durable facts** (long-term memory, injected per turn, editable in the
UI); search its own earlier conversations; remember and reuse skills (export /
import as zip); **delegate** sub-tasks to isolated sub-agents; **schedule**
prompts ("every weekday at 09:00 check CI") that run unattended; call tools from
**MCP servers** (stdio or Streamable HTTP, `mcp.json`); accept **image
attachments** for vision models; and be driven from the web UI, the terminal,
`exec` in CI, or **Telegram** (allowlisted ids only). Plugins can add tools and
hooks without forking. Native tool calling is used when the provider supports
it, with an automatic JSON-protocol fallback when it does not. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/SECURITY.md](docs/SECURITY.md)
and the honest [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

```
python -m grokbot chat                          # terminal agent in the current directory
python -m grokbot exec "run the tests and fix the failure" --json   # CI / scripts
python webui.py                                 # web UI on http://127.0.0.1:5000
python -m grokbot telegram                      # Telegram gateway only (token + allowlist via env)
python -m grokbot exec --mock "hello"           # offline smoke test, no key needed
```

### Memory, automations, sub-agents, MCP, Telegram (0.9)

| Feature | How it works | Where |
|---|---|---|
| Long-term memory | `save_memory` / `search_memory` / `forget_memory` tools; pinned + relevant memories are added to the system prompt each turn; the *Context* row under each reply shows exactly what was injected | **Memory** panel, `/memory`, `/remember <fact>`, `GET/POST /memories` |
| Session search | `search_history` / `read_history` tools over the FTS5 conversation store | automatic |
| Sub-agents | `delegate(task, context, max_steps)` runs an isolated turn with its own context and a reduced tool set (no memory writes, no nested delegation beyond `GROK_MAX_SUBAGENT_DEPTH`); its tool calls stream as a nested card | automatic |
| Automations | `schedule_task` tool or the **Automations** panel: `every day at 08:00`, `every 2 hours`, `every monday at 9:00`, `in 30 minutes`, `tomorrow at 09:00`, or 5-field cron. Runs are *unattended*: approval policy forced to `never`, step budget `GROK_SCHEDULED_MAX_STEPS` (25); each run becomes a ⏰ conversation and a notification | `/schedules`, `GET/POST /schedules` |
| MCP client | `mcp.json` (or `GROK_MCP_CONFIG`) with `{"mcpServers": {"name": {"command", "args", "env"} or {"url", "headers"}}}`; tools appear as `mcp__<server>__<tool>`; `readOnlyHint`/`destructiveHint` annotations feed the approval policy | Settings → Integrations, `GET /mcp`, `POST /mcp/reload` |
| Telegram | `GROK_TELEGRAM_TOKEN` + `GROK_TELEGRAM_ALLOWED_IDS=123,456` (fails closed without the allowlist). Long polling, streamed replies, approval buttons, `/new /stop /policy /status /help`; owner `tg:<chat_id>` with its own workspace, memory and policy | `python webui.py` starts it, or `python -m grokbot telegram` |
| Plugins | `GROK_PLUGINS=my_module,other` — modules with `register(registry)`, `before_tool`, `after_tool`, `on_turn_end` hooks | — |
| Local models | `GROK_LOCAL_API_URL=http://127.0.0.1:11434/v1/chat/completions` (Ollama, LM Studio, vLLM, llama.cpp) adds a *Local model* provider; no key needed | Settings → Provider |
| Images | attach PNG/JPEG/WebP/GIF (drag-drop, paste or 🖼) — sent as `image_url` parts to vision-capable models | composer |
| UI | per-conversation URLs (`/c/<id>`), Markdown/JSON export, in-composer model switch, theme toggle, installable (PWA manifest), dictation via the Web Speech API when the browser has it | — |

Example `mcp.json` (copy `mcp.example.json`):

```json
{"mcpServers": {
  "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]},
  "remote":     {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer …"}}
}}
```

Docker is **optional**. Sandbox backends (`GROK_SANDBOX_BACKEND`, default `auto` = `docker` when the
docker CLI is installed, otherwise `subprocess`): `subprocess` — commands run as your user inside the
workspace with keys scrubbed from the environment; **not** an isolation boundary, so every command asks for approval — or `docker` — one
throwaway container per command, no network unless the policy allows it, workspace bind-mounted,
read-only root, command timeout enforced inside the container. `off` disables `run_command` entirely. `docker compose up` runs the whole app in a
container, which is the recommended way to let it work on untrusted code.

Configuration (environment): `GROK_API_KEY`, `GROK_API_URL`, `GROK_MODEL`,
`GROK_SANDBOX_BACKEND`, `GROK_SANDBOX_IMAGE`, `GROK_MAX_STEPS` (default 40),
`GROK_CONTEXT_TOKENS` (default 128000, auto-filled from the model list when
available), `GROK_SEARCH_BACKEND` (`auto`/`tavily`/`brave`/`searxng`/`duckduckgo`/`off`)
with `TAVILY_API_KEY` / `BRAVE_API_KEY` / `GROK_SEARXNG_URL`, `GROK_MAX_FILE_BYTES`
(10 MB), `GROK_MAX_WORKSPACE_BYTES` (500 MB), `GROK_MAX_FILES` (2000),
`GROK_MAX_SUBAGENT_DEPTH` (1), `GROK_SCHEDULER_TICK` (30 s), `GROK_SCHEDULER_MAX_JOBS`
(100), `GROK_SCHEDULED_MAX_STEPS` (25), `GROK_MCP_CONFIG`, `GROK_MCP_CONNECT_TIMEOUT`
(20 s), `GROK_MCP_CALL_TIMEOUT` (90 s), `GROK_TELEGRAM_TOKEN`, `GROK_TELEGRAM_ALLOWED_IDS`,
`GROK_PLUGINS`, `GROK_LOCAL_API_URL` / `GROK_LOCAL_MODEL`, `GROK_MEMORY_DB`,
`GROK_SCHEDULE_DB`. Project rules in `AGENTS.md` / `GROKBOT.md` / `CLAUDE.md` at the
workspace root are added to the system prompt.

## Run locally

```sh
pip install -r requirements.txt
export GROK_API_KEY='your-key'
export GROK_API_URL='https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
export GROK_MODEL='gemini-flash-latest'
python webui.py
```

Open http://127.0.0.1:5000. The composer talks to the unified agent (`POST /agent`):
replies stream as Markdown, tool calls appear as cards, and anything that crosses
the sandbox policy pauses for your approval. **Advanced → Run as durable background
task** still uses the original restart-safe harness with explicit artifact checks;
[HARNESS.md](HARNESS.md) documents checks, resume, API, budgets, persistence,
retention and deployment limits. Completion there requires explicit supported
artifact checks; a budget stop is never success.

Settings has a provider picker: choose a trusted OpenAI-compatible provider
(Google Gemini, NVIDIA NIM, OpenRouter, Groq, Cerebras, Mistral, DeepSeek, xAI
Grok), paste your key and, if needed, a model name. The endpoint comes from a
server-side list, not from the browser. Common key prefixes (AIza..., nvapi-...,
sk-or-..., gsk_...) select the matching provider automatically. Settings shows the
saved key masked (first 4 and last 3 characters, masked on the server so the full
key never returns to the browser) and a Test button that sends one tiny request
and reports OK, or a plain 401 (wrong key), 404 (no such model), 429 (quota) or
503 (provider busy) message. Test uses a small amount of your free quota.

Model discovery: Settings lists models on provider/key changes (debounced) or with
**Refresh models**. The picker never chooses the first result or replaces a typed
or current model. Choose explicitly, then Save. Manual model entry remains available
when listing fails. A listed model is **not** proof of chat support, free quota, or
account access; Test still sends one small chat request. Hints remain defaults when
no model has been entered, not verified availability claims.

Catalog requests use explicit provider endpoints, no redirects, bounded pagination
(same origin and catalog path only), size/time/model limits and a five-minute,
memory-only cache scoped to the endpoint/provider and a keyed credential digest.
Refresh bypasses the cache; saving settings clears it. Provider errors are sanitized.
NVIDIA hosted listing is best-effort and has not been verified with a real key.
Custom providers (including Token Harbor) require an **explicit models URL** on the
same origin as their chat URL. No catalog path is guessed; without one, type a model.
Custom DNS checks remain best-effort against hostile rebinding, as described below.

A Custom URL (advanced) choice exists for other OpenAI-compatible endpoints. It
only accepts public https addresses: http, userinfo, and private, loopback or
link-local addresses (literal or resolved) are rejected, because the key is POSTed
to that endpoint. The DNS check is best-effort hardening, not a guarantee against
a hostile hostname; prefer the listed providers.

Gemini can work with an eligible free-tier API key, subject to your account's
model availability and quota. Each step is a separate model request, so tasks
use more quota than ordinary chat. No paid services are added by this code.
Do not enable billing if you want to keep usage free. Provider errors do not
silently switch to other models or a mock answer.

## What tasks can actually do

### User-aligned drafts and honest verification

Put your goal in the message box and optionally use **Generation requirements**
for language, format, content, audience and tone, for example:
`Tagalog; warm invitation; numbered list; include December 1 and the venue; DOCX`.
These are generation instructions, not a deterministic guarantee of model quality.
They and the original goal, explicit checks and resume answers survive compaction
and restart. Missing essential details should produce a targeted question; ordinary
style preferences should not require a generic verification dead end.

DOCX creation checks that a candidate reopens and its extracted text equals the
tool input before atomic replacement. A failed build/check/replace preserves the
existing file. This checks file integrity, not whether the model wrote the right
invitation. With no explicit checks, a task that wrote only .docx files completes
once the harness reopens each file and finds text. The result says only that was
verified (language, content and style were not independently checked), and the
download stays available. Add a file_text check for exact-text verification.

`file_text` for DOCX now means the **entire extracted body text equals expected
text**, not substring matching (compatibility change). Paragraphs, including blank
ones and table-cell paragraphs, are joined with `\n`; `# ` heading markers used by
write_docx are removed. There is no whitespace/case normalization. Headers,
footers, visual layout and language detection are not checked. Text files still
use exact UTF-8 bytes; `exact_copy` still uses byte equality. No `contains` check
or semantic model verdict is introduced. Example:
`[{"type":"file_text","path":"invite.docx","text":"Paanyaya\nKumusta!"}]`.

`budgets.steps` caps valid action steps. Invalid replies do not use a step; they
have a separate cap of 5 per task, then the task is `blocked` (Resume works). The
persisted `attempts` counter is informational (provider calls made). Self-review is advisory within
this same budget, with no hidden extra requests. Live generated-answer quality,
native Windows and physical mobile/browser rendering were not tested in this upgrade.

- Calculate arithmetic with +, -, *, /, // and %.
- List, read and write small UTF-8 files in this session's workspace.
- Read back written files before saying the task is done.
- Ask you for missing information or report unsupported work.

Example: "Calculate 17 x 23, save the result in answer.txt, and read it back
to check it." Supply checks JSON `[{"type":"file_text","path":"answer.txt","text":"391"}]`.
Click Workspace files to download the result.

The section above describes the durable background harness. The interactive agent
can additionally run shell commands (under the sandbox policy), fetch pages and
search the web. It still cannot send messages, buy things, or reach files outside its
workspace unless you choose `full-access`. Browser automation (`browse_open`,
`browse_read`, and `browse_click`/`browse_type` with `GROK_BROWSER_INTERACT=on`) is
opt-in with `GROK_BROWSER_ENABLED=on` and currently runs in **durable background
tasks** only, not yet in the interactive composer. Completion of
background tasks verifies only the explicit supported byte checks, not every
possible semantic goal. Review generated content.

**UI preview** (`preview_page`) lets the interactive agent SEE the HTML it
writes: opt in with `GROK_PREVIEW=on` (separate from `GROK_BROWSER_ENABLED`;
needs Playwright + Chromium). The agent renders a workspace HTML file in
headless Chromium and gets back the screenshot as an image (with a
vision-capable model; set `GROK_PREVIEW_IMAGES=off` for text-only models to
keep just the layout numbers) plus measured layout facts: horizontal overflow,
top element boxes and a best-effort contrast pass. It renders workspace files
only — never URLs — from a loopback-only server; the page cannot reach the
internet, the LAN, other local ports or `file://`. Chromium starts on the
first preview of a turn and is killed when the turn ends; at most 3 previews
per turn. See `docs/skills/ui-preview-pass/SKILL.md` for the companion
workflow skill (import it from the Skills panel).

Files are limited per session (defaults: 2000 files, 10 MB per file, 500 MB
total; configurable with `GROK_MAX_*`). Paths cannot escape the workspace. Existing files in that workspace can be
replaced by a task. Download important results. New chat clears only your
conversation, not workspace files or task evidence. Last 24 chat entries and
task trajectories persist in SQLite. Set a stable GROK_SESSION_SECRET to keep
session access across restarts. Remove unused workspace folders manually when
the app is stopped. This demo is not a multi-user service.

Stop is cooperative: it prevents the next action after the current provider
call yields control; it does not hard-kill a thread. Chat-stream Stop is checked
between parsed SSE lines, so a stalled or unterminated line can delay cancellation
beyond one 45-second read-timeout interval; a slow-drip response has no hard
end-to-end deadline.
Tasks survive HTTP disconnects. Restart interrupts active tasks until explicit
Resume; counters/checkpoints are restored without blindly replaying writes.
Saved task selection restores paused questions and completed results on reload
and selector changes. Deterministic evidence summaries appear as `result` only after explicit
artifact checks pass; model self-certification is not published as verified proof. If checks were empty,
fill the checks JSON field before Resume; existing checks remain immutable.
The single-process database guard supports Unix and Windows locking. The test
suite runs on native Windows, but that does not certify production deployment.
Use Windows ACLs to protect the data folder.

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
use it as a public service. Settings only allow the listed provider endpoints,
a validated public custom https endpoint, or a loopback provider configured by
the server owner. Switching endpoints
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

## Learned skills (procedural memory)

Click **Skills** to see the saved workflows and read/edit their full Markdown.
Skills are text instructions, not new model weights, executable plugins or new
permissions. Inspired by Hermes' documented skills system:
https://hermes-agent.nousresearch.com/docs/user-guide/features/skills

- In **Task loop**, the agent gets a small skill index, then uses `list_skills`
  and `read_skill` to load a relevant workflow on demand.
- After a nontrivial task or a useful correction, it can use `save_skill` before
  finishing. It must first pass the task's explicit artifact checks and read
  back written files. Unverified/failed tasks cannot automatically teach skills.
  Selection is model-driven, not a guarantee that every task creates a skill.
- A save appears in the task's visible tool events. The full file stays available
  in the Skills panel. No background model review or extra provider request
  was added; skill actions use the normal bounded loop and consume its steps.
- You can manually create a workflow using **New skill**, a lowercase name,
  **When to use it**, Markdown instructions and **Save skill**. Select a skill
  to edit it; edits use revision checks so stale tabs cannot silently overwrite.
  Delete requires confirmation. Refresh lists recently learned skills.
- Chat mode sees only the index and has no skill tools. Use Task loop to load,
  learn and reuse automatically, or the Skills panel to create/edit directly.

Storage: `workspaces/.skills/<browser-session-id>/<skill-name>/SKILL.md` (under
`GROK_WORKSPACE_DIR` when configured). Agent-created skills carry explicit
provenance and are eligible for the zero-cost stale/archive lifecycle; manually
created and legacy skills are protected from automatic archival. Pin an agent
skill to exempt it from automatic status changes. New chat and server restarts
preserve skills for the same browser session. A different browser or cleared
cookies has a separate library. Existing server session-cookie lifetime still
applies. Skills are outside the task workspace, so ordinary file tools cannot
overwrite or download them. They are never committed to the public repository.
Back up this folder together with server data if you need durable storage elsewhere.

Example of the actual saved format (not seeded into your library):

```markdown
---
name: exact-copy
description: "Copy a text artifact without changing any characters"
---
## When to use
A task needs an exact text copy.
## Steps
1. Use copy_file with separate source and target paths.
2. Read the target back.
## Pitfalls
Do not regenerate the source using the model: characters may change.
## Verification
Use compare_files. Only MATCH means the bytes are identical.
```

Limits: 500 non-archived skills per session, 12 KB each, no supporting scripts. Writes are
atomic; path traversal/symlinks are rejected; common key/private-key patterns
are blocked. That scan is not a complete secret detector. Do not save passwords,
keys, uploaded documents, personal examples or raw chat logs. Skill contents
are untrusted advice and cannot grant authority or bypass tool constraints.
The automatic prompt index includes at most 30 visible skills and 6000 UTF-8
bytes total (not full Markdown), selected by goal word matches, then pinned
status, recent use and name. This is deterministic, with no extra model calls.
Other skills remain stored and loadable by name. The Task loop `list_skills`
tool returns up to 20 name/description/status entries per bounded page; use
`text` for an optional keyword search and `path` for an offset string ("0"
initially, then the returned `next_offset`). Search matches any whole word in
the name/description, not the full skill body. The Skills panel and HTTP list
still show all non-archived entries plus all archived entries. Archived entries
do not count
against the 500 limit but remain excluded from agent discovery until restored.
Restore is refused if all 500 slots are occupied. Archived files still use disk
space and have no separate count limit; delete unwanted archives manually.

No changes were made to reasoning visibility or the existing task log policy.

Test without paid calls: `python -m unittest discover -v`, `node test_ui.cjs`,
`node test_stop_ui.cjs`. `test_skills.py` covers persistence, isolation, edits,
limits, symlinks, failed writes, HTTP CRUD/CSRF, verified learning/reuse and
interrupted save recovery. Tests use scripted fake provider responses, not a
live Gemini call.

### Deterministic curator (no model calls)

Inspired by https://hermes-agent.nousresearch.com/docs/user-guide/features/curator .
The curator runs locally when the skill library is listed or used, not through
an always-on worker. `read_skill` in Task loop records a use. Merely looking at
Skills, listing the index or reading the editor does not count as use.

- Active: used/created/restored less than 14 days ago.
- Stale: 14 days without use. Still available to the agent.
- Archived: 30 days without use. Hidden from the agent's index; `read_skill`
  refuses it until restored. The Markdown file stays on disk, not deleted.
- Click Skills, select an `[archived]` workflow, then **Restore selected skill**.
  This resets the unused-day clock without rewriting its instructions.
- A small `metadata.json` beside SKILL.md stores creation time, last use, use
  count and archive state. The panel shows active/stale/archived status.
- Editing an archived skill does not automatically reactivate it. Restore it
  explicitly. Archived skills no longer count toward the 500-skill limit. Restoring one
  requires a free non-archived slot.

No model-based consolidation, rewrites or deletion is included. If the server
was off at the 30-day mark, archiving is applied on the next library access.

### Combined hardening verification

This branch combines learned skills/curator (#6) with workspace/provider
hardening (#7). Filtered, truncated or other unsupported terminal finish reasons
are shown as incomplete and are not saved in chat history or run as task actions.
Task bodies without a finish_reason remain compatible; chat still requires DONE.
Tests cover both original suites plus the skill/output-budget/upload boundary.
Native Windows and physical iPhone/Safari were not run by the combined-branch
author. Earlier Windows test claims refer to #7's author report, not this build.
