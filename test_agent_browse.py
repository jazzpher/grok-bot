"""Tests for the interactive browse tools (browse_open/see/scroll/click): availability
flags, per-domain approval, action budget, image hand-off, turn-end cleanup, URL guards.
Offline except the clearly marked real-Chromium class, which skips unless
GROK_LIVE_BROWSE_TESTS=1 (needs playwright + chromium)."""
import base64
import http.server
import shutil
import socket
import socketserver
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

from grokbot import agent_browser, config
from grokbot.agent import run_turn, build_system_prompt, BROWSE_RULE
from grokbot.agent_browser import AgentBrowserSession, BrowserError, format_snapshot, host_of
from grokbot.approvals import ApprovalBroker, SandboxPolicy, allow_pattern, pattern_matches
from grokbot.tools import ToolContext, ToolRegistry, default_registry
from grokbot.tools import browse as browse_tool
from agent_loop import Workspace

FAKE_PNG = b'\x89PNG\r\n\x1a\n' + b'fake' * 100


def make_workspace():
    root = tempfile.mkdtemp(prefix='browse-test-')
    return Workspace(root)


def fake_state(url='https://example.com/page', images=True):
    state = {'snapshot': {'title': 'Example', 'url': url, 'scroll_y': 0, 'scroll_height': 2000,
                          'client_height': 800, 'text': 'Hello world ' * 50,
                          'elements': [{'index': 1, 'tag': 'a', 'text': 'More', 'href': 'https://example.com/more'},
                                       {'index': 2, 'tag': 'button', 'text': 'Go'}]},
             'image': (FAKE_PNG, 'image/png') if images else None}
    return state


class HostOfTests(unittest.TestCase):
    def test_host_extraction(self):
        self.assertEqual(host_of('https://Example.COM:8443/x?q=1'), 'example.com')
        self.assertEqual(host_of('http://user:pw@example.com/'), 'example.com')
        self.assertEqual(host_of('not a url'), '')
        self.assertEqual(host_of(''), '')


class ApprovalPatternTests(unittest.TestCase):
    def test_allow_pattern_is_domain_scoped(self):
        self.assertEqual(allow_pattern('browse_open', {'url': 'https://example.com/page'}),
                         'browse_open:example.com')
        self.assertEqual(allow_pattern('browse_open', {'url': ''}), 'browse_open:*')

    def test_pattern_matches_exact_host_only(self):
        self.assertTrue(pattern_matches('browse_open:example.com', 'browse_open',
                                        {'url': 'https://example.com/a'}))
        self.assertFalse(pattern_matches('browse_open:example.com', 'browse_open',
                                         {'url': 'https://www.example.com/a'}))
        self.assertFalse(pattern_matches('browse_open:example.com', 'browse_open',
                                         {'url': 'https://evil-example.com/'}))
        self.assertFalse(pattern_matches('browse_open:example.com', 'browse_open', {'url': 'nope'}))
        self.assertTrue(pattern_matches('browse_open:*', 'browse_open', {'url': 'https://anything.io/'}))
        self.assertFalse(pattern_matches('browse_open:example.com', 'browse_see', {}))


