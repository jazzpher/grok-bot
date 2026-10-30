# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Memory supersession (bitemporal corrections).** Memories can carry a fact key
  (subject/environment/attribute), `effective_from` (when the fact became true) and `recorded_at`
  (when it was learned). `save(..., supersedes=<id>)` and a new `supersede_memory` tool replace a
  fact while keeping the old value in `history(owner, subject, environment, attribute)`; search,
  `relevant()` and the pinned-injection step only return facts current *now*, so a future-dated
  correction never hides its predecessor early, a backdated correction fixes history without
  replacing the current value, and same-key facts saved without a link stay visible with an
  advisory `candidate_supersedes` flag. Keyless or partially-keyed facts never hide one another.
  Rows replaced by an already-effective successor no longer count against the 2000-per-owner budget;
  predecessors of future-dated corrections (still active), ambiguous, candidate-flagged and
  backdated rows still do. Deleting the newest value in a chain restores its predecessor; deleting a
  mid-chain row is refused so no old value is silently resurrected. Text edits on superseded rows are
  refused so history cannot be rewritten (`update()` keeps its legacy overwrite semantics for live
  rows). An explicit link dominates backdate grouping, so an explicit replacement effective earlier
  than its target stays visible instead of vanishing with it, and a keyed row can only be replaced
  under the same key (a fully keyless legacy row may be superseded under a new key, which then
  defines the chain). The memory panel marks superseded/history entries (pin/edit actions are only
  offered on current rows) and counts only current memories. Existing databases migrate in place
  (old rows are dated at `created`). Nothing is auto-superseded, pruned or
  deleted: `forget_memory` remains the only delete, now documented for wrong facts only.

Verifiable agent loops: pixel-diff and command verifiers gate task completion.

### Added

- **Verifiable agent loops.** The durable harness gates `done` on two new deterministic
  check types alongside `file_text`/`exact_copy`: `pixel_diff` (Pillow pixel comparison of
  an output image or headlessly rendered HTML page against a ground-truth image; numeric
  `max_diff` threshold, per-channel `tolerance`, up to 8 `ignore` rectangles, and a `note`
  field documenting known-irrelevant exemptions; highlighted diff saved as `<name>.diff.png`)
  and `command` (owner-supplied acceptance command - tests pass, output matches - run on the
  configured sandbox backend with network disabled; hardline-blocked commands refused at
  submit). A failed check rejects `done` with the measured numbers, so the agent fixes the
  artifact and retries within its step budget instead of declaring success on a feeling.
- **`verify_visual` tool** for the interactive agent: the same deterministic pixel diff
  (workspace image or rendered `.html` output vs a ground-truth image, rendered `.html`, or
  public URL through the existing GROK_PREVIEW / GROK_BROWSER_ENABLED machinery), returning
  the mismatch percent and saving a highlighted diff image next to the output.
- Pillow (`pillow>=10.0`) is now a dependency; image sides need nothing else, HTML rendering
  uses the existing Playwright setup.

### Added (earlier, same unreleased cycle)

Reliability pack: real cancellation, per-run log, backup/restore, optional edit review.

- **Reliable cancellation.** Stop now interrupts a hung provider stream and a running shell
  command within about a second: a watchdog shuts the socket down (close alone does not wake a
  blocked read on Linux), and both sandbox backends poll the stop event and kill the process
  group / container. A stopped command renders as `STOPPED by user`.
- **Hard total cap on provider streams** (`GROK_STREAM_TIMEOUT`, default 600s). The per-read
  timeout could not bound a provider that keeps dribbling bytes; the stream now aborts with a
  clear error when the wall-clock cap passes.
- **Per-run log.** Every turn (web, CLI, Telegram, scheduled) appends one JSON line to
  `.runlog.jsonl` in the data directory: run id, source, model, goal, start/end, status,
  duration, steps, every tool call with ok/duration/error, usage, and the turn's final error.
  Rotated by size. `grokbot runs` prints it; `GET /runs` serves it as JSON.
- **Backup and restore.** `grokbot backup --out file.zip` snapshots the data directory
  (SQLite stores through the backup API, so it is consistent while the server runs);
  `grokbot restore --from file.zip` verifies the archive first (zip readable, every database
  passes `PRAGMA integrity_check`, no path traversal) and keeps the previous data at
  `<dir>.pre-restore-<timestamp>` instead of deleting it.
- **Optional edit review** (`GROK_APPROVE_EDITS=on`): write_file / edit_file / copy_file /
  write_docx ask for approval with a unified diff of the proposed change. Do not combine with
  unattended schedules (their approval=never would deny every edit).
- **benchmark.py**: 10 offline tasks (plain turns, file edits, commands, Stop latency, stream
  cap, backup/restore, run log) for before/after comparisons.

### Fixed

- Docs: `preview_page`'s route guard does not see redirect hops (Playwright does not route them); it is safe
  only because the preview server never answers 3xx. Its docstrings, docs/SECURITY.md and the README's web
  browsing paragraph no longer claim per-hop checks.

## [0.9.3] - 2026-10-18

### Added

