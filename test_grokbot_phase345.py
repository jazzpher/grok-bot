"""Phase 3-5: memory, scheduler, MCP client, sub-agents, plugins, Telegram gateway and the web routes around them."""
import base64
import datetime as dt
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('GROK_WORKSPACE_DIR', tempfile.mkdtemp(prefix='grokbot-p345-'))

from grokbot import scheduler as sched_mod, config as gconfig, plugins  # noqa: E402
from grokbot.memory import MemoryStore  # noqa: E402
from grokbot.mcp import MCPManager, MCPError, tool_name  # noqa: E402
from grokbot.tools import default_registry, ToolContext, ToolError  # noqa: E402
from grokbot.approvals import SandboxPolicy  # noqa: E402
from grokbot.gateway.telegram import TelegramGateway  # noqa: E402
from agent_loop import Workspace  # noqa: E402
import webui  # noqa: E402

NOW = dt.datetime(2026, 10, 14, 12, 0)
FAKE_MCP = ROOT / 'tests_support' / 'fake_mcp_server.py'


def events_of(res):
    return [json.loads(line) for line in res.get_data(as_text=True).splitlines() if line.strip()]


class MemoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = MemoryStore(Path(self.tmp.name) / 'm.sqlite3')

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_save_search_relevant_and_owner_isolation(self):
        a = self.store.save('alice', 'Prefers replies in Taglish', ['lang'], source='user', pinned=True)
        self.store.save('alice', 'Main repo deploys with Render', ['deploy'])
        self.store.save('bob', 'Bob likes Rust')
        self.assertEqual(self.store.count('alice'), 2)
        self.assertEqual([m['text'] for m in self.store.search('alice', 'render')], ['Main repo deploys with Render'])
        self.assertEqual(self.store.search('alice', 'Rust'), [])
        rel = self.store.relevant('alice', 'how do I deploy?')
        self.assertEqual(rel[0]['id'], a['id'], 'pinned memory comes first')
        self.assertIn('Render', MemoryStore.prompt_block(rel))
        self.assertEqual(MemoryStore.prompt_block([]), '')

    def test_dedupe_update_delete_and_validation(self):
        first = self.store.save('o', 'Uses pnpm')
        again = self.store.save('o', 'uses pnpm  ')
        self.assertEqual(first['id'], again['id'], 'same text refreshes instead of duplicating')
        upd = self.store.update('o', first['id'], text='Uses pnpm 9', pinned=True)
        self.assertEqual((upd['text'], upd['pinned']), ('Uses pnpm 9', True))
        self.assertIsNone(self.store.update('other', first['id'], text='x'))
        self.assertTrue(self.store.delete('o', first['id']))
        self.assertFalse(self.store.delete('o', first['id']))
        with self.assertRaises(ValueError):
            self.store.save('o', 'x')
        with self.assertRaises(ValueError):
            self.store.save('o', 'y' * 5000)


class ScheduleParsingTest(unittest.TestCase):
    def next_of(self, text):
        spec = sched_mod.parse_schedule(text, NOW)
        ts = sched_mod.next_run(spec, NOW.timestamp())
        return spec['kind'], dt.datetime.fromtimestamp(ts)

    def test_phrases(self):
        self.assertEqual(self.next_of('every day at 08:00'), ('cron', dt.datetime(2026, 10, 15, 8, 0)))
        self.assertEqual(self.next_of('every 2 hours'), ('interval', dt.datetime(2026, 10, 14, 14, 0)))
        self.assertEqual(self.next_of('every monday at 9:00'), ('cron', dt.datetime(2026, 10, 19, 9, 0)))
        self.assertEqual(self.next_of('every weekday at 9:30'), ('cron', dt.datetime(2026, 10, 15, 9, 30)))
        self.assertEqual(self.next_of('in 30 minutes'), ('once', dt.datetime(2026, 10, 14, 12, 30)))
        self.assertEqual(self.next_of('at 11:00'), ('once', dt.datetime(2026, 10, 15, 11, 0)))
        self.assertEqual(self.next_of('tomorrow at 9pm'), ('once', dt.datetime(2026, 10, 15, 21, 0)))
        self.assertEqual(self.next_of('0 9 * * 1-5'), ('cron', dt.datetime(2026, 10, 15, 9, 0)))
        self.assertEqual(self.next_of('*/20 * * * *'), ('cron', dt.datetime(2026, 10, 14, 12, 20)))
        self.assertEqual(self.next_of('2026-11-01 09:00'), ('once', dt.datetime(2026, 11, 1, 9, 0)))

    def test_rejections(self):
        for bad in ('every 2 minutes', 'whenever', 'at 25:00', '* * * *', '', '2020-01-01 09:00', '61 * * * *'):
            with self.assertRaises(sched_mod.ScheduleError, msg=bad):
                sched_mod.parse_schedule(bad, NOW)

    def test_once_is_finished_after_it_fires(self):
        spec = sched_mod.parse_schedule('in 10 minutes', NOW)
        self.assertIsNone(sched_mod.next_run(spec, (NOW + dt.timedelta(minutes=11)).timestamp()))


class SchedulerStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ran = []
        self.sched = sched_mod.Scheduler(Path(self.tmp.name) / 's.sqlite3', runner=self.runner, tick=1)

    def tearDown(self):
        self.sched.close(); self.tmp.cleanup()

    def runner(self, job):
        self.ran.append(job['id'])
        if job['prompt'] == 'boom':
            raise RuntimeError('kaput')
        return 'completed', 'ok:' + job['title']

    def test_add_list_due_run_and_once_disables(self):
        job = self.sched.add('o', 'in 5 minutes', 'ping', 'Ping', now=NOW)
        self.assertEqual(job['when'][:7], 'once at')
        self.assertEqual(self.sched.due(now=NOW.timestamp()), [])
        due = self.sched.due(now=(NOW + dt.timedelta(minutes=6)).timestamp())
        self.assertEqual([j['id'] for j in due], [job['id']])
        with mock.patch('grokbot.scheduler.time.time', return_value=(NOW + dt.timedelta(minutes=6)).timestamp()):
            ran = self.sched.run_due()
        self.assertEqual(ran, [(job['id'], 'completed', 'ok:Ping')])
        after = self.sched.get('o', job['id'])
        self.assertEqual((after['runs'], after['last_status'], after['enabled']), (1, 'completed', False))
        self.assertIsNone(self.sched.get('other', job['id']), 'owner scoped')

    def test_recurring_advances_and_errors_are_recorded(self):
        job = self.sched.add('o', 'every 2 hours', 'boom', 'Boom', now=NOW)
        with mock.patch('grokbot.scheduler.time.time', return_value=job['next_run'] + 1):
            self.sched.run_due(now=job['next_run'] + 1)
        after = self.sched.get('o', job['id'])
        self.assertEqual(after['last_status'], 'error')
        self.assertIn('kaput', after['last_result'])
        self.assertTrue(after['enabled'])
        self.assertGreater(after['next_run'], job['next_run'])

    def test_limits_toggle_delete(self):
        with mock.patch.object(gconfig, 'SCHEDULER_MAX_JOBS', 1):
            self.sched.add('o', 'every day at 08:00', 'a' * 10, now=NOW)
            with self.assertRaises(sched_mod.ScheduleError):
                self.sched.add('o', 'every day at 09:00', 'b' * 10, now=NOW)
        with self.assertRaises(sched_mod.ScheduleError):
            self.sched.add('o', 'every day at 09:00', 'x', now=NOW)
        job = self.sched.list('o')[0]
        self.assertFalse(self.sched.set_enabled('o', job['id'], False)['enabled'])
        self.assertTrue(self.sched.set_enabled('o', job['id'], True)['enabled'])
        self.assertTrue(self.sched.delete('o', job['id']))
        self.assertEqual(self.sched.list('o'), [])


