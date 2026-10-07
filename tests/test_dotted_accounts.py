"""Dotted account names through isolated protocol, page and local-log fixtures."""
import asyncio
import copy
import json
import shutil
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from app import create_app
from auth import AuthStore
from oas_client import OasClient, UpstreamError, account_identifier, identifier
from statistics_reader import LocalStatisticsReader
from tests.test_backend import FakeOas, FakeUpstreamSocket, request

DOTTED = '07-测试.7'
OTHER = '02-测试'
INVALID = ('.', '..', '.hidden', 'trailing.', 'a..b', 'a/b', r'a\b', 'a%2fb', 'a%252fb',
           'a:b', 'a?b', 'a#b', 'a b', 'a\nb', 'a\x00b', '', 'a' * 81)


class DottedClient(FakeOas):
    async def accounts(self):
        return [OTHER, DOTTED]

    async def settings(self, account, task):
        assert account == DOTTED and task == 'Chess'
        return copy.deepcopy(self.values)

    @asynccontextmanager
    async def connection(self, name):
        assert name == DOTTED
        self.socket = FakeUpstreamSocket()
        self.socket.queue.put_nowait('INFO | dotted account live log')
        yield self.socket


class DottedProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_account_dots_are_separate_from_strict_task_and_field_names(self):
        for value in (DOTTED, 'abc.1.2', '01-测试', 'account_01'):
            self.assertEqual(account_identifier(value), value)
        for value in (*INVALID, None, 7):
            with self.subTest(value=value), self.assertRaises(ValueError):
                account_identifier(value)
        self.assertEqual(identifier('scheduler'), 'scheduler')
        for value in (DOTTED, 'Task.Name', 'group.name', 'next.run'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                identifier(value)

    async def test_account_listing_preserves_ten_valid_names_and_filters_unsafe_entries(self):
        client = OasClient('http://127.0.0.1:22289')
        names = [f'{index:02d}-账号' for index in range(1, 11)]
        names[6] = DOTTED
        async def fixture(path, **kwargs):
            self.assertEqual(path, '/config_list')
            return [*names, 'template', *INVALID, None, 7]
        client._json = fixture
        self.assertEqual(await client.accounts(), names)

    async def test_http_settings_writes_and_statistics_quote_the_exact_dotted_account(self):
        client = OasClient('http://127.0.0.1:22289')
        calls = []
        async def fixture(path, **kwargs):
            calls.append((path, kwargs))
            return True if kwargs.get('method') == 'PUT' else {'script_name': DOTTED, 'dates': ['2026-10-07'], 'tasks': {}}
        client._json = fixture
        await client.settings(DOTTED, 'Chess')
        await client.set_value(DOTTED, 'Chess', 'chess_config', 'run_count', 2, 'integer')
        await client.statistics_dates(DOTTED)
        await client.statistics_day(DOTTED, '2026-10-07')
        name = quote(DOTTED, safe='')
        self.assertEqual([path for path, _ in calls], [f'/{name}/Chess/args', f'/{name}/Chess/chess_config/run_count/value',
                                                     f'/stats/{name}/dates', f'/stats/{name}'])
        for account in INVALID:
            with self.subTest(account=account), self.assertRaises(ValueError):
                await client.settings(account, 'Chess')
        for task, group, argument in [('Chess.X', 'chess_config', 'run_count'), ('Chess', 'chess.config', 'run_count'), ('Chess', 'chess_config', 'run.count')]:
            with self.assertRaises(ValueError):
                await client.set_value(DOTTED, task, group, argument, 2, 'integer')
        self.assertEqual(len(calls), 4, 'Invalid identifiers must not reach the upstream adapter')

    async def test_readonly_websocket_snapshot_uses_the_exact_dotted_path(self):
        client = OasClient('http://127.0.0.1:22289')
        urls = []
        class Socket:
            def __init__(self):
                self.messages = iter(['{"state":1}', '{"schedule":{"running":{},"pending":[]}}'])
            async def recv(self):
                return next(self.messages)
        @asynccontextmanager
        async def fixture(url, **kwargs):
            urls.append(url)
            yield Socket()
        with patch('oas_client.websocket_connect', fixture):
            result = await client.snapshot(DOTTED)
        self.assertTrue(result['connected'])
        self.assertEqual(urls, ['ws://127.0.0.1:22289/ws/' + quote(DOTTED, safe='')])

    async def test_transfer_keeps_account_dots_without_relaxing_task_validation(self):
        client = OasClient('http://127.0.0.1:22289')
        calls = []
        async def capabilities():
            return {'version': 1}
        async def fixture(path, **kwargs):
            calls.append((path, kwargs))
            return {'ok': True}
        client.transfer_capabilities = capabilities
        client._transfer_request = fixture
        await client.transfer_export(DOTTED, 'backup', 'Chess')
        await client.transfer_import(DOTTED, '{"fixture":{}}', 'Chess')
        self.assertEqual(calls[0][1]['query']['config_name'], DOTTED)
        self.assertIn(DOTTED.encode('utf-8'), calls[1][1]['payload'])
        with self.assertRaises(ValueError):
            await client.transfer_export(DOTTED, 'backup', 'Chess.X')
        self.assertEqual(len(calls), 2)


class DottedPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backend = self.root / 'backend'
        self.client = DottedClient()
        self.auth = AuthStore(self.root / 'state')
        self.app = create_app({'backend_url': 'http://127.0.0.1:22289', 'backend_root': str(self.backend),
                               'public_origin': 'https://panel.example.com:4443', 'port': 22300,
                               'authentication_required': False}, client=self.client, auth=self.auth)

    async def asyncTearDown(self):
        self.assertEqual(self.client.actions, [], 'These tests must never request any account start/stop')
        self.temp.cleanup()

    def error(self, record, metadata=None):
        folder = self.backend / 'log' / 'error' / record
        folder.mkdir(parents=True)
        (folder / 'log.txt').write_text('dotted account error fixture', encoding='utf-8')
        (folder / 'screen.png').write_bytes(b'fixture image')
        if metadata is not None:
            (folder / 'metadata.json').write_text(json.dumps(metadata), encoding='utf-8')

    async def test_bootstrap_settings_snapshot_statistics_and_value_update_accept_dots(self):
        for path in ('/api/bootstrap', f'/api/accounts/{DOTTED}/snapshot', f'/api/accounts/{DOTTED}/settings/Chess',
                     f'/api/accounts/{DOTTED}/statistics/dates', f'/api/accounts/{DOTTED}/statistics'):
            with self.subTest(path=path):
                status, data, _ = await request(self.app, 'GET', path, query={'date': '2026-10-07'})
                self.assertEqual(status, 200)
                if path == '/api/bootstrap':
                    self.assertEqual(data['accounts'], [OTHER, DOTTED])
        _, session, _ = await request(self.app, 'GET', '/api/session')
        status, _, _ = await request(self.app, 'PUT', f'/api/accounts/{DOTTED}/settings/Chess/chess_config/run_count',
                                      data={'value': 2, 'expected_value': 1}, csrf=session['csrf'])
        self.assertEqual(status, 200)
        self.assertEqual(self.client.writes[-1][:4], (DOTTED, 'Chess', 'chess_config', 'run_count'))
        for account in ('.', '..', 'bad..name', 'a%2fb', r'a\b'):
            status, _, _ = await request(self.app, 'GET', f'/api/accounts/{account}/snapshot')
            self.assertEqual(status, 404)
        status, _, _ = await request(self.app, 'GET', f'/api/accounts/{DOTTED}/settings/Chess.X')
        self.assertEqual(status, 404)

    async def test_metadata_legacy_prefix_and_account_logs_keep_errors_isolated(self):
        self.error('1791296542006', {'version': 1, 'config_name': DOTTED, 'task': 'Chess'})
        prefixed = DOTTED + '_1791296542007'
        self.error(prefixed)
        self.error('1791296542008')
        (self.backend / 'log' / ('2026-10-07_' + DOTTED + '.txt')).write_text(
            'WARNING | Saving error: ./log/error/1791296542008\n', encoding='utf-8')
        status, data, _ = await request(self.app, 'GET', '/api/errors', query={'config': DOTTED})
        self.assertEqual(status, 200)
        self.assertEqual([item['id'] for item in data['records']], ['1791296542008', prefixed, '1791296542006'])
        for record in ('1791296542006', prefixed, '1791296542008'):
            for suffix in ('', '/images/screen.png'):
                path = '/api/errors/' + record + suffix
                status, data, _ = await request(self.app, 'GET', path, query={'config': DOTTED})
                self.assertEqual(status, 200)
                status, _, _ = await request(self.app, 'GET', path, query={'config': OTHER})
                self.assertEqual(status, 404)
        self.error('bad..name_1791296542009')
        status, _, _ = await request(self.app, 'GET', '/api/errors/bad..name_1791296542009')
        self.assertEqual(status, 404)

    async def test_offline_error_owner_fallback_accepts_real_dotted_config_only(self):
        self.error('1791296542006', {'version': 1, 'config_name': DOTTED})
        config = self.backend / 'config'
        config.mkdir()
        (config / (DOTTED + '.json')).write_text('{}', encoding='utf-8')
        async def offline():
            raise UpstreamError('fixture backend offline')
        self.client.accounts = offline
        status, data, _ = await request(self.app, 'GET', '/api/errors', query={'config': DOTTED})
        self.assertEqual(status, 200)
        self.assertEqual(data['records'][0]['config_name'], DOTTED)
        status, _, _ = await request(self.app, 'GET', '/api/errors', query={'config': 'unknown.7'})
        self.assertEqual(status, 404)

    async def test_account_event_route_relays_dotted_account_logs_without_commands(self):
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        incoming.put_nowait({'type': 'websocket.connect'})
        path = f'/api/accounts/{DOTTED}/events'
        scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'ws', 'path': path,
                 'raw_path': quote(path, safe='/').encode(), 'query_string': b'', 'root_path': '',
                 'headers': [(b'host', b'127.0.0.1:22300'), (b'origin', b'http://127.0.0.1:22300')],
                 'client': ('127.0.0.1', 12000), 'server': ('127.0.0.1', 22300), 'subprotocols': []}
        task = asyncio.create_task(self.app(scope, incoming.get, outgoing.put))
        try:
            self.assertEqual((await asyncio.wait_for(outgoing.get(), 2))['type'], 'websocket.accept')
            while True:
                message = await asyncio.wait_for(outgoing.get(), 2)
                data = json.loads(message.get('text', '{}'))
                if data.get('type') == 'log':
                    self.assertEqual(data['line'], 'INFO | dotted account live log')
                    break
            self.assertEqual(self.client.socket.sent, [], 'Reading logs must not send control commands')
            incoming.put_nowait({'type': 'websocket.disconnect', 'code': 1000})
            await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class DottedLocalStatisticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_standalone_parser_reads_exact_dotted_account_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'module' / 'server'
            source.mkdir(parents=True)
            shutil.copyfile(Path(__file__).resolve().parent / 'fixtures' / 'log_stats.py', source / 'log_stats.py')
            (source / '__init__.py').write_text("raise RuntimeError('Do not load OAS runtime')", encoding='utf-8')
            (root / 'config').mkdir()
            (root / 'config' / (DOTTED + '.json')).write_text('{}', encoding='utf-8')
            (root / 'log').mkdir()
            log = root / 'log' / ('2026-10-07_' + DOTTED + '.txt')
            log.write_text('2026-10-07 10:00:00.000 | script.py:0001 | INFO | Scheduler: Start task `Chess`\n'
                           '2026-10-07 10:01:00.000 | script.py:0001 | INFO | Chess completed games: 1/1\n'
                           '2026-10-07 10:02:00.000 | script.py:0001 | INFO | Scheduler: End task `Chess`\n', encoding='utf-8')
            original = log.read_bytes()
            reader = LocalStatisticsReader(root)
            self.assertEqual((await reader.statistics_dates(DOTTED))['dates'], ['2026-10-07'])
            data = await reader.statistics_day(DOTTED, '2026-10-07')
            self.assertEqual(data['script_name'], DOTTED)
            self.assertEqual((data['total_task_run_count'], data['total_battle_count']), (1, 1))
            self.assertEqual(log.read_bytes(), original)
            for name in INVALID:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    await reader.statistics_dates(name)


if __name__ == '__main__':
    unittest.main()
