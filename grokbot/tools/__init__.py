"""Typed tool registry.

Adding a tool is one function with a JSON-schema signature:

    @registry.tool('greet', 'Say hello', {'type': 'object', 'properties': {...}, 'required': [...]})
    def greet(ctx, name):
        return 'hello ' + name

The same registry produces native OpenAI ``tools`` definitions and the prompt
block used by the JSON fallback protocol, validates arguments before execution,
labels output as untrusted data, and bounds its size.
"""
import dataclasses
import json
import threading
import time
from typing import Any, Callable, Dict, Optional

from grokbot import config

TYPE_MAP = {'string': str, 'integer': int, 'number': (int, float), 'boolean': bool,
            'array': list, 'object': dict}


class ToolError(ValueError):
    """A tool refused or failed; the message is safe to show to the model and user."""


class ApprovalDenied(ToolError):
    """The user denied a tool call that required approval."""


@dataclasses.dataclass
class ToolContext:
    workspace: Any                                  # agent_loop.Workspace (path guards, limits)
    session_id: str = ''
    policy: Any = None                              # grokbot.approvals.SandboxPolicy
    approvals: Any = None                           # grokbot.approvals.ApprovalBroker
    stop: threading.Event = dataclasses.field(default_factory=threading.Event)
    emit: Callable[[dict], None] = lambda event: None
    extras: Dict[str, Any] = dataclasses.field(default_factory=dict)
    turn_id: str = ''


@dataclasses.dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., str]
    category: str = 'general'
    mutates: bool = False
    needs_approval: Optional[Callable[[ToolContext, dict], Optional[str]]] = None
    available: Optional[Callable[[ToolContext], bool]] = None
    parallel_safe: bool = False                      # may run concurrently with other calls in the same step

    def openai(self):
        return {'type': 'function', 'function': {
            'name': self.name, 'description': self.description, 'parameters': self.parameters}}


@dataclasses.dataclass
class ToolResult:
    name: str
    arguments: dict
    output: str
    ok: bool
    duration: float
    truncated: bool = False
    meta: Dict[str, Any] = dataclasses.field(default_factory=dict)


def validate_arguments(spec, arguments):
    """Minimal JSON-schema validation: required keys, primitive types, enums, unknown keys."""
    if not isinstance(arguments, dict):
        raise ToolError(spec.name + ': arguments must be a JSON object.')
    props = spec.parameters.get('properties', {}) or {}
    required = spec.parameters.get('required', []) or []
    missing = [k for k in required if k not in arguments or arguments[k] is None]
    if missing:
        raise ToolError(spec.name + ' requires: ' + ', '.join(missing) + '.')
    if spec.parameters.get('additionalProperties') is False:
        unknown = [k for k in arguments if k not in props]
        if unknown:
            raise ToolError(spec.name + ' got unknown arguments: ' + ', '.join(unknown) + '.')
    clean = {}
    for key, value in arguments.items():
        schema = props.get(key)
        if schema is None:
            continue
        expected = schema.get('type')
        if value is None:
            if key in required:
                raise ToolError(spec.name + ': ' + key + ' must not be null.')
            continue
        if isinstance(expected, str) and expected in TYPE_MAP:
            # JSON models often send numbers as strings; coerce the obvious cases.
            if expected == 'integer' and isinstance(value, str) and value.strip().lstrip('-').isdigit():
                value = int(value)
            elif expected == 'number' and isinstance(value, str):
                try:
                    value = float(value)
                except ValueError:
                    pass
            elif expected == 'boolean' and isinstance(value, str) and value.lower() in ('true', 'false'):
                value = value.lower() == 'true'
            if isinstance(value, bool) and expected in ('integer', 'number'):
                raise ToolError(spec.name + ': ' + key + ' must be a ' + expected + '.')
            if not isinstance(value, TYPE_MAP[expected]):
                raise ToolError(spec.name + ': ' + key + ' must be a ' + expected + '.')
        if 'enum' in schema and value not in schema['enum']:
            raise ToolError(spec.name + ': ' + key + ' must be one of ' + ', '.join(map(str, schema['enum'])) + '.')
        if expected == 'string' and 'maxLength' in schema and len(value) > schema['maxLength']:
            raise ToolError(spec.name + ': ' + key + ' is too long.')
        clean[key] = value
    return clean


