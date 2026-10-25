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
from urllib.parse import urlparse

from agent_loop import RULES, ACTION_FIELDS, Workspace, ToolError, is_upload_path, parse_action, INVALID_HELP, MAX_INVALID, build_docx, extract_text

MAX_SCHEMA_REJECTS = 3
MAX_SELF_VERIFY = 1  # one autonomous self-verification round before done is accepted
CHECK_TYPES = ('file_text', 'exact_copy', 'pixel_diff', 'command')
CHECK_COMMAND_TIMEOUT = (5, 120)      # seconds, min/max for command checks
CHECK_NOTE_MAX = 300

ACTIVE = ('queued', 'running')
RESUMABLE = ('stopped', 'interrupted', 'needs_input', 'blocked')
MAX_RETRIES = 3
RETRY_DELAY = 1.0
DEFAULT_BUDGETS = {'steps': 12, 'elapsed': 300, 'output': 128000, 'context': 24000}


def is_transient(exc):
    """Provider failures worth an automatic retry: overload, gateway errors, timeouts."""
    response = getattr(exc, 'response', None)
    code = getattr(response, 'status_code', None)
    return code in (500, 502, 503, 504) or isinstance(exc, (requests.Timeout, requests.ConnectionError))


PROVIDER_NAMES = (('google', 'Google'), ('nvidia', 'NVIDIA'), ('openrouter', 'OpenRouter'),
                  ('groq', 'Groq'), ('cerebras', 'Cerebras'), ('mistral', 'Mistral'),
                  ('deepseek', 'DeepSeek'), ('x.ai', 'xAI'))


def provider_name(url):
    """Short provider name from an endpoint hostname, for friendly error text."""
    host = urlparse(str(url)).hostname or ''
    for fragment, name in PROVIDER_NAMES:
        if fragment in host:
            return name
    return 'The AI provider'


def _provider_detail(response, limit=180):
    """Short, scrubbed reason from a provider error body (or '').

    A bare "HTTP 400" hides the actual cause ("invalid model", "tools not supported",
    "max_tokens too large"), which is exactly what a user needs to fix it. Keys, bearer
    tokens and URLs are stripped before the text is shown.
    """
    if response is None:
        return ''
    detail = ''
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - not JSON, fall back to text
        data = None
    if isinstance(data, dict):
        err = data.get('error')
        if isinstance(err, dict):
            detail = str(err.get('message') or err.get('code') or '')
        elif isinstance(err, str):
            detail = err
        elif isinstance(data.get('message'), str):
            detail = data['message']
    if not detail:
        try:
            detail = response.text or ''
        except Exception:  # noqa: BLE001
            return ''
    detail = ' '.join(str(detail).split())
    detail = re.sub(r'\b(sk-|AIza|nvapi-|gsk_|xai-)[A-Za-z0-9_\-]{4,}', '<redacted-key>', detail)
    detail = re.sub(r'(?i)\bbearer\s+\S+', 'Bearer <redacted>', detail)
    detail = re.sub(r'(?i)\b(api[_-]?key|key|token|authorization)\b\s*[:=]\s*\S+', r'\1 <redacted>', detail)
    detail = re.sub(r'https?://\S+', '<url>', detail)
    return detail[:limit].strip()


