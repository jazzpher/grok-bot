# Architecture (grokbot 0.9)

```
browser ──POST /agent (NDJSON)──▶ webui.py ──▶ grokbot.agent.run_turn ──▶ grokbot.providers.stream_chat ──▶ provider
   ▲                                 │                 │
   │ approvals, policy, conversations│                 ├─▶ grokbot.tools.ToolRegistry.execute ──▶ files / shell / web / skills
   └──────── GET /agent/current ─────┘                 │          │                         └─ grokbot.sandbox backend (subprocess | docker)
                                                       │          └─ ApprovalBroker (policy → allow / ask / deny)
                                                       └─▶ grokbot.compaction (summarise when context is ~75% full)
```

## One turn (`grokbot/agent.py`)
1. Build the system prompt: identity + environment block (sandbox mode, approval policy, backend, cwd, model) +
   project instructions (`AGENTS.md`/`GROKBOT.md`/`CLAUDE.md` in the workspace) + skills index + (JSON mode only)
   the tool protocol.
2. Compact the history if `estimate_tokens(messages) > COMPACT_RATIO × context_tokens` — model summary first,
   truncation fallback, never splitting a tool result from its call.
3. Stream the completion with native `tools=[...]`. Events: `text`, `reasoning`, `tool_calls`, `usage`, `finish`.
   If the provider answers HTTP 400 mentioning tools → `ToolsUnsupported` → switch this session to the JSON protocol
   (`{"tool": name, "arguments": {...}}`; text is buffered so tool JSON never flashes on screen).
4. Execute tool calls: read-only tools in parallel (bounded pool), mutating tools serially, each wrapped in
   `ToolResult` with bounded output. Results are appended as `role: tool` messages prefixed
   `UNTRUSTED TOOL RESULT (name):`.
5. Repeat until the model stops calling tools, the step budget is hit (`max_steps`), the user stops, or a
   non-transient provider error occurs. Transient errors retry with backoff (`retry` events).

`TurnResult` carries status, text, the updated message list, counts, aggregated usage and the running summary.

## Tools (`grokbot/tools/`)
Each tool is a `ToolSpec` (name, description, JSON-schema parameters, handler, `mutates`, `needs_approval`,
`available`). `default_registry()` registers: files (`read_file`, `write_file`, `edit_file`, `list_files`, `glob`,
`grep`, `copy_file`, `compare_files`, `write_docx`, `delete_file`), misc (`calculate`, `current_time`), skills
(`list_skills`, `read_skill`, `save_skill`), shell (`run_command`), web (`fetch_url`, `web_search`). File tools reuse
`agent_loop.Workspace` for path/size/count guards, so the old protections stay in force.

## Sandbox & approvals (`sandbox.py`, `approvals.py`)
- Policy = `mode` (`read-only` | `workspace-write` | `full-access`) × `approval` (`on-request` | `never`) × `network`
  × saved `allow` rules. Stored per browser session in `workspaces/.policy/<sid>.json`.
- `run_command` runs through a backend: `subprocess` (cwd = workspace, env scrubbed of keys, timeout, output caps —
  **not** an isolation boundary) or `docker` (`--network none` unless allowed, workspace bind-mounted, read-only
  root, no capabilities). Heuristics decide what needs approval: destructive patterns always ask; in read-only
  mode anything that is not an inspection command asks; network/escape detection applies when the backend is not
  isolated. `never` + a request that would ask → denied with a clear tool error. `full-access` + `never` = run
  everything without prompts.
- Approvals block the worker thread on an `Event`; the browser answers through `POST /agent/approve`
  (`allow` / `deny` / `always` → persisted allow rule). Timeouts and Stop resolve to deny.

## Persistence
- `workspaces/.conversations.sqlite3` — `ConversationStore`: per-session conversations, display entries (with tool
  events), the model-facing context (compacted messages + running summary), FTS5 search (LIKE fallback).
- `workspaces/.tasks.sqlite3` — the durable background `TaskManager` (unchanged legacy harness).
- `workspaces/<sid>/` — files (`<sid>` is `owner` for every login in password mode, a random per-browser id in local mode); `uploads/` is read-only for the agent; `.skills/` via `SkillStore`.

