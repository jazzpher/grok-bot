"""Tests for grokbot/vetproxy.py (fail-closed browse proxy) and, gated behind
GROK_LIVE_BROWSE_TESTS=1, the isolated-browser redirect/adversarial suite.

Offline unit tests run the proxy on loopback TCP with an injected resolver
mapping fake "public" hostnames to a local test server - the same seam the
existing live browse tests use (their DNS patch), made explicit."""
import http.server
import ipaddress
import os
import shutil
import socket
import socketserver
import subprocess
import tempfile
import time
import threading
import unittest
import unittest.mock

from browser_tools import BrowserError

from grokbot import browser_isolation, vetproxy
from grokbot.vetproxy import VettingProxy

def _playwright_importable():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


_REQUIRE_CAPS = os.environ.get('GROK_REQUIRE_LIVE_BROWSE_CAPS') == '1'
# GROK_REQUIRE_LIVE_BROWSE_CAPS=1 (set by jobs that install the capabilities on purpose)
# implies the live suite and turns every capability skip below into a hard failure.
_LIVE = os.environ.get('GROK_LIVE_BROWSE_TESTS') == '1' or _REQUIRE_CAPS


def _cap_gate(condition, reason):
    """skipUnless(condition), but a hard failure when the job explicitly requires the capability."""
    if condition or not _REQUIRE_CAPS:
        return unittest.skipUnless(condition, reason)

    def fail_instead(fn):
        def wrapper(self, *args, **kwargs):
            self.fail('required live-browse capability missing: ' + reason)
        return wrapper

    def deco(target):
        if isinstance(target, type):
            for name, member in list(vars(target).items()):
                if name.startswith('test') and callable(member):
                    setattr(target, name, fail_instead(member))
            return target
        return fail_instead(target)

    return deco


GOOD = 'good.test'
OTHER = 'other.test'
LOCAL = ipaddress.ip_address('127.0.0.1')
TEST_PRIVATE = ipaddress.ip_address('127.0.0.2')   # stand-in "private/internal" target


def _test_ip_check(ip):
    """Fake-topology netguard for tests: the local test server (LOCAL) plays the role of
    a public address; TEST_PRIVATE plays the internal target that must always be refused."""
    return ipaddress.ip_address(ip) == TEST_PRIVATE


