# Known limitations (honest list)

- **Subprocess backend is not a security boundary.** Approvals and heuristics reduce risk; they do not contain a
  hostile command. Use the Docker backend (`GROK_SANDBOX_BACKEND=docker`) or run the whole app in the provided
  container when you let the agent run commands on untrusted repositories.
- **Heuristics are heuristics.** Destructive-command detection, read-only whitelist, network/escape detection are
  regex/token based. Obfuscated commands can slip through (which is why the policy defaults to asking in
  read-only mode and why the Docker backend exists).
- **Token estimates are approximate** (~3.6 chars/token). Compaction triggers at 70% of the configured window; if
  the provider's real window is smaller than configured, you can still get a context-length error — lower the
  window in Settings (it is auto-filled when the model list exposes it).
- **The Docker sandbox has a read-only root.** Only the workspace mount and a 256 MB `/tmp` are writable, so
  `pip install` / `apt-get install` inside the sandbox fail; use a venv in the workspace. The image is pulled only
  when missing (`--pull missing`), so a cold cache needs network access on the host.
- **JSON tool protocol fallback is weaker** than native tool calling: one tool call per step, and models sometimes
  wrap JSON in prose. It is only used when the provider rejects `tools`.
- **Web search without a key** scrapes DuckDuckGo HTML, which is best-effort and may break; set `TAVILY_API_KEY`,
  `BRAVE_API_KEY`, or `GROK_SEARXNG_URL` for reliable results.
- **Durable background tasks still use the legacy JSON harness** (`/task`), not the new agent loop. They are
  resumable across restarts but do not get native tool calling, the sandbox policy or web tools yet.
- **Reconnect is coarse.** If the page reloads during a turn, the worker keeps running and the result is persisted;
  the UI polls `/agent/current` and re-renders from the stored entry instead of resuming the live stream.
- **Single-process state.** Sessions, pending approvals and running turns live in process memory; run one worker
  process (as `webui.py` does). Conversations, policies and tasks are on disk.
- **Memory is keyword-ranked, not semantic.** Relevant memories are chosen with FTS5/LIKE matching plus pinning
  (bounded to ~12 items / 2400 chars per turn). There is no embedding store; phrase facts the way you would search
  for them, or pin the ones that must always apply.
- **Scheduled runs are unattended by design.** They run with `approval=never`, so anything outside the sandbox policy
  is denied rather than queued; results land in a ⏰ conversation. The scheduler lives in the web process (one
  process, one scheduler); jobs missed while the server was down run once at the next tick.
- **Sub-agents share the workspace and policy** of the parent — isolation is about context and tool set, not about
  files. Depth is limited (`GROK_MAX_SUBAGENT_DEPTH`, default 1) and sub-agents run sequentially inside the parent's turn.
- **MCP client only, core features.** stdio + Streamable HTTP, `tools/list` and `tools/call` with text/resource
  results. No sampling, roots, prompts/resources browsing, OAuth flows, or server mode (`grok-bot` as an MCP server).
  MCP tools are trusted as much as the server you configure — annotations (`readOnlyHint`, `destructiveHint`) are
  hints from the server, so the approval policy still applies to anything that is not read-only.
- **Telegram gateway is text-only** (no photos/documents yet), long-polling, one bot per process, and strictly
  allowlisted by user id. Replies are plain text (no Markdown entities) to keep provider output from breaking
  formatting; messages longer than 4000 chars are split.
- **Images are passed through, not processed.** Attachments become `image_url` parts; the provider/model must support
  vision. Images stay in the stored context of that conversation (compaction drops them like any old message).
- **Voice is browser-side only.** Dictation uses the Web Speech API when the browser offers it (Chrome/Edge/Safari);
  there is no server-side STT/TTS.
- **No Discord/Slack/WhatsApp gateways or image generation** yet; the gateway runner
  interface (`run_owner_turn`) is platform-agnostic, so adding one is a thin adapter like `grokbot/gateway/telegram.py`.
