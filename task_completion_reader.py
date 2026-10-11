"""Pure, conservative completion views from already-read account/proof data.

No runtime imports, filesystem access, network access, or task control. Scheduler
returns and future scheduling never establish a game-confirmed completion.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
import re


SHANGHAI = timezone(timedelta(hours=8))
TASK_CATALOG = {
    'area_boss': 'AreaBoss', 'experience_youkai': 'ExperienceYoukai',
    'gold_youkai': 'GoldYoukai', 'nian': 'Nian', 'realm_raid': 'RealmRaid',
    'ryou_toppa': 'RyouToppa', 'kekkai_utilize': 'KekkaiUtilize',
    'kekkai_activation': 'KekkaiActivation', 'demon_encounter': 'DemonEncounter',
    'daily_trifles': 'DailyTrifles', 'courtyard_affairs': 'CourtyardAffairs',
    'pets': 'Pets', 'souls_tidy': 'SoulsTidy', 'delegation': 'Delegation',
    'exploration': 'Exploration', 'wanted_quests': 'WantedQuests', 'tako': 'Tako',
    'auto_checkin_big_god': 'AutoCheckinBigGod', 'orochi': 'Orochi',
    'orochi_moans': 'OrochiMoans', 'sougenbi': 'Sougenbi', 'fallen_sun': 'FallenSun',
    'eternity_sea': 'EternitySea', 'six_realms': 'SixRealms',
    'activity_shikigami': 'ActivityShikigami', 'meta_demon': 'MetaDemon',
    'frog_boss': 'FrogBoss', 'float_parade': 'FloatParade', 'quiz': 'Quiz',
    'kitty_shop': 'KittyShop', 'dye_trials': 'DyeTrials',
    'bondling_fairyland': 'BondlingFairyland', 'evo_zone': 'EvoZone',
    'goryou_realm': 'GoryouRealm', 'hyakkiyakou': 'Hyakkiyakou',
    'hero_test': 'HeroTest', 'find_jade': 'FindJade', 'memory_scrolls': 'MemoryScrolls',
    'true_orochi': 'TrueOrochi', 'rich_man': 'RichMan', 'secret': 'Secret',
    'weekly_trifles': 'WeeklyTrifles', 'mystery_shop': 'MysteryShop',
    'duel': 'Duel', 'chess': 'Chess', 'hunt': 'Hunt', 'dokan': 'Dokan',
    'abyss_shadows': 'AbyssShadows', 'guild_banquet': 'GuildBanquet',
    'demon_retreat': 'DemonRetreat', 'guild_activity_monitor': 'GuildActivityMonitor',
    'budokai_tournament': 'BudokaiTournament',
}
EXCLUDED_FIELDS = frozenset({
    'script', 'restart', 'global_game', 'goto_main', 'game_ui', 'general',
    'component', 'utils', 'notifier', 'emulator', 'device',
    'talisman_pass', 'collective_missions',
})
STATUS_LABELS = {'completed': '已完成', 'incomplete': '未完成', 'unverified': '待核验'}
DEFAULT_LABELS = {
    'Dokan': '道馆', 'AbyssShadows': '峡间暗域', 'GuildBanquet': '寮宴会',
    'DemonRetreat': '首领退治', 'CourtyardAffairs': '庭院事务', 'AreaBoss': '地域鬼王',
}
COMPLETION_DETAILS = {
    'Dokan': frozenset({'final_challenge_confirmed'}),
    'AbyssShadows': frozenset({'actual_battle_quota_confirmed'}),
    'GuildBanquet': frozenset({'entered_banquet_ended'}),
    'DemonRetreat': frozenset({'battle_win_confirmed'}),
}
COMPLETION_REASONS = {
    'Dokan': '已确认当天配置的最终挑战。',
    'AbyssShadows': '已确认实际战斗达到任务配额。',
    'GuildBanquet': '已确认参加并结束宴会；不代表经验已满。',
    'DemonRetreat': '已确认首领退治战斗胜利。',
}
FIELD_NAME = re.compile(r'^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$')
DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
PROOF_TIME = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$')
ABYSS_REMAINING = re.compile(
    r'^targets_exhausted_before_quota:\s*boss=(\d{1,3})/2,\s*'
    r'general=(\d{1,3})/4,\s*elite=(\d{1,3})/6$'
)


def _day(value):
    if not isinstance(value, str) or not DAY.fullmatch(value):
        raise ValueError('Expected a YYYY-MM-DD date')
    date.fromisoformat(value)
    return value


def _local_time(value):
    if not isinstance(value, str) or len(value) > 40 or not PROOF_TIME.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None
                else parsed.astimezone(SHANGHAI))
    except (ValueError, OverflowError):
        return None


def _label(task, field, labels):
    value = labels.get(task, labels.get(field)) if isinstance(labels, Mapping) else None
    if isinstance(value, str) and 0 < len(value) <= 120 and not any(ord(c) < 32 for c in value):
        return value
    return DEFAULT_LABELS.get(task, task)


def _proofs_for_day(proofs, day):
    if not isinstance(proofs, Mapping) or proofs.get('date') != day:
        return {}
    outcomes = proofs.get('outcomes')
    return outcomes if isinstance(outcomes, Mapping) else {}


def _task_status(task, proof, day, now):
    if not isinstance(proof, Mapping) or proof.get('date') != day:
        return 'unverified', '尚无可信的同日完成依据。', None
    if task == 'KekkaiUtilize':
        count = proof.get('count')
        if (proof.get('schema_version') != 1 or proof.get('source') != 'kekkai_utilize_log_v1'
                or proof.get('task') != task or type(count) is not int or not 0 <= count <= 1000):
            return 'unverified', '当日成功寄养次数尚无法核验。', None
        if count == 0 and proof.get('at') is None:
            return 'incomplete', '今日尚未记录到脚本成功新寄养。', None
        at = _local_time(proof.get('at'))
        if count <= 0 or at is None or at.date().isoformat() != day or at > now:
            return 'unverified', '成功寄养次数的日期或时间无法核验。', None
        return 'completed', f'今日已确认成功新寄养 {count} 次；已有寄养的检查和只收取奖励不累计。', at.isoformat(timespec='seconds')
    at = _local_time(proof.get('at'))
    if at is None or at.date().isoformat() != day or at > now:
        return 'unverified', '完成依据的日期或时间无法核验。', None
    if task == 'AreaBoss':
        target, reputation, bosses = proof.get('target'), proof.get('reputation'), proof.get('bosses')
        if (proof.get('schema_version') != 1 or proof.get('source') != 'area_boss_task_v1'
                or proof.get('task') != task or type(target) is not int or target not in (1, 2, 3)
                or type(reputation) is not int or reputation < 0
                or target != (3 if reputation >= 10000 else 2 if reputation >= 2000 else 1)
                or not isinstance(bosses, list) or len(bosses) > 100):
            return 'unverified', '地域鬼王目标数或记录尚无法核验。', None
        names = set()
        for entry in bosses:
            if not isinstance(entry, Mapping):
                return 'unverified', '地域鬼王单只记录无法核验。', None
            name, stamp = entry.get('name'), _local_time(entry.get('at'))
            if (not isinstance(name, str) or not 2 <= len(name) <= 80
                    or entry.get('basis') not in ('existing_rank', 'battle_success')
                    or stamp is None or stamp.date().isoformat() != day or stamp > now or stamp > at):
                return 'unverified', '地域鬼王单只记录无法核验。', None
            names.add(name)
        count = len(names)
        if type(proof.get('completed_count')) is not int or proof['completed_count'] != count:
            return 'unverified', '地域鬼王数量记录不一致。', None
        unknown = proof.get('unidentified_successes', 0)
        if type(unknown) is not int or unknown < 0:
            return 'unverified', '地域鬼王数量记录不一致。', None
        if count >= target:
            return 'completed', f'今日已成功处理 {count}/{target} 只地域鬼王（已有排名或战斗成功，按鬼王去重）。', at.isoformat(timespec='seconds')
        if unknown:
            return 'unverified', f'今日已核验 {count}/{target} 只；另有成功结果未识别到鬼王名称，无法去重核验。', at.isoformat(timespec='seconds')
        return 'incomplete', f'今日已成功处理 {count}/{target} 只地域鬼王，尚未达到声望对应目标。', at.isoformat(timespec='seconds')
    if task == 'CourtyardAffairs':
        count = proof.get('one_click_count')
        if (proof.get('schema_version') != 1 or proof.get('task') != task
                or proof.get('source') not in ('courtyard_affairs_task_v1', 'courtyard_affairs_log_v1')
                or type(count) is not int or not 0 <= count <= 100):
            return 'unverified', '庭院事务依据无法核验。', None
        outcome = proof.get('outcome')
        stamp = at.isoformat(timespec='seconds')
        if outcome == 'completed' and count > 0:
            return 'completed', '本次已点击一键完成，并核验到完成状态。', stamp
        if outcome == 'no_entry' and count == 0:
            return 'unverified', '庭院主页未找到事务入口；本次正常结束，下一天仍按设定时间检查。', stamp
        if outcome == 'already_complete' and count == 0:
            return 'unverified', '页面已无可领取；本次没有点击一键完成，不记作脚本领取完成。', stamp
        if outcome == 'failed':
            return 'unverified', '执行后尚未核验到完成状态，不能只凭点击或弹窗判定完成。', stamp
        return 'unverified', '庭院事务记录缺少有效领取完成证据。', None
    outcome, detail = proof.get('outcome'), proof.get('detail')
    if (outcome == 'completed' and isinstance(detail, str)
            and detail in COMPLETION_DETAILS.get(task, ())):
        return 'completed', COMPLETION_REASONS[task], at.isoformat(timespec='seconds')
    if task == 'AbyssShadows' and outcome == 'skipped' and isinstance(detail, str):
        remaining = ABYSS_REMAINING.fullmatch(detail)
        if remaining and any(int(value) < goal for value, goal in zip(remaining.groups(), (2, 4, 6))):
            return 'incomplete', '已记录实际战斗配额未达；后续尚无完成确认。', at.isoformat(timespec='seconds')
    if outcome in ('expired', 'failed', 'skipped', 'blocked_by_dokan'):
        return 'unverified', '已有超时、失败或跳过记录，实际完成情况仍待核验。', at.isoformat(timespec='seconds')
    return 'unverified', '已有记录，但缺少可核验的游戏完成依据。', at.isoformat(timespec='seconds')


def build_task_completion(config, day, today, *, proofs=None, closeout=None, labels=None, now=None, courtyard=None, area_boss=None, kekkai_utilize=None, started_tasks=None, planned_today=False, normal_ends=None, historical_tasks=None):
    """Return today's enabled non-picture tasks; never infer historical scope.