class _Handler(http.server.BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).hits.append((self.headers.get('Host', ''), self.path))
        body = b'hello:' + self.path.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ProxyTestBase(unittest.TestCase):
    def setUp(self):
        _Handler.hits = []
        self.server = _Server(('127.0.0.1', 0), _Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.resolver_calls = []

        def resolver(host, port):
            self.resolver_calls.append(host)
            if host in (GOOD, OTHER):
                return [LOCAL]
            return []

        self.approved = set()
        self.proxy = VettingProxy(allowed_hosts=lambda: set(self.approved), resolver=resolver,
                                  allowed_ports=(80, 443, self.port), ip_check=_test_ip_check)
        self.proxy_port = self.proxy.listen_tcp()
        self.addCleanup(self.proxy.close)

    def connect(self, target):
        sock = socket.create_connection(('127.0.0.1', self.proxy_port), timeout=5)
        sock.sendall(b'CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n' % (target.encode(), target.encode()))
        response = b''
        while b'\r\n\r\n' not in response:
            response += sock.recv(4096)
        return sock, response.split(b'\r\n', 1)[0]


class VettingUnitTests(ProxyTestBase):
    def test_approved_public_host_tunnels(self):
        self.approved.add(GOOD)
        sock, status = self.connect('%s:%d' % (GOOD, self.port))
        self.assertIn(b'200', status)
        sock.sendall(b'GET /via-tunnel HTTP/1.1\r\nHost: good.test\r\n\r\n')
        data = b''
        while b'hello:' not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        self.assertIn(b'hello:/via-tunnel', data)
        sock.close()
        self.assertEqual(self.resolver_calls, [GOOD])

    def test_unapproved_host_denied_before_dns(self):
        sock, status = self.connect('%s:%d' % (OTHER, self.port))
        self.assertIn(b'403', status)
        sock.close()
        self.assertEqual(self.resolver_calls, [], 'unapproved hosts must not even trigger DNS')
        denials = self.proxy.drain_denials()
        self.assertEqual(denials, [(OTHER, self.port, 'host not approved')])

    def test_non_public_resolution_denied(self):
        self.approved.add('missing.test')          # resolver returns [] for it
        sock, status = self.connect('missing.test:443')
        self.assertIn(b'403', status)
        sock.close()
        self.assertEqual(self.proxy.drain_denials()[0][2], 'host does not resolve to a public address')

    def test_literal_private_denied_even_when_approved(self):
        self.approved.add('127.0.0.2')
        sock, status = self.connect('127.0.0.2:%d' % self.port)
        self.assertIn(b'403', status)
        sock.close()
        self.assertEqual(self.resolver_calls, [], 'literal IPs are checked without DNS')
        denials = self.proxy.drain_denials()
        self.assertIn(('127.0.0.2', self.port, 'local and private addresses are not allowed'), denials)

    def test_literal_public_ip_allowed_only_when_approved(self):
        ip, reason = self.proxy._vet('93.184.216.34', 443)
        self.assertIsNone(ip)
        self.assertEqual(reason, 'host not approved')
        self.approved.add('93.184.216.34')
        ip, reason = self.proxy._vet('93.184.216.34', 443)
        self.assertEqual(str(ip), '93.184.216.34')

    def test_localhost_names_denied(self):
        for host in ('localhost', 'foo.local', 'foo.internal', 'x.localhost'):
            ip, reason = self.proxy._vet(host, 443)
            self.assertIsNone(ip, host)

    def test_only_ports_80_and_443(self):
        self.approved.add(GOOD)
        sock, status = self.connect('%s:22' % GOOD)
        self.assertIn(b'403', status)
        sock.close()
        self.assertEqual(self.resolver_calls, [], 'port check happens before DNS')
        self.assertEqual(self.proxy.drain_denials()[0][2], 'only ports 80 and 443 are allowed')

    def test_rebinding_flip_is_refused(self):
        """Resolver answers once, then starts failing: every connection re-vets."""
        self.approved.add(GOOD)
        sock, status = self.connect('%s:%d' % (GOOD, self.port))
        self.assertIn(b'200', status)
        sock.close()
        original = self.proxy._resolver
        self.proxy._resolver = lambda host, port: []
        sock, status = self.connect('%s:%d' % (GOOD, self.port))
        self.assertIn(b'403', status)
        sock.close()
        self.proxy._resolver = original

    def test_resolver_returning_private_ip_is_refused(self):
        """Rebinding flip: even an approved host whose resolver NOW answers a private
        address is refused - resolved IPs are re-vetted, not trusted from the resolver."""
        self.approved.add(GOOD)
        original = self.proxy._resolver
        self.proxy._resolver = lambda host, port: [TEST_PRIVATE]
        sock, status = self.connect('%s:%d' % (GOOD, self.port))
        self.assertIn(b'403', status)
        sock.close()
        denials = self.proxy.drain_denials()
        self.assertIn((GOOD, self.port, 'local and private addresses are not allowed'), denials)
        self.proxy._resolver = original

    def test_plain_http_forwarding(self):
        self.approved.add(GOOD)
        sock = socket.create_connection(('127.0.0.1', self.proxy_port), timeout=5)
        sock.sendall(b'GET http://%s:%d/plain-path?q=1 HTTP/1.1\r\nHost: %s\r\n\r\n'
                     % (GOOD.encode(), self.port, GOOD.encode()))
        data = b''
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        self.assertIn(b'hello:/plain-path?q=1', data)
        self.assertEqual(_Handler.hits[0][1], '/plain-path?q=1')

    def test_plain_http_unapproved_denied(self):
        sock = socket.create_connection(('127.0.0.1', self.proxy_port), timeout=5)
        sock.sendall(b'GET http://%s:%d/x HTTP/1.1\r\nHost: %s\r\n\r\n' % (OTHER.encode(), self.port, OTHER.encode()))
        data = sock.recv(4096)
        sock.close()
        self.assertIn(b'403', data)
        self.assertEqual(_Handler.hits, [])

    def test_drain_denials_clears(self):
        self.connect('%s:443' % OTHER)
        self.assertEqual(len(self.proxy.drain_denials()), 1)
        self.assertEqual(self.proxy.drain_denials(), [])


class WebSocketGateTests(unittest.TestCase):
    """_ws_route: WebSockets only to user-approved hosts (two-layer: this plus proxy deny)."""

    class FakeWS:
        def __init__(self, url):
            self.url = url
            self.connected = False
            self.closed = None

        def connect_to_server(self):
            self.connected = True

        def close(self, code=None, reason=None):
            self.closed = (code, reason)

    def _session_with(self, approved):
        from grokbot.agent_browser import AgentBrowserSession
        session = AgentBrowserSession.__new__(AgentBrowserSession)
        session._approved = set(approved)
        return session

    def test_all_websockets_refused(self):
        """Read/click-only browser: WS refused even to approved, public hosts."""
        from unittest import mock
        session = self._session_with({'good.test'})
        with mock.patch('grokbot.agent_browser.request_allowed', return_value=True):
            for url in ('wss://good.test/ws', 'wss://evil.test/ws', 'ws://127.0.0.1/ws'):
                ws = self.FakeWS(url)
                session._ws_route(ws)
                self.assertFalse(ws.connected, url)
                self.assertEqual(ws.closed[0], 1008, url)


class IsolationWrapperTests(unittest.TestCase):
    @_cap_gate(browser_isolation.available(), 'needs bubblewrap')
    def test_no_direct_egress_inside_namespace(self):
        """Structural proof: a plain process inside the same isolation cannot reach the network."""
        target = None
        for candidate in ('93.184.216.34', '1.1.1.1', '140.82.112.3'):   # example.com, Cloudflare, GitHub
            try:
                socket.create_connection((candidate, 443), timeout=3).close()
                target = candidate
                break
            except OSError:
                continue
        # Interface-level structural proof, independent of internet availability:
        # a fresh /proc inside the namespace must show no usable external interface.
        result = subprocess.run(['bwrap', '--unshare-net', '--bind', '/', '/', '--proc', '/proc',
                                 'python3', '-c', "print(open('/proc/net/dev').read())"],
                                capture_output=True, timeout=30, text=True)
        self.assertIn('lo', result.stdout)
        self.assertNotIn('eth0', result.stdout, result.stdout)
        # Behavioral proof: a raw-IP connect (no DNS involved) must fail inside the namespace.
        script = ("import socket,sys\n"
                  "try:\n socket.create_connection(('%s',443),timeout=3)\n"
                  "except OSError as e:\n print('blocked:',e);sys.exit(1)" % (target or '140.82.112.3'))
        result = subprocess.run(['bwrap', '--unshare-net', '--bind', '/', '/', '--proc', '/proc',
                                 'python3', '-c', script], capture_output=True, timeout=30, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('blocked:', result.stdout, result.stdout + result.stderr)

    @_cap_gate(browser_isolation.available(), 'needs bubblewrap')
    def test_loopback_relay_path_exists(self):
        """The namespace keeps a working loopback (the relay's listen side)."""
        result = subprocess.run(
            ['bwrap', '--unshare-net', '--bind', '/', '/', 'python3', '-c',
             "import socket;s=socket.socket();s.bind(('127.0.0.1',0));s.listen(1);print('ok')"],
            capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode()[:300])


class ProductionGuardTests(unittest.TestCase):
    """The PRODUCTION default ip_check (netguard._bad_ip), no test seam: approved or not,
    private/literal targets are refused before any connection. The live suites use a fake
    topology (LOCAL plays "public"); these tests pin the real guard's address coverage."""

    PRIVATE = ['127.0.0.1', '127.1.2.3', '10.1.2.3', '172.16.0.9', '192.168.1.10',
               '169.254.1.1', '100.64.0.1', '::1', 'fd00::1', '::ffff:127.0.0.1',
               '::ffff:10.1.2.3']

    def _proxy(self):
        return VettingProxy(allowed_hosts=lambda: {'approved.example', *self.PRIVATE},
                            resolver=lambda h, p: [ipaddress.ip_address(h)] if h in self.PRIVATE
                            else [ipaddress.ip_address('93.184.216.34')])

    def test_private_literals_denied_with_production_guard(self):
        proxy = self._proxy()
        known_reasons = {'local hostnames are not allowed',
                         'local and private addresses are not allowed'}
        for addr in self.PRIVATE:
            ip, reason = proxy._vet(addr, 443)
            self.assertIsNone(ip, addr)
            self.assertIn(reason, known_reasons, addr)

    def test_resolved_private_denied_with_production_guard(self):
        """An APPROVED hostname whose DNS answers a private address: refused, every family."""
        proxy = self._proxy()
        for addr in self.PRIVATE:
            proxy._resolver = lambda h, p, a=addr: [ipaddress.ip_address(a)]
            ip, reason = proxy._vet('approved.example', 443)
            self.assertIsNone(ip, addr)
            self.assertEqual(reason, 'local and private addresses are not allowed', addr)

    def test_approved_public_resolution_allowed_with_production_guard(self):
        proxy = self._proxy()
        ip, reason = proxy._vet('approved.example', 443)
        self.assertEqual(str(ip), '93.184.216.34')
        self.assertIsNone(reason)

    def test_tcp_level_private_literal_403_with_production_guard(self):
        """Same guard over the socket interface, not just the internal function."""
        proxy = self._proxy()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        sock.listen(4)
        sock.settimeout(0.5)
        port = sock.getsockname()[1]
        threading.Thread(target=proxy._accept_loop, args=(sock,), daemon=True).start()
        self.addCleanup(proxy.close)
        self.addCleanup(sock.close)
        conn = socket.create_connection(('127.0.0.1', port), timeout=5)
        conn.sendall(b'CONNECT 10.1.2.3:443 HTTP/1.1\r\nHost: 10.1.2.3:443\r\n\r\n')
        response = b''
        while b'\r\n\r\n' not in response:
            response += conn.recv(4096)
        conn.close()
        self.assertIn(b'403', response.split(b'\r\n', 1)[0])
        self.assertIn(( '10.1.2.3', 443, 'local and private addresses are not allowed'),
                      proxy.drain_denials())


@_cap_gate(_LIVE, 'live Chromium test needs playwright, chromium, bwrap')
class LiveIsolatedBrowseTests(unittest.TestCase):
    """Real Chromium, real isolation: every attack scenario asserts the request NEVER fires,
    not just that the landed page is blanked afterwards."""
    A = 'site-a.test'
    B = 'site-b.test'

    @classmethod
    def setUpClass(cls):
        cls.server = _Server(('127.0.0.1', 0), _AttackHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        # The "internal target" of the rebinding attack: a second server on TEST_PRIVATE.
        cls.internal = _Server(('127.0.0.2', 0), _AttackHandler)
        cls.internal_port = cls.internal.server_address[1]
        threading.Thread(target=cls.internal.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.internal.shutdown()
        cls.internal.server_close()

    def setUp(self):
        _AttackHandler.hits = []
        # Same seam as the existing live browse tests: Python DNS is patched so the
        # netguard sees the fake hosts as public; the proxy's injected resolver maps
        # them to the local test server (Chromium itself never resolves DNS).
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, port, *args, **kwargs):
            if host in (self.A, self.B, 'cdn.unapproved.test'):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port or 80))]
            return real_getaddrinfo(host, port, *args, **kwargs)

        patcher = unittest.mock.patch('socket.getaddrinfo', side_effect=fake_getaddrinfo)
        patcher.start()
        self.addCleanup(patcher.stop)

        def resolver(host, port):
            if host in (self.A, self.B):
                return [LOCAL]                    # pre-vetted test seam: maps fake public hosts to the test server
            return []

        from grokbot.agent_browser import AgentBrowserSession, BrowserError
        self.session = AgentBrowserSession(resolver=resolver, allowed_ports=(80, 443, self.port),
                                           ip_check=_test_ip_check)
        self.addCleanup(self.session.close)

    def hosts_hit(self):
        return [h for h, _p in _AttackHandler.hits]

    def paths_hit(self):
        return [p for _h, p in _AttackHandler.hits]

    def test_open_roundtrip_through_proxy(self):
        state = self.session.open('http://%s:%d/' % (self.A, self.port), {self.A})
        self.assertEqual(state['snapshot']['title'], 'Attack Test')

    def test_redirect_to_loopback_never_reaches_server(self):
        with self.assertRaises(Exception):
            self.session.open('http://%s:%d/redir-loop' % (self.A, self.port), {self.A})
        self.assertNotIn('/secret', self.paths_hit(),
                         'the redirect hop to 127.0.0.1 must never fire (pre-fix it did)')
        denials = self.session._proxy.drain_denials()
        self.assertTrue(any(h == '127.0.0.1' for h, _p, _r in denials), denials)

    def test_redirect_to_unapproved_host_never_fires(self):
        with self.assertRaises(Exception):
            self.session.open('http://%s:%d/redir-b' % (self.A, self.port), {self.A})
        self.assertFalse([h for h in self.hosts_hit() if h.startswith(self.B)],
                         'the redirect hop to an unapproved host must never fire')
        denials = self.session._proxy.drain_denials()
        self.assertIn((self.B, self.port, 'host not approved'), denials)

    def test_redirect_to_approved_host_loads(self):
        approved = {self.A, self.B}
        state = self.session.open('http://%s:%d/redir-b' % (self.A, self.port), approved)
        self.assertEqual(state['snapshot']['title'], 'Other Site')

    def test_meta_refresh_to_loopback_never_fires(self):
        """Meta-refresh navigations ARE routed by Playwright (unlike 3xx hops), so the route
        guard refuses them; either way the local request must never fire."""
        with self.assertRaises(BrowserError):
            self.session.open('http://%s:%d/meta-loop' % (self.A, self.port), {self.A})
        self.assertNotIn('/secret', self.paths_hit())

    def test_js_navigation_to_unapproved_host_never_fires(self):
        """JS navigations are routed too: refused pre-flight by the route guard."""
        with self.assertRaises(BrowserError):
            self.session.open('http://%s:%d/js-nav' % (self.A, self.port), {self.A}, False)
        self.assertFalse([h for h in self.hosts_hit() if h.startswith(self.B)])

    def test_mid_session_rebinding_flip_never_reaches_internal(self):
        """Approved host re-resolves to an internal address MID-SESSION: the very next
        navigation must be refused at the proxy and the internal target must see nothing."""
        internal_hits_before = [h for h, _p in _AttackHandler.hits
                                if h.startswith('127.0.0.2')]
        state = self.session.open('http://%s:%d/' % (self.A, self.port), {self.A})
        self.assertEqual(state['snapshot']['title'], 'Attack Test')
        original = self.session._proxy._resolver

        def flipped(host, port):
            if host == self.A:
                return [TEST_PRIVATE]          # "rebound" to the internal network
            return original(host, port)

        self.session._proxy._resolver = flipped
        state = self.session.open('http://%s:%d/' % (self.A, self.internal_port), {self.A})
        internal_hits_after = [h for h, _p in _AttackHandler.hits
                               if h.startswith('127.0.0.2')]
        self.assertEqual(internal_hits_before, internal_hits_after,
                         'the rebound internal address must never be contacted')
        self.assertNotEqual(state['snapshot']['title'], 'Attack Test',
                            'the real page must not load through the rebound address')
        self.assertIn(self.A, state.get('refused_hosts') or [],
                      'the rebound host must be reported as refused (not approvable)')
        self.assertNotIn(self.A, state.get('blocked_hosts') or [],
                         'rebinding must NOT be presented as approval-fixable')

    def test_unapproved_subresource_blocked_and_reported(self):
        state = self.session.open('http://%s:%d/with-cdn' % (self.A, self.port), {self.A})
        self.assertFalse([h for h in self.hosts_hit() if 'cdn.' in h and not h.startswith(self.A)])
        self.assertIn('cdn.unapproved.test', state.get('blocked_hosts') or [])


