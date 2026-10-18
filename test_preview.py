"""Tests for the preview_page tool: SSRF confinement, path guards, image
hand-off to the model, turn-end Chromium cleanup. Offline except the clearly
marked real-Chromium class, which skips when Playwright is unavailable."""
import base64
import json
import os
import pathlib
import shutil
import socket
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest import mock

from grokbot import config, preview
from grokbot.agent import run_turn, scrub_preview_images
from grokbot.tools import ToolContext, ToolRegistry, default_registry
from grokbot.tools import preview as preview_tool
from agent_loop import Workspace


def make_workspace():
    root = tempfile.mkdtemp(prefix='preview-test-')
    return Workspace(root)


class NetlocGuardTests(unittest.TestCase):
    """The preview server + page may only ever talk to 127.0.0.1/localhost on OUR port."""

    PORT = 45678

    def test_own_server_allowed(self):
        self.assertTrue(preview.preview_netloc_allowed('http://127.0.0.1:%d/index.html' % self.PORT, self.PORT))
        self.assertTrue(preview.preview_netloc_allowed('http://localhost:%d/style.css' % self.PORT, self.PORT))
        self.assertTrue(preview.preview_netloc_allowed('http://LOCALHOST:%d/a' % self.PORT, self.PORT))

    def test_file_scheme_denied(self):
        self.assertFalse(preview.preview_netloc_allowed('file:///etc/passwd', self.PORT))
        self.assertFalse(preview.preview_netloc_allowed('FILE:///etc/passwd', self.PORT))

    def test_other_loopback_port_denied(self):
        self.assertFalse(preview.preview_netloc_allowed('http://127.0.0.1:1/', self.PORT))
        self.assertFalse(preview.preview_netloc_allowed('http://localhost:%d/' % (self.PORT + 1), self.PORT))

    def test_lan_and_internet_denied(self):
        for url in ('http://192.168.1.1/', 'http://10.0.0.5/x', 'http://172.16.0.1/',
                    'http://169.254.169.254/latest/meta-data', 'https://example.com/',
                    'http://[::1]:%d/' % self.PORT, 'http://0.0.0.0:%d/' % self.PORT):
            self.assertFalse(preview.preview_netloc_allowed(url, self.PORT), url)

    def test_userinfo_and_tricks_denied(self):
        self.assertFalse(preview.preview_netloc_allowed('http://user@127.0.0.1:%d/' % self.PORT, self.PORT))
        self.assertFalse(preview.preview_netloc_allowed('http://127.0.0.1:%d@evil.com/' % self.PORT, self.PORT))
        self.assertFalse(preview.preview_netloc_allowed('http://127.0.0.1.evil.com/', self.PORT))
        self.assertFalse(preview.preview_netloc_allowed('', self.PORT))
        self.assertFalse(preview.preview_netloc_allowed('javascript:alert(1)', self.PORT))
        self.assertFalse(preview.preview_netloc_allowed('ftp://127.0.0.1:%d/x' % self.PORT, self.PORT))

    def test_host_header(self):
        self.assertTrue(preview.host_header_allowed('127.0.0.1:%d' % self.PORT, self.PORT))
        self.assertTrue(preview.host_header_allowed('localhost:%d' % self.PORT, self.PORT))
        self.assertFalse(preview.host_header_allowed('evil.com:%d' % self.PORT, self.PORT))
        self.assertFalse(preview.host_header_allowed('127.0.0.1:80', self.PORT))
        self.assertFalse(preview.host_header_allowed('127.0.0.1', self.PORT))
        self.assertFalse(preview.host_header_allowed('', self.PORT))

    def test_route_guard_allows_inline_only_besides_server(self):
        self.assertTrue(preview.request_allowed('data:image/png;base64,AAAA', self.PORT))
        self.assertTrue(preview.request_allowed('blob:http://127.0.0.1:%d/x' % self.PORT, self.PORT))
        self.assertTrue(preview.request_allowed('about:blank', self.PORT))
        self.assertTrue(preview.request_allowed('http://127.0.0.1:%d/img.png' % self.PORT, self.PORT))
        self.assertFalse(preview.request_allowed('https://cdn.evil.com/x.js', self.PORT))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.addCleanup(shutil.rmtree, self.ws.root, True)
        (self.ws.root / 'index.html').write_text('<h1>hello</h1>', encoding='utf-8')
        (self.ws.root / 'style.css').write_text('h1{color:red}', encoding='utf-8')
        sub = self.ws.root / 'site'
        sub.mkdir()
        (sub / 'index.html').write_text('<p>sub</p>', encoding='utf-8')
        self.server = preview.PreviewServer(self.ws.root)
        self.addCleanup(self.server.close)
        self.port = self.server._ensure()

    def http(self, path, host=None):
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (self.port, path))
        req.add_header('Host', host or '127.0.0.1:%d' % self.port)
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_serves_workspace_files(self):
        status, body = self.http('/index.html')
        self.assertEqual(status, 200)
        self.assertIn(b'hello', body)
        status, body = self.http('/style.css')
        self.assertEqual(status, 200)
        status, body = self.http('/site/')          # directory index
        self.assertEqual(status, 200)
        self.assertIn(b'sub', body)

    def test_traversal_denied(self):
        # urllib normalises ../, so craft the raw request by hand.
        for raw in (b'GET /../etc/passwd HTTP/1.0', b'GET /%2e%2e/%2e%2e/etc/passwd HTTP/1.0',
                    b'GET /site/../../etc/passwd HTTP/1.0'):
            with socket.create_connection(('127.0.0.1', self.port), timeout=5) as sock:
                sock.sendall(raw + b'\r\nHost: 127.0.0.1:%d\r\n\r\n' % self.port)
                head = sock.recv(200).split(b'\r\n', 1)[0]
            self.assertIn(head.split(b' ')[1], (b'403', b'404'), raw)

    def test_absolute_outside_never_served(self):
        secret = pathlib.Path(tempfile.mkdtemp()) / 'secret.html'
        secret.write_text('SECRET', encoding='utf-8')
        status, _ = self.http('/' + str(secret).lstrip('/'))
        self.assertIn(status, (403, 404))

    def test_wrong_host_header_denied(self):
        status, _ = self.http('/index.html', host='evil.com:%d' % self.port)
        self.assertEqual(status, 403)
        status, _ = self.http('/index.html', host='127.0.0.1:80')
        self.assertEqual(status, 403)

    def test_symlink_escape_denied(self):
        outside = pathlib.Path(tempfile.mkdtemp()) / 'out.html'
        outside.write_text('OUT', encoding='utf-8')
        link = self.ws.root / 'link.html'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('no symlink permission')
        status, _ = self.http('/link.html')
        self.assertIn(status, (403, 404))

    def test_close_stops_serving(self):
        port = self.server._ensure()
        self.server.close()
        with self.assertRaises(OSError):
            urllib.request.urlopen('http://127.0.0.1:%d/index.html' % port, timeout=3)


