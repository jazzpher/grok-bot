"""Key mask + Test button, friendly provider errors, chat retry notices, blocked-task Resume.
Providers are fakes; the only key used here is an obviously fake placeholder."""
import json
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

import requests

import task_harness
import webui
from task_harness import TaskManager, friendly_provider_error

FAKE_KEY = 'AIzaFAKE-NOT-A-REAL-KEY-0123456789x9k'
GEM = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'


class FakeResponse:
    def __init__(self, code=200, lines=None, url=GEM):
        self.status_code, self.url, self._lines = code, url, lines or []
        self.headers = {}
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('HTTP %d' % self.status_code, response=self)
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): self.close()
    def iter_content(self, chunk_size=8192):
        yield b''.join(self._lines)


def sse(text):
    return FakeResponse(200, [b'data: ' + json.dumps({'choices': [{'delta': {'content': text}}]}).encode() + b'\n\n',
                              b'data: [DONE]\n\n'])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.manager = TaskManager(root / 'tasks.db', root / 'workspaces'); self.addCleanup(self.manager.close)
        for p in (patch.object(webui, 'WORKSPACES', root / 'workspaces'), patch.object(webui, 'PASSWORD', ''),
                  patch.object(webui, 'DEPLOYMENT_MODE', 'local'), patch.object(webui, 'TASKS', self.manager, create=True),
                  patch.object(webui, 'TEST_COOLDOWN', 0), patch.object(task_harness, 'RETRY_DELAY', 0),
                  patch.dict(webui.settings, {}, clear=True), patch.dict(webui.app.config, {'API_KEY': '', 'MOCK': False})):
            p.start(); self.addCleanup(p.stop)
        webui.clients.clear()
        self.c = webui.app.test_client()
    def post(self, path, **kw):
        return self.c.post(path, base_url='http://localhost', **kw)
    def get(self, path):
        return self.c.get(path, base_url='http://localhost')
    def save_key(self):
        self.assertEqual(self.post('/set_key', json={'key': FAKE_KEY}).status_code, 200)


class MaskTests(Base):
    def test_mask_shape(self):
        self.assertEqual(webui.mask_key(FAKE_KEY), 'AIza\u2026x9k')
        self.assertEqual(webui.mask_key('short'), '\u2022' * 8)
        self.assertEqual(webui.mask_key(''), '')
        self.assertEqual(webui.mask_key(None), '')

    def test_full_key_never_returned(self):
        r = self.get('/key_status'); self.assertFalse(r.get_json()['configured'])
        save = self.post('/set_key', json={'key': FAKE_KEY})
        self.assertNotIn(FAKE_KEY, save.get_data(as_text=True))
        s = self.get('/key_status'); body = s.get_data(as_text=True)
        self.assertNotIn(FAKE_KEY, body)
        self.assertEqual(s.get_json()['masked'], 'AIza\u2026x9k')
        self.assertEqual(s.get_json()['source'], 'saved')
        self.assertNotIn(FAKE_KEY, self.get('/').get_data(as_text=True))

    def test_env_key_masked_too(self):
        webui.app.config['API_KEY'] = FAKE_KEY
        s = self.get('/key_status').get_json()
        self.assertEqual((s['masked'], s['source']), ('AIza\u2026x9k', 'environment'))


