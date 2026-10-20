"""Per-run log: entries for finished turns, error capture, rotation and reads."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_loop import Workspace
from grokbot import agent as agent_core, runlog
from grokbot.approvals import SandboxPolicy
from grokbot.tools import ToolContext, default_registry


def text_events(text):
    return [{'type': 'text', 'delta': text}, {'type': 'usage', 'prompt_tokens': 5, 'completion_tokens': 2},
            {'type': 'finish', 'reason': 'stop'}]


def tool_events(name, args):
    return [{'type': 'tool_calls', 'calls': [{'id': 'c1', 'name': name, 'arguments': json.dumps(args)}]},
            {'type': 'finish', 'reason': 'tool_calls'}]


def scripted(steps):
    calls = []

    def stream_fn(cfg, messages, tools=None, stop=None, **kw):
        step = steps[min(len(calls), len(steps) - 1)]
        calls.append(step)
        if isinstance(step, Exception):
            raise step
        for event in step:
            yield event
    return stream_fn


class RunLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = str(Path(self.tmp.name) / '.runlog.jsonl')
        runlog.configure(self.log)
        self.addCleanup(runlog.configure, None)
        self.ws = Workspace(Path(self.tmp.name) / 'ws')
        self.registry = default_registry()
        self.cfg = ('key', 'https://api.example.test/v1/chat/completions', 'model-x')

    def turn(self, stream_fn, **extras):
        ctx = ToolContext(workspace=self.ws, session_id='s1',
                          policy=SandboxPolicy(mode='workspace-write', approval='never'),
                          extras=dict({'run_source': 'agent', 'conversation_id': 'c-1'}, **extras))
        return agent_core.run_turn([{'role': 'user', 'content': 'do it'}], self.registry, ctx, self.cfg,
                                   lambda e: None, stream_fn=stream_fn)

    def test_completed_turn_logged_with_tool_calls(self):
        self.turn(scripted([tool_events('write_file', {'path': 'a.txt', 'content': 'x'}), text_events('done')]))
        entries = runlog.recent(path=self.log)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e['status'], 'completed')
        self.assertEqual(e['source'], 'agent')
        self.assertEqual(e['conversation'], 'c-1')
        self.assertEqual(e['goal'], 'do it')
        self.assertEqual(e['tool_calls'], 1)
        self.assertEqual(e['tools'][0]['name'], 'write_file')
        self.assertTrue(e['tools'][0]['ok'])
        self.assertTrue(e['run_id'])
        self.assertGreaterEqual(e['duration'], 0)
        self.assertEqual(e['usage'].get('prompt_tokens'), 5)

    def test_failing_tool_recorded_with_error(self):
        self.turn(scripted([tool_events('read_file', {'path': 'missing.txt'}), text_events('could not')]))
        e = runlog.recent(path=self.log)[0]
        self.assertFalse(e['tools'][0]['ok'])
        self.assertIn('No such file', e['tools'][0]['error'])

    def test_error_status_and_message_logged(self):
        import requests
        self.turn(scripted([requests.ConnectionError('down')]))
        e = runlog.recent(path=self.log)[0]
        self.assertEqual(e['status'], 'error')
        self.assertTrue(e['error'])

    def test_nothing_logged_when_unconfigured(self):
        runlog.configure(None)
        self.turn(scripted([text_events('hi')]))
        self.assertFalse(os.path.exists(self.log))

    def test_rotation_keeps_newest_lines(self):
        old_max = runlog.MAX_BYTES
        runlog.MAX_BYTES = 2000
        try:
            for i in range(60):
                runlog.record({'run_id': 'r%d' % i, 'goal': 'x' * 80, 'status': 'completed'}, path=self.log)
        finally:
            runlog.MAX_BYTES = old_max
        self.assertLessEqual(os.path.getsize(self.log), 2000 + 500)
        entries = runlog.recent(limit=100, path=self.log)
        self.assertEqual(entries[-1]['run_id'], 'r59')
        self.assertGreater(int(entries[0]['run_id'][1:]), 0)

    def test_done_event_carries_run_id(self):
        events = []
        ctx = ToolContext(workspace=self.ws, session_id='s1',
                          policy=SandboxPolicy(mode='workspace-write', approval='never'), extras={})
        agent_core.run_turn([{'role': 'user', 'content': 'hi'}], self.registry, ctx, self.cfg,
                            events.append, stream_fn=scripted([text_events('hi')]))
        done = [e for e in events if e['type'] == 'done'][0]
        self.assertEqual(done['run_id'], ctx.turn_id)


if __name__ == '__main__':
    unittest.main()
