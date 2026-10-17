"""Offline tests for grokbot.agent (native + JSON tool protocols), conversations store and the /agent HTTP surface."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop import Workspace
from grokbot import agent as agent_core, providers
from grokbot.approvals import ApprovalBroker, SandboxPolicy
from grokbot.conversations import ConversationStore
from grokbot.tools import ToolContext, default_registry


def scripted(script):
    """Build a fake stream_fn from a list of per-call event lists."""
    calls = []

    def stream_fn(cfg, messages, tools=None, stop=None, **kwargs):
        calls.append({'messages': [dict(m) for m in messages], 'tools': tools, 'kwargs': kwargs})
        step = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(step, Exception):
            raise step
        for event in step:
            yield event
    stream_fn.calls = calls
    return stream_fn


def text_events(text):
    return [{'type': 'text', 'delta': text}, {'type': 'usage', 'prompt_tokens': 50, 'completion_tokens': 5}, {'type': 'finish', 'reason': 'stop'}]


def tool_call_events(name, args, call_id='c1'):
    return [{'type': 'tool_calls', 'calls': [{'id': call_id, 'name': name, 'arguments': json.dumps(args)}]},
            {'type': 'usage', 'prompt_tokens': 60, 'completion_tokens': 8}, {'type': 'finish', 'reason': 'tool_calls'}]


class RunTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / 'ws')
        self.registry = default_registry()
        self.events = []
        self.cfg = ('key', 'https://api.example.test/v1/chat/completions', 'model-x')

    def ctx(self, **kw):
        return ToolContext(workspace=self.ws, session_id='s1', policy=SandboxPolicy(mode='workspace-write'), **kw)

    def turn(self, stream_fn, message='do it', **kw):
        return agent_core.run_turn([{'role': 'user', 'content': message}], self.registry, self.ctx(), self.cfg,
                                   self.events.append, stream_fn=stream_fn, **kw)

    def test_native_tool_loop_executes_and_persists_messages(self):
        fn = scripted([tool_call_events('write_file', {'path': 'out.txt', 'content': 'hello'}), text_events('Wrote it.')])
        result = self.turn(fn)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.text, 'Wrote it.')
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(self.ws.path('out.txt').read_text(), 'hello')
        roles = [m['role'] for m in result.messages]
        self.assertEqual(roles, ['user', 'assistant', 'tool', 'assistant'])
        self.assertTrue(result.messages[2]['content'].startswith('UNTRUSTED TOOL RESULT (write_file):'))
        types = [e['type'] for e in self.events]
        for expected in ('tool_start', 'tool_end', 'text', 'usage', 'done'):
            self.assertIn(expected, types)
        self.assertEqual(result.usage['prompt_tokens'], 110)
        # system prompt is built per call and lists the tools and sandbox state
        system = fn.calls[0]['messages'][0]
        self.assertEqual(system['role'], 'system')
        self.assertIn('workspace-write', system['content'])
        self.assertTrue(fn.calls[0]['tools'])

    def test_parallel_read_only_calls_and_serial_mutations(self):
        self.ws.path('a.txt').write_text('A'); self.ws.path('b.txt').write_text('B')
        fn = scripted([[{'type': 'tool_calls', 'calls': [
            {'id': '1', 'name': 'read_file', 'arguments': '{"path": "a.txt"}'},
            {'id': '2', 'name': 'read_file', 'arguments': '{"path": "b.txt"}'},
            {'id': '3', 'name': 'write_file', 'arguments': '{"path": "c.txt", "content": "C"}'}]},
            {'type': 'finish', 'reason': 'tool_calls'}], text_events('done')])
        result = self.turn(fn)
        self.assertEqual(result.status, 'completed')
        tool_msgs = [m for m in result.messages if m['role'] == 'tool']
        self.assertEqual([m['tool_call_id'] for m in tool_msgs], ['1', '2', '3'], 'results keep call order')
        self.assertEqual(self.ws.path('c.txt').read_text(), 'C')

    def test_json_fallback_when_provider_rejects_tools(self):
        fn = scripted([providers.ToolsUnsupported('tools not supported'),
                       text_events('{"tool": "write_file", "arguments": {"path": "j.txt", "content": "via json"}}'),
                       text_events('All done.')])
        ctx = self.ctx()
        result = agent_core.run_turn([{'role': 'user', 'content': 'go'}], self.registry, ctx, self.cfg, self.events.append, stream_fn=fn)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.text, 'All done.')
        self.assertEqual(self.ws.path('j.txt').read_text(), 'via json')
        self.assertTrue(ctx.extras.get('json_mode'))
        self.assertIsNone(fn.calls[1]['tools'], 'JSON mode sends no native tools')
        self.assertIn('status', [e['type'] for e in self.events])
        # the JSON tool call text never reaches the user as a text delta
        self.assertFalse(any(e['type'] == 'text' and '"tool"' in e['delta'] for e in self.events))

    def test_step_budget_and_stop_event(self):
        fn = scripted([tool_call_events('current_time', {})])
        result = self.turn(fn, max_steps=3)
        self.assertEqual(result.status, 'max_steps')
        self.assertEqual(result.steps, 3)
        stop = threading.Event()
        ctx = ToolContext(workspace=self.ws, session_id='s1', policy=SandboxPolicy(), stop=stop)

        def stopping_stream(cfg, messages, tools=None, **kw):
            stop.set()
            yield {'type': 'text', 'delta': 'partial'}
            yield {'type': 'finish', 'reason': 'stop'}
        result = agent_core.run_turn([{'role': 'user', 'content': 'x'}], self.registry, ctx, self.cfg, self.events.append, stream_fn=stopping_stream)
        self.assertEqual(result.status, 'stopped')

    def test_transient_provider_errors_retry_then_fail_cleanly(self):
        import requests
        fn = scripted([requests.ConnectionError('boom'), text_events('recovered')])
        with patch.object(agent_core, 'RETRY_DELAY', 0.01):
            result = self.turn(fn)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.text, 'recovered')
        self.assertIn('retry', [e['type'] for e in self.events])
        fn2 = scripted([providers.ProviderError('HTTP 401: bad key')])
        result2 = self.turn(fn2)
        self.assertEqual(result2.status, 'error')
        self.assertTrue(any(e['type'] == 'error' for e in self.events))

    def test_compaction_triggers_on_small_context(self):
        long_history = []
        for i in range(10):
            long_history += [{'role': 'user', 'content': 'q%d ' % i + 'x' * 2000}, {'role': 'assistant', 'content': 'a%d ' % i + 'y' * 2000}]
        fn = scripted([text_events('ok')])
        result = agent_core.run_turn(long_history + [{'role': 'user', 'content': 'final'}], self.registry, self.ctx(), self.cfg,
                                     self.events.append, stream_fn=fn, context_tokens=3000,
                                     summarizer=lambda prompt: 'earlier: lots of x and y')
        self.assertEqual(result.status, 'completed')
        self.assertIn('compaction', [e['type'] for e in self.events])
        self.assertIn('earlier: lots of x and y', result.summary)
        self.assertLess(len(result.messages), len(long_history))

    def test_parse_json_tool_call(self):
        self.assertEqual(agent_core.parse_json_tool_call('{"tool": "x", "arguments": {"a": 1}}'), ('x', {'a': 1}))
        self.assertEqual(agent_core.parse_json_tool_call('```json\n{"tool": "x", "arguments": {}}\n```'), ('x', {}))
        self.assertEqual(agent_core.parse_json_tool_call('Let me check: {"tool": "x", "arguments": {}}'), ('x', {}))
        prose = 'The protocol looks like {"tool": "x", "arguments": {}} and ' + 'here is a long explanation. ' * 10
        self.assertIsNone(agent_core.parse_json_tool_call(prose))
        self.assertIsNone(agent_core.parse_json_tool_call('{"final": "no tool"}'))

    def test_parse_json_tool_call_after_plan(self):
        # Regression: a plan followed by the tool call must still parse, whatever its length
        # (seen live on NVIDIA NIM gemma: the whole reply was dropped as prose).
        call = '{"tool": "write_docx", "arguments": {"content": "' + 'line\\n' * 60 + '", "path": "letter.docx"}}'
        text = 'I will create a sample letter.\n\nPlan:\n1. Write the content.\n2. Use the write_docx tool.\n\n' + call
        self.assertEqual(agent_core.parse_json_tool_call(text)[0], 'write_docx')
        self.assertEqual(agent_core.parse_json_tool_call('Do it: {"tool": "x", "arguments": {}} - running it.'), ('x', {}))
        # braces inside string values do not break the balanced scan
        self.assertEqual(
            agent_core.parse_json_tool_call('{"tool": "x", "arguments": {"content": "brace } here"}}'),
            ('x', {'content': 'brace } here'}))
        # the last near-end object wins when several are present
        self.assertEqual(agent_core.parse_json_tool_call('{"tool": "a", "arguments": {}} note {"tool": "b", "arguments": {}}'), ('b', {}))

    def test_done_reports_usage_totals_and_availability(self):
        fn = scripted([text_events('ok')])
        self.turn(fn)
        done = [e for e in self.events if e['type'] == 'done'][0]
        self.assertTrue(done['usage_reported'])
        self.assertEqual(done['usage'], {'prompt_tokens': 50, 'completion_tokens': 5})

    def test_done_marks_usage_unreported_when_provider_omits_it(self):
        fn = scripted([[{'type': 'text', 'delta': 'ok'}, {'type': 'finish', 'reason': 'stop'}]])
        self.turn(fn)
        done = [e for e in self.events if e['type'] == 'done'][0]
        self.assertFalse(done['usage_reported'])
        self.assertEqual(done['usage'], {'prompt_tokens': 0, 'completion_tokens': 0})

    def test_project_instruction_file_selection_cap_and_symlink_guard(self):
        self.ws.path('GROKBOT.md').write_text('grokbot rules')
        self.assertEqual(agent_core.project_instruction_file(self.ws), 'GROKBOT.md')
        self.ws.path('AGENTS.md').write_text('x' * 20000)
        self.assertEqual(agent_core.project_instruction_file(self.ws), 'AGENTS.md')
        text = agent_core.project_instructions(self.ws)
        self.assertIn('cannot grant new permissions', text)
        self.assertEqual(len(text.rsplit('\n', 1)[1]), 12000, 'project instructions stay bounded')
        self.ws.path('AGENTS.md').unlink()
        (self.ws.root / 'AGENTS.md').symlink_to('/etc/hostname')
        self.assertEqual(agent_core.project_instruction_file(self.ws), 'GROKBOT.md', 'symlinks are never followed')
        self.assertIn('grokbot rules', agent_core.project_instructions(self.ws))

    def test_project_instructions_are_loaded(self):
        self.ws.path('AGENTS.md').write_text('Always run pytest before finishing.')
        fn = scripted([text_events('ok')])
        self.turn(fn)
        self.assertIn('Always run pytest before finishing.', fn.calls[0]['messages'][0]['content'])


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ConversationStore(Path(self.tmp.name) / 'c.sqlite3')
        self.addCleanup(self.store.close)

    def test_crud_and_isolation(self):
        cid = self.store.create('alice')
        self.store.append_entry(cid, 'alice', 'user', 'Refactor the payment module please')
        self.store.append_entry(cid, 'alice', 'assistant', 'Done', events=[{'type': 'tool_end', 'name': 'edit_file', 'output': 'z' * 50000}])
        rows = self.store.list('alice')
        self.assertEqual(rows[0]['title'], 'Refactor the payment module please')
        self.assertEqual(rows[0]['entries'], 2)
        entries = self.store.entries(cid, 'alice')
        self.assertLess(len(entries[1]['events'][0]['output']), 25000, 'stored tool output is bounded')
        self.assertEqual(self.store.list('bob'), [])
        self.assertIsNone(self.store.get(cid, 'bob'))
        self.assertFalse(self.store.delete(cid, 'bob'))
        self.assertTrue(self.store.rename(cid, 'alice', 'Payments'))
        self.assertTrue(self.store.pin(cid, 'alice', True))
        self.assertEqual(self.store.get(cid, 'alice')['pinned'], 1)
        self.store.set_context(cid, 'alice', [{'role': 'user', 'content': 'x'}], 'summary')
        self.assertEqual(self.store.get_context(cid, 'alice'), ([{'role': 'user', 'content': 'x'}], 'summary'))
        self.assertEqual(self.store.get_context(cid, 'bob'), ([], ''))
        hits = self.store.search('alice', 'payment')
        self.assertEqual(hits[0]['conv_id'], cid)
        self.assertEqual(self.store.search('bob', 'payment'), [])
        self.assertTrue(self.store.delete(cid, 'alice'))
        self.assertEqual(self.store.entries(cid, 'alice'), [])

    def test_latest_and_prune(self):
        a = self.store.create('u'); time.sleep(0.01); b = self.store.create('u')
        self.assertEqual(self.store.latest('u')['id'], b)
        self.store.pin(a, 'u', True)
        removed = self.store.prune(age_seconds=0)
        self.assertEqual(removed, 1, 'unpinned, untouched conversations older than the age are pruned')
        self.assertIsNotNone(self.store.get(a, 'u'), 'pinned conversations survive pruning')
        self.assertIsNone(self.store.get(b, 'u'))


class AgentHTTPTests(unittest.TestCase):
    def setUp(self):
        import webui
        self.webui = webui
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patches = [patch.object(webui, 'WORKSPACES', Path(self.tmp.name)),
                        patch.object(webui, 'CONVERSATIONS', None), patch.object(webui, 'TASKS', None),
                        patch.object(webui, 'POLICIES', webui.PolicyStore(Path(self.tmp.name))),
                        patch.dict(webui.app.config, {'MOCK': True, 'API_KEY': ''}),
                        patch.dict(webui.settings, {}, clear=True), patch.dict(webui.clients, {}, clear=True)]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        self.webui.APPROVALS.store = webui.POLICIES
        self.c = webui.app.test_client()

    def post(self, url, **kw):
        return self.c.post(url, base_url='http://localhost', **kw)

    def get(self, url):
        return self.c.get(url, base_url='http://localhost')

    def agent(self, message, cid=None):
        res = self.post('/agent', json={'message': message, 'conversation_id': cid})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertIn('application/x-ndjson', res.content_type)
        return [json.loads(line) for line in res.get_data(as_text=True).splitlines() if line.strip()]

    def test_agent_stream_persists_conversation_and_runs_tools(self):
        events = self.agent('hello')
        self.assertEqual(events[0]['type'], 'conversation')
        self.assertEqual(events[-1]['type'], 'done')
        self.assertIn('(mock) got your message: hello', ''.join(e.get('delta', '') for e in events if e['type'] == 'text'))
        cid = events[0]['id']
        events = self.agent('!tool write_file {"path": "notes/a.txt", "content": "hi"}', cid)
        types = [e['type'] for e in events]
        self.assertIn('tool_start', types); self.assertIn('tool_end', types)
        self.assertTrue(next(e for e in events if e['type'] == 'tool_end')['ok'])
        current = self.get('/conversations/current').get_json()
        self.assertEqual(current['id'], cid)
        self.assertEqual([e['role'] for e in current['entries']], ['user', 'assistant', 'user', 'assistant'])
        self.assertTrue(any(ev['type'] == 'tool_end' for ev in current['entries'][3]['events']))
        self.assertEqual(self.get('/conversations').get_json()[0]['title'], 'hello')
        self.assertEqual(self.get('/history').get_json()[0]['content'], 'hello', 'legacy history mirrors the exchange')
        sid_dirs = [p for p in Path(self.tmp.name).iterdir() if p.is_dir() and not p.name.startswith('.')]
        self.assertEqual((sid_dirs[0] / 'notes/a.txt').read_text(), 'hi')

    def test_agent_validation_and_busy(self):
        self.assertEqual(self.post('/agent', json={'message': ''}).status_code, 400)
        self.assertEqual(self.post('/agent', data='x', content_type='application/json').status_code, 400)
        self.assertEqual(self.post('/agent', json={'message': 'x' * 12001}).status_code, 400)
        with self.c.session_transaction() as s:
            s['sid'] = 'fixed'
        self.get('/history')  # materialises the per-session client record
        client = self.webui.clients['fixed']
        client['busy'].acquire()
        try:
            self.assertEqual(self.post('/agent', json={'message': 'hi'}).status_code, 409)
            self.assertEqual(self.post('/conversations', json={}).status_code, 409)
        finally:
            client['busy'].release()

    def test_policy_route_and_approval_flow(self):
        policy = self.get('/policy').get_json()
        self.assertEqual(policy['mode'], 'workspace-write')
        self.assertIn('run_command', policy['tools'])
        self.assertEqual(self.post('/policy', json={'mode': 'bogus'}).status_code, 400)
        self.assertEqual(self.post('/policy', json={'mode': 'read-only'}).get_json()['mode'], 'read-only')
        decisions = []

        def approver(decision):
            for _ in range(200):
                pending = self.get('/agent/pending').get_json()
                if pending:
                    decisions.append(pending[0]['always_pattern'])
                    self.assertEqual(self.post('/agent/approve', json={'id': pending[0]['id'], 'decision': decision}).status_code, 200)
                    return
                time.sleep(0.02)
        t = threading.Thread(target=approver, args=('deny',)); t.start()
        events = self.agent('!tool write_file {"path": "x.txt", "content": "1"}')
        t.join()
        self.assertIn('approval_required', [e['type'] for e in events])
        self.assertFalse(next(e for e in events if e['type'] == 'tool_end')['ok'])
        cid = events[0]['id']
        t = threading.Thread(target=approver, args=('always',)); t.start()
        events = self.agent('!tool write_file {"path": "x.txt", "content": "2"}', cid)
        t.join()
        self.assertTrue(next(e for e in events if e['type'] == 'tool_end')['ok'])
        self.assertEqual(self.get('/policy').get_json()['allow'], ['write_file:*'])
        self.assertEqual(decisions, ['write_file:*', 'write_file:*'])
        events = self.agent('!tool write_file {"path": "x.txt", "content": "3"}', cid)
        self.assertNotIn('approval_required', [e['type'] for e in events])
        self.assertEqual(self.post('/agent/approve', json={'id': 'nope', 'decision': 'allow'}).status_code, 404)
        self.assertEqual(self.post('/agent/approve', json={'id': 'nope', 'decision': 'maybe'}).status_code, 400)
        self.assertEqual(self.post('/policy', json={'allow': []}).get_json()['allow'], [])

    def test_never_policy_denies_without_prompt_and_read_only_commands_run(self):
        self.post('/policy', json={'mode': 'read-only', 'approval': 'never'})
        events = self.agent('!tool run_command {"command": "echo hi > out.txt"}')
        self.assertNotIn('approval_required', [e['type'] for e in events])
        self.assertFalse(next(e for e in events if e['type'] == 'tool_end')['ok'])
        events = self.agent('!tool run_command {"command": "echo hi"}', events[0]['id'])
        end = next(e for e in events if e['type'] == 'tool_end')
        from grokbot import sandbox as _sb
        if _sb.get_backend().isolated:
            self.assertTrue(end['ok']); self.assertIn('hi', end['output'])
        else:  # no Docker: even read-only commands need approval, and "never" denies without prompting
            self.assertFalse(end['ok'])
            self.assertNotIn('approval_required', [e['type'] for e in events])

    def test_conversation_management_routes(self):
        a = self.agent('first topic about kittens')[0]['id']
        b = self.post('/conversations', json={}).get_json()['id']
        self.assertNotEqual(a, b)
        self.assertEqual(self.get('/conversations/current').get_json()['id'], b)
        self.assertEqual(self.post('/conversations/%s/select' % a).get_json()['id'], a)
        self.assertEqual(self.post('/conversations/%s/rename' % a, json={'title': 'Kittens'}).status_code, 200)
        self.assertEqual(self.post('/conversations/%s/rename' % a, json={'title': ''}).status_code, 400)
        self.assertEqual(self.post('/conversations/%s/pin' % a, json={'pinned': True}).status_code, 200)
        rows = self.get('/conversations').get_json()
        self.assertEqual(rows[0]['id'], a, 'pinned first')
        self.assertEqual(rows[0]['title'], 'Kittens')
        self.assertEqual(self.get('/conversations/search?q=kittens').get_json()[0]['conv_id'], a)
        self.assertEqual(self.get('/conversations/%s' % a).get_json()['entries'][0]['content'], 'first topic about kittens')
        self.assertEqual(self.post('/conversations/%s/delete' % a).status_code, 200)
        self.assertEqual(self.get('/conversations/%s' % a).status_code, 404)
        # other sessions cannot see or touch it
        other = self.webui.app.test_client()
        self.assertEqual(other.get('/conversations/%s' % b, base_url='http://localhost').status_code, 404)
        self.assertEqual(other.post('/conversations/%s/delete' % b, base_url='http://localhost').status_code, 404)

    def test_compact_set_model_and_capabilities(self):
        cid = self.agent('one')[0]['id']
        self.agent('two', cid)
        res = self.post('/compact', json={'conversation_id': cid})
        self.assertEqual(res.status_code, 200)
        self.assertIn(res.get_json()['method'], ('truncate', 'none', 'model'))
        self.assertEqual(self.post('/set_model', json={'model': 'm2', 'context_length': 32000}).status_code, 200)
        status = self.get('/key_status').get_json()
        self.assertEqual((status['model'], status['context_length']), ('m2', 32000))
        self.assertEqual(self.post('/set_model', json={'model': 'm3'}).status_code, 200)
        self.assertNotEqual(self.get('/key_status').get_json()['context_length'], 32000, 'window resets with a new model')
        self.assertEqual(self.post('/set_model', json={'model': 'm', 'context_length': 5}).status_code, 400)
        caps = self.get('/capabilities').get_json()
        self.assertIn('run_command', [t['name'] for t in caps['tools']])
        self.assertIn('sandbox', caps)

    def test_index_serves_external_assets_only(self):
        page = self.get('/').get_data(as_text=True)
        self.assertIn('/static/app.js', page)
        self.assertNotIn('<script>', page, 'no inline scripts: CSP stays script-src self')
        self.assertEqual(self.get('/static/vendor/marked.min.js').status_code, 200)
        self.assertEqual(self.get('/static/vendor/purify.min.js').status_code, 200)


class ModelDiscoveryDetailsTests(unittest.TestCase):
    def test_context_length_extraction(self):
        import model_discovery as md
        self.assertEqual(md.context_length({'id': 'a', 'context_length': 128000}), 128000)
        self.assertEqual(md.context_length({'id': 'a', 'top_provider': {'context_length': 200000}}), 200000)
        self.assertEqual(md.context_length({'id': 'a', 'inputTokenLimit': '1048576'}), 1048576)
        self.assertEqual(md.context_length({'id': 'a', 'context_length': 5}), 0)
        self.assertEqual(md.context_length({'id': 'a', 'context_length': True}), 0)
        self.assertEqual(md.context_length('nope'), 0)


if __name__ == '__main__':
    unittest.main()
