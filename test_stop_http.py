"""Session-wide Stop contract and isolation over real local HTTP; no provider calls."""
import json
import threading
import unittest
import webui
import test_harness_http

class StopHTTPTests(unittest.TestCase):
    def setUp(self):
        test_harness_http.HTTPHarnessTests.setUp(self)
        self.a.get(self.url + '/history')
        self.b.get(self.url + '/history')
    shutdown = test_harness_http.HTTPHarnessTests.shutdown
    owner = test_harness_http.HTTPHarnessTests.owner

    def test_idle_stop_is_not_reported_as_requested(self):
        result = self.a.post(self.url + '/stop')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers['Content-Type'], 'application/json')
        self.assertEqual(result.json(), {'requested': False})

    def test_stop_only_cancels_current_owners_work(self):
        release = threading.Event()
        self.addCleanup(release.set)
        entered = [threading.Event(), threading.Event()]
        owners = list(webui.clients)
        owner_a = self.owner()
        owner_b = next(owner for owner in owners if owner != owner_a)
        def provider(index):
            def call(_):
                entered[index].set()
                if not release.wait(5): raise RuntimeError('test release timeout')
                return json.dumps({'action':'need_input', 'text':'paused'})
            return call
        a = self.manager.submit(owner_a, 'A', [], provider(0))
        b = self.manager.submit(owner_b, 'B', [], provider(1))
        self.assertTrue(all(event.wait(2) for event in entered))
        result = self.a.post(self.url + '/stop')
        self.assertEqual(result.json(), {'requested': True})
        self.assertTrue(self.manager.stops[a].is_set())
        self.assertFalse(self.manager.stops[b].is_set())
        self.assertTrue(webui.clients[owner_a]['stop'].is_set())
        self.assertFalse(webui.clients[owner_b]['stop'].is_set())
        release.set()
        for tid in (a, b): self.manager.threads[tid].join(2)
        self.assertEqual(self.manager.get(a, owner_a)['status'], 'stopped')
        self.assertEqual(self.manager.get(b, owner_b)['status'], 'needs_input')
        self.assertEqual(self.a.post(self.url + '/stop').json(), {'requested': False})

    def test_chat_stream_honors_stop_without_stopping_next_chat(self):
        from unittest.mock import patch
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def iter_lines(self):
                yield b'data: {"choices":[{"delta":{"content":"first"}}]}'
                yield b'data: {"choices":[{"delta":{"content":"should-not-appear"}}]}'
        c = webui.clients[self.owner()]
        with patch.object(webui, 'model_reply', lambda *a, **kw: Reply()):
            with webui.app.test_request_context('/chat', method='POST', json={'message':'hello'}):
                webui.session['sid'] = self.owner()
                reply = webui.chat(); stream = iter(reply.response)
                self.assertEqual(next(stream), 'first')
                self.assertEqual(self.a.post(self.url + '/stop').json(), {'requested': True})
                self.assertEqual(list(stream), [])
                reply.close()
            with webui.app.test_request_context('/chat', method='POST', json={'message':'next'}):
                webui.session['sid'] = self.owner()
                reply = webui.chat()
                self.assertEqual(list(reply.response), ['first', 'should-not-appear'])
                reply.close()
        self.assertFalse(c['busy'].locked())

    def test_stop_reports_active_chat_and_preserves_other_session(self):
        owner = self.owner(); c = webui.clients[owner]
        other = next(value for key, value in webui.clients.items() if key != owner)
        c['busy'].acquire()
        self.addCleanup(c['busy'].release)
        self.assertEqual(self.a.post(self.url + '/stop').json(), {'requested': True})
        self.assertTrue(c['stop'].is_set())
        self.assertFalse(other['stop'].is_set())
