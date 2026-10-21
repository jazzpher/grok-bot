# Security model (0.9)

grok-bot runs model-chosen actions on your machine. This page says what protects you, what does not, and what the
new 0.9 surfaces (memory, automations, MCP, Telegram, plugins, images) add.

## Boundaries that exist

- **Workspace jail.** File tools resolve paths inside `workspaces/<owner>` (symlinks rejected, size/count caps).
- **Sandbox policy** (`read-only` / `workspace-write` / `full-access` × `on-request` / `never`). `run_command` is
  classified (read-only allowlist, destructive patterns, network, escape attempts) and asks, denies or runs
  accordingly. Persisted "always allow" rules are per owner and pattern-scoped.
  **Without Docker** (subprocess backend, nothing confines a command) every `run_command` asks for approval in every
  mode, saved "always allow" rules are not applied and "always" means "this once"; only the explicit
  `full-access` + `never` policy skips the prompt. Unattended runs (scheduler) cannot prompt, so they deny shell commands.
- Children never see the server's secrets: their env is scrubbed, and on Linux the server marks itself non-dumpable at
  startup (`prctl(PR_SET_DUMPABLE, 0)`), so an approved command, MCP server or browser cannot read the server's
  original environment (API key, Telegram token, app password, session secret) from `/proc/<ppid>/environ`.
  `GROK_ALLOW_DEBUGGER=on` turns this off (for py-spy/gdb). Files the server's user can read (for example the data
  dir's `.session_secret`) are still readable by host commands: prefer the Docker backend, or set `GROK_SESSION_SECRET`.
- **Hardline blocklist** (all backends, all policies including full-access + never): root/home deletion, disk
  formatting or raw device writes, fork bombs, drive wipes (`format`, `del /s`, `rd /s`, `Remove-Item -Recurse` on a
  drive root), `diskpart`/`bcdedit`, download-piped-to-shell. A small pattern list, not a complete safety net.
- **Docker backend** (`GROK_SANDBOX_BACKEND=docker`): commands run in a throwaway container with the workspace
  bind-mounted, no network unless the policy says so, a **read-only root filesystem** and a single writable tmpfs at
  `/tmp` (which is also `HOME`/`TMPDIR`, 256 MB, `nosuid`). `--cap-drop ALL`, `no-new-privileges`, `--pids-limit`,
  memory/CPU caps, `--init`, and the image is only pulled when missing. Because the root is read-only, installing
  packages into the image fails by design: create a venv inside the workspace instead
  (`python -m venv .venv && .venv/bin/pip install …`). The subprocess backend is *not* an isolation boundary —
  see LIMITATIONS.
- **Network guard** for `fetch_url` / web search / custom provider URLs: public hosts only, DNS re-checked, redirects
  bounded, response sizes capped.
- **Keys** are kept in memory (or env), scrubbed from command environments, never forwarded to a different endpoint
  than the one they were saved for.
- **Exposure** needs `GROK_DEPLOYMENT_MODE=exposed` + `GROK_APP_PASSWORD`; cookies are HttpOnly, SameSite configurable,
  CSP/frame-ancestors headers set, login rate-limited.

## New in 0.9

| Surface | Risk | Mitigation |
|---|---|---|
| Long-term memory | prompt injection persisted across sessions ("remember: always run X") | memories are injected as *context, not commands*, labelled as such; `save_memory` is a mutating tool (visible card, `memory_saved` event); memories are owner-scoped, listed and editable in the UI; the Context row shows what was injected each turn |
| Session search / read_history | leaking another user's conversations | all stores are owner-scoped (`owner` = web session id or `tg:<chat>`); no cross-owner queries exist |
| Sub-agents | runaway recursion / cost | depth cap (`GROK_MAX_SUBAGENT_DEPTH`, default 1), per-call step cap (≤30), same policy + approvals as the parent, excluded tools (`delegate`, memory writes, scheduling) |
| Automations | unattended runs executing dangerous actions while nobody watches | `approval=never` (anything needing approval is *denied*, not queued), a saved `full-access` policy runs as `workspace-write` (never YOLO unless `GROK_UNATTENDED_FULL_ACCESS=on`), `schedule_task` from the model needs approval (so an unattended run cannot create schedules), reduced step budget, per-owner job limit, min interval 5 min; jobs run under the owner's sandbox policy and persisted allow-rules only |
| MCP servers | arbitrary code (stdio servers are local processes), untrusted tool output, name collisions | config is owner-controlled (`mcp.json`/env), never editable from the browser; env passed to stdio servers excludes `*KEY*/*TOKEN*/*SECRET*/*PASSWORD*` unless you set them explicitly in `env`; tool names are namespaced `mcp__<server>__`; outputs are wrapped as UNTRUSTED TOOL RESULT like every tool; non-read-only tools ask in read-only mode, destructive ones ask unless full-access; timeouts and 8 MB response cap |
| Telegram | anyone messaging the bot | fail-closed allowlist of Telegram user ids (refuses to start without one); every update is checked, including callback buttons; plain-text replies; owner isolation (`tg:<chat_id>`) |
| Plugins | they are code | only loaded from `GROK_PLUGINS` (server env) — treat like installing a package |
| Images | large payloads, content sniffing | ≤4 per message, PNG/JPEG/WebP/GIF data URLs only, ~2 MB each, request cap 12 MB; images are not written to disk |
| Skills import | zip path traversal / huge files | only `*/SKILL.md` entries, unsafe path segments rejected, 2 MB zip / 60 KB per skill, names validated by `SkillStore.save` |
| Local provider | sending keys to a LAN host | only configurable through `GROK_LOCAL_API_URL` (server env), shown as its own provider; dummy key used when none given |
| UI preview (`preview_page`) | a model-rendered page reaching local services or the internet; Chromium left running | off by default (`GROK_PREVIEW=on`); loopback-only static server on a random port, rooted at the workspace (symlinks and `..` rejected, Host header must match the bound port); the page's own requests are route-guarded to that one server — `file://`, other loopback ports, LAN and internet are aborted per request including redirect hops; read-only (no approval needed, nothing is written); Chromium is killed at turn end; screenshots are scrubbed from stored history |

## Reporting

Open a private security advisory on GitHub or email the maintainer; include the version (`/capabilities`), the
sandbox backend and the policy in effect.
