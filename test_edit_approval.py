"""GROK_APPROVE_EDITS=on: mutating file tools ask first, with a unified diff of the proposal."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop import Workspace
from grokbot.approvals import SandboxPolicy
from grokbot.tools import ToolContext, default_registry


class EditApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / 'ws')
        self.ws.root.mkdir(parents=True, exist_ok=True)
        self.ctx = ToolContext(workspace=self.ws, session_id='s1', policy=SandboxPolicy(mode='workspace-write'))
        self.registry = default_registry()

    def approval(self, tool, arguments):
        spec = self.registry.get(tool)
        self.assertIsNotNone(spec, tool)
        return spec.needs_approval(self.ctx, arguments)

    def test_off_by_default(self):
        with patch('grokbot.config.APPROVE_EDITS', False):
            self.assertIsNone(self.approval('write_file', {'path': 'a.txt', 'content': 'hi'}))
            self.assertIsNone(self.approval('edit_file', {'path': 'a.txt', 'old_text': 'x', 'new_text': 'y'}))

    def test_write_new_file_shows_added_lines(self):
        with patch('grokbot.config.APPROVE_EDITS', True):
            reason = self.approval('write_file', {'path': 'new.txt', 'content': 'line one\nline two\n'})
        self.assertIn('GROK_APPROVE_EDITS', reason)
        self.assertIn('+line one', reason)
        self.assertIn('(proposed)', reason)

    def test_edit_file_shows_unified_diff(self):
        self.ws.path('code.py').write_text('alpha = 1\nbeta = 2\ngamma = 3\n', encoding='utf-8')
        with patch('grokbot.config.APPROVE_EDITS', True):
            reason = self.approval('edit_file', {'path': 'code.py', 'old_text': 'beta = 2', 'new_text': 'beta = 42'})
        self.assertIn('-beta = 2', reason)
        self.assertIn('+beta = 42', reason)
        self.assertLessEqual(len(reason), 4200)

    def test_edit_with_missing_snippet_defers_to_tool_error(self):
        # The diff cannot be computed, so the reason is generic; the tool itself raises the
        # "old_text was not found" error when it runs.
        self.ws.path('code.py').write_text('alpha = 1\n', encoding='utf-8')
        with patch('grokbot.config.APPROVE_EDITS', True):
            reason = self.approval('edit_file', {'path': 'code.py', 'old_text': 'not there', 'new_text': 'y'})
        self.assertIn('GROK_APPROVE_EDITS', reason)

    def test_read_only_mode_still_wins(self):
        self.ctx.policy = SandboxPolicy(mode='read-only')
        with patch('grokbot.config.APPROVE_EDITS', True):
            reason = self.approval('write_file', {'path': 'a.txt', 'content': 'x'})
        self.assertIn('read-only', reason)

    def test_docx_summary_without_text_diff(self):
        with patch('grokbot.config.APPROVE_EDITS', True):
            reason = self.approval('write_docx', {'path': 'doc.docx', 'content': '# Title\nbody'})
        self.assertIn('Word document', reason)

    def test_delete_file_still_always_asks(self):
        self.ws.path('gone.txt').write_text('x', encoding='utf-8')
        with patch('grokbot.config.APPROVE_EDITS', False):
            reason = self.approval('delete_file', {'path': 'gone.txt'})
        self.assertIn('irreversible', reason)


if __name__ == '__main__':
    unittest.main()
