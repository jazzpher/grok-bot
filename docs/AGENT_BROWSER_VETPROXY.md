# Agent browser: vetting proxy + network isolation (experimental branch)

Status: EXPERIMENTAL, branch `exp/agent-browser-vetproxy` only. Not merged,
not enabled anywhere by default. Linux only. Do not enable
`GROK_AGENT_BROWSER` on a real installation while the open gaps below stand.

## Threat model

The interactive agent browser (`grokbot/agent_browser.py`) lets a model open
public web pages and read them. A page is hostile input: it can redirect to
`127.0.0.1` or the LAN, embed subresources from hosts the user never approved,
or race DNS. Before this branch, Playwright routed only the top-level
navigation through the public netguard; redirect hops and subresource requests
went out unvetted and were only blanked after landing.

## Design

Three pieces, all stdlib + bubblewrap, no new dependencies:

1. **Vetting proxy** (`grokbot/vetproxy.py`). The only network path the
   browser can use. A connection is allowed only when the destination host is
   user-approved AND resolves to public IPs. The proxy resolves and pins DNS
   itself, so Chromium never does DNS and a rebinding flip changes nothing.
   Ports 80/443 only. Every refusal is recorded and surfaced to the user as
   "approve this host and retry".
2. **Network isolation** (`grokbot/browser_isolation.py`). Chromium launches
   inside a bubblewrap network namespace with no external interfaces: direct
   egress is impossible by construction, not by policy. The namespace mounts
   a MINIMAL read-only filesystem (runtime libraries, a narrow /etc
   allowlist - fonts, CA store, glibc lookup files - never wholesale /etc,
   the Chromium install directory, the relay script - everything else is
   absent, not just unreadable), a fresh minimal /dev, private /tmp and
   /run (host path-based unix sockets and host files are unreachable), a
   fresh /proc (only namespace processes visible), no capabilities
   (--cap-drop ALL; measured: CapInh/Prm/Eff/Bnd/Amb all zero inside), an
   isolated IPC namespace (measured: a host-created SysV segment is not
   visible inside) and a new session (--new-session; measured: the browser
   is detached from the host session); abstract unix sockets are isolated
   with the network namespace (measured). Isolation is
   MANDATORY: `available()` launches the exact production flag and mount
   setup through a real bwrap run (cached), not an executable lookup or a
   generic probe, and
   without a working bubblewrap the browser refuses to start; there is no
   fallback to an unproxied browser. This is NOT a claim of containment
   against a fully compromised renderer: it removes network paths and host
   data; kernel and bwrap semantics themselves are assumed, not proven.
3. **In-namespace relay** (`grokbot/isorelay.py`). A loopback relay inside the
   namespace forwards to the proxy on the host, so the browser's only route
   out leads through vetting.

## Measured vs structural

Measured behaviorally (live Chromium tests, `test_vetproxy.py`, plain HTTP
and real TLS+HTTP/2 fixtures):

- A 302 redirect to `127.0.0.1` never reaches the server (it fired before
  this branch).
- A redirect to an unapproved host never fires (HTTP and HTTPS); the denial
  is recorded. A redirect to an approved host loads.
- An unapproved CDN subresource is blocked and named in the tool result.
- Direct top-level navigation to an unapproved origin, with the approved
  h2 connection already established, is refused and the destination sees
  zero requests.
- HTTP/2 coalescing: with a wildcard certificate covering both origins at
  the same IP:port AND `--host-resolver-rules` handing Chromium the shared
  IP - every condition h2 coalescing needs - the unapproved origin still
  got its own fresh CONNECT, vetoed by the proxy, with zero server hits.
  Through an explicit CONNECT proxy Chromium does not resolve DNS itself,
  so cross-origin h2 reuse had no basis on the tested build.
- Mid-session DNS rebinding: an approved host whose resolver flips to an
  internal address between navigations is refused by the proxy (resolved
  IPs are re-vetted on every connection, not trusted from the resolver);
  the internal target records zero requests and the host is reported back.
- Speculative hints (`<link rel="preconnect">`, `dns-prefetch`) to an
  unapproved host fired no connection at all on the tested build.
- Service workers: `service_workers='block'` is set on the context; the
  worker script is never fetched (no proxy transit, no server hit).
  Playwright fulfills the script request locally with empty content, so
  `register()` may still resolve with an empty, code-less worker.
- WebSockets are refused entirely, at both the agent-action layer and the
  proxy route layer (read/click-only design; see gaps).
