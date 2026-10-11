"""Pure-data completion tests; never import OAS or touch the running project."""
import copy
from datetime import datetime, timezone, timedelta
import json
import unittest

from task_completion_reader import build_task_completion, TASK_CATALOG


DAY = '2026-10-08'
NOW = datetime(2026, 10, 8, 22, tzinfo=timezone(timedelta(hours=8)))


def config(*fields):
    return {field: {'scheduler': {'enable': True}} for field in fields}


def proofs(task, outcome='completed', detail='final_challenge_confirmed', at='2026-10-08T19:18:34', day=DAY):
    return {'date': day, 'outcomes': {task: {'date': day, 'at': at, 'outcome': outcome, 'detail': detail}}}


class TaskCompletionReaderTests(unittest.TestCase):
    def build(self, value, **kwargs):
        return build_task_completion(value, DAY, DAY, now=NOW, **kwargs)

    def test_no_evidence_is_unknown_not_zero_or_completed(self):
        result = self.build(config('dokan', 'daily_trifles'))
        self.assertTrue(result['available'])
        self.assertEqual(result['scope'], 'enabled_today')
        self.assertEqual(result['counts'], {'completed': 0, 'incomplete': 0, 'unverified': 2})
        self.assertTrue(all(row['status'] == 'unverified' and row['evidence_at'] is None for row in result['tasks']))

    def test_explicit_results_are_visible_before_unknown_tasks(self):
        evidence = proofs('Dokan')
        evidence['outcomes']['AbyssShadows'] = proofs('AbyssShadows', 'skipped',
            'targets_exhausted_before_quota: boss=1/2, general=4/4, elite=6/6')['outcomes']['AbyssShadows']
        result = self.build(config('area_boss', 'dokan', 'abyss_shadows'), proofs=evidence)
        self.assertEqual([row['status'] for row in result['tasks']], ['incomplete', 'completed', 'unverified'])

    def test_enabled_true_only_and_system_and_picture_tasks_excluded(self):
        value = config('dokan', 'restart', 'global_game', 'script', 'talisman_pass', 'collective_missions')
        value.update({'hunt': {'scheduler': {'enable': False}}, 'orochi': {'scheduler': {'enable': 1}},
                      'pets': {'scheduler': {'enable': 'true'}}, 'not_task': {'enable': True},
                      '../escape': {'scheduler': {'enable': True}}})
        self.assertEqual([row['task'] for row in self.build(value)['tasks']], ['Dokan'])

    def test_known_catalog_and_safe_unknown_task_label_fallback(self):
        result = self.build(config(*TASK_CATALOG, 'new_game_task'), labels={'NewGameTask': '新任务', 'dokan': '自定义道馆'})
        rows = {row['task']: row for row in result['tasks']}
        self.assertEqual(rows['NewGameTask']['label'], '新任务')
        self.assertEqual(rows['Dokan']['label'], '自定义道馆')
        self.assertEqual(len(rows), len(TASK_CATALOG) + 1)
        self.assertTrue(all(row['status'] == 'unverified' for row in rows.values()))

    def test_history_does_not_apply_current_enabled_scope(self):
        result = build_task_completion(config('dokan'), '2026-10-07', DAY, proofs=proofs('Dokan', day='2026-10-07'), now=NOW)
        self.assertFalse(result['available'])
        self.assertEqual(result['tasks'], [])
        self.assertEqual(sum(result['counts'].values()), 0)
        self.assertIn('未保存', result['note'])

    def test_strong_completed_details_only(self):
        cases = [('dokan', 'Dokan', 'final_challenge_confirmed'),
                 ('abyss_shadows', 'AbyssShadows', 'actual_battle_quota_confirmed'),
                 ('guild_banquet', 'GuildBanquet', 'entered_banquet_ended'),
                 ('demon_retreat', 'DemonRetreat', 'battle_win_confirmed')]
        for field, task, detail in cases:
            with self.subTest(task=task):
                row = self.build(config(field), proofs=proofs(task, detail=detail))['tasks'][0]
                self.assertEqual(row['status'], 'completed')
                self.assertEqual(row['evidence_at'], '2026-10-08T19:18:34+08:00')
        row = self.build(config('guild_banquet'), proofs=proofs('GuildBanquet', detail='entered_banquet_ended'))['tasks'][0]
        self.assertIn('参加并结束', row['reason'])
        self.assertIn('不代表经验已满', row['reason'])

    def test_unknown_completed_detail_and_unsupported_task_never_promoted(self):
        for value in ('finished', '', None, ['final_challenge_confirmed']):
            with self.subTest(detail=value):
                self.assertEqual(self.build(config('dokan'), proofs=proofs('Dokan', detail=value))['tasks'][0]['status'], 'unverified')
        self.assertEqual(self.build(config('daily_trifles'), proofs=proofs('DailyTrifles'))['tasks'][0]['status'], 'unverified')

    def test_wrong_day_invalid_timestamp_and_future_proof_rejected(self):
        candidates = [proofs('Dokan', day='2026-10-07'),
                      proofs('Dokan', at='2026-10-07T19:18:34'),
                      proofs('Dokan', at='2026-10-08T22:00:01'),
                      proofs('Dokan', at='2026-10-09T00:00:00'),
                      proofs('Dokan', at='2026-10-08'),
                      proofs('Dokan', at='invalid'),
                      proofs('Dokan', at='2026-10-08T25:00:00'),
                      proofs('Dokan', at='9999-12-31T23:59:59-23:00'),
                      proofs('Dokan', at=True)]
        for value in candidates:
            with self.subTest(value=value):
                row = self.build(config('dokan'), proofs=value)['tasks'][0]
                self.assertEqual(row['status'], 'unverified')
                self.assertIsNone(row['evidence_at'])

    def test_aware_proof_uses_shanghai_day_and_now_boundary(self):
        row = self.build(config('dokan'), proofs=proofs('Dokan', at='2026-10-08T11:18:34Z'))['tasks'][0]
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['evidence_at'], '2026-10-08T19:18:34+08:00')
        row = self.build(config('dokan'), proofs=proofs('Dokan', at='2026-10-08T22:00:00+08:00'))['tasks'][0]
        self.assertEqual(row['status'], 'completed')
        row = self.build(config('dokan'), proofs=proofs('Dokan', at='2026-10-08T23:00:00Z'))['tasks'][0]
        self.assertEqual(row['status'], 'unverified')

    def test_completed_proof_wins_over_old_error_summary(self):
        closeout = {'at': '2026-10-08T18:00:00', 'incomplete': [{'task': 'Dokan', 'outcome': 'failed'}]}
        row = self.build(config('dokan'), proofs=proofs('Dokan'), closeout=closeout)['tasks'][0]
        self.assertEqual(row['status'], 'completed')

    def test_summary_without_detailed_proof_is_not_completion(self):
        closeout = {'phase': 'final', 'at': '2026-10-08T21:00:00', 'completed': ['Dokan', 'GuildBanquet']}
        self.assertTrue(all(row['status'] == 'unverified' for row in self.build(config('dokan', 'guild_banquet'), closeout=closeout)['tasks']))

    def test_failed_ocr_unknown_expired_skipped_and_blocked_stay_unknown(self):
        for outcome, detail in [('failed', 'previous_submission_not_confirmed'),
                                ('expired', 'opening_window_expired'),
                                ('skipped', 'existing_reward_claimed_without_new_battle'),
                                ('blocked_by_dokan', '')]:
            row = self.build(config('demon_retreat'), proofs=proofs('DemonRetreat', outcome, detail))['tasks'][0]
            self.assertEqual(row['status'], 'unverified')

    def test_explicit_abyss_remaining_quota_is_incomplete(self):
        detail = 'targets_exhausted_before_quota: boss=1/2, general=4/4, elite=6/6'
        result = self.build(config('abyss_shadows'), proofs=proofs('AbyssShadows', 'skipped', detail))
        self.assertEqual(result['tasks'][0]['status'], 'incomplete')
        self.assertEqual(result['counts']['incomplete'], 1)
        for detail in ('targets_exhausted_before_quota',
                       'targets_exhausted_before_quota: boss=2/2, general=4/4, elite=6/6',
                       'targets_exhausted_before_quota: boss=-1/2, general=0/4, elite=0/6',
                       'targets_exhausted_before_quota: boss=1/20, general=4/40, elite=6/60'):
            self.assertEqual(self.build(config('abyss_shadows'), proofs=proofs('AbyssShadows', 'skipped', detail))['tasks'][0]['status'], 'unverified')

    def test_schedule_deadline_last_run_and_finish_flags_do_not_prove_completion(self):
        value = config('dokan')
        value['dokan']['scheduler'].update({'next_run': '2026-10-09T19:00:00', 'last_run': '2026-10-08T19:00:00', 'finished': True})
        value['dokan'].update({'deadline': '2026-10-08T20:00:00', 'success': True, 'TaskEnd': True})
        self.assertEqual(self.build(value)['tasks'][0]['status'], 'unverified')

    def test_config_secrets_never_enter_output_and_inputs_are_unchanged(self):
        value = config('dokan')
        value['dokan']['secret'] = 'private-value'
        value['dokan']['scheduler']['next_run'] = 'credential-value'
        value['notifier'] = {'token': 'push-secret', 'scheduler': {'enable': True}}
        evidence = proofs('Dokan')
        evidence['outcomes']['Dokan']['private'] = 'proof-secret'
        original = copy.deepcopy((value, evidence))
        result = self.build(value, proofs=evidence)
        for secret in ('private-value', 'credential-value', 'push-secret', 'proof-secret'):
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual((value, evidence), original)

    def test_invalid_config_and_proof_shapes_are_conservative(self):
        self.assertFalse(self.build(None)['available'])
        for value in (None, [], {'date': DAY, 'outcomes': []}, {'date': DAY, 'outcomes': {'Dokan': 'completed'}}):
            self.assertEqual(self.build(config('dokan'), proofs=value)['tasks'][0]['status'], 'unverified')


if __name__ == '__main__':
    unittest.main()