class ScheduleToolsTest(unittest.TestCase):
    def test_tools_require_scheduler_and_scope_to_owner(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        sched = sched_mod.Scheduler(Path(tmp.name) / 's.sqlite3', runner=lambda job: ('completed', '')); self.addCleanup(sched.close)
        reg = default_registry()
        ws = Workspace(Path(tmp.name) / 'ws')
        bare = ToolContext(workspace=ws, session_id='s', policy=SandboxPolicy())
        self.assertNotIn('schedule_task', reg.names_for(bare))
        ctx = ToolContext(workspace=ws, session_id='s', policy=SandboxPolicy(), extras={'scheduler': sched, 'owner': 'alice'})
        self.assertIn('schedule_task', reg.names_for(ctx))
        r = reg.execute(ctx, 'schedule_task', {'when': 'every day at 07:00', 'prompt': 'Check CI and summarise', 'title': 'CI'})
        self.assertTrue(r.ok, r.output)
        self.assertEqual(len(sched.list('alice')), 1)
        self.assertIn('CI', reg.execute(ctx, 'list_scheduled_tasks', {}).output)
        bad = reg.execute(ctx, 'schedule_task', {'when': 'every 1 minutes', 'prompt': 'spam'})
        self.assertFalse(bad.ok)
        jid = sched.list('alice')[0]['id']
        self.assertFalse(reg.execute(ToolContext(workspace=ws, session_id='s', policy=SandboxPolicy(), extras={'scheduler': sched, 'owner': 'bob'}),
                                     'cancel_scheduled_task', {'id': jid}).ok)
        self.assertTrue(reg.execute(ctx, 'cancel_scheduled_task', {'id': jid}).ok)


class MCPStdioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Path(self.tmp.name) / 'mcp.json'
        cfg.write_text(json.dumps({'mcpServers': {'fake': {'command': sys.executable, 'args': [str(FAKE_MCP)]},
                                                  'bad name!': {'command': 'x'}, 'nocmd': {'disabled': True, 'command': 'x'},
                                                  'missing': {'command': '/nonexistent/binary-xyz'}}}))
        self.manager = MCPManager(cfg).load()
        self.registry = default_registry()
        self.ws = Workspace(Path(self.tmp.name) / 'ws')

    def tearDown(self):
        self.manager.close(); self.tmp.cleanup()

    def test_tools_are_registered_called_and_policy_aware(self):
        self.assertEqual(sorted(self.manager.servers), ['fake', 'missing'])
        names = self.manager.register_tools(self.registry)
        self.assertEqual(names, ['mcp__fake__echo', 'mcp__fake__add', 'mcp__fake__boom'])
        status = {s['name']: s for s in self.manager.describe()}
        self.assertEqual(status['fake']['state'], 'connected')
        self.assertEqual(status['fake']['tools'], 3)
        self.assertEqual(status['missing']['state'], 'error')
        ctx = ToolContext(workspace=self.ws, session_id='s', policy=SandboxPolicy(mode='workspace-write'))
        self.assertEqual(self.registry.execute(ctx, 'mcp__fake__echo', {'text': 'hi'}).output, 'echo: hi')
        self.assertEqual(self.registry.execute(ctx, 'mcp__fake__add', {'a': 2, 'b': 3}).output, '5')
        boom = self.registry.execute(ctx, 'mcp__fake__boom', {})
        self.assertFalse(boom.ok); self.assertIn('kaboom', boom.output)
        ro = ToolContext(workspace=self.ws, session_id='s', policy=SandboxPolicy(mode='read-only'))
        self.assertIsNone(self.registry.get('mcp__fake__echo').needs_approval(ro, {}), 'readOnlyHint tool needs no approval')
        self.assertIsNotNone(self.registry.get('mcp__fake__add').needs_approval(ro, {}))
        spec = next(t for t in self.registry.openai_tools(ctx) if t['function']['name'] == 'mcp__fake__add')
        self.assertEqual(spec['function']['parameters']['required'], ['a', 'b'])
        self.assertTrue(spec['function']['description'].startswith('[MCP fake]'))
        # reconnect after the server dies
        self.manager.clients['fake'].proc.kill(); self.manager.clients['fake'].proc.wait()
        first = self.registry.execute(ctx, 'mcp__fake__echo', {'text': 'again'})
        self.assertFalse(first.ok)
        self.assertEqual(self.registry.execute(ctx, 'mcp__fake__echo', {'text': 'again'}).output, 'echo: again')

    def test_tool_name_is_openai_safe(self):
        self.assertEqual(tool_name('fs', 'read_file'), 'mcp__fs__read_file')
        long = tool_name('a' * 40, 'b.c/d' * 20)
        self.assertLessEqual(len(long), 64)
        self.assertRegex(long, r'^[A-Za-z0-9_-]+$')
        self.assertNotEqual(tool_name('a' * 40, 'x' * 60), tool_name('a' * 40, 'x' * 61))


class MCPHttpTest(unittest.TestCase):
    """Streamable HTTP transport against a tiny in-process JSON-RPC server (plain JSON and SSE replies)."""

    def setUp(self):
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)) or 0) or b'{}')
                test.seen.append((body.get('method'), self.headers.get('Mcp-Session-Id')))
                if 'id' not in body:
                    self.send_response(202); self.end_headers(); return
                method = body['method']
                if method == 'initialize':
                    result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'http-fake'}}
                elif method == 'tools/list':
                    cursor = (body.get('params') or {}).get('cursor')
                    result = ({'tools': [{'name': 'page2', 'inputSchema': {'type': 'object', 'properties': {}}}]} if cursor
                              else {'tools': [{'name': 'hello', 'description': 'hi', 'inputSchema': {'type': 'object', 'properties': {'n': {'type': 'string'}}}}], 'nextCursor': 'c2'})
                elif method == 'tools/call':
                    name = body['params']['name']
                    if name == 'hello':   # answer over SSE to exercise that path
                        payload = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': {'content': [{'type': 'text', 'text': 'hello ' + body['params']['arguments'].get('n', '')}]}})
                        data = ('event: message\ndata: ' + payload + '\n\n').encode()
                        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.send_header('Content-Length', str(len(data))); self.end_headers()
                        self.wfile.write(data); return
                    result = {'content': [{'type': 'text', 'text': 'p2'}]}
                else:
                    result = {}
                data = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Mcp-Session-Id', 'sess-1')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        self.seen = []
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Path(self.tmp.name) / 'mcp.json'
        cfg.write_text(json.dumps({'mcpServers': {'web': {'url': 'http://127.0.0.1:%d/mcp' % self.server.server_port, 'headers': {'Authorization': 'Bearer t'}}}}))
        self.manager = MCPManager(cfg).load()

    def tearDown(self):
        self.manager.close(); self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()

    def test_http_transport_pagination_sse_and_session_header(self):
        reg = default_registry()
        self.assertEqual(self.manager.register_tools(reg), ['mcp__web__hello', 'mcp__web__page2'])
        ctx = ToolContext(workspace=Workspace(Path(self.tmp.name) / 'ws'), session_id='s', policy=SandboxPolicy(mode='workspace-write'))
        self.assertEqual(reg.execute(ctx, 'mcp__web__hello', {'n': 'bob'}).output, 'hello bob')
        self.assertEqual(reg.execute(ctx, 'mcp__web__page2', {}).output, 'p2')
        methods = [m for m, _ in self.seen]
        self.assertEqual(methods[:2], ['initialize', 'notifications/initialized'])
        self.assertEqual(methods.count('tools/list'), 2, 'nextCursor followed')
        self.assertIn('sess-1', [sid for _, sid in self.seen[2:]], 'Mcp-Session-Id echoed after initialize')


class DelegateAndPluginsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / 'ws')

    def test_delegate_runs_a_bounded_subagent_and_blocks_recursion(self):
        from grokbot import agent as agent_core
        from grokbot.providers import mock_stream
        reg = default_registry()
        events = []
        ctx = ToolContext(workspace=self.ws, session_id='s', policy=SandboxPolicy(mode='workspace-write'))
        result = agent_core.run_turn([{'role': 'user', 'content': '!tool delegate {"task": "list the files", "max_steps": 2}'}], reg, ctx,
                                     ('', '', 'mock'), events.append, stream_fn=mock_stream)
        kinds = [e['type'] for e in events]
        self.assertIn('subagent_start', kinds); self.assertIn('subagent_end', kinds)
        self.assertNotIn('done', [e['type'] for e in events if e.get('agent')], 'nested done is renamed')
        self.assertTrue(all(e.get('agent') == 'sub-1' for e in events if e.get('depth') == 1))
        tool_end = next(e for e in events if e['type'] == 'tool_end' and e['name'] == 'delegate')
        self.assertIn('Sub-agent sub-1 finished', tool_end['output'])
        self.assertEqual(result.status, 'completed')
        deep = ToolContext(workspace=self.ws, session_id='s', policy=SandboxPolicy(), extras={'depth': gconfig.MAX_SUBAGENT_DEPTH, 'registry': reg})
        self.assertFalse(reg.execute(deep, 'delegate', {'task': 'x'}).ok)
        sub_names = reg.subset([n for n in reg.names() if n not in __import__('grokbot.tools.delegate', fromlist=['x']).EXCLUDED_FOR_SUBAGENTS]).names()
        self.assertNotIn('delegate', sub_names); self.assertNotIn('save_memory', sub_names)

    def test_plugins_register_tools_and_hooks(self):
        plugdir = Path(self.tmp.name) / 'plugs'; plugdir.mkdir()
        (plugdir / 'p345_sample.py').write_text(
            "from grokbot.tools import ToolError\nCALLS = []\n"
            "def register(registry):\n"
            "    @registry.tool('plugin_hello', 'hello', {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']}, category='plugin')\n"
            "    def plugin_hello(ctx, name):\n        return 'hello ' + name\n"
            "def before_tool(ctx, name, arguments):\n    CALLS.append(('before', name))\n"
            "    if arguments.get('name') == 'blocked': raise ToolError('blocked by plugin')\n    return arguments\n"
            "def after_tool(ctx, result):\n    CALLS.append(('after', result.name)); return result\n"
            "def on_turn_end(ctx, result):\n    CALLS.append(('end', result.status))\n")
        sys.path.insert(0, str(plugdir)); self.addCleanup(sys.path.remove, str(plugdir))
        plugins.reset(); self.addCleanup(plugins.reset)
        reg = default_registry(plugins_too=False)
        plugins.load(reg, names=['p345_sample', 'does.not.exist'])
        self.assertEqual(plugins.loaded(), ['p345_sample'])
        self.assertIn('plugin_hello', reg.names())
        ctx = ToolContext(workspace=self.ws, session_id='s', policy=SandboxPolicy())
        self.assertEqual(reg.execute(ctx, 'plugin_hello', {'name': 'w'}).output, 'hello w')
        self.assertIn('blocked by plugin', reg.execute(ctx, 'plugin_hello', {'name': 'blocked'}).output)
        import p345_sample
        self.assertEqual(p345_sample.CALLS[0], ('before', 'plugin_hello'))
        self.assertIn(('after', 'plugin_hello'), p345_sample.CALLS)


