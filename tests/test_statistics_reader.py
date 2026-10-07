"""Only temporary logs are read; no OAS imports, connections or control actions."""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from statistics_reader import LocalStatisticsReader
from oas_client import UpstreamError

PARSER_SOURCE = Path(__file__).resolve().parent / 'fixtures' / 'log_stats.py'


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = self.root / "module" / "server"
        source.mkdir(parents=True)
        shutil.copyfile(PARSER_SOURCE, source / "log_stats.py")
        (source / "__init__.py").write_text("raise RuntimeError('OAS package must not be imported')", encoding="utf-8")
        (self.root / "config").mkdir()
        (self.root / "config" / "09.json").write_text("{}", encoding="utf-8")
        (self.root / "log").mkdir()
        self.log = self.root / "log" / "2026-10-05_09.txt"
        self.log.write_text(
            "2026-10-05 10:00:00.000 | script.py:0001 | INFO | Scheduler: Start task `Chess`\n"
            "2026-10-05 10:01:00.000 | script.py:0001 | INFO | Chess completed games: 1/1\n"
            "2026-10-05 10:02:00.000 | script.py:0001 | INFO | Scheduler: End task `Chess`\n",
            encoding="utf-8",
        )
        self.reader = LocalStatisticsReader(self.root)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_reads_only_bound_root_without_loading_runtime(self):
        before = {name for name in sys.modules if name.startswith("module.")}
        dates = await self.reader.statistics_dates("09")
        day = await self.reader.statistics_day("09", "2026-10-05")
        self.assertEqual(dates["dates"], ["2026-10-05"])
        self.assertEqual(day["total_task_run_count"], 1)
        self.assertEqual(day["total_battle_count"], 1)
        self.assertEqual(day["total_runtime_seconds"], 120)
        self.assertEqual(day["tasks"]["Chess"]["runs"][0]["status"], "completed")
        self.assertEqual(before, {name for name in sys.modules if name.startswith("module.")})
        self.assertEqual(day["source"]["kind"], "local_logs")

    async def test_append_refresh_and_invalid_request_never_mutate_logs(self):
        initial = self.log.read_bytes()
        for account in ("../09", "C:09"):
            with self.assertRaises(ValueError):
                await self.reader.statistics_dates(account)
        with self.assertRaises(ValueError):
            await self.reader.statistics_day("09", "2026-2-05")
        with self.assertRaises(UpstreamError):
            await self.reader.statistics_dates("unknown")
        self.assertEqual(self.log.read_bytes(), initial)
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write("2026-10-05 11:00:00.000 | script.py:0001 | INFO | Scheduler: Start task `Chess`\n")
        day = await self.reader.statistics_day("09", "2026-10-05")
        self.assertEqual(day["total_task_run_count"], 2)
        self.assertEqual(day["incomplete_run_count"], 1)

    def write_bondling_log(self):
        self.log.write_text(
            "2026-10-05 10:00:00.000 | script.py:0001 | INFO | Scheduler: Start task `BondlingFairyland`\n"
            "2026-10-05 10:00:01.000 | battle.py:0001 | INFO | Start battle process\n"
            "2026-10-05 10:01:00.000 | battle.py:0001 | INFO | Catch success\n"
            "2026-10-05 10:01:01.000 | script_task.py:0001 | INFO | Catch successful, back to the page\n"
            "2026-10-05 10:01:02.000 | battle.py:0001 | INFO | Start battle process\n"
            "2026-10-05 10:02:00.000 | battle.py:0001 | INFO | Catch failure\n"
            "2026-10-05 10:03:00.000 | script.py:0001 | INFO | Scheduler: End task `BondlingFairyland`\n",
            encoding="utf-8",
        )

    async def test_counts_bondling_outcomes_in_panel_reader(self):
        self.write_bondling_log()
        day = await self.reader.statistics_day("09", "2026-10-05")
        self.assertEqual(day["total_battle_count"], 2)
        self.assertEqual(day["tasks"]["BondlingFairyland"]["battle"]["count"], 2)
        self.assertIn("total_battle_count", day["available_metrics"])

    async def test_parser_update_reloads_and_reparses_existing_cached_logs(self):
        self.write_bondling_log()
        source = PARSER_SOURCE.read_text(encoding="utf-8")
        old_source = source.replace('_BONDLING_RESULTS = {"Catch success", "Catch failure"}',
                                    '_BONDLING_RESULTS = set()')
        self.assertNotEqual(old_source, source, "Fixture must reproduce the parser's missing outcomes")
        parser_path = self.root / "module" / "server" / "log_stats.py"
        parser_path.write_text(old_source, encoding="utf-8")
        reader = LocalStatisticsReader(self.root)
        before = await reader.statistics_day("09", "2026-10-05")
        service = reader.service
        self.assertEqual(before["total_battle_count"], 0)
        await reader.statistics_dates("09")
        self.assertIs(service, reader.service, "Unchanged parser must keep incremental log cache")
        parser_path.write_text(source, encoding="utf-8")
        after = await reader.statistics_day("09", "2026-10-05")
        self.assertIsNot(service, reader.service)
        self.assertEqual(after["total_battle_count"], 2,
                         "Already consumed logs must be reparsed after the parser changes")

    async def test_same_size_same_second_edit_does_not_reuse_bytecode(self):
        self.write_bondling_log()
        parser_path = self.root / "module" / "server" / "log_stats.py"
        source = parser_path.read_text(encoding="utf-8")
        original_info = parser_path.stat()
        before = await self.reader.statistics_day("09", "2026-10-05")
        self.assertEqual(before["total_battle_count"], 2)
        edited = source.replace('"Catch success", "Catch failure"', '"Catch unknown", "Catch failure"')
        self.assertEqual(len(edited.encode()), len(source.encode()))
        parser_path.write_text(edited, encoding="utf-8")
        original_seconds = original_info.st_mtime_ns // 1_000_000_000
        new_ns = original_seconds * 1_000_000_000 + (original_info.st_mtime_ns + 100_000_000) % 1_000_000_000
        os.utime(parser_path, ns=(original_info.st_atime_ns, new_ns))
        after = await self.reader.statistics_day("09", "2026-10-05")
        self.assertEqual(after["total_battle_count"], 1)

    async def test_partial_parser_update_fails_without_poisoning_recovery(self):
        self.write_bondling_log()
        parser_path = self.root / "module" / "server" / "log_stats.py"
        source = parser_path.read_bytes()
        original_service = self.reader.service
        parser_path.write_text("def broken(\n", encoding="utf-8")
        with self.assertRaises(UpstreamError):
            await self.reader.statistics_day("09", "2026-10-05")
        self.assertIs(self.reader.service, original_service)
        parser_path.write_bytes(source)
        dates, day = await asyncio.gather(self.reader.statistics_dates("09"),
                                         self.reader.statistics_day("09", "2026-10-05"))
        self.assertEqual(dates["dates"], ["2026-10-05"])
        self.assertEqual(day["total_battle_count"], 2)

    async def test_local_and_phone_https_routes_share_corrected_reader(self):
        from app import create_app
        from auth import AuthStore
        from tests.test_backend import request

        class AccountFixture:
            async def accounts(self):
                return ["09"]

        self.write_bondling_log()
        auth = AuthStore(self.root / "panel-state")
        initial = (self.root / "panel-state" / "initial-login.txt").read_text(encoding="utf-8")
        password = initial.split("密码：", 1)[1].splitlines()[0]
        token, _ = auth.authenticate("admin", password)
        settings = {"host": "127.0.0.1", "port": 22300,
                    "public_origin": "https://panel.example.com:4443",
                    "backend_url": "http://127.0.0.1:22289", "backend_root": str(self.root)}
        application = create_app(settings, client=AccountFixture(), auth=auth, statistics=self.reader)
        for origin_args in ({}, {"host": "panel.example.com:4443", "origin": "https://panel.example.com:4443",
                                  "extra_headers": [("x-forwarded-proto", "https")]}):
            with self.subTest(origin=origin_args.get("host", "local")):
                status, dates, _ = await request(application, "GET", "/api/accounts/09/statistics/dates",
                                                token=token, **origin_args)
                self.assertEqual(status, 200)
                self.assertEqual(dates["dates"], ["2026-10-05"])
                status, day, headers = await request(application, "GET", "/api/accounts/09/statistics",
                                                     query={"date": "2026-10-05"}, token=token, **origin_args)
                self.assertEqual(status, 200)
                self.assertEqual(day["total_battle_count"], 2)
                self.assertIn((b"cache-control", b"no-store"), headers)


if __name__ == "__main__":
    unittest.main()
