"""OpenAI-compatible streaming client with native tool-call assembly.

``stream_chat`` yields small event dicts: text deltas, reasoning deltas,
assembled tool calls, usage and the finish reason. The response body is
bounded, malformed SSE lines are skipped, and a provider that rejects the
``tools`` parameter raises ``ToolsUnsupported`` so the agent can fall back to
the JSON protocol.
"""
import json

import requests

DEFAULT_TIMEOUT = (10, 120)
MAX_BODY_BYTES = 6 * 1024 * 1024


class ProviderError(Exception):
    def __init__(self, message, status=None, response=None):
        super().__init__(message)
        self.status = status
        self.response = response


class ToolsUnsupported(ProviderError):
    """The endpoint rejected the tools/tool_choice parameters."""


class ResponseTooLarge(ProviderError):
    pass


def _headers(key):
    return {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Accept': 'text/event-stream'}


def iter_sse_lines(response, max_bytes=MAX_BODY_BYTES):
    received = 0
    pending = bytearray()
    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue
        received += len(chunk)
        if received > max_bytes:
            raise ResponseTooLarge('Provider stream exceeded the response limit.')
        pending.extend(chunk)
        while True:
            pos = pending.find(b'\n')
            if pos < 0:
                break
            line = bytes(pending[:pos]).rstrip(b'\r')
            del pending[:pos + 1]
            yield line
    if pending:
        yield bytes(pending).rstrip(b'\r')


def _looks_like_tools_rejection(body):
    text = (body or '').lower()
    return 'tool' in text and ('not support' in text or 'unsupported' in text or 'invalid' in text or 'unknown parameter' in text
                               or 'unrecognized' in text or 'function' in text)


def stream_chat(cfg, messages, tools=None, stop=None, timeout=DEFAULT_TIMEOUT, extra=None, include_usage=True, max_bytes=MAX_BODY_BYTES):
    """Yield events from a streaming chat completion. ``stop`` is an optional threading.Event."""
    key, url, model = cfg
    payload = {'model': model, 'messages': messages, 'stream': True}
    if include_usage:
        payload['stream_options'] = {'include_usage': True}
    if tools:
        payload['tools'] = tools
        payload['tool_choice'] = 'auto'
    if extra:
        payload.update(extra)
    try:
        res = requests.post(url, headers=_headers(key), json=payload, stream=True, timeout=timeout)
    except requests.RequestException:
        raise
    if res.status_code == 400 and include_usage:
        # Some providers reject stream_options; retry once without it before blaming tools.
        body = res.text[:2000]
        res.close()
        if 'stream_options' in body:
            yield from stream_chat(cfg, messages, tools, stop, timeout, extra, include_usage=False, max_bytes=max_bytes)
            return
        if tools and _looks_like_tools_rejection(body):
            raise ToolsUnsupported(body, 400)
        exc = requests.HTTPError('400 Client Error', response=res)
        raise exc
    if res.status_code == 400 and tools:
        body = res.text[:2000]
        res.close()
        if _looks_like_tools_rejection(body):
            raise ToolsUnsupported(body, 400)
        raise requests.HTTPError('400 Client Error', response=res)
    try:
        res.raise_for_status()
    except requests.RequestException:
        res.close()
        raise
    calls = {}
    finish = None
    usage = None
    complete = False
    with res:
        for raw in iter_sse_lines(res, max_bytes):
            if stop is not None and stop.is_set():
                break
            try:
                line = raw.decode('utf-8')
            except UnicodeDecodeError:
                continue
            if not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                complete = True
                break
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('error') is not None:
                err = event['error']
                message = err.get('message') if isinstance(err, dict) else str(err)
                raise ProviderError('Provider error: ' + str(message)[:300], (err.get('code') if isinstance(err, dict) else None))
            if isinstance(event.get('usage'), dict):
                usage = event['usage']
            choices = event.get('choices')
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get('delta') if isinstance(choice.get('delta'), dict) else {}
            if choice.get('finish_reason'):
                finish = choice['finish_reason']
            reasoning = delta.get('reasoning_content') or delta.get('reasoning')
            if isinstance(reasoning, str) and reasoning:
                yield {'type': 'reasoning', 'delta': reasoning}
            content = delta.get('content')
            if isinstance(content, str) and content:
                yield {'type': 'text', 'delta': content}
            elif isinstance(content, list):  # some providers stream content parts
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get('text'), str) and part['text']:
                        yield {'type': 'text', 'delta': part['text']}
            tool_calls = delta.get('tool_calls')
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    index = tc.get('index', len(calls))
                    slot = calls.setdefault(index, {'id': '', 'name': '', 'arguments': ''})
                    if tc.get('id'):
                        slot['id'] = str(tc['id'])
                    fn = tc.get('function') if isinstance(tc.get('function'), dict) else {}
                    name = fn.get('name')
                    if isinstance(name, str) and name:
                        if not slot['name']:
                            slot['name'] = name          # usual case: full name in the first delta
                        elif name != slot['name'] and not slot['name'].endswith(name):
                            slot['name'] += name         # rare: name streamed in pieces
                    if isinstance(fn.get('arguments'), str):
                        slot['arguments'] += fn['arguments']
            # Non-streaming style full message (some proxies send it once)
            message = choice.get('message') if isinstance(choice.get('message'), dict) else None
            if message:
                if isinstance(message.get('content'), str) and message['content']:
                    yield {'type': 'text', 'delta': message['content']}
                for i, tc in enumerate(message.get('tool_calls') or []):
                    if isinstance(tc, dict):
                        fn = tc.get('function') or {}
                        calls[('m', i)] = {'id': str(tc.get('id') or ''), 'name': str(fn.get('name') or ''),
                                           'arguments': fn.get('arguments') if isinstance(fn.get('arguments'), str) else json.dumps(fn.get('arguments') or {})}
                finish = finish or 'stop'
                complete = True
    if calls:
        ordered = [calls[k] for k in sorted(calls, key=lambda k: (isinstance(k, tuple), k))]
        for i, call in enumerate(ordered):
            if not call['id']:
                call['id'] = 'call_%d' % i
        yield {'type': 'tool_calls', 'calls': [c for c in ordered if c['name']]}
    if usage:
        yield {'type': 'usage', 'prompt_tokens': usage.get('prompt_tokens'), 'completion_tokens': usage.get('completion_tokens'),
               'total_tokens': usage.get('total_tokens')}
    yield {'type': 'finish', 'reason': finish or ('stop' if complete else 'interrupted'), 'complete': complete or finish is not None}


