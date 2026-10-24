"""Small reliability benchmark: does the agent finish real tasks, and how fast does it stop?

Runs 10 representative tasks offline (scripted provider + a local dribbling HTTP server) and
prints a table. Run it on any checkout:

    python benchmark.py

Tasks 6-10 measure the reliability pack (Stop latency, hard stream cap, backup/restore, run
log). On a checkout without those features they report "n/a" or the pre-fix behavior, which is
the before/after comparison.
"""
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_loop import Workspace
from grokbot import agent as agent_core, providers
from grokbot.approvals import SandboxPolicy
from grokbot.tools import ToolContext, default_registry

CFG = ('key', 'https://api.example.test/v1/chat/completions', 'model-x')
RESULTS = []


def record(name, ok, detail, seconds):
    if ok is not None:
        ok = bool(ok)
    RESULTS.append((name, ok, detail, seconds))
    print('%-34s %-4s %7.2fs  %s' % (name, 'PASS' if ok else ('n/a' if ok is None else 'FAIL'), seconds, detail))


def text_events(text):
    return [{'type': 'text', 'delta': text}, {'type': 'usage', 'prompt_tokens': 5, 'completion_tokens': 2},
            {'type': 'finish', 'reason': 'stop'}]


def tool_events(name, args, call_id='c1'):
    return [{'type': 'tool_calls', 'calls': [{'id': call_id, 'name': name, 'arguments': json.dumps(args)}]},
            {'type': 'finish', 'reason': 'tool_calls'}]


def scripted(steps):
    calls = []

    def stream_fn(cfg, messages, tools=None, stop=None, **kw):
        calls.append(1)
        if len(calls) <= len(steps):
            step = steps[len(calls) - 1]
        else:
            step = text_events('done: ' + str(messages[-1].get('content'))[:80])
        for event in step:
            yield event
    return stream_fn


class Bench:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name) / 'ws')
        self.registry = default_registry()

    def ctx(self, **extras):
        return ToolContext(workspace=self.ws, session_id='bench',
                           policy=SandboxPolicy(mode='workspace-write', approval='never'), extras=extras)

    def turn(self, stream_fn, goal='bench task', ctx=None):
        return agent_core.run_turn([{'role': 'user', 'content': goal}], self.registry, ctx or self.ctx(), CFG,
                                   lambda e: None, stream_fn=stream_fn)

    def cleanup(self):
        self.tmp.cleanup()


def task_chat(b):
    t = time.monotonic()
    r = b.turn(scripted([text_events('hello')]))
    return r.status == 'completed' and 'hello' in r.text, 'plain answer turn'


def task_write(b):
    t = time.monotonic()
    r = b.turn(scripted([tool_events('write_file', {'path': 'out.txt', 'content': 'bench'})]))
    ok = r.status == 'completed' and b.ws.path('out.txt').read_text() == 'bench'
    return ok, 'write_file and verify'


def task_edit(b):
    t = time.monotonic()
    b.ws.path('code.py').write_text('a = 1\n', encoding='utf-8')
    r = b.turn(scripted([tool_events('edit_file', {'path': 'code.py', 'old_text': 'a = 1', 'new_text': 'a = 2'})]))
    ok = r.status == 'completed' and 'a = 2' in b.ws.path('code.py').read_text()
    return ok, 'edit_file and verify'


def task_command(b):
    t = time.monotonic()
    r = b.turn(scripted([tool_events('run_command', {'command': 'echo bench-ok'})]))
    tool_msgs = [m for m in r.messages if m.get('role') == 'tool']
    ok = r.status == 'completed' and tool_msgs and 'bench-ok' in tool_msgs[0]['content']
    return ok, 'run_command and verify output'


def task_chain(b):
    t = time.monotonic()
    steps = [tool_events('write_file', {'path': 'c1.txt', 'content': 'x'}),
             tool_events('read_file', {'path': 'c1.txt'}),
             tool_events('copy_file', {'source_path': 'c1.txt', 'path': 'c2.txt'})]
    r = b.turn(scripted(steps))
    ok = r.status == 'completed' and r.tool_calls == 3 and b.ws.path('c2.txt').read_text() == 'x'
    return ok, '3-step chain: write -> read -> copy'


def task_stop_shell(b):
    if os.name != 'posix':
        return None, 'POSIX only'
    t = time.monotonic()
    ctx = b.ctx()
    threading.Timer(0.5, ctx.stop.set).start()
    r = b.turn(scripted([tool_events('run_command', {'command': 'sleep 30', 'timeout': 30})]), ctx=ctx)
    elapsed = time.monotonic() - t
    stopped_fast = elapsed < 15
    detail = 'Stop during sleep 30 -> turn ended in %.1fs%s' % (elapsed, '' if stopped_fast else ' (command blocked to its timeout)')
    return r.status == 'stopped' and stopped_fast, detail


