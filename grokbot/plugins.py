"""Tiny plugin/hook system so grok-bot can be extended without forking.

A plugin is a Python module named in ``GROK_PLUGINS`` (comma-separated import paths) or exposed through the
``grokbot.plugins`` entry-point group. It may define any of:

    register(registry)                      -> add tools with registry.tool(...)
    before_tool(ctx, name, arguments)       -> return (possibly modified) arguments, or raise ToolError to block
    after_tool(ctx, result)                 -> return (possibly modified) ToolResult
    on_turn_end(ctx, result)                -> observe a finished turn (logging, metrics, notifications)
    on_message(owner, text)                 -> observe inbound messages (gateways)

Hooks run in load order; a failing hook is logged and skipped, it never breaks the agent.
"""
from __future__ import annotations

import importlib
import logging
import os
import threading

log = logging.getLogger('grokbot.plugins')
_lock = threading.Lock()
_loaded: list = []
_names: list = []


def load(registry=None, names=None):
    """Import plugins (idempotent per module) and let them register tools."""
    spec = names if names is not None else [n.strip() for n in os.environ.get('GROK_PLUGINS', '').split(',') if n.strip()]
    modules = []
    with _lock:
        for name in spec:
            if name in _names:
                modules.append(next(m for n, m in zip(_names, _loaded) if n == name))
                continue
            try:
                module = importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001
                log.warning('plugin %s failed to import: %s', name, exc)
                continue
            _names.append(name); _loaded.append(module); modules.append(module)
        try:
            from importlib import metadata
            for ep in metadata.entry_points(group='grokbot.plugins'):
                if ep.name in _names:
                    continue
                try:
                    module = ep.load()
                except Exception as exc:  # noqa: BLE001
                    log.warning('plugin entry point %s failed: %s', ep.name, exc)
                    continue
                _names.append(ep.name); _loaded.append(module); modules.append(module)
        except Exception:  # noqa: BLE001 - metadata may be unavailable in frozen builds
            pass
    if registry is not None:
        for module in modules:
            fn = getattr(module, 'register', None)
            if callable(fn):
                try:
                    fn(registry)
                except Exception as exc:  # noqa: BLE001
                    log.warning('plugin %s register() failed: %s', getattr(module, '__name__', module), exc)
    return modules


def reset():
    with _lock:
        _loaded.clear(); _names.clear()


def loaded():
    return list(_names)


def _hooks(name):
    return [getattr(m, name) for m in list(_loaded) if callable(getattr(m, name, None))]


def run_before_tool(ctx, name, arguments):
    from grokbot.tools import ToolError
    for hook in _hooks('before_tool'):
        try:
            out = hook(ctx, name, arguments)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning('before_tool hook failed: %s', exc)
            continue
        if isinstance(out, dict):
            arguments = out
    return arguments


def run_after_tool(ctx, result):
    for hook in _hooks('after_tool'):
        try:
            out = hook(ctx, result)
        except Exception as exc:  # noqa: BLE001
            log.warning('after_tool hook failed: %s', exc)
            continue
        if out is not None:
            result = out
    return result


def run_turn_end(ctx, result):
    for hook in _hooks('on_turn_end'):
        try:
            hook(ctx, result)
        except Exception as exc:  # noqa: BLE001
            log.warning('on_turn_end hook failed: %s', exc)


def run_message(owner, text):
    for hook in _hooks('on_message'):
        try:
            hook(owner, text)
        except Exception as exc:  # noqa: BLE001
            log.warning('on_message hook failed: %s', exc)