class PathResolutionTests(unittest.TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.addCleanup(shutil.rmtree, self.ws.root, True)
        (self.ws.root / 'index.html').write_text('<h1>x</h1>', encoding='utf-8')
        (self.ws.root / 'notes.md').write_text('# notes', encoding='utf-8')
        (self.ws.root / 'site').mkdir()
        (self.ws.root / 'site' / 'index.html').write_text('<p>s</p>', encoding='utf-8')
        self.ctx = ToolContext(workspace=self.ws, session_id='t')

    def test_html_file_ok(self):
        rel, _ = preview.resolve_html_path(self.ctx, 'index.html')
        self.assertEqual(rel, 'index.html')

    def test_directory_resolves_to_index(self):
        rel, _ = preview.resolve_html_path(self.ctx, 'site')
        self.assertEqual(rel, 'site/index.html')

    def test_non_html_refused(self):
        with self.assertRaises(preview.PreviewError):
            preview.resolve_html_path(self.ctx, 'notes.md')

    def test_missing_file(self):
        with self.assertRaises(preview.PreviewError):
            preview.resolve_html_path(self.ctx, 'nope.html')

    def test_traversal_and_absolute_refused(self):
        for bad in ('../outside.html', '/etc/passwd', '..', ''):
            with self.assertRaises(preview.PreviewError):
                preview.resolve_html_path(self.ctx, bad)


FAKE_PNG = b'\x89PNG\r\n\x1a\n' + b'fake' * 100


def fake_capture(rel, viewport='desktop'):
    return {'url': 'http://127.0.0.1:9999/' + rel,
            'viewport': {'width': 1280, 'height': 800, 'name': viewport},
            'layout': {'title': 'T', 'scroll_width': 1400, 'client_width': 1280, 'scroll_height': 900,
                       'element_count': 12, 'body_background': 'rgb(0, 0, 0)', 'horizontal_overflow': True,
                       'boxes': [{'tag': 'h1', 'depth': 2, 'text': 'Hi', 'x': 0, 'y': 0, 'w': 100, 'h': 20}],
                       'contrast_failures': [{'text': 'faint', 'ratio': 2.1, 'fg': 'rgb(1,1,1)', 'bg': 'rgb(0,0,0)'}]},
            'image': (FAKE_PNG, 'image/png')}


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.addCleanup(shutil.rmtree, self.ws.root, True)
        (self.ws.root / 'index.html').write_text('<h1>x</h1>', encoding='utf-8')
        self.ctx = ToolContext(workspace=self.ws, session_id='t', turn_id='turn1')
        preview_tool._TURN_LOCAL.turn = None
        preview_tool._TURN_LOCAL.count = 0

    def registry(self):
        return default_registry(plugins_too=False)

    def test_off_by_default(self):
        reg = self.registry()
        with mock.patch.object(config, 'PREVIEW_ENABLED', False):
            self.assertNotIn('preview_page', reg.names_for(self.ctx))

    def test_available_when_enabled(self):
        reg = self.registry()
        with mock.patch.object(config, 'PREVIEW_ENABLED', True):
            self.assertIn('preview_page', reg.names_for(self.ctx))

    def run_tool(self, path='index.html', viewport='desktop'):
        reg = self.registry()
        with mock.patch.object(config, 'PREVIEW_ENABLED', True), \
             mock.patch.object(preview.PreviewSession, 'capture', lambda self, rel, viewport='desktop': fake_capture(rel, viewport)):
            return reg.execute(self.ctx, 'preview_page', {'path': path, 'viewport': viewport})

    def test_success_returns_text_and_image_meta(self):
        result = self.run_tool()
        self.assertTrue(result.ok, result.output)
        self.assertIn('horizontal_overflow: YES', result.output)
        self.assertIn('contrast', result.output)
        self.assertIn('h1', result.output)
        images = result.meta.get('images')
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0]['data_url'].startswith('data:image/png;base64,'))
        self.assertEqual(base64.b64decode(images[0]['data_url'].split(',', 1)[1]), FAKE_PNG)
        self.assertEqual(result.meta['preview']['path'], 'index.html')

    def test_images_disabled_by_flag(self):
        reg = self.registry()
        with mock.patch.object(config, 'PREVIEW_ENABLED', True), \
             mock.patch.object(config, 'PREVIEW_IMAGES', False), \
             mock.patch.object(preview.PreviewSession, 'capture', lambda self, rel, viewport='desktop': fake_capture(rel, viewport)):
            result = reg.execute(self.ctx, 'preview_page', {'path': 'index.html'})
        self.assertTrue(result.ok)
        self.assertEqual(result.meta.get('images'), [])
        self.assertIn('GROK_PREVIEW_IMAGES', result.output)

    def test_bad_viewport_rejected_by_schema(self):
        reg = self.registry()
        with mock.patch.object(config, 'PREVIEW_ENABLED', True):
            result = reg.execute(self.ctx, 'preview_page', {'path': 'index.html', 'viewport': 'watch'})
        self.assertFalse(result.ok)
        self.assertIn('viewport', result.output)

    def test_non_html_and_traversal_errors_are_safe_text(self):
        (self.ws.root / 'x.py').write_text('print(1)', encoding='utf-8')
        result = self.run_tool('x.py')
        self.assertFalse(result.ok)
        self.assertIn('not HTML', result.output)
        result = self.run_tool('/etc/passwd')
        self.assertFalse(result.ok)

    def test_missing_playwright_is_clear_error_not_crash(self):
        reg = self.registry()
        with mock.patch.object(config, 'PREVIEW_ENABLED', True), \
             mock.patch.object(preview.PreviewSession, 'capture',
                               lambda self, rel, viewport='desktop': (_ for _ in ()).throw(
                                   preview.PreviewError('preview_page needs Playwright installed: pip install playwright && playwright install chromium.'))):
            result = reg.execute(self.ctx, 'preview_page', {'path': 'index.html'})
        self.assertFalse(result.ok)
        self.assertIn('Playwright', result.output)

    def test_round_budget_three_per_turn(self):
        for _ in range(3):
            self.assertTrue(self.run_tool().ok)
        result = self.run_tool()
        self.assertFalse(result.ok)
        self.assertIn('budget', result.output)

    def test_budget_resets_on_new_turn(self):
        for _ in range(3):
            self.run_tool()
        self.ctx.turn_id = 'turn2'
        self.assertTrue(self.run_tool().ok)


