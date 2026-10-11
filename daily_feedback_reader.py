"""Read only the public daily-verification evidence, never backend credentials.

This module deliberately does not import OAS. Reports can be read while OAS is
offline and malformed/stale evidence is never interpreted as zero or success.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import re
import stat

from oas_client import account_identifier, statistics_date, ACCOUNT_IDENTIFIER
from task_completion_reader import build_task_completion, _local_time
from task_history import TaskHistory
from service_checklist import build_service_checklist

SHANGHAI = timezone(timedelta(hours=8))
MAX_JSON = 2 * 1024 * 1024
MAX_IMAGE = 4 * 1024 * 1024
PNG_NAME = re.compile(r"^(?:talisman|collective)-[A-Za-z0-9_-]{1,120}\.png$")
TASK_FIELDS = {'Dokan': 'dokan', 'CollectiveMissions': 'collective_missions',
               'AbyssShadows': 'abyss_shadows', 'GuildBanquet': 'guild_banquet',
               'DemonRetreat': 'demon_retreat'}
TASK_LABELS = {'Dokan': '道馆', 'CollectiveMissions': '寮集体任务',
               'AbyssShadows': '峡间暗域', 'GuildBanquet': '寮宴会',
               'DemonRetreat': '首领退治'}
OUTCOMES = {'completed', 'expired', 'failed', 'skipped', 'blocked_by_dokan'}
STATUS_LABELS = {'pending': '待执行', 'running': '执行中', 'verified': '已核验',
                 'attention': '需要关注', 'disabled': '未启用', 'unverified': '未核验'}


class FeedbackNotFound(ValueError):
    pass


def checked_path(root: Path, *parts: str) -> Path:
    """Reject links/junctions at every level and paths escaping the trusted root."""
    if any(not part or part in ('.', '..') or '/' in part or '\\' in part for part in parts):
        raise FeedbackNotFound('无效路径')
    base = root.resolve()
    current = root
    try:
        for part in (None,) + parts:
            if part is not None:
                current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise FeedbackNotFound('链接文件不允许访问')
        resolved = current.resolve(strict=True)
        if resolved != base and base not in resolved.parents:
            raise FeedbackNotFound('无效路径')
        return resolved
    except OSError as exc:
        raise FeedbackNotFound('记录不存在') from exc


def read_json(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > MAX_JSON:
        raise ValueError('记录无效')
    with path.open('rb') as stream:
        data = stream.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise ValueError('记录过大')
    result = json.loads(data.decode('utf-8-sig'))
    if not isinstance(result, dict):
        raise ValueError('记录无效')
    return result


def integer(value):
    return value if type(value) is int and 0 <= value <= 10000 else None


def text(value, limit=240):
    # Generated descriptions should never contain credential-bearing URLs.
    if not isinstance(value, str):
        return ''
    value = re.sub(r'https?://\S+', '[链接已隐藏]', value)
    return ''.join(char for char in value if ord(char) >= 32 or char == '\n')[:limit]


def timestamp(value, day=None):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        local = parsed.astimezone(SHANGHAI) if parsed.tzinfo else parsed
        if day is not None and local.date().isoformat() != day:
            return None
        return value
    except ValueError:
        return None


def enabled(config, field):
    task = config.get(field, {})
    return isinstance(task, dict) and isinstance(task.get('scheduler'), dict) and task['scheduler'].get('enable') is True


def task_list(value):
    if not isinstance(value, list):
        return []
    items = [item.get('task') if isinstance(item, dict) else item for item in value]
    return list(dict.fromkeys(item for item in items if isinstance(item, str) and item in TASK_FIELDS))


class DailyFeedbackReader:
    def __init__(self, backend_root: Path, *, now=None, history_root=None):
        self.root = Path(backend_root)
        self.history = TaskHistory(history_root) if history_root is not None else None
        self.now = now or (lambda: datetime.now(SHANGHAI))

    def today(self):
        return self.now().astimezone(SHANGHAI).date().isoformat()

    def _config(self, account):
        account_identifier(account)
        if account == 'template':
            raise FeedbackNotFound('配置不存在')
        try:
            return read_json(checked_path(self.root, 'config', account + '.json'))
        except (OSError, ValueError) as exc:
            raise FeedbackNotFound('配置不存在或无法读取') from exc

    def accounts(self):
        try:
            paths = checked_path(self.root, 'config').iterdir()
            return sorted((path.stem for path in paths if path.suffix == '.json'
                           and path.stem != 'template' and ACCOUNT_IDENTIFIER.fullmatch(path.stem)
                           and self._readable_account(path.stem)), key=str.casefold)
        except (OSError, FeedbackNotFound):
            return []

    def _readable_account(self, account):
        try:
            self._config(account)
            return True
        except ValueError:
            return False

    @staticmethod
    def account_hash(account):
        return hashlib.sha256(account.encode('utf-8')).hexdigest()[:16]

    def _report(self, account, day):
        try:
            result = read_json(checked_path(self.root, 'config', 'daily_feedback', day,
                                          self.account_hash(account), 'report.json'))
            if type(result.get('schema_version')) is not int or result.get('schema_version') != 1 or result.get('account') != account or result.get('date') != day:
                raise ValueError('记录所属账号或日期不匹配')
            return result, False
        except FeedbackNotFound:
            return {}, False
        except (OSError, ValueError):
            return {}, True

    def _image_path(self, account, day, name):
        if not isinstance(name, str) or not PNG_NAME.fullmatch(name):
            raise FeedbackNotFound('图片不存在')
        path = checked_path(self.root, 'config', 'daily_feedback', day,
                            self.account_hash(account), name)
        if not path.is_file() or path.stat().st_size > MAX_IMAGE:
            raise FeedbackNotFound('图片不存在或过大')
        with path.open('rb') as stream:
            if stream.read(8) != b'\x89PNG\r\n\x1a\n':
                raise FeedbackNotFound('图片格式无效')
        return path

    def _evidence(self, account, day, kind, raw, expected):
        raw = raw if isinstance(raw, dict) else {}
        captured_at = timestamp(raw.get('captured_at'), day)
        image_name = raw.get('image')
        captured = raw.get('status') == 'captured' and captured_at is not None
        if captured:
            try:
                self._image_path(account, day, image_name)
            except FeedbackNotFound:
                captured = False
        current = integer(raw.get('current'))
        total = integer(raw.get('total')) if kind == 'collective' else None
        target = integer(raw.get('target')) if kind == 'talisman' else None
        target = target if target is not None and 1 <= target <= 300 else None
        valid_count = current is not None and (kind != 'collective' or total == 30 and current <= total)
        verified = captured and raw.get('verified') is True and valid_count
        # Unverified OCR values are deliberately withheld rather than displayed
        # as a persuasive but potentially false zero or completed count.
        if not verified:
            current = total = target = None
        return {'status': 'captured' if captured else 'unavailable', 'expected': expected,
                'captured_at': captured_at if captured else None, 'image': image_name if captured else None,
                'current': current, 'total': total, 'target': target, 'verified': verified,
                'detail': text(raw.get('detail')),
                'capture_error': text(raw.get('capture_error')),
                'last_attempt_at': timestamp(raw.get('last_attempt_at'), day),
                'final': raw.get('final') is True,
                'complete': verified and raw.get('final') is True and not raw.get('capture_error') and
                            (current == 30 if kind == 'collective' else target is None or current >= target)}

    def _fallback_closeout(self, config, account, day):
        weekday = date.fromisoformat(day).weekday()
        collective = config.get('collective_missions', {})
        options = collective.get('missions_config', {}) if isinstance(collective, dict) else {}
        options = options if isinstance(options, dict) else {}
        if weekday <= 3:
            candidates = ['Dokan']
            if enabled(config, 'dokan') and options.get('run_after_dokan') is True:
                candidates.append('CollectiveMissions')
        elif weekday in (4, 6):
            candidates = ['AbyssShadows', 'GuildBanquet']
        else:
            candidates = ['AbyssShadows', 'DemonRetreat']
        required = [item for item in candidates if enabled(config, TASK_FIELDS[item])]
        outcomes, queued = {}, False
        try:
            state = read_json(checked_path(self.root, 'config', 'daily_closeout', account + '.json'))
            if state.get('date') == day:
                outcomes = state.get('outcomes', {})
                outcomes = outcomes if isinstance(outcomes, dict) else {}
                queued = state.get('queued_date') == day and state.get('completed_date') != day
        except (OSError, ValueError):
            pass
        waiting, incomplete, completed = [], [], []
        for task in required:
            proof = outcomes.get(task, {})
            proof = proof if isinstance(proof, dict) else {}
            outcome = proof.get('outcome') if proof.get('date') == day else None
            if outcome == 'completed':
                completed.append(task)
            elif outcome in OUTCOMES:
                incomplete.append({'task': task, 'label': TASK_LABELS[task], 'outcome': outcome})
            else:
                waiting.append(task)
        return {'phase': 'provisional', 'at': None, 'required': required, 'waiting': waiting,
                'incomplete': incomplete, 'completed': completed, 'queued': queued}

    def _closeout(self, raw, fallback, day):
        if not isinstance(raw, dict):
            return fallback
        incomplete = []
        for item in raw.get('incomplete', []) if isinstance(raw.get('incomplete'), list) else []:
            if isinstance(item, dict) and isinstance(item.get('task'), str) and item.get('task') in TASK_FIELDS and isinstance(item.get('outcome'), str) and item.get('outcome') in OUTCOMES - {'completed'}:
                incomplete.append({'task': item['task'], 'label': TASK_LABELS[item['task']], 'outcome': item['outcome']})
        at = timestamp(raw.get('at'), day)
        return {'phase': 'final' if raw.get('phase') == 'final' and at else 'provisional',
                'at': at, 'required': task_list(raw.get('required')),
                'waiting': task_list(raw.get('waiting')), 'incomplete': incomplete,
                'completed': task_list(raw.get('completed')), 'queued': fallback['queued']}

    def _courtyard_proof(self, account, day):
        sidecar = None
        try:
            proof = read_json(checked_path(self.root, 'log', 'courtyard_affairs', account, day + '.json'))
            if (proof.get('schema_version') == 1 and proof.get('account') == account
                    and proof.get('task') == 'CourtyardAffairs' and proof.get('date') == day
                    and proof.get('source') == 'courtyard_affairs_task_v1'):
                if proof.get('outcome') == 'completed':
                    return proof
                sidecar = proof
        except (OSError, ValueError):
            pass
        # Legacy same-day attempts have no sidecar. Only a complete task
        # segment with explicit page/claim evidence can provide a fallback.
        try:
            path = checked_path(self.root, 'log', day + '_' + account + '.txt')
            with path.open('rb') as stream:
                size = path.stat().st_size
                stream.seek(max(0, size - MAX_JSON))
                if size > MAX_JSON:
                    stream.readline()
                text = stream.read(MAX_JSON).decode('utf-8', errors='replace')
        except (OSError, ValueError):
            return sidecar
        active = False
        result = None
        main = note = missing = done = after_main = intervening_click = False
        clicks = 0
        for line in text.splitlines():
            event = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\s*\|\s*([\w]+\.py):\d+\s*\|\s*(?:INFO|WARNING)\s*\|\s*(.*?)\s*$', line)
            if not event or event.group(1)[:10] != day:
                continue
            source, message = event.group(2), event.group(3)
            if source == 'script.py' and message == 'Scheduler: Start task `CourtyardAffairs`':
                active = True
                main = note = missing = done = after_main = intervening_click = False
                clicks = 0
                continue
            if not active:
                continue
            if source == 'logger.py' and message == '[UI] page_main' and not missing:
                main = True
            if missing and source == 'control.py' and message.startswith('Click ('):
                intervening_click = True
            if (missing and source == 'logger.py' and message.endswith('Page arrived page_main')
                    and not intervening_click):
                after_main = True
            if source == 'control.py' and message.startswith('Click (') and '@ COURTYARD_AFFAIRS_NOTE' in message:
                note = True
            if source == 'control.py' and message.startswith('Click (') and re.search(r'@ (?:COURTYARD_AFFAIRS_COMPLETE_TASKS|BLUE_COMPLETE_TASKS)\s*$', message):
                clicks += 1
            if source in ('script_task.py', 'selected_skin.py', 'courtyard_affairs.py') and message == 'Courtyard affairs entry was not found':
                missing = True
            if source in ('script_task.py', 'selected_skin.py', 'courtyard_affairs.py', 'completion_state.py') and (
                    message == 'Courtyard affairs completed' or message.endswith(
                        'courtyard daily affairs completed; disabled button and completed rows confirmed')):
                done = True
            if source == 'script.py' and message == 'Scheduler: End task `CourtyardAffairs`':
                active = False
                stamp = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                if not stamp:
                    continue
                outcome = ('completed' if done and clicks > 0 else 'already_complete' if done
                           else 'no_entry' if missing and main and after_main and not note and not intervening_click else 'failed')
                candidate = {'schema_version': 1, 'account': account, 'task': 'CourtyardAffairs',
                          'date': day, 'at': stamp.group(1), 'outcome': outcome,
                          'one_click_count': clicks, 'source': 'courtyard_affairs_log_v1'}
                if result is None or result['outcome'] != 'completed':
                    result = candidate
            elif source == 'script.py' and message.startswith('Scheduler: Start task `'):
                active = False
        if result is not None and result['outcome'] == 'completed':
            return result
        return sidecar if sidecar is not None else result

    def _area_boss_proof(self, account, day):
        try:
            proof = read_json(checked_path(self.root, 'log', 'area_boss_completion', account, day + '.json'))
            if (proof.get('account') == account and proof.get('date') == day
                    and proof.get('task') == 'AreaBoss' and proof.get('source') == 'area_boss_task_v1'):
                return proof
        except (OSError, ValueError):
            pass
        return None

    def _task_execution(self, account, day):
        # An End line also follows some handled failures: pair it with a start
        # and reject errors, recovery requests and worker boundaries.
        try:
            path = checked_path(self.root, 'log', day + '_' + account + '.txt')
            with path.open('rb') as stream:
                size = path.stat().st_size
                if size > 32 * 1024 * 1024:
                    return None
                raw = stream.read(size)
                if len(raw) != size:
                    return None
        except FeedbackNotFound as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return {'started': set(), 'finished': {}, 'kekkai_utilize': {
                    'schema_version': 1, 'source': 'kekkai_utilize_log_v1',
                    'task': 'KekkaiUtilize', 'date': day, 'count': 0, 'at': None}}
            return None
        except OSError:
            return None
        started, finished = set(), {}
        active = None
        failed = False
        foster_stage = None
        foster_events = {}
        now = self.now().astimezone(SHANGHAI)
        for line in raw.split(b'\n')[:-1]:
            if len(line) > 65536:
                return None
            text = line.decode('utf-8-sig', errors='replace').rstrip('\r')
            match = re.fullmatch(
                r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s*\|\s*([A-Za-z_][A-Za-z0-9_]*\.py):\d+\s*\|\s*(INFO|WARNING|ERROR|CRITICAL|DEBUG)\s*\|\s*(.*?)\s*', text)
            if not match or match.group(1)[:10] != day:
                continue
            try:
                stamp = datetime.fromisoformat(match.group(1)).replace(tzinfo=SHANGHAI)
            except ValueError:
                continue
            if stamp > now:
                continue
            source, level, message = match.group(2, 3, 4)
            if (source == 'script.py' and message.startswith('Start scheduler loop:')
                    or source == 'script_process.py' and re.fullmatch(r'Script .+ process exit', message)):
                active = None
                failed = False
                foster_stage = None
                continue
            event = re.fullmatch(r'Scheduler: (Start|End) task `([A-Z][A-Za-z0-9]{0,63})`', message)
            if source == 'script.py' and level == 'INFO' and event:
                action, task = event.groups()
                if action == 'Start':
                    started.add(task)
                    active = (task, stamp)
                    failed = False
                    foster_stage = None
                else:
                    if active and active[0] == task and stamp >= active[1] and not failed:
                        finished[task] = stamp.isoformat(timespec='seconds')
                    active = None
                    failed = False
                    foster_stage = None
                continue
            if active and (level in ('ERROR', 'CRITICAL')
                    or source == 'config.py' and message.casefold() == 'task call: restart'):
                failed = True
                foster_stage = None
            if active and active[0] == 'KekkaiUtilize' and not failed:
                if level == 'WARNING' and message == 'Ocr remaining time error':
                    foster_stage = None
                elif level == 'INFO' and source == 'script_task.py' and message == '开始执行进入结界蹭卡流程':
                    foster_stage = ('selected', stamp, stamp)
                elif foster_stage and stamp >= foster_stage[2] and stamp - foster_stage[1] <= timedelta(minutes=10):
                    phase, began, _ = foster_stage
                    if level == 'INFO' and source == 'script_task.py' and message == 'Enter friend realm' and phase == 'selected':
                        foster_stage = ('friend', began, stamp)
                    elif level == 'INFO' and source == 'replace_shikigami.py' and re.fullmatch(r'Set shikigami: [1-7]', message) and phase == 'friend':
                        foster_stage = ('placed', began, stamp)
                    elif level == 'INFO' and source == 'script_task.py' and phase == 'placed':
                        remaining = re.fullmatch(r'Utilize remaining time: (\d{1,2}):([0-5]\d):([0-5]\d)(?:\.\d{1,6})?', message)
                        if remaining:
                            hours, minutes, seconds = map(int, remaining.groups())
                            if 0 < hours * 3600 + minutes * 60 + seconds <= 24 * 3600:
                                # Count immediately after placement is confirmed, even
                                # if unrelated harvesting later makes this run fail.
                                foster_events[began.isoformat()] = stamp
                            foster_stage = None
        if len(foster_events) > 1000:
            return None
        last = max(foster_events.values()) if foster_events else None
        return {'started': started, 'finished': finished, 'kekkai_utilize': {
            'schema_version': 1, 'source': 'kekkai_utilize_log_v1',
            'task': 'KekkaiUtilize', 'date': day, 'count': len(foster_events),
            'at': last.isoformat(timespec='seconds') if last else None}}

    def _started_tasks(self, account, day):
        execution = self._task_execution(account, day)
        return execution['started'] if execution is not None else None

    def _task_completion(self, account, day, config, closeout):
        # Read the coordinator's independent proofs without importing OAS or
        # invoking its state helper, which would create directories and locks.
        proofs = {}
        try:
            state = read_json(checked_path(self.root, 'config', 'daily_closeout', account + '.json'))
            if state.get('date') == day and isinstance(state.get('outcomes'), dict):
                proofs = state
        except (OSError, ValueError):
            pass
        labels = {}
        for parts in (('module', 'config', 'i18n', 'zh-CN.json'), ('assets', 'i18n', 'zh-CN.json')):
            try:
                labels.update(read_json(checked_path(self.root, *parts)))
            except (OSError, ValueError):
                pass
        now = self.now()
        today = now.astimezone(SHANGHAI).date().isoformat()
        execution = self._task_execution(account, day)
        started = execution['started'] if execution is not None else None
        archived, archive_error = None, False
        if self.history is not None:
            try:
                archived = self.history.load(account, day)
            except (OSError, ValueError):
                archive_error = True
        historical = None
        if day < today:
            historical = set(started or ())
            if archived:
                historical.update(row['task'] for row in archived['tasks'])
        courtyard = self._courtyard_proof(account, day)
        area_boss = self._area_boss_proof(account, day)
        foster = execution['kekkai_utilize'] if execution is not None else None
        result = build_task_completion(config, day, today, proofs=proofs,
                                     closeout=closeout, labels=labels, now=now,
                                     courtyard=courtyard, area_boss=area_boss, kekkai_utilize=foster,
                                     started_tasks=started, planned_today=day == today,
                                     normal_ends=execution['finished'] if execution is not None else {},
                                     historical_tasks=historical)
        if started is None:
            result['note'] += ' 当天启动日志无法完整读取，相关执行情况待核验。'
        saved = {row['task']: row for row in archived['tasks']} if archived else {}
        for index, row in enumerate(result['tasks']):
            if row['task'] != 'KekkaiUtilize':
                continue
            old = saved.get(row['task'])
            old_count = old.get('completion_count') if old else None
            old_at = _local_time(old.get('evidence_at')) if old else None
            fresh_count = row.get('completion_count')
            if (type(old_count) is int and 0 < old_count <= 1000 and old_at is not None
                    and old_at.date().isoformat() == day and old_at <= now
                    and (type(fresh_count) is not int or old_count > fresh_count)):
                retained = dict(old)
                retained['status'] = 'unverified'
                retained['reason'] = f'当日已保存成功寄养 {old_count} 次；当前日志不足以核验完整次数，可能还有遗漏。'
                retained['status_label'] = f'{old_count}次'
                result['tasks'][index] = retained
        if day < today:
            for index, row in enumerate(result['tasks']):
                old = saved.get(row['task'])
                if old and row['task'] != 'KekkaiUtilize' and row['status'] != 'completed' and row['evidence_at'] is None:
                    result['tasks'][index] = old
            result['scope'] = 'historical_planned' if archived and archived['planned'] else 'historical_executed'
            result['note'] = ('展示当日保存的任务清单，并按当日日志更新结束状态。' if archived and archived['planned']
                              else '该日未保存完整计划清单；目前仅恢复日志中实际启动的任务，未启动任务无法完整还原。')
            if execution is None:
                result['note'] += ' 当日日志无法完整读取，当前显示已保存状态。'
            if archive_error:
                result['note'] += ' 历史存档无法读取，原文件已保留。'
            result['counts'] = {key: sum(row['status'] == key for row in result['tasks'])
                                for key in ('completed', 'incomplete', 'unverified')}
        else:
            result['counts'] = {key: sum(row['status'] == key for row in result['tasks'])
                                for key in ('completed', 'incomplete', 'unverified')}
        result['service_checklist'] = build_service_checklist(
            config, day, today, now=now, report=result, proofs=proofs,
            courtyard=courtyard, area_boss=area_boss, kekkai_utilize=foster,
            execution=execution, archived=archived.get('service_checklist') if archived else None)
        if self.history is not None:
            try:
                self.history.save(account, day, result, now.isoformat(), planned=day == today)
            except (OSError, ValueError):
                result['note'] += ' 任务历史保存失败，本次结果仍可查看。'
        return result

    def capture_plans(self):
        # Cheap config snapshots only: no log scan, backend request or account action.
        if self.history is None:
            return
        now = self.now().astimezone(SHANGHAI)
        day = now.date().isoformat()
        labels = {}
        for parts in (('module', 'config', 'i18n', 'zh-CN.json'), ('assets', 'i18n', 'zh-CN.json')):
            try:
                labels.update(read_json(checked_path(self.root, *parts)))
            except (OSError, ValueError):
                pass
        for account in self.accounts():
            try:
                config = self._config(account)
                report = build_task_completion(config, day, day, now=now,
                                               planned_today=True, started_tasks=set(), labels=labels)
                report['service_checklist'] = build_service_checklist(
                    config, day, day, now=now, report=report)
                self.history.save(account, day, report, now.isoformat(), planned=True, plan_only=True)
            except (OSError, ValueError):
                continue

    def account_day(self, account, day):
        statistics_date(day)
        config = self._config(account)
        report, damaged = self._report(account, day)
        talisman_enabled = enabled(config, 'talisman_pass')
        collective = config.get('collective_missions', {})
        options = collective.get('missions_config', {}) if isinstance(collective, dict) else {}
        options = options if isinstance(options, dict) else {}
        collective_enabled = enabled(config, 'collective_missions')
        collective_today = collective_enabled and not ((options.get('monday_to_thursday') is True or options.get('run_after_dokan') is True)
                                                       and date.fromisoformat(day).weekday() > 3)
        evidence_raw = report.get('evidence', {})
        evidence_raw = evidence_raw if isinstance(evidence_raw, dict) else {}
        evidence = {kind: self._evidence(account, day, kind, evidence_raw.get(kind), expected)
                    for kind, expected in [('talisman', talisman_enabled), ('collective', collective_today)]}
        closeout = self._closeout(report.get('closeout'), self._fallback_closeout(config, account, day), day)
        if evidence['collective']['complete']:
            # A fresh, double-checked 30/30 picture can verify this result even
            # when the coordinator was first enabled after today's guild run.
            closeout['waiting'] = [task for task in closeout['waiting'] if task != 'CollectiveMissions']
            closeout['incomplete'] = [item for item in closeout['incomplete'] if item['task'] != 'CollectiveMissions']
            if 'CollectiveMissions' not in closeout['completed']:
                closeout['completed'].append('CollectiveMissions')
        notification = report.get('notification', {})
        notification = notification if isinstance(notification, dict) else {}
        notification = {'status': notification.get('status') if notification.get('status') in
                        ('pending', 'sending', 'accepted', 'failed', 'disabled') else 'pending',
                        'at': timestamp(notification.get('at'))}
        expected = [item for item in evidence.values() if item['expected']]
        if damaged:
            status, reason = 'attention', '验收记录无法读取，请重新采集。'
        elif not expected and not report:
            status, reason = 'disabled', '此账号未启用今天的两项验收任务。'
        elif closeout['phase'] == 'final' and closeout['incomplete']:
            status, reason = 'attention', '当天收尾仍有未完成或未核验项目。'
        elif expected and all(item['complete'] for item in expected) and closeout['phase'] == 'final':
            status, reason = 'verified', '两项验收中已启用的项目均有游戏画面证据。'
            if evidence['talisman']['expected'] and evidence['talisman']['target'] is None:
                reason = '花合战今日经验已读取；目标未核验，不能判断是否达标。'
        elif closeout['phase'] == 'final':
            status, reason = 'attention', '收尾后仍有未核验或未达标的项目。'
        elif day != self.today():
            status, reason = 'unverified', '这一天尚无最终验收记录。' if report else '这一天没有验收记录。'
        elif report or closeout['queued']:
            status, reason = 'running', '尚未收尾，截图和进度可能继续更新。'
        elif day == self.today():
            status, reason = 'pending', '等待当天任务结束后采集游戏画面。'
        if notification['status'] == 'failed' and status == 'verified':
            status, reason = 'attention', '游戏结果已核验，手机推送未发送成功。'
        talisman_options = config.get('talisman_pass', {})
        talisman_options = talisman_options if isinstance(talisman_options, dict) else {}
        closeout_options = talisman_options.get('closeout_config', {})
        closeout_options = closeout_options if isinstance(closeout_options, dict) else {}
        return {'schema_version': 1, 'date': day, 'account': account,
                'updated_at': timestamp(report.get('updated_at')), 'status': status,
                'status_label': STATUS_LABELS[status], 'reason': reason,
                'has_report': bool(report), 'evidence': evidence, 'closeout': closeout,
                'task_completion': self._task_completion(account, day, config, closeout),
                'notification': notification,
                'settings': {'talisman_enabled': talisman_enabled, 'collective_enabled': collective_enabled,
                             'collective_today': collective_today,
                             'dynamic_closeout_enabled': talisman_enabled and closeout_options.get('enable') is True}}

    def day(self, day):
        statistics_date(day)
        priority = {'attention': 0, 'running': 1, 'pending': 2, 'unverified': 3, 'verified': 4, 'disabled': 5}
        rows = [self.account_day(account, day) for account in self.accounts()]
        rows.sort(key=lambda row: (priority[row['status']], row['account'].casefold()))
        counts = {status: sum(row['status'] == status for row in rows) for status in STATUS_LABELS}
        return {'date': day, 'today': self.today(), 'accounts': rows, 'counts': counts,
                'generated_at': self.now().isoformat()}

    def image(self, account, day, image):
        statistics_date(day)
        row = self.account_day(account, day)
        if image not in {item['image'] for item in row['evidence'].values() if item['status'] == 'captured'}:
            raise FeedbackNotFound('图片不属于这份验收记录')
        path = self._image_path(account, day, image)
        with path.open('rb') as stream:
            payload = stream.read(MAX_IMAGE + 1)
        if len(payload) > MAX_IMAGE or not payload.startswith(b'\x89PNG\r\n\x1a\n'):
            raise FeedbackNotFound('图片无效')
        return payload