class ToolFlagTests(unittest.TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.addCleanup(shutil.rmtree, self.ws.root, True)
        self.ctx = ToolContext(workspace=self.ws, session_id='t', turn_id='turn1')
        browse_tool._TURN_LOCAL.turn = None
        browse_tool._TURN_LOCAL.count = 0

    def registry(self):
        return default_registry(plugins_too=False)

    def test_off_by_default(self):
        with mock.patch.object(config, 'AGENT_BROWSER_ENABLED', False):
            names = self.registry().names_for(self.ctx)
        for name in ('browse_open', 'browse_see', 'browse_scroll', 'browse_click'):
            self.assertNotIn(name, names)

    def test_available_when_enabled(self):
        with mock.patch.object(config, 'AGENT_BROWSER_ENABLED', True):
            names = self.registry().names_for(self.ctx)
        for name in ('browse_open', 'browse_see', 'browse_scroll', 'browse_click'):
            self.assertIn(name, names)

    def test_prompt_rule_included_only_when_enabled(self):
        reg = self.registry()
        with mock.patch.object(config, 'AGENT_BROWSER_ENABLED', True):
            self.assertIn(BROWSE_RULE, build_system_prompt(self.ctx, reg, SandboxPolicy()))
        with mock.patch.object(config, 'AGENT_BROWSER_ENABLED', False):
            self.assertNotIn(BROWSE_RULE, build_system_prompt(self.ctx, reg, SandboxPolicy()))


def mocked_run(ctx, method, *args):
    """Replacement for browse_tool._run that skips Playwright and spends budget."""
    browse_tool._spend_action(ctx)
    return fake_state(images=config.AGENT_BROWSER_IMAGES), config.AGENT_BROWSER_IMAGES


class ToolBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.addCleanup(shutil.rmtree, self.ws.root, True)
        self.ctx = ToolContext(workspace=self.ws, session_id='t', turn_id='turn1')
        browse_tool._TURN_LOCAL.turn = None
        browse_tool._TURN_LOCAL.count = 0
        self.enabled = mock.patch.object(config, 'AGENT_BROWSER_ENABLED', True)
        self.runner = mock.patch.object(browse_tool, '_run', mocked_run)
        self.enabled.start()
        self.runner.start()
        self.addCleanup(self.enabled.stop)
        self.addCleanup(self.runner.stop)
        self.reg = default_registry(plugins_too=False)

    def test_open_returns_snapshot_and_image(self):
        result = self.reg.execute(self.ctx, 'browse_open', {'url': 'https://example.com/page'})
        self.assertTrue(result.ok, result.output)
        self.assertIn('TITLE: Example', result.output)
        self.assertIn('[1] a "More"', result.output)
        self.assertIn('untrusted data', result.output)
        images = result.meta.get('images')
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0]['data_url'].startswith('data:image/png;base64,'))
        self.assertEqual(base64.b64decode(images[0]['data_url'].split(',', 1)[1]), FAKE_PNG)

    def test_images_off_gives_text_only_fallback(self):
        with mock.patch.object(config, 'AGENT_BROWSER_IMAGES', False):
            result = self.reg.execute(self.ctx, 'browse_open', {'url': 'https://example.com/page'})
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.meta.get('images'), None)
        self.assertIn('GROK_AGENT_BROWSER_IMAGES', result.output)

    def test_domain_approval_asked_once_per_turn(self):
        spec = self.reg.get('browse_open')
        reason = spec.needs_approval(self.ctx, {'url': 'https://example.com/page'})
        self.assertIn('example.com', reason)
        self.reg.execute(self.ctx, 'browse_open', {'url': 'https://example.com/page'})
        self.assertIsNone(spec.needs_approval(self.ctx, {'url': 'https://example.com/other'}))
        # A different domain still asks.
        self.assertIn('other.io', spec.needs_approval(self.ctx, {'url': 'https://other.io/'}))
        # New turn asks again (session set is per-ctx; a fresh turn reuses ctx here, so clear extras).
        self.ctx.extras.clear()
        self.assertIn('example.com', spec.needs_approval(self.ctx, {'url': 'https://example.com/page'}))

    def test_saved_always_rule_skips_prompt(self):
        policy = SandboxPolicy()
        policy.allow.append('browse_open:example.com')
        self.ctx.policy = policy
        self.assertTrue(pattern_matches(policy.allow[0], 'browse_open', {'url': 'https://example.com/x'}))

    def test_denied_domain_is_tool_error(self):
        class DenyBroker(ApprovalBroker):
            def request(self, ctx, tool, arguments, reason):
                return 'deny'
        self.ctx.approvals = DenyBroker()
        result = self.reg.execute(self.ctx, 'browse_open', {'url': 'https://example.com/page'})
        self.assertFalse(result.ok)
        self.assertIn('denied', result.output)

    def test_budget_eight_actions_per_turn(self):
        with mock.patch.object(config, 'AGENT_BROWSER_MAX_ACTIONS', 8):
            for _ in range(8):
                self.assertTrue(self.reg.execute(self.ctx, 'browse_see', {}).ok)
            result = self.reg.execute(self.ctx, 'browse_see', {})
        self.assertFalse(result.ok)
        self.assertIn('budget', result.output)

    def test_budget_resets_on_new_turn(self):
        with mock.patch.object(config, 'AGENT_BROWSER_MAX_ACTIONS', 2):
            self.reg.execute(self.ctx, 'browse_see', {})
            self.reg.execute(self.ctx, 'browse_see', {})
            self.assertFalse(self.reg.execute(self.ctx, 'browse_see', {}).ok)
            self.ctx.turn_id = 'turn2'
            self.assertTrue(self.reg.execute(self.ctx, 'browse_see', {}).ok)

    def test_bad_scroll_direction_rejected_by_schema(self):
        result = self.reg.execute(self.ctx, 'browse_scroll', {'direction': 'sideways'})
        self.assertFalse(result.ok)

    def test_missing_playwright_is_clear_error_not_crash(self):
        self.runner.stop()
        session = mock.Mock()
        session.see.side_effect = BrowserError('Web browsing needs Playwright installed: pip install playwright && playwright install chromium.')
        self.ctx.extras['agent_browser_session'] = session
        result = self.reg.execute(self.ctx, 'browse_see', {})
        self.assertFalse(result.ok)
        self.assertIn('Playwright', result.output)
        self.runner.start()


