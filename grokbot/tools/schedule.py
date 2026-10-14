"""Scheduling tools. Available when the host attaches ``ctx.extras['scheduler']`` (grokbot.scheduler.Scheduler)."""
import datetime as dt

from grokbot.scheduler import ScheduleError
from grokbot.tools import ToolError


def _owner(ctx):
    return ctx.extras.get('owner') or ctx.session_id


def _fmt(ts):
    return dt.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M') if ts else '—'


def register(registry):
    has_sched = lambda ctx: ctx.extras.get('scheduler') is not None  # noqa: E731

    @registry.tool('schedule_task', 'Schedule a prompt to run later or on a repeating schedule as an unattended agent run '
                   '(results appear as a new conversation / message). "when" accepts "every day at 08:00", "every 2 hours", '
                   '"every monday at 9:00", "in 30 minutes", "tomorrow at 09:00" or a 5-field cron expression. '
                   'Unattended runs cannot ask for approval, so only allowed tools run.',
                   {'type': 'object', 'properties': {
                       'when': {'type': 'string'},
                       'prompt': {'type': 'string', 'description': 'The full task to run each time'},
                       'title': {'type': 'string', 'description': 'Short label'}},
                    'required': ['when', 'prompt']}, category='automation', mutates=True, available=has_sched)
    def schedule_task(ctx, when, prompt, title=''):
        try:
            job = ctx.extras['scheduler'].add(_owner(ctx), when, prompt, title, delivery=ctx.extras.get('delivery') or {})
        except ScheduleError as exc:
            raise ToolError(str(exc)) from exc
        ctx.emit({'type': 'schedule_created', 'id': job['id'], 'title': job['title'], 'when': job['when'], 'next_run': job['next_run']})
        return 'Scheduled "%s" (id %s): %s · next run %s.' % (job['title'], job['id'], job['when'], _fmt(job['next_run']))

    @registry.tool('list_scheduled_tasks', 'List this user\'s scheduled tasks with their next run and last status.',
                   {'type': 'object', 'properties': {}, 'required': []}, category='automation', available=has_sched)
    def list_scheduled_tasks(ctx):
        jobs = ctx.extras['scheduler'].list(_owner(ctx))
        if not jobs:
            return 'No scheduled tasks.'
        return '\n'.join('- [%s] %s · %s · next %s · last %s%s' % (
            j['id'], j['title'], j['when'], _fmt(j['next_run']) if j['enabled'] else 'disabled',
            j['last_status'] or 'never', (' (' + (j['last_result'] or '')[:80] + ')') if j['last_result'] else '') for j in jobs)

    @registry.tool('cancel_scheduled_task', 'Cancel (delete) a scheduled task by id.',
                   {'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']},
                   category='automation', mutates=True, available=has_sched)
    def cancel_scheduled_task(ctx, id):
        if not ctx.extras['scheduler'].delete(_owner(ctx), id):
            raise ToolError('No scheduled task with that id.')
        return 'Cancelled scheduled task ' + id + '.'
