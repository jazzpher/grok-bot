# Durable bounded harness v1

This change is intentionally smaller than Claude Code/Hermes. It adds durable
orchestration around the existing calculator and session workspace tools, not
shell execution, browsing, messaging, installs or production code execution.

## Usage

Task mode now returns immediately with a task ID. Closing the tab or HTTP
connection does **not** stop the worker. The page polls durable events, restores
saved tasks on reload, and offers Stop and Resume selected task. Put a reply to a
missing-input question in the message box before Resume. Resume keeps the same
trajectory and cumulative counters; it does not grant a fresh budget.

For `Copy uploads/notes.txt.txt exactly to output.txt`, deterministic exact-copy
checks are inferred from that narrow grammar. Use the literal workspace source
path (no filename aliases or spaces). For other tasks, enter explicit checks in
the checks JSON field, for example:

```json
[{"type":"file_text","path":"answer.txt","text":"391"}]
```

Four deterministic check types are supported:

- `file_text` (`path`, `text`): exact UTF-8 bytes for text files; exact extracted text for DOCX.
- `exact_copy` (`source_path`, `path`): bounded byte equality.
- `pixel_diff` (`path`, `source_path`, optional `max_diff` default 0.01, `tolerance` 0-255,
  `ignore` up to 8 `[x,y,w,h]` rectangles, `viewport`, `note`): the output is pixel-compared
  against a ground-truth image with Pillow and passes when the mismatch ratio is at most
  `max_diff`. An `.html` side is rendered headlessly first (needs `GROK_PREVIEW=on` and
  Playwright). The highlighted diff is saved next to the output as `<name>.diff.png`.
- `command` (`command`, optional `expect_exit` default 0, `expect_output` substring,
  `timeout` 5-120s default 60, `note`): runs the owner-supplied command in the workspace on
  the configured sandbox backend with network disabled; passes on the expected exit code and
  substring. Hardline-blocked commands are refused at submit, the same as run_command.

A `note` on any check documents a known-irrelevant difference (an exemption, e.g. font
rendering); it is echoed in verification messages and never lowers a threshold by itself.
Unsupported check types return 400. Without
an explicit check a proposed completion becomes `needs_input`, not a claim of
success. If the original checks are empty, enter nonempty checks JSON and click
Resume to establish the contract once. Checks are validated exactly as at admission
and persisted with the resumed checkpoint; existing nonempty checks cannot change
(use a new task). Resume never resets budgets. Read-back is required for
files written by this task, but it is **not** general semantic verification.
Existing artifacts may satisfy a check without a new write. Checks verify present
content, not authorship, quality, correctness of arbitrary calculations, or an
immutable historical source. Download and independently review important results.

## API

- `POST /task` JSON: `message`, optional `requirements` text (8 KB), optional `checks`, optional lower `budgets`, optional `result_schema` (list of at most 16 required result field names; the task cannot complete until the final `done` action carries a `result` object with every field, with nonempty text values; strict providers receive the exact requested field names, other provider responses are matched case- and punctuation-insensitively; names must contain an ASCII letter or digit; schema-rejected completions are refunded, not counted as steps, up to 3 retries); 202
  returns `id`, `status_url`. This replaces the old request-bound NDJSON protocol.
- `GET /tasks`: most recent 50 tasks for this session.
- `GET /tasks/ID`: status and cumulative counters.
- `GET /tasks/ID/events?after=SEQ`: ordered batch of up to 200 events, global
  monotonic cursor. Continue until fewer than 200; do not assume one batch is all.
- `POST /tasks/ID/stop`: cooperative cancellation.
- `POST /tasks/ID/resume` JSON `{}` or `{"answer":"..."}`: resumes only stopped,
  interrupted, needs_input or blocked tasks (the UI shows a one-click "Resume task"
  button on blocked/stopped/interrupted tasks). Completed/budget_exhausted are terminal.
