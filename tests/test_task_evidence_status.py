"""Synthetic completion records for the portable dashboard, no OAS imports."""
from datetime import datetime, timezone, timedelta
import unittest

from task_completion_reader import build_task_completion

DAY = '2026-10-11'
AT = DAY + 'T10:00:00+08:00'
NOW = datetime(2026, 10, 11, 22, tzinfo=timezone(timedelta(hours=8)))

def result(field, **evidence):
    rows = build_task_completion({field: {'scheduler': {'enable': True}}}, DAY, DAY, now=NOW, **evidence)['tasks']
    return rows[0]

def boss_proof():
    return {'schema_version': 1, 'source': 'area_boss_task_v1', 'task': 'AreaBoss', 'date': DAY,
            'at': AT, 'target': 2, 'reputation': 5980, 'completed_count': 2, 'unidentified_successes': 0,
            'bosses': [{'name': name, 'at': AT, 'basis': 'battle_success'} for name in ('鬼王甲', '鬼王乙')]}

class TaskEvidenceStatusTests(unittest.TestCase):
    def test_area_boss_distinct_confirmed_count_meets_reputation_quota(self):
        row = result('area_boss', area_boss=boss_proof())
        self.assertEqual(row['status'], 'completed')
        self.assertIn('2/2', row['reason'])

    def test_duplicate_names_and_invalid_quota_cannot_claim_completion(self):
        duplicate = boss_proof()
        duplicate['bosses'][1]['name'] = duplicate['bosses'][0]['name']
        self.assertEqual(result('area_boss', area_boss=duplicate)['status'], 'unverified')
        malformed = boss_proof()
        malformed['target'] = 3
        self.assertEqual(result('area_boss', area_boss=malformed)['status'], 'unverified')

    def test_foster_count_excludes_zero_and_rejects_boolean_count(self):
        proof = {'schema_version': 1, 'source': 'kekkai_utilize_log_v1', 'task': 'KekkaiUtilize', 'date': DAY, 'at': AT, 'count': 2}
        row = result('kekkai_utilize', kekkai_utilize=proof)
        self.assertEqual((row['status'], row['status_label'], row['completion_count']), ('completed', '2次', 2))
        proof.update(count=0, at=None)
        self.assertEqual(result('kekkai_utilize', kekkai_utilize=proof)['status_label'], '0次')
        proof.update(count=True, at=AT)
        self.assertEqual(result('kekkai_utilize', kekkai_utilize=proof)['status'], 'unverified')

    def test_courtyard_claim_requires_completion_evidence(self):
        proof = {'schema_version': 1, 'source': 'courtyard_affairs_task_v1', 'task': 'CourtyardAffairs', 'date': DAY, 'at': AT, 'outcome': 'no_entry', 'one_click_count': 0}
        self.assertEqual(result('courtyard_affairs', courtyard=proof)['status_label'], '没找到入口')
        proof.update(outcome='completed', one_click_count=0)
        self.assertEqual(result('courtyard_affairs', courtyard=proof)['status'], 'unverified')
        proof['one_click_count'] = 1
        self.assertEqual(result('courtyard_affairs', courtyard=proof)['status'], 'completed')

    def test_future_only_task_is_excluded_until_same_day_execution(self):
        config = {'dokan': {'scheduler': {'enable': True, 'next_run': '2026-10-12T19:00:00'}}}
        view = build_task_completion(config, DAY, DAY, now=NOW, planned_today=True, started_tasks=set())
        self.assertEqual(view['tasks'], [])
        view = build_task_completion(config, DAY, DAY, now=NOW, planned_today=True,
            started_tasks={'Dokan'}, normal_ends={'Dokan': AT})
        self.assertEqual(view['tasks'][0]['status'], 'completed')

if __name__ == '__main__':
    unittest.main()
