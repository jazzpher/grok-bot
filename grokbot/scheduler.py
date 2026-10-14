"""Scheduled automations: "every day at 08:00, check CI and summarise failures".

Jobs live in SQLite; a background thread runs due jobs through a host-provided ``runner(job)`` (the host
builds the agent turn, persists the result into a conversation and delivers it to the web UI / Telegram).
Schedules accept 5-field cron expressions or simple phrases:

    every 15 minutes · every 2 hours · every day at 08:00 · every weekday at 9:30 · every monday at 18:00
    in 2 hours · in 45 minutes · at 17:30 (today or tomorrow) · tomorrow at 09:00 · once at 2026-11-01 09:00
"""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from grokbot import config

WEEKDAYS = {'monday': 0, 'mon': 0, 'tuesday': 1, 'tue': 1, 'tues': 1, 'wednesday': 2, 'wed': 2, 'thursday': 3, 'thu': 3,
            'thurs': 3, 'friday': 4, 'fri': 4, 'saturday': 5, 'sat': 5, 'sunday': 6, 'sun': 6}
UNIT_SECONDS = {'minute': 60, 'minutes': 60, 'min': 60, 'mins': 60, 'hour': 3600, 'hours': 3600, 'hr': 3600, 'hrs': 3600,
                'day': 86400, 'days': 86400, 'week': 604800, 'weeks': 604800}
MIN_INTERVAL = 300


class ScheduleError(ValueError):
    pass


