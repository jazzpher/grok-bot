"""Deterministic output contracts: no live provider calls."""
import json
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch
from agent_loop import Workspace, ToolError
from documents import build_docx
from task_harness import TaskManager


def act(kind, **kw):
    return json.dumps({'action': kind, **kw})


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.m = TaskManager(self.root/'tasks.db', self.root/'ws')
        self.addCleanup(self.m.close)

    def wait(self, tid):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            row = self.m.get(tid, 'alice')
            if row['status'] not in ('queued', 'running') and tid not in self.m.threads:
                return row
            time.sleep(.01)
        self.fail('worker did not settle')

    def test_docx_extra_text_fails_exact_check(self):
        ws = Workspace(self.root/'ws/alice')
        ws.write_docx('out.docx', 'hello\nextra')
        status, _ = self.m._verify({'checks':[{'type':'file_text','path':'out.docx','text':'hello'}]}, {}, ws)
        self.assertEqual(status, 'check')

    def test_irrelevant_docx_completes_with_honest_review_caveat(self):
        replies = iter([act('write_docx', path='out.docx', content='unrelated'),
                        act('done', text='All requirements satisfied!')])
        tid = self.m.submit('alice', 'Write a Tagalog invitation', [], lambda _: next(replies))
        row = self.wait(tid)
        self.assertEqual(row['status'], 'completed')
        events = self.m.events(tid, 'alice')
        result = [e['text'] for e in events if e['type'] == 'result']
        self.assertEqual(len(result), 1)
        self.assertNotIn('All requirements satisfied', result[0])
        self.assertIn('not independently checked', result[0])

    def test_invalid_replies_do_not_use_steps_but_are_capped(self):
        calls = []
        tid = self.m.submit('alice', 'Write hello', [], lambda _: (calls.append(1), 'bad')[1], budgets={'steps': 1})
        row = self.wait(tid)
        self.assertEqual(len(calls), 6)
        self.assertEqual(row['status'], 'blocked')
        self.assertEqual(row['state']['steps'], 0)
        self.assertEqual(row['state']['invalid'], 6)

    def test_invalid_then_valid_within_two_step_budget(self):
        replies = iter(['bad', 'worse', act('write_docx', path='a.docx', content='Hello'), act('done', text='ok')])
        tid = self.m.submit('alice', 'Write hello', [], lambda _: next(replies), budgets={'steps': 2})
        row = self.wait(tid)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['state']['steps'], 2)

    def test_requirements_validation_and_owner_isolation(self):
        for requirements in (None, {}, '漢'*3000):
            with self.assertRaises(ValueError):
                self.m.submit('alice', 'goal', [], lambda _: '', requirements=requirements)
        tid = self.m.submit('alice', 'goal', [], lambda _: act('need_input', text='date?'), requirements='French, concise')
        self.wait(tid)
        self.assertIsNone(self.m.get(tid, 'bob'))
        self.assertFalse(self.m.resume(tid, 'bob', lambda _: ''))

    def test_atomic_replace_failure_and_invalid_docx_preserve_original(self):
        ws = Workspace(self.root/'ws/alice')
        ws.write_docx('out.docx', 'original')
        path = ws.root/'out.docx'
        original = path.read_bytes()
        for target, kwargs in [('agent_loop.extract_text', {'side_effect':ValueError('invalid zip')}),
                               ('agent_loop.os.replace', {'side_effect':OSError('disk error')})]:
            with patch(target, **kwargs), self.assertRaises((ToolError, OSError)):
                ws.write_docx('out.docx', 'replacement')
            self.assertEqual(path.read_bytes(), original)
            self.assertIn('out.docx', ws.checked)
            self.assertEqual(list(ws.root.glob('.artifact-*')), [])

    def test_failed_docx_replacement_preserves_original(self):
        ws = Workspace(self.root/'ws/alice')
        path = ws.root/'out.docx'
        original = build_docx('original')
        path.write_bytes(original)
        with patch('agent_loop.extract_text', return_value='wrong'):
            with self.assertRaises(ToolError):
                ws.write_docx('out.docx', 'replacement')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(ws.written, set())
        self.assertEqual(list(ws.root.glob('.artifact-*')), [])

    def test_requirements_and_checks_reach_provider_after_compaction_restart(self):
        seen = []
        checks = [{'type':'file_text','path':'out.txt','text':'Kumusta'}]
        requirements = 'Language: Tagalog. Format: numbered list. Tone: warm. Include date.'
        tid = self.m.submit('alice', 'Create an invitation', [],
                            lambda msgs: (seen.append(json.dumps(msgs)), act('need_input',text='Which date?'))[1],
                            checks=checks, requirements=requirements, budgets={'context':7000})
        row = self.wait(tid)
        state = row['state']
        state['messages'].extend([{'role':'user','content':'old data '*1500}] * 3)
        self.m.save(tid, 'needs_input', state, {'type':'test','text':'force compaction'})
        self.m.close()
        self.m = TaskManager(self.root/'tasks.db', self.root/'ws')
        self.addCleanup(self.m.close)
        self.assertTrue(self.m.resume(tid, 'alice', lambda msgs: (seen.append(json.dumps(msgs)), act('need_input',text='Which venue?'))[1], answer='December 1'))
        self.wait(tid)
        for prompt in seen:
            self.assertIn(requirements, prompt)
            self.assertIn('Kumusta', prompt)
            self.assertIn('Create an invitation', prompt)
        self.assertIn('December 1', seen[-1])


if __name__ == '__main__':
    unittest.main()
