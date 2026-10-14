"""Offline regression tests for the six local fixes; no provider requests."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import requests
import webui
from agent_loop import Workspace, ToolError, run_loop


class AuthRegressionTests(unittest.TestCase):
    def test_remote_peer_cannot_spoof_loopback_host(self):
        with patch.object(webui, 'PASSWORD', ''):
            response = webui.app.test_client().post('/chat', base_url='http://localhost',
                environ_overrides={'REMOTE_ADDR': '203.0.113.10'}, json={'message': 'hi'})
            self.assertEqual(response.status_code, 403)

    def test_proxy_headers_do_not_authorize_local_mode(self):
        with patch.object(webui, 'PASSWORD', ''):
            response = webui.app.test_client().get('/', base_url='http://localhost',
                headers={'X-Forwarded-For': '127.0.0.1'})
            self.assertEqual(response.status_code, 403)

    def test_exposed_mode_requires_password_even_for_local_proxy(self):
        with patch.object(webui, 'PASSWORD', ''), patch.object(webui, 'DEPLOYMENT_MODE', 'exposed', create=True):
            self.assertEqual(webui.app.test_client().get('/', base_url='http://localhost').status_code, 403)

    def test_remote_peer_denied_on_sensitive_routes(self):
        with patch.object(webui, 'PASSWORD', ''):
            for route in ('/chat', '/task', '/set_key'):
                response = webui.app.test_client().post(route, json={},
                    environ_overrides={'REMOTE_ADDR': '203.0.113.10'})
                self.assertEqual(response.status_code, 403)

    def test_unknown_peer_and_invalid_mode_fail_closed(self):
        with patch.object(webui, 'PASSWORD', ''):
            self.assertEqual(webui.app.test_client().get('/', environ_overrides={'REMOTE_ADDR': ''}).status_code, 403)
            with patch.object(webui, 'DEPLOYMENT_MODE', 'typo'):
                self.assertEqual(webui.app.test_client().get('/').status_code, 403)

    def test_ipv6_loopback_works(self):
        with patch.object(webui, 'PASSWORD', ''):
            self.assertEqual(webui.app.test_client().get('/', base_url='http://[::1]',
                environ_overrides={'REMOTE_ADDR': '::1'}).status_code, 200)

    def test_direct_loopback_still_works(self):
        with patch.object(webui, 'PASSWORD', ''):
            self.assertEqual(webui.app.test_client().get('/', base_url='http://localhost').status_code, 200)

    def test_authenticated_proxy_ignores_forwarded_identity(self):
        client = webui.app.test_client()
        with patch.object(webui, 'PASSWORD', 'test-password'), patch.object(webui, 'DEPLOYMENT_MODE', 'exposed', create=True):
            self.assertEqual(client.get('/', base_url='http://localhost').status_code, 302)
            client.post('/login', data={'password': 'test-password'})
            self.assertEqual(client.get('/', headers={'X-Forwarded-For': '203.0.113.10'}).status_code, 200)


class CopyCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(self.tmp.name)
        self.ws.execute({'action': 'write_file', 'path': 'uploads/proof.txt.txt', 'content': 'proof\n'}, allow_upload=True)
        self.ws.checked.add('uploads/proof.txt.txt')

    def run_actions(self, actions, goal='Copy proof.txt exactly to status.txt'):
        replies = iter(actions)
        return list(run_loop(goal, [], self.ws, lambda _: json.dumps(next(replies)), lambda: False, len(actions)))

    def test_no_write_done_is_not_completion(self):
        events = self.run_actions([{'action': 'done', 'text': 'Copied and verified.'}])
        self.assertEqual(events[-1]['type'], 'limit')
        self.assertFalse(self.ws.path('status.txt').exists())
        self.assertIn('status.txt', str(events))

    def test_wrong_target_cannot_satisfy_expected_artifact(self):
        events = self.run_actions([
            {'action': 'copy_file', 'source_path': 'uploads/proof.txt.txt', 'path': 'wrong.txt'},
            {'action': 'read_file', 'path': 'wrong.txt'}, {'action': 'done', 'text': 'Done'}])
        self.assertEqual(events[-1]['type'], 'limit')

    def test_preexisting_matching_output_is_valid_evidence(self):
        self.ws.execute({'action': 'copy_file', 'source_path': 'uploads/proof.txt.txt', 'path': 'status.txt'})
        self.ws.checked.add('status.txt')
        events = self.run_actions([{'action': 'done', 'text': 'Already matches'}])
        self.assertEqual(events[-1]['type'], 'done')

    def test_wrong_content_requires_repair(self):
        events = self.run_actions([
            {'action': 'write_file', 'path': 'status.txt', 'content': 'wrong'},
            {'action': 'read_file', 'path': 'status.txt'}, {'action': 'done', 'text': 'Done'},
            {'action': 'copy_file', 'source_path': 'uploads/proof.txt.txt', 'path': 'status.txt'},
            {'action': 'read_file', 'path': 'status.txt'}, {'action': 'done', 'text': 'Repaired'}])
        self.assertEqual(events[-1]['type'], 'done')
        self.assertTrue(any(e['type'] == 'check' for e in events))

    def test_ambiguous_exact_copy_needs_explicit_paths(self):
        events = self.run_actions([{'action': 'done', 'text': 'Done'}], 'Copy the document exactly somewhere useful')
        self.assertEqual(events[-1]['type'], 'blocked')

    def test_exact_copy_rejects_unsafe_or_ambiguous_paths(self):
        goals = ['Copy proof.txt exactly to ../escape',
                 'Copy proof.txt exactly to uploads/out',
                 'Copy missing.txt exactly to out']
        try:
            self.ws.path('link').symlink_to('/opt/data/cache/scratch')
        except (OSError, NotImplementedError):
            pass
        else:
            goals.append('Copy proof.txt exactly to link/out')
        for goal in goals:
            with self.subTest(goal=goal):
                self.assertEqual(self.run_actions([{'action': 'done', 'text': 'Done'}], goal)[-1]['type'], 'blocked')

    def test_current_request_checks_bytes_again(self):
        self.ws.execute({'action': 'copy_file', 'source_path': 'uploads/proof.txt.txt', 'path': 'status.txt'})
        self.ws.checked.add('status.txt')
        self.ws.path('status.txt').write_bytes(b'changed')
        self.assertEqual(self.run_actions([{'action': 'done', 'text': 'Done'}])[-1]['type'], 'limit')


class ByteCopyTests(CopyCompletionTests):
    def test_copy_preserves_crlf_and_lone_cr(self):
        for raw in (b'first\r\nsecond\r\n', b'first\rsecond\r'):
            with self.subTest(raw=raw):
                self.ws.path('source').write_bytes(raw)
                self.ws.execute({'action': 'copy_file', 'source_path': 'source', 'path': 'target'})
                self.assertEqual(self.ws.path('target').read_bytes(), raw)
                if os.name != 'nt':
                    self.assertEqual(self.ws.path('target').stat().st_mode & 0o777, 0o600)
                self.assertNotIn('target', self.ws.checked)

    def test_copy_retains_bounds_and_upload_protection(self):
        self.ws.path('source').write_bytes(b'x' * 32001)
        with self.assertRaises(ToolError):
            self.ws.execute({'action': 'copy_file', 'source_path': 'source', 'path': 'target'})
        self.ws.path('source').write_bytes(b'x')
        for target in ('uploads/proof.txt.txt', '../escape', '/outside'):
            with self.assertRaises(ToolError):
                self.ws.execute({'action': 'copy_file', 'source_path': 'source', 'path': target})
        for i in range(8):
            self.ws.execute({'action': 'write_file', 'path': str(i), 'content': 'x' * 31999})
        with self.assertRaises(ToolError):
            self.ws.execute({'action': 'copy_file', 'source_path': '0', 'path': 'full'})

    def test_copy_retains_file_count_limit(self):
        for i in range(24):
            self.ws.execute({'action': 'write_file', 'path': str(i), 'content': 'x'})
        with self.assertRaises(ToolError):
            self.ws.execute({'action': 'copy_file', 'source_path': '0', 'path': 'extra'})


class RepeatTests(CopyCompletionTests):
    def test_successful_rereads_remain_available(self):
        self.ws.execute({'action': 'write_file', 'path': 'a', 'content': 'reference'})
        events = self.run_actions([{'action': 'read_file', 'path': 'a'}] * 4 + [{'action': 'done', 'text': 'Read'}], 'goal')
        self.assertEqual(events[-1]['type'], 'done')

    def test_read_write_progress_resets_failure_streak(self):
        actions = []
        for i in range(3):
            actions += [{'action': 'read_file', 'path': 'uploads/proof.txt.txt'},
                        {'action': 'write_file', 'path': 'out', 'content': str(i)},
                        {'action': 'read_file', 'path': 'out'}]
        events = self.run_actions(actions + [{'action': 'done', 'text': 'Done'}], 'goal')
        self.assertEqual(events[-1]['type'], 'done')

    def test_failed_fingerprint_ignores_unused_fields(self):
        events = self.run_actions([{'action': 'read_file', 'path': 'missing', 'text': str(i)} for i in range(4)], 'goal')
        self.assertEqual(events[-1]['type'], 'blocked')

    def test_success_between_failures_resets_streak(self):
        actions = []
        for _ in range(3):
            actions += [{'action': 'read_file', 'path': 'missing'}, {'action': 'read_file', 'path': 'uploads/proof.txt.txt'}]
        self.assertEqual(self.run_actions(actions + [{'action': 'done', 'text': 'Done'}], 'goal')[-1]['type'], 'done')

    def test_repeated_mismatch_stops_no_progress(self):
        self.ws.execute({'action': 'write_file', 'path': 'wrong', 'content': 'wrong'})
        action = {'action': 'compare_files', 'source_path': 'uploads/proof.txt.txt', 'path': 'wrong'}
        self.assertEqual(self.run_actions([action] * 4, 'goal')[-1]['type'], 'blocked')

    def test_successful_read_loop_still_has_step_budget(self):
        events = self.run_actions([{'action': 'read_file', 'path': 'uploads/proof.txt.txt'}] * 4, 'goal')
        self.assertEqual(events[-1]['type'], 'limit')
        self.assertEqual(events[-1]['step'], 4)


class JsonShapeTests(unittest.TestCase):
    def test_all_post_routes_reject_nonobjects(self):
        with patch.object(webui, 'PASSWORD', ''):
            client = webui.app.test_client()
            for route in ('/chat', '/task', '/set_key'):
                for data in (['not', 'object'], [], 'text', 42, True, None):
                    with self.subTest(route=route, data=data):
                        response = client.post(route, data=json.dumps(data), content_type='application/json')
                        self.assertEqual(response.status_code, 400)


class StreamReply:
    def __init__(self, lines, failure=None):
        self.lines = lines
        self.failure = failure
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_lines(self, **_):
        yield from self.lines
        if self.failure:
            raise self.failure

    def close(self):
        self.closed = True


class CliStreamTests(unittest.TestCase):
    chunk = 'data: {"choices":[{"delta":{"content":"partial"}}]}'

    def test_stream_failure_is_structured_and_not_saved(self):
        response = StreamReply([self.chunk], requests.ConnectionError('do not log private provider text'))
        chat = bot.Chat('', 'offline-test')
        with patch.object(bot.requests, 'post', return_value=response), contextlib.redirect_stdout(io.StringIO()):
            result = chat.ask('hello')
        self.assertEqual(result.status, 'interrupted')
        self.assertEqual(result.partial, 'partial')
        self.assertTrue(response.closed)
        self.assertEqual([m['role'] for m in chat.history], ['system'])
        self.assertNotIn('private', str(result))

    def test_interactive_loop_recovers_after_failure(self):
        response = StreamReply([self.chunk], requests.ConnectionError('offline failure'))
        with patch.object(bot.requests, 'post', return_value=response), patch('builtins.input', side_effect=['hello', '/new', '/exit']), patch.object(bot.sys, 'argv', ['bot.py']), patch.dict(bot.os.environ, {'GROK_API_KEY': 'TEST_ONLY_NOT_REAL'}), contextlib.redirect_stdout(io.StringIO()) as output:
            bot.main()
        self.assertIn('interrupted', output.getvalue().lower())
        self.assertIn('memory cleared', output.getvalue())
        self.assertIn('bye!', output.getvalue())

    def test_malformed_sse_shapes_do_not_crash(self):
        response = StreamReply(['data: []', 'data: {"choices":null}', 'data: {"choices":[{"delta":[]}]}', 'data: bad json', self.chunk, 'data: [DONE]'])
        chat = bot.Chat('', 'offline-test')
        with patch.object(bot.requests, 'post', return_value=response), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(chat.ask('hello'), 'partial')
        self.assertEqual(chat.history[-1], {'role': 'assistant', 'content': 'partial'})
        self.assertTrue(response.closed)

    def test_eof_without_done_does_not_save_partial(self):
        response = StreamReply([self.chunk])
        chat = bot.Chat('', 'offline-test')
        with patch.object(bot.requests, 'post', return_value=response), contextlib.redirect_stdout(io.StringIO()):
            result = chat.ask('hello')
        self.assertEqual(result.status, 'interrupted')
        self.assertEqual(len(chat.history), 1)

    def test_http_error_closes_response_and_preserves_history(self):
        response = StreamReply([])
        response.raise_for_status = lambda: (_ for _ in ()).throw(requests.HTTPError('private provider detail'))
        chat = bot.Chat('', 'offline-test')
        with patch.object(bot.requests, 'post', return_value=response), contextlib.redirect_stdout(io.StringIO()):
            result = chat.ask('hello')
        self.assertEqual(result.status, 'interrupted')
        self.assertTrue(response.closed)
        self.assertEqual(len(chat.history), 1)

    def test_followup_after_interruption_has_no_partial_history(self):
        responses = [StreamReply([self.chunk], requests.ConnectionError('offline')),
                     StreamReply([self.chunk, 'data: [DONE]'])]
        chat = bot.Chat('', 'offline-test')
        with patch.object(bot.requests, 'post', side_effect=responses), contextlib.redirect_stdout(io.StringIO()):
            chat.ask('failed turn')
            self.assertEqual(chat.ask('retry'), 'partial')
        self.assertEqual([m['content'] for m in chat.history[1:]], ['retry', 'partial'])

    def test_initial_connection_error_does_not_fabricate_history(self):
        chat = bot.Chat('', 'offline-test')
        with patch.object(bot.requests, 'post', side_effect=requests.ConnectionError('private text')):
            result = chat.ask('hello')
        self.assertEqual(result.status, 'interrupted')
        self.assertEqual(len(chat.history), 1)


if __name__ == '__main__':
    unittest.main()
