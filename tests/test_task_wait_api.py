"""Offline quick-wait API checks; fake backend only."""
from unittest.mock import AsyncMock
from tests.test_task_enable_api import TaskEnableApiTests
from tests.test_backend import request
from oas_client import OasClient, UpstreamError

class TaskWaitTests(TaskEnableApiTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async def wait(account, task, *, expected_value):
            self.assertEqual(expected_value, '2026-10-07 17:45:00')
            self.client.values['scheduler'][1]['value'] = '2026-10-13 09:00:00'
        self.client.wait_task = AsyncMock(side_effect=wait)
    async def wait(self, expected='2026-10-07 17:45:00', csrf=True):
        return await request(self.app, 'POST', '/api/accounts/oas1/tasks/FrogBoss/wait',
            data={'expected_value': expected}, csrf=self.session['csrf'] if csrf else None)
    async def test_wait_uses_dedicated_command_and_returns_actual_schedule(self):
        status, result, _ = await self.wait()
        self.assertEqual((status, result), (200, {'ok': True, 'value': '2026-10-13 09:00:00'}))
        self.client.wait_task.assert_awaited_once_with('oas1', 'FrogBoss', expected_value='2026-10-07 17:45:00')
        self.assertEqual(self.client.writes, [])
        self.assertTrue(self.client.values['scheduler'][0]['value'])
    async def test_wait_conflict_and_csrf_do_not_call_backend(self):
        self.assertEqual((await self.wait('stale'))[0], 409)
        self.assertEqual((await self.wait(csrf=False))[0], 403)
        self.client.wait_task.assert_not_awaited()
    async def test_readonly_does_not_call_backend(self):
        self.client.values['scheduler'][1]['readOnly'] = True
        self.assertEqual((await self.wait())[0], 403)
        self.client.wait_task.assert_not_awaited()
    async def test_client_matches_oasx_wire_command(self):
        client = object.__new__(OasClient)
        client._json = AsyncMock(return_value=True)
        await client.wait_task('oas1', 'FrogBoss')
        client._json.assert_awaited_once_with('/oas1/FrogBoss/sync_next_run', method='PUT', query={'target_dt': ''})
        client._json.return_value = False
        with self.assertRaises(UpstreamError):
            await client.wait_task('oas1', 'FrogBoss')