class TestButtonTests(Base):
    def run_test(self, effect):
        self.save_key()
        with patch.object(webui.requests, 'post', side_effect=effect) as post:
            r = self.post('/test_key')
        self.assertNotIn(FAKE_KEY, r.get_data(as_text=True))
        return r.get_json(), post

    def test_ok_sends_one_tiny_request(self):
        j, post = self.run_test(lambda *a, **k: FakeResponse(200))
        self.assertTrue(j['ok']); self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 32)
        self.assertEqual(len(post.call_args.kwargs['json']['messages']), 1)

    def test_clear_errors(self):
        for code, text in ((401, 'Wrong key'), (403, 'not allowed'), (404, 'No such model'), (503, 'Google is busy right now'),
                           (429, 'quota')):
            j, post = self.run_test(lambda *a, c=code, **k: FakeResponse(c))
            self.assertFalse(j['ok']); self.assertEqual(j['status'], code)
            self.assertIn(text, j['message']); self.assertEqual(post.call_count, 1, 'Test must never auto-retry')

    def test_network_errors(self):
        j, _ = self.run_test(requests.Timeout('secret-ish detail'))
        self.assertIn("Can't reach Google", j['message']); self.assertNotIn('secret-ish', json.dumps(j))
        j, _ = self.run_test(requests.ConnectionError('x'))
        self.assertFalse(j['ok'])

    def test_no_key(self):
        with patch.object(webui.requests, 'post') as post:
            j = self.post('/test_key').get_json()
        self.assertFalse(j['ok']); post.assert_not_called(); self.assertIn('No key', j['message'])

    def test_cooldown_blocks_hammering(self):
        self.save_key()
        with patch.object(webui, 'TEST_COOLDOWN', 60), patch.object(webui.requests, 'post', return_value=FakeResponse(200)) as post:
            self.assertEqual(self.post('/test_key').status_code, 200)
            r = self.post('/test_key')
        self.assertEqual(r.status_code, 429); self.assertEqual(post.call_count, 1)

    def test_get_not_allowed_and_cross_origin_denied(self):
        self.assertEqual(self.get('/test_key').status_code, 405)
        self.assertEqual(self.post('/test_key', headers={'Origin': 'https://evil.example'}).status_code, 403)


class FriendlyTextTests(unittest.TestCase):
    def test_messages(self):
        mk = lambda c, u=GEM: requests.HTTPError(response=FakeResponse(c, url=u))
        self.assertEqual(friendly_provider_error(mk(503)), 'Google is busy right now.')
        self.assertEqual(friendly_provider_error(mk(503), retrying=True), 'Google is busy right now, retrying...')
        self.assertIn('No such model', friendly_provider_error(mk(404)))
        self.assertIn('Wrong key', friendly_provider_error(mk(401)))
        self.assertTrue(friendly_provider_error(mk(503, 'https://api.x.ai/v1/chat/completions')).startswith('xAI is busy'))
        self.assertIn('HTTP 418', friendly_provider_error(mk(418)))
        self.assertNotIn('googleapis', friendly_provider_error(mk(404)))


class ChatTests(Base):
    def chat(self, effect):
        self.save_key()
        with patch.object(webui.requests, 'post', side_effect=effect) as post:
            r = self.post('/chat', json={'message': 'hi'}); body = r.get_data(as_text=True)
        return body, post

    def test_503_retries_with_visible_notices_then_succeeds(self):
        seq = [FakeResponse(503), FakeResponse(503), sse('hello there')]
        body, post = self.chat(lambda *a, **k: seq.pop(0))
        self.assertEqual(post.call_count, 3)
        self.assertIn('\x1eGoogle is busy right now, retrying 1 of 3...\x1f', body)
        self.assertIn('retrying 2 of 3', body)
        self.assertTrue(body.endswith('hello there'))
        self.assertEqual(self.get('/history').get_json()[-1]['content'], 'hello there')

    def test_gives_up_after_three_retries_with_friendly_text(self):
        body, post = self.chat(lambda *a, **k: FakeResponse(503))
        self.assertEqual(post.call_count, 4)
        self.assertIn('[Google is busy right now.]', body); self.assertNotIn('HTTP 503', body)

    def test_auth_and_model_errors_not_retried(self):
        for code, text in ((401, 'Wrong key'), (404, 'No such model')):
            body, post = self.chat(lambda *a, c=code, **k: FakeResponse(c))
            self.assertEqual(post.call_count, 1); self.assertIn(text, body); self.assertNotIn('\x1e', body)

    def test_timeout_then_ok(self):
        seq = [requests.Timeout(), sse('ok')]
        def eff(*a, **k):
            x = seq.pop(0)
            if isinstance(x, Exception): raise x
            return x
        body, post = self.chat(eff)
        self.assertIn("Can't reach Google right now, retrying 1 of 3", body); self.assertTrue(body.endswith('ok'))

    def test_failed_reply_not_saved_to_history(self):
        self.chat(lambda *a, **k: FakeResponse(503))
        self.assertEqual(self.get('/history').get_json(), [])