class _Dribble(BaseHTTPRequestHandler):
    """One SSE line, then silence until the deadline, then close."""
    hang = 8.0
    dribble = False

    def do_POST(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        end = time.monotonic() + self.hang
        try:
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
            self.wfile.flush()
            while time.monotonic() < end:
                if self.dribble:
                    self.wfile.write(b' ')   # partial line: keeps the read timeout alive, never completes an event
                    self.wfile.flush()
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


def _server(dribble=False, hang=8.0):
    handler = type('H', (_Dribble,), {'dribble': dribble, 'hang': hang})
    srv = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def task_stop_stream(b):
    srv = _server(dribble=False, hang=8.0)
    url = 'http://127.0.0.1:%d/v1/chat/completions' % srv.server_address[1]
    stop = threading.Event()
    t = time.monotonic()
    events = []

    def consume():
        try:
            events.extend(providers.stream_chat(('k', url, 'm'), [{'role': 'user', 'content': 'hi'}], stop=stop))
        except Exception:
            pass

    worker = threading.Thread(target=consume)
    worker.start()
    time.sleep(0.5)
    stop.set()
    worker.join(timeout=12)
    srv.shutdown()
    elapsed = time.monotonic() - t
    fast = elapsed < 3.0
    return fast, 'Stop during a hung provider stream -> %.1fs%s' % (elapsed, '' if fast else ' (waited for the socket)')


def task_stream_cap(b):
    capped = 'total_timeout' in inspect.signature(providers.stream_chat).parameters
    srv = _server(dribble=True, hang=6.0)
    url = 'http://127.0.0.1:%d/v1/chat/completions' % srv.server_address[1]
    t = time.monotonic()
    try:
        if capped:
            try:
                list(providers.stream_chat(('k', url, 'm'), [{'role': 'user', 'content': 'hi'}], total_timeout=2))
                return False, 'dribbling stream was NOT capped'
            except providers.ProviderError:
                return True, 'dribbling stream capped in %.1fs (limit 2s)' % (time.monotonic() - t)
        list(providers.stream_chat(('k', url, 'm'), [{'role': 'user', 'content': 'hi'}], timeout=(5, 30)))
        return None, 'no total cap available; dribble ran the full %.1fs' % (time.monotonic() - t)
    finally:
        srv.shutdown()


def task_backup_restore(b):
    try:
        from grokbot import backup
    except ImportError:
        return None, 'backup module not present'
    t = time.monotonic()
    data = Path(b.tmp.name) / 'data'
    data.mkdir(exist_ok=True)
    (data / 'file.txt').write_text('important', encoding='utf-8')
    conn = sqlite3.connect(str(data / '.memory.sqlite3'))
    conn.execute('CREATE TABLE m(id TEXT)')
    conn.execute("INSERT INTO m VALUES('fact-1')")
    conn.commit()
    conn.close()
    zf = Path(b.tmp.name) / 'b.zip'
    backup.create_backup(data, zf)
    (data / 'file.txt').write_text('damaged', encoding='utf-8')
    backup.restore_backup(zf, data)
    ok = (data / 'file.txt').read_text() == 'important'
    ok = ok and sqlite3.connect(str(data / '.memory.sqlite3')).execute('SELECT * FROM m').fetchall() == [('fact-1',)]
    return ok, 'backup, damage, restore, verify (%.1fs)' % (time.monotonic() - t)


def task_runlog(b):
    try:
        from grokbot import runlog
    except ImportError:
        return None, 'runlog module not present'
    t = time.monotonic()
    log = str(Path(b.tmp.name) / 'runs.jsonl')
    runlog.configure(log)
    try:
        b.turn(scripted([tool_events('write_file', {'path': 'logged.txt', 'content': 'y'})]))
    finally:
        runlog.configure(None)
    entries = runlog.recent(path=log)
    e = entries[0] if entries else {}
    ok = bool(entries) and e.get('status') == 'completed' and e.get('tools', [{}])[0].get('name') == 'write_file' \
        and 'duration' in e and e.get('run_id')
    return ok, 'run log entry with id/status/duration/tool calls'


TASKS = [
    ('1. chat turn', task_chat, False),
    ('2. write file', task_write, False),
    ('3. edit file', task_edit, False),
    ('4. run command', task_command, False),
    ('5. three-step chain', task_chain, False),
    ('6. Stop during shell command', task_stop_shell, True),
    ('7. Stop during hung stream', task_stop_stream, True),
    ('8. hard cap on dribbling stream', task_stream_cap, True),
    ('9. backup/restore round trip', task_backup_restore, True),
    ('10. run log entry', task_runlog, True),
]


def main():
    print('grok-bot reliability benchmark (%s)\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
    for name, fn, _ in TASKS:
        b = Bench()
        started = time.monotonic()
        try:
            outcome = fn(b)
            ok, detail = outcome if isinstance(outcome, tuple) else (outcome, '')
        except Exception as exc:  # noqa: BLE001 - the benchmark reports, never crashes
            ok, detail = False, type(exc).__name__ + ': ' + str(exc)[:120]
        finally:
            elapsed = time.monotonic() - started
            b.cleanup()
        record(name, ok, detail, elapsed)
    passed = sum(1 for _, ok, _, _ in RESULTS if ok is True)
    failed = sum(1 for _, ok, _, _ in RESULTS if ok is False)
    na = sum(1 for _, ok, _, _ in RESULTS if ok is None)
    print('\n%d passed, %d failed, %d n/a' % (passed, failed, na))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