``proofs`` is the already-read daily_closeout JSON (date plus outcomes). An
archived closeout summary alone lacks each task's confirmation detail, so the
``closeout`` argument deliberately cannot promote a task to completed.
"""
    day, today = _day(day), _day(today)
    result = {
        'scope': 'historical_executed' if historical_tasks is not None else 'scheduled_today' if planned_today else 'executed_today' if started_tasks is not None else 'enabled_today', 'date': day, 'available': day == today or historical_tasks is not None and day < today,
        'tasks': [], 'counts': {status: 0 for status in STATUS_LABELS},
        'note': '仅展示今天已启用的其它任务；缺少完成证据时显示待核验。花合战和寮30见截图区。',
    }
    if planned_today:
        result['note'] = '展示今日应运行及已运行的已启用任务；尚未开始显示未完成，已执行但缺少完成证据显示待核验。花合战和寮30见截图区。'
    elif started_tasks is not None:
        result['note'] = '仅展示今天实际启动执行过的其它任务；启动或结束不代表完成。花合战和寮30见截图区。'
    if day != today and (historical_tasks is None or day > today):
        result['note'] = '当前只展示今日任务完成情况；历史截图验收仍可查看。' if started_tasks is not None else '未保存这一天的任务启用范围，无法核验其他任务；历史截图验收仍可查看。'
        return result
    if not isinstance(config, Mapping):
        result['available'] = False
        result['note'] = '当前任务配置无法读取，其他任务完成情况待核验。'
        return result
    if now is None:
        now = datetime.now(SHANGHAI)
    elif not isinstance(now, datetime):
        raise ValueError('now must be a datetime')
    now = now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)
    outcomes = _proofs_for_day(proofs, day)
    items = config.items()
    if historical_tasks is not None or started_tasks is not None and not planned_today:
        reverse_catalog = {task: field for field, task in TASK_CATALOG.items()}
        actual = {task for task in (historical_tasks if historical_tasks is not None else started_tasks) if isinstance(task, str) and re.fullmatch(r'[A-Z][A-Za-z0-9]{0,63}', task)}
        fields = {reverse_catalog.get(task, re.sub(r'(?<!^)(?=[A-Z])', '_', task).lower()) for task in actual}
        items = [(field, config.get(field) if isinstance(config.get(field), Mapping) else {}) for field in sorted(fields)]
    for field, task_config in items:
        if (not isinstance(field, str) or len(field) > 128 or not FIELD_NAME.fullmatch(field)
                or field in EXCLUDED_FIELDS or not isinstance(task_config, Mapping)):
            continue
        scheduler = task_config.get('scheduler')
        if historical_tasks is None and (planned_today or started_tasks is None) and (not isinstance(scheduler, Mapping) or scheduler.get('enable') is not True):
            continue
        task = TASK_CATALOG.get(field, ''.join(part[:1].upper() + part[1:] for part in field.split('_')))
        proof = courtyard if task == 'CourtyardAffairs' else area_boss if task == 'AreaBoss' else kekkai_utilize if task == 'KekkaiUtilize' else outcomes.get(task)
        started = task in (started_tasks or ())
        if planned_today:
            # Match explicit task calendar guards without importing runtime code.
            weekday = date.fromisoformat(day).weekday()
            if task == 'AbyssShadows' and weekday not in (4, 5, 6):
                continue
            if task == 'Dokan' and isinstance(task_config.get('dokan_config'), Mapping) and task_config['dokan_config'].get('monday_to_thursday') is True and weekday >= 4:
                continue
            next_run = _local_time(scheduler.get('next_run'))
            same_day_proof = isinstance(proof, Mapping) and proof.get('date') == day
            if task == 'KekkaiUtilize' and same_day_proof:
                # A synthetic zero from scanning a day is not evidence that
                # this task belongs to today's schedule.
                same_day_proof = type(proof.get('count')) is int and proof['count'] > 0
            # Completed jobs move next_run forward; today's execution/evidence
            # keeps them visible. Future-only jobs belong to a later day.
            if not started and not same_day_proof and next_run is not None and next_run.date().isoformat() > day:
                continue
        status, reason, at = _task_status(task, proof, day, now)
        if planned_today and not started and proof is None:
            if next_run is None:
                status, reason = 'unverified', '任务已启用，但今日安排时间无法核验。'
            elif started_tasks is None:
                status, reason = 'unverified', '任务今日应运行，但启动日志无法完整读取，执行情况待核验。'
            else:
                status, reason = 'incomplete', '今日计划任务尚未开始。'
        ended = _local_time(normal_ends.get(task)) if isinstance(normal_ends, Mapping) else None
        if (task != 'KekkaiUtilize' and proof is None and ended is not None and ended.date().isoformat() == day
                and ended <= now):
            status, reason, at = ('completed', '按脚本正常结束判定；尚未核验全部游戏目标。',
                                  ended.isoformat(timespec='seconds'))
        status_label = STATUS_LABELS[status]
        if task == 'CourtyardAffairs' and at is not None and status == 'unverified':
            status_label = {'no_entry': '没找到入口', 'already_complete': '已无可领取',
                            'failed': '未确认完成'}.get(proof.get('outcome'), status_label)
        row = {
            'task': task, 'label': _label(task, field, labels), 'status': status,
            'status_label': status_label, 'reason': reason, 'evidence_at': at,
        }
        if task == 'KekkaiUtilize':
            count = proof.get('count') if isinstance(proof, Mapping) and status != 'unverified' else None
            if count is None and status == 'incomplete' and planned_today and not started and proof is None:
                count = 0
            row['status_label'] = f'{count}次' if type(count) is int else '待核验'
            if type(count) is int:
                row['completion_count'] = count
        result['tasks'].append(row)
        result['counts'][status] += 1
    priority = {'incomplete': 0, 'completed': 1, 'unverified': 2}
    result['tasks'].sort(key=lambda row: (priority[row['status']], row['task']))
    return result