def bound_output(text, limit=None):
    limit = limit or config.MAX_TOOL_OUTPUT_CHARS
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    return (text[:head] + '\n…[output truncated: %d of %d characters shown]…\n' % (limit, len(text))
            + text[-tail:]), True


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec):
        if spec.name in self._tools:
            raise ValueError('Duplicate tool: ' + spec.name)
        self._tools[spec.name] = spec
        return spec

    def tool(self, name, description, parameters, **kwargs):
        def decorator(fn):
            self.register(ToolSpec(name, description, parameters, fn, **kwargs))
            return fn
        return decorator

    def names(self):
        return list(self._tools)

    def unregister(self, name):
        return self._tools.pop(name, None)

    def names_for(self, ctx):
        return [name for name, spec in self._tools.items() if spec.available is None or spec.available(ctx)]

    def describe(self):
        """Public summary for /capabilities and the CLI."""
        return [{'name': t.name, 'description': t.description.split('\n')[0][:200], 'category': t.category, 'mutates': t.mutates}
                for t in self._tools.values()]

    def get(self, name):
        return self._tools.get(name)

    def __contains__(self, name):
        return name in self._tools

    def __len__(self):
        return len(self._tools)

    def specs(self, ctx=None):
        for spec in self._tools.values():
            if ctx is not None and spec.available is not None and not spec.available(ctx):
                continue
            yield spec

    def subset(self, names):
        sub = ToolRegistry()
        for name in names:
            if name in self._tools:
                sub.register(self._tools[name])
        return sub

    def openai_tools(self, ctx=None):
        return [spec.openai() for spec in self.specs(ctx)]

    def prompt_block(self, ctx=None):
        """Compact tool documentation for providers without native tool calling."""
        lines = []
        for spec in self.specs(ctx):
            props = spec.parameters.get('properties', {}) or {}
            required = set(spec.parameters.get('required', []) or [])
            args = ', '.join(k + ('' if k in required else '?') + ': ' + str(v.get('type', 'any')) for k, v in props.items())
            lines.append('- ' + spec.name + '(' + args + '): ' + spec.description.split('\n')[0])
        return '\n'.join(lines)

    def execute(self, ctx: ToolContext, name, arguments) -> ToolResult:
        started = time.monotonic()
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(name, arguments if isinstance(arguments, dict) else {},
                              'Tool error: unknown tool ' + str(name) + '. Available: ' + ', '.join(self._tools), False,
                              time.monotonic() - started)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                return ToolResult(name, {}, 'Tool error: arguments were not valid JSON.', False, time.monotonic() - started)
        from grokbot import plugins
        result = None
        try:
            clean = validate_arguments(spec, arguments)
            if spec.available is not None and not spec.available(ctx):
                raise ToolError(name + ' is not available in this configuration.')
            clean = plugins.run_before_tool(ctx, name, clean)
            if spec.needs_approval is not None and ctx.approvals is not None:
                reason = spec.needs_approval(ctx, clean)
                if reason:
                    decision = ctx.approvals.request(ctx, name, clean, reason)
                    if decision != 'allow':
                        raise ApprovalDenied('The user denied ' + name + '. Ask how to proceed instead of retrying.')
            output = spec.handler(ctx, **clean)
            if output is None:
                output = ''
            text, truncated = bound_output(output)
            result = ToolResult(name, clean, text, True, time.monotonic() - started, truncated)
        except ToolError as exc:
            result = ToolResult(name, arguments if isinstance(arguments, dict) else {}, 'Tool error: ' + str(exc), False, time.monotonic() - started)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            result = ToolResult(name, arguments if isinstance(arguments, dict) else {},
                                'Tool error: ' + type(exc).__name__ + ': ' + str(exc)[:500], False, time.monotonic() - started)
        return plugins.run_after_tool(ctx, result)


def default_registry(plugins_too=True):
    """All built-in tools. Availability of shell/web/memory/scheduler tools is decided per context."""
    from grokbot.tools import delegate, files, memory, misc, schedule, shell, skills, web
    registry = ToolRegistry()
    for module in (files, misc, skills, shell, web, memory, delegate, schedule):
        module.register(registry)
    if plugins_too:
        from grokbot import plugins
        plugins.load(registry)
    return registry
