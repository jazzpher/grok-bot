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

Only `file_text` (exact UTF-8 bytes) and `exact_copy` (`source_path`, `path`, current
bounded byte equality) are supported. Unsupported check types return 400. Without
an explicit check a proposed completion becomes `needs_input`, not a claim of
success. Adding/changing criteria requires a new task; Resume is for clarification
or interruption, not changing the acceptance contract. Read-back is required for
files written by this task, but it is **not** general semantic verification.
Existing artifacts may satisfy a check without a new write. Checks verify present
content, not authorship, quality, correctness of arbitrary calculations, or an
immutable historical source. Download and independently review important results.

## API

- `POST /task` JSON: `message`, optional `checks`, optional lower `budgets`; 202
  returns `id`, `status_url`. This replaces the old request-bound NDJSON protocol.
- `GET /tasks`: most recent 50 tasks for this session.
- `GET /tasks/ID`: status and cumulative counters.
- `GET /tasks/ID/events?after=SEQ`: ordered batch of up to 200 events, global
  monotonic cursor. Continue until fewer than 200; do not assume one batch is all.
- `POST /tasks/ID/stop`: cooperative cancellation.
- `POST /tasks/ID/resume` JSON `{}` or `{"answer":"..."}`: resumes only stopped,
  interrupted or needs_input tasks. Completed/blocked/budget_exhausted are terminal.
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

Defaults/hard upper bounds: 12 provider attempts, 300 seconds cumulative active
elapsed time, 128000 cumulative UTF-8 bytes of model/tool output, 24000 serialized
context characters. Client budgets may only lower these. Minimum context is 4000.
Transient connection/timeouts and HTTP 500/502/503/504 retry at most three
consecutive attempts with cancellable bounded backoff; each costs a step.
400/401/403/429 never retry. No fallback model, billing changes or quota bypass.
Malformed actions consume steps without tools. Repeated same failed required
arguments stop after two failures. Successful tool reads reset that guard.

Context compaction creates a bounded provider view retaining rules, original goal,
artifact sets/counters and recent evidence. Full original messages remain in
SQLite. It is deterministic omission, not a model-written semantic summary;
older omissions are disclosed and artifacts should be reread. If immutable
rules/goal cannot fit, task is budget_exhausted. Budgets are checked before calls
and before actions. Cooperative cancellation/deadlines do not kill an in-flight
request: the configured connect/read timeout bounds ordinary waits, but Python
threads and an adversarial slow-drip provider are not hard wall-clock sandboxes.
No token-price/currency billing estimator is claimed.

Run **one Linux server process**. A stdlib `fcntl` database lock refuses a second
process; no Gunicorn multiworker, Flask reloader or network-filesystem database.
Use local disk, threaded serving, restrictive filesystem permissions, and never
mount the data directory as public static files. Database/lock mode is 0600.
Windows support, migration from future incompatible schemas, distributed queueing,
hard process sandboxing and automatic workspace GC are not included.

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