class TaskResumeTests(Base):
    def wait(self, tid, owner):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            row = self.manager.get(tid, owner)
            if row['status'] not in ('queued', 'running'): return row
            time.sleep(.01)
        self.fail('did not settle')

    def test_transient_blocks_friendly_then_resume_succeeds(self):
        calls = []
        def provider(messages):
            calls.append(1)
            if len(calls) <= 4:
                raise requests.HTTPError('boom', response=FakeResponse(503))
            return json.dumps({'action': 'need_input', 'text': 'finished'})
        tid = self.manager.submit('o', 'goal', [], provider)
        row = self.wait(tid, 'o')
        self.assertEqual(row['status'], 'blocked'); self.assertEqual(len(calls), 4)
        events = self.manager.events(tid, 'o', 0)
        retries = [e['text'] for e in events if e['type'] == 'retry']
        self.assertEqual(retries, ['Google is busy right now, retrying %d of 3...' % i for i in (1, 2, 3)])
        blocked = [e['text'] for e in events if e['type'] == 'blocked'][0]
        self.assertIn('Google is busy right now.', blocked); self.assertIn('Resume', blocked); self.assertNotIn('HTTP', blocked)
        end = time.monotonic() + 5
        while tid in self.manager.threads and time.monotonic() < end: time.sleep(.01)
        self.assertTrue(self.manager.resume(tid, 'o', provider))
        self.assertEqual(self.wait(tid, 'o')['status'], 'needs_input')

    def test_streak_resets_so_two_blips_do_not_block(self):
        seq = [503, None, 503, None]
        def provider(messages):
            x = seq.pop(0)
            if x: raise requests.HTTPError(response=FakeResponse(x))
            return json.dumps({'action': 'list_files'}) if len(seq) > 1 else json.dumps({'action': 'need_input', 'text': 'x'})
        tid = self.manager.submit('o', 'goal', [], provider)
        self.assertEqual(self.wait(tid, 'o')['status'], 'needs_input')

    def test_auth_error_blocks_immediately_with_key_hint_and_is_resumable(self):
        calls = []
        def provider(messages):
            calls.append(1); raise requests.HTTPError(response=FakeResponse(401))
        tid = self.manager.submit('o', 'goal', [], provider)
        row = self.wait(tid, 'o')
        self.assertEqual((row['status'], len(calls)), ('blocked', 1))
        self.assertIn('Wrong key', [e['text'] for e in self.manager.events(tid, 'o', 0) if e['type'] == 'blocked'][0])
        end = time.monotonic() + 5
        while tid in self.manager.threads and time.monotonic() < end: time.sleep(.01)
        self.assertTrue(self.manager.resume(tid, 'o', provider))
        self.wait(tid, 'o'); self.assertEqual(len(calls), 2)

    def test_resume_route_for_blocked_task_and_other_owner_denied(self):
        def bad(messages): raise requests.HTTPError(response=FakeResponse(404))
        with patch.object(webui, 'task_provider', lambda: bad):
            tid = self.post('/task', json={'message': 'go'}).get_json()['id']
        end = time.monotonic() + 5
        while time.monotonic() < end and self.get('/tasks/' + tid).get_json()['status'] in ('queued', 'running'): time.sleep(.01)
        self.assertEqual(self.get('/tasks/' + tid).get_json()['status'], 'blocked')
        while tid in self.manager.threads and time.monotonic() < end: time.sleep(.01)
        other = webui.app.test_client()
        self.assertEqual(other.post('/tasks/' + tid + '/resume', json={}, base_url='http://localhost').status_code, 404)
        ok = lambda m: json.dumps({'action': 'need_input', 'text': 'back'})
        with patch.object(webui, 'task_provider', lambda: ok):
            self.assertEqual(self.post('/tasks/' + tid + '/resume', json={}).status_code, 202)
        end = time.monotonic() + 5
        while time.monotonic() < end and self.get('/tasks/' + tid).get_json()['status'] in ('queued', 'running'): time.sleep(.01)
        self.assertEqual(self.get('/tasks/' + tid).get_json()['status'], 'needs_input')
        # a second one-click while already finished-and-not-blocked-by-provider still behaves (idempotent enough, no crash)
        self.assertIn(self.post('/tasks/' + tid + '/resume', json={}).status_code, (202, 409))


if __name__ == '__main__':
    unittest.main()
