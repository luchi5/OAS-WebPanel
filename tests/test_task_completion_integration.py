"""Completion evidence through the existing reader/HTTP path, without OAS actions."""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import unittest

from app import create_app
from auth import AuthStore
from tests.test_backend import FakeOas, request
from tests.test_daily_feedback import FeedbackFixture, DAY, ACCOUNT


class CompletionIntegrationTests(FeedbackFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()
        self.report()

    def tearDown(self):
        self.temp.cleanup()

    def save_proof(self, *, day=DAY, outcome='completed', detail='final_challenge_confirmed'):
        directory = self.root / 'config' / 'daily_closeout'
        directory.mkdir(exist_ok=True)
        path = directory / (ACCOUNT + '.json')
        state = {'date': day, 'outcomes': {'Dokan': {
            'date': day, 'at': day + 'T21:00:00', 'outcome': outcome,
            'detail': detail, 'private_value': 'PROOF-SECRET'}}}
        path.write_text(json.dumps(state), encoding='utf-8')
        return path

    def row(self):
        return self.reader.account_day(ACCOUNT, DAY)

    def started_task(self, task, day=DAY):
        directory = self.root / 'log'
        directory.mkdir(exist_ok=True)
        (directory / (day + '_' + ACCOUNT + '.txt')).write_text(
            f'{day} 20:00:00 | script.py:1 | INFO | Scheduler: Start task `{task}`\n',
            encoding='utf-8')

    def test_reads_independent_proof_and_only_enabled_other_tasks(self):
        path = self.root / 'config' / (ACCOUNT + '.json')
        config = json.loads(path.read_text(encoding='utf-8'))
        config['daily_trifles'] = {'scheduler': {'enable': True, 'next_run': '2099-01-01T00:00:00'}}
        path.write_text(json.dumps(config), encoding='utf-8')
        # A future next_run alone is not today's plan. A real start today keeps
        # this task in today's execution scope even after it is rescheduled.
        self.started_task('DailyTrifles')
        self.save_proof()
        labels = self.root / 'assets' / 'i18n'
        labels.mkdir(parents=True)
        (labels / 'zh-CN.json').write_text(json.dumps({'DailyTrifles': '每日琐事'}), encoding='utf-8')
        row = self.row()
        tasks = {item['task']: item for item in row['task_completion']['tasks']}
        self.assertEqual(set(tasks), {'Dokan', 'DailyTrifles'})
        self.assertEqual(tasks['Dokan']['status_label'], '已完成')
        self.assertEqual(tasks['DailyTrifles']['status_label'], '待核验')
        self.assertEqual(tasks['DailyTrifles']['label'], '每日琐事')
        self.assertEqual(row['task_completion']['counts'], {'completed': 1, 'incomplete': 0, 'unverified': 1})
        self.assertEqual(set(row['evidence']), {'talisman', 'collective'})
        self.assertTrue(row['evidence']['collective']['complete'])
        self.assertNotIn('PROOF-SECRET', json.dumps(row))
        self.assertNotIn('DO-NOT-EXPOSE', json.dumps(row))

    def test_future_enabled_task_without_today_start_is_excluded(self):
        path = self.root / 'config' / (ACCOUNT + '.json')
        config = json.loads(path.read_text(encoding='utf-8'))
        config['daily_trifles'] = {'scheduler': {'enable': True, 'next_run': '2099-01-01T00:00:00'}}
        path.write_text(json.dumps(config), encoding='utf-8')
        self.save_proof()
        row = self.row()
        self.assertEqual([item['task'] for item in row['task_completion']['tasks']], ['Dokan'])
        self.assertEqual(row['task_completion']['counts'], {'completed': 1, 'incomplete': 0, 'unverified': 0})
        self.assertTrue(row['evidence']['collective']['complete'])

    def test_report_success_does_not_make_other_tasks_complete(self):
        row = self.row()
        self.assertEqual(row['status'], 'verified')
        self.assertEqual(row['task_completion']['tasks'][0]['status'], 'unverified')

    def test_completed_proof_is_preserved_despite_earlier_exit_error_summary(self):
        self.save_proof()
        directory, report = self.report()
        report['closeout']['completed'] = ['CollectiveMissions']
        report['closeout']['incomplete'] = [{'task': 'Dokan', 'outcome': 'failed'}]
        self.write_report(directory, report)
        self.assertEqual(self.row()['task_completion']['tasks'][0]['status'], 'completed')

    def test_damaged_stale_and_ocr_unknown_proofs_do_not_become_success_or_failure(self):
        path = self.save_proof(day='2026-10-06')
        self.assertEqual(self.row()['task_completion']['tasks'][0]['status'], 'unverified')
        path.write_text('{damaged', encoding='utf-8')
        row = self.row()
        self.assertEqual(row['task_completion']['tasks'][0]['status'], 'unverified')
        self.assertTrue(row['evidence']['talisman']['complete'])
        self.save_proof(outcome='failed', detail='submission_not_confirmed_30_of_30')
        self.assertEqual(self.row()['task_completion']['tasks'][0]['status'], 'unverified')

    def test_configuration_changes_are_read_without_task_or_device_control(self):
        self.save_proof()
        path = self.root / 'config' / (ACCOUNT + '.json')
        config = json.loads(path.read_text(encoding='utf-8'))
        config['dokan']['scheduler']['enable'] = False
        path.write_text(json.dumps(config), encoding='utf-8')
        self.assertEqual(self.row()['task_completion']['tasks'], [])
        config['dokan']['scheduler']['enable'] = True
        path.write_text(json.dumps(config), encoding='utf-8')
        self.assertEqual(self.row()['task_completion']['tasks'][0]['status'], 'completed')

    def test_history_does_not_use_current_enabled_task_scope(self):
        self.save_proof()
        self.reader.now = lambda: datetime(2026, 10, 8, 22, tzinfo=timezone(timedelta(hours=8)))
        row = self.row()
        # An empty recovered execution scope is available as an empty view; it
        # must not borrow today's enabled Dokan entry or infer a full plan.
        self.assertTrue(row['task_completion']['available'])
        self.assertEqual(row['task_completion']['scope'], 'historical_executed')
        self.assertEqual(row['task_completion']['tasks'], [])
        self.assertEqual(row['task_completion']['counts'], {'completed': 0, 'incomplete': 0, 'unverified': 0})
        self.assertTrue(row['evidence']['collective']['complete'])

    def test_history_restores_started_task_even_when_current_enabled_scope_differs(self):
        self.save_proof()
        self.started_task('DailyTrifles')
        path = self.root / 'config' / (ACCOUNT + '.json')
        config = json.loads(path.read_text(encoding='utf-8'))
        config['daily_trifles'] = {'scheduler': {'enable': False}}
        self.assertTrue(config['dokan']['scheduler']['enable'])
        path.write_text(json.dumps(config), encoding='utf-8')
        self.reader.now = lambda: datetime(2026, 10, 8, 22, tzinfo=timezone(timedelta(hours=8)))
        row = self.row()
        completion = row['task_completion']
        self.assertTrue(completion['available'])
        self.assertEqual(completion['scope'], 'historical_executed')
        self.assertEqual([item['task'] for item in completion['tasks']], ['DailyTrifles'])
        self.assertEqual(completion['tasks'][0]['status'], 'unverified')
        self.assertEqual(completion['counts'], {'completed': 0, 'incomplete': 0, 'unverified': 1})
        self.assertTrue(row['evidence']['collective']['complete'])


class CompletionApiTests(FeedbackFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_fixture()
        self.oas = FakeOas()
        settings = {'backend_url': 'http://127.0.0.1:22289', 'backend_root': str(self.root),
                    'public_origin': 'https://panel.example.com:4443', 'host': '127.0.0.1',
                    'port': 22300, 'authentication_required': False}
        self.app = create_app(settings, client=self.oas, auth=AuthStore(self.root / 'state'))

    async def asyncTearDown(self):
        self.assertEqual(self.oas.actions, [])
        self.assertEqual(self.oas.writes, [])
        self.temp.cleanup()

    async def test_day_and_account_routes_include_same_conservative_completion_view(self):
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        status, row, _ = await request(self.app, 'GET', '/api/daily-feedback/' + ACCOUNT, query={'date': day})
        self.assertEqual(status, 200)
        self.assertTrue(row['task_completion']['available'])
        self.assertEqual(row['task_completion']['tasks'][0]['status'], 'unverified')
        status, report, _ = await request(self.app, 'GET', '/api/daily-feedback', query={'date': day})
        self.assertEqual(status, 200)
        self.assertEqual(report['accounts'][0]['task_completion'], row['task_completion'])


if __name__ == '__main__':
    unittest.main()
