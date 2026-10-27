"""Weekly progress from explicit game evidence, never scheduler start/end counts."""
from datetime import date, timedelta
import re

TASKS = ('SixRealms', 'Chess', 'Dokan', 'Kirin', 'AbyssShadows', 'Netherworld')

def read_weekly(root, account, day, history=None, proofs=None):
    end = date.fromisoformat(day)
    start = end - timedelta(days=end.weekday())
    done = {task: set() for task in TASKS}
    boxes = set()
    coin = None
    coverage = True
    for offset in range((end-start).days+1):
        current = (start+timedelta(days=offset)).isoformat()
        path = root/'log'/f'{current}_{account}.txt'
        try:
            if path.is_symlink() or path.stat().st_size > 32*1024*1024:
                raise OSError('Unsafe or oversized log')
            lines = path.read_text(encoding='utf-8-sig').splitlines()
        except (OSError, UnicodeError):
            lines = []
            coverage = False
        active = None
        for line in lines:
            match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*\|\s*(\w+\.py):\d+\s*\|\s*\w+\s*\|\s*(.*?)\s*$', line)
            if not match or match[1][:10] != current:
                continue
            stamp, source, message = match.groups()
            event = re.fullmatch(r'Scheduler: Start task `(.+)`', message)
            if event:
                active = event[1]
            if source == 'daily_closeout.py' and message.startswith('Weekly task completed: '):
                task = message.removeprefix('Weekly task completed: ')
                if task in ('Dokan', 'AbyssShadows'):
                    done[task].add(current)
            if active == 'Hunt' and message in ('Battle win', 'Today have already challenged the Kirin', 'Today have already challenged the Netherworld'):
                task = 'Kirin' if date.fromisoformat(current).weekday() < 4 else 'Netherworld'
                done[task].add(current)
            if source == 'moon_sea.py' and message == 'Weekly realm chest consumed: 1':
                boxes.add(stamp)
            if active == 'Chess':
                result = re.search(r'(?:Chess coin: |\[COIN [^]]+\] \[)(\d{1,3})/600', message)
                if result and 0 <= int(result[1]) <= 600:
                    coin = int(result[1])
            if message.startswith('Scheduler: End task `') or 'process exit' in message:
                active = None
        if history:
            try:
                saved = history.load(account, current) or {}
                for row in saved.get('service_checklist', {}).get('tasks', []):
                    if row.get('task') in ('Dokan','AbyssShadows') and row.get('status') == 'completed' and '正常结束' not in row.get('reason',''):
                        done[row['task']].add(current)
            except (OSError, ValueError):
                coverage = False
        if (proofs or {}).get('date') == current:
            for task in ('Dokan','AbyssShadows'):
                if proofs.get('outcomes', {}).get(task, {}).get('outcome') == 'completed':
                    done[task].add(current)
    result = {}
    for task in TASKS:
        value = coin if task == 'Chess' else len(boxes) if task == 'SixRealms' else len(done[task])
        result[task] = {'value': value, 'total': 600 if task == 'Chess' else None,
                        'start': start.isoformat(), 'end': day,
                        'verified': value is not None and (task == 'Chess' or coverage and start >= date(2026, 10, 15)),
                        'note': '按周一至所选日期累计；仅统计有完成证据的记录。旧日志缺少宝箱消费证据时可能遗漏。'}
    return result