- The 28 pre-existing live browse tests pass through the proxy+isolation
  path unchanged, including with the minimal-mount wrapper.
- The PRODUCTION wrapper itself (not a hand-rolled bwrap) is tested: no
  /sys, no real NIC (lo + kernel down pseudo-devices only), test-created
  host /tmp file and filesystem-socket canaries invisible, a known-existing
  non-allowlisted /etc file absent, the relay-script mount chain containing
  no siblings at any level (asserted exactly), host abstract unix sockets
  unreachable, all capability sets zero inside, a new session, host SysV
  IPC invisible, the loopback relay round-trips through the real wrapper,
  and missing/unusable bwrap refuses startup (probe of the exact production
  setup, not an executable lookup).
- The production default IP guard (netguard._bad_ip, no test seam) refuses
  approved private literals and approved hosts resolving private across
  loopback, 10/8, 172.16/12, 192.168/16, link-local, CGNAT, ::1, ULA and
  IPv4-mapped IPv6 - at function and socket level.

Measured structurally (isolation tests):

- Inside the namespace, the interface list read through a fresh `/proc`
  mount is loopback-only, and a raw-IP outbound connect attempt fails.
- Chromium's own background chatter (update.googleapis.com etc.) is caught
  and denied by the proxy - evidence the namespace has no other way out.

Structural assumptions, NOT proven:

- Bubblewrap namespace semantics themselves (kernel enforcement).
- That no Chromium feature bypasses the configured proxy for connections
  other than the chatter classes observed in tests.
- HTTP/2 coalescing behavior is Chromium-version-dependent: the tests
  above are the regression guard, not a proof about future builds. QUIC is
  flag-disabled and the namespace blocks UDP, so h3 has no path.
- The service-worker block relies on Playwright's context option; the
  network-level property (no worker script fetch) is what is measured.

## Open gaps

- **WebSockets cannot be vetted as WebSockets.** A CONNECT tunnel cannot
  distinguish WSS from HTTPS, so no proxy layer can apply WS-specific
  policy. WS is therefore refused as a feature. Do not claim the proxy
  "denies WebSockets" - it cannot identify them; the browser-side route
  guard is what refuses them.
- **Host-scoped policy.** The proxy sees host:port per tunnel, not URLs, so
  approval is per host, not per path.
- **Version drift.** h2 coalescing and speculative-hint behavior are
  measured for the tested Chromium build only; keep the suite as the
  standing guard when Chromium is upgraded.

## Denial reporting

The tool result distinguishes what the user can act on: `blocked_hosts`
are unapproved hosts the user MAY approve ("approve this host and retry"),
while `refused_hosts` are policy refusals (private/internal address,
forbidden port, failed verification) that approval cannot fix - the text
says so instead of offering a pointless approval. Denial reasons come from
the proxy's fixed internal set; page-supplied text is never echoed into
the reason. Denials split into blocked_hosts (only "host not approved" -
approval-fixable) and refused_hosts (private address, forbidden port,
verification failure - approval cannot allow these), and the model-facing
text says so explicitly.

## Chromium runtime mount

The wrapper mounts the Chromium install directory read-only (unless it
already lives under /usr, which is mounted). A configured executable that
sits directly in a broad location (/, /opt, /usr/local, /tmp, a home root,
any one-component path) is REJECTED with an explicit error instead of
mounting that whole tree: misconfiguration must narrow the failure, not
widen the mount.

## Resource bounds

The proxy caps concurrent connections (128, fail-fast 503 beyond), tracks
every live connection - client AND upstream - and `close()` tears down
in-flight tunnels and joins its workers instead of leaving them to the
120s idle timeout. Measured by ConcurrencyCapTests (overflow refused,
slot release recovers) and CloseActiveTunnelTests (an active tunnel ends
promptly on close()).

## Operational note: orphaned processes

bwrap PID-namespace teardown reparents Chromium grandchildren to PID 1. On
hosts whose PID 1 does not reap (minimal containers, some sandboxes), they
accumulate as zombies until the app's own `RLIMIT_NPROC` sandbox limit makes
every spawned command-check fail with `Cannot fork`. Run browser tests
through `tools_reaprun.py` (a child subreaper) on such hosts, and terminate
test runs with SIGTERM so `close()` runs. Long-running servers that launch
isolated browsers on such hosts need the same treatment.

## Platform scope

Linux + bubblewrap only. There is no Windows or macOS isolation path; on
those platforms the browser refuses to start under this branch. A WSL2 or
Docker-based path is a separate, undecided piece of work.
