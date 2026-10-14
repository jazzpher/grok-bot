# Architecture (grokbot 0.2)

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
- `workspaces/<sid>/` — files; `uploads/` is read-only for the agent; `.skills/` via `SkillStore`.

## HTTP surface added in 0.2
`POST /agent` (NDJSON events), `GET /agent/current`, `POST /agent/approve`, `GET /agent/pending`,
`GET|POST /policy`, `GET|POST /conversations`, `GET /conversations/current`, `GET /conversations/search`,
`GET /conversations/<id>`, `POST /conversations/<id>/{select,rename,pin,delete}`, `POST /compact`,
`POST /set_model`, `GET /capabilities`. `/models` now returns `details[model].context_length` when the provider
exposes it; `/set_key` accepts `context_length`; `/key_status` reports it.

## CLI (`grokbot/cli.py`)
`python -m grokbot chat|exec|serve`. `exec` is non-interactive (exit code 1 unless the turn completed; `--json`
prints a result object) for CI. `--mock` runs the whole loop offline.