class AgentLoopImageTests(unittest.TestCase):
    """The screenshot must reach the model as an image part, then be scrubbed from history."""

    def setUp(self):
        self.ws = make_workspace()
        self.addCleanup(shutil.rmtree, self.ws.root, True)
        (self.ws.root / 'index.html').write_text('<h1>x</h1>', encoding='utf-8')

    def make_registry(self):
        reg = ToolRegistry()

        @reg.tool('fake_preview', 'fake', {'type': 'object', 'properties': {}, 'required': []})
        def fake_preview(ctx):
            from grokbot.tools import ToolResult
            data_url = 'data:image/png;base64,' + base64.b64encode(FAKE_PNG).decode()
            return ToolResult('fake_preview', {}, 'PREVIEW text', True, 0.0,
                              meta={'images': [{'data_url': data_url, 'name': 'preview-index.html'}]})

        return reg

    def scripted_stream(self, cfg, messages, tools=None, stop=None):
        if not any(m.get('role') == 'tool' for m in messages):
            yield {'type': 'tool_calls', 'calls': [{'id': 'c1', 'name': 'fake_preview', 'arguments': '{}'}]}
            yield {'type': 'finish', 'reason': 'tool_calls'}
        else:
            # The model must have received the image part by the time it answers.
            saw_image = any(isinstance(m.get('content'), list)
                            and any(p.get('type') == 'image_url' for p in m['content'] if isinstance(p, dict))
                            for m in messages)
            yield {'type': 'text', 'delta': 'saw image' if saw_image else 'NO IMAGE RECEIVED'}
            yield {'type': 'finish', 'reason': 'stop'}

    def test_image_reaches_model_and_history_is_scrubbed(self):
        events = []
        ctx = ToolContext(workspace=self.ws, session_id='t')
        result = run_turn([{'role': 'user', 'content': 'preview it'}], self.make_registry(), ctx,
                          ('key', 'http://x', 'model'), events.append, stream_fn=self.scripted_stream)
        self.assertEqual(result.text, 'saw image')
        kinds = [e['type'] for e in events]
        self.assertIn('preview_image', kinds)
        shot = next(e for e in events if e['type'] == 'preview_image')
        self.assertTrue(shot['data_url'].startswith('data:image/png;base64,'))
        for message in result.messages:
            self.assertNotIn('_preview_image', message)
            content = message.get('content')
            if isinstance(content, list):
                self.assertFalse(any(p.get('type') == 'image_url' for p in content if isinstance(p, dict)),
                                 'scrubbed history must not keep image parts')
        scrubbed = [m['content'] for m in result.messages if isinstance(m.get('content'), str)
                    and 'screenshot omitted' in m['content']]
        self.assertTrue(scrubbed)

    def test_turn_end_closes_preview_session(self):
        class DummySession:
            closed = False

            def close(self):
                self.closed = True

        session = DummySession()
        ctx = ToolContext(workspace=self.ws, session_id='t')
        ctx.extras['preview_session'] = session

        def stream(cfg, messages, tools=None, stop=None):
            yield {'type': 'text', 'delta': 'done'}
            yield {'type': 'finish', 'reason': 'stop'}

        run_turn([{'role': 'user', 'content': 'hi'}], self.make_registry(), ctx,
                 ('key', 'http://x', 'model'), lambda e: None, stream_fn=stream)
        self.assertTrue(session.closed)
        self.assertNotIn('preview_session', ctx.extras)

    def test_prompt_rule_included_only_when_enabled(self):
        from grokbot.agent import build_system_prompt, PREVIEW_RULE
        from grokbot.approvals import SandboxPolicy
        reg = default_registry(plugins_too=False)
        ctx = ToolContext(workspace=self.ws, session_id='t')
        with mock.patch.object(config, 'PREVIEW_ENABLED', True):
            self.assertIn(PREVIEW_RULE, build_system_prompt(ctx, reg, SandboxPolicy()))
        with mock.patch.object(config, 'PREVIEW_ENABLED', False):
            self.assertNotIn(PREVIEW_RULE, build_system_prompt(ctx, reg, SandboxPolicy()))


