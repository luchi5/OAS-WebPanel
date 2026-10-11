"""Fixed poster checklist; only interprets already-read configuration/evidence."""
from collections.abc import Mapping
from datetime import date
from task_completion_reader import _local_time, _task_status

# The first five daily entries follow the user's explicit order.
SERVICE_TASKS = (
    ('WantedQuests', 'wanted_quests', 'WantedQuests', '悬赏封印', 'daily'),
    ('KekkaiUtilize', 'kekkai_utilize', 'KekkaiUtilize', '结界蹭卡', 'daily'),
    ('KekkaiActivation', 'kekkai_activation', 'KekkaiActivation', '结界挂卡', 'daily'),
    ('RealmRaid', 'realm_raid', 'RealmRaid', '个人突破', 'daily'),
    ('AreaBoss', 'area_boss', 'AreaBoss', '地域鬼王', 'daily'),
    ('DailySign', 'restart', 'Restart', '每日签到', 'daily'),
    ('Delegation', 'delegation', 'Delegation', '式神委派', 'daily'),
    ('CourtyardAffairs', 'courtyard_affairs', 'CourtyardAffairs', '庭院事务', 'daily'),
    ('LuckMessage', 'daily_trifles', 'DailyTrifles', '吉闻点赞', 'daily'),
    ('DemonEncounter', 'demon_encounter', 'DemonEncounter', '逢魔之时', 'daily'),
    ('GoldYoukai', 'gold_youkai', 'GoldYoukai', '金币妖怪', 'daily'),
    ('PetSoul', 'orochi', 'Orochi', '小动物助战御魂', 'daily'),
    ('TalismanPass', 'talisman_pass', 'TalismanPass', '花合战领取', 'daily'),
    ('Dokan', 'dokan', 'Dokan', '道馆突破', 'weekly'),
    ('AbyssShadows', 'abyss_shadows', 'AbyssShadows', '狭间暗域', 'weekly'),
    ('Netherworld', 'hunt', 'Hunt', '阴界之门', 'weekly'),
    ('GuildBanquet', 'guild_banquet', 'GuildBanquet', '寮宴会', 'weekly'),
    ('TrueOrochi', 'true_orochi', 'TrueOrochi', '真蛇', 'weekly'),
    ('DemonRetreat', 'demon_retreat', 'DemonRetreat', '首领退治', 'weekly'),
    ('Kirin', 'hunt', 'Hunt', '麒麟', 'weekly'),
    ('Chess', 'chess', 'Chess', '百鬼棋局（600鼬乐币）', 'weekly'),
    ('BondlingFairyland', 'bondling_fairyland', 'BondlingFairyland', '契灵之境（2000契忆）', 'weekly'),
    ('SixRealms', 'six_realms', 'SixRealms', '六道之门', 'weekly'),
    ('Secret', 'secret', 'Secret', '秘闻竞速／百战', 'weekly'),
)
LABELS = {'completed': '已完成', 'incomplete': '未完成', 'unverified': '待核验',
          'disabled': '未开启', 'not_scheduled': '今日无需运行'}
WEEKDAYS = {f'星期{x}': i for i, x in enumerate('一二三四五六日')}


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _enabled(task, options):
    if task == 'DailySign':
        harvest = _mapping(options.get('harvest_config'))
        return harvest.get('enable') is True and harvest.get('enable_sign') is True
    value = _mapping(options.get('scheduler')).get('enable') is True
    if task == 'LuckMessage':
        value = value and _mapping(options.get('trifles_config')).get('luck_msg') is True
    return value


def _calendar(task, options, weekday):
    if task == 'Netherworld':
        return weekday >= 4
    if task == 'Kirin':
        return weekday <= 3
    if task == 'AbyssShadows':
        return weekday >= 4
    if task == 'Dokan' and _mapping(options.get('dokan_config')).get('monday_to_thursday') is True:
        return weekday <= 3
    if task == 'GuildBanquet':
        days = _mapping(options.get('guild_banquet_time'))
        valid = {WEEKDAYS[days[k]] for k in ('day_1', 'day_2') if days.get(k) in WEEKDAYS}
        return weekday in valid if valid else True
    return True


