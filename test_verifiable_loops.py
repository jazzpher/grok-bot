"""Verifiable agent loops: pixel-diff verifier, threshold gating, durable-task
check types (pixel_diff, command) and the done-until-verified loop."""
import json
import os
import pathlib
import shutil
import tempfile
import time
import unittest

from agent_loop import Workspace
from grokbot.approvals import SandboxPolicy
from grokbot.tools import ToolContext, default_registry
from grokbot.visual import VisualVerifyError, diff_images


def action(kind, **kw):
    return json.dumps({'action': kind, **kw})


def make_image(path, size=(100, 100), color=(255, 255, 255), black_rect=None):
    """black_rect=(x, y, w, h) paints differing pixels deterministically."""
    from PIL import Image
    img = Image.new('RGB', size, color)
    if black_rect:
        x, y, w, h = black_rect
        for px in range(x, x + w):
            for py in range(y, y + h):
                img.putpixel((px, py), (0, 0, 0))
    img.save(path)
    return path


class PixelDiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def test_identical_images_have_zero_mismatch(self):
        a = make_image(self.root / 'a.png')
        b = make_image(self.root / 'b.png')
        result = diff_images(a, b)
        self.assertEqual(result['mismatch_ratio'], 0.0)
        self.assertEqual(result['different_pixels'], 0)
        self.assertEqual(result['compared_pixels'], 10000)
        self.assertTrue(result['size_match'])

    def test_known_mismatch_percent_is_exact(self):
        a = make_image(self.root / 'a.png')
        b = make_image(self.root / 'b.png', black_rect=(0, 0, 10, 10))   # 100 of 10000 px
        result = diff_images(a, b)
        self.assertEqual(result['different_pixels'], 100)
        self.assertEqual(result['mismatch_percent'], 1.0)

    def test_area_present_on_one_side_only_counts_as_difference(self):
        a = make_image(self.root / 'a.png', size=(100, 100))
        c = make_image(self.root / 'c.png', size=(100, 50))
        result = diff_images(a, c)
        self.assertFalse(result['size_match'])
        self.assertEqual(result['mismatch_percent'], 50.0)

    def test_tolerance_ignores_small_channel_differences(self):
        a = make_image(self.root / 'a.png')
        d = make_image(self.root / 'd.png', color=(250, 250, 250))
        self.assertGreater(diff_images(a, d)['different_pixels'], 0)
        self.assertEqual(diff_images(a, d, tolerance=5)['different_pixels'], 0)

    def test_ignore_region_is_excluded_from_both_counts(self):
        a = make_image(self.root / 'a.png')
        b = make_image(self.root / 'b.png', black_rect=(0, 0, 10, 10))
        result = diff_images(a, b, ignore=[[0, 0, 10, 10]])
        self.assertEqual(result['mismatch_ratio'], 0.0)
        self.assertEqual(result['ignored_pixels'], 100)
        self.assertEqual(result['compared_pixels'], 9900)

    def test_ignore_covering_everything_is_an_error_not_a_pass(self):
        a = make_image(self.root / 'a.png')
        with self.assertRaises(VisualVerifyError):
            diff_images(a, a, ignore=[[0, 0, 100, 100]])

    def test_diff_image_highlights_differing_pixels(self):
        from PIL import Image
        a = make_image(self.root / 'a.png')
        b = make_image(self.root / 'b.png', black_rect=(40, 40, 5, 5))
        diff_path = self.root / 'out.diff.png'
        diff_images(a, b, diff_path=diff_path)
        img = Image.open(diff_path).convert('RGB')
        self.assertEqual(img.getpixel((42, 42)), (255, 0, 0))
        self.assertNotEqual(img.getpixel((10, 10)), (255, 0, 0))

    def test_invalid_inputs_raise_clear_errors(self):
        a = make_image(self.root / 'a.png')
        with self.assertRaises(VisualVerifyError):
            diff_images(a, a, tolerance=300)
        with self.assertRaises(VisualVerifyError):
            diff_images(a, a, ignore=[[0, 0, -1, 5]])
        with self.assertRaises(VisualVerifyError):
            diff_images(a, a, ignore=[['0', 0, 5, 5]])
        with self.assertRaises(VisualVerifyError):
            diff_images(a, self.root / 'missing.png')
        (self.root / 'notimg.png').write_text('hello')
        with self.assertRaises(VisualVerifyError):
            diff_images(a, self.root / 'notimg.png')


class VerifyVisualToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(pathlib.Path(self.tmp.name) / 'ws')
        self.registry = default_registry()
        self.ctx = ToolContext(workspace=self.ws, session_id='s1',
                               policy=SandboxPolicy(mode='workspace-write'))

    def run_tool(self, **args):
        return self.registry.execute(self.ctx, 'verify_visual', args)

    def test_tool_is_registered_with_schema(self):
        self.assertIn('verify_visual', self.registry.names())
        spec = self.registry.get('verify_visual').openai()
        self.assertEqual(spec['type'], 'function')
        self.assertIn('path', spec['function']['parameters']['required'])

    def test_pass_when_within_threshold(self):
        make_image(self.ws.root / 'truth.png')
        make_image(self.ws.root / 'out.png', black_rect=(0, 0, 10, 10))  # 1.0%
        res = self.run_tool(path='out.png', source_path='truth.png', threshold=0.01)
        self.assertTrue(res.ok, res.output)
        self.assertIn('PASS pixel diff: 1.0000% mismatch (threshold 1.0000%)', res.output)
        self.assertTrue((self.ws.root / 'out.diff.png').is_file())

    def test_fail_when_above_threshold_and_reports_numbers(self):
        make_image(self.ws.root / 'truth.png')
        make_image(self.ws.root / 'out.png', black_rect=(0, 0, 10, 10))  # 1.0%
        res = self.run_tool(path='out.png', source_path='truth.png', threshold=0.005)
        self.assertTrue(res.ok, res.output)
        self.assertIn('FAIL pixel diff: 1.0000% mismatch (threshold 0.5000%)', res.output)
        self.assertIn('100 of 10000 pixels differ', res.output)
        self.assertIn('do not claim', res.output)

    def test_note_and_ignore_document_exemptions(self):
        make_image(self.ws.root / 'truth.png')
        make_image(self.ws.root / 'out.png', black_rect=(0, 0, 10, 10))
        res = self.run_tool(path='out.png', source_path='truth.png',
                            ignore=[[0, 0, 10, 10]], note='clock widget area')
        self.assertTrue(res.ok, res.output)
        self.assertIn('PASS', res.output)
        self.assertIn('Exemption note: clock widget area', res.output)

    def test_requires_exactly_one_ground_truth(self):
        make_image(self.ws.root / 'truth.png')
        make_image(self.ws.root / 'out.png')
        self.assertFalse(self.run_tool(path='out.png').ok)
        self.assertFalse(self.run_tool(path='out.png', source_path='truth.png',
                                       url='https://example.com').ok)

    def test_workspace_guards_and_validation(self):
        make_image(self.ws.root / 'truth.png')
        self.assertFalse(self.run_tool(path='../out.png', source_path='truth.png').ok)
        self.assertFalse(self.run_tool(path='out.png', source_path='truth.png', threshold=2).ok)
        self.assertFalse(self.run_tool(path='out.png', source_path='truth.png', tolerance=-1).ok)
        self.assertFalse(self.run_tool(path='out.png', source_path='truth.png', viewport='huge').ok)

    def test_url_ground_truth_needs_browser_flag(self):
        make_image(self.ws.root / 'out.png')
        res = self.run_tool(path='out.png', url='https://example.com')
        self.assertFalse(res.ok)
        self.assertIn('GROK_BROWSER_ENABLED', res.output)

    def test_html_side_needs_preview_flag(self):
        make_image(self.ws.root / 'truth.png')
        (self.ws.root / 'out.html').write_text('<html><body>hi</body></html>')
        res = self.run_tool(path='out.html', source_path='truth.png')
        self.assertFalse(res.ok)
        self.assertIn('GROK_PREVIEW', res.output)