- `/stop` remains a compatibility stop-all-active-in-session endpoint.

Ownership comes solely from the signed session cookie, never a submitted owner
field. Unknown and other-owner IDs return 404. Chat/clear/upload/task start are
serialized per session; tasks also reserve the session in SQLite. Four worker
threads maximum globally. Provider configuration remains shared and memory-only.
A resumed task takes a fresh provider configuration snapshot, not an old key.

## Persistence and restart

`GROK_TASK_DB` defaults to `workspaces/.tasks.sqlite3`; `GROK_WORKSPACE_DIR`
selects the workspace root. SQLite commits checkpoint state and event in one
transaction. The retained trajectory includes original goal, action arguments,
tool evidence, written/checked sets, pending action, step/output/elapsed counters,
retry/failure counters and the acceptance contract. Keys/passwords/config tuples
and raw provider exception messages are **not** stored. User prompts, uploads,
tool contents and model text **are** persisted: do not include secrets there.
This is not automatic secret redaction or encrypted storage.

Set a stable secret via `GROK_SESSION_SECRET` before the first run to keep signed
cookie ownership across process restarts. With the random default secret, old
sessions become inaccessible after restart; files are not reassigned. Cookie loss
or clearing cookies also loses access. No account recovery or cross-device sync.
Last 24 chat entries persist; uploads are reconstructed from read-only extracted
files. Task trajectories live separately from visible chat history. New chat clears
chat memory only, never task evidence/files. In-memory client eviction does not
reassign another session's files.

At restart queued/running tasks become `interrupted` and do **not** silently run
or incur provider cost. Explicit Resume is required. A pending write is never
blindly re-executed: the expected write bytes or pre-action copy SHA256 are
compared to the bounded target. Matching artifacts are reconciled and must be
read again. Missing/different targets block with instructions to inspect and
start a new task. Pending read/calculator operations are not replayed; context
states the interrupted boundary and requires rechecking. This conservative policy
may block an action which never started, rather than risk a duplicate write.
Filesystem access assumes no hostile local process changing workspace paths.

Metadata older than 30 days is pruned at CLI server startup (never active tasks),
including associated events and inactive session histories. Workspace folders are
**not** deleted automatically. Operators should stop the server, back up needed
artifacts, then explicitly remove stale folders. Metadata persistence is not an
unlimited archival guarantee. The direct manager API exposes explicit `prune()`.

## Bounds and limitations

Defaults/hard upper bounds: 12 action steps, 300 seconds cumulative active
elapsed time, 128000 cumulative UTF-8 bytes of model/tool output, 24000 serialized
context characters. Client budgets may only lower these. Minimum context is 4000.
Provider responses are streamed with a body cap of six times the remaining output
budget plus 64 KiB for the JSON envelope; parsed model content must fit the
remaining byte budget. Oversized file reads are skipped, and tool output is
discarded rather than allowed to push the cumulative counter over its limit.
Chat replies are capped at 128000 UTF-8 bytes, with the provider stream body
capped at six times that limit plus 64 KiB; malformed SSE events are ignored, and
incomplete or oversized replies are not saved to history.
Transient connection/timeouts and HTTP 500/502/503/504 retry at most three
consecutive times (the UI shows "retrying 2 of 3") with cancellable bounded backoff;
each attempt consumes the attempt budget. After the third retry the task is `blocked` with a plain
message and can be resumed. 400/401/403/404/429 never retry. No fallback model, billing changes or quota bypass.
Malformed actions do not use a step; they stop after 5 per task. Repeated same failed required
arguments stop after two failures. Successful tool reads reset that guard.