# ---------------------------------------------------------------- cron -----------------------------------------
def _cron_field(field, lo, hi, names=None):
    values = set()
    for part in field.split(','):
        step = 1
        if '/' in part:
            part, step_s = part.split('/', 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise ScheduleError('Bad cron step: ' + step_s)
            step = int(step_s)
        if part in ('*', ''):
            start, end = lo, hi
        elif '-' in part:
            a, b = part.split('-', 1)
            start, end = _cron_value(a, names), _cron_value(b, names)
        else:
            start = end = _cron_value(part, names)
            if step > 1:
                end = hi
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise ScheduleError('Cron value out of range: ' + field)
        values.update(range(start, end + 1, step))
    return values


def _cron_value(token, names):
    token = token.strip().lower()
    if names and token in names:
        return names[token]
    if not token.isdigit():
        raise ScheduleError('Bad cron token: ' + token)
    return int(token)


def parse_cron(expr):
    parts = expr.split()
    if len(parts) != 5:
        raise ScheduleError('Cron needs 5 fields: minute hour day month weekday.')
    minute = _cron_field(parts[0], 0, 59)
    hour = _cron_field(parts[1], 0, 23)
    dom = _cron_field(parts[2], 1, 31)
    month = _cron_field(parts[3], 1, 12, {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
                                          'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12})
    dow = _cron_field(parts[4].replace('7', '0') if parts[4].strip() == '7' else parts[4], 0, 6,
                      {'sun': 0, 'mon': 1, 'tue': 2, 'wed': 3, 'thu': 4, 'fri': 5, 'sat': 6})
    return {'minute': sorted(minute), 'hour': sorted(hour), 'dom': sorted(dom), 'month': sorted(month), 'dow': sorted(dow),
            'dom_star': parts[2].strip() == '*', 'dow_star': parts[4].strip() == '*'}


def cron_next(fields, after):
    """First minute strictly after ``after`` matching the cron fields (local time), or None within 366 days."""
    t = (after + dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
    limit = after + dt.timedelta(days=366)
    minute, hour, dom, month, dow = (set(fields[k]) for k in ('minute', 'hour', 'dom', 'month', 'dow'))
    while t <= limit:
        if t.month not in month:
            t = (t.replace(day=1, hour=0, minute=0) + dt.timedelta(days=32)).replace(day=1)
            continue
        cron_dow = (t.weekday() + 1) % 7  # cron: 0 = Sunday
        day_ok = (t.day in dom, cron_dow in dow)
        if fields['dom_star'] and fields['dow_star']:
            day_match = True
        elif fields['dom_star']:
            day_match = day_ok[1]
        elif fields['dow_star']:
            day_match = day_ok[0]
        else:
            day_match = day_ok[0] or day_ok[1]
        if not day_match:
            t = (t + dt.timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour not in hour:
            t = (t + dt.timedelta(hours=1)).replace(minute=0)
            continue
        if t.minute not in minute:
            t = t + dt.timedelta(minutes=1)
            continue
        return t
    return None


# ---------------------------------------------------------------- natural language ---------------------------
def _hhmm(text):
    m = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', text.strip().lower())
    if not m:
        raise ScheduleError('Time must look like 08:00, 17:30 or 9pm.')
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == 'pm' and h < 12:
        h += 12
    if ap == 'am' and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ScheduleError('Time out of range.')
    return h, mi


def parse_schedule(text, now=None):
    """Return a spec dict: {'kind': 'cron'|'interval'|'once', ...}. Raises ScheduleError."""
    now = now or dt.datetime.now()
    raw = ' '.join((text or '').strip().split())
    low = raw.lower()
    if not low:
        raise ScheduleError('Say when: a cron expression, "every day at 08:00", "every 2 hours", "in 30 minutes"…')
    if low.startswith('cron '):
        low = low[5:]
    if len(low.split()) == 5 and re.fullmatch(r'[\d*,/\-a-z ]+', low):
        parse_cron(low)
        return {'kind': 'cron', 'expr': low, 'text': raw}
    m = re.fullmatch(r'every (\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)', low)
    if m:
        seconds = int(m.group(1)) * UNIT_SECONDS[m.group(2)]
        if seconds < MIN_INTERVAL:
            raise ScheduleError('Minimum interval is 5 minutes.')
        return {'kind': 'interval', 'seconds': seconds, 'text': raw}
    m = re.fullmatch(r'(?:every )?(hourly|daily|weekly)(?: at (.+))?', low)
    if m:
        kind = m.group(1)
        if kind == 'hourly':
            return {'kind': 'cron', 'expr': '0 * * * *', 'text': raw}
        h, mi = _hhmm(m.group(2) or '09:00')
        return {'kind': 'cron', 'expr': '%d %d * * %s' % (mi, h, '*' if kind == 'daily' else '1'), 'text': raw}
    m = re.fullmatch(r'every (day|weekday|weekend|[a-z]+day|mon|tue|tues|wed|thu|thurs|fri|sat|sun)(?: at (.+))?', low)
    if m:
        which, at = m.group(1), m.group(2) or '09:00'
        h, mi = _hhmm(at)
        if which == 'day':
            dow = '*'
        elif which == 'weekday':
            dow = '1-5'
        elif which == 'weekend':
            dow = '0,6'
        elif which in WEEKDAYS:
            dow = str((WEEKDAYS[which] + 1) % 7)
        else:
            raise ScheduleError('Unknown day: ' + which)
        return {'kind': 'cron', 'expr': '%d %d * * %s' % (mi, h, dow), 'text': raw}
    m = re.fullmatch(r'in (\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)', low)
    if m:
        seconds = int(m.group(1)) * UNIT_SECONDS[m.group(2)]
        if seconds < 60:
            raise ScheduleError('Minimum delay is 1 minute.')
        return {'kind': 'once', 'at': (now + dt.timedelta(seconds=seconds)).timestamp(), 'text': raw}
    m = re.fullmatch(r'(?:today |tomorrow |once )?at (.+)', low)
    if m:
        h, mi = _hhmm(m.group(1))
        when = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if low.startswith('tomorrow') or when <= now:
            when += dt.timedelta(days=1)
        return {'kind': 'once', 'at': when.timestamp(), 'text': raw}
    m = re.fullmatch(r'(?:once )?(?:at |on )?(\d{4}-\d{2}-\d{2})(?:[ t](\d{1,2}:\d{2}))?', low)
    if m:
        try:
            when = dt.datetime.strptime(m.group(1) + ' ' + (m.group(2) or '09:00'), '%Y-%m-%d %H:%M')
        except ValueError as exc:
            raise ScheduleError('Bad date.') from exc
        if when <= now:
            raise ScheduleError('That time is in the past.')
        return {'kind': 'once', 'at': when.timestamp(), 'text': raw}
    raise ScheduleError('Could not understand "%s". Try "every day at 08:00", "every 2 hours", "every monday at 9:00", '
                        '"in 30 minutes" or a cron expression.' % raw)


def next_run(spec, after=None):
    """Timestamp of the next run strictly after ``after`` (default now), or None when finished."""
    after_dt = dt.datetime.fromtimestamp(after) if after is not None else dt.datetime.now()
    if spec['kind'] == 'once':
        return spec['at'] if spec['at'] > after_dt.timestamp() else None
    if spec['kind'] == 'interval':
        return after_dt.timestamp() + spec['seconds']
    nxt = cron_next(parse_cron(spec['expr']), after_dt)
    return nxt.timestamp() if nxt else None


def describe(spec):
    if spec['kind'] == 'once':
        return 'once at ' + dt.datetime.fromtimestamp(spec['at']).strftime('%Y-%m-%d %H:%M')
    if spec['kind'] == 'interval':
        s = spec['seconds']
        return 'every ' + ('%d min' % (s // 60) if s < 3600 else '%d h' % (s // 3600) if s < 86400 else '%d d' % (s // 86400))
    return spec.get('text') or ('cron ' + spec['expr'])


# ---------------------------------------------------------------- store + runner ----------------------------
class Scheduler:
    def __init__(self, database, runner=None, tick=None):
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.runner = runner
        self.tick = tick or config.SCHEDULER_TICK
        self.running: set = set()
        self._stop = threading.Event()
        self._thread = None
        with self.lock:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, prompt TEXT NOT NULL, spec TEXT NOT NULL,
                delivery TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
                next_run REAL, last_run REAL, last_status TEXT, last_result TEXT, runs INTEGER NOT NULL DEFAULT 0)''')
            self.db.execute('CREATE INDEX IF NOT EXISTS jobs_due ON jobs(enabled, next_run)')

    @staticmethod
    def _row(r):
        d = dict(r)
        d['spec'] = json.loads(d['spec']); d['delivery'] = json.loads(d['delivery'] or '{}')
        d['enabled'] = bool(d['enabled']); d['when'] = describe(d['spec'])
        return d

    def add(self, owner, when, prompt, title='', delivery=None, now=None):
        if not isinstance(prompt, str) or not 3 <= len(prompt.strip()) <= 4000:
            raise ScheduleError('The task prompt must be 3-4000 characters.')
        spec = parse_schedule(when, now)
        first = next_run(spec, (now or dt.datetime.now()).timestamp() if now else None)
        if first is None:
            raise ScheduleError('That schedule never runs.')
        title = ' '.join((title or prompt).split())[:80]
        with self.lock:
            if self.db.execute('SELECT COUNT(*) FROM jobs WHERE owner=?', (owner,)).fetchone()[0] >= config.SCHEDULER_MAX_JOBS:
                raise ScheduleError('Too many scheduled tasks (%d). Cancel some first.' % config.SCHEDULER_MAX_JOBS)
            jid = secrets.token_hex(5)
            self.db.execute('INSERT INTO jobs(id, owner, title, prompt, spec, delivery, enabled, created, next_run) VALUES (?,?,?,?,?,?,1,?,?)',
                            (jid, owner, title, prompt.strip(), json.dumps(spec), json.dumps(delivery or {}), time.time(), first))
        return self.get(owner, jid)

    def get(self, owner, jid):
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE id=? AND owner=?', (jid, owner)).fetchone()
        return self._row(row) if row else None

    def list(self, owner):
        with self.lock:
            rows = self.db.execute('SELECT * FROM jobs WHERE owner=? ORDER BY enabled DESC, next_run', (owner,)).fetchall()
        return [self._row(r) for r in rows]

    def delete(self, owner, jid):
        with self.lock:
            return self.db.execute('DELETE FROM jobs WHERE id=? AND owner=?', (jid, owner)).rowcount > 0

    def set_enabled(self, owner, jid, enabled):
        job = self.get(owner, jid)
        if job is None:
            return None
        nxt = next_run(job['spec']) if enabled else job['next_run']
        with self.lock:
            self.db.execute('UPDATE jobs SET enabled=?, next_run=? WHERE id=?', (1 if enabled else 0, nxt, jid))
        return self.get(owner, jid)

    def due(self, now=None):
        now = now or time.time()
        with self.lock:
            rows = self.db.execute('SELECT * FROM jobs WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=? ORDER BY next_run', (now,)).fetchall()
        return [self._row(r) for r in rows if r['id'] not in self.running]

    def mark_started(self, job):
        with self.lock:
            self.running.add(job['id'])
            nxt = next_run(job['spec'], time.time())
            self.db.execute('UPDATE jobs SET last_run=?, next_run=?, enabled=? WHERE id=?',
                            (time.time(), nxt, 0 if nxt is None else 1, job['id']))

    def mark_finished(self, job, status, result_text=''):
        with self.lock:
            self.running.discard(job['id'])
            self.db.execute('UPDATE jobs SET last_status=?, last_result=?, runs=runs+1 WHERE id=?',
                            (status, (result_text or '')[:4000], job['id']))

    def run_job(self, job):
        """Run one job through the host runner, recording status. Safe to call from any thread."""
        if self.runner is None:
            raise ScheduleError('No runner configured.')
        self.mark_started(job)
        try:
            outcome = self.runner(job)
            status, text = (outcome if isinstance(outcome, tuple) else ('completed', str(outcome or '')))
        except Exception as exc:  # noqa: BLE001
            status, text = 'error', type(exc).__name__ + ': ' + str(exc)[:500]
        self.mark_finished(job, status, text)
        return status, text

    def run_now(self, owner, jid):
        job = self.get(owner, jid)
        if job is None:
            return None
        return self.run_job(job)

    def run_due(self, now=None):
        ran = []
        for job in self.due(now):
            ran.append((job['id'],) + tuple(self.run_job(job)))
        return ran

    def start(self):
        if self._thread is not None:
            return self
        self._stop.clear()

        def loop():
            while not self._stop.wait(self.tick):
                try:
                    for job in self.due():
                        threading.Thread(target=self.run_job, args=(job,), daemon=True, name='job-' + job['id']).start()
                except Exception:  # noqa: BLE001
                    pass
        self._thread = threading.Thread(target=loop, daemon=True, name='grokbot-scheduler')
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread = None

    def close(self):
        self.stop()
        with self.lock:
            self.db.close()
