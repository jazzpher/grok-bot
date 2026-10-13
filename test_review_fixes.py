import json
import test_harness
from test_harness import action

import unittest

class ReviewFixTests(unittest.TestCase):
    setUp = test_harness.HarnessTests.setUp
    manager = test_harness.HarnessTests.manager
    wait = test_harness.HarnessTests.wait
    def test_missing_checks_resume_validates_persists_and_keeps_budgets(self):
        m=self.manager()
        tid=m.submit('alice','make output',[],lambda _:action('done',text='unchecked'),budgets={'steps':5})
        original=self.wait(m,tid); m.threads[tid].join(2)
        checks=[{'type':'file_text','path':'out','text':'ok'}]
        self.assertFalse(m.resume(tid,'bob',lambda _: '',checks=checks))
        with self.assertRaises(ValueError):
            m.resume(tid,'alice',lambda _: '',checks=[{'type':'file_text','path':'../out','text':'ok'}])
        self.assertEqual(m.get(tid,'alice')['checks'],[])
        replies=iter([action('write_file',path='out',content='ok'),action('read_file',path='out'),action('done',text='verified')])
        self.assertTrue(m.resume(tid,'alice',lambda _:next(replies),checks=checks))
        row=self.wait(m,tid)
        self.assertEqual(row['status'],'completed')
        self.assertEqual(row['checks'],checks)
        self.assertEqual(row['state']['steps'],4)
        self.assertEqual(row['state']['budgets'],original['state']['budgets'])
        m.close(); reopened=self.manager(); self.assertEqual(reopened.get(tid,'alice')['checks'],checks)

    def test_existing_checks_cannot_be_changed_on_resume(self):
        m=self.manager(); checks=[{'type':'file_text','path':'out','text':'ok'}]
        tid=m.submit('alice','wait',[],lambda _:action('need_input',text='question'),checks=checks)
        self.wait(m,tid); m.threads[tid].join(2)
        with self.assertRaises(ValueError): m.resume(tid,'alice',lambda _: '',checks=[{'type':'file_text','path':'out','text':'bad'}])
        self.assertEqual(m.get(tid,'alice')['checks'],checks)

    def test_checks_resume_race_only_one_contract_is_admitted(self):
        import concurrent.futures, threading
        m=self.manager(); tid=m.submit('alice','wait',[],lambda _:action('need_input',text='checks'))
        self.wait(m,tid); m.threads[tid].join(2)
        gate=threading.Event()
        def provider(_): gate.wait(2); return action('need_input',text='wait')
        checks=[{'type':'file_text','path':'out','text':'ok'}]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:m.resume(tid,'alice',provider,checks=checks),range(8)))
        self.assertEqual(results.count(True),1); self.assertEqual(m.get(tid,'alice')['checks'],checks)
        gate.set(); self.wait(m,tid)

    def test_import_without_fcntl_uses_windows_exclusive_lock(self):
        import subprocess, sys
        script='''
import builtins, sys, types, tempfile, pathlib
original=builtins.__import__
def importing(name,*args,**kw):
    if name=='fcntl': raise ModuleNotFoundError('simulated Windows')
    return original(name,*args,**kw)
builtins.__import__=importing
locks=set()
def locking(fd,mode,count):
    if fd in locks: raise OSError('busy')
    if locks: raise OSError('busy')
    locks.add(fd)
sys.modules['msvcrt']=types.SimpleNamespace(locking=locking,LK_NBLCK=1)
from task_harness import TaskManager
with tempfile.TemporaryDirectory() as tmp:
    p=pathlib.Path(tmp); m=TaskManager(p/'db',p/'w')
    try:
        TaskManager(p/'db',p/'w')
        raise AssertionError('second owner admitted')
    except RuntimeError: pass
    assert locks
    m.close()
print('PASS simulated Windows import and exclusivity')
'''
        result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_verified_answer_published_and_unverified_answer_hidden(self):
        m=self.manager()
        p=self.root/'workspaces/alice'; p.mkdir(parents=True); (p/'out').write_text('ok')
        checks=[{'type':'file_text','path':'out','text':'ok'}]
        tid=m.submit('alice','verify',[],lambda _:action('done',text='FINAL_SENTINEL'),checks=checks)
        self.assertEqual(self.wait(m,tid)['status'],'completed')
        self.assertIn({'type':'result','text':'FINAL_SENTINEL','step':1},[{k:v for k,v in e.items() if k!='seq'} for e in m.events(tid,'alice')])
        tid=m.submit('alice','verify',[],lambda _:action('done',text='UNCHECKED'),checks=[],budgets={'steps':2})
        self.wait(m,tid)
        self.assertFalse(any(e['type']=='result' for e in m.events(tid,'alice')))
