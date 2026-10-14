"""Sub-agents: ``delegate`` runs a self-contained subtask in a fresh context (same workspace, same policy,
bounded budget) and returns only the result to the parent. Several delegate calls in one step run in parallel."""
from __future__ import annotations

import threading

from grokbot import config
from grokbot.tools import ToolContext, ToolError

EXCLUDED_FOR_SUBAGENTS = {'delegate', 'save_skill', 'save_memory', 'forget_memory', 'schedule_task', 'cancel_scheduled_task'}
_counter = [0]
_counter_lock = threading.Lock()


def register(registry):
    @registry.tool('delegate', 'Delegate a self-contained subtask to a fresh sub-agent that shares the workspace and sandbox '
                   'policy but has its own context window and step budget. It returns only its final report. Use for '
                   'independent research or implementation chunks; call delegate several times in one step to run them in '
                   'parallel. Give it everything it needs in the task text — it does not see this conversation.',
                   {'type': 'object', 'properties': {
                       'task': {'type': 'string', 'description': 'Complete instructions for the sub-agent'},
                       'context': {'type': 'string', 'description': 'Relevant facts, file paths, constraints'},
                       'max_steps': {'type': 'integer', 'description': 'Step budget (default 15, max 30)'}},
                    'required': ['task']}, category='agents', mutates=True, parallel_safe=True)
    def delegate(ctx, task, context='', max_steps=15):
        from grokbot import agent as agent_core
        depth = int(ctx.extras.get('depth', 0))
        if depth >= config.MAX_SUBAGENT_DEPTH:
            raise ToolError('Sub-agents cannot delegate further (max depth %d). Do the work directly.' % config.MAX_SUBAGENT_DEPTH)
        parent_registry = ctx.extras.get('registry')
        if parent_registry is None:
            raise ToolError('delegate is unavailable in this context.')
        with _counter_lock:
            _counter[0] += 1
            label = 'sub-%d' % _counter[0]
        max_steps = max(1, min(int(max_steps or 15), 30))
        names = [n for n in parent_registry.names() if n not in EXCLUDED_FOR_SUBAGENTS]
        sub_registry = parent_registry.subset(names)
        parent_emit = ctx.emit

        def sub_emit(event):
            kind = event.get('type')
            if kind in ('done',):
                event = dict(event, type='subagent_done')
            parent_emit(dict(event, agent=label, depth=depth + 1))
        sub_ctx = ToolContext(workspace=ctx.workspace, session_id=ctx.session_id, policy=ctx.policy, approvals=ctx.approvals,
                              stop=ctx.stop, emit=sub_emit,
                              extras={k: v for k, v in ctx.extras.items() if k not in ('registry', 'depth')})
        sub_ctx.extras['depth'] = depth + 1
        prompt = 'You are a sub-agent. Complete this task, then reply with a concise report of what you did, what you ' \
                 'found and anything the caller must know (file paths, commands run, open issues).\n\nTASK:\n' + task
        if context:
            prompt += '\n\nCONTEXT:\n' + context
        parent_emit({'type': 'subagent_start', 'agent': label, 'task': task[:300], 'depth': depth + 1})
        result = agent_core.run_turn([{'role': 'user', 'content': prompt}], sub_registry, sub_ctx, ctx.extras.get('cfg'),
                                     sub_emit, max_steps=max_steps, context_tokens=ctx.extras.get('context_tokens'),
                                     model_label=ctx.extras.get('model_label', ''), stream_fn=ctx.extras.get('stream_fn'),
                                     summarizer=ctx.extras.get('summarizer'))
        parent_emit({'type': 'subagent_end', 'agent': label, 'status': result.status, 'steps': result.steps,
                     'tool_calls': result.tool_calls, 'depth': depth + 1})
        head = 'Sub-agent %s finished (%s after %d steps, %d tool calls).\nREPORT:\n' % (label, result.status, result.steps, result.tool_calls)
        return head + (result.text or '(no report)')
