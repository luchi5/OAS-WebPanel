"""Anonymous panel access tests with a fake backend; never controls real OAS."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from app import create_app
from auth import AuthStore
from tests.test_backend import FakeOas, request


class NoLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = AuthStore(root / 'state')
        self.client = FakeOas()
        self.settings = {'backend_url': 'http://127.0.0.1:22289', 'backend_root': str(root / 'backend'),
                         'public_origin': 'https://panel.example.com:4443', 'host': '127.0.0.1',
                         'port': 22300, 'authentication_required': False}
        self.app = create_app(self.settings, client=self.client, auth=self.store)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_session_and_bootstrap_open_without_cookie_or_login(self):
        status, session, _ = await request(self.app, 'GET', '/api/session')
        self.assertEqual(status, 200)
        self.assertTrue(session['authenticated'])
        self.assertFalse(session['login_required'])
        self.assertFalse(session['must_change_password'])
        self.assertTrue(session['csrf'])
        status, data, _ = await request(self.app, 'GET', '/api/bootstrap', token='old-or-forged-cookie')
        self.assertEqual(status, 200)
        self.assertFalse(data['login_required'])
        self.assertEqual(data['accounts'], ['oas1'])

    async def test_writes_use_csrf_but_do_not_need_account_cookie(self):
        status, _, _ = await request(self.app, 'POST', '/api/accounts/oas1/actions', data={'action': 'start'})
        self.assertEqual(status, 403)
        self.assertEqual(self.client.actions, [])
        _, session, _ = await request(self.app, 'GET', '/api/session')
        status, data, _ = await request(self.app, 'POST', '/api/accounts/oas1/actions',
                                      data={'action': 'start'}, csrf=session['csrf'])
        self.assertEqual(status, 200)
        self.assertEqual(self.client.actions, [('oas1', 'start')])

    async def test_setting_save_still_checks_current_value(self):
        _, session, _ = await request(self.app, 'GET', '/api/session')
        route = '/api/accounts/oas1/settings/Chess/chess_config/run_count'
        status, _, _ = await request(self.app, 'PUT', route,
                                    data={'value': 2, 'expected_value': 0}, csrf=session['csrf'])
        self.assertEqual(status, 409)
        self.assertEqual(self.client.writes, [])
        status, result, _ = await request(self.app, 'PUT', route,
                                         data={'value': 2, 'expected_value': 1}, csrf=session['csrf'])
        self.assertEqual(status, 200)
        self.assertEqual(result['value'], 2)

    async def test_login_password_and_logout_are_disabled(self):
        for route in ('/api/login', '/api/password', '/api/logout'):
            with self.subTest(route=route):
                status, _, _ = await request(self.app, 'POST', route, data={})
                self.assertEqual(status, 404)

    async def test_public_origin_can_open_and_foreign_origin_is_rejected(self):
        status, result, _ = await request(self.app, 'GET', '/api/bootstrap',
                                         host='panel.example.com:4443', origin='https://panel.example.com:4443',
                                         extra_headers=[('x-forwarded-proto', 'https')])
        self.assertEqual(status, 200)
        self.assertFalse(result['login_required'])
        status, _, _ = await request(self.app, 'GET', '/api/bootstrap', origin='https://unrelated.example')
        self.assertEqual(status, 403)

    async def test_websocket_events_work_without_login_cookie(self):
        incoming = asyncio.Queue()
        incoming.put_nowait({'type': 'websocket.connect'})
        sent = []
        scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'ws',
                 'path': '/api/accounts/oas1/events', 'raw_path': b'/api/accounts/oas1/events',
                 'query_string': b'', 'root_path': '', 'headers': [(b'host', b'127.0.0.1:22300'),
                 (b'origin', b'http://127.0.0.1:22300')], 'client': ('127.0.0.1', 12000),
                 'server': ('127.0.0.1', 22300), 'subprotocols': []}
        async def send(message):
            sent.append(message)
        task = asyncio.create_task(self.app(scope, incoming.get, send))
        try:
            for _ in range(100):
                if any(item['type'] == 'websocket.send' and 'connection' in item.get('text', '') for item in sent):
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(any(item['type'] == 'websocket.accept' for item in sent))
            self.assertTrue(any(item['type'] == 'websocket.send' and
                                json.loads(item['text']).get('type') == 'connection' for item in sent))
            incoming.put_nowait({'type': 'websocket.disconnect', 'code': 1000})
            await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def test_invalid_auth_mode_is_not_silently_interpreted(self):
        with self.assertRaises(ValueError):
            create_app({**self.settings, 'authentication_required': 'false'}, client=self.client, auth=self.store)


if __name__ == '__main__':
    unittest.main()
