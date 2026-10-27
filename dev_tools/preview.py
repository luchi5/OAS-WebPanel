"""Local UI fixture: all accounts, settings, actions and logs stay in memory."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app
from auth import AuthStore, password_hash
from oas_client import OasClient

RUNTIME = ROOT / '.test-runtime' / 'preview'
PASSWORD = 'Preview-only-password-2026'


class Stream:
    def __init__(self, client, account):
        self.client, self.account = client, account
        self.queue = asyncio.Queue()
        self.queue.put_nowait(json.dumps({'state': client.states[account]}))
        self.queue.put_nowait(json.dumps({'schedule': client.schedule(account)}))
        self.queue.put_nowait('2026-10-05 16:42:01 | INFO | 百鬼棋局：等待准备阶段，当前第 3 回目')
        self.queue.put_nowait('INFO | 16:42:01.120 | [CHESS_MODE 0.003s] [备]')
        self.queue.put_nowait('ERROR | 16:42:01.130 | [ROUND 0.003s] 演示错误必须保留')
        self.queue.put_nowait('2026-10-05 16:42:02 | WARNING | <img src=x onerror=alert(1)> 这是一条转义测试日志')

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await asyncio.wait_for(self.queue.get(), 4)
        except asyncio.TimeoutError:
            return '2026-10-05 16:42:05 | INFO | 演示连接正常 · 不连接模拟器'


class FixtureOas:
    def __init__(self):
        self.states = {'演示一': 0, '演示二': 1}
        self.streams = []
        self.events = []
        self.values = {}

    async def accounts(self):
        return list(self.states)

    async def menu(self):
        return {'Script': ['Script', 'Restart', 'GlobalGame'], 'Daily Task': ['DailyTrifles', 'WantedQuests'],
                'Soul Zones': ['Orochi', 'EternitySea'], 'Weekly Task': ['Chess', 'Duel']}

    def schedule(self, account):
        return {'running': {'name': 'Chess', 'next_run': '2026-10-05 16:40:00'} if self.states[account] else {},
                'pending': [{'name': 'DailyTrifles', 'next_run': '2026-10-05 17:00:00'}],
                'waiting': [{'name': 'Orochi', 'next_run': '2026-10-05 18:00:00'},
                            {'name': 'Restart', 'next_run': '2026-10-06 08:00:00'}]}

    async def snapshot(self, account):
        return {'state': self.states[account], 'schedule': self.schedule(account), 'connected': True}

    async def statistics_dates(self, account):
        return {'script_name': account, 'dates': ['2026-10-05', '2026-10-04'] if account == '演示一' else [],
                'source': {'kind': 'local_logs', 'label': '演示 · 本地日志解析',
                           'detail': '以下全部为合成统计，不读取真实配置、日志或模拟器。'}}

    async def statistics_day(self, account, day):
        source = (await self.statistics_dates(account))['source']
        historical = day == '2026-10-04'
        task = {'run_count': 2, 'total_duration_seconds': 1200.0,
                'completed_run_count': 2, 'running_run_count': 0, 'interrupted_run_count': 0,
                'available_metrics': ['run_count', 'total_duration_seconds', 'battle_count'],
                'battle': {'count': 6, 'avg_duration_seconds': None},
                'runs': [{'start_time': day + ' 08:00:00', 'end_time': day + ' 08:10:00',
                          'duration_seconds': 600.0, 'status': 'completed',
                          'available_metrics': ['duration_seconds', 'battle_count'],
                          'battle': {'count': 3, 'avg_duration_seconds': None}},
                         {'start_time': day + ' 12:00:00', 'end_time': day + ' 12:10:00',
                          'duration_seconds': 600.0, 'status': 'completed',
                          'available_metrics': ['duration_seconds', 'battle_count'],
                          'battle': {'count': 3, 'avg_duration_seconds': None}}]}
        chess = {'run_count': 1, 'total_duration_seconds': None,
                 'completed_run_count': 0, 'running_run_count': 0, 'interrupted_run_count': 0, 'incomplete_run_count': 1,
                 'available_metrics': ['run_count', 'battle_count'],
                 'battle': {'count': 3, 'avg_duration_seconds': None},
                 'runs': [{'start_time': day + ' 16:40:00', 'end_time': '', 'duration_seconds': None,
                           'status': 'incomplete', 'status_reason': 'unclosed_interval', 'available_metrics': ['battle_count'],
                           'battle': {'count': 3, 'avg_duration_seconds': None}}]}
        return {'script_name': account, 'date': day, 'source': source,
                'total_runtime_seconds': 1200.0 if historical else None,
                'total_task_run_count': 2 if historical else 3, 'total_battle_count': 6 if historical else 9,
                'available_metrics': ['total_task_run_count', 'total_battle_count'] + (['total_runtime_seconds'] if historical else []),
                'tasks': {'Orochi': task} if historical else {'Orochi': task, 'Chess': chess}}

    async def settings(self, account, task):
        value = self.values.setdefault((account, task), {
            'costume_config': [
                {'name': 'costume_main_type', 'title': '庭院皮肤', 'description': '可多选，自动识别当前庭院；至少保留一个。', 'type': 'multi_enum', 'enumEnum': ['costume_main', 'costume_main_13', 'costume_main_17'], 'minItems': 1, 'value': ['costume_main']},
            ],
            'scheduler': [
                {'name': 'enable', 'title': 'Enable', 'description': 'enable_help', 'type': 'boolean', 'value': False},
                {'name': 'next_run', 'title': 'Next Run', 'description': 'next_run_help', 'type': 'date_time', 'value': '2026-10-05 18:00:00'},
                {'name': 'priority', 'title': 'Priority', 'description': 'priority_help', 'type': 'integer', 'value': 5},
            ],
            'chess_config': [
                {'name': 'lineup_bond', 'title': '选择阵容羁绊', 'description': '选择本次棋局使用的阵容。', 'type': 'enum', 'enumEnum': ['荒川', '大江山', '海国'], 'value': '荒川'},
                {'name': 'run_count', 'title': '执行次数', 'description': '填写局数；-1 表示不限次数。', 'type': 'integer', 'value': 1},
                {'name': 'limit_time', 'title': '运行时间限制', 'description': '格式：时:分:秒。', 'type': 'time', 'value': '00:30:00'},
                {'name': 'coin_full_exit', 'title': '刷满鼬乐币', 'description': '满币后结束当前任务。', 'type': 'boolean', 'value': True},
                {'name': 'note', 'title': '测试备注', 'description': '仅保存在演示进程内。', 'type': 'multi_line', 'value': ''},
            ],
        })
        return copy.deepcopy(value)

    async def set_value(self, account, task, group, argument, value, kind):
        await self.settings(account, task)
        row = next(row for row in self.values[(account, task)][group] if row['name'] == argument)
        row['value'] = value
        self.record({'action': 'save', 'account': account, 'task': task, 'field': argument, 'value': value})

    async def action(self, account, action):
        self.states[account] = 1 if action == 'start' else 0
        self.record({'action': action, 'account': account})
        for stream in self.streams:
            if stream.account == account:
                stream.queue.put_nowait(json.dumps({'state': self.states[account]}))
                stream.queue.put_nowait(json.dumps({'schedule': self.schedule(account)}))
        return {'ok': True, 'state': self.states[account], 'changed': True}

    def record(self, event):
        self.events.append(event)
        (RUNTIME / 'events.json').write_text(json.dumps(self.events, ensure_ascii=False), encoding='utf-8')

    event = staticmethod(OasClient.event)

    @asynccontextmanager
    async def connection(self, account):
        stream = Stream(self, account)
        self.streams.append(stream)
        try:
            yield stream
        finally:
            self.streams.remove(stream)


def main():
    import uvicorn
    backend = RUNTIME / 'backend'
    labels = backend / 'assets' / 'i18n'
    labels.mkdir(parents=True, exist_ok=True)
    (labels / 'zh-CN.json').write_text(json.dumps({
        'Chess': '百鬼棋局', 'Script': '脚本设置', 'Restart': '重启', 'GlobalGame': '全局配置', 'Daily Task': '日常任务',
        'Soul Zones': '御魂副本', 'Weekly Task': '每周任务', 'DailyTrifles': '日常杂事',
        'WantedQuests': '悬赏封印', 'Orochi': '八岐大蛇', 'EternitySea': '永生之海', 'Duel': '斗技',
        'scheduler': '任务调度', 'Enable': '启用任务', 'enable': '启用任务', 'Next Run': '下次运行',
        'Priority': '优先级', 'chess_config': '百鬼棋局配置',
        'costume_config': '游戏装饰自定义', 'costume_main': '初语谧景',
        'costume_main_13': '云景阆苑', 'costume_main_17': '玉岚狐庭',
    }, ensure_ascii=False), encoding='utf-8')
    error = backend / 'log' / 'error' / '1791188400000'
    error.mkdir(parents=True, exist_ok=True)
    (error / 'log.txt').write_text('演示错误记录\nGameStuckError: 等待页面加载超时\n此记录仅用于网页测试，不来自真实游戏。', encoding='utf-8')
    auth = AuthStore(RUNTIME / 'state')
    with auth._db() as db:
        db.execute('UPDATE users SET password_hash=?,must_change=0 WHERE username=?', (password_hash(PASSWORD), 'admin'))
    settings = {'host': '127.0.0.1', 'port': 22301, 'public_origin': 'https://panel.example.com',
                'backend_url': 'http://127.0.0.1:22289', 'backend_root': str(backend),
                'state_dir': str(RUNTIME / 'state')}
    fake = FixtureOas()
    fake.record({'action': 'fixture_started', 'real_backend_access': False})
    uvicorn.run(create_app(settings, client=fake, auth=auth), host='127.0.0.1', port=22301, proxy_headers=False, access_log=False)


if __name__ == '__main__':
    main()
