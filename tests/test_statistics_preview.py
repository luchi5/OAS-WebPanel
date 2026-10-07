"""Authenticated ASGI fixture checks. No HTTP server or real OAS is started."""
from __future__ import annotations

import sys
import secrets
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from auth import AuthStore, password_hash
from tests.test_backend import FakeOas, request


class FixtureOas(FakeOas):
    """Synthetic statistics fixture; independent of local preview tools."""
    def __init__(self):
        super().__init__()
        self.events = []
        self.streams = []

    async def accounts(self):
        return ['演示一', '演示二']

    async def statistics_dates(self, account):
        return {'script_name': account,
                'dates': ['2026-10-05', '2026-10-04'] if account == '演示一' else []}

    async def statistics_day(self, account, day):
        return {'script_name': account, 'date': day,
                'available_metrics': ['total_task_run_count'],
                'tasks': {'Chess': {'run_count': 1, 'total_duration_seconds': None,
                                    'runs': [{'status': 'incomplete', 'duration_seconds': None}]}}}


class StatisticsPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        auth = AuthStore(root / 'state')
        password = secrets.token_urlsafe(24)
        with auth._db() as database:
            database.execute('UPDATE users SET password_hash=?,must_change=0 WHERE username=?',
                             (password_hash(password), 'admin'))
        self.token, _ = auth.authenticate('admin', password)
        self.fixture = FixtureOas()
        settings = {'host': '127.0.0.1', 'port': 22300, 'public_origin': 'https://panel.example.com',
                    'backend_url': 'http://127.0.0.1:22289', 'backend_root': str(root / 'synthetic'),
                    'state_dir': str(root / 'state')}
        self.app = create_app(settings, client=self.fixture, auth=auth)

    async def asyncTearDown(self):
        self.assertEqual(self.fixture.events, [])
        self.assertEqual(self.fixture.streams, [])
        self.temporary.cleanup()

    async def test_dates_day_and_empty_account_use_only_synthetic_documents(self):
        status, dates, _ = await request(self.app, 'GET', '/api/accounts/演示一/statistics/dates', token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(len(dates['dates']), 2)
        status, day, _ = await request(self.app, 'GET', '/api/accounts/演示一/statistics',
                                       token=self.token, query={'date': dates['dates'][0]})
        self.assertEqual(status, 200)
        self.assertIsNone(day['tasks']['Chess']['total_duration_seconds'])
        self.assertEqual(day['tasks']['Chess']['runs'][0]['status'], 'incomplete')
        self.assertNotIn('total_runtime_seconds', day['available_metrics'])
        status, empty, _ = await request(self.app, 'GET', '/api/accounts/演示二/statistics/dates', token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(empty['dates'], [])


if __name__ == '__main__':
    unittest.main()
