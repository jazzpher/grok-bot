import sys
import tempfile
import unittest
from unittest import mock

from agent_loop import ACTION_FIELDS, ACTION_SCHEMA, ToolError, Workspace, parse_action
import browser_tools


class FakeBrowser:
    def __init__(self):
        self.calls = []

    def open(self, url):
        self.calls.append(('open', url))
        return 'TITLE: Example\nURL: ' + url + '\n\nhi'

    def click(self, selector):
        self.calls.append(('click', selector))
        return 'TITLE: t\nURL: u\n\nafter'

    def type(self, selector, value):
        self.calls.append(('type', selector, value))
        return 'typed'

    def text(self):
        return 'TITLE: t\nURL: u\n\nbody'

    def close(self):
        self.calls.append(('close',))
        return 'Browser closed.'


class BrowserProtocolTests(unittest.TestCase):
    def test_browse_actions_are_registered(self):
        self.assertEqual(ACTION_FIELDS['browse_open'], ('url',))
        self.assertEqual(ACTION_FIELDS['browse_read'], ())
        self.assertEqual(ACTION_FIELDS['browse_click'], ('selector',))
        self.assertEqual(ACTION_FIELDS['browse_type'], ('selector', 'content'))
        self.assertEqual(ACTION_FIELDS['browse_close'], ())
        for field in ('url', 'selector'):
            self.assertIn(field, ACTION_SCHEMA['required'])
            self.assertIn(field, ACTION_SCHEMA['properties'])

    def test_parse_accepts_browse_actions(self):
        a = parse_action('{"action":"browse_open","text":"","path":"","source_path":"",'
                         '"content":"","expression":"","url":"https://example.com","selector":""}')
        self.assertEqual(a['url'], 'https://example.com')
        a = parse_action('{"action":"browse_click","text":"","path":"","source_path":"",'
                         '"content":"","expression":"","url":"","selector":"#submit"}')
        self.assertEqual(a['selector'], '#submit')

    def test_parse_rejects_browse_actions_without_fields(self):
        for bad in ('{"action":"browse_open","url":""}',
                    '{"action":"browse_click","selector":""}'):

            with self.assertRaises(Exception):
                parse_action(bad)


class UrlGuardTests(unittest.TestCase):
    def test_rejects_non_http_schemes(self):
        for bad in ('ftp://example.com/x', 'javascript:alert(1)', 'file:///etc/passwd', ''):
            with self.assertRaises(browser_tools.BrowserError):
                browser_tools.check_url(bad)

    def test_rejects_local_and_private_hosts(self):
        for bad in ('http://localhost/', 'http://127.0.0.1/', 'http://169.254.169.254/latest',
                    'http://10.0.0.1/', 'http://192.168.1.1/', 'http://172.16.0.1/',
                    'http://172.31.255.1/', 'http://metadata.google.internal/', 'http://printer.local/'):
            with self.assertRaises(browser_tools.BrowserError):
                browser_tools.check_url(bad)

    @staticmethod
    def _dns(mapping):
        def fake(host, port, *a, **k):
            return [(2, 1, 6, '', (mapping[host], port))]
        return mock.patch('grokbot.netguard.socket.getaddrinfo', fake)

    def test_rejects_hostname_that_resolves_to_private_ip(self):
        for ip in ('127.0.0.1', '10.1.2.3', '192.168.0.1', '169.254.169.254', '::1'):
            with self._dns({'evil.example.com': ip}):
                with self.assertRaises(browser_tools.BrowserError):
                    browser_tools.check_url('https://evil.example.com/')

    def test_rejects_unresolvable_host(self):
        with mock.patch('grokbot.netguard.socket.getaddrinfo', side_effect=OSError):
            with self.assertRaises(browser_tools.BrowserError):
                browser_tools.check_url('https://nope.example.com/')

    def test_request_allowed_covers_redirect_hops_and_schemes(self):
        with self._dns({'good.example.com': '93.184.216.34', 'rebind.example.com': '127.0.0.1'}):
            self.assertTrue(browser_tools.request_allowed('https://good.example.com/x'))
            self.assertFalse(browser_tools.request_allowed('https://rebind.example.com/'))  # redirect target
            self.assertFalse(browser_tools.request_allowed('http://192.168.1.1/admin'))
            self.assertFalse(browser_tools.request_allowed('file:///etc/passwd'))
            self.assertFalse(browser_tools.request_allowed('ftp://good.example.com/'))
        self.assertTrue(browser_tools.request_allowed('data:text/plain,hi'))
        self.assertTrue(browser_tools.request_allowed('about:blank'))

    def test_route_guard_aborts_blocked_and_continues_allowed(self):
        class Req:
            def __init__(self, url): self.url = url
        class Route:
            def __init__(self, url): self.request = Req(url); self.done = None
            def continue_(self): self.done = 'continue'
            def abort(self, reason): self.done = 'abort'
        with self._dns({'good.example.com': '93.184.216.34'}):
            ok, bad = Route('https://good.example.com/'), Route('http://127.0.0.1:8080/')
            browser_tools._route_guard(ok); browser_tools._route_guard(bad)
        self.assertEqual((ok.done, bad.done), ('continue', 'abort'))

    def test_accepts_public_http_and_https(self):
        with self._dns({'example.com': '93.184.216.34', 'Example.COM': '93.184.216.34'}):
            self._accepts()

    def _accepts(self):
        self.assertEqual(browser_tools.check_url('https://example.com/a?b=1'),
                         'https://example.com/a?b=1')
        self.assertEqual(browser_tools.check_url('http://Example.COM'),
                         'http://Example.COM')


