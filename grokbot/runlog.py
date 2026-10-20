"""Per-run log: one JSON line per agent turn (interactive, CLI, scheduled, Telegram, durable task).

Each entry records the run id, source, model, start/end times, status, duration, step and
tool counts, every tool call (name, ok, duration, error) and the turn's final error, so a
failed or slow run can be diagnosed after the fact instead of guessed at. The log lives at
``.runlog.jsonl`` in the data directory and is rotated by size (keep the newest lines).
"""
import json
import os
import threading
import time

_LOCK = threading.Lock()
_PATH = None
MAX_BYTES = 5 * 1024 * 1024
KEEP_TAIL_BYTES = 2 * 1024 * 1024
MAX_TOOLS_PER_RUN = 200


def configure(path):
    """Enable the log at ``path`` (called once by the host app). None disables it."""
    global _PATH
    with _LOCK:
        _PATH = str(path) if path else None


def configured_path():
    with _LOCK:
        return _PATH


def record(entry, path=None):
    """Append one run entry. Never raises: logging must not break a run."""
    target = path or configured_path()
    if not target:
        return
    try:
        line = json.dumps(entry, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return
    with _LOCK:
        try:
            with open(target, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')
            _rotate(target)
        except OSError:
            pass


def _rotate(target):
    try:
        size = os.path.getsize(target)
        if size <= MAX_BYTES:
            return
        keep = min(KEEP_TAIL_BYTES, size // 2)
        with open(target, 'rb') as fh:
            fh.seek(-keep, os.SEEK_END)
            tail = fh.read()
        nl = tail.find(b'\n')
        tail = tail[nl + 1:] if nl >= 0 else tail
        with open(target, 'wb') as fh:
            fh.write(tail)
    except OSError:
        pass


def recent(limit=50, path=None):
    """Newest ``limit`` entries, oldest first."""
    target = path or configured_path()
    if not target:
        return []
    try:
        with open(target, 'r', encoding='utf-8') as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def entry_from_turn(ctx, result, started, error=''):
    """Build the log entry for a finished ``agent.run_turn``."""
    tools = ctx.extras.get('run_tools') or []
    return {
        'run_id': ctx.turn_id,
        'source': ctx.extras.get('run_source', 'agent'),
        'conversation': ctx.extras.get('conversation_id'),
        'model': ctx.extras.get('model_label') or '',
        'goal': (ctx.extras.get('run_goal') or '')[:200],
        'started': started,
        'ended': time.time(),
        'duration': round(time.time() - started, 3),
        'status': result.status,
        'steps': result.steps,
        'tool_calls': result.tool_calls,
        'tools': tools[:MAX_TOOLS_PER_RUN],
        'usage': {k: v for k, v in (result.usage or {}).items() if isinstance(v, int)},
        'error': (error or '')[:300],
    }