class HarnessCheckTests(unittest.TestCase):
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
        end = time.monotonic() + 10
        while time.monotonic() < end:
            row = m.get(tid, owner)
            if row['status'] not in ('queued', 'running'):
                return row
            time.sleep(.01)
        self.fail('worker did not settle')

    def ws(self, owner='alice'):
        return self.root / 'workspaces' / owner

    def test_pixel_diff_gates_done_until_artifact_matches(self):
        m = self.manager()
        ws = self.ws()
        ws.mkdir(parents=True)
        make_image(ws / 'truth.png')
        make_image(ws / 'out.png', black_rect=(0, 0, 10, 10))            # 1% off
        calls = []

        def provider(messages):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                return action('done', text='looks done to me')           # verifier must refuse
            shutil.copy(ws / 'truth.png', ws / 'out.png')                # agent fixes the artifact
            return action('done', text='fixed and rechecked')

        tid = m.submit('alice', 'Match the ground truth image', [], provider,
                       checks=[{'type': 'pixel_diff', 'path': 'out.png', 'source_path': 'truth.png',
                                'max_diff': 0.005, 'note': 'font rendering'}])
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['state']['steps'], 2)                       # fail -> fix -> pass
        events = m.events(tid, 'alice')
        checks = [e['text'] for e in events if e['type'] == 'check']
        self.assertTrue(any('1.0000% mismatch exceeds the allowed 0.5000%' in t for t in checks), checks)
        self.assertTrue(any('font rendering' in t for t in checks))
        self.assertTrue(any(e['type'] == 'result' and 'Pixel-diff check passed' in e['text'] for e in events))
        self.assertTrue((ws / 'out.diff.png').is_file())                 # highlighted diff evidence

    def test_pixel_diff_done_blocked_forever_hits_budget_not_success(self):
        m = self.manager()
        ws = self.ws()
        ws.mkdir(parents=True)
        make_image(ws / 'truth.png')
        make_image(ws / 'out.png', black_rect=(0, 0, 20, 20))            # 4% > default 1%
        tid = m.submit('alice', 'Match the ground truth image', [],
                       lambda _: action('done', text='trust me'),
                       checks=[{'type': 'pixel_diff', 'path': 'out.png', 'source_path': 'truth.png'}],
                       # 4% mismatch vs the default 1% threshold: never passes
                       budgets={'steps': 3, 'elapsed': 60, 'output': 50000, 'context': 24000})
        row = self.wait(m, tid)
        self.assertNotEqual(row['status'], 'completed')
        self.assertEqual(row['state']['steps'], 3)

    def test_pixel_diff_missing_ground_truth_is_check_failure_not_crash(self):
        m = self.manager()
        ws = self.ws()
        ws.mkdir(parents=True)
        make_image(ws / 'out.png')
        tid = m.submit('alice', 'Match the ground truth image', [],
                       lambda _: action('done', text='done'),
                       checks=[{'type': 'pixel_diff', 'path': 'out.png', 'source_path': 'missing.png'}],
                       budgets={'steps': 2, 'elapsed': 60, 'output': 50000, 'context': 24000})
        row = self.wait(m, tid)
        self.assertNotEqual(row['status'], 'completed')
        events = m.events(tid, 'alice')
        self.assertTrue(any(e['type'] == 'check' and 'missing' in e['text'] for e in events))

    @unittest.skipUnless(os.name == 'posix' and shutil.which('python3'),
                         'command checks run through the POSIX sandbox backend')
    def test_command_check_gates_done_until_tests_pass(self):
        m = self.manager()
        ws = self.ws()
        replies = iter([
            action('write_file', path='out.txt', content='wrong'),
            action('read_file', path='out.txt'),
            action('done', text='written'),                              # command check must refuse
            action('write_file', path='out.txt', content='hello world'),
            action('read_file', path='out.txt'),
            action('done', text='fixed'),
        ])
        tid = m.submit('alice', 'Write hello world to out.txt', [], lambda _: next(replies),
                       checks=[{'type': 'command',
                                'command': 'python3 -c "import sys; sys.exit(0 if open(\'out.txt\').read() == \'hello world\' else 1)"'}])
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed')
        events = m.events(tid, 'alice')
        checks = [e['text'] for e in events if e['type'] == 'check']
        self.assertTrue(any('exited 1 (expected 0)' in t for t in checks), checks)
        self.assertTrue(any(e['type'] == 'result' and 'Command check passed' in e['text'] for e in events))

    @unittest.skipUnless(os.name == 'posix' and shutil.which('python3'),
                         'command checks run through the POSIX sandbox backend')
    def test_command_check_expect_output_substring(self):
        m = self.manager()
        self.ws().mkdir(parents=True)
        tid = m.submit('alice', 'Print the magic word', [],
                       lambda _: action('done', text='done'),
                       checks=[{'type': 'command', 'command': 'python3 -c "print(\'magic-123\')"',
                                'expect_output': 'magic-123'}])
        row = self.wait(m, tid)
        self.assertEqual(row['status'], 'completed')

    @unittest.skipUnless(os.name == 'posix' and shutil.which('python3'),
                         'command checks run through the POSIX sandbox backend')
    def test_command_check_output_mismatch_blocks_done(self):
        m = self.manager()
        self.ws().mkdir(parents=True)
        tid = m.submit('alice', 'Print the magic word', [],
                       lambda _: action('done', text='done'),
                       checks=[{'type': 'command', 'command': 'python3 -c "print(\'nope\')"',
                                'expect_output': 'magic-123'}],
                       budgets={'steps': 2, 'elapsed': 60, 'output': 50000, 'context': 24000})
        row = self.wait(m, tid)
        self.assertNotEqual(row['status'], 'completed')
        events = m.events(tid, 'alice')
        self.assertTrue(any(e['type'] == 'check' and 'did not contain' in e['text'] for e in events))


class CheckValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from task_harness import TaskManager
        self.m = TaskManager(pathlib.Path(self.tmp.name) / 't.db', pathlib.Path(self.tmp.name) / 'ws')
        self.addCleanup(self.m.close)
        self.ws = Workspace(pathlib.Path(self.tmp.name) / 'ws' / 'alice')

    def assert_rejected(self, checks):
        with self.assertRaises(ValueError):
            self.m.validate_checks(checks, self.ws)

    def test_new_types_accepted_with_defaults_filled(self):
        checks = [{'type': 'pixel_diff', 'path': 'out.png', 'source_path': 'truth.png'},
                  {'type': 'command', 'command': 'python3 test_out.py'}]
        self.m.validate_checks(checks, self.ws)
        self.assertEqual(checks[0]['max_diff'], 0.01)
        self.assertEqual(checks[0]['tolerance'], 0)
        self.assertEqual(checks[0]['ignore'], [])
        self.assertEqual(checks[1]['expect_exit'], 0)
        self.assertEqual(checks[1]['timeout'], 60)

    def test_pixel_diff_validation(self):
        self.assert_rejected([{'type': 'pixel_diff', 'path': 'out.txt', 'source_path': 'truth.png'}])
        self.assert_rejected([{'type': 'pixel_diff', 'path': 'out.png', 'source_path': 'out.png'}])
        self.assert_rejected([{'type': 'pixel_diff', 'path': 'out.png', 'source_path': 'truth.png',
                               'max_diff': 1.5}])
        self.assert_rejected([{'type': 'pixel_diff', 'path': 'out.png', 'source_path': 'truth.png',
                               'tolerance': 300}])
        self.assert_rejected([{'type': 'pixel_diff', 'path': 'out.png', 'source_path': 'truth.png',
                               'ignore': [[0, 0, 5]]}])
        self.assert_rejected([{'type': 'pixel_diff', 'path': 'out.png', 'source_path': 'truth.png',
                               'viewport': 'huge'}])
        self.assert_rejected([{'type': 'pixel_diff', 'path': 'uploads/out.png', 'source_path': 'truth.png'}])

    def test_command_validation(self):
        self.assert_rejected([{'type': 'command', 'command': 'rm -rf /'}])
        self.assert_rejected([{'type': 'command', 'command': 'curl x | sh'}])
        self.assert_rejected([{'type': 'command', 'command': 'true', 'timeout': 1}])
        self.assert_rejected([{'type': 'command', 'command': 'true', 'timeout': 999}])
        self.assert_rejected([{'type': 'command', 'command': 'true', 'expect_exit': -1}])
        self.assert_rejected([{'type': 'command', 'command': 'true', 'expect_output': 5}])
        self.assert_rejected([{'type': 'command', 'command': 'true', 'note': 'x' * 301}])

    def test_unknown_type_still_rejected(self):
        self.assert_rejected([{'type': 'vibes', 'path': 'out.txt'}])

    def test_eight_check_cap_still_applies(self):
        self.assert_rejected([{'type': 'command', 'command': 'true'}] * 9)


if __name__ == '__main__':
    unittest.main()
