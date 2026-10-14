"""Provider picker: presets, prefix autodetect, custom-URL SSRF guard, labels.
Providers are fakes; the only keys used here are obviously fake placeholders."""
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import task_harness
import webui
from task_harness import friendly_provider_error, provider_name

FAKE_NV = 'nvapi-FAKE-NOT-A-REAL-KEY-000000000000001'
FAKE_OR = 'sk-or-FAKE-NOT-A-REAL-KEY-00000000000001'
FAKE_GROQ = 'gsk_FAKENOTAREALKEY00000000000000000001'
FAKE_GEM = 'AIzaFAKE-NOT-A-REAL-KEY-0123456789x9k'
NVIDIA = 'https://integrate.api.nvidia.com/v1/chat/completions'
OPENROUTER = 'https://openrouter.ai/api/v1/chat/completions'
GROQ = 'https://api.groq.com/openai/v1/chat/completions'
GEMINI = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
CUSTOM = 'https://api.example-llm.test/v1/chat/completions'


def fake_addrinfo(ips):
    return [(2, 1, 6, '', (ip, 443)) for ip in ips]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.manager = task_harness.TaskManager(root / 'tasks.db', root / 'workspaces')
        self.addCleanup(self.manager.close)
        for p in (patch.object(webui, 'WORKSPACES', root / 'workspaces'), patch.object(webui, 'PASSWORD', ''),
                  patch.object(webui, 'DEPLOYMENT_MODE', 'local'), patch.object(webui, 'TASKS', self.manager, create=True),
                  patch.dict(webui.settings, {}, clear=True), patch.dict(webui.app.config, {'API_KEY': '', 'MOCK': False})):
            p.start(); self.addCleanup(p.stop)
        webui.custom_endpoints.clear(); self.addCleanup(webui.custom_endpoints.clear)
        webui.clients.clear()
        self.c = webui.app.test_client()

    def post(self, path, **kw):
        return self.c.post(path, base_url='http://localhost', **kw)

    def get(self, path):
        return self.c.get(path, base_url='http://localhost')

    def status(self):
        return self.get('/key_status').get_json()