@unittest.skipUnless(shutil.which('python3') and os.environ.get('GROK_LIVE_PREVIEW_TESTS') == '1',
                     'live Chromium test: set GROK_LIVE_PREVIEW_TESTS=1 with playwright + chromium installed')
class LiveChromiumTests(unittest.TestCase):
    """Real Playwright pass: screenshot bytes, layout numbers, guard blocks, cleanup."""

    def setUp(self):
        self.ws = make_workspace()
        self.addCleanup(shutil.rmtree, self.ws.root, True)
        (self.ws.root / 'index.html').write_text(
            '<html><head><title>Preview Test</title><link rel="stylesheet" href="style.css"></head>'
            '<body><h1>Hello</h1><div id="wide">wide</div>'
            '<img src="http://169.254.169.254/x.png">'          # must be aborted by the guard
            '<script>fetch("file:///etc/passwd").catch(()=>{});'
            'fetch("http://127.0.0.1:1/").catch(()=>{});</script>'
            '</body></html>', encoding='utf-8')
        (self.ws.root / 'style.css').write_text(
            'body{background:#111;color:#eee}#wide{width:3000px;height:10px}', encoding='utf-8')
        self.session = preview.PreviewSession(self.ws.root)
        self.addCleanup(self.session.close)

    def test_capture_real_page(self):
        capture = self.session.capture('index.html')
        image, mime = capture['image']
        self.assertEqual(mime, 'image/png')
        self.assertTrue(image.startswith(b'\x89PNG'))
        self.assertGreater(len(image), 2000)
        layout = capture['layout']
        self.assertEqual(layout['title'], 'Preview Test')
        self.assertTrue(layout['horizontal_overflow'], 'the 3000px div must flag overflow')
        self.assertTrue(any(b['tag'] == 'h1' for b in layout['boxes']))
        self.assertEqual(layout['body_background'], 'rgb(17, 17, 17)')

    def test_guard_blocks_external_and_page_still_renders(self):
        capture = self.session.capture('index.html')
        # Page rendered despite the blocked subrequests; url stayed on the preview server.
        self.assertTrue(capture['url'].startswith('http://127.0.0.1:'))
        self.assertTrue(preview.preview_netloc_allowed(capture['layout']['url'], self.session.server.port))

    def test_mobile_viewport(self):
        capture = self.session.capture('index.html', viewport='mobile')
        self.assertEqual(capture['viewport']['width'], 390)

    def test_close_kills_browser_and_server(self):
        self.session.capture('index.html')
        port = self.session.server.port
        self.session.close()
        self.assertIsNone(self.session._browser)
        with self.assertRaises(OSError):
            urllib.request.urlopen('http://127.0.0.1:%d/index.html' % port, timeout=3)


if __name__ == '__main__':
    unittest.main()
