import json
from unittest.mock import patch
import test_harness_http
import webui

class ResumeHTTPTests(__import__('unittest').TestCase):
    setUp = test_harness_http.HTTPHarnessTests.setUp
    shutdown = test_harness_http.HTTPHarnessTests.shutdown
    wait = test_harness_http.HTTPHarnessTests.wait
    wait_worker_gone = test_harness_http.HTTPHarnessTests.wait_worker_gone
    owner = test_harness_http.HTTPHarnessTests.owner
    def test_requirements_http_validation_prompt_and_owner_isolation(self):
        seen = []
        def provider():
            return lambda msgs: (seen.append(json.dumps(msgs)), json.dumps({'action':'need_input','text':'Which venue?'}))[1]
        with patch.object(webui, 'task_provider', provider):
            for invalid in (None, {}, '漢'*3000):
                self.assertEqual(self.a.post(self.url+'/task', json={'message':'invitation','requirements':invalid}).status_code, 400)
            r = self.a.post(self.url+'/task', json={'message':'invitation','requirements':'Tagalog; numbered list; warm'})
            self.assertEqual(r.status_code, 202)
            tid = r.json()['id']
            self.assertEqual(self.wait(tid)['status'], 'needs_input')
            row = self.a.get(self.url+'/tasks/'+tid).json()
            self.assertEqual(row['requirements'], 'Tagalog; numbered list; warm')
            self.assertEqual(row['attempts'], 1)
            self.assertIn('Tagalog; numbered list; warm', seen[0])
            self.assertEqual(self.b.get(self.url+'/tasks/'+tid).status_code, 404)

    def test_checks_on_resume_route_validates_owner_and_publishes_verified_result(self):
        calls = []
        def provider():
            def f(_):
                calls.append(1)
                if len(calls) == 1:
                    return json.dumps({'action':'need_input','text':'paused for checks'})
                return json.dumps({'action':'done','text':'HTTP_FINAL_SENTINEL'})
            return f
        with patch.object(webui,'task_provider',provider):
            r=self.a.post(self.url+'/task',json={'message':'verify artifact'})
            self.assertEqual(r.status_code,202); tid=r.json()['id']
            self.assertEqual(self.wait(tid)['status'],'needs_input'); self.wait_worker_gone(tid)
            checks=[{'type':'file_text','path':'out','text':'ok'}]
            self.assertEqual(self.b.post(self.url+'/tasks/'+tid+'/resume',json={'checks':checks}).status_code,404)
            self.assertEqual(self.a.post(self.url+'/tasks/'+tid+'/resume',json={'checks':[{'type':'file_text','path':'../out','text':'ok'}]}).status_code,400)
            self.assertEqual(self.manager.get(tid,self.owner())['checks'],[])
            (self.root/'workspaces'/self.owner()/'out').write_text('ok')
            r=self.a.post(self.url+'/tasks/'+tid+'/resume',json={'checks':checks,'answer':'checks supplied'})
            self.assertEqual(r.status_code,202)
            self.assertEqual(self.wait(tid)['status'],'completed')
            events=self.a.get(self.url+'/tasks/'+tid+'/events').json()
            results = [e['text'] for e in events if e['type']=='result']
            self.assertEqual(len(results), 1)
            self.assertIn('Explicit artifact checks passed', results[0])
            self.assertNotIn('HTTP_FINAL_SENTINEL', results[0])
            self.assertEqual(self.manager.get(tid,self.owner())['state']['steps'],2)