class PresetTests(Base):
    def test_providers_endpoint_lists_presets(self):
        rows = self.get('/providers').get_json()
        ids = [r['id'] for r in rows]
        self.assertEqual(ids, ['gemini', 'nvidia', 'openrouter', 'groq', 'cerebras', 'mistral', 'deepseek', 'xai'])
        for r in rows:
            self.assertTrue(r['url'].startswith('https://'), r)
            self.assertIn(r['url'], webui.ALLOWED_API_URLS)
            self.assertEqual(set(r), {'id', 'label', 'url', 'model_hint'})

    def test_save_with_provider_id(self):
        r = self.post('/set_key', json={'key': FAKE_NV, 'provider': 'nvidia', 'model': 'some-model'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('NVIDIA NIM selected', r.get_data(as_text=True))
        self.assertNotIn(FAKE_NV, r.get_data(as_text=True))
        s = self.status()
        self.assertEqual((s['provider'], s['model']), ('NVIDIA NIM', 'some-model'))
        self.assertEqual(webui.settings['url_override'], NVIDIA)

    def test_save_with_preset_url_still_works(self):
        r = self.post('/set_key', json={'key': FAKE_NV, 'url': NVIDIA})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(webui.settings['url_override'], NVIDIA)

    def test_unknown_provider_rejected(self):
        r = self.post('/set_key', json={'key': FAKE_NV, 'provider': 'evil-corp'})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn('key_override', webui.settings)

    def test_provider_and_url_together_rejected(self):
        r = self.post('/set_key', json={'key': FAKE_NV, 'provider': 'nvidia', 'url': NVIDIA})
        self.assertEqual(r.status_code, 400)

    def test_unlisted_url_rejected(self):
        r = self.post('/set_key', json={'key': FAKE_NV, 'url': 'http://integrate.api.nvidia.com/v1/chat/completions'})
        self.assertEqual(r.status_code, 400)

    def test_model_hint_fills_blank_model(self):
        self.post('/set_key', json={'key': FAKE_GEM, 'provider': 'gemini'})
        self.assertEqual(webui.settings['model_override'], 'gemini-flash-latest')

    def test_change_provider_requires_new_key(self):
        self.post('/set_key', json={'key': FAKE_GEM})
        r = self.post('/set_key', json={'provider': 'nvidia'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('requires a new key', r.get_data(as_text=True))


class PrefixTests(Base):
    def test_nvidia_prefix_autodetects(self):
        r = self.post('/set_key', json={'key': FAKE_NV})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(webui.settings['url_override'], NVIDIA)
        self.assertEqual(self.status()['provider'], 'NVIDIA NIM')

    def test_openrouter_and_groq_prefixes(self):
        for key, url in ((FAKE_OR, OPENROUTER), (FAKE_GROQ, GROQ)):
            webui.settings.clear()
            r = self.post('/set_key', json={'key': key})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(webui.settings['url_override'], url)

    def test_gemini_prefix_still_autodetects(self):
        self.post('/set_key', json={'key': FAKE_GEM})
        self.assertEqual(webui.settings['url_override'], GEMINI)
        self.assertEqual(webui.settings['model_override'], 'gemini-flash-latest')

    def test_unknown_prefix_keeps_default_url(self):
        r = self.post('/set_key', json={'key': 'sk-something-else-entirely-0000'})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('url_override', webui.settings)


class CustomUrlTests(Base):
    def test_http_rejected(self):
        r = self.post('/set_key', json={'key': FAKE_NV, 'url': 'http://api.example-llm.test/v1'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('https', r.get_data(as_text=True))

    def test_private_and_loopback_rejected(self):
        bad = ['https://127.0.0.1:8080/v1', 'https://localhost/v1', 'https://10.0.0.5/v1',
               'https://192.168.1.1/v1', 'https://172.16.0.1/v1', 'https://[::1]/v1',
               'https://169.254.169.254/latest/meta-data', 'https://0.0.0.0/v1']
        for url in bad:
            r = self.post('/set_key', json={'key': FAKE_NV, 'url': url})
            self.assertEqual(r.status_code, 400, url)
            self.assertNotIn('url_override', webui.settings, url)

    def test_private_dns_resolution_rejected(self):
        with patch.object(webui.socket, 'getaddrinfo', return_value=fake_addrinfo(['10.1.2.3'])):
            r = self.post('/set_key', json={'key': FAKE_NV, 'url': CUSTOM})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn('url_override', webui.settings)

    def test_unresolvable_host_rejected(self):
        import socket as real_socket
        with patch.object(webui.socket, 'getaddrinfo', side_effect=real_socket.gaierror()):
            r = self.post('/set_key', json={'key': FAKE_NV, 'url': CUSTOM})
        self.assertEqual(r.status_code, 400)

    def test_userinfo_rejected(self):
        with patch.object(webui.socket, 'getaddrinfo', return_value=fake_addrinfo(['8.8.8.8'])):
            r = self.post('/set_key', json={'key': FAKE_NV, 'url': 'https://user:pass@api.example-llm.test/v1'})
        self.assertEqual(r.status_code, 400)

    def test_public_custom_url_accepted_and_used(self):
        with patch.object(webui.socket, 'getaddrinfo', return_value=fake_addrinfo(['8.8.8.8'])):
            r = self.post('/set_key', json={'key': FAKE_NV, 'url': CUSTOM, 'model': 'm1'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('custom URL', r.get_data(as_text=True))
        self.assertEqual(webui.settings['url_override'], CUSTOM)
        self.assertIn(CUSTOM, webui.custom_endpoints)
        self.assertTrue(webui.url_allowed(CUSTOM))
        # model_reply must accept the validated custom endpoint and refuse strangers
        cfg = webui.config_snapshot()
        self.assertEqual(cfg[1], CUSTOM)
        with self.assertRaises(ValueError):
            webui.model_reply([], ('k', 'https://never-validated.example/x', 'm'))

    def test_custom_change_requires_new_key(self):
        self.post('/set_key', json={'key': FAKE_GEM})
        with patch.object(webui.socket, 'getaddrinfo', return_value=fake_addrinfo(['8.8.8.8'])):
            r = self.post('/set_key', json={'url': CUSTOM})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn(CUSTOM, webui.custom_endpoints)


class LabelTests(Base):
    def test_key_status_labels(self):
        self.post('/set_key', json={'key': FAKE_NV, 'provider': 'nvidia'})
        self.assertEqual(self.status()['provider'], 'NVIDIA NIM')
        self.assertEqual(self.status()['provider_id'], 'nvidia')
        webui.settings.clear()
        self.post('/set_key', json={'key': FAKE_GEM})
        self.assertEqual(self.status()['provider'], 'Google Gemini')
        with patch.object(webui.socket, 'getaddrinfo', return_value=fake_addrinfo(['8.8.8.8'])):
            self.post('/set_key', json={'key': FAKE_NV, 'url': CUSTOM})
        self.assertEqual(self.status()['provider'], 'api.example-llm.test')
        self.assertEqual(self.status()['provider_id'], '')

    def test_provider_name_mapping(self):
        self.assertEqual(provider_name('https://integrate.api.nvidia.com/v1'), 'NVIDIA')
        self.assertEqual(provider_name('https://openrouter.ai/api/v1'), 'OpenRouter')
        self.assertEqual(provider_name('https://api.groq.com/openai/v1'), 'Groq')
        self.assertEqual(provider_name('https://weird-host.example'), 'The AI provider')

    def test_friendly_error_names_provider(self):
        class R:
            status_code = 503
            url = NVIDIA
        class E(Exception):
            response = R()
        self.assertTrue(friendly_provider_error(E()).startswith('NVIDIA is busy right now'))


if __name__ == '__main__':
    unittest.main()