class PageSummaryTests(unittest.TestCase):
    def test_long_text_is_truncated_with_notice(self):
        out = browser_tools.page_summary('T', 'https://e.com/', 'x' * 20000, limit=12000)
        self.assertLessEqual(len(out), 12200)
        self.assertIn('truncated', out)

    def test_short_text_is_kept_whole(self):
        out = browser_tools.page_summary('Hi', 'https://e.com/', 'body text', limit=12000)
        self.assertIn('TITLE: Hi', out)
        self.assertIn('body text', out)
        self.assertNotIn('truncated', out)


class WorkspaceBrowserTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Workspace(self._tmp.name)

    def test_dispatch_uses_injected_session(self):
        fake = FakeBrowser()
        self.ws.browser = fake
        for name in ('BROWSER_ENABLED', 'BROWSER_INTERACT'):
            patcher = mock.patch('grokbot.config.' + name, True)
            patcher.start()
            self.addCleanup(patcher.stop)
        out = self.ws.execute({'action': 'browse_open', 'url': 'https://example.com'})
        self.assertIn('TITLE: Example', out)
        self.assertEqual(fake.calls[0], ('open', 'https://example.com'))
        self.ws.execute({'action': 'browse_click', 'selector': '#go'})
        self.ws.execute({'action': 'browse_type', 'selector': '#q', 'content': 'hi'})
        self.ws.execute({'action': 'browse_close'})
        self.assertEqual(fake.calls[-1], ('close',))

    def test_browser_is_off_by_default(self):
        import grokbot.config as cfg, importlib, os
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('GROK_BROWSER_ENABLED', None); os.environ.pop('GROK_BROWSER_INTERACT', None)
            importlib.reload(cfg)
            try:
                self.assertFalse(cfg.BROWSER_ENABLED)
                self.assertFalse(cfg.BROWSER_INTERACT)
            finally:
                importlib.reload(cfg)

    def test_click_and_type_need_interact_opt_in(self):
        fake = FakeBrowser()
        self.ws.browser = fake
        with mock.patch('grokbot.config.BROWSER_ENABLED', True), mock.patch('grokbot.config.BROWSER_INTERACT', False):
            self.ws.execute({'action': 'browse_open', 'url': 'https://example.com'})
            self.ws.execute({'action': 'browse_read'})
            for act in ({'action': 'browse_click', 'selector': '#go'},
                        {'action': 'browse_type', 'selector': '#q', 'content': 'hi'}):
                with self.assertRaises(ToolError) as ctx:
                    self.ws.execute(act)
                self.assertIn('GROK_BROWSER_INTERACT', str(ctx.exception))
        self.assertEqual([c[0] for c in fake.calls], ['open'])

    def test_disabled_browser_is_a_clean_error(self):
        self.ws.browser = FakeBrowser()
        with mock.patch('grokbot.config.BROWSER_ENABLED', False):
            with self.assertRaises(ToolError) as ctx:
                self.ws.execute({'action': 'browse_open', 'url': 'https://example.com'})
        self.assertIn('GROK_BROWSER_ENABLED', str(ctx.exception))
        self.assertEqual(self.ws.browser.calls, [])

    def test_browse_type_requires_content(self):
        self.ws.browser = FakeBrowser()
        with mock.patch('grokbot.config.BROWSER_ENABLED', True), mock.patch('grokbot.config.BROWSER_INTERACT', True), \
                self.assertRaises(ToolError):
            self.ws.execute({'action': 'browse_type', 'selector': '#q', 'content': ''})
        self.assertEqual(self.ws.browser.calls, [])

    def test_missing_playwright_gives_install_hint(self):
        with mock.patch('grokbot.config.BROWSER_ENABLED', True), \
                mock.patch.dict(sys.modules, {'playwright': None, 'playwright.sync_api': None}):
            with self.assertRaises(ToolError) as ctx:
                self.ws.execute({'action': 'browse_open', 'url': 'https://example.com'})
        self.assertIn('pip install playwright', str(ctx.exception))

    def test_close_is_safe_and_idempotent(self):
        fake = FakeBrowser()
        self.ws.browser = fake
        self.ws.close()
        self.ws.close()
        self.assertIsNone(self.ws.browser)
        self.assertEqual(fake.calls[-1], ('close',))


class WorkerThreadTests(unittest.TestCase):
    def test_close_then_reuse_gets_a_fresh_worker_thread(self):
        import threading
        seen = []

        class Session(browser_tools.BrowserSession):
            @browser_tools._on_worker
            def probe(self):
                seen.append(threading.current_thread().name)
                return 'ok'

        s = Session()
        self.assertEqual(s.probe(), 'ok')
        self.assertNotEqual(seen[0], threading.current_thread().name)
        s.close()
        self.assertIsNone(s._worker)
        self.assertEqual(s.probe(), 'ok')
        s.close()


if __name__ == '__main__':
    unittest.main()
