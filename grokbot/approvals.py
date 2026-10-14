"""Sandbox policy and approval broker (Codex-style two dials).

* ``mode``     — what tools may touch:  read-only | workspace-write | full-access
* ``approval`` — when to ask the user:  on-request | never

``needs_approval`` hooks on tools return a *reason* when a call crosses the
sandbox boundary. In ``on-request`` the broker pauses the turn, emits an
``approval_required`` event, and waits for the UI/CLI to answer allow / deny /
always. In ``never`` such calls are denied automatically (nothing escapes the
sandbox without a human) — except ``full-access`` + ``never`` which runs
everything (YOLO; for trusted automation only).
"""
import dataclasses
import json
import os
import pathlib
import secrets
import shlex
import tempfile
import threading
import time
from typing import Dict, List, Optional

from grokbot import config

MODES = ('read-only', 'workspace-write', 'full-access')
APPROVALS = ('on-request', 'never')


@dataclasses.dataclass
class SandboxPolicy:
    mode: str = config.DEFAULT_SANDBOX_MODE
    approval: str = config.DEFAULT_APPROVAL
    network: bool = config.SANDBOX_NETWORK == 'on'
    allow: List[str] = dataclasses.field(default_factory=list)   # "run_command:git status", "delete_file:*"
    deny: List[str] = dataclasses.field(default_factory=list)

    @classmethod
    def from_dict(cls, data):
        data = data if isinstance(data, dict) else {}
        policy = cls()
        if data.get('mode') in MODES:
            policy.mode = data['mode']
        if data.get('approval') in APPROVALS:
            policy.approval = data['approval']
        if isinstance(data.get('network'), bool):
            policy.network = data['network']
        for field in ('allow', 'deny'):
            values = data.get(field)
            if isinstance(values, list):
                setattr(policy, field, [v for v in values if isinstance(v, str) and 0 < len(v) <= 300][:500])
        return policy

    def to_dict(self):
        return dataclasses.asdict(self)

    @property
    def yolo(self):
        return self.mode == 'full-access' and self.approval == 'never'

    def label(self):
        return self.mode + ' · ' + self.approval + (' · network' if self.network else '')


def allow_pattern(tool, arguments):
    """Pattern stored by 'always allow': tool plus the first two command words."""
    if tool == 'run_command':
        try:
            tokens = shlex.split(str(arguments.get('command', '')))
        except ValueError:
            tokens = str(arguments.get('command', '')).split()
        words = [t for t in tokens[:2] if t and not t.startswith('-')]
        return 'run_command:' + ' '.join(words[:2] or ['*'])
    return tool + ':*'


def pattern_matches(pattern, tool, arguments):
    if ':' not in pattern:
        return False
    p_tool, p_match = pattern.split(':', 1)
    if p_tool != tool:
        return False
    if p_match == '*':
        return True
    if tool == 'run_command':
        try:
            tokens = shlex.split(str(arguments.get('command', '')))
        except ValueError:
            return False
        words = [t for t in tokens if t and not t.startswith('-')]
        want = p_match.split()
        return words[:len(want)] == want
    return False


class PolicyStore:
    """Per-session policy persisted as JSON under <workspaces>/.policy/<sid>.json."""

    def __init__(self, root):
        self.root = pathlib.Path(root) / '.policy'
        self.lock = threading.Lock()

    def _path(self, sid):
        safe = ''.join(c for c in str(sid) if c.isalnum() or c in '-_')[:64] or 'default'
        return self.root / (safe + '.json')

    def load(self, sid):
        try:
            return SandboxPolicy.from_dict(json.loads(self._path(sid).read_text(encoding='utf-8')))
        except (OSError, ValueError):
            return SandboxPolicy()

    def save(self, sid, policy):
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self._path(sid)
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix='.policy-')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(policy.to_dict(), f)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        return policy


@dataclasses.dataclass
class PendingApproval:
    id: str
    session_id: str
    turn_id: str
    tool: str
    arguments: dict
    reason: str
    created: float
    event: threading.Event = dataclasses.field(default_factory=threading.Event)
    decision: Optional[str] = None


