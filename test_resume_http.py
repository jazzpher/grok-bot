import json
from unittest.mock import patch
import test_harness_http
import webui

class ResumeHTTPTests(__import__('unittest').TestCase):
    setUp = test_harness_http.HTTPHarnessTests.setUp
    shutdown = test_harness_http.HTTPHarnessTests.shutdown
    wait = test_harness_http.HTTPHarnessTests.wait
    owner = test_harness_http.HTTPHarnessTests.owner
    def test_checks_on_resume_route_validates_owner_and_publishes_verified_result(self):
        def provider(): return lambda _:json.dumps({'action':'done','text':'HTTP_FINAL_SENTINEL'})
        with patch.object(webui,'task_provider',provider):
            r=self.a.post(self.url+'/task',json={'message':'verify artifact'})
            self.assertEqual(r.status_code,202); tid=r.json()['id']
            self.assertEqual(self.wait(tid)['status'],'needs_input'); self.manager.threads[tid].join(2)
            checks=[{'type':'file_text','path':'out','text':'ok'}]
            self.assertEqual(self.b.post(self.url+'/tasks/'+tid+'/resume',json={'checks':checks}).status_code,404)
            self.assertEqual(self.a.post(self.url+'/tasks/'+tid+'/resume',json={'checks':[{'type':'file_text','path':'../out','text':'ok'}]}).status_code,400)
            self.assertEqual(self.manager.get(tid,self.owner())['checks'],[])
            (self.root/'workspaces'/self.owner()/'out').write_text('ok')
            r=self.a.post(self.url+'/tasks/'+tid+'/resume',json={'checks':checks,'answer':'checks supplied'})
            self.assertEqual(r.status_code,202)
            self.assertEqual(self.wait(tid)['status'],'completed')
            events=self.a.get(self.url+'/tasks/'+tid+'/events').json()
            self.assertEqual([e['text'] for e in events if e['type']=='result'],['HTTP_FINAL_SENTINEL'])
            self.assertEqual(self.manager.get(tid,self.owner())['state']['steps'],2)
