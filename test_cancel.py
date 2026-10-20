"""Cancellation and hard-timeout behavior: provider streams and sandboxed commands.

Cooperative Stop (checked between steps/lines) is not enough: a hung provider socket or a
sleeping subprocess used to hold the turn until a long timeout. These tests prove Stop now
interrupts both within about a second, and that a dribbling stream hits a hard total cap.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop import Workspace
from grokbot import agent as agent_core, providers, sandbox
from grokbot.approvals import SandboxPolicy
from grokbot.tools import ToolContext, default_registry


class HangingResponse:
    """A provider socket that sends one event then blocks until closed."""

    status_code = 200

    def __init__(self, chunks=None):
        self.closed = threading.Event()
        self.chunks = chunks

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        if self.chunks is not None:
            for chunk in self.chunks:
                if self.closed.is_set():
                    return
                yield chunk
                time.sleep(0.05)
            while not self.closed.is_set():
                time.sleep(0.05)
            return
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        while not self.closed.is_set():
            time.sleep(0.05)

    def close(self):
        self.closed.set()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class StreamCancelTests(unittest.TestCase):
    def cfg(self):
        return ('key', 'https://api.example.test/v1/chat/completions', 'model-x')

    def test_stop_closes_hung_stream(self):
        resp = HangingResponse()
        stop = threading.Event()
        with patch('grokbot.providers.requests.post', return_value=resp):
            events = []

            def consume():
                events.extend(providers.stream_chat(self.cfg(), [{'role': 'user', 'content': 'hi'}], stop=stop))

            worker = threading.Thread(target=consume)
            started = time.monotonic()
            worker.start()
            time.sleep(0.5)
            stop.set()
            worker.join(timeout=5)
            elapsed = time.monotonic() - started
        self.assertFalse(worker.is_alive(), 'stream did not stop after the cancel watchdog')
        self.assertLess(elapsed, 3.0, 'stop took %.1fs; the old code waited out the read timeout' % elapsed)
        self.assertFalse(any(e.get('type') == 'finish' for e in events), 'a stopped stream must not finish normally')

    def test_total_timeout_caps_dribbling_stream(self):
        # Bytes keep arriving (read timeout never fires) but no SSE line ever completes.
        resp = HangingResponse(chunks=[b' '] * 100000)
        started = time.monotonic()
        with patch('grokbot.providers.requests.post', return_value=resp):
            with self.assertRaises(providers.ProviderError):
                list(providers.stream_chat(self.cfg(), [{'role': 'user', 'content': 'hi'}], total_timeout=1))
        self.assertLess(time.monotonic() - started, 5.0)

    def test_normal_stream_unaffected(self):
        resp = HangingResponse(chunks=[b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', b'data: [DONE]\n\n'])
        with patch('grokbot.providers.requests.post', return_value=resp):
            events = list(providers.stream_chat(self.cfg(), [{'role': 'user', 'content': 'hi'}], total_timeout=10))
        self.assertEqual([e['type'] for e in events], ['text', 'finish'])


@unittest.skipUnless(os.name == 'posix', 'process-group kill is POSIX-specific')
class SubprocessCancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = sandbox.SubprocessBackend()

    def test_stop_kills_running_command(self):
        stop = threading.Event()
        timer = threading.Timer(0.5, stop.set)
        started = time.monotonic()
        timer.start()
        result = self.backend.run('sleep 30', Path(self.tmp.name), 60, stop=stop)
        timer.cancel()
        elapsed = time.monotonic() - started
        self.assertTrue(result.stopped)
        self.assertIsNone(result.exit_code)
        self.assertLess(elapsed, 10.0, 'stop took %.1fs; the command kept running' % elapsed)
        self.assertIn('STOPPED', result.render())

    def test_timeout_still_kills(self):
        result = self.backend.run('sleep 30', Path(self.tmp.name), 1)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.stopped)

    def test_finished_command_unaffected(self):
        result = self.backend.run('echo hello', Path(self.tmp.name), 10, stop=threading.Event())
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.stopped)
        self.assertIn('hello', result.stdout)


class TurnCancelTests(unittest.TestCase):
    """End to end: Stop pressed while a long run_command is executing ends the turn quickly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / 'ws')
        self.registry = default_registry()
        self.cfg = ('key', 'https://api.example.test/v1/chat/completions', 'model-x')

    @unittest.skipUnless(os.name == 'posix', 'process-group kill is POSIX-specific')
    def test_stop_during_run_command(self):
        calls = [{'id': 'c1', 'name': 'run_command',
                  'arguments': json.dumps({'command': 'sleep 30', 'timeout': 60})}]

        def stream_fn(cfg, messages, tools=None, stop=None, **kw):
            if messages[-1].get('role') == 'tool':
                yield {'type': 'text', 'delta': 'done'}
                yield {'type': 'finish', 'reason': 'stop'}
                return
            yield {'type': 'tool_calls', 'calls': calls}
            yield {'type': 'finish', 'reason': 'tool_calls'}

        policy = SandboxPolicy(mode='workspace-write', approval='never')
        ctx = ToolContext(workspace=self.ws, session_id='s1', policy=policy, stop=threading.Event())
        timer = threading.Timer(0.5, ctx.stop.set)
        started = time.monotonic()
        timer.start()
        result = agent_core.run_turn([{'role': 'user', 'content': 'sleep'}], self.registry, ctx, self.cfg,
                                     lambda e: None, stream_fn=stream_fn)
        timer.cancel()
        elapsed = time.monotonic() - started
        self.assertEqual(result.status, 'stopped')
        self.assertLess(elapsed, 15.0, 'turn took %.1fs after Stop; the command blocked it' % elapsed)
        tool_messages = [m for m in result.messages if m.get('role') == 'tool']
        self.assertTrue(tool_messages and 'STOPPED' in tool_messages[0]['content'])


if __name__ == '__main__':
    unittest.main()
