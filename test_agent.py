import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_loop import Workspace, ToolError, calculate, run_loop
import webui

class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def run_actions(self, actions, limit=12):
        calls = iter(actions)
        return list(run_loop('goal', [], self.workspace, lambda _: json.dumps(next(calls)), lambda: False, limit))
    def test_plan_write_check_done(self):
        events = self.run_actions([{'action':'plan','text':'Make file'}, {'action':'write_file','path':'hello.txt','content':'Hello ₱'},
            {'action':'done','text':'too early'}, {'action':'read_file','path':'hello.txt'}, {'action':'done','text':'Written and checked.'}])
        self.assertEqual(events[-1]['type'], 'done')
        self.assertIn('check', [e['type'] for e in events])
        self.assertEqual((Path(self.tmp.name)/'hello.txt').read_text(), 'Hello ₱')
    def test_blocked(self):
        self.assertEqual(self.run_actions([{'action':'need_input','text':'No browser tool.'}])[-1]['type'],'blocked')
    def test_repeat(self):
        a={'action':'read_file','path':'missing'}
        self.assertEqual(self.run_actions([a,a,a])[-1]['type'],'blocked')
    def test_limit(self):
        self.assertEqual(self.run_actions([{'action':'plan','text':'still planning'}]*3,3)[-1]['type'],'limit')
    def test_stop(self):
        events=list(run_loop('goal',[],self.workspace,lambda _: 'unused',lambda:True))
        self.assertEqual(events[-1]['type'],'stopped')
    def test_stop_after_model_before_write(self):
        stop=[False]
        def call(_):
            stop[0]=True
            return json.dumps({'action':'write_file','path':'bad','content':'bad'})
        events=list(run_loop('goal',[],self.workspace,call,lambda:stop[0]))
        self.assertFalse((Path(self.tmp.name)/'bad').exists())
        self.assertEqual(events[-1]['type'],'stopped')
    def test_bad_json_budget(self):
        events=list(run_loop('goal',[],self.workspace,lambda _: 'not JSON',lambda:False,2))
        self.assertEqual(events[-1]['type'],'limit')
    def test_provider_error(self):
        def fail(_): raise RuntimeError('secret api text')
        events=list(run_loop('goal',[],self.workspace,fail,lambda:False))
        self.assertEqual(events[-1]['type'],'blocked'); self.assertNotIn('secret', str(events))
    def test_path_and_symlinks(self):
        for p in ['../secret','/etc/passwd','.']:
            with self.assertRaises(ToolError): self.workspace.path(p)
        (Path(self.tmp.name)/'link').symlink_to('/tmp')
        with self.assertRaises(ToolError): self.workspace.path('link/secret')
    def test_calculator(self):
        self.assertEqual(calculate('17 * 23'),391)
        for expression in ['__import__("os")','2 ** 999999', '1/0']:
            with self.assertRaises(ToolError): calculate(expression)
    def test_file_limits(self):
        with self.assertRaises(ToolError): self.workspace.execute({'action':'write_file','path':'x','content':'x'*32001})
    def test_unsupported_tool(self):
        with self.assertRaises(ToolError): self.workspace.execute({'action':'shell','command':'whoami'})

class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        webui.WORKSPACES=Path(self.tmp.name); webui.clients.clear(); webui.settings.clear()
        webui.app.config.update(TESTING=True, MOCK=True, API_KEY='')
        self.a=webui.app.test_client(); self.b=webui.app.test_client()
    def tearDown(self): self.tmp.cleanup()
    def post(self, client, path, **kw): return client.post(path, base_url='http://localhost', **kw)
    def test_chat_utf8_and_isolation(self):
        r=self.post(self.a,'/chat',json={'message':'₱ hello'}); self.assertIn('₱',r.get_data(as_text=True))
        self.post(self.b,'/chat',json={'message':'other'}).get_data()
        self.assertEqual(len(webui.clients),2)
        histories=[v['history'][0]['content'] for v in webui.clients.values()]
        self.assertCountEqual(histories,['₱ hello','other'])
    def test_public_fail_closed(self):
        with patch.object(webui,'PASSWORD',''):
            self.assertEqual(self.a.get('/',base_url='https://public.example').status_code,403)
    def test_login(self):
        with patch.object(webui,'PASSWORD','test-password'):
            self.assertEqual(self.post(self.a,'/chat',json={'message':'hi'}).status_code,401)
            self.assertEqual(self.post(self.a,'/login',data={'password':'test-password'}).status_code,302)
            r=self.post(self.a,'/chat',json={'message':'hi'}); self.assertEqual(r.status_code,200); r.get_data()
    def test_cross_origin(self):
        r=self.post(self.a,'/clear',headers={'Origin':'https://evil.example'})
        self.assertEqual(r.status_code,403)
    def test_key_destination(self):
        self.assertEqual(self.post(self.a,'/set_key',json={'url':'https://evil.example','key':'placeholder'}).status_code,400)
        gem='https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
        self.assertEqual(self.post(self.a,'/set_key',json={'url':gem}).status_code,400)
        self.assertEqual(self.post(self.a,'/set_key',json={'key':'AIzaTEST_ONLY_NOT_REAL'}).status_code,200)
        self.assertEqual(webui.settings['url_override'],gem)
        self.assertEqual(webui.settings['model_override'],'gemini-flash-latest')
    def test_debug_removed(self):
        self.assertEqual(self.a.get('/debug',base_url='http://localhost').status_code,404)
    def test_mock_task_honest(self):
        r=self.post(self.a,'/task',json={'message':'Make a file'})
        self.assertEqual(json.loads(r.get_data(as_text=True))['type'],'blocked')
    def test_task_end_to_end(self):
        webui.app.config.update(MOCK=False)
        class Reply:
            def __init__(self,a): self.a=a
            def __enter__(self): return self
            def __exit__(self,*_): pass
            def json(self): return {'choices':[{'message':{'content':json.dumps(self.a)}}]}
        actions=[{'action':'calculate','expression':'17*23'}, {'action':'write_file','path':'answer.txt','content':'391'},
                 {'action':'read_file','path':'answer.txt'}, {'action':'done','text':'Checked 391 in answer.txt'}]
        with patch.object(webui,'model_reply',side_effect=[Reply(a) for a in actions]):
            events=[json.loads(line) for line in self.post(self.a,'/task',json={'message':'calculate then save'}).get_data(as_text=True).splitlines()]
        self.assertEqual(events[-1]['type'],'done')
        self.assertEqual(self.a.get('/files/answer.txt',base_url='http://localhost').get_data(as_text=True),'391')
        self.assertEqual(self.b.get('/files/answer.txt',base_url='http://localhost').status_code,404)
    def test_clear_busy(self):
        self.a.get('/',base_url='http://localhost')
        c=next(iter(webui.clients.values()),None)
        self.post(self.a,'/chat',json={'message':'hi'}).get_data()
        c=next(iter(webui.clients.values())); c['busy'].acquire()
        try: self.assertEqual(self.post(self.a,'/clear').status_code,409)
        finally: c['busy'].release()
    def test_validation(self):
        for data in [{}, {'message':42}, {'message':'x'*12001}]:
            self.assertEqual(self.post(self.a,'/task',json=data).status_code,400)
        self.assertEqual(self.post(self.a,'/set_key',json={'key':42}).status_code,400)

if __name__=='__main__': unittest.main()