def complete_once(cfg, messages, timeout=(10, 120), extra=None, max_tokens=None):
    """Non-streaming helper for summaries/titles. Returns the text content."""
    key, url, model = cfg
    payload = {'model': model, 'messages': messages, 'stream': False}
    if max_tokens:
        payload['max_tokens'] = max_tokens
    if extra:
        payload.update(extra)
    res = requests.post(url, headers=_headers(key), json=payload, timeout=timeout)
    res.raise_for_status()
    data = res.json()
    try:
        content = data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        raise ProviderError('Provider returned no content.')
    if isinstance(content, list):
        content = ''.join(p.get('text', '') for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ''


def mock_stream(cfg, messages, tools=None, stop=None, **_):
    """Deterministic provider stand-in (``--mock`` / no key): echoes the message, or runs a tool when the
    user writes ``!tool name {json}`` so the UI, CLI and tests can exercise the whole loop offline."""
    last = next((m for m in reversed(messages) if m.get('role') == 'user'), {})
    text = last.get('content') or ''
    if isinstance(text, list):
        text = ' '.join(part.get('text', '') for part in text if isinstance(part, dict))
    after_tool = bool(messages) and messages[-1].get('role') == 'tool'
    if text.startswith('!tool ') and not after_tool:
        name, _, raw = text[6:].strip().partition(' ')
        yield {'type': 'tool_calls', 'calls': [{'id': 'mock1', 'name': name, 'arguments': raw or '{}'}]}
        yield {'type': 'finish', 'reason': 'tool_calls'}
        return
    if after_tool:
        yield {'type': 'text', 'delta': '(mock) tool said: ' + str(messages[-1].get('content'))[:400]}
    else:
        for piece in ('(mock) got your message: ', text):
            yield {'type': 'text', 'delta': piece}
    yield {'type': 'usage', 'prompt_tokens': 10, 'completion_tokens': 5}
    yield {'type': 'finish', 'reason': 'stop'}