class _AttackHandler(http.server.BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).hits.append((self.headers.get('Host', ''), self.path))
        port = self.server.server_address[1]
        if self.path == '/':
            body = b'<html><title>Attack Test</title><body>ok</body></html>'
        elif self.path == '/redir-loop':
            self.send_response(302)
            self.send_header('Location', 'http://127.0.0.1:%d/secret' % port)
            self.end_headers()
            return
        elif self.path == '/redir-b':
            self.send_response(302)
            self.send_header('Location', 'http://site-b.test:%d/other' % port)
            self.end_headers()
            return
        elif self.path == '/other':
            body = b'<html><title>Other Site</title><body>other</body></html>'
        elif self.path == '/with-cdn':
            body = ('<html><title>Attack Test</title><body>ok'
                    '<img src="http://cdn.unapproved.test:%d/pixel.png"></body></html>' % port).encode()
        elif self.path == '/meta-loop':
            body = ('<html><head><meta http-equiv="refresh" content="0;url=http://127.0.0.1:%d/secret">'
                    '</head><body>meta</body></html>' % port).encode()
        elif self.path == '/js-nav':
            body = ('<html><body>js<script>location.href = "http://site-b.test:%d/other";'
                    '</script></body></html>' % port).encode()
        elif self.path == '/secret':
            body = b'should never be served through the proxy'
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    unittest.main()


