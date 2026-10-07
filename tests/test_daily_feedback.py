"""Offline evidence/HTTP checks. No backend, account or emulator is controlled."""
from __future__ import annotations
import base64
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from app import create_app
from auth import AuthStore
from daily_feedback_reader import DailyFeedbackReader, FeedbackNotFound, MAX_IMAGE, MAX_JSON
from tests.test_backend import FakeOas, request

DAY = '2026-10-07'
AT = DAY + 'T21:10:00'
ACCOUNT = '05-测试'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aAvsAAAAASUVORK5CYII=')


class FeedbackFixture:
    def setup_fixture(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        self.reader = DailyFeedbackReader(self.root, now=lambda: datetime(2026, 10, 7, 22, tzinfo=timezone(timedelta(hours=8))))
        self.config(ACCOUNT)

    def config(self, account, *, talisman=True, collective=True, mon_thu=True):
        value = {'talisman_pass': {'scheduler': {'enable': talisman}, 'closeout_config': {'enable': True}},
                 'collective_missions': {'scheduler': {'enable': collective}, 'missions_config': {'monday_to_thursday': mon_thu, 'run_after_dokan': True}},
                 'dokan': {'scheduler': {'enable': collective}},
                 'restart': {'push_config': {'bark_key': 'DO-NOT-EXPOSE', 'pushplus_token': 'ALSO-PRIVATE'}},
                 'global_game': {'device': {'serial': 'PRIVATE-DEVICE'}}}
        (self.root / 'config' / (account + '.json')).write_text(json.dumps(value), encoding='utf-8')
        return value

    def report(self, account=ACCOUNT, day=DAY, *, count=30, phase='final'):
        directory = self.root / 'config' / 'daily_feedback' / day / self.reader.account_hash(account)
        directory.mkdir(parents=True, exist_ok=True)
        for kind in ('talisman', 'collective'):
            (directory / (kind + '-20261007-abcdef.png')).write_bytes(PNG)
        result = {'schema_version': 1, 'date': day, 'account': account, 'updated_at': day + 'T21:10:00',
                  'evidence': {kind: {'status': 'captured', 'captured_at': day + 'T21:10:00',
                                     'image': kind + '-20261007-abcdef.png', 'current': 100 if kind == 'talisman' else count,
                                     'total': None if kind == 'talisman' else 30,
                                     'target': 100 if kind == 'talisman' else None,
                                     'verified': True, 'final': phase == 'final', 'detail': '游戏画面已读取'}
                               for kind in ('talisman', 'collective')},
                  'closeout': {'phase': phase, 'at': day + 'T21:10:00', 'required': ['Dokan', 'CollectiveMissions'],
                               'waiting': [], 'incomplete': [], 'completed': ['Dokan', 'CollectiveMissions']},
                  'notification': {'status': 'accepted', 'at': day + 'T21:10:01'}}
        self.write_report(directory, result)
        return directory, result

    def write_report(self, directory, result):
        (directory / 'report.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


class ReaderTests(FeedbackFixture, unittest.TestCase):
    def setUp(self): self.setup_fixture()
    def tearDown(self): self.temp.cleanup()

    def test_no_evidence_never_reports_zero_or_completed(self):
        row = self.reader.account_day(ACCOUNT, DAY)
        self.assertEqual(row['status'], 'pending')
        for item in row['evidence'].values():
            self.assertIsNone(item['current'])
            self.assertFalse(item['verified'])
        self.assertEqual(row['closeout']['waiting'], ['Dokan', 'CollectiveMissions'])

    def test_verified_requires_real_same_day_images_and_final_closeout(self):
        self.report()
        row = self.reader.account_day(ACCOUNT, DAY)
        self.assertEqual(row['status'], 'verified')
        self.assertEqual(row['evidence']['collective']['current'], 30)
        self.assertEqual(row['evidence']['talisman']['current'], 100)
        self.assertEqual(self.reader.image(ACCOUNT, DAY, row['evidence']['talisman']['image']), PNG)

    def test_partial_count_not_treated_as_success(self):
        self.report(count=25)
        self.assertEqual(self.reader.account_day(ACCOUNT, DAY)['status'], 'attention')

    def test_fresh_picture_resolves_missing_pre_activation_collective_event(self):
        directory, report = self.report()
        report['closeout']['completed'] = [{'task': 'Dokan', 'label': '道馆'}]
        report['closeout']['waiting'] = ['CollectiveMissions']
        self.write_report(directory, report)
        row = self.reader.account_day(ACCOUNT, DAY)
        self.assertEqual(row['closeout']['completed'], ['Dokan', 'CollectiveMissions'])
        self.assertEqual(row['closeout']['waiting'], [])
        self.assertEqual(row['status'], 'verified')

    def test_phase_capture_remains_running_even_with_full_counts(self):
        self.report(phase='provisional')
        self.assertEqual(self.reader.account_day(ACCOUNT, DAY)['status'], 'running')

    def test_talisman_below_explicit_target_needs_attention_but_no_target_not_guessed(self):
        directory, report = self.report()
        report['evidence']['talisman']['current'] = 80
        self.write_report(directory, report)
        row = self.reader.account_day(ACCOUNT, DAY)
        self.assertEqual(row['status'], 'attention')
        self.assertEqual(row['evidence']['talisman']['current'], 80)
        report['evidence']['talisman']['target'] = None
        self.write_report(directory, report)
        row = self.reader.account_day(ACCOUNT, DAY)
        self.assertEqual(row['status'], 'verified')
        self.assertIn('目标未核验', row['reason'])

    def test_talisman_experience_can_exceed_target_and_old_total_is_not_a_denominator(self):
        directory, report = self.report()
        report['evidence']['talisman']['current'] = 113
        report['evidence']['talisman']['target'] = 100
        report['evidence']['talisman']['total'] = 100
        self.write_report(directory, report)
        row = self.reader.account_day(ACCOUNT, DAY)
        self.assertEqual(row['status'], 'verified')
        self.assertEqual(row['evidence']['talisman']['current'], 113)
        self.assertEqual(row['evidence']['talisman']['target'], 100)
        self.assertIsNone(row['evidence']['talisman']['total'])
        self.assertTrue(row['evidence']['talisman']['complete'])

    def test_target_is_only_returned_for_verified_numbers_and_valid_integer_range(self):
        for invalid in (True, False, 0, -1, 301, '100', [], {}, None):
            directory, report = self.report()
            report['evidence']['talisman']['target'] = invalid
            self.write_report(directory, report)
            self.assertIsNone(self.reader.account_day(ACCOUNT, DAY)['evidence']['talisman']['target'])
        directory, report = self.report()
        report['evidence']['talisman']['verified'] = False
        self.write_report(directory, report)
        self.assertIsNone(self.reader.account_day(ACCOUNT, DAY)['evidence']['talisman']['target'])

    def test_dokan_linked_collective_only_expected_monday_through_thursday(self):
        self.config(ACCOUNT, mon_thu=False)
        self.assertFalse(self.reader.account_day(ACCOUNT, '2026-10-10')['evidence']['collective']['expected'])
        self.assertTrue(self.reader.account_day(ACCOUNT, DAY)['evidence']['collective']['expected'])

    def test_final_report_cannot_promote_daytime_capture_after_failed_retake(self):
        directory, report = self.report()
        report['evidence']['talisman']['final'] = False
        report['evidence']['talisman']['capture_error'] = '未能进入花合战页面'
        report['evidence']['talisman']['last_attempt_at'] = DAY + 'T22:00:00'
        self.write_report(directory, report)
        row = self.reader.account_day(ACCOUNT, DAY)
        self.assertEqual(row['status'], 'attention')
        self.assertEqual(row['evidence']['talisman']['captured_at'], AT)
        self.assertEqual(row['evidence']['talisman']['last_attempt_at'], DAY + 'T22:00:00')
        self.assertFalse(row['evidence']['talisman']['complete'])
        self.assertEqual(self.reader.image(ACCOUNT, DAY, report['evidence']['talisman']['image']), PNG)

    def test_unverified_count_is_not_returned(self):
        directory, report = self.report()
        report['evidence']['collective']['verified'] = False
        self.write_report(directory, report)
        row = self.reader.account_day(ACCOUNT, DAY)
        self.assertEqual(row['status'], 'attention')
        self.assertIsNone(row['evidence']['collective']['current'])
        self.assertEqual(row['evidence']['collective']['status'], 'captured')

    def test_bool_negative_and_over_total_counts_are_not_numbers(self):
        for invalid in (True, -1, 31, '30', None):
            directory, report = self.report()
            report['evidence']['collective']['current'] = invalid
            self.write_report(directory, report)
            self.assertFalse(self.reader.account_day(ACCOUNT, DAY)['evidence']['collective']['verified'])

    def test_missing_image_and_stale_capture_do_not_verify(self):
        directory, report = self.report()
        (directory / report['evidence']['collective']['image']).unlink()
        self.assertFalse(self.reader.account_day(ACCOUNT, DAY)['evidence']['collective']['verified'])
        directory, report = self.report()
        report['evidence']['collective']['captured_at'] = '2026-10-06T22:00:00'
        self.write_report(directory, report)
        self.assertFalse(self.reader.account_day(ACCOUNT, DAY)['evidence']['collective']['verified'])

    def test_copied_account_or_date_document_is_rejected(self):
        for field, value in [('account', '06-其他'), ('date', '2026-10-06'), ('schema_version', True)]:
            directory, report = self.report()
            report[field] = value
            self.write_report(directory, report)
            self.assertEqual(self.reader.account_day(ACCOUNT, DAY)['status'], 'attention')

    def test_disabled_accounts_and_weekend_guild_not_red(self):
        self.config('09', talisman=False, collective=False)
        self.config('01-测试', collective=False)
        self.assertEqual(self.reader.account_day('09', DAY)['status'], 'disabled')
        self.assertEqual(self.reader.account_day('01-测试', DAY)['status'], 'pending')
        self.assertFalse(self.reader.account_day('01-测试', DAY)['evidence']['collective']['expected'])
        weekend = self.reader.account_day(ACCOUNT, '2026-10-10')
        self.assertFalse(weekend['evidence']['collective']['expected'])
        self.assertNotEqual(weekend['status'], 'attention')

    def test_no_history_is_unverified_not_error(self):
        self.assertEqual(self.reader.account_day(ACCOUNT, '2026-10-06')['status'], 'unverified')

    def test_attention_sorted_first_and_disabled_last(self):
        self.config('01-已完成', collective=True)
        self.report('01-已完成')
        self.report(count=25)
        self.config('09', talisman=False, collective=False)
        names = [row['account'] for row in self.reader.day(DAY)['accounts']]
        self.assertEqual(names[0], ACCOUNT)
        self.assertEqual(names[-1], '09')

    def test_credentials_are_not_returned_and_url_details_redacted(self):
        directory, report = self.report()
        report['notification']['bark_key'] = 'DO-NOT-EXPOSE'
        report['evidence']['talisman']['detail'] = '失败 https://api.day.app/SECRET/token'
        report['credentials'] = {'password': 'SECRET'}
        self.write_report(directory, report)
        output = json.dumps(self.reader.day(DAY), ensure_ascii=False)
        for forbidden in ('DO-NOT-EXPOSE', 'ALSO-PRIVATE', 'PRIVATE-DEVICE', 'SECRET', 'password', 'bark_key'):
            self.assertNotIn(forbidden, output)

    def test_closeout_outcomes_read_exact_date_without_claiming_screenshot(self):
        directory = self.root / 'config' / 'daily_closeout'
        directory.mkdir()
        state = {'date': DAY, 'outcomes': {'Dokan': {'date': DAY, 'outcome': 'completed'},
                                        'CollectiveMissions': {'date': DAY, 'outcome': 'failed'}}}
        (directory / (ACCOUNT + '.json')).write_text(json.dumps(state), encoding='utf-8')
        row = self.reader.account_day(ACCOUNT, DAY)
        self.assertEqual(row['closeout']['completed'], ['Dokan'])
        self.assertEqual(row['closeout']['incomplete'][0]['outcome'], 'failed')
        self.assertIsNone(row['evidence']['collective']['current'])
        state['date'] = '2026-10-06'
        (directory / (ACCOUNT + '.json')).write_text(json.dumps(state), encoding='utf-8')
        self.assertEqual(self.reader.account_day(ACCOUNT, DAY)['closeout']['completed'], [])

    def test_report_incomplete_and_push_failure_are_visible(self):
        directory, report = self.report()
        report['closeout']['incomplete'] = [{'task': 'Dokan', 'outcome': 'expired'}]
        self.write_report(directory, report)
        self.assertEqual(self.reader.account_day(ACCOUNT, DAY)['status'], 'attention')
        report['closeout']['incomplete'] = []
        report['notification']['status'] = 'failed'
        self.write_report(directory, report)
        self.assertIn('推送', self.reader.account_day(ACCOUNT, DAY)['reason'])

    def test_unreferenced_traversal_wrong_prefix_and_large_image_rejected(self):
        directory, report = self.report()
        (directory / 'talisman-unreferenced.png').write_bytes(PNG)
        for name in ('../secret.png', 'talisman-unreferenced.png', 'error-123.png', 'collective-../../x.png'):
            with self.assertRaises(FeedbackNotFound): self.reader.image(ACCOUNT, DAY, name)
        path = directory / report['evidence']['collective']['image']
        path.write_bytes(PNG + b'0' * MAX_IMAGE)
        self.assertFalse(self.reader.account_day(ACCOUNT, DAY)['evidence']['collective']['verified'])

    def test_invalid_png_not_served(self):
        directory, report = self.report()
        (directory / report['evidence']['collective']['image']).write_bytes(b'<script>secret</script>')
        with self.assertRaises(FeedbackNotFound):
            self.reader.image(ACCOUNT, DAY, report['evidence']['collective']['image'])

    def test_junction_or_symlink_report_tree_not_followed(self):
        directory, report = self.report()
        outside = self.root / 'outside.png'
        outside.write_bytes(PNG)
        image = directory / report['evidence']['collective']['image']
        image.unlink()
        try:
            image.symlink_to(outside)
        except OSError:
            self.skipTest('Host does not permit creating test symlinks')
        self.assertFalse(self.reader.account_day(ACCOUNT, DAY)['evidence']['collective']['verified'])
        with self.assertRaises(FeedbackNotFound): self.reader.image(ACCOUNT, DAY, image.name)

    def test_windows_reparse_directory_is_refused_without_creating_a_link(self):
        directory, _ = self.report()
        original = Path.lstat
        def simulate_junction(path):
            info = original(path)
            if path == directory:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, 'lstat', simulate_junction):
            row = self.reader.account_day(ACCOUNT, DAY)
            self.assertFalse(row['evidence']['collective']['verified'])
            with self.assertRaises(FeedbackNotFound): self.reader.image(ACCOUNT, DAY, 'collective-20261007-abcdef.png')

    def test_old_provisional_capture_is_unverified_not_still_executing(self):
        self.report(day='2026-10-06', phase='provisional')
        self.assertEqual(self.reader.account_day(ACCOUNT, '2026-10-06')['status'], 'unverified')

    def test_malformed_optional_report_fields_do_not_crash_or_leak(self):
        directory, report = self.report()
        report['closeout']['required'] = [{}, None, 'Dokan']
        report['closeout']['incomplete'] = [{'task': {}, 'outcome': []}, None]
        report['notification'] = ['DO-NOT-EXPOSE']
        self.write_report(directory, report)
        row = self.reader.account_day(ACCOUNT, DAY)
        self.assertEqual(row['closeout']['required'], ['Dokan'])
        self.assertEqual(row['notification']['status'], 'pending')


class ApiTests(FeedbackFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_fixture()
        self.report()
        self.oas = FakeOas()
        self.auth = AuthStore(self.root / 'state')
        self.settings = {'backend_url': 'http://127.0.0.1:22289', 'backend_root': str(self.root),
                         'public_origin': 'https://panel.example.com:4443', 'host': '127.0.0.1', 'port': 22300,
                         'authentication_required': False}
        self.app = create_app(self.settings, client=self.oas, auth=self.auth)

    async def asyncTearDown(self):
        self.assertEqual(self.oas.actions, [])
        self.assertEqual(self.oas.writes, [])
        self.temp.cleanup()

    async def test_open_day_account_and_image_without_login_or_upstream_account(self):
        status, result, _ = await request(self.app, 'GET', '/api/daily-feedback', query={'date': DAY})
        self.assertEqual(status, 200)
        self.assertEqual(result['accounts'][0]['account'], ACCOUNT)
        status, row, _ = await request(self.app, 'GET', '/api/daily-feedback/' + ACCOUNT, query={'date': DAY})
        self.assertEqual(status, 200)
        status, payload, headers = await request(self.app, 'GET', '/api/daily-feedback/' + ACCOUNT + '/images/' + row['evidence']['talisman']['image'], query={'date': DAY})
        self.assertEqual(status, 200)
        self.assertEqual(payload, PNG)
        self.assertIn((b'content-type', b'image/png'), headers)

    async def test_authentication_mode_still_protects_all_evidence(self):
        app = create_app({**self.settings, 'authentication_required': True}, client=self.oas, auth=self.auth)
        for path in ('/api/daily-feedback', '/api/daily-feedback/' + ACCOUNT,
                     '/api/daily-feedback/' + ACCOUNT + '/images/talisman-20261007-abcdef.png'):
            status, _, _ = await request(app, 'GET', path, query={'date': DAY})
            self.assertEqual(status, 401)

    async def test_foreign_origin_invalid_date_account_and_image_rejected(self):
        status, _, _ = await request(self.app, 'GET', '/api/daily-feedback', origin='https://unrelated.example', query={'date': DAY})
        self.assertEqual(status, 403)
        for day in ('../2026-10-07', '2026-02-30', '2026-1-1'):
            status, _, _ = await request(self.app, 'GET', '/api/daily-feedback', query={'date': day})
            self.assertEqual(status, 422)
        status, _, _ = await request(self.app, 'GET', '/api/daily-feedback/not-a-real-account', query={'date': DAY})
        self.assertEqual(status, 404)
        status, _, _ = await request(self.app, 'GET', '/api/daily-feedback/' + ACCOUNT + '/images/talisman-unreferenced.png', query={'date': DAY})
        self.assertEqual(status, 404)

    async def test_html_entry_exists_but_does_not_trigger_actions(self):
        status, payload, _ = await request(self.app, 'GET', '/daily-feedback')
        self.assertEqual(status, 200)
        self.assertIn('每日验收'.encode(), payload)
        self.assertIn(b'daily-feedback.js', payload)


if __name__ == '__main__': unittest.main()
