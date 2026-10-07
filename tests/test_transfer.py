"""Authenticated backup/import protocol checks against isolated fake services."""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tests.test_backend import FakeOas, request
from app import create_app
from auth import AuthStore
from oas_client import OasClient, UpstreamError, transfer_document, MAX_TRANSFER_BYTES


class TransferOas(FakeOas):
    def __init__(self):
        super().__init__()
        self.transfers = []
        self.names = ['oas1']
        self.current_state = 0
        self.supported = True

    async def accounts(self):
        return list(self.names)

    async def transfer_capabilities(self):
        if not self.supported:
            raise UpstreamError('尚未加载备份接口', 503)
        return {'version': 1, 'max_bytes': MAX_TRANSFER_BYTES,
                'modes': ['backup', 'share'], 'config_import': True, 'task_import': True}

    async def transfer_export(self, account, mode, task=None):
        await self.transfer_capabilities()
        self.transfers.append(('export', account, mode, task))
        return {'fixture': {'device': 'sample-device' if mode == 'backup' else '__OAS_REDACTED__'}}

    async def transfer_import(self, account, text, task=None):
        await self.transfer_capabilities()
        self.transfers.append(('import', account, json.loads(text), task))
        if task is None:
            self.names.append(account)
        return {'name': account, 'updated': task is not None, 'warnings': []}

    async def snapshot(self, name):
        return {'state': self.current_state, 'connected': self.current_state is not None, 'schedule': {}}


class TransferRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.auth = AuthStore(root / 'state')
        initial = (root / 'state' / 'initial-login.txt').read_text(encoding='utf-8').split('密码：', 1)[1].splitlines()[0]
        self.token, self.session = self.auth.authenticate('admin', initial)
        self.auth.change_password(self.session, initial, 'local-test-password')
        self.session = self.auth.resolve(self.token)
        self.client = TransferOas()
        self.app = create_app({'backend_root': str(root / 'isolated-backend'),
                               'backend_url': 'http://127.0.0.1:22289',
                               'public_origin': 'https://panel.example.com:4443', 'port': 22300},
                              client=self.client, auth=self.auth)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def call(self, method, path, data=None, **kwargs):
        return await request(self.app, method, path, data=data, token=self.token,
                             csrf=self.session.csrf, **kwargs)

    async def test_export_requires_login_and_import_requires_csrf(self):
        status, _, _ = await request(self.app, 'GET', '/api/accounts/oas1/export')
        self.assertEqual(status, 401)
        status, _, _ = await request(self.app, 'POST', '/api/config/import', token=self.token,
                                      data={'name': 'new', 'json_text': '{"fixture":{}}'})
        self.assertEqual(status, 403)
        self.assertFalse(self.client.transfers)

    async def test_modes_are_explicit_and_export_is_an_attachment(self):
        for mode, expected in [('backup', 'sample-device'), ('share', '__OAS_REDACTED__')]:
            status, data, headers = await self.call('GET', '/api/accounts/oas1/export', query={'mode': mode})
            self.assertEqual(status, 200)
            self.assertEqual(data['fixture']['device'], expected)
            self.assertIn('attachment;', dict(headers)[b'content-disposition'].decode())
            self.assertEqual(self.client.transfers[-1], ('export', 'oas1', mode, None))
        status, _, _ = await self.call('GET', '/api/accounts/oas1/export', query={'mode': 'invalid'})
        self.assertEqual(status, 422)
        self.assertFalse(self.client.actions)

    async def test_import_never_overwrites_existing_and_keeps_new_name(self):
        for name, expected in [('oas1', 409), ('../escape', 422), ('template', 422), ('新备份', 200)]:
            status, data, _ = await self.call('POST', '/api/config/import', {'name': name, 'json_text': '\ufeff{"fixture":{}}'})
            self.assertEqual(status, expected)
        self.assertEqual(self.client.transfers, [('import', '新备份', {'fixture': {}}, None)])
        self.assertFalse(self.client.actions)

    async def test_task_import_requires_stopped_configuration(self):
        for value in (1, 2, 3, None):
            self.client.current_state = value
            status, _, _ = await self.call('POST', '/api/accounts/oas1/tasks/Chess/import', {'json_text': '{"chess":{}}'})
            self.assertEqual(status, 409)
        self.assertFalse(self.client.transfers)
        self.client.current_state = 0
        status, _, _ = await self.call('POST', '/api/accounts/oas1/tasks/Chess/import', {'json_text': '{"chess":{}}'})
        self.assertEqual(status, 200)
        self.assertEqual(self.client.transfers[-1], ('import', 'oas1', {'chess': {}}, 'Chess'))
        self.assertFalse(self.client.actions)

    async def test_invalid_large_or_non_object_json_never_reaches_backend(self):
        for text in ('{invalid', '[]', 'null', '{}', '{"a":NaN}', '{"a":1}' + ' ' * MAX_TRANSFER_BYTES):
            status, _, _ = await self.call('POST', '/api/config/import', {'name': 'new', 'json_text': text})
            self.assertEqual(status, 422)
        self.assertFalse(self.client.transfers)

    async def test_capability_failure_is_clear_and_no_import_occurs(self):
        self.client.supported = False
        status, data, _ = await self.call('GET', '/api/transfer/capabilities')
        self.assertEqual(status, 503)
        self.assertIn('备份接口', data['detail'])
        status, _, _ = await self.call('POST', '/api/config/import', {'name': 'new', 'json_text': '{"fixture":{}}'})
        self.assertEqual(status, 503)
        self.assertFalse(self.client.transfers)


class TransferAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_field_errors_keep_message_and_paths(self):
        client = OasClient('http://127.0.0.1:22289')
        class Opener:
            def open(self, request, timeout):
                detail = {'detail': {'message': '参数范围无效', 'fields': ['chess.run_count']}}
                raise urllib.error.HTTPError(request.full_url, 400, 'invalid', {},
                                             io.BytesIO(json.dumps(detail).encode()))
        client.opener = Opener()
        with self.assertRaises(UpstreamError) as error:
            await client.transfer_capabilities()
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn('参数范围无效', str(error.exception))
        self.assertIn('chess.run_count', str(error.exception))

    async def test_utf8_multipart_is_bound_to_local_service_and_preserves_data(self):
        client = OasClient('http://127.0.0.1:22289')
        calls = []
        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                result = {'version': 1, 'modes': ['backup', 'share'], 'config_import': True, 'task_import': True}
                if len(calls) > 1:
                    result = {'name': '测试'}
                return io.BytesIO(json.dumps(result).encode())
        client.opener = Opener()
        await client.transfer_import('测试', '{"fixture":{"text":"中文"}}')
        req = calls[-1]
        self.assertEqual(req.full_url, 'http://127.0.0.1:22289/config/import')
        self.assertEqual(req.method, 'POST')
        self.assertIn('multipart/form-data; boundary=', req.headers['Content-type'])
        self.assertIn(b'name="file"; filename="config.json"', req.data)
        self.assertIn('中文'.encode(), req.data)
        calls.clear()
        await client.transfer_import('测试', '{"chess":{}}', 'Chess')
        req = calls[-1]
        self.assertTrue(req.full_url.endswith('/config/task/import'))
        self.assertIn(b'name="config_name"', req.data)
        self.assertIn(b'name="task_name"', req.data)
        self.assertIn(b'name="json_text"', req.data)

    async def test_missing_route_does_not_silently_export_a_share_as_backup(self):
        client = OasClient('http://127.0.0.1:22289')
        class Opener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 404, 'missing', {}, io.BytesIO(b'{}'))
        client.opener = Opener()
        with self.assertRaises(UpstreamError) as error:
            await client.transfer_export('oas1', 'backup')
        self.assertEqual(error.exception.status_code, 503)


if __name__ == '__main__':
    unittest.main()
