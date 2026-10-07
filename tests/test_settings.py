"""Configuration changes must retain loopback, origin and authentication guards."""
import json
from pathlib import Path
import tempfile
import unittest

from app import load_settings, OriginGuard
from oas_client import OasClient


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root / 'settings.json'

    def tearDown(self):
        self.temp.cleanup()

    def load(self, **updates):
        values = {'backend_root': str(self.root), **updates}
        self.file.write_text(json.dumps(values), encoding='utf-8')
        return load_settings(self.file)

    def test_local_defaults_and_explicit_authentication_toggle(self):
        values = self.load()
        self.assertEqual(values['host'], '127.0.0.1')
        self.assertEqual(values['port'], 22300)
        self.assertEqual(values['backend_url'], 'http://127.0.0.1:22289')
        self.assertEqual(values['public_origin'], 'http://127.0.0.1:22300')
        self.assertIs(values['authentication_required'], True)
        self.assertIs(self.load(authentication_required=False)['authentication_required'], False)
        with self.assertRaises(ValueError):
            self.load(authentication_required='false')

    def test_configured_local_backend_is_shared_by_http_and_websocket(self):
        for url, expected in [('http://localhost:23456/', 'localhost:23456'),
                              ('http://127.0.0.1:23456', '127.0.0.1:23456'),
                              ('http://[::1]:23456', '[::1]:23456')]:
            with self.subTest(url=url):
                values = self.load(backend_url=url)
                client = OasClient(values['backend_url'])
                self.assertEqual(client.base_url, 'http://' + expected)
                self.assertEqual(client.ws_base, 'ws://' + expected)

    def test_backend_rejects_remote_addresses_credentials_and_redirect_paths(self):
        for url in ['http://example.com:23456', 'https://127.0.0.1:23456',
                    'http://127.0.0.1:23456/path', 'http://user@localhost:23456',
                    'http://localhost:23456?query=1', 'http://localhost:80',
                    'http://localhost:23456#fragment', 'http://127.0.0.1:bad',
                    'http://127.0.0.1:23456 ', None]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.load(backend_url=url)

    def test_backend_root_must_be_explicit_existing_absolute_directory(self):
        for value in ['', 'relative/path', str(self.root / 'missing')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(backend_root=value)
        self.file.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            load_settings(self.file)

    def test_local_origin_follows_configured_port(self):
        self.assertEqual(self.load(port=23457)['public_origin'], 'http://127.0.0.1:23457')
        self.assertEqual(self.load(port=23457, public_origin='http://localhost:23457/')['public_origin'],
                         'http://localhost:23457')
        for value in ['http://example.com:23457', 'http://localhost:22300']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(port=23457, public_origin=value)

    def test_https_origin_is_configurable_and_canonical(self):
        self.assertEqual(self.load(public_origin='https://PANEL.EXAMPLE.COM:443/')['public_origin'],
                         'https://panel.example.com')
        self.assertEqual(self.load(public_origin='https://panel.example.com:4443')['public_origin'],
                         'https://panel.example.com:4443')
        for value in ['https://panel.example.com:0', 'https://panel.example.com:80',
                      'https://user:password@panel.example.com', 'https://panel.example.com/path',
                      'https://panel.example.com?query=1', 'https://panel.example.com#fragment',
                      'https://panel.example.com:invalid', 'https://panel.example.com ', None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(public_origin=value)

    def test_local_mode_has_no_public_proxy_exception(self):
        guard = OriginGuard(None, self.load())
        scope = {'type': 'http', 'method': 'POST', 'scheme': 'http',
                 'headers': [(b'host', b'127.0.0.1:22300'),
                             (b'origin', b'http://127.0.0.1:22300')],
                 'client': ('127.0.0.1', 12345)}
        self.assertEqual(guard.check(scope), (True, False))
        self.assertFalse(guard.public_hosts)
        for headers in [[(b'host', b'panel.example.com'), (b'origin', b'https://panel.example.com'),
                         (b'x-forwarded-proto', b'https')],
                        [(b'host', b'127.0.0.1:22300')],
                        [(b'host', b'127.0.0.1:22300'), (b'origin', b'https://other.example.com')]]:
            with self.subTest(headers=headers):
                self.assertEqual(guard.check({**scope, 'headers': headers}), (False, False))


if __name__ == '__main__':
    unittest.main()
