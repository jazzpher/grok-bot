"""Real HTTP lifecycle regression: disconnect, worker, ownership and restart."""
import io
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import requests
from werkzeug.serving import make_server
import webui
from task_harness import TaskManager


class HTTPHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.manager = TaskManager(self.root/'tasks.db', self.root/'workspaces')
        self.addCleanup(self.manager.close)
        self.patches = [patch.object(webui, 'WORKSPACES', self.root/'workspaces'),
                        patch.object(webui, 'PASSWORD', ''), patch.object(webui, 'DEPLOYMENT_MODE', 'local'),
                        patch.object(webui, 'TASKS', self.manager, create=True)]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        webui.clients.clear()
        self.server = make_server('127.0.0.1', 0, webui.app, threaded=True)
        self.config = dict(webui.app.config)
        webui.app.config.update(MOCK=False, TESTING=False)
        self.addCleanup(lambda: webui.app.config.update(self.config))
        self.thread = threading.Thread(target=self.server.serve_forever); self.thread.start()
        self.addCleanup(self.shutdown)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.a = requests.Session(); self.b = requests.Session()
        self.addCleanup(self.a.close); self.addCleanup(self.b.close)
        self.a.get(self.url+'/'); self.b.get(self.url+'/')

    def shutdown(self):
        self.server.shutdown(); self.thread.join(3); self.server.server_close()

    def wait(self, tid):
        for _ in range(300):
            r = self.a.get(self.url+'/tasks/'+tid)
            self.assertEqual(r.status_code, 200)
            row = r.json()
            if row['status'] not in ('running', 'queued'):
                return row
            time.sleep(.01)
        self.fail('HTTP task stuck')

    def wait_worker_gone(self, tid):
        deadline = time.monotonic() + 5
        while tid in self.manager.threads and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertNotIn(tid, self.manager.threads)

    def test_disconnect_worker_completes_and_restart_restores_session(self):
        entered, release = threading.Event(), threading.Event()
        replies = iter([{'action':'copy_file','source_path':'uploads/input.txt.txt','path':'out.txt'},
                        {'action':'read_file','path':'out.txt'}, {'action':'done','text':'verified'}])
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def iter_content(self, chunk_size):
                entered.set(); release.wait(3)
                yield json.dumps({'choices':[{'message':{'content':json.dumps(next(replies))}}]}).encode()
        with patch.object(webui, 'model_reply', lambda *a, **k: Reply()):
            upload = self.a.post(self.url+'/upload', files={'file':('input.txt', b'a\r\nb\rc\n')})
            self.assertEqual(upload.status_code, 200)
            response = self.a.post(self.url+'/task', json={'message':'Copy uploads/input.txt.txt exactly to out.txt'}, stream=True)
            self.assertEqual(response.status_code, 202)
            tid = response.json()['id']; response.close()
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.a.post(self.url+'/task', json={'message':'second'}).status_code, 409)
            self.assertEqual(self.a.post(self.url+'/chat', json={'message':'chat'}).status_code, 409)
            self.assertEqual(self.a.post(self.url+'/clear').status_code, 409)
            self.assertEqual(self.b.get(self.url+'/tasks/'+tid).status_code, 404)
            self.assertEqual(self.b.get(self.url+'/tasks/'+tid+'/events').status_code, 404)
            self.assertEqual(self.b.post(self.url+'/tasks/'+tid+'/stop').status_code, 404)
            self.assertEqual(self.b.post(self.url+'/tasks/'+tid+'/resume', json={}).status_code, 404)
            release.set()
            self.assertEqual(self.wait(tid)['status'], 'completed')
        self.assertEqual(self.a.get(self.url+'/files/out.txt').content, b'a\r\nb\rc\n')
        events = self.a.get(self.url+'/tasks/'+tid+'/events').json()
        self.assertTrue(any(e['type']=='verification' for e in events))
        self.assertEqual(self.a.get(self.url+'/tasks/'+tid+'/events?after='+str(events[-1]['seq'])).json(), [])
        self.shutdown()
        self.manager.close()
        webui.clients.clear()
        reopened = TaskManager(self.root/'tasks.db', self.root/'workspaces')
        self.addCleanup(reopened.close); webui.TASKS = reopened
        self.server = make_server('127.0.0.1', self.server.server_port, webui.app, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever); self.thread.start()
        self.assertEqual(self.a.get(self.url+'/tasks/'+tid).json()['status'], 'completed')
        self.assertIn('uploads/input.txt.txt', self.a.get(self.url+'/files').json())
        self.assertTrue(self.a.get(self.url+'/history').json())
        self.assertIn(tid, [r['id'] for r in self.a.get(self.url+'/tasks').json()])

    def test_chat_rechecks_task_reservation_after_acquiring_session_lock(self):
        self.a.get(self.url+'/history')
        c = webui.clients[self.owner()]
        class RacingLock:
            def acquire(inner, blocking=True):
                self.manager.submit(self.owner(), 'Wait', [], lambda _: (time.sleep(.2), json.dumps({'action':'need_input','text':'wait'}))[1])
                return True
            def release(inner): pass
            def locked(inner): return False
        c['busy'] = RacingLock()
        self.assertEqual(self.a.post(self.url+'/chat', json={'message':'chat'}).status_code, 409)

    def owner(self):
        with webui.app.test_request_context(headers={'Cookie': 'session='+self.a.cookies.get('session')}):
            return webui.session['sid']

    def test_ui_has_restore_resume_and_explicit_checks(self):
        page = self.a.get(self.url+'/').text
        self.assertIn('restoreTasks()', page)
        self.assertIn('resumeTask', page)
        self.assertIn('checks', page)
        for payload in ([], None, {'message':'x','checks':[{'type':'semantic'}]}):
            self.assertEqual(self.a.post(self.url+'/task', json=payload).status_code, 400)