class ApprovalBroker:
    def __init__(self, store: Optional[PolicyStore] = None, timeout=None):
        self.store = store
        self.timeout = timeout or config.APPROVAL_TIMEOUT
        self.pending: Dict[str, PendingApproval] = {}
        self.lock = threading.Lock()

    def request(self, ctx, tool, arguments, reason):
        policy = ctx.policy or SandboxPolicy()
        for pattern in policy.deny:
            if pattern_matches(pattern, tool, arguments):
                ctx.emit({'type': 'approval', 'tool': tool, 'decision': 'deny', 'reason': 'Denied by your policy deny list.'})
                return 'deny'
        strict = bool(tool == 'run_command' and (getattr(ctx, 'extras', None) or {}).get('strict_approval'))
        for pattern in ([] if strict else policy.allow):
            if pattern_matches(pattern, tool, arguments):
                ctx.emit({'type': 'approval', 'tool': tool, 'decision': 'allow', 'reason': 'Allowed by your saved rule: ' + pattern})
                return 'allow'
        if policy.approval == 'never':
            ctx.emit({'type': 'approval', 'tool': tool, 'decision': 'deny',
                      'reason': 'Approval policy is "never": ' + reason + (' Switch to on-request to approve commands one by one.' if strict else ' Switch to on-request or add an allow rule.')})
            return 'deny'
        pending = PendingApproval(secrets.token_hex(8), ctx.session_id, ctx.turn_id, tool, arguments, reason, time.time())
        with self.lock:
            self.pending[pending.id] = pending
        ctx.emit({'type': 'approval_required', 'id': pending.id, 'tool': tool, 'arguments': arguments, 'reason': reason,
                  'always_pattern': allow_pattern(tool, arguments)})
        deadline = time.monotonic() + self.timeout
        try:
            while not pending.event.is_set():
                if ctx.stop.is_set():
                    pending.decision = 'deny'
                    break
                if time.monotonic() > deadline:
                    pending.decision = 'deny'
                    ctx.emit({'type': 'approval', 'id': pending.id, 'tool': tool, 'decision': 'deny', 'reason': 'No answer within the approval timeout.'})
                    break
                pending.event.wait(0.25)
        finally:
            with self.lock:
                self.pending.pop(pending.id, None)
        decision = pending.decision or 'deny'
        if decision == 'always' and strict:
            decision = 'allow'   # no saved rules for host commands without an isolated sandbox: this one time only
        if decision == 'always':
            pattern = allow_pattern(tool, arguments)
            if pattern not in policy.allow:
                policy.allow.append(pattern)
                if self.store is not None and ctx.session_id:
                    self.store.save(ctx.session_id, policy)
            ctx.emit({'type': 'approval', 'id': pending.id, 'tool': tool, 'decision': 'allow', 'reason': 'Saved rule: ' + pattern})
            return 'allow'
        if pending.event.is_set():
            ctx.emit({'type': 'approval', 'id': pending.id, 'tool': tool, 'decision': decision, 'reason': 'Your decision.'})
        return 'allow' if decision == 'allow' else 'deny'

    def resolve(self, request_id, decision, session_id=None):
        if decision not in ('allow', 'deny', 'always'):
            return False
        with self.lock:
            pending = self.pending.get(request_id)
            if pending is None or (session_id is not None and pending.session_id != session_id):
                return False
            pending.decision = decision
            pending.event.set()
            return True

    def list_pending(self, session_id=None):
        with self.lock:
            return [{'id': p.id, 'tool': p.tool, 'arguments': p.arguments, 'reason': p.reason, 'turn_id': p.turn_id,
                     'always_pattern': allow_pattern(p.tool, p.arguments)}
                    for p in self.pending.values() if session_id is None or p.session_id == session_id]

    def cancel_all(self, session_id=None):
        with self.lock:
            for p in list(self.pending.values()):
                if session_id is None or p.session_id == session_id:
                    p.decision = 'deny'
                    p.event.set()
