"""Restricted OAS protocol adapter: no generic proxy, HTTP start or HTTP stop."""
from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import date

import websockets
from websockets.legacy.client import connect as websocket_connect

IDENTIFIER = re.compile(r"^[A-Za-z0-9_\-\u3400-\u9fff]{1,80}$")
# Configuration filenames may contain decimal/date dots (for example 11.7).
# Keep account names separate from task/field identifiers: no dot path segments,
# empty segments, trailing Windows dots, separators, escapes or percent encoding.
ACCOUNT_IDENTIFIER = re.compile(r"^(?=.{1,80}$)[A-Za-z0-9_\-\u3400-\u9fff]+(?:\.[A-Za-z0-9_\-\u3400-\u9fff]+)*$")
MAX_JSON_BYTES = 4 * 1024 * 1024


class UpstreamError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


MAX_TRANSFER_BYTES = 2 * 1024 * 1024


def transfer_document(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError('请选择 JSON 配置文件')
    try:
        raw = text.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('文件文字编码不正确') from exc
    if len(raw) > MAX_TRANSFER_BYTES:
        raise ValueError('配置文件不能超过 2 MiB')
    try:
        value = json.loads(text.lstrip('\ufeff'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError('请选择有效的 JSON 配置文件') from exc
    if not isinstance(value, dict) or not value:
        raise ValueError('配置文件必须包含 JSON 对象')
    return text.lstrip('\ufeff')


def transfer_multipart(fields: dict[str, str], document: str | None = None):
    boundary = 'oas-panel-' + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode('utf-8'))
    if document is not None:
        parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="config.json"\r\nContent-Type: application/json\r\n\r\n'.encode(),
                      document.encode('utf-8'), b'\r\n'])
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), 'multipart/form-data; boundary=' + boundary


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("名称格式无效")
    return value


def account_identifier(value: str) -> str:
    if not isinstance(value, str) or not ACCOUNT_IDENTIFIER.fullmatch(value):
        raise ValueError("配置名称格式无效")
    return value


def statistics_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("统计日期需为 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("统计日期无效") from exc
    if parsed.isoformat() != value:
        raise ValueError("统计日期无效")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpstreamError("后端返回了不允许的重定向")


def local_backend_url(value: str = "http://127.0.0.1:22289") -> str:
    """Accept only an explicit loopback HTTP endpoint, without proxy routing."""
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ValueError("后端地址必须为本机回环 HTTP 地址")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("后端地址或端口无效") from exc
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or port is None or not 1024 <= port <= 65535):
        raise ValueError("后端地址必须为带端口的本机回环 HTTP 地址")
    host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}:{port}"