def friendly_provider_error(exc_or_code, url=None, retrying=False):
    """Plain-language provider error text. Never includes keys or URLs."""
    response = getattr(exc_or_code, 'response', None)
    code = getattr(response, 'status_code', None) if not isinstance(exc_or_code, int) else exc_or_code
    url = url or getattr(response, 'url', '') or ''
    who = provider_name(url)
    tail = ', retrying...' if retrying else '.'
    if code == 503:
        return who + ' is busy right now' + tail
    if code in (500, 502, 504):
        return who + ' had a temporary problem' + tail
    if code is None and isinstance(exc_or_code, (requests.Timeout, requests.ConnectionError)):
        return "Can't reach " + who + ' right now' + tail
    if code == 404:
        return 'No such model on your key. Check the model name in Settings.'
    if code in (401, 403):
        return 'Wrong key, or this key is not allowed. Check the key in Settings.' if code == 401 else 'This key is not allowed to use that model. Check the key in Settings.'
    if code == 429:
        return 'Free quota reached. Wait a bit, then try again.'
    if code == 400:
        detail = _provider_detail(response)
        base = 'The provider rejected the request (HTTP 400)'
        if detail:
            return base + ': ' + detail.rstrip('.') + '. Check the model name and settings.'
        return base + '. Check the model name in Settings.'
    return 'Provider error' + (' (HTTP %s)' % code if code else '') + '. Check key, model and quota.'


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
            if not isinstance(c, dict) or c.get('type') not in CHECK_TYPES:
                raise ValueError('Supported checks: file_text, exact_copy, pixel_diff and command; '
                                 'semantic criteria need clarification.')
            if c['type'] == 'command':
                self._validate_command_check(c)
                continue
            workspace.path(c.get('path'))
            if c['type'] == 'file_text':
                if not isinstance(c.get('text'), str) or len(c['text'].encode('utf-8')) > 32000:
                    raise ValueError('file_text requires expected UTF-8 text up to 32 KB.')
            elif c['type'] == 'pixel_diff':
                self._validate_pixel_check(c, workspace)
            else:
                source = workspace.path(c.get('source_path'))
                target = workspace.path(c['path'])
                if (source == target or is_upload_path(c['path'])
                        or is_upload_path(target.relative_to(workspace.root))):
                    raise ValueError('Copy target must be separate and writable.')

    @staticmethod
    def _validate_command_check(c):
        command = c.get('command')
        if not isinstance(command, str) or not 1 <= len(command) <= 500:
            raise ValueError('command checks require a command string up to 500 characters.')
        from grokbot.sandbox import hardline_reason
        reason = hardline_reason(command)
        if reason:
            raise ValueError('command check refused. ' + reason)
        expect_exit = c.setdefault('expect_exit', 0)
        if isinstance(expect_exit, bool) or type(expect_exit) is not int or not 0 <= expect_exit <= 255:
            raise ValueError('expect_exit must be an integer from 0 to 255.')
        expect_output = c.get('expect_output')
        if expect_output is not None and (not isinstance(expect_output, str) or len(expect_output) > 1000):
            raise ValueError('expect_output must be text up to 1000 characters.')
        timeout = c.setdefault('timeout', 60)
        if isinstance(timeout, bool) or type(timeout) is not int or not CHECK_COMMAND_TIMEOUT[0] <= timeout <= CHECK_COMMAND_TIMEOUT[1]:
            raise ValueError('command timeout must be an integer from %d to %d seconds.' % CHECK_COMMAND_TIMEOUT)
        TaskManager._validate_note(c)

    @staticmethod
    def _validate_pixel_check(c, workspace):
        from grokbot.visual import HTML_SUFFIXES, IMAGE_SUFFIXES, VisualVerifyError, validate_ignore, validate_threshold, validate_tolerance
        kinds = ', '.join(sorted(IMAGE_SUFFIXES)) + ' or ' + '/'.join(sorted(HTML_SUFFIXES))
        for key in ('path', 'source_path'):
            value = c.get(key)
            if not isinstance(value, str) or pathlib.PurePath(value).suffix.lower() not in IMAGE_SUFFIXES | HTML_SUFFIXES:
                raise ValueError('pixel_diff requires %s to be an image (%s); HTML is rendered headlessly first.' % (key, kinds))
        if workspace.path(c['path']) == workspace.path(c['source_path']):
            raise ValueError('pixel_diff needs separate output and ground-truth paths.')
        if is_upload_path(c['path']):
            raise ValueError('pixel_diff output must be writable, not an upload.')
        try:
            c['max_diff'] = validate_threshold(c.get('max_diff', 0.01), name='max_diff')
            c['tolerance'] = validate_tolerance(c.get('tolerance', 0))
            c['ignore'] = validate_ignore(c.get('ignore'))
        except VisualVerifyError as exc:
            raise ValueError(str(exc)) from exc
        viewport = c.setdefault('viewport', 'desktop')
        if viewport not in ('desktop', 'mobile'):
            raise ValueError('viewport must be desktop or mobile.')
        TaskManager._validate_note(c)

    @staticmethod
    def _validate_note(c):
        note = c.get('note')
        if note is not None and (not isinstance(note, str) or len(note) > CHECK_NOTE_MAX):
            raise ValueError('check notes (documented exemptions) must be text up to %d characters.' % CHECK_NOTE_MAX)

    def submit(self, owner, goal, history, call_model, checks=None, budgets=None, requirements='', result_schema=None):
        if result_schema is not None:
            if not isinstance(result_schema, list) or len(result_schema) > 16:
                raise ValueError('Result schema must be a list of at most 16 required field names.')
            for field in result_schema:
                if not isinstance(field, str) or not field.strip() or len(field.strip()) > 64:
                    raise ValueError('Each result field name must be nonempty text up to 64 characters.')
            result_schema = [f.strip() for f in result_schema]
            if any(not re.sub(r'[^a-z0-9]', '', f.lower()) for f in result_schema):
                raise ValueError('Result field names must contain an ASCII letter or digit.')
        if not isinstance(requirements, str) or len(requirements.encode('utf-8')) > 8000:
            raise ValueError('Requirements must be text up to 8 KB; include language, format, content and style.')
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
        # Lazy: a module-level import would drag the agent loop (and subprocess) into hosts
        # that import task_harness under restricted environments.
        from grokbot.agent import project_instruction_file, project_instructions
        project_name = project_instruction_file(workspace)
        project = project_instructions(workspace)
        state = {'steps': 0, 'attempts': 0, 'requirements': requirements, 'messages': [{'role': 'system', 'content': context_rules + '\n' + workspace.skills.index_context(goal) + project + '\n' + RULES},
                  *past, {'role': 'user', 'content': goal}], 'written': [], 'checked': [],
                 'pending': None, 'budgets': limits, 'elapsed': 0, 'output': 0, 'retry_streak': 0, 'usage': None,
                 'failure_signature': None, 'failure_count': 0, 'result_schema': result_schema}
        now = time.time()
        with self.lock, self.db:
            if self.closed or self.closing or self.busy(owner) or sum(t.is_alive() for t in self.threads.values()) >= 4:
                raise RuntimeError('This session is already running or shutting down.')
            self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)',
                            (tid, owner, 'queued', goal, json.dumps(checks), json.dumps(state), now, now))
            if project_name:
                self.db.execute('INSERT INTO events(task,payload) VALUES(?,?)',
                                (tid, json.dumps({'type': 'context', 'text': 'Loaded project instructions from ' + project_name
                                                  + '; they cannot grant new permissions.'})))
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
                clarification = state.get('clarification', '') + '\n' + answer
                if len(clarification.encode('utf-8')) > 12000:
                    raise ValueError('Cumulative clarification limit is 12 KB; start a new task with consolidated requirements.')
                state['clarification'] = clarification
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
        if a['action'] in ('write_file', 'copy_file', 'write_docx'):
            target = workspace.path(a['path'])
            digest = state.get('pending_digest')
            if a['action'] == 'write_file':
                digest = hashlib.sha256(a['content'].encode('utf-8')).hexdigest()
            if a['action'] == 'write_docx':
                digest = hashlib.sha256(build_docx(a['content'])).hexdigest()
            if digest is None or not target.is_file() or target.stat().st_size > 32000 or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ToolError('Interrupted write outcome cannot be verified. No action replayed; inspect files and start a new task.')
            name = target.relative_to(workspace.root).as_posix()
            workspace.written.add(name)
            workspace.checked.discard(name)
        if a['action'] == 'save_skill':
            try:
                saved = workspace.skills.read(a['path'])
                if saved['content'].strip() != a['content'].strip() or saved['description'] != a['text'].strip():
                    raise ValueError('Different saved workflow')
            except (ValueError, OSError, UnicodeError):
                raise ToolError('Interrupted skill save cannot be verified. No replay; inspect Skills and start a new task.')
        state['pending'] = None
        state['written'] = sorted(workspace.written)
        state['checked'] = sorted(workspace.checked)
        state['messages'].append({'role': 'user', 'content': 'Interrupted action reconciled from current artifact. No action replayed. Read and verify output again.'})
        self.save(tid, 'running', state, {'type': 'reconciled', 'text': 'Recovered interrupted action without replay.'})

    def _verify_command(self, c, workspace, stop=None):
        """Run an owner-supplied acceptance command (tests pass, output matches).

        Checks come from the owner, not the model, and hardline-blocked commands
        are rejected at submit; they run without a per-command approval prompt on
        the configured sandbox backend (subprocess fallback), network disabled."""
        from grokbot import sandbox as _sandbox
        backend = _sandbox.get_backend()
        if backend is None or not backend.available():
            backend = _sandbox.SubprocessBackend()
        if not backend.available():
            return 'blocked', 'Command checks need a command backend, but none is available on this host.'
        timeout = c.get('timeout', 60)
        try:
            result = backend.run(c['command'], workspace.root, timeout, network=False, stop=stop)
        except Exception as exc:
            return 'blocked', 'Command check could not run (%s).' % type(exc).__name__
        note = (' Note: ' + c['note']) if c.get('note') else ''
        if result.stopped:
            return 'check', 'Command check interrupted by stop.'
        out = (result.stdout + '\n' + result.stderr).strip()
        tail = out[-800:] if out else '(no output)'
        if result.timed_out or result.exit_code is None:
            return 'check', ('Command check timed out after %ds: `%s`. Make the artifact pass within '
                             'the timeout, then call done again.%s' % (timeout, c['command'], note))
        expect_exit = c.get('expect_exit', 0)
        if result.exit_code != expect_exit:
            return 'check', ('Command check failed: `%s` exited %s (expected %d). Output tail:\n%s\n'
                             'Fix the artifact and call done again.%s'
                             % (c['command'], result.exit_code, expect_exit, tail, note))
        expect_output = c.get('expect_output')
        if expect_output is not None and expect_output not in out:
            return 'check', ('Command check failed: `%s` output did not contain %r. Output tail:\n%s\n'
                             'Fix the artifact and call done again.%s' % (c['command'], expect_output, tail, note))
        return 'ok', 'Command check passed: `%s` exited %d.' % (c['command'], expect_exit)

    def _verify_pixel_diff(self, c, workspace):
        """Deterministic pixel diff between an output artifact and a ground truth.

        A .html/.htm side is rendered headlessly first (needs GROK_PREVIEW=on);
        image sides are compared directly. The highlighted diff lands next to
        the output as <name>.diff.png."""
        from grokbot import config as _gconfig
        from grokbot.visual import MAX_IMAGE_BYTES, VisualVerifyError, diff_images, is_html_name

        def side(rel, label):
            path = workspace.path(rel)
            if is_html_name(rel):
                if not _gconfig.PREVIEW_ENABLED:
                    raise VisualVerifyError('rendering HTML needs GROK_PREVIEW=on')
                if not path.is_file():
                    raise VisualVerifyError('HTML file missing: ' + label)
                from grokbot.preview import PreviewError, PreviewSession
                session = PreviewSession(workspace.root)
                try:
                    return session.capture(path.relative_to(workspace.root).as_posix(),
                                           c.get('viewport', 'desktop'))['image'][0]
                except PreviewError as exc:
                    raise VisualVerifyError(str(exc)) from exc
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
            if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
                raise VisualVerifyError('image missing or exceeds the %d MB limit: %s'
                                        % (MAX_IMAGE_BYTES // (1024 * 1024), label))
            return path.read_bytes()

        try:
            out_bytes = side(c['path'], c['path'])
            truth_bytes = side(c['source_path'], c['source_path'])
            diff_rel = c['path'].rsplit('.', 1)[0] + '.diff.png'
            result = diff_images(truth_bytes, out_bytes, tolerance=c.get('tolerance', 0),
                                 ignore=c.get('ignore'), diff_path=workspace.path(diff_rel))
        except VisualVerifyError as exc:
            text = str(exc)
            if 'GROK_PREVIEW' in text or 'Pillow' in text or 'Playwright' in text:
                return 'blocked', ('Pixel-diff check cannot run: ' + text + '. Install the verifier '
                                   '(pip install pillow; HTML sides also need GROK_PREVIEW=on and Playwright), '
                                   'then resume.')
            return 'check', 'Artifact verification could not run: ' + text
        max_diff = c.get('max_diff', 0.01)
        note = (' Known-irrelevant differences per task note: ' + c['note']) if c.get('note') else ''
        if result['mismatch_ratio'] <= max_diff:
            return 'ok', ('Pixel-diff check passed: %s matches %s within %.4f%% (mismatch %.4f%%, '
                          '%d of %d pixels).%s' % (c['path'], c['source_path'], max_diff * 100,
                                                   result['mismatch_percent'], result['different_pixels'],
                                                   result['compared_pixels'], note))
        return 'check', ('Artifact verification failed: %s differs from ground truth %s: %.4f%% mismatch '
                         'exceeds the allowed %.4f%% (%d of %d pixels differ, sizes %s vs %s). Highlighted '
                         'diff saved: %s (red pixels differ). Fix the artifact and call done again.%s'
                         % (c['path'], c['source_path'], result['mismatch_percent'], max_diff * 100,
                            result['different_pixels'], result['compared_pixels'],
                            'x'.join(map(str, result['output_size'])),
                            'x'.join(map(str, result['ground_truth_size'])), diff_rel, note))

    @staticmethod
    def _check_result_schema(state, result):
        """Required-field check against the task's result schema, key matching
        case- and punctuation-insensitive like rtrvr.ai's output validation."""
        def norm(key):
            return re.sub(r'[^a-z0-9]', '', str(key).lower())
        missing = []
        for field in state.get('result_schema') or []:
            match = next((v for k, v in result.items() if norm(k) == norm(field)), None)
            if not isinstance(match, str) or not match.strip():
                missing.append(field)
        return missing

    def _verify(self, row, state, workspace, gate=False):
        if workspace.written - workspace.checked:
            return 'check', 'Read these written files first: ' + ', '.join(sorted(workspace.written - workspace.checked))
        if not row['checks']:
            # Preview screenshots are verification aids, not user artifacts.
            docs = sorted(n for n in workspace.written
                          if not (n.lower().endswith('.preview.png') or n.lower().endswith('.browse.png')
                                  or n.lower().endswith('.diff.png')))
            if docs and all(n.lower().endswith('.docx') for n in docs) and not workspace.written - workspace.checked:
                # Built-in check for Word output: each saved file must reopen as DOCX with text.
                for n in docs:
                    p = workspace.path(n)
                    try:
                        if not p.is_file() or not extract_text(p.name, p.read_bytes()).strip():
                            return 'check', 'Word document is missing or has no text: ' + n
                    except Exception:
                        return 'check', 'Word document failed to open as DOCX: ' + n
                return 'completed', 'Word document(s) reopened as DOCX with text present: ' + ', '.join(docs) + '. Only that was verified; language, content and style were not independently checked, so review the download.'
            if gate:
                return 'needs_input', ('No deterministic acceptance checks supplied; learned skills require '
                    'verified task artifacts (file_text, exact_copy, pixel_diff or command checks, or the built-in Word check).')
            rounds = state.get('self_verify', 0)
            if docs and rounds < MAX_SELF_VERIFY:
                # No user prompt: the task assistant verifies its own output.
                state['self_verify'] = rounds + 1
                return 'check', ('Verify your own output before done; do not ask the user to review it. '
                    'Read back ' + ', '.join(docs) + ' and compare each against the goal\'s requirements, '
                    'fix any mismatch yourself, then call done again stating what you checked and what matched. '
                    'A claim you did not check stays a claim, not evidence.')
            if docs:
                return 'completed_unverified', ('Task finished after ' + str(rounds) + ' self-verification round(s); read-back checked for '
                    + ', '.join(docs) + '. Requirements, relevance and quality were checked by the task assistant itself, not '
                    'independently; review the downloads at your leisure.')
            if state.get('any_failure'):
                if rounds < MAX_SELF_VERIFY:
                    state['self_verify'] = rounds + 1
                    return 'check', ('A tool failed during this task and no file was produced. Fix the failure yourself '
                        'or report specifically what is missing, then call done again. Do not ask the user to review.')
                return 'needs_input', ('A tool failed during this task and no artifact was produced. '
                    'Fix or restate the goal, then resume.')
            return 'completed_unverified', ('Answer delivered directly (no file artifacts). Content was not independently verified; '
                'it reflects the task assistant\'s own review of the goal.')
        passed = []
        for c in row['checks']:
            if c['type'] == 'command':
                status, text = self._verify_command(c, workspace, stop=self.stops.get(row['id']))
                if status != 'ok':
                    return status, text
                passed.append(text)
                continue
            if c['type'] == 'pixel_diff':
                status, text = self._verify_pixel_diff(c, workspace)
                if status != 'ok':
                    return status, text
                passed.append(text)
                continue
            p = workspace.path(c['path'])
            if not p.is_file() or p.stat().st_size > 32000:
                return 'check', 'Expected artifact missing or oversized: ' + c['path']
            expected = c.get('text', '').encode('utf-8')
            if c['type'] == 'file_text' and p.suffix.lower() == '.docx':
                try:
                    got = extract_text(p.name, p.read_bytes())
                except Exception:
                    return 'check', 'Artifact is not a valid DOCX: ' + c['path']
                if c.get('text', '') != got:
                    return 'check', 'Artifact verification failed: ' + c['path']
                continue
            if c['type'] == 'exact_copy':
                s = workspace.path(c['source_path'])
                if not s.is_file() or s.stat().st_size > 32000:
                    return 'check', 'Copy source missing or oversized.'
                expected = s.read_bytes()
            if p.read_bytes() != expected:
                return 'check', 'Artifact verification failed: ' + c['path']
        detail = (' ' + ' '.join(passed)) if passed else ''
        return 'completed', ('Explicit artifact checks passed: ' + json.dumps(row['checks'], ensure_ascii=False)
                             + '.' + detail
                             + ' Only these checks were verified; language, style, relevance and factual '
                             'quality are not independently verified.')

    def _run(self, tid, owner, call_model):
        row = self.get(tid, owner)
        state = row['state']
        state.setdefault('attempts', state['steps'] + state.get('invalid', 0))
        started = time.monotonic()
        elapsed_before = state['elapsed']
        def finish(status, text):
            state['elapsed'] = elapsed_before + time.monotonic() - started
            self.save(tid, status, state, {'type': status, 'text': text, 'step': state['steps']})
        def over_budget():
            state['elapsed'] = elapsed_before + time.monotonic() - started
            return (state['elapsed'] >= state['budgets']['elapsed']
                    or state['output'] >= state['budgets']['output'])
        workspace = None
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
                context = self._context(tid, state, row['goal'], row['checks'])
                if context is None:
                    finish('budget_exhausted', 'Original goal and rules exceed context budget.'); return
                state['steps'] += 1
                state['attempts'] += 1
                self.save(tid, 'running', state, {'type': 'progress', 'text': 'Requesting next action', 'step': state['steps'], 'attempt': state['attempts']})
                try:
                    remaining_output = state['budgets']['output'] - state['output']
                    raw = self._call_model(call_model, context, remaining_output)
                    usage = getattr(call_model, 'last_usage', None)
                    if isinstance(usage, dict):
                        prompt, completion = usage.get('prompt_tokens'), usage.get('completion_tokens')
                        if type(prompt) is int and type(completion) is int:
                            totals = state.get('usage') or {'prompt_tokens': 0, 'completion_tokens': 0}
                            totals['prompt_tokens'] += max(0, prompt)
                            totals['completion_tokens'] += max(0, completion)
                            state['usage'] = totals
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
                    transient = is_transient(exc)
                    state['retry_streak'] += 1
                    if not transient or state['retry_streak'] > MAX_RETRIES:
                        finish('blocked', friendly_provider_error(exc) + ' Task paused, nothing was lost. Press Resume to continue.'); return
                    self.save(tid, 'running', state, {'type': 'retry', 'text': friendly_provider_error(exc).rstrip('.') + ', retrying %d of %d...' % (state['retry_streak'], MAX_RETRIES)})
                    self.stops[tid].wait(min(RETRY_DELAY * state['retry_streak'], 4))
                    continue
                except (ValueError, TypeError, AttributeError) as exc:
                    # An invalid reply is not a tool step: refund it, with a separate small cap.
                    state['steps'] -= 1
                    state['invalid'] = state.get('invalid', 0) + 1
                    if state['invalid'] > MAX_INVALID:
                        finish('blocked', 'The model returned %d invalid actions in this task. Try a stronger model or a simpler request, then press Resume.' % state['invalid']); return
                    state['messages'].append({'role': 'user', 'content': 'Invalid action: ' + str(exc)[:500] + ' ' + INVALID_HELP})
                    self.save(tid, 'running', state, {'type': 'error', 'text': 'Invalid action (%d of %d allowed); retrying without using a step.' % (state['invalid'], MAX_INVALID)})
                    continue
                if self.stops[tid].is_set():
                    finish('stopped', 'Stopped before executing action.'); return
                state['messages'].append({'role': 'assistant', 'content': json.dumps(a)})
                kind = a['action']
                if kind == 'need_input':
                    finish('needs_input', a['text'][:2000]); return
                if kind == 'done':
                    missing = self._check_result_schema(state, a.get('result') or {})
                    if missing:
                        # A schema-rejected done is not accepted, not executed and
                        # not a tool step: refund it, with a separate small cap.
                        state['steps'] -= 1
                        state['schema_rejects'] = state.get('schema_rejects', 0) + 1
                        if state['schema_rejects'] > MAX_SCHEMA_REJECTS:
                            finish('blocked', 'The model could not produce the required result fields in %d attempts. Try a stronger model, or resubmit without the result schema, then press Resume.' % state['schema_rejects']); return
                        state['messages'].append({'role': 'user', 'content': 'Result schema check failed: the done action is missing required result field(s): ' + ', '.join(missing) + '. Gather the data if it is not yet available, then call done again with a result object containing every required field. Nothing was accepted and this did not use a step.'})
                        self.save(tid, 'running', state, {'type': 'check', 'text': 'Result schema check failed (%d of %d allowed); retrying without using a step.' % (state['schema_rejects'], MAX_SCHEMA_REJECTS)})
                        continue
                    status, text = self._verify(row, state, workspace)
                    if status != 'check':
                        final = 'completed' if status in ('completed', 'completed_unverified') else status
                        if status == 'completed' and state.get('result_schema'):
                            text = 'Result schema validated (' + ', '.join(state['result_schema']) + '). ' + text
                        state['elapsed'] = elapsed_before + time.monotonic() - started
                        # Commit terminal status and evidence under the same lock.
                        # Do not publish model self-certification as verified proof:
                        # only check-backed completions get a result event.
                        result_events = [{'type': 'result', 'text': text, 'step': state['steps']}] if status == 'completed' else []
                        self.save(tid, final, state, {'type': 'verification', 'text': text, 'step': state['steps']}, result_events)
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
                arg = ''
                if kind == 'browse_open':
                    arg = a.get('url') or ''
                    state['last_url'] = arg
                elif kind == 'browse_read':
                    arg = state.get('last_url') or ''
                elif kind in ('browse_click', 'browse_type'):
                    arg = a.get('selector') or ''
                elif kind in ('read_file', 'read_skill', 'write_file', 'write_docx', 'save_skill'):
                    arg = a.get('path') or ''
                elif kind in ('copy_file', 'compare_files'):
                    arg = (a.get('source_path') or '') + ' -> ' + (a.get('path') or '')
                elif kind == 'calculate':
                    arg = a.get('expression') or ''
                self.save(tid, 'running', state, {'type': 'action', 'text': 'Executing ' + kind, 'arg': arg})
                remaining_output = state['budgets']['output'] - state['output']
                try:
                    if kind == 'save_skill':
                        status, detail = self._verify(row, state, workspace, gate=True)
                        if status != 'completed':
                            raise ToolError('Verify task artifacts before saving learned skills: ' + detail)
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
                except (ToolError, OSError, UnicodeError) as exc:
                    output = 'Tool failed: ' + str(exc)[:1000]
                    failed = True
                output_size = len(output.encode('utf-8'))
                if output_size > remaining_output:
                    output = 'Tool output exceeded remaining output budget; discarded.'
                    output_size = len(output.encode('utf-8'))
                    if output_size > remaining_output:
                        output, output_size = '', 0
                    failed = True
                if failed:
                    state['any_failure'] = True
                    state['failure_count'] = state['failure_count'] + 1 if signature == state['failure_signature'] else 1
                    state['failure_signature'] = signature
                else:
                    state['failure_signature'], state['failure_count'] = None, 0
                state['pending'] = None
                state['output'] += output_size
                state['written'] = sorted(workspace.written)
                state['checked'] = sorted(workspace.checked)
                state['messages'].append({'role': 'user', 'content': 'UNTRUSTED TOOL RESULT: ' + output})
                extra = []
                if kind in ('write_docx', 'write_file', 'copy_file') and not failed:
                    extra = [{'type': 'file', 'text': workspace.path(a['path']).relative_to(workspace.root).as_posix()}]
                elif kind in ('browse_open', 'browse_click', 'browse_type'):
                    emitted = state.get('shots_emitted', 0)
                    extra = [{'type': 'file', 'text': n} for n in workspace.shots[emitted:]]
                    state['shots_emitted'] = len(workspace.shots)
                self.save(tid, 'running', state, {'type': 'tool', 'text': output[:4000], 'step': state['steps']}, extra)
            finish('budget_exhausted', 'Step budget exhausted; not complete.')
        except ToolError as exc:
            finish('blocked', str(exc))
        except Exception:
            finish('blocked', 'Task/provider failed; not complete. Check configuration and quota.')
        finally:
            if workspace is not None:
                workspace.close()

    def _context(self, tid, state, goal, checks=None):
        """Deterministic view compaction; immutable full evidence remains in SQLite."""
        contract = {'role': 'user', 'content': 'Original user instruction: ' + goal
                    + '\nGeneration requirements/preferences (not deterministic proof): '
                    + state.get('requirements', '')
                    + '\nUser clarifications: ' + state.get('clarification', '')
                    + '\nHard acceptance checks (enforced by harness): '
                    + json.dumps(checks or [], ensure_ascii=False)
                    + (('\nRequired result fields: when the goal is complete, call done with a "result" object containing nonempty text values under these exact field names (use empty text values on other actions): '
                        + json.dumps(state['result_schema'], ensure_ascii=False)) if state.get('result_schema') else '')}
        messages = [state['messages'][0], contract, *state['messages'][1:]]
        limit = state['budgets']['context']
        size = lambda rows: len(json.dumps(rows, ensure_ascii=False))
        if size(messages) <= limit:
            return messages
        head = [messages[0], contract]
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