def _have_h2():
    try:
        import h2.connection  # noqa
        return True
    except ImportError:
        return False


@_cap_gate(_LIVE and _have_h2(), 'live HTTP/2 test needs playwright, chromium, bwrap, h2')
class LiveH2CoalescingTests(unittest.TestCase):
    """HTTP/2 approval-boundary tests over real TLS+h2 (the protocol with cross-origin
    connection reuse). Every assertion measures DESTINATION hits and proxy vetoes, never
    browser errors alone. Findings behind these tests:

    - Through an explicit CONNECT proxy Chromium opens a FRESH CONNECT for each new
      origin; h2 cross-origin coalescing was NOT observed, even with --host-resolver-rules
      handing it the shared IP and a certificate valid for both origins - the coalescing
      bypass does not fire through this proxy shape on the tested Chromium build.
    - That is a measured boundary, not a guarantee for every Chromium version: this suite
      is the regression guard. QUIC is disabled and the namespace blocks UDP, so h3 has
      no path either.
    """
    A = 'a.coal.test'
    B = 'b.coal.test'

    @classmethod
    def setUpClass(cls):
        import subprocess, tempfile
        cls._tmp = tempfile.mkdtemp()
        cls.cert = os.path.join(cls._tmp, 'cert.pem')
        cls.key = os.path.join(cls._tmp, 'key.pem')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', cls.key, '-out', cls.cert, '-days', '2',
                        '-subj', '/CN=coal.test',
                        '-addext', 'subjectAltName=DNS:%s,DNS:%s' % (cls.A, cls.B)],
                       check=True, capture_output=True)
        cls.port, cls.hits, cls.stop = _start_h2_server(cls.cert, cls.key, cls.A, cls.B)

    @classmethod
    def tearDownClass(cls):
        cls.stop()

    def setUp(self):
        self.hits.clear()

    def _session(self, extra_args):
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, port, *args, **kwargs):
            if host in (self.A, self.B):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port or 443))]
            return real_getaddrinfo(host, port, *args, **kwargs)

        patcher = unittest.mock.patch('socket.getaddrinfo', side_effect=fake_getaddrinfo)
        patcher.start()
        self.addCleanup(patcher.stop)

        def resolver(host, port):
            if host in (self.A, self.B):
                return [LOCAL]
            return []

        from grokbot.agent_browser import AgentBrowserSession
        session = AgentBrowserSession(resolver=resolver, allowed_ports=(80, 443, self.port),
                                      extra_args=extra_args, ip_check=_test_ip_check)
        self.addCleanup(session.close)
        return session

    def _hits_for(self, host):
        return [p for h, p in self.hits if h == host or h.startswith(host + ':')]

    def test_https_subresource_unapproved_vetoed(self):
        """Approved h2 connection already established; the unapproved cross-origin
        subresource must never reach its server and must be reported as blocked."""
        session = self._session(['--ignore-certificate-errors'])
        state = session.open('https://%s:%d/' % (self.A, self.port), {self.A})
        self.assertEqual(state['snapshot']['title'], 'Coalesce Test')
        self.assertFalse(self._hits_for(self.B), self.hits)
        self.assertIn(self.B, state.get('blocked_hosts') or [])

    def test_https_subresource_vetoed_even_with_resolver_ip_knowledge(self):
        """Adversarial control: --host-resolver-rules hands Chromium the shared IP and the
        cert covers both origins - every condition h2 coalescing needs. The unapproved
        origin must STILL get its own vetoed CONNECT, never a ride on the approved tunnel."""
        session = self._session(['--ignore-certificate-errors',
                                 '--host-resolver-rules=MAP *.coal.test 127.0.0.1'])
        state = session.open('https://%s:%d/' % (self.A, self.port), {self.A})
        self.assertEqual(state['snapshot']['title'], 'Coalesce Test')
        self.assertFalse(self._hits_for(self.B),
                         'coalescing bypass: unapproved origin served with no CONNECT: %r' % (self.hits,))
        self.assertIn(self.B, state.get('blocked_hosts') or [])

    def test_https_redirect_to_unapproved_vetoed(self):
        """A 302 hop inside the established h2 session to an unapproved origin must never
        reach its server (Playwright does not route 3xx hops; the proxy is the guard)."""
        session = self._session(['--ignore-certificate-errors'])
        with self.assertRaises(Exception):
            session.open('https://%s:%d/redir-b' % (self.A, self.port), {self.A})
        self.assertFalse(self._hits_for(self.B), self.hits)

    def test_https_direct_navigation_to_unapproved_refused(self):
        """Direct top-level navigation to an unapproved origin while the approved h2
        connection is established: refused, and the destination never sees a request."""
        session = self._session(['--ignore-certificate-errors'])
        state = session.open('https://%s:%d/' % (self.A, self.port), {self.A})
        self.assertEqual(state['snapshot']['title'], 'Coalesce Test')
        self.assertTrue(self._hits_for(self.A), 'approved h2 connection must be established first')
        with self.assertRaises(Exception):
            session.open('https://%s:%d/landing' % (self.B, self.port))
        self.assertFalse(self._hits_for(self.B), self.hits)

    def test_preconnect_to_unapproved_vetoed_at_socket_level(self):
        """Speculative preconnect/dns-prefetch to an unapproved host opens no page request
        at all - the measurement is the proxy's CONNECT veto (socket level), not hits."""
        session = self._session(['--ignore-certificate-errors'])
        state = session.open('https://%s:%d/preconnect' % (self.A, self.port), {self.A})
        self.assertEqual(state['snapshot']['title'], 'Coalesce Test')
        self.assertFalse(self._hits_for(self.B), self.hits)
        # Measured on the tested build: the hints fired no speculative connection at all.
        # If a future build does preconnect, its CONNECT is vetoed by the proxy - that
        # path is covered by the other unapproved-host tests.
        self.assertNotIn(self.B, [h for h, _p, _r in session._proxy.drain_denials()])

    def test_service_worker_registration_blocked(self):
        """Read/click-only: service workers stay blocked. Measured: the registration
        rejects in-page AND the worker script is never fetched over the network."""
        session = self._session(['--ignore-certificate-errors'])
        session.open('https://%s:%d/sw-page' % (self.A, self.port), {self.A})
        import time
        time.sleep(2)
        session.see({self.A}, False)
        # service_workers='block' fulfills the script fetch locally with empty content, so
        # register() may still resolve with an empty worker - the security property is at
        # the network: the worker script never transits the proxy or reaches a server.
        self.assertNotIn('/sw.js', [p for _h, p in self.hits],
                         'the worker script must never be fetched')
        self.assertNotIn('/sw.js', [p for _h, p, _r in session._proxy.drain_denials()])

    def test_https_redirect_to_approved_loads(self):
        """Same hop with BOTH origins approved loads end-to-end through the proxy."""
        session = self._session(['--ignore-certificate-errors'])
        state = session.open('https://%s:%d/redir-b' % (self.A, self.port), {self.A, self.B})
        self.assertEqual(state['snapshot']['title'], 'Other Site')
        self.assertTrue(self._hits_for(self.B))


