"""Offline cache invalidation tests using temporary logs only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from daily_feedback_reader import DailyFeedbackReader, SHANGHAI
from tests.test_daily_feedback import FeedbackFixture, ACCOUNT, DAY


def event(message, stamp='2026-10-07 12:00:00', source='script.py'):
    return f'{stamp} | {source}:1 | INFO | {message}\n'.encode()


START = event('Scheduler: Start task `DailyTrifles`')
END = event('Scheduler: End task `DailyTrifles`', '2026-10-07 12:00:01')


class FeedbackCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'log').mkdir()
        (self.root / 'config').mkdir()
        (self.root / 'config' / 'test.json').write_text(json.dumps({'daily_trifles': {'scheduler': {'enable': True}}}))
        self.path = self.root / 'log' / (DAY + '_test.txt')
        self.path.write_bytes(START + END)
        self.now = datetime(2026, 10, 7, 13, tzinfo=SHANGHAI)
        self.reader = DailyFeedbackReader(self.root, now=lambda: self.now)

    def read(self):
        return self.reader._task_execution('test', DAY)

    def assert_fresh(self):
        fresh = DailyFeedbackReader(self.root, now=lambda: self.now)
        self.assertEqual(self.reader.account_day('test', DAY), fresh.account_day('test', DAY))

    def test_unchanged_result_uses_one_parse_and_returns_detached_data(self):
        with patch.object(self.reader, '_execution_log', wraps=self.reader._execution_log) as parse:
            original = self.read()
            original['finished'].clear()
            self.assertIn('DailyTrifles', self.read()['finished'])
            self.assertEqual(parse.call_count, 1)

    def test_partial_line_then_append_is_reparsed(self):
        self.path.write_bytes(START + END[:-1])
        self.assertFalse(self.read()['finished'])
        with self.path.open('ab') as stream:
            stream.write(b'\n')
        self.assertIn('DailyTrifles', self.read()['finished'])
        self.assert_fresh()

    def test_truncation_clears_previously_finished_tasks(self):
        self.read()
        self.path.write_bytes(START)
        self.assertFalse(self.read()['finished'])
        self.assert_fresh()

    def test_same_size_replacement_with_preserved_mtime(self):
        old = self.path.stat()
        self.read()
        replacement = self.path.with_suffix('.tmp')
        replacement.write_bytes((START + END).replace(b'DailyTrifles', b'DailyOtherxx'))
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        replacement.replace(self.path)
        self.assertEqual(self.path.stat().st_size, old.st_size)
        self.assertIn('DailyOtherxx', self.read()['finished'])
        self.assert_fresh()

    def test_same_second_modification_and_timestamp_rollback(self):
        self.read()
        old = self.path.stat()
        self.path.write_bytes((START + END).replace(b'DailyTrifles', b'DailyOtherxx'))
        os.utime(self.path, ns=(old.st_atime_ns, old.st_mtime_ns - 100))
        self.assertIn('DailyOtherxx', self.read()['finished'])

    def test_deleted_log_is_not_served_from_cache(self):
        self.read()
        self.path.unlink()
        self.assertEqual(self.read()['started'], set())
        self.assert_fresh()

    def test_future_event_becomes_visible_without_file_change(self):
        self.now = datetime(2026, 10, 7, 12, tzinfo=SHANGHAI)
        self.assertFalse(self.read()['finished'])
        self.now += timedelta(seconds=1)
        self.assertIn('DailyTrifles', self.read()['finished'])
        self.now -= timedelta(seconds=1)
        self.assertFalse(self.read()['finished'])

    def test_day_rollover_keeps_reports_distinct(self):
        self.assert_fresh()
        self.now += timedelta(days=1)
        self.assert_fresh()
        next_day = self.now.date().isoformat()
        (self.root / 'log' / (next_day + '_test.txt')).write_bytes(START.replace(DAY.encode(), next_day.encode()))
        self.assertFalse(self.reader._task_execution('test', next_day)['finished'])
        self.assertIn('DailyTrifles', self.read()['finished'])

    def test_concurrent_requests_share_parse_without_shared_results(self):
        with patch.object(self.reader, '_execution_log', wraps=self.reader._execution_log) as parse:
            with ThreadPoolExecutor(max_workers=6) as pool:
                values = list(pool.map(lambda _: self.read(), range(20)))
            self.assertEqual(parse.call_count, 1)
            values[0]['started'].clear()
            self.assertTrue(all(value['started'] for value in values[1:]))

    def test_file_change_during_read_is_not_cached(self):
        parse = self.reader._execution_log
        def changing(*args):
            result = parse(*args)
            self.path.write_bytes(START)
            return result
        with patch.object(self.reader, '_execution_log', side_effect=changing):
            self.read()
        self.assertFalse(self.read()['finished'])

    def test_report_does_not_cache_changed_config_or_sidecars(self):
        self.assert_fresh()
        config = self.root / 'config' / 'test.json'
        config.write_text(json.dumps({'daily_trifles': {'scheduler': {'enable': False}}}))
        self.assert_fresh()
        proof = self.root / 'log' / 'courtyard_affairs' / 'test' / (DAY + '.json')
        proof.parent.mkdir(parents=True)
        proof.write_text(json.dumps({'schema_version': 1, 'account': 'test', 'task': 'CourtyardAffairs',
                                     'date': DAY, 'source': 'courtyard_affairs_task_v1', 'outcome': 'completed'}))
        self.assertEqual(self.reader._courtyard_proof('test', DAY)['outcome'], 'completed')
        self.assert_fresh()


class FeedbackImageTests(FeedbackFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()
        self.addCleanup(self.temp.cleanup)

    def test_image_verifies_report_without_parsing_tasks_or_writing_history(self):
        _, report = self.report()
        with patch.object(self.reader, '_task_completion', side_effect=AssertionError('No task read for image')):
            self.assertTrue(self.reader.image(ACCOUNT, DAY, report['evidence']['talisman']['image']).startswith(b'\x89PNG'))


if __name__ == '__main__':
    unittest.main()