## HTTP surface added in 0.2
`POST /agent` (NDJSON events), `GET /agent/current`, `POST /agent/approve`, `GET /agent/pending`,
`GET|POST /policy`, `GET|POST /conversations`, `GET /conversations/current`, `GET /conversations/search`,
`GET /conversations/<id>`, `POST /conversations/<id>/{select,rename,pin,delete}`, `POST /compact`,
`POST /set_model`, `GET /capabilities`. `/models` now returns `details[model].context_length` when the provider
exposes it; `/set_key` accepts `context_length`; `/key_status` reports it.

## CLI (`grokbot/cli.py`)
`python -m grokbot chat|exec|serve`. `exec` is non-interactive (exit code 1 unless the turn completed; `--json`
prints a result object) for CI. `--mock` runs the whole loop offline.

## Added in 0.9 (phases 3–5)

```
webui.run_owner_turn(owner, workspace, cid, text, emit=…, stop=…, images=…, unattended=…)
        ▲                 ▲                    ▲
   POST /agent      Scheduler.run_job     TelegramGateway (owner tg:<chat>)
```

- **`grokbot/memory.py`** — `MemoryStore` (SQLite + FTS5, owner-scoped). `relevant(owner, goal)` = pinned first, then
  best keyword matches, bounded; `agent.build_system_prompt` appends `prompt_block(...)` when the host attaches the
  store in `ctx.extras['memory']`. Tools in `grokbot/tools/memory.py` (`save_memory`, `search_memory`, `forget_memory`,
  `search_history`, `read_history`). The `context_info` event tells the UI what was injected (memories, skill index,
  project file, uploads, MCP servers, tool count).
- **`grokbot/tools/delegate.py`** — sub-agents: a nested `run_turn` with a `registry.subset(...)` (no memory writes,
  no further delegation past `MAX_SUBAGENT_DEPTH`), fresh context, same workspace/policy/approvals. Nested events carry
  `agent`/`depth`; the nested `done` is renamed `subagent_done`; `subagent_start`/`subagent_end` wrap them.
- **`grokbot/scheduler.py`** — `parse_schedule()` (cron / phrases) → spec `{kind: cron|interval|once}`; `Scheduler`
  stores jobs in SQLite, a tick thread runs due jobs through the host `runner(job)`; `once` jobs disable themselves.
  The host runner (`webui.run_scheduled_job`) creates a ⏰ conversation, runs an unattended turn
  (`approval=never`, `SCHEDULED_MAX_STEPS`) and queues a notification (`GET /notifications`) and, if the job was
  created from Telegram, sends the result there. Tools: `schedule_task`, `list_scheduled_tasks`, `cancel_scheduled_task`.
- **`grokbot/mcp.py`** — JSON-RPC client for stdio (`StdioClient`, reader thread, id-matched replies) and Streamable
  HTTP (`HttpClient`, JSON or SSE responses, `Mcp-Session-Id`). `MCPManager.register_tools(registry)` turns every
  remote tool into a `ToolSpec` named `mcp__<server>__<tool>` (sanitised, ≤64 chars) with the server's input schema;
  `mutates = not readOnlyHint`, approval required in read-only mode or when `destructiveHint` is set; failures
  disconnect so the next call reconnects.
- **`grokbot/plugins.py`** — `GROK_PLUGINS` modules may `register(registry)` tools and hook `before_tool` /
  `after_tool` / `on_turn_end` / `on_message`; `ToolRegistry.execute` and `run_turn` call them; a failing hook is
  logged and skipped.
- **`grokbot/gateway/telegram.py`** — `TelegramGateway(token, allowed_ids, runner, approvals, policy_store)`:
  `getUpdates` long polling, strict allowlist, draft message edited every ~2 s while streaming, approvals as inline
  buttons (`ap|<id>|allow|always|deny` → `ApprovalBroker.resolve`), commands `/new /stop /policy /status /help`.
  `TelegramRunner` in `webui.py` maps a chat to its current conversation and calls `run_owner_turn`.
- **Web routes** — `/memories…`, `/schedules…`, `/notifications`, `/mcp`, `/mcp/reload`, `/skills/export`,
  `/skills/import`, `/conversations/<id>/export?format=md|json`, `/c/<id>` (deep link), `/capabilities` (now
  includes memory/schedule counts, MCP status, plugins, telegram, local provider). `/agent` accepts
  `images: [{name, data_url}]` (≤4, PNG/JPEG/WebP/GIF, ~2 MB each) which become `image_url` parts.
- **UI** — Memory and Automations panels, Context row, sub-agent cards, attachments (drag-drop/paste), quick model
  switch in the top bar, export button, theme toggle, PWA manifest, Web Speech dictation, notifications toast.