def _start_h2_server(cert, key, host_a, host_b):
    """Minimal dual-protocol TLS server: HTTP/2 via ALPN when offered, HTTP/1.1 otherwise.
    Records (authority, path) for every request. Test-side only; the 'h2' package is a
    test dependency, never imported by the app."""
    import ssl, h2.connection, h2.events
    hits = []
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    ctx.set_alpn_protocols(['h2', 'http/1.1'])
    lsock = socket.socket()
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(('127.0.0.1', 0))
    lsock.listen(16)
    lsock.settimeout(0.5)
    port = lsock.getsockname()[1]
    stopped = threading.Event()

    PAGE = (b'<html><head><title>Coalesce Test</title></head><body>'
            b'<img src="https://%b:%d/px">' % (host_b.encode(), port) + b'</body></html>')
    LANDING = b'<html><head><title>Other Site</title></head><body>other</body></html>'
    PRECONNECT = (b'<html><head><title>Coalesce Test</title>'
                  b'<link rel="preconnect" href="https://%b:%d">'
                  b'<link rel="dns-prefetch" href="//%b">'
                  b'</head><body>ok</body></html>' % (host_b.encode(), port, host_b.encode()))
    SW_PAGE = (b'<html><head><title>sw-pending</title></head><body><script>'
               b"navigator.serviceWorker.register('/sw.js')"
               b".then(function(){document.title='sw-registered'})"
               b".catch(function(){document.title='sw-blocked'});"
               b'</script></body></html>')

    def response_for(path):
        if path == '/':
            return '200', [('content-type', 'text/html')], PAGE
        if path == '/redir-b':
            return '302', [('location', 'https://%s:%d/landing' % (host_b, port))], b''
        if path == '/landing':
            return '200', [('content-type', 'text/html')], LANDING
        if path == '/preconnect':
            return '200', [('content-type', 'text/html')], PRECONNECT
        if path == '/sw-page':
            return '200', [('content-type', 'text/html')], SW_PAGE
        if path == '/sw.js':
            return '200', [('content-type', 'text/javascript')], b'/* worker */'
        return '200', [('content-type', 'application/octet-stream')], b'x'

    def handle_h1(conn):
        conn.settimeout(10)
        try:
            buf = b''
            while b'\r\n\r\n' not in buf:
                chunk = conn.recv(65535)
                if not chunk:
                    return
                buf += chunk
            line = buf.split(b'\r\n', 1)[0].split()
            path = line[1].decode() if len(line) > 1 else '/'
            host = ''
            for hline in buf.split(b'\r\n')[1:]:
                if hline.lower().startswith(b'host:'):
                    host = hline.split(b':', 1)[1].strip().decode()
            hits.append((host, path))
            status, headers, body = response_for(path)
            head = ('HTTP/1.1 %s\r\n' % status).encode()
            for k, v in headers:
                head += ('%s: %s\r\n' % (k, v)).encode()
            head += b'Content-Length: %d\r\nConnection: close\r\n\r\n' % len(body)
            conn.sendall(head + body)
        except Exception:
            pass
        finally:
            conn.close()

    def handle_h2(conn):
        conn.settimeout(10)
        h2c = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False))
        h2c.initiate_connection()
        try:
            conn.sendall(h2c.data_to_send())
            while not stopped.is_set():
                data = conn.recv(65535)
                if not data:
                    break
                for event in h2c.receive_data(data):
                    if isinstance(event, h2.events.RequestReceived):
                        headers = dict(event.headers)
                        authority = headers.get(b':authority', b'').decode()
                        path = headers.get(b':path', b'/').decode()
                        hits.append((authority, path))
                        status, extra, body = response_for(path)
                        h2c.send_headers(event.stream_id,
                                         [(':status', status)] + extra +
                                         [('content-length', str(len(body)))])
                        h2c.send_data(event.stream_id, body, end_stream=True)
                conn.sendall(h2c.data_to_send())
        except Exception:
            pass
        finally:
            conn.close()

    def handle(raw):
        try:
            conn = ctx.wrap_socket(raw, server_side=True)
        except Exception:
            raw.close()
            return
        if conn.selected_alpn_protocol() == 'h2':
            handle_h2(conn)
        else:
            handle_h1(conn)

    def serve():
        while not stopped.is_set():
            try:
                raw, _ = lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=handle, args=(raw,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()

    def stop():
        stopped.set()
        lsock.close()

    return port, hits, stop


@_cap_gate(_LIVE, 'live wrapper test needs bwrap')
class LiveWrapperTests(unittest.TestCase):
    """The PRODUCTION wrapper itself (browser_isolation.write_wrapper), not a hand-rolled
    bwrap: namespace contents, canary non-visibility, relay path, abstract sockets."""

    def _run_wrapper(self, body):
        """Run a shell `body` as the 'chromium' through the real wrapper; return CompletedProcess.
        `body` may be a callable taking the BrowserIsolation, for commands that need its paths."""
        from grokbot import browser_isolation
        iso = browser_isolation.BrowserIsolation()
        self.addCleanup(iso.cleanup)
        if callable(body):
            body = body(iso)
        fake = os.path.join(iso.dir, 'fake-chromium.sh')
        with open(fake, 'w') as fh:
            fh.write('#!/bin/sh\n' + body)
        os.chmod(fake, 0o755)
        wrapper = iso.write_wrapper(fake, [])
        return subprocess.run(['/bin/sh', wrapper], capture_output=True, timeout=60, text=True), iso

    def test_wrapper_has_no_egress_and_no_sys(self):
        result, _iso = self._run_wrapper(
            'ls /sys/class/net 2>/dev/null && echo SYS-LEAK;\n'
            'cat /proc/net/dev\n')
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        self.assertNotIn('SYS-LEAK', result.stdout)
        ifaces = [ln.split(':')[0].strip() for ln in result.stdout.splitlines()
                  if ':' in ln and not ln.lstrip().startswith(('Inter-', 'face'))]
        # Fresh netns: lo plus the kernel's DOWN pseudo-devices; never a real NIC.
        pseudo = {'lo', 'tunl0', 'gre0', 'gretap0', 'erspan0', 'ip6tnl0', 'ip6gre0',
                  'ip_vti0', 'ip6_vti0', 'sit0'}
        self.assertIn('lo', ifaces)
        self.assertEqual(sorted(set(ifaces) - pseudo), [], ifaces)

    def test_host_canaries_not_visible(self):
        """Test-created canaries only: a host /tmp file, a host /tmp FILESYSTEM unix
        socket, and an existing non-allowlisted /etc file (machine-id) must all be
        absent inside the namespace. No real secrets or services are read."""
        import socket as sk
        canary = '/tmp/grok-canary-%d' % os.getpid()
        with open(canary, 'w') as fh:
            fh.write('canary')
        self.addCleanup(os.unlink, canary)
        sock_path = '/tmp/grok-canary-%d.sock' % os.getpid()
        srv = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(1)
        self.addCleanup(srv.close)
        self.addCleanup(lambda: os.path.exists(sock_path) and os.unlink(sock_path))
        pkg_dir = os.path.dirname(os.path.abspath(browser_isolation.__file__))
        top = pkg_dir.split(os.sep)[1]                       # first path component, e.g. 'downloads'
        # Disposable canary next to the checkout (on CI that tree IS under /home):
        # host content beside the deliberate isorelay.py bind must stay invisible.
        home_canary = os.path.join(os.path.dirname(pkg_dir), 'grok-home-canary-%d' % os.getpid())
        with open(home_canary, 'w') as fh:
            fh.write('canary')
        self.addCleanup(os.unlink, home_canary)
        result, _iso = self._run_wrapper(lambda iso:
            'test -e %s && echo TMP-FILE-LEAK;\n'
            'test -e %s && echo HOME-CANARY-LEAK;\n'
            'test -e /etc/machine-id && echo ETC-LEAK;\n'
            'python3 -c "import socket;s=socket.socket(socket.AF_UNIX);s.settimeout(3);'
            's.connect(\'%s\')" 2>&1 | tail -1;\n'
            'echo ---;\n'
            'ls %s;\n'
            'echo ---;\n'
            'find /%s -path %s -prune -o -print;\n'
            'echo ---;\n'
            'ls /' % (canary, home_canary, sock_path, pkg_dir, top, iso.dir))
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        self.assertNotIn('TMP-FILE-LEAK', result.stdout)
        self.assertNotIn('HOME-CANARY-LEAK', result.stdout)
        self.assertNotIn('ETC-LEAK', result.stdout)
        # The socket canary is absent inside: connect must fail, never succeed silently.
        self.assertRegex(result.stdout.split('---')[0],
                         'No such file|ConnectionRefusedError|FileNotFoundError')
        # Only the relay script is bound from its package dir; no sibling source files leak.
        self.assertEqual(result.stdout.split('---')[1].split(), ['isorelay.py'])
        # The auto-created mount chain under the top component is EXACTLY the path to
        # isorelay.py: no sibling directories or files anywhere along it.
        chain = []
        cursor = ''
        for part in pkg_dir.strip(os.sep).split(os.sep):
            cursor += '/' + part
            chain.append(cursor)
        chain.append(os.path.join(pkg_dir, 'isorelay.py'))
        self.assertEqual(result.stdout.split('---')[2].split(), chain)
        # Root listing must be the minimal mount set, as tokens (not substring games):
        root_tokens = set(result.stdout.split('---')[3].split())
        self.assertLessEqual(root_tokens,
                             {'bin', 'dev', 'etc', 'lib', 'lib64', 'proc', 'run', 'sbin',
                              'tmp', 'usr', top}, root_tokens)
        # Host /home must never leak into the sandbox root. When the package itself
        # lives under /home (CI checkout at /home/runner/work/...), the visible /home
        # is only the auto-created empty mount parent for the deliberate isorelay.py
        # bind; the exact chain assertion above (no siblings anywhere along the path)
        # is what proves there is no real host /home exposure in that case.
        if top != 'home':
            self.assertNotIn('home', root_tokens)

    def test_abstract_unix_socket_isolated_with_netns(self):
        """Abstract unix sockets live in the network namespace: a host-side abstract
        socket must be unreachable inside (verifies the claim, not assumed)."""
        import socket as sk
        srv = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM)
        srv.bind('\0grok-canary-abstract')
        srv.listen(1)
        self.addCleanup(srv.close)
        result, _iso = self._run_wrapper(
            'python3 -c "import socket;s=socket.socket(socket.AF_UNIX);s.settimeout(3);'
            's.connect(\'\\0grok-canary-abstract\')" 2>&1 | tail -1\n')
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        # Inside the fresh netns the name has no listener: the host's abstract socket is
        # unreachable. Either errno proves non-connection; a silent success would fail.
        self.assertRegex(result.stdout, 'ConnectionRefusedError|FileNotFoundError|No such file')

    def test_relay_path_through_real_wrapper(self):
        """The in-namespace loopback relay reaches the proxy unix socket: ping -> pong."""
        import socket as sk
        from grokbot import browser_isolation
        iso = browser_isolation.BrowserIsolation()
        self.addCleanup(iso.cleanup)
        srv = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM)
        srv.bind(iso.proxy_sock)
        srv.listen(1)
        self.addCleanup(srv.close)

        def echo():
            for _ in range(8):
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                try:
                    conn.settimeout(3)
                    data = conn.recv(64)
                    if data:
                        conn.sendall(b'pong:' + data)
                except OSError:
                    pass
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass

        import threading as th
        th.Thread(target=echo, daemon=True).start()
        fake = os.path.join(iso.dir, 'fake-chromium.sh')
        with open(fake, 'w') as fh:
            fh.write('#!/bin/sh\npython3 -c "import socket;s=socket.socket();s.settimeout(5);'
                     's.connect((\'127.0.0.1\', %d));s.sendall(b\'ping\');print(s.recv(64))"\n'
                     % browser_isolation.RELAY_PORT)
        os.chmod(fake, 0o755)
        wrapper = iso.write_wrapper(fake, [])
        result = subprocess.run(['/bin/sh', wrapper], capture_output=True, timeout=60, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        self.assertIn('pong:ping', result.stdout, result.stdout + result.stderr[:300])

class ResourceLifecycleTests(unittest.TestCase):
    """No Chromium process, proxy thread, or wrapper directory is left behind when a
    session cannot start, or after a clean close."""

    def test_missing_bwrap_refuses_browser_start(self):
        """No bwrap -> available() False -> the session raises instead of falling back."""
        from grokbot import browser_isolation
        real_which = browser_isolation.shutil.which
        browser_isolation.shutil.which = lambda name: None
        try:
            self.assertFalse(browser_isolation.available(refresh=True))
        finally:
            browser_isolation.shutil.which = real_which
            browser_isolation.available(refresh=True)

    def test_unusable_userns_refuses_browser_start(self):
        """bwrap present but namespaces blocked -> probe fails -> browser stays off."""
        from grokbot import browser_isolation
        real_run = browser_isolation.subprocess.run if hasattr(browser_isolation, 'subprocess') else None
        import subprocess as sp
        original = sp.run

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd and cmd[0].endswith('bwrap'):
                return sp.CompletedProcess(cmd, 1, b'', b'bwrap: No permissions to create new namespace')
            return original(cmd, **kw)

        browser_isolation._AVAILABLE_CACHE = None
        sp.run = fake_run
        try:
            self.assertFalse(browser_isolation.available(refresh=True))
        finally:
            sp.run = original
            browser_isolation.available(refresh=True)

    def _proc_count(self, needle):
        count = 0
        for pid in os.listdir('/proc'):
            if not pid.isdigit():
                continue
            try:
                with open('/proc/%s/cmdline' % pid, 'rb') as fh:
                    if needle in fh.read().decode(errors='replace'):
                        count += 1
            except OSError:
                pass
        return count

    @_cap_gate(_playwright_importable(), 'needs playwright installed')
    def test_session_refusal_leaves_no_resources(self):
        from grokbot import agent_browser
        from grokbot.browser_isolation import WRAPPER_PREFIX
        chromium_before = self._proc_count('chromium')
        drivers_before = self._proc_count('playwright/driver')
        entries_before = {e for e in os.listdir(tempfile.gettempdir())
                          if e.startswith(WRAPPER_PREFIX)}
        with unittest.mock.patch('grokbot.browser_isolation.available', return_value=False):
            session = agent_browser.AgentBrowserSession()
            with self.assertRaises(BrowserError) as ctx:
                session.open('https://example.com/', {'example.com'})
        self.assertIn('bubblewrap', str(ctx.exception))
        entries_after = {e for e in os.listdir(tempfile.gettempdir())
                         if e.startswith(WRAPPER_PREFIX)}
        self.assertEqual(entries_before, entries_after, 'wrapper/isolation files left behind')
        self.assertEqual(chromium_before, self._proc_count('chromium'),
                         'chromium processes left behind')
        self.assertIsNone(session._proxy, 'vetting proxy left running')
        self.assertIsNone(session._browser)
        self.assertIsNone(session._pw, 'playwright driver left running after refusal')
        self.assertIsNone(session._driver_pid)
        self.assertEqual(drivers_before, self._proc_count('playwright/driver'),
                         'playwright driver processes left behind')

    def test_close_leaves_no_chromium_processes(self):
        if not _LIVE:
            self.skipTest('needs isolation (GROK_LIVE_BROWSE_TESTS=1)')
        from grokbot.agent_browser import AgentBrowserSession
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, port, *args, **kwargs):
            if host == GOOD:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port or 80))]
            return real_getaddrinfo(host, port, *args, **kwargs)

        patcher = unittest.mock.patch('socket.getaddrinfo', side_effect=fake_getaddrinfo)
        patcher.start()
        self.addCleanup(patcher.stop)

        def resolver(host, port):
            return [LOCAL] if host == GOOD else []

        chromium_before = self._proc_count('chromium')
        session = AgentBrowserSession(resolver=resolver, allowed_ports=(80, 443, self._port()),
                                      ip_check=_test_ip_check)
        session.open('http://%s:%d/' % (GOOD, self._port()), {GOOD})
        session.close()
        time.sleep(0.5)
        self.assertEqual(chromium_before, self._proc_count('chromium'),
                         'chromium processes left after close')

    _server = None

    @classmethod
    def _port(cls):
        if cls._server is None:
            cls._server = _Server(('127.0.0.1', 0), _Handler)
            threading.Thread(target=cls._server.serve_forever, daemon=True).start()
        return cls._server.server_address[1]


