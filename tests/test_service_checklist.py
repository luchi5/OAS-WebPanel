"""Offline fixed-service view and historical-switch regressions."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from service_checklist import SERVICE_TASKS, build_service_checklist, validate_checklist
from task_history import TaskHistory
from tests.test_daily_feedback import FeedbackFixture, ACCOUNT, DAY

NOW = datetime(2026, 10, 10, 20, tzinfo=timezone(timedelta(hours=8)))
DATE = '2026-10-10'


def config():
    result = {field: {'scheduler': {'enable': True, 'next_run': DATE + ' 21:00:00'}}
              for _, field, _, _, _ in SERVICE_TASKS}
    result['restart']['harvest_config'] = {'enable': True, 'enable_sign': True}
    result['daily_trifles']['trifles_config'] = {'luck_msg': True}
    return result


def checklist(settings=None, **kwargs):
    report = kwargs.pop('report', {'tasks': []})
    return build_service_checklist(settings if settings is not None else config(), DATE, DATE,
        now=NOW, report=report, execution=kwargs.pop('execution', {'started': set(), 'finished': {}}), **kwargs)


def rows(report):
    return {row['task']: row for row in report['tasks']}


class ServiceChecklistTests(unittest.TestCase):
    def test_fixed_order_and_all_poster_rows(self):
        result = checklist()
        self.assertEqual(len(result['tasks']), 25)
        self.assertEqual([r['label'] for r in result['tasks'][:5]], ['悬赏封印', '结界蹭卡', '结界挂卡', '个人突破', '地域鬼王'])
        self.assertEqual(sum(r['group'] == 'daily' for r in result['tasks']), 14)
        self.assertEqual(sum(r['group'] == 'weekly' for r in result['tasks']), 11)
        self.assertEqual(rows(result)['WantedQuests']['status'], 'pending')

    def test_started_failure_is_unverified_even_if_next_run_is_future(self):
        result = rows(checklist(execution={'started': {'WantedQuests'}, 'finished': {}},
                     report={'tasks': [{'task':'WantedQuests','status':'incomplete',
                              'status_label':'未完成','reason':'运行报错','evidence_at':None}]}))
        self.assertEqual(result['WantedQuests']['status'], 'unverified')
        self.assertEqual(result['AutoCheckinBigGod']['status'], 'pending')

    def test_unstarted_observed_task_is_pending(self):
        result = rows(checklist(report={'tasks': [{'task':'WantedQuests','status':'incomplete',
                              'status_label':'未完成','reason':'等待','evidence_at':None}]}))
        self.assertEqual(result['WantedQuests']['status'], 'pending')

    def test_legacy_checklist_retains_rows_and_adds_unknown_god_sign(self):
        old = checklist()
        old['tasks'] = [row for row in old['tasks'] if row['task'] != 'AutoCheckinBigGod']
        upgraded = validate_checklist(old, DATE)
        self.assertEqual(len(upgraded['tasks']), 25)
        self.assertEqual(rows(upgraded)['AutoCheckinBigGod']['status'], 'unverified')
        self.assertEqual(rows(upgraded)['WantedQuests']['status'], 'pending')

    def test_disabled_subfeatures_and_shared_hunt_calendar(self):
        settings = config()
        settings['wanted_quests']['scheduler']['enable'] = False
        settings['restart']['harvest_config']['enable_sign'] = False
        settings['daily_trifles']['trifles_config']['luck_msg'] = False
        result = rows(checklist(settings, execution={'started': {'Hunt'}, 'finished': {'Hunt': DATE + 'T19:00:00+08:00'}}))
        for task in ('WantedQuests', 'DailySign', 'LuckMessage'):
            self.assertEqual(result[task]['status_label'], '未开启')
        self.assertEqual(result['Kirin']['status_label'], '今日无需运行')
        self.assertEqual(result['Netherworld']['status'], 'completed')

    def test_weekly_future_and_explicit_calendar_not_unfinished(self):
        settings = config()
        settings['chess']['scheduler']['next_run'] = '2026-10-12 10:00:00'
        settings['dokan']['dokan_config'] = {'monday_to_thursday': True}
        settings['guild_banquet']['guild_banquet_time'] = {'day_1': '星期三', 'day_2': '星期日'}
        result = rows(checklist(settings))
        for task in ('Chess', 'Dokan', 'GuildBanquet'):
            self.assertEqual(result[task]['status'], 'not_scheduled')
        ended = checklist(settings, execution={'started': {'Chess'}, 'finished': {'Chess': DATE + 'T19:00:00'}})
        self.assertEqual(rows(ended)['Chess']['status'], 'completed')

    def test_preserves_foster_count_area_progress_and_courtyard_result(self):
        actual = [{'task': 'KekkaiUtilize', 'status': 'completed', 'status_label': '2次', 'completion_count': 2,
                   'reason': '真实新寄养次数', 'evidence_at': DATE + 'T10:00:00'},
                  {'task': 'AreaBoss', 'status': 'incomplete', 'status_label': '未完成',
                   'reason': '今日已成功处理 1/2 只地域鬼王', 'evidence_at': DATE + 'T10:00:00'},
                  {'task': 'CourtyardAffairs', 'status': 'unverified', 'status_label': '没找到入口',
                   'reason': '当天入口未出现', 'evidence_at': DATE + 'T10:00:00'}]
        result = rows(checklist(report={'tasks': actual}))
        self.assertEqual(result['KekkaiUtilize']['completion_count'], 2)
        self.assertIn('1/2', result['AreaBoss']['reason'])
        self.assertEqual(result['CourtyardAffairs']['status_label'], '没找到入口')

    def test_excluded_composites_normal_end_and_no_false_foster_completion(self):
        result = rows(checklist(execution={'started': {'Restart', 'DailyTrifles', 'Orochi', 'TalismanPass', 'KekkaiUtilize'},
                  'finished': {task: DATE + 'T19:00:00' for task in ('Restart', 'DailyTrifles', 'Orochi', 'TalismanPass', 'KekkaiUtilize')}}))
        for task in ('DailySign', 'LuckMessage', 'PetSoul', 'TalismanPass'):
            self.assertEqual(result[task]['status'], 'completed')
        self.assertIn('未单独核验', result['PetSoul']['reason'])
        self.assertNotEqual(result['KekkaiUtilize']['status'], 'completed')

    def test_history_uses_snapshot_not_todays_switches(self):
        settings = config()
        settings['wanted_quests']['scheduler']['enable'] = False
        snapshot = checklist(settings)
        with tempfile.TemporaryDirectory() as directory:
            history = TaskHistory(Path(directory))
            report = {'available': True, 'tasks': [], 'service_checklist': snapshot}
            history.save('01-test', DATE, report, NOW.isoformat(), planned=True)
            # Cheap plan capture cannot erase accumulated results/switch snapshot.
            report['service_checklist'] = checklist()
            history.save('01-test', DATE, report, NOW.isoformat(), planned=True, plan_only=True)
            saved = history.load('01-test', DATE)['service_checklist']
            old = build_service_checklist(config(), DATE, '2026-10-11', now=NOW + timedelta(days=1),
                 report={'tasks': []}, archived=saved)
            self.assertEqual(rows(old)['WantedQuests']['status_label'], '未开启')
            self.assertEqual(len(old['tasks']), 25)
            unknown = build_service_checklist(settings, DATE, '2026-10-11', now=NOW + timedelta(days=1), report={'tasks': []})
            self.assertEqual(rows(unknown)['WantedQuests']['status'], 'unverified')

    def test_unreadable_config_and_invalid_history_do_not_claim_disabled(self):
        result = checklist({})
        self.assertTrue(all(r['status'] == 'unverified' for r in result['tasks']))
        bad = copy.deepcopy(result)
        bad['tasks'][0]['status'] = 'completed'
        with self.assertRaises(ValueError): validate_checklist(bad, DATE)

    def test_repeated_refresh_of_saved_composite_does_not_expand_reason(self):
        snapshot = checklist(execution={'started': {'Orochi'}, 'finished': {'Orochi': DATE + 'T19:00:00'}})
        reason = rows(snapshot)['PetSoul']['reason']
        for _ in range(30):
            snapshot = checklist(archived=snapshot, execution=None)
        self.assertEqual(rows(snapshot)['PetSoul']['reason'], reason)


class ServiceIntegrationTests(FeedbackFixture, unittest.TestCase):
    def setUp(self): self.setup_fixture()
    def tearDown(self): self.temp.cleanup()

    def test_reader_serves_fixed_disabled_rows_without_backend_actions(self):
        result = self.reader.account_day(ACCOUNT, DAY)['task_completion']['service_checklist']
        self.assertEqual(len(result['tasks']), 25)
        self.assertEqual(rows(result)['WantedQuests']['status_label'], '未开启')
        self.assertEqual(result['date'], DAY)
