# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
