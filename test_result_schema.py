"""Result schema regression tests through the actual provider payload path."""
import json
import unittest
from unittest.mock import patch
import webui
from agent_loop import ACTION_SCHEMA, result_action_schema
from task_harness import TaskManager

class ResultSchemaTests(unittest.TestCase):
    def test_strict_provider_payload_can_return_result(self):
        payloads = []
        class Reply:
            headers = {}
            def raise_for_status(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def iter_content(self, chunk_size):
                yield json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'action': 'done', 'text': 'ok', 'path': '', 'source_path': '', 'content': '', 'expression': '', 'result': {'Summary': 'hello'}})}}]}).encode()
        def post(*args, **kwargs):
            payloads.append(kwargs['json'])
            return Reply()
        with patch.object(webui, 'config_snapshot', return_value=('fake', webui.API_URL, 'fake-model')), patch.object(webui.requests, 'post', post):
            value = json.loads(webui.task_provider(['Summary'])([], 32000))
        schema = payloads[0]['response_format']['json_schema']['schema']
        self.assertIn('result', schema['required'])
        self.assertEqual(set(value), set(schema['properties']))
        self.assertEqual(schema['properties']['result'], {'type': 'object', 'properties': {'Summary': {'type': 'string', 'description': 'Nonempty text value on done; empty string on other actions.'}}, 'required': ['Summary'], 'additionalProperties': False})
        self.assertEqual(TaskManager._check_result_schema({'result_schema': ['Summary']}, value['result']), [])

    def test_default_action_schema_is_unchanged_and_not_mutated(self):
        result_action_schema(['Summary'])
        self.assertNotIn('result', ACTION_SCHEMA['properties'])
        self.assertIs(result_action_schema(), ACTION_SCHEMA)
        self.assertIs(result_action_schema([]), ACTION_SCHEMA)

    def test_empty_and_nontext_values_fail(self):
        for value in [None, '', '  ', [], {}, False, 0]:
            self.assertEqual(TaskManager._check_result_schema({'result_schema': ['Summary']}, {'Summary': value}), ['Summary'])
        self.assertEqual(TaskManager._check_result_schema({'result_schema': ['Summary']}, {'S U M-MARY.': 'text'}), [])

    def test_normalized_empty_names_rejected_before_submission(self):
        import pathlib, tempfile
        with tempfile.TemporaryDirectory() as root:
            m = TaskManager(pathlib.Path(root)/'tasks.db', pathlib.Path(root)/'ws')
            try:
                for field in ['!!!', '___', '日本語']:
                    with self.assertRaises(ValueError):
                        m.submit('alice', 'goal', [], lambda _: '', result_schema=[field])
                self.assertEqual(m.list('alice'), [])
            finally:
                m.close()

    def test_http_submit_and_resume_keep_result_schema(self):
        import pathlib, tempfile, time
        with tempfile.TemporaryDirectory() as root:
            m = TaskManager(pathlib.Path(root)/'tasks.db', pathlib.Path(root)/'ws')
            captured = []
            def provider(fields=None):
                captured.append(fields)
                return lambda *a, **k: json.dumps({'action': 'need_input', 'text': 'wait'})
            with patch.object(webui, 'TASKS', m), patch.object(webui, 'WORKSPACES', pathlib.Path(root)/'ws'), patch.object(webui, 'task_provider', provider), patch.object(webui, 'PASSWORD', ''), patch.object(webui, 'DEPLOYMENT_MODE', 'local'), patch.dict(webui.app.config, {'MOCK': False}):
                webui.clients.clear()
                client = webui.app.test_client()
                response = client.post('/task', json={'message': 'goal', 'result_schema': ['Summary']})
                self.assertEqual(response.status_code, 202)
                tid = response.json['id']
                for _ in range(300):
                    if client.get('/tasks/'+tid).json['status'] == 'needs_input' and tid not in m.threads:
                        break
                    time.sleep(.01)
                self.assertEqual(client.post('/tasks/'+tid+'/resume', json={}).status_code, 202)
                self.assertEqual(captured, [['Summary'], ['Summary']])
                m.close()
                webui.clients.clear()