class TurnEndCleanupTests(unittest.TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.addCleanup(shutil.rmtree, self.ws.root, True)

    def test_turn_end_closes_browse_session(self):
        class DummySession:
            closed = False

            def close(self):
                self.closed = True

        session = DummySession()
        ctx = ToolContext(workspace=self.ws, session_id='t')
        ctx.extras['agent_browser_session'] = session

        def stream(cfg, messages, tools=None, stop=None):
            yield {'type': 'text', 'delta': 'done'}
            yield {'type': 'finish', 'reason': 'stop'}

        run_turn([{'role': 'user', 'content': 'hi'}], ToolRegistry(), ctx,
                 ('key', 'http://x', 'model'), lambda e: None, stream_fn=stream)
        self.assertTrue(session.closed)
        self.assertNotIn('agent_browser_session', ctx.extras)


class FormatSnapshotTests(unittest.TestCase):
    def test_bounded_and_labelled(self):
        snapshot = fake_state()['snapshot']
        snapshot['text'] = 'x' * 50000
        out = format_snapshot(snapshot, text_limit=2000)
        self.assertLess(len(out), 2600)
        self.assertIn('untrusted data', out)
        self.assertIn('truncated', out.lower())
        self.assertIn('[2] button "Go"', out)

    def test_javascript_href_marked(self):
        snapshot = fake_state()['snapshot']
        snapshot['elements'] = [{'index': 1, 'tag': 'a', 'text': 'x', 'href': '(javascript)'}]
        self.assertIn('(javascript)', format_snapshot(snapshot))


# ---------------- live Chromium ----------------

PAGE_INDEX = ('<html><head><title>Browse Test</title></head><body>'
              '<h1>Alpha page</h1>'
              + ''.join('<p>filler paragraph %d with enough text to scroll past the fold</p>' % i for i in range(60))
              + '<a href="/two">Go to page two</a>'
              '<a href="http://site-b.test:{port}/other">Cross domain</a>'
              '<a href="/redir-loop">Sneaky redirect</a>'
              '<button onclick="document.body.insertAdjacentHTML(\'beforeend\',\'<p id=clicked>clicked!</p>\')">Press me</button>'
              '<img src="http://169.254.169.254/x.png">'
              '<script>fetch("http://127.0.0.1:1/").catch(()=>{{}});</script>'
              '</body></html>')
PAGE_TWO = '<html><head><title>Second</title></head><body><h1>Page two loaded</h1></body></html>'
PAGE_OTHER = '<html><head><title>Other site</title></head><body><h1>Beta page</h1></body></html>'


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/':
            body = PAGE_INDEX.format(port=self.server.port).encode()
        elif self.path == '/two':
            body = PAGE_TWO.encode()
        elif self.path == '/other':
            body = PAGE_OTHER.encode()
        elif self.path == '/busy':
            body = b'<html><body>busy<script>setTimeout(() => { while (true) {} }, 2000)</script></body></html>'
        elif self.path == '/redir-loop':
            self.send_response(302)
            self.send_header('Location', 'http://127.0.0.1:%d/secret' % self.server.port)
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@unittest.skipUnless(__import__('os').environ.get('GROK_LIVE_BROWSE_TESTS') == '1',
                     'live Chromium test: set GROK_LIVE_BROWSE_TESTS=1 with playwright + chromium installed')
class LiveChromiumTests(unittest.TestCase):
    """Real Playwright pass against a local server, reached through a fake PUBLIC hostname
    (Chromium --host-resolver-rules maps it to 127.0.0.1; Python DNS is patched to return a
    public IP so the netguard sees a public site). Proves open/see/scroll/click, the
    netguard denials, that a page redirected to loopback is blanked and refused (the hop itself is NOT blocked)
    and the cross-domain approval wall."""

    A = 'site-a.test'
    B = 'site-b.test'

    @classmethod
    def setUpClass(cls):
        cls.server = _Server(('127.0.0.1', 0), _Handler)
        cls.server.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_a = 'http://%s:%d' % (cls.A, cls.server.port)
        cls.base_b = 'http://%s:%d' % (cls.B, cls.server.port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        from grokbot import browser_isolation
        if not browser_isolation.available():
            self.skipTest('isolated browsing needs bubblewrap (bwrap)')
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, port, *args, **kwargs):
            if host in (self.A, self.B):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port or 80))]
            return real_getaddrinfo(host, port, *args, **kwargs)

        patcher = mock.patch('socket.getaddrinfo', side_effect=fake_getaddrinfo)
        patcher.start()
        self.addCleanup(patcher.stop)

        import ipaddress

        def resolver(h, port):
            # Test seam for the vetting proxy: the fake public hosts map to the local test
            # server (pre-vetted). Chromium no longer resolves DNS itself - the proxy does.
            if h in (self.A, self.B):
                return [ipaddress.ip_address('127.0.0.1')]
            return []

        # Same fake topology as test_vetproxy: the local server plays "public",
        # 127.0.0.2 plays the forbidden internal target.
        ip_check = lambda ip: ipaddress.ip_address(ip) == ipaddress.ip_address('127.0.0.2')
        self.session = AgentBrowserSession(resolver=resolver, allowed_ports=(80, 443, self.server.port),
                                           ip_check=ip_check)
        self.addCleanup(self.session.close)

    def test_open_see_scroll_click_roundtrip(self):
        approved = {self.A}
        state = self.session.open(self.base_a + '/', approved)
        snap = state['snapshot']
        self.assertEqual(snap['title'], 'Browse Test')
        self.assertIn('Alpha page', snap['text'])
        image, mime = state['image']
        self.assertTrue(image.startswith(b'\x89PNG') or image.startswith(b'\xff\xd8'))
        hrefs = [e.get('href', '') for e in snap['elements']]
        self.assertIn(self.base_a + '/two', hrefs)

        scrolled = self.session.scroll('down', approved)['snapshot']
        self.assertGreater(scrolled['scroll_y'], 0)

        # Click the same-domain link by index.
        index = next(e['index'] for e in snap['elements'] if e.get('href') == self.base_a + '/two')
        # Element indexes are re-tagged per snapshot; use the latest snapshot.
        index = next(e['index'] for e in scrolled['elements'] if e.get('href') == self.base_a + '/two')
        after = self.session.click(index, None, approved)['snapshot']
        self.assertEqual(after['title'], 'Second')
        self.assertIn('Page two loaded', after['text'])

    def test_cross_domain_navigation_blocked_until_approved(self):
        approved = {self.A}
        snap = self.session.open(self.base_a + '/', approved)['snapshot']
        index = next(e['index'] for e in snap['elements'] if 'site-b' in (e.get('href') or ''))
        with self.assertRaises(BrowserError) as cm:
            self.session.click(index, None, approved)
        self.assertIn(self.B, str(cm.exception))
        self.assertIn('browse_open', str(cm.exception))
        # After approval, the domain loads.
        approved.add(self.B)
        state = self.session.open(self.base_b + '/other', approved)
        self.assertEqual(state['snapshot']['title'], 'Other site')

    def test_redirect_to_loopback_page_is_blanked(self):
        # With the vetting proxy the 302 hop to 127.0.0.1 is refused before it is sent (the
        # server never sees /secret); the tool still refuses with BrowserError. The full
        # never-fires assertions live in test_vetproxy.py's LiveIsolatedBrowseTests.
        approved = {self.A}
        self.session.open(self.base_a + '/', approved)
        with self.assertRaises(BrowserError):
            self.session.open(self.base_a + '/redir-loop', approved)

    def test_private_urls_never_load(self):
        for bad in ('http://127.0.0.1:%d/' % self.server.port, 'file:///etc/passwd',
                    'http://localhost:%d/' % self.server.port, 'http://169.254.169.254/latest'):
            with self.assertRaises(BrowserError, msg=bad):
                self.session.open(bad, {'127.0.0.1', 'localhost'})

    def test_wedged_page_is_killed_not_hung(self):
        with mock.patch('grokbot.agent_browser._action_deadline', return_value=6):
            self.session.open(self.base_a + '/busy', {self.A}, False)
            import time as _time
            _time.sleep(3)
            started = _time.monotonic()
            with self.assertRaises(BrowserError) as cm:
                self.session.see({self.A}, False)
            self.assertLess(_time.monotonic() - started, 20)
            self.assertIn('stopped responding', str(cm.exception))
        # The session recovers with a fresh browser on the next action.
        self.assertEqual(self.session.open(self.base_a + '/two', {self.A}, False)['snapshot']['title'], 'Second')

    def test_browser_processes_do_not_inherit_secrets(self):
        import os as _os
        with mock.patch.dict(_os.environ, {'GROK_CANARY_API_KEY': 'canary-31337'}):
            self.session.open(self.base_a + '/two', {self.A}, False)
            from grokbot import agent_browser as _ab
            pids = [self.session._driver_pid] + _ab._descendants(self.session._driver_pid)
            self.assertGreater(len(pids), 1)
            for pid in pids:
                try:
                    with open('/proc/%d/environ' % pid, 'rb') as fh:
                        self.assertNotIn(b'canary-31337', fh.read(), pid)
                except FileNotFoundError:
                    pass

    def test_unapproved_navigation_refused_before_request(self):
        approved = {self.A}
        snap = self.session.open(self.base_a + '/', approved, False)['snapshot']
        index = next(e['index'] for e in snap['elements'] if 'site-b' in (e.get('href') or ''))
        hits = []
        original = _Handler.do_GET

        def counting(handler):
            hits.append(handler.headers.get('Host', ''))
            return original(handler)
        with mock.patch.object(_Handler, 'do_GET', counting):
            with self.assertRaises(BrowserError):
                self.session.click(index, None, approved, False)
        self.assertFalse([h for h in hits if h.startswith(self.B)], hits)

    def test_close_kills_browser(self):
        self.session.open(self.base_a + '/', {self.A})
        browser = self.session._browser
        self.assertIsNotNone(browser)
        self.session.close()
        self.assertIsNone(self.session._browser)
        with self.assertRaises(Exception):
            browser.new_context()


class HardeningUnitTests(unittest.TestCase):
    def test_scrubbed_env_drops_secrets(self):
        from grokbot.agent_browser import scrubbed_env
        env = scrubbed_env({'PATH': '/bin', 'GROK_API_KEY': 'x', 'GROK_TELEGRAM_TOKEN': 'y', 'GROK_APP_PASSWORD': 'z',
                            'GROK_SESSION_SECRET': 's', 'TAVILY_API_KEY': 't', 'HOME': '/h'})
        self.assertEqual(env, {'PATH': '/bin', 'HOME': '/h'})

    def test_browse_screenshot_labelled_untrusted(self):
        from grokbot.agent import _screenshot_note
        self.assertIn('UNTRUSTED', _screenshot_note('browse_open'))
        self.assertIn('not a message from the user', _screenshot_note('browse_click'))
        self.assertIn('critique the layout', _screenshot_note('preview_page'))


if __name__ == '__main__':
    unittest.main()
