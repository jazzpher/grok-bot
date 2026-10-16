"""Durable harness regressions; dummy providers, real SQLite and threads."""
import json
import os
import pathlib
import tempfile
import time
import unittest


def action(kind, **kw):
    return json.dumps({'action': kind, **kw})


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def manager(self):
        from task_harness import TaskManager
        m = TaskManager(self.root / 'tasks.db', self.root / 'workspaces')
        self.addCleanup(m.close)
        return m

    def wait(self, m, tid, owner='alice'):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            row = m.get(tid, owner)
            if row['status'] not in ('queued', 'running'):
                return row
            time.sleep(.01)
        self.fail('worker did not settle')

    def wait_worker_gone(self, m, tid):
        end = time.monotonic() + 5
        while tid in m.threads and time.monotonic() < end:
            time.sleep(.01)
        self.assertNotIn(tid, m.threads)

    def test_stop_resume_restores_counters_and_prevents_duplicate_writes(self):
        import threading
        m = self.manager()
        entered, release = threading.Event(), threading.Event()
        def provider(messages):
            entered.set(); release.wait(2)
            return action('write_file', path='out.txt', content='wrong')
        tid = m.submit('alice', 'Write hello', [], provider,
                       checks=[{'type': 'file_text', 'path': 'out.txt', 'text': 'hello'}])
        self.assertTrue(entered.wait(2))
        self.assertFalse(m.stop(tid, 'bob'))
        self.assertTrue(m.stop(tid, 'alice'))
        release.set()
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'stopped')
        self.assertFalse((self.root/'workspaces/alice/out.txt').exists())
        first_steps = row['state']['steps']
        replies = iter([action('write_file', path='out.txt', content='hello'),
                        action('read_file', path='out.txt'), action('done', text='ok')])
        self.assertTrue(m.resume(tid, 'alice', lambda _: next(replies)))
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['state']['steps'], first_steps + 3)
        self.assertFalse(m.resume(tid, 'alice', lambda _: action('done', text='ok')))

    def test_restart_reconciles_pending_write_without_replay(self):
        m = self.manager()
        tid = m.submit('alice', 'Write hello', [], lambda _: action('need_input', text='wait'),
                       checks=[{'type': 'file_text', 'path': 'out.txt', 'text': 'hello'}])
        row = self.wait(m, tid)
        s = row['state']
        s['pending'] = {'action': 'write_file', 'path': 'out.txt', 'content': 'hello'}
        m.save(tid, 'running', s, {'type': 'test', 'text': 'simulated crash boundary'})
        p = self.root/'workspaces/alice/out.txt'; p.write_bytes(b'hello')
        m.close()
        reopened = self.manager()
        self.assertEqual(reopened.get(tid, 'alice')['status'], 'interrupted')
        replies = iter([action('read_file', path='out.txt'), action('done', text='ok')])
        self.assertTrue(reopened.resume(tid, 'alice', lambda _: next(replies)))
        row = self.wait(reopened, tid)
        self.assertEqual(row['status'], 'completed')
        self.assertIsNone(row['state']['pending'])
        self.assertIn('out.txt', row['state']['written'])
        self.assertTrue(any(e['type'] == 'reconciled' for e in reopened.events(tid, 'alice')))

    def test_unverifiable_pending_write_blocks_not_replayed(self):
        m = self.manager()
        tid = m.submit('alice', 'Write hello', [], lambda _: action('need_input', text='wait'))
        row = self.wait(m, tid)
        s = row['state']; s['pending'] = {'action': 'write_file', 'path': 'x', 'content': 'secret'}
        m.save(tid, 'interrupted', s, {'type': 'test', 'text': 'crash'})
        calls = []
        self.wait_worker_gone(m, tid)
        self.assertTrue(m.resume(tid, 'alice', lambda _: calls.append(1)))
        self.assertEqual(self.wait(m, tid)['status'], 'blocked')
        self.assertEqual(calls, [])
        self.assertFalse((self.root/'workspaces/alice/x').exists())

    def test_completion_requires_explicit_supported_checks(self):
        m = self.manager()
        tid = m.submit('alice', 'Make it beautiful', [], lambda _: action('done', text='beautiful'))
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed')
        self.assertIn('not independently verified', [e['text'] for e in m.events(tid, 'alice') if e['type'] == 'verification'][0])
        with self.assertRaises(ValueError):
            m.submit('bob', 'anything', [], lambda _: '', checks=[{'type': 'semantic', 'text': 'beautiful'}])


    def test_done_with_unverified_artifact_bounces_once_then_completes(self):
        m = self.manager()
        replies = iter([action('write_file', path='out.txt', content='hello'),
                        action('read_file', path='out.txt'),
                        action('done', text='wrote it'),
                        action('done', text='verified: out.txt says hello')])
        tid = m.submit('alice', 'Write hello to out.txt', [], lambda _: next(replies))
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed')
        texts = [e['text'] for e in m.events(tid, 'alice') if e['type'] == 'check']
        self.assertTrue(any('Verify your own output' in t for t in texts), texts)
        verif = [e['text'] for e in m.events(tid, 'alice') if e['type'] == 'verification']
        self.assertTrue(any('self-verification round' in t for t in verif), verif)
        self.assertEqual(row['state'].get('self_verify'), 1)

    def test_done_without_self_verification_is_returned_not_prompted(self):
        m = self.manager()
        replies = iter([action('write_file', path='out.txt', content='hello'),
                        action('done', text='wrote it'),
                        action('read_file', path='out.txt'),
                        action('done', text='wrote it again'),
                        action('done', text='verified: out.txt says hello')])
        tid = m.submit('alice', 'Write hello to out.txt', [], lambda _: next(replies))
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed')
        statuses = [e['type'] for e in m.events(tid, 'alice')]
        self.assertIn('check', statuses)
        self.assertNotIn('needs_input', statuses)

    def test_exact_copy_check_cannot_target_uploads_case_variant(self):
        m = self.manager()
        from agent_loop import Workspace
        workspace = Workspace(m.workspaces / 'alice')
        for path in ('Uploads/output.txt', 'uploads./output.txt', 'uploads /output.txt', '. /uploads/output.txt'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                m.validate_checks([{'type': 'exact_copy', 'source_path': 'source.txt', 'path': path}], workspace)

    def test_worker_passes_remaining_output_budget_to_provider(self):
        m = self.manager(); limits = []
        def provider(messages, output_limit):
            limits.append(output_limit)
            return action('need_input', text='wait')
        tid = m.submit('alice', 'pause', [], provider, budgets={'output': 1000})
        self.assertEqual(self.wait(m, tid)['status'], 'needs_input')
        self.assertEqual(limits, [1000])

    def test_file_tool_output_does_not_overrun_output_budget(self):
        m = self.manager()
        workspace = self.root/'workspaces/alice'; workspace.mkdir(parents=True)
        (workspace/'large.txt').write_text('x' * 5000)
        tid = m.submit('alice', 'read the file', [], lambda _: action('read_file', path='large.txt'),
                       budgets={'output': 200})
        row = self.wait(m, tid)
        self.assertLessEqual(row['state']['output'], row['state']['budgets']['output'])
        self.assertNotIn('x' * 100, json.dumps(row['state']['messages']))

    def test_worker_bookkeeping_is_released_after_task_finishes(self):
        m = self.manager()
        tid = m.submit('alice', 'pause', [], lambda _: action('need_input', text='wait'))
        self.assertEqual(self.wait(m, tid)['status'], 'needs_input')
        self.wait_worker_gone(m, tid)
        self.assertNotIn(tid, m.stops)

    def test_resume_normalizes_persisted_evidence_paths(self):
        m = self.manager()
        workspace = self.root/'workspaces/alice'
        (workspace/'nested').mkdir(parents=True)
        (workspace/'nested/output.txt').write_text('verified')
        tid = m.submit('alice', 'verify output', [], lambda _: action('need_input', text='checks needed'))
        row = self.wait(m, tid); self.wait_worker_gone(m, tid)
        self.assertIsNotNone(row)
        state = row['state']
        state['written'] = ['nested\\output.txt' if os.name == 'nt' else 'nested/output.txt']
        state['checked'] = []
        m.save(tid, 'needs_input', state, {'type': 'status', 'text': 'legacy path shape'})
        replies = iter([action('read_file', path='nested/output.txt'), action('done', text='Verified')])
        checks = [{'type': 'file_text', 'path': 'nested/output.txt', 'text': 'verified'}]
        self.assertTrue(m.resume(tid, 'alice', lambda _: next(replies), answer='Run checks', checks=checks))
        result = self.wait(m, tid)
        self.assertIsNotNone(result)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['state']['written'], ['nested/output.txt'])

    def test_invalid_persisted_evidence_path_blocks_and_releases_owner(self):
        m = self.manager()
        tid = m.submit('alice', 'verify output', [], lambda _: action('need_input', text='checks needed'))
        row = self.wait(m, tid); self.wait_worker_gone(m, tid)
        state = row['state']
        state['written'] = ['../outside.txt']
        m.save(tid, 'needs_input', state, {'type': 'status', 'text': 'invalid persisted path'})
        self.assertTrue(m.resume(tid, 'alice', lambda _: action('done', text='Done'), answer='Continue'))
        result = self.wait(m, tid)
        self.assertIsNotNone(result)
        self.wait_worker_gone(m, tid)
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(m.busy('alice'))
        self.assertFalse(m.stop(tid, 'alice'))
        self.assertNotIn(tid, m.stops)

    def test_owner_concurrency_is_reserved_atomically(self):
        import threading
        m = self.manager(); release = threading.Event()
        tid = m.submit('alice', 'Wait', [], lambda _: (release.wait(2), action('need_input', text='wait'))[1])
        with self.assertRaises(RuntimeError):
            m.submit('alice', 'Other', [], lambda _: action('done', text='ok'))
        self.assertFalse(m.resume(tid, 'alice', lambda _: ''))
        release.set(); self.wait(m, tid)

    def test_transient_retry_and_quota_fail_closed(self):
        import requests
        m = self.manager(); calls = []
        def transient(messages):
            calls.append(1)
            if len(calls) < 3:
                raise requests.ConnectionError('dummy credential must not be stored')
            return action('need_input', text='wait')
        tid = m.submit('alice', 'Wait', [], transient)
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'needs_input')
        self.assertEqual(row['state']['steps'], 3)
        self.assertEqual(len(calls), 3)
        self.assertNotIn('dummy credential', json.dumps(row))
        for status in (401, 403, 429, 400):
            attempts = []
            def denied(messages):
                attempts.append(1)
                response = requests.Response(); response.status_code = status
                raise requests.HTTPError('sensitive error', response=response)
            tid = m.submit('alice', 'Wait', [], denied)
            self.assertEqual(self.wait(m, tid)['status'], 'blocked')
            self.assertEqual(len(attempts), 1)

    def test_budgets_and_compaction_retain_original_evidence(self):
        m = self.manager()
        tid = m.submit('alice', 'Wait', [], lambda _: action('plan', text='x'*2000),
                       budgets={'steps': 4, 'context': 6000})
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'budget_exhausted')
        self.assertEqual(row['state']['steps'], 4)
        self.assertTrue(any(e['type'] == 'compaction' for e in m.events(tid, 'alice')))
        self.assertGreater(len(json.dumps(row['state']['messages'])), 6000)
        tid = m.submit('bob', 'Wait', [], lambda _: 'x'*10000, budgets={'output': 1000})
        row = self.wait(m, tid, 'bob')
        self.assertEqual(row['status'], 'budget_exhausted')
        self.assertLessEqual(row['state']['output'], row['state']['budgets']['output'])
        tid = m.submit('carol', 'Wait', [], lambda _: (time.sleep(.05), action('write_file', path='x', content='no'))[1], budgets={'elapsed': .02})
        self.assertEqual(self.wait(m, tid, 'carol')['status'], 'budget_exhausted')
        self.assertFalse((self.root/'workspaces/carol/x').exists())
        with self.assertRaises(ValueError):
            m.submit('dave', 'Wait', [], lambda _: '', budgets={'steps': 10000})

    def test_malformed_actions_stay_bounded(self):
        m = self.manager()
        tid = m.submit('alice', 'Wait', [], lambda _: '[]', budgets={'steps': 3})
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'blocked')
        self.assertEqual(row['state']['steps'], 0)
        self.assertEqual(row['state']['invalid'], 6)
        self.assertEqual(row['state']['written'], [])

    def test_exact_copy_verification_preserves_bytes(self):
        m = self.manager(); p = self.root/'workspaces/alice'; p.mkdir(parents=True)
        (p/'source.txt').write_bytes(b'a\r\nb\rc\n')
        replies = iter([action('copy_file', source_path='source.txt', path='out.txt'),
                        action('read_file', path='out.txt'), action('done', text='ok')])
        tid = m.submit('alice', 'Copy source.txt exactly to out.txt', [], lambda _: next(replies))
        self.assertEqual(self.wait(m, tid)['status'], 'completed')
        self.assertEqual((p/'out.txt').read_bytes(), b'a\r\nb\rc\n')

    def test_retention_is_explicit_and_never_prunes_active_tasks(self):
        m = self.manager()
        tid = m.submit('alice', 'Wait', [], lambda _: action('need_input', text='wait'))
        self.wait(m, tid)
        m.store_history('alice', [{'role':'user','content':'hello'}])
        with m.lock, m.db:
            m.db.execute('UPDATE tasks SET updated=0 WHERE id=?', (tid,))
            m.db.execute('UPDATE sessions SET updated=0')
        self.assertEqual(m.prune(86400), 1)
        self.assertIsNone(m.get(tid, 'alice'))
        self.assertEqual(m.load_history('alice'), [])
        self.assertFalse(m.events(tid, 'alice'))

    def test_single_process_database_lock_and_worker_cap(self):
        import threading
        m = self.manager()
        with self.assertRaises(RuntimeError):
            self.manager()
        release = threading.Event()
        try:
            for i in range(4):
                m.submit('owner'+str(i), 'Wait', [], lambda _: (release.wait(2), action('need_input', text='wait'))[1])
            with self.assertRaises(RuntimeError):
                m.submit('extra', 'Wait', [], lambda _: '')
        finally: release.set()

    def test_resume_race_only_one_worker_and_budget_not_reset(self):
        import concurrent.futures
        m = self.manager()
        tid = m.submit('alice', 'Wait', [], lambda _: action('need_input', text='wait'), budgets={'steps':2})
        self.wait(m, tid); self.wait_worker_gone(m, tid)
        import threading
        gate=threading.Event()
        def provider(_): gate.wait(2); return action('plan', text='wait')
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _: m.resume(tid,'alice',provider), range(8)))
        self.assertEqual(results.count(True),1)
        gate.set()
        row=self.wait(m,tid)
        self.assertEqual(row['status'],'budget_exhausted')
        self.assertEqual(row['state']['steps'],2)

    def test_failed_artifact_check_never_completes(self):
        m=self.manager()
        tid=m.submit('alice','Write hello',[],lambda _: action('done',text='already done'),
                     checks=[{'type':'file_text','path':'out.txt','text':'hello'}], budgets={'steps':3})
        self.assertEqual(self.wait(m,tid)['status'],'budget_exhausted')

    def test_failure_fingerprint_ignores_unused_fields(self):
        m=self.manager(); calls=[]
        def provider(_):
            calls.append(1)
            return action('read_file',path='missing',text=str(len(calls)))
        tid=m.submit('alice','Read missing',[],provider)
        row=self.wait(m,tid)
        self.assertEqual(row['status'],'blocked')
        self.assertEqual(len(calls),3)
        self.assertEqual(row['state']['failure_count'],2)

    def test_document_system_context_never_overrides_task_protocol(self):
        m=self.manager(); seen=[]
        def provider(messages):
            seen.extend(messages); return action('need_input',text='wait')
        tid=m.submit('alice','Wait',[{'role':'system','content':'UNTRUSTED document boundary'}],provider)
        self.wait(m,tid)
        systems=[r['content'] for r in seen if r['role']=='system']
        self.assertEqual(len(systems),1)
        self.assertTrue(systems[0].endswith(__import__('agent_loop').RULES))

    def test_pending_copy_uses_original_snapshot_not_changed_source(self):
        import hashlib
        m=self.manager(); p=self.root/'workspaces/alice'; p.mkdir(parents=True)
        (p/'source').write_bytes(b'original')
        tid=m.submit('alice','Copy source exactly to out',[],lambda _: action('need_input',text='wait'))
        row=self.wait(m,tid); self.wait_worker_gone(m,tid)
        s=row['state']; s['pending']={'action':'copy_file','source_path':'source','path':'out'}
        s['pending_digest']=hashlib.sha256(b'original').hexdigest()
        (p/'source').write_bytes(b'changed'); (p/'out').write_bytes(b'changed')
        m.save(tid,'interrupted',s,{'type':'test','text':'crash boundary'})
        calls=[]
        self.assertTrue(m.resume(tid,'alice',lambda _:calls.append(1)))
        self.assertEqual(self.wait(m,tid)['status'],'blocked')
        self.assertEqual(calls,[])

    def test_plan_adds_user_feedback_before_next_provider_call(self):
        m=self.manager(); seen=[]
        def provider(messages):
            seen.append(json.loads(json.dumps(messages)))
            return action('plan',text='do it') if len(seen)==1 else action('need_input',text='wait')
        tid=m.submit('alice','Wait',[],provider)
        self.wait(m,tid)
        self.assertEqual(seen[1][-1]['role'],'user')
        self.assertIn('next action',seen[1][-1]['content'])

    def test_worker_verifies_artifact_and_persists_trajectory(self):
        m = self.manager()
        replies = iter([action('write_file', path='out.txt', content='hello'),
                        action('read_file', path='out.txt'), action('done', text='saved')])
        tid = m.submit('alice', 'Write hello', [], lambda _: next(replies),
                       checks=[{'type': 'file_text', 'path': 'out.txt', 'text': 'hello'}])
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['state']['steps'], 3)
        self.assertEqual(row['state']['written'], ['out.txt'])
        self.assertIn('hello', json.dumps(row['state']['messages']))
        self.assertTrue(any(e['type'] == 'verification' for e in m.events(tid, 'alice')))
        self.assertEqual((self.root/'workspaces/alice/out.txt').read_text(), 'hello')
        m.close()
        reopened = self.manager()
        self.assertEqual(reopened.get(tid, 'alice')['status'], 'completed')
        self.assertIsNone(reopened.get(tid, 'bob'))
        self.assertEqual(reopened.events(tid, 'bob'), [])

    def test_done_result_defaults_to_object_and_must_be_object(self):
        from agent_loop import parse_action, ToolError
        self.assertEqual(parse_action(action('done', text='ok'))['result'], {})
        with self.assertRaises(ToolError):
            parse_action(action('done', text='ok', result='nope'))

    def test_result_schema_input_is_validated(self):
        m = self.manager()
        for bad in ('summary', ['a', 5], [''] * 17, ['x' * 65]):
            with self.assertRaises(ValueError):
                m.submit('alice', 'g', [], lambda _: action('done', text='ok'), result_schema=bad)
        self.assertEqual(m.list('alice'), [])
        m.close()

    def test_result_schema_gates_done_with_refund(self):
        m = self.manager()
        replies = iter([
            action('write_file', path='out.txt', content='hello'),
            action('read_file', path='out.txt'),
            action('done', text='ok', result={'Title': 'hello'}),
            action('done', text='ok', result={'S U M-MARY.': 'hello', 'title': 'hi'}),
        ])
        tid = m.submit('alice', 'Write hello', [], lambda _: next(replies),
                       checks=[{'type': 'file_text', 'path': 'out.txt', 'text': 'hello'}],
                       result_schema=['summary'])
        row = self.wait(m, tid)
        self.wait_worker_gone(m, tid)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['state']['steps'], 3)
        self.assertEqual(row['state'].get('schema_rejects'), 1)
        events = m.events(tid, 'alice')
        self.assertTrue(any(e['type'] == 'check' and 'Result schema check failed' in e['text'] for e in events))
        self.assertTrue(any(e['type'] == 'verification' and e['text'].startswith('Result schema validated (summary).') for e in events))
        m.close()

    def test_result_schema_rejects_are_bounded(self):
        m = self.manager()
        replies = iter([action('write_file', path='out.txt', content='hello'),
                        action('read_file', path='out.txt')]
                       + [action('done', text='ok') for _ in range(4)])
        tid = m.submit('alice', 'Write hello', [], lambda _: next(replies),
                       checks=[{'type': 'file_text', 'path': 'out.txt', 'text': 'hello'}],
                       result_schema=['summary'])
        row = self.wait(m, tid)
        self.wait_worker_gone(m, tid)
        self.assertEqual(row['status'], 'blocked')
        self.assertEqual(row['state'].get('schema_rejects'), 4)
        self.assertIn('required result fields in 4 attempts', m.events(tid, 'alice')[-1]['text'])
        m.close()