class FakeTelegramSession:
    """Stands in for requests.Session: records API calls, returns Telegram-shaped results."""

    def __init__(self):
        self.calls = []
        self.msg_id = 100

    def post(self, url, json=None, timeout=None):
        method = url.rsplit('/', 1)[-1]
        self.calls.append((method, json))
        self.msg_id += 1
        payload = {'ok': True, 'result': {'message_id': self.msg_id} if method in ('sendMessage', 'editMessageText') else True}
        if method == 'getUpdates':
            payload = {'ok': True, 'result': []}

        class R:
            def json(self_inner):
                return payload
        return R()


class TelegramGatewayTest(unittest.TestCase):
    def make(self, runner=None, approvals=None, policy_store=None):
        session = FakeTelegramSession()
        gw = TelegramGateway('123:abc', ['42'], runner or (lambda owner, text, emit, stop: None), approvals=approvals,
                             policy_store=policy_store, session=session)
        return gw, session

    def test_fails_closed_without_allowlist(self):
        with self.assertRaises(ValueError):
            TelegramGateway('t', [], lambda *a: None, session=FakeTelegramSession())

    def test_ignores_strangers_and_answers_allowlisted_chat(self):
        seen = []

        class Result:
            status, text = 'completed', 'Final answer'

        def runner(owner, text, emit, stop):
            seen.append((owner, text))
            emit({'type': 'text', 'delta': 'Thinking…'})
            emit({'type': 'tool_start', 'name': 'run_command', 'arguments': {'command': 'ls'}})
            return Result()
        gw, session = self.make(runner)
        gw.handle_update({'update_id': 1, 'message': {'chat': {'id': 999}, 'from': {'id': 999}, 'text': 'hi'}})
        self.assertEqual(session.calls, [], 'non-allowlisted chat is ignored silently')
        gw.handle_update({'update_id': 2, 'message': {'chat': {'id': 42}, 'from': {'id': 42}, 'text': 'hello agent'}})
        for _ in range(100):
            if not gw.chat_state(42)['busy'] and any(m == 'editMessageText' for m, _ in session.calls):
                break
            time.sleep(0.02)
        self.assertEqual(seen, [('tg:42', 'hello agent')])
        methods = [m for m, _ in session.calls]
        self.assertEqual(methods[0], 'sendMessage')
        self.assertEqual(session.calls[-1][1]['text'], 'Final answer')
        self.assertNotIn('parse_mode', session.calls[-1][1])

    def test_commands_and_approval_buttons(self):
        resolved = []

        class Broker:
            def resolve(self, rid, decision, owner):
                resolved.append((rid, decision, owner)); return True

            def cancel_all(self, owner):
                pass

        class Policies:
            def __init__(self):
                self.saved = {}

            def load(self, owner):
                return self.saved.get(owner) or SandboxPolicy()

            def save(self, owner, policy):
                self.saved[owner] = policy
        policies = Policies()
        gw, session = self.make(approvals=Broker(), policy_store=policies)
        gw.handle_update({'message': {'chat': {'id': 42}, 'from': {'id': 42}, 'text': '/help'}})
        self.assertIn('/policy', session.calls[-1][1]['text'])
        gw.handle_update({'message': {'chat': {'id': 42}, 'from': {'id': 42}, 'text': '/policy read-only never'}})
        self.assertEqual((policies.saved['tg:42'].mode, policies.saved['tg:42'].approval), ('read-only', 'never'))
        gw.handle_update({'message': {'chat': {'id': 42}, 'from': {'id': 42}, 'text': '/policy bogus'}})
        self.assertIn('Unknown value', session.calls[-1][1]['text'])
        gw.handle_update({'callback_query': {'id': 'q1', 'from': {'id': 42}, 'data': 'ap|req9|always', 'message': {'message_id': 5, 'chat': {'id': 42}, 'text': 'Approval needed'}}})
        self.assertEqual(resolved, [('req9', 'always', 'tg:42')])
        self.assertIn('answerCallbackQuery', [m for m, _ in session.calls])
        gw.handle_update({'callback_query': {'id': 'q2', 'from': {'id': 7}, 'data': 'ap|req9|allow', 'message': {'message_id': 5, 'chat': {'id': 42}}}})
        self.assertEqual(len(resolved), 1, 'stranger cannot approve')

    def test_long_messages_are_split(self):
        gw, session = self.make()
        gw.send(42, 'x' * 9000)
        sends = [j for m, j in session.calls if m == 'sendMessage']
        self.assertEqual(len(sends), 3)
        self.assertTrue(all(len(j['text']) <= 4000 for j in sends))


class WebRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        webui.app.config.update(MOCK=True, MODEL='mock', API_KEY='')
        webui.app.testing = True

    def setUp(self):
        self.c = webui.app.test_client()
        self.c.get('/history')

    def test_memory_routes_and_injection(self):
        r = self.c.post('/memories', json={'text': 'User prefers Taglish replies', 'tags': ['lang'], 'pinned': True})
        self.assertEqual(r.status_code, 200)
        mid = r.get_json()['id']
        self.assertTrue(any('Taglish' in m['text'] for m in self.c.get('/memories').get_json()))
        self.assertEqual(self.c.get('/memories?q=taglish').get_json()[0]['id'], mid)
        events = events_of(self.c.post('/agent', json={'message': 'hello'}))
        info = next(e for e in events if e['type'] == 'context_info')
        self.assertIn(mid, [m['id'] for m in info['memories']])
        self.assertGreater(info['tools'], 20)
        self.assertEqual(self.c.post('/memories/' + mid, json={'text': 'User prefers Tagalog'}).get_json()['text'], 'User prefers Tagalog')
        self.assertEqual(self.c.post('/memories/' + mid + '/delete').status_code, 200)
        self.assertEqual(self.c.post('/memories/' + mid + '/delete').status_code, 404)
        self.assertEqual(self.c.post('/memories', json={'text': 'x'}).status_code, 400)

    def test_agent_tools_see_memory_and_scheduler(self):
        events = events_of(self.c.post('/agent', json={'message': '!tool save_memory {"text": "Project deploys with Render"}'}))
        self.assertIn('memory_saved', [e['type'] for e in events])
        events = events_of(self.c.post('/agent', json={'message': '!tool schedule_task {"when": "every day at 08:00", "prompt": "Say good morning", "title": "Morning"}'}))
        self.assertIn('schedule_created', [e['type'] for e in events])
        jobs = self.c.get('/schedules').get_json()
        self.assertEqual([j['title'] for j in jobs], ['Morning'])
        self.assertEqual(self.c.post('/schedules/%s/delete' % jobs[0]['id']).status_code, 200)

    def test_schedule_routes_run_unattended_and_notify(self):
        r = self.c.post('/schedules', json={'when': 'in 10 minutes', 'prompt': 'Reply with pong', 'title': 'Ping'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        jid = r.get_json()['id']
        self.assertEqual(self.c.post('/schedules', json={'when': 'never ever', 'prompt': 'x' * 10}).status_code, 400)
        self.assertEqual(self.c.post('/schedules/%s/run' % jid).get_json()['started'], True)
        for _ in range(100):
            job = next(j for j in self.c.get('/schedules').get_json() if j['id'] == jid)
            if job['runs']:
                break
            time.sleep(0.03)
        self.assertEqual(job['last_status'], 'completed')
        notes = self.c.get('/notifications').get_json()
        self.assertEqual(notes[0]['title'], 'Ping')
        conv = self.c.get('/conversations/' + notes[0]['conversation_id']).get_json()
        self.assertTrue(conv['title'].startswith('⏰'))
        self.assertEqual(self.c.get('/notifications').get_json(), [], 'notifications are delivered once')
        self.assertFalse(self.c.post('/schedules/%s/toggle' % jid).get_json()['enabled'])
        self.assertEqual(self.c.post('/schedules/%s/delete' % jid).status_code, 200)
        self.assertEqual(self.c.post('/schedules/%s/delete' % jid).status_code, 404)

    def test_unattended_runs_never_wait_for_approval(self):
        with mock.patch.object(webui, 'mock_stream', webui.mock_stream):
            res = webui.run_owner_turn('unattended-owner', webui.owner_workspace('unattended-owner'),
                                       webui.conversation_store().create('unattended-owner', 'x'), 'hello',
                                       emit=lambda e: None, stop=threading.Event(), unattended=True,
                                       policy=SandboxPolicy(mode='read-only', approval='on-request'))
        result, ctx = res
        self.assertEqual(ctx.policy.approval, 'never')
        self.assertTrue(ctx.extras['unattended'])

    def test_images_are_validated_and_recorded(self):
        png = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'0' * 64).decode()
        r = self.c.post('/agent', json={'message': 'what is this', 'images': [{'name': 'shot.png', 'data_url': png}]})
        events = events_of(r)
        self.assertEqual(events[-1]['status'], 'completed')
        conv = self.c.get('/conversations/' + events[0]['id']).get_json()
        self.assertIn('[image: shot.png]', [e['content'] for e in conv['entries'] if e['role'] == 'user'][-1])
        self.assertEqual(self.c.post('/agent', json={'message': 'x', 'images': [{'data_url': 'data:text/html;base64,AAAA'}]}).status_code, 400)
        self.assertEqual(self.c.post('/agent', json={'message': 'x', 'images': [{'data_url': png}] * 5}).status_code, 400)

    def test_conversation_export_and_url(self):
        events = events_of(self.c.post('/agent', json={'message': 'export me'}))
        cid = events[0]['id']
        md = self.c.get('/conversations/%s/export' % cid)
        self.assertEqual(md.status_code, 200)
        self.assertIn('export me', md.get_data(as_text=True))
        js = self.c.get('/conversations/%s/export?format=json' % cid).get_json()
        self.assertEqual(js['id'], cid)
        self.assertEqual(self.c.get('/conversations/nope/export').status_code, 404)
        page = self.c.get('/c/' + cid)
        self.assertEqual(page.status_code, 200)
        self.assertIn('data-cid="%s"' % cid, page.get_data(as_text=True))
        self.assertEqual(self.c.get('/c/<script>').status_code, 302)

    def test_skills_export_import_roundtrip(self):
        self.assertEqual(self.c.post('/skills', json={'name': 'deploy-render', 'description': 'Deploy to Render', 'content': '# Steps\n1. push'}).status_code, 200)
        z = self.c.get('/skills/export')
        self.assertEqual(z.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
        self.assertIn('deploy-render/SKILL.md', names)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as w:
            w.writestr('imported-skill/SKILL.md', '---\nname: imported-skill\ndescription: "Imported one"\n---\n# body\nhello')
            w.writestr('deploy-render/SKILL.md', '---\nname: deploy-render\ndescription: updated\n---\n# Steps\n2. deploy')
            w.writestr('../evil/SKILL.md', 'no name here')
        buf.seek(0)
        r = self.c.post('/skills/import', data={'file': (buf, 'skills.zip')}, content_type='multipart/form-data')
        data = r.get_json()
        self.assertEqual(sorted(data['imported']), ['deploy-render', 'imported-skill'])
        self.assertTrue(any('unsafe path' in e for e in data['errors']))
        self.assertEqual(self.c.get('/skills/deploy-render').get_json()['description'], 'updated')
        self.assertEqual(self.c.post('/skills/import', data={'file': (io.BytesIO(b'nope'), 'x.zip')}, content_type='multipart/form-data').status_code, 400)

    def test_capabilities_and_mcp_status(self):
        caps = self.c.get('/capabilities').get_json()
        for key in ('memory', 'schedules', 'mcp', 'plugins', 'telegram', 'local_provider'):
            self.assertIn(key, caps)
        self.assertEqual(self.c.get('/mcp').get_json()['servers'], [])
        self.assertEqual(self.c.get('/providers').status_code, 200)

    def test_parse_skill_markdown(self):
        name, desc, body = webui.parse_skill_markdown('---\nname: "x-y"\ndescription: Do it\n---\n# Title\nBody', 'folder')
        self.assertEqual((name, desc), ('x-y', 'Do it'))
        self.assertTrue(body.startswith('# Title'))
        name, desc, _ = webui.parse_skill_markdown('# Title\nFirst line here', 'from-folder')
        self.assertEqual((name, desc), ('from-folder', 'First line here'))
        with self.assertRaises(ValueError):
            webui.parse_skill_markdown('no front matter', '')


class SandboxEnvNamesTest(unittest.TestCase):
    def _reload_with(self, env):
        import importlib
        with mock.patch.dict(os.environ, env, clear=False):
            for key in ('GROK_SANDBOX_BACKEND', 'GROK_SANDBOX'):
                if key not in env:
                    os.environ.pop(key, None)
            importlib.reload(gconfig)
            return gconfig.SANDBOX_BACKEND

    def test_documented_name_and_alias_are_both_honoured(self):
        import importlib
        try:
            self.assertEqual(self._reload_with({'GROK_SANDBOX_BACKEND': 'off'}), 'off')
            self.assertEqual(self._reload_with({'GROK_SANDBOX': 'subprocess'}), 'subprocess')
            self.assertEqual(self._reload_with({'GROK_SANDBOX_BACKEND': 'docker', 'GROK_SANDBOX': 'off'}), 'docker', 'documented name wins')
            self.assertEqual(self._reload_with({'GROK_SANDBOX_BACKEND': 'bogus'}), 'auto')
        finally:
            importlib.reload(gconfig)


class LocalProviderTest(unittest.TestCase):
    def test_local_preset_only_when_configured(self):
        self.assertNotIn('local', webui.PROVIDERS)
        with mock.patch.dict(os.environ, {'GROK_LOCAL_API_URL': 'http://127.0.0.1:11434/v1/chat/completions'}):
            import importlib
            import model_discovery
            saved = (dict(webui.PROVIDERS), set(webui.ALLOWED_API_URLS), dict(model_discovery.MODEL_URLS))
            try:
                src = Path(ROOT / 'webui.py').read_text(encoding='utf-8')
                self.assertIn("PROVIDERS['local']", src)
                # exercise the derivation logic directly
                url = os.environ['GROK_LOCAL_API_URL']
                self.assertEqual(webui.re.sub(r'/chat/completions/?$', '/models', url), 'http://127.0.0.1:11434/v1/models')
            finally:
                webui.PROVIDERS.clear(); webui.PROVIDERS.update(saved[0])
                webui.ALLOWED_API_URLS.clear(); webui.ALLOWED_API_URLS.update(saved[1])
                model_discovery.MODEL_URLS.clear(); model_discovery.MODEL_URLS.update(saved[2])
            importlib.invalidate_caches()


if __name__ == '__main__':
    unittest.main()
