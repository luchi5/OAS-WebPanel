"""Private minimal task-view snapshots; never store account configuration."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from datetime import date, datetime, timezone, timedelta
from service_checklist import validate_checklist

_LOCK = threading.RLock()

class TaskHistory:
    def __init__(self, root):
        self.root = Path(root).absolute()

    def _safe(self, path, create=False):
        for node in [*reversed(path.parents), path]:
            try:
                st = node.lstat()
            except FileNotFoundError:
                if create:
                    node.mkdir()
            else:
                if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('History link rejected')
        if path.exists() and path.is_file() and path.stat().st_nlink != 1:
            raise ValueError('History hard link rejected')

    def _path(self, account, day):
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError('History date invalid')
        return self.root / day / (hashlib.sha256(account.encode('utf-8')).hexdigest() + '.json')

    def _rows(self, rows, day):
        if not isinstance(rows, list) or len(rows) > 256:
            raise ValueError('History tasks invalid')
        clean = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('History row invalid')
            task = row.get('task')
            if not isinstance(task, str) or not re.fullmatch(r'[A-Z][A-Za-z0-9]{0,63}', task) or task in seen:
                raise ValueError('History task invalid')
            if row.get('status') not in ('completed', 'incomplete', 'unverified'):
                raise ValueError('History status invalid')
            fields = {'task': task, 'status': row['status']}
            for key, maximum in [('label', 120), ('status_label', 120), ('reason', 1000)]:
                value = row.get(key)
                if not isinstance(value, str) or len(value) > maximum:
                    raise ValueError('History text invalid')
                fields[key] = value
            stamp = row.get('evidence_at')
            if stamp is not None and (not isinstance(stamp, str) or len(stamp) > 40):
                raise ValueError('History evidence invalid')
            if stamp is not None:
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                local = timezone(timedelta(hours=8))
                parsed = parsed.replace(tzinfo=local) if parsed.tzinfo is None else parsed.astimezone(local)
                if parsed.date().isoformat() != day:
                    raise ValueError('History evidence day invalid')
            elif row['status'] == 'completed':
                raise ValueError('Completed history lacks a timestamp')
            fields['evidence_at'] = stamp
            if task == 'KekkaiUtilize' and 'completion_count' in row:
                count = row['completion_count']
                if type(count) is not int or not 0 <= count <= 1000 or count > 0 and stamp is None:
                    raise ValueError('History foster count invalid')
                if count == 0 and row['status'] == 'completed':
                    raise ValueError('Zero foster count is not a completion')
                fields['completion_count'] = count
            clean.append(fields)
            seen.add(task)
        return clean

    def load(self, account, day):
        with _LOCK:
            path = self._path(account, day)
            self._safe(path)
            try:
                with path.open('rb') as stream:
                    data = stream.read(512 * 1024 + 1)
            except FileNotFoundError:
                return None
            if len(data) > 512 * 1024:
                raise ValueError('History too large')
            value = json.loads(data)
            if (not isinstance(value, dict) or value.get('schema') != 1
                    or value.get('account') != account or value.get('date') != day
                    or type(value.get('planned')) is not bool):
                raise ValueError('History identity invalid')
            value['tasks'] = self._rows(value.get('tasks'), day)
            if 'service_checklist' in value:
                value['service_checklist'] = validate_checklist(value['service_checklist'], day)
            return value

    def save(self, account, day, report, stamp, *, planned, plan_only=False):
        if report.get('available') is not True:
            return
        with _LOCK:
            old = self.load(account, day)
            rows = {row['task']: row for row in old['tasks']} if old else {}
            for row in self._rows(report.get('tasks'), day):
                previous = rows.get(row['task'])
                if previous and row['task'] == 'KekkaiUtilize' and not plan_only:
                    old_count, new_count = previous.get('completion_count'), row.get('completion_count')
                    if type(old_count) is int and (type(new_count) is not int or old_count > new_count):
                        continue
                    # Replace legacy normal-End completions with actual counts.
                    rows[row['task']] = row
                    continue
                if previous and (plan_only or previous['status'] == 'completed'
                                 and row['status'] != 'completed' and row['evidence_at'] is None):
                    continue
                rows[row['task']] = row
            value = {'schema': 1, 'account': account, 'date': day,
                     'planned': planned or bool(old and old['planned']),
                     'updated_at': stamp, 'tasks': list(rows.values())}
            checklist = report.get('service_checklist')
            if plan_only and old and old.get('service_checklist'):
                checklist = old['service_checklist']
            elif checklist is None and old:
                checklist = old.get('service_checklist')
            if checklist is not None:
                value['service_checklist'] = validate_checklist(checklist, day)
            if (old and old['tasks'] == value['tasks'] and old['planned'] == value['planned']
                    and old.get('service_checklist') == value.get('service_checklist')):
                return
            path = self._path(account, day)
            self._safe(path.parent, create=True)
            self._safe(path)
            fd, temporary = tempfile.mkstemp(prefix='.history-', suffix='.tmp', dir=path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(value, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._safe(path)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
