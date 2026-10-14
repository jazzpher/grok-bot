import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from skills import SkillStore, MAX_SKILLS
from agent_loop import Workspace, parse_action
from task_harness import TaskManager

BODY = '## When to use\nExact text copies.\n## Steps\n1. copy_file to a different target.\n2. Read target.\n## Pitfalls\nDo not regenerate text.\n## Verification\nUse compare_files; only MATCH is verified.'

class Skills(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SkillStore(self.root / 'workspaces' / 'alice')
    def tearDown(self): self.temp.cleanup()
    def test_persistence_and_unicode(self):
        row = self.store.save('exact-copy', 'Copy without pagbabago', BODY)
        self.assertIn('SKILL.md', str(self.store.path('exact-copy')))
        self.assertEqual(SkillStore(self.root/'workspaces'/'alice').read('exact-copy'), row)
        self.assertEqual(row['content'].strip(), BODY)
        self.assertNotIn(BODY, self.store.index_context())
    def test_edit_conflict_and_delete(self):
        row = self.store.save('copy', 'Copies', BODY)
        with self.assertRaises(ValueError): self.store.save('copy','Copies','changed')
        new = self.store.save('copy', 'Copies', BODY+'\nUpdated', row['revision'])
        with self.assertRaises(ValueError): self.store.delete('copy', row['revision'])
        self.store.delete('copy', new['revision']); self.assertEqual(self.store.list(), [])
    def test_session_isolation_and_workspace_cannot_write_skills(self):
        self.store.save('copy','Copies',BODY)
        self.assertEqual(SkillStore(self.root/'workspaces'/'bob').list(), [])
        ws = Workspace(self.root/'workspaces'/'alice')
        with self.assertRaises(ValueError): ws.execute({'action':'write_file','path':'../.skills/alice/copy/SKILL.md','content':'bad'})
        self.assertEqual(json.loads(ws.execute({'action':'list_files'})), [])
    def test_invalid_inputs_limits_and_secrets(self):
        for name in ('../escape', 'Upper', 'a/b', '', 'a'*65, None):
            with self.assertRaises(ValueError): self.store.save(name,'desc',BODY)
        for desc, body in [('',BODY), ('x'*161,BODY), ('desc',''), ('desc','é'*12000), ('desc','AIza'+'A'*32), ('desc','-----BEGIN RSA PRIVATE KEY-----')]:
            with self.assertRaises(ValueError): self.store.save('test',desc,body)
        for i in range(MAX_SKILLS): self.store.save('skill'+str(i),'Desc',BODY)
        with self.assertRaises(ValueError): self.store.save('extra','Desc',BODY)
    def test_symlinks(self):
        outside = self.root/'outside'; outside.mkdir()
        self.store.root.parent.mkdir(parents=True)
        try:
            self.store.root.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks require platform privileges.')
        with self.assertRaises(ValueError): self.store.save('copy','Copies',BODY)
        self.assertEqual(list(outside.iterdir()), [])
    def test_atomic_failed_write(self):
        row=self.store.save('copy','Copies',BODY)
        with patch('skills.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError): self.store.save('copy','Copies','changed',row['revision'])
        self.assertEqual(self.store.read('copy'),row)
    def test_curator_boundaries_usage_archive_restore(self):
        with patch('skills.time.time', return_value=1000):
            row = self.store.save('copy', 'Copies', BODY)
        with patch('skills.time.time', return_value=1000 + 14 * 86400 - 1):
            self.assertEqual(self.store.list()[0]['status'], 'active')
        with patch('skills.time.time', return_value=1000 + 14 * 86400):
            self.assertEqual(self.store.list()[0]['status'], 'stale')
            self.store.read('copy')  # Editor views do not count as use.
            self.assertEqual(self.store.metadata('copy')['uses'], 0)
        with patch('skills.time.time', return_value=1000 + 30 * 86400):
            self.assertEqual(self.store.list(), [])
            self.assertEqual(self.store.list(True)[0]['status'], 'archived')
            self.assertTrue(self.store.path('copy').exists())
            with self.assertRaises(ValueError): self.store.use('copy')
            self.store.restore('copy', row['revision'])
            self.assertEqual(self.store.list()[0]['status'], 'active')
            self.store.use('copy')
            self.assertEqual(self.store.metadata('copy')['uses'], 1)
        self.assertEqual(self.store.read('copy')['markdown'], row['markdown'])

    def test_metadata_and_temp_symlinks(self):
        self.store.save('copy', 'Copies', BODY)
        path = self.store.path('copy')
        outside = self.root / 'outside'; outside.write_text('untouched')
        try:
            path.with_suffix('.tmp').symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks require platform privileges.')
        row = self.store.read('copy')
        with self.assertRaises(ValueError): self.store.save('copy', 'Copies', BODY, row['revision'])
        path.with_suffix('.tmp').unlink()
        path.with_name('metadata.json').unlink()
        path.with_name('metadata.json').symlink_to(outside)
        with self.assertRaises(ValueError): self.store.use('copy')
        self.assertEqual(outside.read_text(), 'untouched')

    def test_tools_and_parse(self):
        ws=Workspace(self.root/'workspaces'/'alice')
        action={'action':'save_skill','path':'copy','text':'Copies','content':BODY,'source_path':''}
        self.assertIn('read-back verified',ws.execute(parse_action(json.dumps(action))))
        self.assertIn('copy',ws.execute({'action':'list_skills'}))
        self.assertIn(BODY.splitlines()[0],ws.execute({'action':'read_skill','path':'copy'}))
    def wait(self, manager, tid):
        for _ in range(200):
            row=manager.get(tid,'alice')
            if row['status'] not in ('running','queued'): return row
            time.sleep(.01)
        self.fail('worker timed out')
    def test_harness_learn_then_reuse_restart(self):
        manager=TaskManager(self.root/'tasks.sqlite3',self.root/'workspaces')
        ws=Workspace(self.root/'workspaces'/'alice')
        actions=iter([{'action':'write_file','path':'out','content':'ok'}, {'action':'read_file','path':'out'},
                      {'action':'save_skill','path':'copy','text':'Copies','content':BODY,'source_path':''}, {'action':'done','text':'Saved copy'}])
        tid=manager.submit('alice','Make out and learn',[],lambda _:json.dumps(next(actions)),checks=[{'type':'file_text','path':'out','text':'ok'}])
        self.assertEqual(self.wait(manager,tid)['status'],'completed')
        self.assertTrue(any('Saved skill copy' in e.get('text','') for e in manager.events(tid,'alice')))
        manager.close(); manager=TaskManager(self.root/'tasks.sqlite3',self.root/'workspaces')
        seen=[]; actions=iter([{'action':'read_skill','path':'copy'}, {'action':'done','text':'Used copy'}])
        def provider(messages): seen.append(json.loads(json.dumps(messages))); return json.dumps(next(actions))
        tid=manager.submit('alice','Reuse copy',[],provider,checks=[{'type':'file_text','path':'out','text':'ok'}])
        self.assertEqual(self.wait(manager,tid)['status'],'completed')
        self.assertIn('copy',seen[0][0]['content']); self.assertIn('## Pitfalls',seen[1][-1]['content'])
        manager.close()
    def test_unverified_learning_blocked(self):
        manager=TaskManager(self.root/'tasks.sqlite3',self.root/'workspaces')
        actions=iter([{'action':'save_skill','path':'bad','text':'Bad','content':BODY,'source_path':''},{'action':'need_input','text':'Need check'}])
        tid=manager.submit('alice','learn',[],lambda _:json.dumps(next(actions)))
        self.wait(manager,tid); self.assertEqual(self.store.list(),[])
        self.assertTrue(any('Verify task artifacts' in e.get('text','') for e in manager.events(tid,'alice')))
        manager.close()
    def test_interrupted_save_reconcile_no_replay(self):
        manager=TaskManager(self.root/'tasks.sqlite3',self.root/'workspaces')
        ws=Workspace(self.root/'workspaces'/'alice')
        a={'action':'save_skill','path':'copy','text':'Copies','content':BODY,'source_path':''}
        state={'pending':a,'written':[],'checked':[],'messages':[]}
        with self.assertRaises(ValueError): manager._reconcile('missing',state,ws)
        ws.execute(a)
        manager._reconcile('missing',state,ws); self.assertIsNone(state['pending'])
        manager.close()

class SkillsHTTP(unittest.TestCase):
    def setUp(self):
        import webui
        self.webui=webui; self.temp=tempfile.TemporaryDirectory()
        self.old_root=webui.WORKSPACES
        self.old_tasks=webui.TASKS
        webui.TASKS=None
        webui.WORKSPACES=Path(self.temp.name)/'workspaces'
        webui.clients.clear(); self.client=webui.app.test_client()
    def tearDown(self):
        self.webui.clients.clear(); self.webui.WORKSPACES=self.old_root
        if self.webui.TASKS: self.webui.TASKS.close()
        self.webui.TASKS=self.old_tasks; self.temp.cleanup()
    def test_crud_and_isolation(self):
        data={'name':'copy','description':'Copies','content':BODY}
        saved=self.client.post('/skills',json=data); self.assertEqual(saved.status_code,200)
        self.assertEqual(len(self.client.get('/skills').json),1)
        self.assertEqual(self.client.get('/skills/copy').json['content'].strip(),BODY)
        self.assertEqual(self.webui.app.test_client().get('/skills').json,[])
        self.assertEqual(self.client.post('/skills',json=data).status_code,400)
        self.assertEqual(self.client.post('/skills/copy/restore',json={'revision':saved.json['revision']}).status_code,200)
        self.assertEqual(self.client.post('/skills/copy/delete',json={'revision':saved.json['revision']}).status_code,200)
        self.assertEqual(self.client.get('/skills/copy').status_code,404)
    def test_bad_requests_and_csrf(self):
        self.assertEqual(self.client.post('/skills',json=[]).status_code,400)
        self.assertEqual(self.client.post('/skills',json={'name':'bad'},headers={'Origin':'https://evil.invalid'}).status_code,403)
        self.assertEqual(self.client.post('/skills/copy/delete',json=[]).status_code,400)
        self.assertEqual(self.client.post('/skills',data='x'*19000).status_code,413)

if __name__=='__main__': unittest.main()