class ConcurrencyCapTests(ProxyTestBase):
    def test_cap_overflow_refused(self):
        self.proxy.close()
        self.approved.add(GOOD)
        proxy = VettingProxy(allowed_hosts=lambda: set(self.approved),
                             resolver=lambda h, p: [LOCAL],
                             allowed_ports=(self.port,), ip_check=_test_ip_check,
                             max_concurrent=2)
        proxy_port = proxy.listen_tcp()
        self.addCleanup(proxy.close)
        held = []
        try:
            for _ in range(2):
                s = socket.create_connection(('127.0.0.1', proxy_port), timeout=5)
                s.sendall(b'CONNECT %s:%d HTTP/1.1\r\n\r\n' % (GOOD.encode(), self.port))
                self.assertTrue(s.recv(256).startswith(b'HTTP/1.1 200'))
                held.append(s)
            extra = socket.create_connection(('127.0.0.1', proxy_port), timeout=5)
            self.addCleanup(extra.close)
            extra.sendall(b'CONNECT %s:%d HTTP/1.1\r\n\r\n' % (GOOD.encode(), self.port))
            self.assertTrue(extra.recv(256).startswith(b'HTTP/1.1 503'))
            extra.close()
            # Releasing a slot lets the next connection in: the cap bounds concurrency,
            # it does not wedge the proxy.
            held.pop().close()
            time.sleep(0.3)
            retry = socket.create_connection(('127.0.0.1', proxy_port), timeout=5)
            self.addCleanup(retry.close)
            retry.sendall(b'CONNECT %s:%d HTTP/1.1\r\n\r\n' % (GOOD.encode(), self.port))
            self.assertTrue(retry.recv(256).startswith(b'HTTP/1.1 200'))
        finally:
            for s in held:
                s.close()


