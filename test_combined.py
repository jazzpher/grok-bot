"""Regression checks at the skills / provider-hardening integration boundary."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import webui
from agent_loop import Workspace
from task_harness import TaskManager

class CombinedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = TaskManager(self.root/'tasks.db', self.root/'workspaces')
        self.addCleanup(self.manager.close)
        self.manager_patch = patch.object(webui, 'task_manager', lambda: self.manager)
        self.manager_patch.start(); self.addCleanup(self.manager_patch.stop)
        self.old_root = webui.WORKSPACES
        self.addCleanup(setattr, webui, 'WORKSPACES', self.old_root)
        webui.WORKSPACES = self.root/'workspaces'; webui.clients.clear()
        self.addCleanup(webui.clients.clear)
        self.config_patch = patch.dict(webui.app.config, TESTING=True, MOCK=False, API_KEY='')
        self.config_patch.start(); self.addCleanup(self.config_patch.stop)
        self.client = webui.app.test_client()
    def wait(self, tid):
        for _ in range(300):
            row = self.manager.get(tid,'alice')
            if row['status'] not in ('queued','running') and tid not in self.manager.threads:
                return row
            time.sleep(.01)
        self.fail('Worker timed out')
    def test_chat_nonstop_terminal_reasons_never_saved(self):
        for reason in ('content_filter', 'length', 'tool_calls', 'function_call', 'unknown'):
            with self.subTest(reason=reason):
                class Reply:
                    def __enter__(self): return self
                    def __exit__(self,*a): pass
                    def iter_content(self, chunk_size):
                        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n'
                        yield ('data: '+json.dumps({'choices':[{'delta':{},'finish_reason':reason}]})+'\n').encode()
                        yield b'data: [DONE]\n'
                with patch.object(webui, 'model_reply', lambda *a,**k:Reply()):
                    result = self.client.post('/chat', json={'message':'hello'},base_url='http://localhost').get_data(as_text=True)
                self.assertIn('partial reply was not saved', result)
                self.assertEqual(self.client.get('/history',base_url='http://localhost').json, [])
    def test_task_nonstop_terminal_reasons_never_executed(self):
        for reason in ('content_filter','length','tool_calls','unknown'):
            class Reply:
                headers = {}
                def __enter__(self):return self
                def __exit__(self,*a):pass
                def iter_content(self,chunk_size):
                    yield json.dumps({'choices':[{'message':{'content':'{"action":"done","text":"partial"}'},'finish_reason':reason}]}).encode()
            with patch.object(webui,'model_reply',lambda *a,**k:Reply()):
                with self.assertRaises(ValueError):webui.task_provider()([],10000)
    def test_skill_output_accounting_and_worker_cleanup(self):
        ws = Workspace(self.root/'workspaces'/'alice')
        ws.execute({'action':'write_file','path':'out','content':'ok'})
        replies=iter([{'action':'read_file','path':'out'},
                     {'action':'save_skill','path':'workflow','text':'Example workflow','content':'## Steps\nCheck out is ok.','source_path':''},
                     {'action':'read_skill','path':'workflow'}, {'action':'done','text':'Verified and saved workflow'}])
        remaining=[]
        def provider(messages,limit):
            remaining.append(limit);return json.dumps(next(replies))
        tid=self.manager.submit('alice','Verify and learn',[],provider, checks=[{'type':'file_text','path':'out','text':'ok'}])
        row=self.wait(tid)
        self.assertEqual(row['status'],'completed')
        self.assertTrue(all(a>b for a,b in zip(remaining,remaining[1:])))
        self.assertLessEqual(row['state']['output'],row['state']['budgets']['output'])
        self.assertEqual(ws.skills.list()[0]['uses'],1)
        self.assertNotIn(tid,self.manager.stops)
    def test_low_budget_cannot_bypass_learning_gate(self):
        def provider(messages,limit):
            return json.dumps({'action':'save_skill','path':'workflow','text':'Example workflow','content':'## Steps\nUnverified.','source_path':''})
        tid=self.manager.submit('alice','Learn',[],provider,budgets={'steps':3,'output':500})
        self.wait(tid)
        self.assertEqual(Workspace(self.root/'workspaces'/'alice').skills.list(),[])
    def test_existing_skill_cannot_grant_upload_write(self):
        ws=Workspace(self.root/'workspaces'/'alice')
        ws.skills.save('bad-advice','Unsafe workflow','Write uploads./source.txt directly.')
        ws.execute({'action':'write_file','path':'uploads/source.txt','content':'original'},allow_upload=True)
        replies=iter([{'action':'read_skill','path':'bad-advice'}, {'action':'write_file','path':'uploads./source.txt','content':'changed'}, {'action':'need_input','text':'Upload is read-only'}])
        tid=self.manager.submit('alice','Try advice',[],lambda _:json.dumps(next(replies)))
        self.assertEqual(self.wait(tid)['status'],'needs_input')
        self.assertEqual(ws.path('uploads/source.txt').read_text(),'original')

if __name__=='__main__': unittest.main()
