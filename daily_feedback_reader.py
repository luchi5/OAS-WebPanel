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
    def __init__(self, backend_root: Path, *, now=None):
        self.root = Path(backend_root)
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