Context compaction creates a bounded provider view retaining rules, original goal, requirements, checks and cumulative clarifications (12 KB),
artifact sets/counters and recent evidence. Full original messages remain in
SQLite. It is deterministic omission, not a model-written semantic summary;
older omissions are disclosed and artifacts should be reread. If immutable
rules/goal cannot fit, task is budget_exhausted. Budgets are checked before calls
and before actions. Cooperative cancellation/deadlines do not kill an in-flight
request: the configured connect/read timeout bounds ordinary waits, but Python
threads and an adversarial slow-drip provider are not hard wall-clock sandboxes.
Chat-stream Stop is checked between parsed SSE lines, so a stalled or unterminated
line can also delay cancellation.
No token-price/currency billing estimator is claimed.

Run **one server process**. Unix uses stdlib `fcntl.flock`; Windows uses
`msvcrt.locking` on the first lock-file byte. Both refuse a second owner; closing
the handle releases ownership. No Gunicorn multiworker, Flask reloader or
network-filesystem database. Use local disk and threaded serving. Unix database
and lock mode is 0600; on Windows restrict the data directory with Windows ACLs.
Linux and native Windows execution are tested; symlink and POSIX mode assertions
are conditional on platform support. Migration from future incompatible
schemas, distributed queues, hard sandboxing and automatic workspace GC are absent.

For any tunnel/proxy use exposed mode, strong `GROK_APP_PASSWORD`, stable secret,
HTTPS/secure cookies as documented in README. Local-only authentication safeguards
from PR #3 remain. The password is single-owner access, not multi-user accounts.

## Repeatable verification

```sh
python -m unittest discover -v
python -m py_compile bot.py agent_loop.py webui.py task_harness.py
```

Tests exercise real SQLite reopen/thread workers; HTTP server disconnect and
restart with same cookie; subprocess kill/restart against a dummy local HTTP
provider; owner isolation, upload/download byte invariants, stop/resume races,
no-write replay, copy snapshot recovery, acceptance failure, malformed output,
retry classification, budgets, context retention, worker cap and process lock.
Live-provider smoke tests are separate and dummy-content only; passing offline
tests does not guarantee a given model follows the protocol or has free quota.

## Browser tools (optional)

The agent can optionally get read-mostly web access: `browse_open` (navigate and
read a page), `browse_read` (read the current page), `browse_click` (click by
CSS or text selector), `browse_type` (fill a field), `browse_close` (release the
browser early). One headless Chromium page lives per workspace and is closed
when the task run ends. Page text is untrusted data, truncated to
`GROK_BROWSER_TEXT_LIMIT` (default 12000 characters).

**Off by default.** `GROK_BROWSER_ENABLED=on` turns on open/read/close.
`browse_click` and `browse_type` can act on pages (submit forms, trigger
requests), and the task loop has no approval prompt, so they need a second
opt-in: `GROK_BROWSER_INTERACT=on`. Until then they return an error.

Playwright is an optional dependency: `pip install playwright && playwright
install chromium`. Without it, browse actions fail with an install hint instead
of crashing the harness. Other settings: `GROK_BROWSER_HEADLESS=no` keeps a
visible window; `GROK_BROWSER_EXECUTABLE` points at a custom Chromium binary;
`GROK_BROWSER_ARGS` adds launch flags (comma-separated);
`GROK_BROWSER_TIMEOUT` bounds navigation and element waits (default 30s).

Guards: only http/https URLs. Every URL is checked with `grokbot.netguard`:
hostnames are resolved and anything that points at a private, loopback,
link-local, multicast or reserved address is refused (so a public name that
resolves to 127.0.0.1 is blocked), as are `.local`, `.internal`, `.localhost`
and `.arpa` names and unresolvable hosts. The check also runs on every browser
request through a Playwright route, which covers redirect hops, subresources
and navigations caused by clicks, and again on the final page URL before text
is read. This is best-effort: like `fetch_url`, it does not defend against DNS
rebinding between the check and the connection, and WebSocket traffic is not
routed.
`test_grokbot_browser.py` covers protocol registration,
the URL guard (including DNS-to-private and redirect hops), default-off and
the interact opt-in, truncation, workspace dispatch, the missing-Playwright
hint and close idempotence.
