"""Expected-value transport and response tests using only fake upstreams."""
import io
import json
import unittest
from unittest.mock import AsyncMock, Mock
from urllib.error import HTTPError

from oas_client import OasClient, UpstreamError
from tests.test_task_enable_api import TaskEnableApiTests
from tests.test_backend import request


class AtomicSettingApiTests(TaskEnableApiTests):
    async def test_write_racing_after_initial_read_returns_conflict_without_retry(self):
        async def conflict(account, task, group, argument, value, kind, *, expected_value):
            self.assertIs(expected_value, True)
            self.client.values['scheduler'][0]['value'] = False
            raise UpstreamError('changed at backend commit', 409)
        self.client.set_value = AsyncMock(side_effect=conflict)
        status, result, _ = await self.put(False, True)
        self.assertEqual(status, 409)
        self.assertEqual(result['detail'], 'changed at backend commit')
        self.client.set_value.assert_awaited_once()

    async def test_wait_racing_after_read_preserves_authoritative_409(self):
        self.client.wait_task = AsyncMock(side_effect=UpstreamError('schedule changed', 409))
        status, result, _ = await request(
            self.app, 'POST', '/api/accounts/oas1/tasks/FrogBoss/wait',
            data={'expected_value': '2026-10-07 17:45:00'}, csrf=self.session['csrf'])
        self.assertEqual((status, result['detail']), (409, 'schedule changed'))
        self.client.wait_task.assert_awaited_once_with(
            'oas1', 'FrogBoss', expected_value='2026-10-07 17:45:00')


class AtomicAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_expected_values_keep_json_types(self):
        client = object.__new__(OasClient)
        client._json = AsyncMock(return_value=True)
        for expected in (False, 0, 1.0, ['a', 'b'], '中文', None):
            await client.set_value('oas1', 'Chess', 'scheduler', 'enable', False,
                                   'boolean', expected_value=expected)
            query = client._json.await_args.kwargs['query']
            self.assertEqual(json.loads(query['expected_value']), expected)
        await client.wait_task('oas1', 'Chess', expected_value='2026-10-13 09:00:00')
        self.assertEqual(client._json.await_args.kwargs['query']['expected_value'],
                         '"2026-10-13 09:00:00"')

    async def test_legacy_call_keeps_existing_wire_shape(self):
        client = object.__new__(OasClient)
        client._json = AsyncMock(return_value=True)
        await client.set_value('oas1', 'Chess', 'scheduler', 'enable', False, 'boolean')
        self.assertNotIn('expected_value', client._json.await_args.kwargs['query'])

    async def test_http_conflict_keeps_status_and_does_not_retry(self):
        client = OasClient('http://127.0.0.1:22289')
        client.opener = Mock()
        client.opener.open.side_effect = HTTPError(
            'http://fake.invalid', 409, 'conflict', {}, io.BytesIO(b'{}'))
        with self.assertRaises(UpstreamError) as error:
            await client.set_value('oas1', 'Chess', 'scheduler', 'enable', False,
                                   'boolean', expected_value=True)
        self.assertEqual(error.exception.status_code, 409)
        client.opener.open.assert_called_once()


if __name__ == '__main__':
    unittest.main()
