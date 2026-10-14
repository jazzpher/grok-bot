"""Single-process durable worker. Only agent_loop's safe workspace tools."""
import json
import os
import pathlib
import re
import secrets
import sqlite3
import threading
import time
import requests
try:
    import fcntl
except ImportError:  # Windows: lock the first byte, never silently skip exclusivity.
    fcntl = None
    import msvcrt
import hashlib
import inspect

from agent_loop import RULES, ACTION_FIELDS, Workspace, ToolError, is_upload_path, parse_action

ACTIVE = ('queued', 'running')
RESUMABLE = ('stopped', 'interrupted', 'needs_input')
DEFAULT_BUDGETS = {'steps': 12, 'elapsed': 300, 'output': 128000, 'context': 24000}


class OutputBudgetExceeded(Exception):
    """Provider output was larger than the remaining per-task budget."""


class TaskManager:
    def __init__(self, database, workspaces):
        self.database = pathlib.Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspaces = pathlib.Path(workspaces)
        self.process_lock = open(str(self.database) + '.lock', 'a+b')
        os.chmod(self.process_lock.name, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(self.process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                self.process_lock.seek(0, os.SEEK_END)
                if self.process_lock.tell() == 0:
                    self.process_lock.write(b'\0'); self.process_lock.flush()
                self.process_lock.seek(0)
                msvcrt.locking(self.process_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.process_lock.close()
            raise RuntimeError('Task database already owned by another server. Use one process.') from None
        self.lock = threading.RLock()
        self.threads = {}
        self.stops = {}
        self.closing = False
        self.db = sqlite3.connect(self.database, check_same_thread=False)
        os.chmod(self.database, 0o600)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions(owner TEXT PRIMARY KEY, history TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL,
              goal TEXT NOT NULL, checks TEXT NOT NULL, state TEXT NOT NULL,
              created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL,
              payload TEXT NOT NULL);
        ''')
        self.db.execute("UPDATE tasks SET status='interrupted' WHERE status IN ('queued','running')")
        self.db.commit()
        self.closed = False

    def get(self, tid, owner):
        with self.lock:
            row = self.db.execute('SELECT id,owner,status,goal,checks,state,created,updated FROM tasks WHERE id=? AND owner=?', (tid, owner)).fetchone()
            if not row:
                return None
            return dict(zip(('id', 'owner', 'status', 'goal', 'checks', 'state', 'created', 'updated'),
                            (*row[:4], json.loads(row[4]), json.loads(row[5]), *row[6:])))

    def list(self, owner):
        with self.lock:
            return [self.get(tid, owner) for (tid,) in self.db.execute(
                'SELECT id FROM tasks WHERE owner=? ORDER BY created DESC LIMIT 50', (owner,)).fetchall()]

    def load_history(self, owner):
        with self.lock:
            row = self.db.execute('SELECT history FROM sessions WHERE owner=?', (owner,)).fetchone()
            return json.loads(row[0]) if row else []

    def prune(self, age_seconds=30 * 86400):
        """Explicit metadata retention; workspace deletion is an operator decision."""
        if age_seconds < 86400:
            raise ValueError('Retention minimum is one day.')
        cutoff = time.time() - age_seconds
        with self.lock, self.db:
            ids = [r[0] for r in self.db.execute("SELECT id FROM tasks WHERE updated<? AND status NOT IN ('running','queued')", (cutoff,))]
            for tid in ids:
                self.db.execute('DELETE FROM events WHERE task=?', (tid,))
                self.db.execute('DELETE FROM tasks WHERE id=?', (tid,))
            self.db.execute("DELETE FROM sessions WHERE updated<? AND owner NOT IN (SELECT owner FROM tasks WHERE status IN ('running','queued'))", (cutoff,))
            return len(ids)

    def store_history(self, owner, history):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)',
                            (owner, json.dumps(history[-24:]), time.time()))

    def events(self, tid, owner, after=0):
        with self.lock:
            if not self.get(tid, owner):
                return []
            return [dict(json.loads(p), seq=s) for s, p in self.db.execute(
                'SELECT seq,payload FROM events WHERE task=? AND seq>? ORDER BY seq LIMIT 200', (tid, after))]

    def save(self, tid, status, state, event, additional_events=()):
        with self.lock, self.db:
            self.db.execute('UPDATE tasks SET status=?,state=?,updated=? WHERE id=?',
                            (status, json.dumps(state), time.time(), tid))
            for payload in (event, *additional_events):
                self.db.execute('INSERT INTO events(task,payload) VALUES(?,?)',
                                (tid, json.dumps(payload)))

    def busy(self, owner):
        with self.lock:
            return self.db.execute("SELECT 1 FROM tasks WHERE owner=? AND status IN ('queued','running')", (owner,)).fetchone() is not None

    def validate_checks(self, checks, workspace):
        if not isinstance(checks, list) or len(checks) > 8:
            raise ValueError('Supply up to eight explicit checks.')
        for c in checks:
            if not isinstance(c, dict) or c.get('type') not in ('file_text', 'exact_copy'):
                raise ValueError('Supported checks: file_text and exact_copy; semantic criteria need clarification.')
            workspace.path(c.get('path'))
            if c['type'] == 'file_text':
                if not isinstance(c.get('text'), str) or len(c['text'].encode('utf-8')) > 32000:
                    raise ValueError('file_text requires expected UTF-8 text up to 32 KB.')
            else:
                source = workspace.path(c.get('source_path'))
                target = workspace.path(c['path'])
                if (source == target or is_upload_path(c['path'])
                        or is_upload_path(target.relative_to(workspace.root))):
                    raise ValueError('Copy target must be separate and writable.')

    def submit(self, owner, goal, history, call_model, checks=None, budgets=None):
        if not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', owner):
            raise ValueError('Invalid session identifier.')
        workspace = Workspace(self.workspaces / owner)
        if checks is None:
            match = re.fullmatch(r'\s*copy\s+([\w./-]+)\s+exact(?:ly)?\s+to\s+([\w./-]+)\s*', goal, re.I)
            checks = [{'type': 'exact_copy', 'source_path': match[1], 'path': match[2]}] if match else []
        self.validate_checks(checks, workspace)
        limits = dict(DEFAULT_BUDGETS)
        if budgets is not None:
            if not isinstance(budgets, dict) or set(budgets) - set(limits):
                raise ValueError('Unknown budget.')
            limits.update(budgets)
        for key, value in limits.items():
            if type(value) not in (int, float) or not 0 < value <= DEFAULT_BUDGETS[key]:
                raise ValueError('Budgets must be positive and cannot exceed server limits.')
        if type(limits['steps']) is not int or type(limits['context']) is not int or limits['context'] < 4000:
            raise ValueError('Steps/context must be integers; context minimum is 4000 characters.')
        tid = secrets.token_hex(16)
        context_rules = '\n'.join(m['content'] for m in history if m.get('role') == 'system')
        past = [m for m in history if m.get('role') != 'system'][-24:]
        state = {'steps': 0, 'messages': [{'role': 'system', 'content': context_rules + '\n' + RULES},
                  *past, {'role': 'user', 'content': goal}], 'written': [], 'checked': [],
                 'pending': None, 'budgets': limits, 'elapsed': 0, 'output': 0, 'retry_streak': 0,
                 'failure_signature': None, 'failure_count': 0}
        now = time.time()
        with self.lock, self.db:
            if self.closed or self.closing or self.busy(owner) or sum(t.is_alive() for t in self.threads.values()) >= 4:
                raise RuntimeError('This session is already running or shutting down.')
            self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)',
                            (tid, owner, 'queued', goal, json.dumps(checks), json.dumps(state), now, now))
            self._start(tid, owner, call_model)
        return tid

    def _start(self, tid, owner, call_model):
        self.stops[tid] = threading.Event()
        thread = threading.Thread(target=self._run_and_cleanup, args=(tid, owner, call_model), daemon=True)
        self.threads[tid] = thread
        thread.start()

    def _run_and_cleanup(self, tid, owner, call_model):
        try:
            self._run(tid, owner, call_model)
        finally:
            with self.lock:
                if self.threads.get(tid) is threading.current_thread():
                    self.threads.pop(tid, None)
                self.stops.pop(tid, None)

    def stop(self, tid, owner):
        with self.lock:
            row = self.get(tid, owner)
            if not row or row['status'] not in ACTIVE:
                return False
            self.stops[tid].set()
            return True

    def resume(self, tid, owner, call_model, answer='', checks=None):
        with self.lock:
            row = self.get(tid, owner)
            thread = self.threads.get(tid)
            if (self.closed or self.closing or not row or row['status'] not in RESUMABLE or self.busy(owner)
                    or (thread and thread.is_alive()) or sum(t.is_alive() for t in self.threads.values()) >= 4):
                return False
            if checks is not None:
                self.validate_checks(checks, Workspace(self.workspaces / owner))
                if row['checks'] and checks != row['checks']:
                    raise ValueError('Existing checks are immutable; start a new task to change them.')
                if not row['checks'] and not checks:
                    raise ValueError('Supply nonempty file_text or exact_copy checks in the checks field.')
            state = row['state']
            if answer:
                state['messages'].append({'role': 'user', 'content': answer})
            with self.db:
                if checks is not None and not row['checks']:
                    self.db.execute('UPDATE tasks SET checks=? WHERE id=? AND owner=?',
                                    (json.dumps(checks), tid, owner))
                self.save(tid, 'queued', state, {'type': 'status', 'text': 'Resuming existing trajectory'})
            self._start(tid, owner, call_model)
            return True

    def _reconcile(self, tid, state, workspace):
        a = state.get('pending')
        if not a:
            return
        # Never re-execute an action whose checkpointed outcome is unknown.
        if a['action'] in ('write_file', 'copy_file'):
            target = workspace.path(a['path'])
            digest = state.get('pending_digest')
            if a['action'] == 'write_file':
                digest = hashlib.sha256(a['content'].encode('utf-8')).hexdigest()
            if digest is None or not target.is_file() or target.stat().st_size > 32000 or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ToolError('Interrupted write outcome cannot be verified. No action replayed; inspect files and start a new task.')
            name = target.relative_to(workspace.root).as_posix()
            workspace.written.add(name)
            workspace.checked.discard(name)
        state['pending'] = None
        state['written'] = sorted(workspace.written)
        state['checked'] = sorted(workspace.checked)
        state['messages'].append({'role': 'user', 'content': 'Interrupted action reconciled from current artifact. No action replayed. Read and verify output again.'})
        self.save(tid, 'running', state, {'type': 'reconciled', 'text': 'Recovered interrupted action without replay.'})

    def _verify(self, row, state, workspace):
        if not row['checks']:
            return 'needs_input', 'Supply explicit file_text or exact_copy checks; no generic semantic verifier is available.'
        if workspace.written - workspace.checked:
            return 'check', 'Read these written files first: ' + ', '.join(sorted(workspace.written - workspace.checked))
        for c in row['checks']:
            p = workspace.path(c['path'])
            if not p.is_file() or p.stat().st_size > 32000:
                return 'check', 'Expected artifact missing or oversized: ' + c['path']
            expected = c.get('text', '').encode('utf-8')
            if c['type'] == 'exact_copy':
                s = workspace.path(c['source_path'])
                if not s.is_file() or s.stat().st_size > 32000:
                    return 'check', 'Copy source missing or oversized.'
                expected = s.read_bytes()
            if p.read_bytes() != expected:
                return 'check', 'Artifact verification failed: ' + c['path']
        return 'completed', 'Explicit artifact checks passed.'

    def _run(self, tid, owner, call_model):
        row = self.get(tid, owner)
        state = row['state']
        started = time.monotonic()
        elapsed_before = state['elapsed']
        def finish(status, text):
            state['elapsed'] = elapsed_before + time.monotonic() - started
            self.save(tid, status, state, {'type': status, 'text': text, 'step': state['steps']})
        def over_budget():
            state['elapsed'] = elapsed_before + time.monotonic() - started
            return (state['elapsed'] >= state['budgets']['elapsed']
                    or state['output'] >= state['budgets']['output'])
        try:
            workspace = Workspace(self.workspaces / owner)
            workspace.written = {workspace.path(name).relative_to(workspace.root).as_posix()
                                 for name in state['written']}
            workspace.checked = {workspace.path(name).relative_to(workspace.root).as_posix()
                                 for name in state['checked']}
            state['written'] = sorted(workspace.written)
            state['checked'] = sorted(workspace.checked)
            self.save(tid, 'running', state, {'type': 'status', 'text': 'Running'})
            self._reconcile(tid, state, workspace)
            while state['steps'] < state['budgets']['steps']:
                if self.stops[tid].is_set():
                    finish('stopped', 'Stopped by you.'); return
                if over_budget():
                    finish('budget_exhausted', 'Elapsed/output budget exhausted.'); return
                context = self._context(tid, state, row['goal'])
                if context is None:
                    finish('budget_exhausted', 'Original goal and rules exceed context budget.'); return
                state['steps'] += 1
                self.save(tid, 'running', state, {'type': 'progress', 'text': 'Requesting next action', 'step': state['steps']})
                try:
                    remaining_output = state['budgets']['output'] - state['output']
                    raw = self._call_model(call_model, context, remaining_output)
                    if not isinstance(raw, str):
                        raise ValueError('Provider response must be text.')
                    raw_size = len(raw.encode('utf-8'))
                    if raw_size > remaining_output:
                        state['output'] = state['budgets']['output']
                        finish('budget_exhausted', 'Provider response exceeded the remaining output budget; action discarded.')
                        return
                    state['output'] += raw_size
                    if over_budget():
                        finish('budget_exhausted', 'Elapsed/output budget exhausted before action.'); return
                    a = parse_action(raw)
                    state['retry_streak'] = 0
                except OutputBudgetExceeded:
                    state['output'] = state['budgets']['output']
                    finish('budget_exhausted', 'Provider response exceeded the remaining output budget; action discarded.')
                    return
                except (requests.RequestException,) as exc:
                    response = getattr(exc, 'response', None)
                    code = response.status_code if response is not None else None
                    transient = code in (500, 502, 503, 504) or isinstance(exc, (requests.Timeout, requests.ConnectionError))
                    state['retry_streak'] += 1
                    if not transient or state['retry_streak'] >= 3:
                        finish('blocked', 'Provider failed' + (' (HTTP %s)' % code if code else '') + '; no completion. Authentication/quota errors are not retried.'); return
                    self.save(tid, 'running', state, {'type': 'retry', 'text': 'Transient provider failure; bounded retry.'})
                    self.stops[tid].wait(.05 * state['retry_streak'])
                    continue
                except (ValueError, TypeError, AttributeError):
                    state['messages'].append({'role': 'user', 'content': 'Invalid action; return valid JSON. No tool executed.'})
                    self.save(tid, 'running', state, {'type': 'error', 'text': 'Invalid action; retrying within step budget.'})
                    continue
                if self.stops[tid].is_set():
                    finish('stopped', 'Stopped before executing action.'); return
                state['messages'].append({'role': 'assistant', 'content': json.dumps(a)})
                kind = a['action']
                if kind == 'need_input':
                    finish('needs_input', a['text'][:2000]); return
                if kind == 'done':
                    status, text = self._verify(row, state, workspace)
                    if status != 'check':
                        state['elapsed'] = elapsed_before + time.monotonic() - started
                        # Publish the model answer only after artifact verification.
                        # Commit terminal status and both events under the same lock.
                        result_events = [{'type': 'result', 'text': a['text'], 'step': state['steps']}] if status == 'completed' else []
                        self.save(tid, status, state, {'type': 'verification', 'text': text, 'step': state['steps']}, result_events)
                        return
                    state['messages'].append({'role': 'user', 'content': text})
                    self.save(tid, 'running', state, {'type': 'check', 'text': text}); continue
                if kind == 'plan':
                    state['messages'].append({'role': 'user', 'content': 'Plan recorded. Choose the next action; a plan is not completion.'})
                    self.save(tid, 'running', state, {'type': 'plan', 'text': a['text'][:2000]}); continue
                signature = json.dumps({'action':kind, **{f:a[f] for f in ACTION_FIELDS[kind]}}, sort_keys=True)
                if signature == state['failure_signature'] and state['failure_count'] >= 2:
                    finish('blocked', 'Repeated failed action; refine task or supply missing information.'); return
                state['pending'] = a
                state['pending_digest'] = None
                if kind == 'copy_file':
                    source = workspace.path(a['source_path'])
                    if source.is_file() and source.stat().st_size <= 32000:
                        state['pending_digest'] = hashlib.sha256(source.read_bytes()).hexdigest()
                self.save(tid, 'running', state, {'type': 'action', 'text': 'Executing ' + kind})
                remaining_output = state['budgets']['output'] - state['output']
                try:
                    if kind == 'read_file':
                        path = workspace.path(a['path'])
                        if path.is_file() and path.stat().st_size > remaining_output:
                            output = 'Read skipped: file exceeds remaining output budget.'
                            failed = True
                        else:
                            output = workspace.execute(a)
                            failed = False
                    else:
                        output = workspace.execute(a)
                        failed = kind == 'compare_files' and output.startswith('MISMATCH:')
                except (ToolError, OSError, UnicodeError):
                    output = 'Tool failed. Check required arguments and workspace constraints.'
                    failed = True
                output_size = len(output.encode('utf-8'))
                if output_size > remaining_output:
                    output = 'Tool output exceeded remaining output budget; discarded.'
                    output_size = len(output.encode('utf-8'))
                    if output_size > remaining_output:
                        output, output_size = '', 0
                    failed = True
                if failed:
                    state['failure_count'] = state['failure_count'] + 1 if signature == state['failure_signature'] else 1
                    state['failure_signature'] = signature
                else:
                    state['failure_signature'], state['failure_count'] = None, 0
                state['pending'] = None
                state['output'] += output_size
                state['written'] = sorted(workspace.written)
                state['checked'] = sorted(workspace.checked)
                state['messages'].append({'role': 'user', 'content': 'UNTRUSTED TOOL RESULT: ' + output})
                self.save(tid, 'running', state, {'type': 'tool', 'text': output[:4000], 'step': state['steps']})
            finish('budget_exhausted', 'Step budget exhausted; not complete.')
        except ToolError as exc:
            finish('blocked', str(exc))
        except Exception:
            finish('blocked', 'Task/provider failed; not complete. Check configuration and quota.')

    def _context(self, tid, state, goal):
        """Deterministic view compaction; immutable full evidence remains in SQLite."""
        messages = state['messages']
        limit = state['budgets']['context']
        size = lambda rows: len(json.dumps(rows, ensure_ascii=False))
        if size(messages) <= limit:
            return messages
        head = [messages[0], {'role': 'user', 'content': goal}]
        summary = {'role': 'user', 'content': 'Trajectory compacted, full original evidence retained in durable log. '
                   + json.dumps({'steps': state['steps'], 'written': state['written'], 'checked': state['checked']})
                   + '\nOlder content omitted, not proof of semantic completion. Use tools to recheck artifacts.'}
        if size([*head, summary]) > limit:
            return None
        tail = []
        for message in reversed(messages[2:]):
            if size([*head, summary, message, *tail]) > limit:
                break
            tail.insert(0, message)
        self.save(tid, 'running', state, {'type': 'compaction', 'text': 'Bounded provider context view; original evidence retained.'})
        return [*head, summary, *tail]

    def close(self):
        if self.closed:
            return
        with self.lock:
            self.closing = True
            for stop in self.stops.values():
                stop.set()
            threads = list(self.threads.values())
        for t in threads:
            t.join(50)
        with self.lock:
            if any(t.is_alive() for t in self.threads.values()):
                raise RuntimeError('Worker still running; cannot close database safely.')
            self.db.close()
            self.process_lock.close()
            self.closed = True

    @staticmethod
    def _call_model(call_model, messages, output_limit):
        """Pass remaining output budget to providers that support it; keep 1-arg callers compatible."""
        try:
            signature = inspect.signature(call_model)
            signature.bind(messages, output_limit)
        except (TypeError, ValueError):
            return call_model(messages)
        return call_model(messages, output_limit)
