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
        self.assertEqual(self.store.path('exact-copy').stat().st_size, len(row['markdown'].encode('utf-8')))
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
    def test_failed_new_write_leaves_no_orphan(self):
        with patch('skills.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError): self.store.save('orphan', 'Orphan', BODY)
        self.assertFalse((self.store.root / 'orphan').exists())
        self.assertEqual(self.store.list(), [])

    def test_failed_metadata_write_rolls_back_skill_content(self):
        with patch.object(self.store, 'write_metadata', side_effect=OSError('metadata disk error')):
            with self.assertRaises(OSError): self.store.save('new-skill', 'New skill', BODY)
        self.assertFalse((self.store.root / 'new-skill').exists())

        old = self.store.save('legacy', 'Legacy', BODY)
        self.store.path('legacy').with_name('metadata.json').unlink()
        with patch.object(self.store, 'write_metadata', side_effect=OSError('metadata disk error')):
            with self.assertRaises(OSError):
                self.store.save('legacy', 'Legacy', BODY + '\\nChanged', old['revision'])
        self.assertEqual(self.store.read('legacy')['markdown'], old['markdown'])

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

    def test_curator_provenance_pin_and_legacy_safety(self):
        start = 5000
        with patch('skills.time.time', return_value=start):
            agent = self.store.save('agent-workflow', 'Agent workflow', BODY)
            self.store.save('retired-workflow', 'Retired workflow', BODY)
            self.store.save('manual-workflow', 'Manual workflow', BODY, created_by='user')
            self.store.set_pinned('agent-workflow', agent['revision'], True)
        with patch('skills.time.time', return_value=start + 40 * 86400):
            visible = {row['name']: row for row in self.store.list()}
            self.assertEqual(set(visible), {'manual-workflow', 'agent-workflow'})
            self.assertEqual(visible['agent-workflow']['status'], 'pinned')
            self.assertEqual(visible['manual-workflow']['status'], 'active')
            archived = {row['name']: row for row in self.store.list(True)}
            self.assertEqual(archived['retired-workflow']['status'], 'archived')
            with self.assertRaises(ValueError): self.store.use('retired-workflow')
            self.assertEqual(self.store.use('agent-workflow')['name'], 'agent-workflow')
        with patch('skills.time.time', return_value=start + 71 * 86400):
            self.assertEqual(self.store.curate('manual-workflow')['status'], 'active')
            self.store.set_pinned('agent-workflow', agent['revision'], False)
            self.assertEqual(self.store.curate('agent-workflow')['status'], 'archived')

    def test_secret_scan_boundaries_and_orphans_do_not_consume_capacity(self):
        self.store.save('legitimate', 'Normal workflow', BODY + '\\nPrefer risk-framework-tooling, task-0123456789abcdef and foo_sk-proj-abcdefghijklmnopqrstuvwx.')
        orphan = self.store.root / 'aaa-orphan'
        orphan.mkdir(parents=True)
        (orphan / 'SKILL.md').write_text('x' * 13000, encoding='utf-8')
        self.assertEqual([row['name'] for row in self.store.list()], ['legitimate'])
        self.store.save('next', 'Another workflow', BODY)
        with self.assertRaises(ValueError):
            self.store.save('secret', 'Credential workflow', BODY + '\\nToken: sk-abcdefghijklmnopqrstuvwx')
        for index, token in enumerate(('sk-proj-' + 'a' * 24, 'sk-ant-' + 'b' * 24)):
            with self.assertRaises(ValueError):
                self.store.save('provider-secret-' + str(index), 'Credential workflow', BODY + '\\nToken: ' + token)
        self.assertFalse((self.store.root / 'secret').exists())

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
        pinned = self.client.post('/skills/copy/pin', json={'revision': saved.json['revision'], 'pinned': True})
        self.assertEqual(pinned.status_code, 200)
        self.assertTrue(pinned.json['pinned'])
        self.assertEqual(pinned.json['created_by'], 'user')
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


class LargeSkills(unittest.TestCase):
    setUp = Skills.setUp
    tearDown = Skills.tearDown
    def seed(self, count=500, description='Workflow'):
        for i in range(count):
            name = f'workflow-{i:03d}'
            path = self.store.path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = '---\nname: ' + name + '\ndescription: ' + json.dumps(description) + '\n---\n' + BODY
            path.write_text(raw, encoding='utf-8')
            self.store.write_metadata(name, {'created': 1, 'last_used': time.time(), 'uses': 0,
                                            'archived': False, 'created_by': 'user', 'pinned': False})

    def test_500_and_501_edit_delete_and_isolation(self):
        self.assertEqual(MAX_SKILLS, 500)
        self.seed(499)
        row = self.store.save('last-slot', 'Last slot', BODY)
        self.assertEqual(len(self.store.list()), 500)
        with self.assertRaisesRegex(ValueError, r'limit reached \(500\)'):
            self.store.save('overflow', 'Overflow', BODY)
        self.assertFalse(self.store.path('overflow').exists())
        self.store.save('last-slot', 'Updated', BODY, row['revision'])
        self.store.delete('workflow-000', self.store.read('workflow-000')['revision'])
        self.store.save('replacement', 'Replacement', BODY)
        self.assertEqual(len(self.store.list()), 500)
        self.assertEqual(SkillStore(self.root/'workspaces'/'bob').list(), [])

    def test_archives_free_slots_and_restore_cannot_exceed_500(self):
        self.seed()
        meta = self.store.metadata('workflow-000')
        meta.update(created_by='agent', last_used=time.time() - 31 * 86400)
        self.store.write_metadata('workflow-000', meta)
        self.assertEqual(len(self.store.list()), 499)
        row = self.store.read('workflow-000')
        self.store.save('replacement', 'Replacement', BODY)
        self.assertEqual(len(self.store.list(True)), 501)
        self.assertEqual(len(self.store.list()), 500)
        with self.assertRaisesRegex(ValueError, 'before restoring'):
            self.store.restore('workflow-000', row['revision'])
        self.assertTrue(self.store.metadata('workflow-000')['archived'])
        self.store.delete('replacement', self.store.read('replacement')['revision'])
        self.store.restore('workflow-000', row['revision'])
        self.assertEqual(len(self.store.list()), 500)
        self.assertEqual(self.store.use('workflow-000')['name'], 'workflow-000')

    def test_large_index_bound_relevance_pin_recency_and_unicode(self):
        self.seed(description='漢' * 160)
        old = self.store.read('workflow-499')
        self.store.save('workflow-499', 'Spreadsheet reconciliation', BODY, old['revision'])
        pinned = self.store.read('workflow-001')
        self.store.set_pinned('workflow-001', pinned['revision'], True)
        rendered = self.store.index_context('Spreadsheet')
        self.assertLessEqual(len(rendered.encode('utf-8')), 6000)
        rows = json.loads(rendered.split('\n')[1])
        self.assertLessEqual(len(rows), 30)
        self.assertEqual(rows[0]['name'], 'workflow-499')
        self.assertEqual(rows[1]['name'], 'workflow-001')
        self.assertNotIn(BODY, rendered)
        self.assertIn('Other skills remain saved', rendered)
        self.assertEqual(self.store.use('workflow-200')['name'], 'workflow-200')
        recent = self.store.index_context()
        self.assertEqual(json.loads(recent.split('\n')[1])[1]['name'], 'workflow-200')

    def test_bounded_paging_search_validation_and_all_names_reachable(self):
        self.seed(description='漢' * 160)
        ws = Workspace(self.root/'workspaces'/'alice')
        names = []; offset = 0
        while offset is not None:
            output = ws.execute({'action': 'list_skills', 'path': str(offset)})
            self.assertLessEqual(len(output.encode('utf-8')), 6000)
            page = json.loads(output)
            self.assertLessEqual(len(page['skills']), 20)
            names.extend(r['name'] for r in page['skills'])
            offset = page['next_offset']
        self.assertEqual(len(names), 500)
        self.assertEqual(len(set(names)), 500)
        page = self.store.list_page('workflow-499')
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['skills'][0]['name'], 'workflow-499')
        self.assertEqual(self.store.list_page('missing-word')['skills'], [])
        for offset in ('-1', '501', 'abc', 2, True):
            with self.assertRaises(ValueError):
                ws.execute({'action': 'list_skills', 'path': offset})
        for query in (None, ['x'], 'x' * 501):
            with self.assertRaises(ValueError): self.store.list_page(query)
        self.assertEqual(self.store.list_page(offset=500)['next_offset'], None)


    def test_large_library_http_count_and_model_task_context(self):
        self.seed()
        import webui
        old_root, old_tasks = webui.WORKSPACES, webui.TASKS
        try:
            webui.WORKSPACES = self.root/'workspaces'
            webui.TASKS = None
            webui.clients.clear()
            client = webui.app.test_client()
            with client.session_transaction() as session:
                session['sid'] = 'alice'
            # Resolve the actual session workspace rather than assume the cookie key.
            with client:
                client.get('/skills')
                actual = webui.client()['workspace'].skills
                self.assertEqual(len(actual.list()), 500)
                self.assertEqual(len(client.get('/skills').json), 500)
        finally:
            webui.clients.clear(); webui.WORKSPACES = old_root
            if webui.TASKS: webui.TASKS.close()
            webui.TASKS = old_tasks
        manager = TaskManager(self.root/'tasks.sqlite3', self.root/'workspaces')
        seen = []
        def provider(messages):
            seen.append(messages)
            return json.dumps({'action': 'need_input', 'text': 'Test reached provider'})
        try:
            tid = manager.submit('alice', 'Use workflow-499', [], provider)
            for _ in range(200):
                if manager.get(tid, 'alice')['status'] not in ('queued', 'running'): break
                time.sleep(.01)
            self.assertEqual(manager.get(tid, 'alice')['status'], 'needs_input')
            self.assertTrue(seen)
            self.assertLess(len(json.dumps(seen[0], ensure_ascii=False)), 24000)
            self.assertIn('workflow-499', seen[0][0]['content'])
        finally:
            manager.close()

if __name__=='__main__': unittest.main()
