# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.3] - 2026-10-18

### Added

- **Interactive web browsing for the /agent composer** (`browse_open` / `browse_see` / `browse_scroll` /
  `browse_click`, off by default behind `GROK_AGENT_BROWSER=on`; needs Playwright + Chromium). The agent opens
  public web pages in a headless Chromium and receives each view as a screenshot image (vision models;
  `GROK_AGENT_BROWSER_IMAGES=off` keeps the text snapshot only) plus a bounded text snapshot with numbered
  clickable elements. Safety shape: public netguard on every request including redirect hops (no loopback, LAN,
  private or reserved addresses, no userinfo); the user approves each new domain before it loads (per-domain
  "always allow" rules; a one-time allow covers that host for the turn); pages that navigate to an unapproved
  domain are blanked and blocked; no typing exists, the browser context is ephemeral (no cookies, profile or
  saved logins) and downloads are never accepted; at most `GROK_AGENT_BROWSER_MAX_ACTIONS` (default 8) browse
  actions per turn; Chromium is killed at turn end. Unattended runs (scheduler) cannot prompt, so domains
  without a saved rule are denied there.

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
