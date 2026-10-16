"""Word (.docx) tool and invalid-action budget. Providers are scripted fakes."""
import io
import json
import pathlib
import tempfile
import time
import unittest
import zipfile

from agent_loop import Workspace, ToolError, run_loop, INVALID_HELP, MAX_INVALID
from documents import build_docx, extract_text, docx_paragraphs
from task_harness import TaskManager

LETTER = '# Office of the Mayor\nCity of Marisol, Republic of Veloria\n\nDear Ms. Lena Okafor,\nThanks & regards <fictional> "ok"\nHon. Rafael Dumont'


def act(action, **kw):
    base = {'action': action, 'text': '', 'path': '', 'source_path': '', 'content': '', 'expression': ''}
    base.update(kw)
    return json.dumps(base)


class BuildTests(unittest.TestCase):
    def test_round_trip_and_escaping(self):
        raw = build_docx(LETTER)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(sorted(z.namelist()), ['[Content_Types].xml', '_rels/.rels', 'word/document.xml'])
        text = extract_text('x.docx', raw)
        self.assertIn('Thanks & regards <fictional> "ok"', text)
        self.assertTrue(text.startswith('Office of the Mayor\n'))

    def test_unicode_and_deterministic(self):
        self.assertEqual(build_docx('Ünïcode 日本語 ✓'), build_docx('Ünïcode 日本語 ✓'))
        self.assertEqual(extract_text('x.docx', build_docx('Ünïcode 日本語 ✓')), 'Ünïcode 日本語 ✓')

    def test_markdown_headings_and_bold(self):
        text = ('# Rep\n## Office of Examples\n### Division\n#### Unit\n'
                'This is **important** and this is not.\n | *** FBI OFFICIAL *** |')
        raw = build_docx(text)
        self.assertEqual(extract_text('x.docx', raw), '\n'.join(docx_paragraphs(text)))
        got = extract_text('x.docx', raw)
        self.assertIn(' | *** FBI OFFICIAL *** |', got)  # ascii art must not be touched
        self.assertNotIn('**important**', got)
        import re
        xml = zipfile.ZipFile(io.BytesIO(raw)).read('word/document.xml').decode()
        # heading sizes: 16/14/12/11 pt, all bold
        for size in ('32', '28', '24', '22'):
            self.assertIn('<w:rPr><w:b/><w:sz w:val="%s"/></w:rPr>' % size, xml)
        # **important** is its own bold run between plain runs
        self.assertIn('<w:t xml:space="preserve">This is </w:t></w:r><w:r><w:rPr><w:b/></w:rPr>'
                      '<w:t xml:space="preserve">important</w:t></w:r><w:r><w:t xml:space="preserve">', xml)

    def test_rejects_empty_and_huge(self):
        for bad in ('', '   \n', None):
            with self.assertRaises(ValueError):
                build_docx(bad)
        with self.assertRaises(ValueError):
            build_docx('\n' * 600)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(pathlib.Path(self.tmp.name) / 'ws')

    def test_write_is_verified_and_readable(self):
        out = self.ws.execute({'action': 'write_docx', 'path': 'letter.docx', 'content': LETTER})
        self.assertIn('Verified', out)
        self.assertIn('letter.docx', self.ws.checked)
        self.assertEqual(self.ws.written - self.ws.checked, set())
        self.assertIn('Dear Ms. Lena Okafor,', self.ws.execute({'action': 'read_file', 'path': 'letter.docx'}))
        self.assertIn('letter.docx', self.ws.execute({'action': 'list_files'}))

    def test_rejections(self):
        for path, content in (('letter.txt', LETTER), ('../x.docx', LETTER), ('/abs.docx', LETTER), ('a.docx', ''), ('uploads/a.docx', LETTER)):
            with self.assertRaises(ToolError, msg=path):
                self.ws.execute({'action': 'write_docx', 'path': path, 'content': content})
        self.assertEqual(self.ws.written, set())

    def test_control_characters_do_not_break_verification(self):
        self.ws.execute({'action': 'write_docx', 'path': 'c.docx', 'content': 'a\x00b\x0bc\nd'})
        self.assertEqual(extract_text('c.docx', (self.ws.root / 'c.docx').read_bytes()), 'abc\nd')


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.m = TaskManager(self.root / 't.db', self.root / 'w'); self.addCleanup(self.m.close)

    def run_script(self, replies, **kw):
        it = iter(replies)
        tid = self.m.submit('alice', 'Write a sample letter as Word docx', [], lambda _msgs: next(it), **kw)
        end = time.monotonic() + 8
        while time.monotonic() < end:
            row = self.m.get(tid, 'alice')
            if row['status'] not in ('queued', 'running'):
                return tid, row
            time.sleep(.01)
        self.fail('task did not finish')

    def events(self, tid):
        return [json.loads(r[0]) for r in self.m.db.execute('SELECT payload FROM events WHERE task=? ORDER BY seq', (tid,))]

    def test_invalid_replies_do_not_burn_steps_and_docx_completes(self):
        replies = ['nope', '[]', '{"action":"write_docx"}', 'Sure! here is a letter', '{"x":1}',
                   act('write_docx', path='letter.docx', content=LETTER), act('done', text='Saved letter.docx')]
        tid, row = self.run_script(replies, budgets={'steps': 3})
        self.assertEqual(row['status'], 'completed', row)
        self.assertEqual(row['state']['steps'], 2)
        self.assertEqual(row['state']['invalid'], 5)
        self.assertTrue((self.root / 'w/alice/letter.docx').is_file())
        self.assertIn({'type': 'file', 'text': 'letter.docx'}, self.events(tid))

    def test_too_many_invalid_blocks_with_clear_message(self):
        tid, row = self.run_script(['nope'] * (MAX_INVALID + 1))
        self.assertEqual(row['status'], 'blocked')
        self.assertEqual(row['state']['steps'], 0)
        self.assertIn('invalid actions', [e for e in self.events(tid) if e['type'] == 'blocked'][-1]['text'])

    def test_invalid_error_teaches_the_format(self):
        seen = []
        replies = iter(['nope', act('need_input', text='done')])
        def call(messages):
            seen.append(json.dumps(messages)); return next(replies)
        tid = self.m.submit('alice', 'goal', [], call)
        end = time.monotonic() + 5
        while self.m.get(tid, 'alice')['status'] in ('queued', 'running') and time.monotonic() < end:
            time.sleep(.01)
        self.assertIn('write_docx', seen[1])
        self.assertIn('Valid actions', seen[1])
        self.assertIn('Nothing was executed', seen[1])

    def test_explicit_docx_text_check(self):
        replies = [act('write_docx', path='l.docx', content=LETTER), act('done', text='ok')]
        it = iter(replies)
        tid = self.m.submit('alice', 'g', [], lambda _m: next(it), checks=[{'type': 'file_text', 'path': 'l.docx', 'text': extract_text('l.docx', build_docx(LETTER))}])
        end = time.monotonic() + 5
        while self.m.get(tid, 'alice')['status'] in ('queued', 'running') and time.monotonic() < end:
            time.sleep(.01)
        self.assertEqual(self.m.get(tid, 'alice')['status'], 'completed')

    def test_explicit_docx_check_fails_on_missing_text(self):
        replies = [act('write_docx', path='l.docx', content=LETTER), act('done', text='ok')] + [act('need_input', text='stuck')]
        it = iter(replies)
        tid = self.m.submit('alice', 'g', [], lambda _m: next(it), checks=[{'type': 'file_text', 'path': 'l.docx', 'text': 'NOT IN LETTER'}])
        end = time.monotonic() + 5
        while self.m.get(tid, 'alice')['status'] in ('queued', 'running') and time.monotonic() < end:
            time.sleep(.01)
        self.assertEqual(self.m.get(tid, 'alice')['status'], 'needs_input')

    def test_corrupt_docx_is_not_auto_completed(self):
        # A hand-written file with a .docx name is not a written-by-tool docx, so no auto check applies.
        replies = [act('write_file', path='fake.docx', content='plain text'), act('done', text='ok'), act('need_input', text='stuck')]
        tid, row = self.run_script(replies)
        self.assertNotEqual(row['status'], 'completed')
        self.assertFalse((self.root / 'w/alice/fake.docx').exists())


class LoopTests(unittest.TestCase):
    def test_run_loop_invalid_not_counted_and_docx_works(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        ws = Workspace(pathlib.Path(tmp.name) / 'ws')
        it = iter(['bad', 'bad', act('write_docx', path='a.docx', content='Hello'), act('done', text='ok')])
        events = list(run_loop('g', [], ws, lambda _m: next(it), lambda: False, 2))
        self.assertEqual(events[-1]['type'], 'done')
        self.assertTrue((ws.root / 'a.docx').is_file())

    def test_invalid_help_lists_every_action(self):
        from agent_loop import ACTION_FIELDS
        for name in ACTION_FIELDS:
            self.assertIn(name, INVALID_HELP)


if __name__ == '__main__':
    unittest.main()