class OasClient:
    def __init__(self, base_url: str):
        self.base_url = local_backend_url(base_url)
        self.ws_base = "ws://" + urllib.parse.urlsplit(self.base_url).netloc
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    async def _json(self, path: str, *, method="GET", query=None):
        def work():
            url = self.base_url + path
            if query:
                url += "?" + urllib.parse.urlencode(query)
            request = urllib.request.Request(url, method=method, headers={"Accept": "application/json"})
            try:
                with self.opener.open(request, timeout=8) as response:
                    raw = response.read(MAX_JSON_BYTES + 1)
                    if len(raw) > MAX_JSON_BYTES:
                        raise UpstreamError("后端响应过大")
                    return json.loads(raw)
            except UpstreamError:
                raise
            except (OSError, ValueError, urllib.error.URLError) as exc:
                raise UpstreamError("无法连接 OAS 后端或后端返回无效数据") from exc
        return await asyncio.to_thread(work)

    async def accounts(self) -> list[str]:
        result = await self._json("/config_list")
        if not isinstance(result, list):
            raise UpstreamError("后端配置列表格式无效")
        return [name for name in result if isinstance(name, str) and ACCOUNT_IDENTIFIER.fullmatch(name) and name != "template"]

    async def _transfer_request(self, path, *, method='GET', query=None, payload=None, content_type=None):
        def work():
            url = self.base_url + path
            if query:
                url += '?' + urllib.parse.urlencode(query)
            headers = {'Accept': 'application/json'}
            if content_type:
                headers['Content-Type'] = content_type
            request = urllib.request.Request(url, data=payload, method=method, headers=headers)
            try:
                with self.opener.open(request, timeout=15) as response:
                    raw = response.read(MAX_TRANSFER_BYTES + 1)
                    if len(raw) > MAX_TRANSFER_BYTES:
                        raise UpstreamError('后端配置文件超过大小限制')
                    result = json.loads(raw.decode('utf-8-sig'))
                    if not isinstance(result, dict):
                        raise UpstreamError('后端备份数据格式无效')
                    return result
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise UpstreamError('后台尚未加载备份接口；任务结束后重新打开OAS 后端X 再试', 503) from exc
                try:
                    value = json.loads(exc.read(8192))
                    detail = value.get('detail') if isinstance(value, dict) else None
                except (ValueError, OSError):
                    detail = None
                status = exc.code if exc.code in {400, 409, 413, 422, 503} else 502
                message = '备份或导入未成功，请检查文件和当前运行状态'
                if isinstance(detail, str):
                    message = detail
                elif isinstance(detail, dict) and isinstance(detail.get('message'), str):
                    message = detail['message']
                    fields = detail.get('fields')
                    if isinstance(fields, list):
                        paths = [path[:160] for path in fields if isinstance(path, str)][:5]
                        if paths:
                            message += '（' + '、'.join(paths) + '）'
                raise UpstreamError(message[:1000], status) from exc
            except UpstreamError:
                raise
            except (OSError, ValueError, RecursionError) as exc:
                raise UpstreamError('无法连接备份接口或后端返回无效数据') from exc
        return await asyncio.to_thread(work)

    async def transfer_capabilities(self):
        result = await self._transfer_request('/config/transfer/capabilities')
        if (result.get('version') != 1 or result.get('modes') != ['backup', 'share'] or
                result.get('config_import') is not True or result.get('task_import') is not True):
            raise UpstreamError('当前后台不支持完整备份与导入', 503)
        return result

    async def transfer_export(self, account: str, mode: str, task: str | None = None):
        name = account_identifier(account)
        if mode not in {'backup', 'share'}:
            raise ValueError('导出模式无效')
        await self.transfer_capabilities()
        if task is None:
            return await self._transfer_request('/config/export', query={'name': name, 'mode': mode})
        return await self._transfer_request('/config/task/export', query={'config_name': name, 'task_name': identifier(task), 'mode': mode})

    async def transfer_import(self, account: str, text: str, task: str | None = None):
        name = account_identifier(account)
        document = transfer_document(text)
        await self.transfer_capabilities()
        if task is None:
            payload, kind = transfer_multipart({'name': name}, document)
            path = '/config/import'
        else:
            payload, kind = transfer_multipart({'config_name': name, 'task_name': identifier(task), 'json_text': document})
            path = '/config/task/import'
        return await self._transfer_request(path, method='POST', payload=payload, content_type=kind)

    async def menu(self) -> dict:
        result = await self._json("/script_menu")
        if not isinstance(result, dict):
            raise UpstreamError("后端任务菜单格式无效")
        return {str(category): [name for name in tasks if isinstance(name, str) and IDENTIFIER.fullmatch(name)]
                for category, tasks in result.items() if isinstance(tasks, list)}

    async def statistics_dates(self, account: str) -> dict:
        name = account_identifier(account)
        result = await self._json("/stats/" + urllib.parse.quote(name, safe="") + "/dates")
        if not isinstance(result, dict) or result.get("script_name") != name or not isinstance(result.get("dates"), list):
            raise UpstreamError("后端统计日期格式无效")
        try:
            dates = [statistics_date(item) for item in result["dates"]]
        except ValueError as exc:
            raise UpstreamError("后端统计日期格式无效") from exc
        return {**result, "dates": sorted(set(dates), reverse=True)}

    async def statistics_day(self, account: str, day: str) -> dict:
        name = account_identifier(account)
        result = await self._json("/stats/" + urllib.parse.quote(name, safe=""), query={"date": statistics_date(day)})
        if not isinstance(result, dict) or result.get("script_name") != name or not isinstance(result.get("tasks"), dict):
            raise UpstreamError("后端统计记录格式无效")
        return result

    async def settings(self, account: str, task: str) -> dict:
        parts = [urllib.parse.quote(account_identifier(account), safe=""), urllib.parse.quote(identifier(task), safe="")]
        result = await self._json("/" + "/".join(parts) + "/args")
        if not isinstance(result, dict):
            raise UpstreamError("后端设置格式无效")
        return result

    async def set_value(self, account: str, task: str, group: str, argument: str, value, kind: str):
        parts = (account_identifier(account), identifier(task), identifier(group), identifier(argument))
        path = "/" + "/".join(urllib.parse.quote(part, safe="") for part in parts) + "/value"
        wire_kind = "string" if kind in {"enum", "multi_line", "multi_enum"} else kind
        if kind == "time_delta":
            # The original parser supports 0–9; script_set_arg's strptime path
            # correctly constructs timedelta for 10–31. Larger days are refused
            # by validation because the upstream model doesn't validate setattr.
            wire_kind = "time_delta" if int(value[:2]) < 10 else "string"
        wire_value = ("true" if value else "false") if isinstance(value, bool) else str(value)
        if kind == 'multi_enum':
            wire_value = json.dumps(value, ensure_ascii=False)
        result = await self._json(path, method="PUT", query={"types": wire_kind, "value": wire_value})
        if result is not True:
            raise UpstreamError("OAS 未确认保存该设置")

    @asynccontextmanager
    async def connection(self, account: str):
        try:
            async with websocket_connect(
                self.ws_base + "/ws/" + urllib.parse.quote(account_identifier(account), safe=""),
                open_timeout=5, close_timeout=1, ping_interval=20, ping_timeout=20,
                max_size=1024 * 1024, max_queue=64,
            ) as ws:
                yield ws
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            raise UpstreamError("OAS 实时连接已断开") from exc

    @staticmethod
    def event(raw) -> dict:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            if type(parsed.get("state")) is int and parsed["state"] in (0, 1, 2, 3):
                return {"type": "state", "state": parsed["state"]}
            if isinstance(parsed.get("schedule"), dict):
                return {"type": "schedule", "schedule": parsed["schedule"]}
        return {"type": "log", "line": str(raw)[:65536]}

    async def snapshot(self, account: str) -> dict:
        result = {"state": None, "schedule": {}, "connected": False}
        async with self.connection(account) as ws:
            async def read_initial():
                got_state = got_schedule = False
                while not (got_state and got_schedule):
                    event = self.event(await ws.recv())
                    if event["type"] == "state":
                        result["state"] = event["state"]
                        got_state = True
                    elif event["type"] == "schedule":
                        result["schedule"] = event["schedule"]
                        got_schedule = True
                result["connected"] = True
            try:
                await asyncio.wait_for(read_initial(), 5)
            except asyncio.TimeoutError as exc:
                raise UpstreamError("读取 OAS 状态超时") from exc
        return result

    async def action(self, account: str, action: str) -> dict:
        if action not in {"start", "stop"}:
            raise ValueError("不支持的操作")
        target = 1 if action == "start" else 0
        async with self.connection(account) as ws:
            async def command():
                # Consume both initial messages before sending, so a stale initial
                # state cannot be mistaken for an acknowledgement of this action.
                state = None
                schedule_seen = False
                while state is None or not schedule_seen:
                    event = self.event(await ws.recv())
                    if event["type"] == "state":
                        state = event["state"]
                    elif event["type"] == "schedule":
                        schedule_seen = True
                if state == target:
                    return {"ok": True, "state": state, "changed": False}
                if state == 3:
                    raise UpstreamError("OAS 正在更新，请稍后操作")
                if action == "start" and state == 2:
                    raise UpstreamError("该配置处于异常状态，请先停止后再启动")
                await ws.send(action)
                while True:
                    event = self.event(await ws.recv())
                    if event["type"] == "state" and event["state"] == target:
                        return {"ok": True, "state": target, "changed": True}
            try:
                return await asyncio.wait_for(command(), 10)
            except asyncio.TimeoutError as exc:
                raise UpstreamError("操作已发送但未确认状态，请刷新查看，勿反复提交") from exc