def validate_checklist(value, day):
    """Validate minimal historical rows without persisting any account settings."""
    if not isinstance(value, dict) or value.get('date') != day or value.get('scope') != 'poster_checklist':
        raise ValueError('Checklist identity invalid')
    tasks = value.get('tasks')
    if not isinstance(tasks, list) or len(tasks) != len(SERVICE_TASKS):
        raise ValueError('Checklist tasks invalid')
    cleaned = []
    for row, spec in zip(tasks, SERVICE_TASKS):
        if not isinstance(row, dict) or row.get('task') != spec[0] or row.get('group') != spec[4]:
            raise ValueError('Checklist row invalid')
        status = row.get('status')
        if status not in LABELS:
            raise ValueError('Checklist status invalid')
        item = dict(row)
        for key, limit in (('label', 120), ('status_label', 120), ('reason', 1000)):
            if not isinstance(item.get(key), str) or len(item[key]) > limit:
                raise ValueError('Checklist text invalid')
        stamp = _local_time(item.get('evidence_at'))
        if item.get('evidence_at') is not None and (stamp is None or stamp.date().isoformat() != day):
            raise ValueError('Checklist evidence invalid')
        if status == 'completed' and stamp is None:
            raise ValueError('Checklist completion lacks evidence')
        if 'completion_count' in item:
            count = item['completion_count']
            if (spec[0] != 'KekkaiUtilize' or type(count) is not int or not 0 <= count <= 1000
                    or count > 0 and stamp is None or count == 0 and status == 'completed'):
                raise ValueError('Checklist foster count invalid')
        # Whitelist the minimal view fields rather than storing arbitrary metadata.
        cleaned.append({k: item[k] for k in ('task', 'group', 'label', 'status',
                       'status_label', 'reason', 'evidence_at', 'completion_count') if k in item})
    return {'scope': 'poster_checklist', 'date': day, 'available': True, 'tasks': cleaned,
            'note': '按海报固定列出每日和每周项目；周常状态对应所选当天，不代表整周配额。'}


def build_service_checklist(config, day, today, *, now, report, proofs=None,
                            courtyard=None, area_boss=None, kekkai_utilize=None,
                            execution=None, archived=None):
    current = day == today
    observed = {row['task']: row for row in report.get('tasks', [])}
    saved = {row['task']: row for row in (archived or {}).get('tasks', [])}
    outcomes = _mapping(_mapping(proofs).get('outcomes')) if _mapping(proofs).get('date') == day else {}
    started = _mapping(execution).get('started', ())
    ends = _mapping(_mapping(execution).get('finished'))
    weekday = date.fromisoformat(day).weekday()
    rows = []
    for task, field, source, label, group in SERVICE_TASKS:
        options = _mapping(_mapping(config).get(field))
        prior = saved.get(task)
        row = {'task': task, 'label': label, 'group': group, 'status': 'unverified',
               'status_label': '待核验', 'reason': '未保存当日功能开关，无法还原。', 'evidence_at': None}
        if not current:
            if prior:
                row.update(prior)
                if (prior['status'] not in ('disabled', 'not_scheduled') and source in observed
                        and task not in ('DailySign', 'LuckMessage', 'PetSoul', 'Netherworld', 'Kirin')):
                    row.update(observed[source], task=task, label=label, group=group)
            elif source in observed and task not in ('DailySign', 'LuckMessage', 'PetSoul', 'Netherworld', 'Kirin'):
                row.update(observed[source])
            rows.append(row)
            continue
        if not config:
            row['reason'] = '账号配置无法读取，功能是否开启待核验。'
        elif not _enabled(task, options):
            row.update(status='disabled', status_label='未开启', reason='此账号未开启该功能。')
        elif not _calendar(task, options, weekday):
            row.update(status='not_scheduled', status_label='今日无需运行', reason='该功能已开启，今天不是配置的运行日。')
        else:
            proof = {'CourtyardAffairs': courtyard, 'AreaBoss': area_boss,
                     'KekkaiUtilize': kekkai_utilize}.get(source, outcomes.get(source))
            status, reason, at = _task_status(source, proof, day, now)
            ended = _local_time(ends.get(source))
            if source in observed:
                row.update(observed[source])
            else:
                if source != 'KekkaiUtilize' and proof is None and ended is not None and ended.date().isoformat() == day and ended <= now:
                    status, reason, at = 'completed', '按脚本正常结束判定；尚未核验全部游戏目标。', ended.isoformat(timespec='seconds')
                next_run = _local_time(_mapping(options.get('scheduler')).get('next_run'))
                if group == 'weekly' and source not in started and at is None and next_run and next_run.date().isoformat() > day:
                    status, reason = 'not_scheduled', '该功能已开启，下次计划在所选日期之后。'
                elif status == 'unverified' and proof is None and source not in started and execution is not None:
                    status, reason = 'incomplete', '今日尚未记录到任务运行完成。'
                row.update(status=status, status_label=LABELS[status], reason=reason, evidence_at=at)
                if source == 'KekkaiUtilize' and status != 'unverified' and isinstance(proof, Mapping):
                    row['completion_count'] = proof['count']
                    row['status_label'] = f"{proof['count']}次"
            row.update(task=task, label=label, group=group)
            # Keep a saved result for a composite/excluded task if its logs disappear.
            if prior and row['status'] not in ('completed', 'disabled') and row['evidence_at'] is None and prior['evidence_at']:
                row.update(prior)
            if task == 'PetSoul' and '未单独核验小动物助战是否触发' not in row['reason']:
                row['reason'] += ' 此项复用御魂任务结果，未单独核验小动物助战是否触发。'
        rows.append(row)
    return validate_checklist({'scope': 'poster_checklist', 'date': day, 'tasks': rows}, day)