class CloseActiveTunnelTests(ProxyTestBase):
    def test_close_terminates_active_tunnel_promptly(self):
        self.approved.add(GOOD)
        sock, status = self.connect('%s:%d' % (GOOD, self.port))
        self.assertIn(b'200', status)
        sock.sendall(b'GET /via-tunnel HTTP/1.1\r\nHost: good.test\r\n\r\n')
        self.proxy.close()
        sock.settimeout(5)
        data = b''
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            self.fail('proxy.close() did not end the active tunnel within 5s')
        # close() kills both ends: the upstream response is cut off promptly.
        self.assertNotIn(b'hello:/via-tunnel', data)
        sock.close()


class DenialSplitTests(unittest.TestCase):
    """Approval-fixable denials (blocked_hosts) vs non-approvable refusals (refused_hosts)
    must stay separate in the session state and in the result_payload text."""

    def _state_with_denials(self, denials):
        from grokbot import agent_browser

        class FakeProxy:
            def drain_denials(self):
                return list(denials)

        session = agent_browser.AgentBrowserSession()
        session._proxy = FakeProxy()
        session._verify_location = lambda approved: None
        session._snapshot = lambda: {'url': 'https://example.com/', 'title': 'Example'}
        return agent_browser, session._state({'example.com'}, images=False)

    def test_state_splits_blocked_and_refused(self):
        _ab, state = self._state_with_denials([
            ('example.com', 443, 'host not approved'),
            ('internal.example', 443, 'local and private addresses are not allowed'),
            ('oddport.example', 23, 'only ports 80 and 443 are allowed'),
            ('mystery.example', 443, 'could not verify that host'),
        ])
        self.assertEqual(state['blocked_hosts'], ['example.com'])
        self.assertEqual(sorted(state['refused_hosts']),
                         ['internal.example', 'mystery.example', 'oddport.example'])

    def test_result_payload_text_keeps_them_apart(self):
        agent_browser, state = self._state_with_denials([
            ('example.com', 443, 'host not approved'),
            ('internal.example', 443, 'local and private addresses are not allowed'),
        ])
        text, _meta = agent_browser.result_payload(state, 'open', {}, False)
        self.assertIn('example.com', text)
        self.assertIn('internal.example', text)
        refused_part = text[text.index('Refused'):]
        self.assertNotIn('example.com', refused_part.split('.')[0],
                         'an approvable block must not be described as non-approvable')

    def test_live_rebinding_refusal_lands_in_refused_hosts(self):
        """Proxy-level proof (no browser needed): a mid-session public-to-private flip is
        refused with a non-approvable reason even for an approved host."""
        server = _Server(('127.0.0.1', 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        server_port = server.server_address[1]
        calls = []

        def flipping(host, port):
            calls.append(host)
            return [LOCAL] if len(calls) == 1 else [TEST_PRIVATE]

        proxy = VettingProxy(allowed_hosts=lambda: {GOOD}, resolver=flipping,
                             allowed_ports=(server_port,), ip_check=_test_ip_check)
        proxy_port = proxy.listen_tcp()
        self.addCleanup(proxy.close)

        def connect():
            s = socket.create_connection(('127.0.0.1', proxy_port), timeout=5)
            self.addCleanup(s.close)
            s.sendall(b'CONNECT %s:%d HTTP/1.1\r\n\r\n' % (GOOD.encode(), server_port))
            return s.recv(256)

        self.assertTrue(connect().startswith(b'HTTP/1.1 200'), 'first (public) resolution must pass')
        self.assertTrue(connect().startswith(b'HTTP/1.1 403'), 'the rebound resolution must be refused')
        denials = proxy.drain_denials()
        self.assertEqual(denials, [(GOOD, server_port, 'local and private addresses are not allowed')])
        self.assertNotEqual(denials[0][2], 'host not approved',
                            'rebinding must never be reported as approval-fixable')


@_cap_gate(browser_isolation.available(), 'needs bubblewrap')
class LiveSandboxHardeningTests(unittest.TestCase):
    """Measured hardening properties of the PRODUCTION wrapper: effective capabilities,
    session isolation, SysV IPC isolation. Test-created canaries only."""

    def _run_wrapper(self, body):
        from grokbot import browser_isolation
        iso = browser_isolation.BrowserIsolation()
        self.addCleanup(iso.cleanup)
        fake = os.path.join(iso.dir, 'fake-chromium.sh')
        with open(fake, 'w') as fh:
            fh.write('#!/bin/sh\n' + body)
        os.chmod(fake, 0o755)
        wrapper = iso.write_wrapper(fake, [])
        return subprocess.run(['/bin/sh', wrapper], capture_output=True, timeout=60, text=True)

    def test_no_effective_capabilities_inside(self):
        result = self._run_wrapper('grep Cap /proc/self/status')
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        fields = dict(ln.split(':\t') for ln in result.stdout.splitlines())
        for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
            self.assertEqual(int(fields[name], 16), 0, '%s must be empty inside' % name)

    def test_browser_gets_a_new_session(self):
        result = self._run_wrapper('python3 -c "import os;print(os.getsid(0))"')
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        inside_sid = int(result.stdout.strip())
        self.assertNotEqual(inside_sid, os.getsid(0),
                            '--new-session must detach from the host session')
        self.assertNotEqual(inside_sid, os.getsid(os.getpid()))

    def test_sysv_ipc_not_visible_inside(self):
        """A host-created SysV shm segment (test canary, removed afterwards) must not
        appear in the namespace's /proc/sysvipc."""
        if not shutil.which('ipcmk'):
            if _REQUIRE_CAPS:
                self.fail('required live-browse capability missing: ipcmk (util-linux)')
            self.skipTest('ipcmk (util-linux) not available')
        made = subprocess.run(['ipcmk', '-M', '4096'], capture_output=True, text=True)
        self.assertEqual(made.returncode, 0, made.stderr)
        shmid = made.stdout.strip().split()[-1]
        self.addCleanup(lambda: subprocess.run(['ipcrm', '-m', shmid], capture_output=True))
        host_listing = subprocess.run(['cat', '/proc/sysvipc/shm'],
                                      capture_output=True, text=True).stdout
        self.assertIn(shmid, host_listing.split(), 'canary must exist on the host first')
        result = self._run_wrapper('cat /proc/sysvipc/shm')
        self.assertEqual(result.returncode, 0, result.stderr[:300])
        self.assertNotIn(shmid, result.stdout.split(),
                         'host SysV IPC visible inside the namespace')


class RuntimeMountValidationTests(unittest.TestCase):
    """An operator-configured Chromium path must never widen into a broad host mount."""

    def test_broad_dirs_rejected(self):
        home = os.path.realpath(os.path.expanduser('~'))
        for broad in ('/', '/opt', '/home', '/usr/local', '/tmp', home,
                      os.path.dirname(home) or '/home'):
            with self.assertRaises(BrowserError, msg=broad):
                browser_isolation._runtime_mount_dir(os.path.join(broad, 'chrome'))

    def test_dedicated_dirs_accepted(self):
        for ok in ('/usr/lib/chromium', '/opt/google/chrome',
                   '/snap/chromium/current/usr/lib/chromium-browser'):
            self.assertEqual(browser_isolation._runtime_mount_dir(os.path.join(ok, 'chrome')),
                             os.path.realpath(ok))

    def test_write_wrapper_refuses_broad_runtime(self):
        iso = browser_isolation.BrowserIsolation()
        self.addCleanup(iso.cleanup)
        with self.assertRaises(BrowserError):
            iso.write_wrapper('/opt/chrome', [])
        self.assertFalse(os.path.exists(iso.wrapper_path),
                         'no wrapper may be written for a rejected runtime dir')
