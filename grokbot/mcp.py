"""MCP (Model Context Protocol) client: stdio and Streamable-HTTP servers from an ``mcp.json`` file.

    {
      "mcpServers": {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"], "env": {}},
        "remote":     {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer ..."}}
      }
    }

Every tool a server exposes is registered as ``mcp__<server>__<tool>`` with the server's input schema, so the
model sees it like any built-in tool. Servers connect lazily (first use) and are reconnected after failures.
The config file is owner-controlled, so loopback URLs are allowed for HTTP servers; the usual tool-output caps,
approval policy (read-only mode asks unless the tool is annotated ``readOnlyHint``) and timeouts still apply.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import requests

from grokbot import __version__, config
from grokbot.tools import ToolError, ToolSpec

log = logging.getLogger('grokbot.mcp')
PROTOCOL_VERSION = '2025-06-18'
CLIENT_INFO = {'name': 'grok-bot', 'version': __version__}
_NAME_OK = re.compile(r'[^A-Za-z0-9_-]+')


class MCPError(Exception):
    pass


def tool_name(server, tool):
    """OpenAI-safe function name (<=64 chars, [A-Za-z0-9_-]) that stays unique per server/tool."""
    base = 'mcp__' + _NAME_OK.sub('_', server) + '__' + _NAME_OK.sub('_', tool)
    if len(base) <= 64:
        return base
    digest = hashlib.sha1((server + '/' + tool).encode()).hexdigest()[:8]
    return base[:55] + '_' + digest


class _JsonRpc:
    """Shared request bookkeeping for both transports."""

    def __init__(self, name):
        self.name = name
        self._ids = itertools.count(1)
        self.server_info = {}
        self.capabilities = {}

    def _frame(self, method, params=None, notification=False):
        msg = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            msg['params'] = params
        if not notification:
            msg['id'] = next(self._ids)
        return msg

    def initialize(self, timeout):
        result = self.request('initialize', {'protocolVersion': PROTOCOL_VERSION, 'capabilities': {}, 'clientInfo': CLIENT_INFO}, timeout)
        self.server_info = result.get('serverInfo') or {}
        self.capabilities = result.get('capabilities') or {}
        self.notify('notifications/initialized')
        return result

    def list_tools(self, timeout):
        tools, cursor = [], None
        for _ in range(20):
            params = {'cursor': cursor} if cursor else None
            result = self.request('tools/list', params, timeout)
            tools.extend(t for t in result.get('tools') or [] if isinstance(t, dict) and isinstance(t.get('name'), str))
            cursor = result.get('nextCursor')
            if not cursor:
                break
        return tools

    def call_tool(self, name, arguments, timeout):
        result = self.request('tools/call', {'name': name, 'arguments': arguments or {}}, timeout)
        parts = []
        for item in result.get('content') or []:
            if not isinstance(item, dict):
                continue
            kind = item.get('type')
            if kind == 'text':
                parts.append(str(item.get('text', '')))
            elif kind == 'image':
                parts.append('[image %s, %d base64 chars]' % (item.get('mimeType', '?'), len(item.get('data', '') or '')))
            elif kind == 'resource':
                res = item.get('resource') or {}
                parts.append(res.get('text') or ('[resource %s]' % res.get('uri', '?')))
            else:
                parts.append(json.dumps(item, ensure_ascii=False)[:2000])
        if 'structuredContent' in result and not parts:
            parts.append(json.dumps(result['structuredContent'], ensure_ascii=False))
        text = '\n'.join(parts) if parts else '(empty result)'
        if result.get('isError'):
            raise ToolError('MCP tool reported an error: ' + text[:2000])
        return text

    # transports implement these
    def request(self, method, params, timeout):
        raise NotImplementedError

    def notify(self, method, params=None):
        raise NotImplementedError

    def close(self):
        pass


class StdioClient(_JsonRpc):
    def __init__(self, name, command, args=(), env=None, cwd=None):
        super().__init__(name)
        full_env = {k: v for k, v in os.environ.items() if not re.search(r'(KEY|TOKEN|SECRET|PASSWORD)', k, re.I)}
        full_env.update({k: str(v) for k, v in (env or {}).items()})
        self.proc = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=full_env, cwd=cwd or None, text=True, encoding='utf-8', bufsize=1)
        self.pending = {}
        self.cond = threading.Condition()
        self.write_lock = threading.Lock()
        self.dead = None
        threading.Thread(target=self._reader, daemon=True, name='mcp-' + name).start()

    def _reader(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and 'id' in msg and ('result' in msg or 'error' in msg):
                    with self.cond:
                        self.pending[msg['id']] = msg
                        self.cond.notify_all()
                # requests from the server (sampling, roots) and notifications are ignored in this client
        finally:
            with self.cond:
                self.dead = 'MCP server %s exited' % self.name
                self.cond.notify_all()

    def _send(self, msg):
        if self.proc.poll() is not None:
            raise MCPError('MCP server %s is not running.' % self.name)
        with self.write_lock:
            try:
                self.proc.stdin.write(json.dumps(msg) + '\n')
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise MCPError('Could not write to MCP server %s: %s' % (self.name, exc)) from exc

    def request(self, method, params, timeout):
        msg = self._frame(method, params)
        self._send(msg)
        deadline = time.monotonic() + timeout
        with self.cond:
            while msg['id'] not in self.pending:
                if self.dead:
                    raise MCPError(self.dead)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError('MCP server %s did not answer %s within %ds.' % (self.name, method, timeout))
                self.cond.wait(min(remaining, 1.0))
            reply = self.pending.pop(msg['id'])
        if 'error' in reply:
            err = reply['error'] if isinstance(reply['error'], dict) else {'message': str(reply['error'])}
            raise MCPError('%s: %s' % (method, err.get('message', 'error')))
        return reply.get('result') or {}

    def notify(self, method, params=None):
        self._send(self._frame(method, params, notification=True))

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


class HttpClient(_JsonRpc):
    """Streamable HTTP transport (single endpoint, JSON or SSE responses, Mcp-Session-Id header)."""

    def __init__(self, name, url, headers=None):
        super().__init__(name)
        if not re.match(r'^https?://', url or ''):
            raise MCPError('MCP server %s: url must be http(s).' % name)
        self.url = url
        self.headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', **(headers or {})}
        self.session_id = None
        self.http = requests.Session()

    def _post(self, msg, timeout):
        headers = dict(self.headers)
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        try:
            res = self.http.post(self.url, json=msg, headers=headers, timeout=(10, timeout), stream=True)
        except requests.RequestException as exc:
            raise MCPError('MCP server %s unreachable: %s' % (self.name, type(exc).__name__)) from exc
        return res

    def request(self, method, params, timeout):
        msg = self._frame(method, params)
        res = self._post(msg, timeout)
        with res:
            if res.status_code >= 400:
                raise MCPError('MCP server %s answered HTTP %d for %s.' % (self.name, res.status_code, method))
            if res.headers.get('Mcp-Session-Id'):
                self.session_id = res.headers['Mcp-Session-Id']
            ctype = res.headers.get('Content-Type', '')
            body = bytearray()
            for chunk in res.iter_content(8192):
                body.extend(chunk)
                if len(body) > 8_000_000:
                    raise MCPError('MCP response too large.')
            text = body.decode('utf-8', 'replace')
        reply = None
        if 'text/event-stream' in ctype:
            for block in text.split('\n\n'):
                data = '\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith('data:'))
                if not data:
                    continue
                try:
                    candidate = json.loads(data)
                except ValueError:
                    continue
                if isinstance(candidate, dict) and candidate.get('id') == msg['id']:
                    reply = candidate
                    break
        else:
            try:
                candidate = json.loads(text) if text.strip() else None
            except ValueError as exc:
                raise MCPError('MCP server %s sent invalid JSON.' % self.name) from exc
            if isinstance(candidate, list):
                candidate = next((c for c in candidate if isinstance(c, dict) and c.get('id') == msg['id']), None)
            reply = candidate
        if not isinstance(reply, dict):
            raise MCPError('MCP server %s sent no response for %s.' % (self.name, method))
        if 'error' in reply:
            err = reply['error'] if isinstance(reply['error'], dict) else {'message': str(reply['error'])}
            raise MCPError('%s: %s' % (method, err.get('message', 'error')))
        return reply.get('result') or {}

    def notify(self, method, params=None):
        try:
            self._post(self._frame(method, params, notification=True), 10).close()
        except MCPError:
            pass

    def close(self):
        self.http.close()


class MCPManager:
    """Loads mcp.json, connects servers lazily and registers their tools into a ToolRegistry."""

    def __init__(self, config_path=None):
        self.config_path = Path(config_path) if config_path else None
        self.servers = {}          # name -> config dict
        self.clients = {}          # name -> client
        self.status = {}           # name -> {'state', 'tools', 'error'}
        self.lock = threading.RLock()
        self.registered = []       # tool names we added

    @staticmethod
    def default_config_path():
        if config.MCP_CONFIG:
            return Path(config.MCP_CONFIG)
        candidate = Path(__file__).resolve().parent.parent / 'mcp.json'
        return candidate if candidate.exists() else None

    def load(self):
        path = self.config_path or self.default_config_path()
        self.servers = {}
        if not path or not path.exists():
            return self
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            log.warning('mcp.json unreadable: %s', exc)
            return self
        servers = data.get('mcpServers') if isinstance(data, dict) else None
        for name, cfg in (servers or {}).items():
            if not isinstance(name, str) or not isinstance(cfg, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', name):
                continue
            if cfg.get('disabled'):
                continue
            if isinstance(cfg.get('command'), str) or isinstance(cfg.get('url'), str):
                self.servers[name] = cfg
                self.status[name] = {'state': 'configured', 'tools': 0, 'error': ''}
        return self

    def connect(self, name):
        with self.lock:
            client = self.clients.get(name)
            if client is not None:
                return client
            cfg = self.servers.get(name)
            if cfg is None:
                raise MCPError('Unknown MCP server ' + name)
            try:
                if cfg.get('command'):
                    client = StdioClient(name, cfg['command'], cfg.get('args') or [], cfg.get('env') or {}, cfg.get('cwd'))
                else:
                    client = HttpClient(name, cfg['url'], cfg.get('headers') or {})
                client.initialize(config.MCP_CONNECT_TIMEOUT)
            except (MCPError, OSError) as exc:
                self.status[name] = {'state': 'error', 'tools': 0, 'error': str(exc)[:300]}
                raise MCPError(str(exc)) from exc
            self.clients[name] = client
            self.status[name] = {'state': 'connected', 'tools': self.status.get(name, {}).get('tools', 0), 'error': ''}
            return client

    def disconnect(self, name):
        with self.lock:
            client = self.clients.pop(name, None)
            if client is not None:
                client.close()
            if name in self.status:
                self.status[name]['state'] = 'configured'

    def close(self):
        for name in list(self.clients):
            self.disconnect(name)

    def register_tools(self, registry):
        """Connect every configured server, list its tools and add them to ``registry``. Failures are recorded, not raised."""
        for tname in self.registered:
            registry.unregister(tname)
        self.registered = []
        for name in list(self.servers):
            try:
                client = self.connect(name)
                tools = client.list_tools(config.MCP_CONNECT_TIMEOUT)
            except MCPError as exc:
                self.status[name] = {'state': 'error', 'tools': 0, 'error': str(exc)[:300]}
                continue
            for tool in tools:
                spec = self._spec(name, tool)
                if spec.name in registry:
                    continue
                registry.register(spec)
                self.registered.append(spec.name)
            self.status[name]['tools'] = len([t for t in self.registered if t.startswith('mcp__' + _NAME_OK.sub('_', name) + '__')])
        return self.registered

    def _spec(self, server, tool):
        schema = tool.get('inputSchema') if isinstance(tool.get('inputSchema'), dict) else {}
        if schema.get('type') != 'object':
            schema = {'type': 'object', 'properties': schema.get('properties') or {}, 'required': schema.get('required') or []}
        schema.setdefault('properties', {})
        annotations = tool.get('annotations') if isinstance(tool.get('annotations'), dict) else {}
        read_only = bool(annotations.get('readOnlyHint'))
        # MCP spec: destructiveHint defaults to true for tools that are not read-only, so an unannotated tool is
        # treated as destructive (asks first) unless the server explicitly says otherwise.
        destructive = not read_only and bool(annotations.get('destructiveHint', True))
        remote_name = tool['name']
        description = '[MCP %s] %s' % (server, (tool.get('description') or remote_name).strip()[:800])

        def needs_approval(ctx, arguments):
            policy = ctx.policy
            if destructive and policy is not None and policy.mode != 'full-access':
                return 'MCP tool %s/%s is marked destructive.' % (server, remote_name)
            if not read_only and policy is not None and policy.mode == 'read-only':
                return 'Sandbox is read-only; MCP tool %s/%s may change external state.' % (server, remote_name)
            return None

        def handler(ctx, **arguments):
            try:
                client = self.connect(server)
                return client.call_tool(remote_name, arguments, config.MCP_CALL_TIMEOUT)
            except MCPError as exc:
                self.disconnect(server)   # reconnect on next use
                raise ToolError(str(exc)) from exc
        return ToolSpec(tool_name(server, remote_name), description, schema, handler, category='mcp:' + server,
                        mutates=not read_only, needs_approval=needs_approval)

    def describe(self):
        return [{'name': name, 'transport': 'stdio' if cfg.get('command') else 'http', **self.status.get(name, {})}
                for name, cfg in self.servers.items()]
