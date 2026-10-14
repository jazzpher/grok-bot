# Known limitations (honest list)

- **Subprocess backend is not a security boundary.** Approvals and heuristics reduce risk; they do not contain a
  hostile command. Use the Docker backend (`GROK_SANDBOX_BACKEND=docker`) or run the whole app in the provided
  container when you let the agent run commands on untrusted repositories.
- **Heuristics are heuristics.** Destructive-command detection, read-only whitelist, network/escape detection are
  regex/token based. Obfuscated commands can slip through (which is why the policy defaults to asking in
  read-only mode and why the Docker backend exists).
- **Token estimates are approximate** (~3.6 chars/token). Compaction triggers at 75% of the configured window; if
  the provider's real window is smaller than configured, you can still get a context-length error — lower the
  window in Settings (it is auto-filled when the model list exposes it).
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
- **No MCP, subagents, scheduled jobs, messaging gateways or voice yet** (roadmap phases 4–5).
