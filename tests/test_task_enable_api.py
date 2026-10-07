"""Task enable saves do not send account start/stop or change its clock."""
import copy
from pathlib import Path
import tempfile
import unittest

from app import create_app
from auth import AuthStore
from tests.test_backend import FakeOas, request


class TaskClient(FakeOas):
    def __init__(self):
        super().__init__()
        self.values = {'scheduler': [
            {'name': 'enable', 'type': 'boolean', 'value': True},
            {'name': 'next_run', 'type': 'date_time', 'value': '2026-10-07 17:45:00'},
        ]}

    async def menu(self):
        return {'Activity Task': ['FrogBoss']}

    async def settings(self, account, task):
        assert account == 'oas1' and task == 'FrogBoss'
        return copy.deepcopy(self.values)


class TaskEnableApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.client = TaskClient()
        self.app = create_app({'backend_url': 'http://127.0.0.1:22289', 'backend_root': str(root / 'backend'),
                               'public_origin': 'https://panel.example.com:4443', 'port': 22300,
                               'authentication_required': False}, client=self.client, auth=AuthStore(root / 'state'))
        _, self.session, _ = await request(self.app, 'GET', '/api/session')

    async def asyncTearDown(self):
        self.assertEqual(self.client.actions, [])
        self.temp.cleanup()

    async def put(self, value, expected, csrf=True):
        return await request(self.app, 'PUT', '/api/accounts/oas1/settings/FrogBoss/scheduler/enable',
                             data={'value': value, 'expected_value': expected},
                             csrf=self.session['csrf'] if csrf else None)

    async def test_disable_and_reenable_confirm_boolean_without_actions_or_time_changes(self):
        status, result, _ = await self.put(False, True)
        self.assertEqual((status, result), (200, {'ok': True, 'value': False}))
        status, fields, _ = await request(self.app, 'GET', '/api/accounts/oas1/settings/FrogBoss')
        self.assertIs(fields['scheduler'][0]['value'], False)
        self.assertEqual(fields['scheduler'][1]['value'], '2026-10-07 17:45:00')
        status, result, _ = await self.put(True, False)
        self.assertEqual((status, result), (200, {'ok': True, 'value': True}))
        self.assertEqual([x[3] for x in self.client.writes], ['enable', 'enable'])

    async def test_conflict_does_not_write_anything(self):
        status, _, _ = await self.put(False, False)
        self.assertEqual(status, 409)
        self.assertEqual(self.client.writes, [])

    async def test_csrf_and_boolean_type_remain_required(self):
        status, _, _ = await self.put(False, True, csrf=False)
        self.assertEqual(status, 403)
        status, _, _ = await self.put('false', True)
        self.assertEqual(status, 422)
        self.assertEqual(self.client.writes, [])


if __name__ == '__main__':
    unittest.main()
