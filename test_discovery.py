"""Model discovery contracts with fake keys and mocked HTTP only."""
import json
import unittest
from unittest.mock import patch

import model_discovery as md
import webui
from test_providers import Base, FAKE_NV, FAKE_GEM, fake_addrinfo


class Reply:
    def __init__(self, data=None, status=200, raw=None):
        self.status_code = status
        self.body = json.dumps(data).encode() if raw is None else raw
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, *args): yield self.body


def reply(*ids, **kw):
    return Reply({'data': [{'id': i} for i in ids], **kw})


class DiscoveryTests(Base):
    def setUp(self):
        super().setUp()
        md.clear_cache()
        self.addCleanup(md.clear_cache)

    def models(self, **data):
        return self.post('/models', json=data)

    def test_all_explicit_endpoints(self):
        for pid in webui.PROVIDERS:
            with self.subTest(pid=pid), patch('model_discovery.requests.get', return_value=reply('z', 'a', 'a')) as get:
                r = self.models(provider=pid, key='FAKE', refresh=True)
                self.assertEqual(r.get_json()['models'], ['a', 'z'])
                args, kw = get.call_args
                self.assertEqual(args[0], md.MODEL_URLS[pid])
                self.assertFalse(kw['allow_redirects'])
                self.assertTrue(kw['stream'])
        self.assertEqual(md.MODEL_URLS['gemini'], 'https://generativelanguage.googleapis.com/v1beta/openai/models')

    def test_no_saved_key_cross_provider_or_guessed_custom(self):
        self.post('/set_key', json={'key': FAKE_NV, 'provider': 'nvidia', 'model': 'manual'})
        with patch('model_discovery.requests.get') as get:
            self.assertEqual(self.models(provider='gemini').status_code, 400)
            self.assertEqual(self.models(url='https://tokenharbor.ai/v1/chat/completions').status_code, 400)
            get.assert_not_called()
        self.assertEqual(self.status()['model'], 'manual')

    def test_current_key_used_without_settings_mutation(self):
        self.post('/set_key', json={'key': FAKE_GEM, 'provider': 'gemini', 'model': 'manual'})
        with patch('model_discovery.requests.get', return_value=reply('new')) as get:
            self.assertEqual(self.models(provider='gemini').status_code, 200)
            self.assertEqual(get.call_args.kwargs['headers']['Authorization'], 'Bearer ' + FAKE_GEM)
        self.assertEqual(self.status()['model'], 'manual')

    def test_blank_save_preserves_current_model_on_same_provider(self):
        self.post('/set_key', json={'key': FAKE_GEM, 'provider': 'gemini', 'model': 'manual'})
        self.post('/set_key', json={'key': FAKE_GEM, 'provider': 'gemini', 'model': ''})
        self.assertEqual(self.status()['model'], 'manual')

    def test_cache_scoped_key_provider_refresh_and_save(self):
        with patch('model_discovery.requests.get', side_effect=lambda *a, **k: reply('a')) as get:
            for key, pid in [('one', 'gemini'), ('one', 'gemini'), ('two', 'gemini'), ('one', 'nvidia')]:
                self.models(provider=pid, key=key)
            self.assertEqual(get.call_count, 3)
            self.models(provider='gemini', key='one', refresh=True)
            self.assertEqual(get.call_count, 4)
            self.post('/set_key', json={'key': FAKE_GEM, 'provider': 'gemini'})
            self.models(provider='gemini', key='one')
            self.assertEqual(get.call_count, 5)

    def test_custom_explicit_same_origin_public_only(self):
        with patch('webui.socket.getaddrinfo', return_value=fake_addrinfo(['8.8.8.8'])), patch('model_discovery.requests.get', return_value=reply('custom')) as get:
            data = dict(url='https://tokenharbor.ai/v1/chat/completions', models_url='https://tokenharbor.ai/v1/models', key='FAKE')
            self.assertEqual(self.models(**data).get_json()['models'], ['custom'])
            for bad in ['https://evil.example/models', 'http://tokenharbor.ai/models', 'https://tokenharbor.ai/v1/models?key=x', 'https://tokenharbor.ai/v1/models#x']:
                self.assertEqual(self.models(**dict(data, models_url=bad)).status_code, 400)
            self.assertEqual(get.call_count, 1)
        with patch('webui.socket.getaddrinfo', return_value=fake_addrinfo(['127.0.0.1'])), patch('model_discovery.requests.get') as get:
            self.assertEqual(self.models(**data).status_code, 400)
            get.assert_not_called()

    def test_bounded_pagination_same_endpoint_only(self):
        ep = md.MODEL_URLS['openrouter']
        with patch('model_discovery.requests.get', side_effect=[reply('a', links={'next': '/api/v1/models?offset=1'}), reply('b')]) as get:
            self.assertEqual(md.discover('FAKE', ep, 'openrouter')['models'], ['a', 'b'])
            self.assertEqual(get.call_args.args[0], ep + '?offset=1')
        for link in ['https://evil.example/models', '//evil.example/models', '/api/v1/chat/completions', 'https://user:pass@openrouter.ai/api/v1/models', '/api/v1/models#x']:
            with patch('model_discovery.requests.get', return_value=reply('a', links={'next': link})) as get:
                with self.assertRaises(md.DiscoveryError): md.discover('FAKE', ep, 'openrouter', True)
                self.assertEqual(get.call_count, 1)

    def test_token_cursor_and_repeated_page_limits(self):
        for more, param in [({'nextPageToken': 'token'}, {'pageToken': 'token'}), ({'has_more': True, 'last_id': 'a'}, {'after': 'a'})]:
            with patch('model_discovery.requests.get', side_effect=[reply('a', **more), reply('b')]) as get:
                self.assertEqual(md.discover('FAKE', md.MODEL_URLS['gemini'], refresh=True)['models'], ['a', 'b'])
                self.assertEqual(get.call_args.kwargs['params'], param)
        with patch('model_discovery.requests.get', return_value=reply('a', nextPageToken='same')) as get:
            with self.assertRaises(md.DiscoveryError): md.discover('FAKE', md.MODEL_URLS['gemini'], refresh=True)
            self.assertEqual(get.call_count, 2)
        with patch('model_discovery.requests.get', side_effect=[reply(str(i), nextPageToken=str(i)) for i in range(5)]):
            with self.assertRaises(md.DiscoveryError): md.discover('FAKE', md.MODEL_URLS['gemini'], refresh=True)

    def test_errors_never_reflect_body_key_or_exception(self):
        for res in [Reply(status=302), Reply(status=401), Reply(status=403), Reply(status=429), Reply(status=500), Reply(raw=b'FAKE_SECRET <html>'), Reply({'models': []})]:
            with patch('model_discovery.requests.get', return_value=res):
                r = self.models(provider='gemini', key='FAKE_SECRET', refresh=True)
                self.assertEqual(r.status_code, 502)
                self.assertNotIn('FAKE_SECRET', r.get_data(as_text=True))
        with patch('model_discovery.requests.get', side_effect=md.requests.Timeout('FAKE_SECRET')):
            self.assertNotIn('FAKE_SECRET', self.models(provider='gemini', key='FAKE_SECRET').get_data(as_text=True))

    def test_malformed_inputs_and_empty_list(self):
        for data in [[], None, {'provider': []}, {'provider': 'unknown'}, {'provider': 'gemini', 'refresh': 'yes'}, {'provider': 'gemini', 'url': 'https://evil.example'}]:
            self.assertEqual(self.post('/models', json=data).status_code, 400)
        with patch('model_discovery.requests.get', return_value=reply()):
            r = self.models(provider='gemini', key='FAKE')
            self.assertEqual(r.get_json()['models'], [])
            self.assertIn('manually', r.get_json()['message'])

    def test_response_and_model_caps_filter_non_chat(self):
        with patch.object(md, 'MAX_BYTES', 10), patch('model_discovery.requests.get', return_value=reply('oversize')):
            with self.assertRaises(md.DiscoveryError): md.discover('FAKE', md.MODEL_URLS['gemini'], refresh=True)
        with patch.object(md, 'MAX_MODELS', 1), patch('model_discovery.requests.get', return_value=reply('a', 'b')):
            with self.assertRaises(md.DiscoveryError): md.discover('FAKE', md.MODEL_URLS['gemini'], refresh=True)
        rows = [{'id': 'chat'}, {'id': 'embed', 'capabilities': {'completion_chat': False}}, {'id': 1}, {'id': '\n'}, {}, {'id': '<script>safe-text</script>'}]
        with patch('model_discovery.requests.get', return_value=Reply({'data': rows})):
            self.assertEqual(md.discover('FAKE', md.MODEL_URLS['mistral'], refresh=True)['models'], ['<script>safe-text</script>', 'chat'])


if __name__ == '__main__': unittest.main()