- **Interactive web browsing for the /agent composer** (`browse_open` / `browse_see` / `browse_scroll` /
  `browse_click`, off by default behind `GROK_AGENT_BROWSER=on`; needs Playwright + Chromium). The agent opens
  public web pages in a headless Chromium and receives each view as a screenshot image (vision models;
  `GROK_AGENT_BROWSER_IMAGES=off` keeps the text snapshot only) plus a bounded text snapshot with numbered
  clickable elements. Safety shape: the public netguard (no loopback, LAN, private or reserved addresses, no
  userinfo) vets each request Playwright routes - top-level navigations, subresources and WebSockets - before it
  is sent, and the user approves each new domain before it loads (per-domain "always allow" rules; a one-time
  allow covers that host for the turn; navigations to an unapproved host are refused before they are sent). **Known
  gap:** redirect hops are *not* vetted - Playwright does not route them - so a public or approved page that
  answers 3xx to `127.0.0.1`, a LAN address or an unapproved host still gets that request sent; only the final
  page is checked afterwards, then blanked and refused (its content never reaches the model, but the request
  itself already happened). The netguard's DNS lookup is separate from Chromium's own, so DNS rebinding is left
  to Chromium's built-in local-network checks. Keep the feature off except on a trusted machine until a
  vetting-proxy fix lands (see docs/LIMITATIONS.md). No typing exists, the browser context is ephemeral (no
  cookies, profile or saved logins) and downloads are never accepted; the Playwright driver and Chromium start
  from a scrubbed environment (no API keys or tokens); each browse action has a wall-clock limit after which the
  browser is killed; screenshots reach the model labelled as untrusted page content; at most
  `GROK_AGENT_BROWSER_MAX_ACTIONS` (default 8) browse actions per turn; Chromium is killed at turn end.
  Unattended runs (scheduler) cannot prompt, so domains without a saved rule are denied there.

## [0.9.2] - 2026-10-17

Hard-test fixes (live runs against a small free model, reproduced offline).

### Fixed

- **A malformed JSON tool call is no longer reported as success.** With the JSON fallback protocol, a reply
  containing a wrong-schema or truncated call (`{"action": …}`, missing `}`, …) was shown as the final answer and
  `grokbot exec` exited 0 while nothing had run. The model now gets up to two corrective nudges naming the exact
  parse error; if it keeps failing, the turn ends with status `error` (exit 1) and an explicit message. Only a
  real call attempt is treated this way (a `"tool"` key, or `"action"`/`"function"` with `"arguments"`/`"args"`,
  or a lone `{"action": …}` object that is the whole reply): JSON quoted inside a prose answer or code fence
  stays a normal answer.
- **HTTP 400 now says why.** The provider's own error message is included after redaction (API keys, bearer tokens
  and URLs) instead of a bare "rejected the request".

### Added

- `GROK_TESTS_READ_ONLY=1`: file tools refuse to modify test paths, so an eval run cannot "fix" a failing test by
  rewriting it (opt-in; shell writes are not covered — documented in LIMITATIONS).
- `preview_page` tool (off by default, `GROK_PREVIEW=on`): renders a workspace HTML file in headless Chromium
  and returns the screenshot to the model as an image plus measured layout facts (horizontal overflow, top
  element boxes, best-effort contrast). Loopback-only workspace-confined static server; the page cannot reach
  `file://`, other local ports, the LAN or the internet. Chromium starts on first preview and is killed at turn
  end; 3 previews per turn; `GROK_PREVIEW_IMAGES=off` for text-only models. Ships the `ui-preview-pass` skill
  under `docs/skills/`.

## [0.9.1] - 2026-10-17

Review fixes: the data directory is stable, the session secret is explicit, and the
Docker sandbox matches its documentation.

### Fixed

- **Sessions no longer look empty after a restart.** A generated `GROK_SESSION_SECRET`
  is persisted (0600, atomic create) next to the workspaces instead of being random per
  boot, so conversations, workspaces, tasks and memories stay reachable.
- **Stable default data directory.** Without `GROK_WORKSPACE_DIR`, data lives in
  `$XDG_DATA_HOME/grokbot/workspaces` (`~/.local/share/grokbot/workspaces`) or
  `%LOCALAPPDATA%\grokbot\workspaces` on Windows, not in the current working directory —
  starting the app from another folder no longer opens a different, empty dataset.
  Startup prints the data directory and whether sessions are `env`, `persisted` or `ephemeral`.
- **The Docker backend is really read-only-rooted.** `docker run` now passes `--read-only`,
  a 256 MB writable tmpfs at `/tmp` (which is also `HOME`/`TMPDIR`), `--init` and
  `--pull missing`. Installing packages into the image fails by design; use a venv in the
  workspace. `docs/SECURITY.md` and `docs/LIMITATIONS.md` describe what the sandbox does.
- `grokbot.mcp` reports the package version instead of a hard-coded one.

### Documentation

- README no longer implies that the interactive agent drives a browser: `browse_*` tools are
  opt-in and currently run in durable background tasks (`/task`) only.
- Replaced the stale "no browser automation" limitation with what the browser tools actually do.

### Packaging

- `LICENSE` (MIT) is shipped with the wheel; `pyproject.toml` declares the MIT classifier.
- `pip install .` ships `grokbot.gateway`, `grokbot/templates` and `grokbot/static`;
  CI now installs the wheel and smoke-tests `/` and `/static/app.js`.
- `.coverage` and build artifacts are ignored.

## [0.9.0] - 2026-10-16

First agent-v2 release: unified tool-using agent with native tool calling and a JSON
fallback, sandbox backends (subprocess/docker/off) with a read-only/workspace-write/
full-access policy and approvals, compaction, conversations in SQLite with FTS5, long-term
memory, sub-agents, scheduled automations, an MCP client, a Telegram gateway, plugins,
a local-provider preset, images, skills export/import, and the new web UI.

## [0.8.x] - 2026-10-12 … 2026-10-16

Model catalog discovery, provider pickers and manual fallback, durable background tasks
with explicit artifact checks and resume, document/PDF/DOCX tooling.
