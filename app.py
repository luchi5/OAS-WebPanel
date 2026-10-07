"""Independent authenticated web panel for the existing mainline OAS service.

Closing a browser or stopping this panel never stops OAS. No backend subprocess
is launched or terminated here. Start: python app.py --settings settings.json
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import json
import math
import mimetypes
import re
import secrets
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, quote

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from auth import AuthStore, COOKIE_NAME, Session
from oas_client import OasClient, UpstreamError, identifier, IDENTIFIER, account_identifier, ACCOUNT_IDENTIFIER, statistics_date, transfer_document, MAX_TRANSFER_BYTES, local_backend_url
from statistics_reader import LocalStatisticsReader
from daily_feedback_reader import DailyFeedbackReader, FeedbackNotFound

ROOT = Path(__file__).resolve().parent
MAX_BODY = 64 * 1024
MAX_LOG = 512 * 1024
MAX_IMAGE = 4 * 1024 * 1024
MAX_IMAGES_TOTAL = 12 * 1024 * 1024
ARCHIVE_ID = re.compile(r"^(?:(?P<config>[A-Za-z0-9_.\-\u3400-\u9fff]{1,80})[_-])?(?P<timestamp>[0-9]{10,17})$")
IMAGE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,150}\.(?:png|jpg|jpeg|webp)$", re.I)
ACCOUNT_LOG = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_(?P<config>[A-Za-z0-9_.\-\u3400-\u9fff]{1,80})\.txt$")
SAVED_ERROR = re.compile(rb"Saving error:\s+(?:\.[/\\])?log[/\\]error[/\\](?P<id>[0-9]{10,17})\s*$")


class ArchiveLogOwners:
    """Prove legacy ownership from exact per-account error-save log entries.

    Keep a per-file cursor and partial line. Ordinary refreshes read only newly
    appended bytes; truncated/replaced files are rebuilt and removed logs drop
    their old evidence. The lock also covers concurrent detail/image reads.
    """
    def __init__(self, backend_root):
        self.backend_root = backend_root
        self.files = {}
        self.lock = threading.Lock()

    def read(self, accounts):
        accounts = set(accounts)
        with self.lock:
            try:
                log_root = safe_path(self.backend_root, "log")
                paths = list(log_root.iterdir())
            except (HTTPException, OSError):
                self.files.clear()
                return {}
            seen = set()
            for candidate in paths:
                match = ACCOUNT_LOG.fullmatch(candidate.name)
                if not match or not ACCOUNT_IDENTIFIER.fullmatch(match.group("config")) or match.group("config") not in accounts:
                    continue
                try:
                    path = safe_path(log_root, candidate.name)
                    info = path.stat()
                    if not path.is_file():
                        continue
                    seen.add(candidate.name)
                    identity = (info.st_dev, info.st_ino)
                    cached = self.files.get(candidate.name)
                    if not cached or cached["identity"] != identity or info.st_size < cached["offset"] or (
                            info.st_size == cached["offset"] and info.st_mtime_ns != cached["mtime"]):
                        cached = {"identity": identity, "offset": 0, "mtime": None,
                                  "carry": b"", "ids": set(), "config": match.group("config")}
                    if info.st_size != cached["offset"] or info.st_mtime_ns != cached["mtime"]:
                        with path.open("rb") as stream:
                            stream.seek(cached["offset"])
                            fresh = stream.read()
                        lines = (cached["carry"] + fresh).split(b"\n")
                        cached["carry"] = lines.pop()
                        for line in lines:
                            saved = SAVED_ERROR.search(line)
                            if saved:
                                cached["ids"].add(saved.group("id").decode("ascii"))
                        cached["offset"] += len(fresh)
                        cached["mtime"] = info.st_mtime_ns
                    self.files[candidate.name] = cached
                except (OSError, HTTPException):
                    self.files.pop(candidate.name, None)
            self.files = {name: cached for name, cached in self.files.items() if name in seen}
            owners = {}
            for cached in self.files.values():
                for record_id in cached["ids"]:
                    owners.setdefault(record_id, set()).add(cached["config"])
            return {record_id: next(iter(names)) for record_id, names in owners.items() if len(names) == 1}


def load_settings(path: Path) -> dict:
    settings = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(settings, dict):
        raise ValueError("面板配置必须为 JSON 对象")
    if settings.get("host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("面板只能监听 127.0.0.1")
    port = settings.get("port", 22300)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("面板端口无效")
    settings["backend_url"] = local_backend_url(settings.get("backend_url", "http://127.0.0.1:22289"))
    public_value = settings.get("public_origin", f"http://127.0.0.1:{port}")
    if not isinstance(public_value, str) or any(char.isspace() for char in public_value):
        raise ValueError("public_origin 必须为本地来源或 HTTPS 来源")
    try:
        public = urlsplit(public_value)
        public_port = public.port
    except ValueError as exc:
        raise ValueError("public_origin 地址或端口无效") from exc
    if (not public.hostname or public.username is not None or public.password is not None
            or public.path not in ("", "/") or public.query or public.fragment
            or "%" in public.netloc or "\\" in public.netloc):
        raise ValueError("public_origin 只能包含协议、主机和可选端口")
    if public.scheme == "http":
        if public.hostname not in {"127.0.0.1", "localhost"} or public_port != port:
            raise ValueError("本地来源必须使用面板的回环地址和端口")
        settings["public_origin"] = f"http://{public.hostname}:{port}"
    elif public.scheme == "https":
        public_port = 443 if public_port is None else public_port
        if public_port != 443 and not 1024 <= public_port <= 65535:
            raise ValueError("HTTPS 来源端口无效")
        try:
            hostname = public.hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("HTTPS 来源主机无效") from exc
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.\-]{0,251}[a-z0-9])?", hostname):
            raise ValueError("HTTPS 来源必须使用有效域名或 IPv4 地址")
        settings["public_origin"] = "https://" + hostname + (f":{public_port}" if public_port != 443 else "")
    else:
        raise ValueError("远程来源必须使用 HTTPS")
    settings["port"] = port
    settings["host"] = "127.0.0.1"
    settings.setdefault("authentication_required", True)
    if type(settings["authentication_required"]) is not bool:
        raise ValueError("authentication_required 必须为布尔值")
    backend_root = settings.get("backend_root")
    if not isinstance(backend_root, str) or not backend_root or not Path(backend_root).is_absolute():
        raise ValueError("必须配置 backend_root 为后端的绝对路径")
    if not Path(backend_root).is_dir():
        raise ValueError("backend_root 必须为已存在的后端目录")
    settings["backend_root"] = str(Path(backend_root).resolve())
    runtime = settings.get("backend_python")
    if runtime and (not isinstance(runtime, str) or not Path(runtime).is_absolute() or not Path(runtime).is_file()):
        raise ValueError("backend_python 必须为后端 Python 的绝对文件路径")
    state_dir = settings.get("state_dir", "state")
    if not isinstance(state_dir, str) or not state_dir:
        raise ValueError("state_dir 必须为目录路径")
    state_path = Path(state_dir)
    settings["state_dir"] = str((state_path if state_path.is_absolute() else ROOT / state_path).resolve())
    return settings


class OriginGuard:
    """Transport-level host/origin checks also cover static assets and WS."""
    def __init__(self, app, settings):
        self.app = app
        self.public = settings["public_origin"]
        self.port = settings["port"]
        self.local_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        public = urlsplit(self.public)
        self.public_hosts = set()
        if public.scheme == "https":
            self.public_hosts.add(public.netloc.lower())
            if public.port in (None, 443):
                self.public_hosts.add(public.hostname.lower() + ":443")


    def check(self, scope):
        headers: dict[str, list[str]] = {}
        for key, value in scope.get("headers", []):
            headers.setdefault(key.decode("latin1").lower(), []).append(value.decode("latin1"))
        if len(headers.get("host", [])) != 1 or len(headers.get("origin", [])) > 1:
            return False, False
        host = headers["host"][0].lower()
        if any(char in host for char in ("/", "\\", "@", " ", ",")):
            return False, False
        scheme = scope.get("scheme", "http")
        secure = scheme in ("https", "wss")
        if host in self.local_hosts:
            expected = ("https" if secure else "http") + "://" + host
        elif host in self.public_hosts:
            peer = (scope.get("client") or ("", 0))[0]
            try:
                local_proxy = ipaddress.ip_address(peer).is_loopback
            except ValueError:
                local_proxy = False
            forwarded = headers.get("x-forwarded-proto", [])
            if not local_proxy or forwarded != ["https"]:
                return False, False
            secure = True
            expected = self.public
        else:
            return False, False
        origins = headers.get("origin", [])
        if origins and origins[0] != expected:
            return False, False
        needs_origin = scope["type"] == "websocket" or scope.get("method") not in {"GET", "HEAD", "OPTIONS"}
        if needs_origin and not origins:
            return False, False
        # No cross-origin resource embedding, even when Origin is absent.
        if headers.get("sec-fetch-site", [""])[0] == "cross-site":
            return False, False
        return True, secure

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        allowed, secure = self.check(scope)
        if not allowed:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            response = JSONResponse({"detail": "访问来源不被允许，请使用面板的本地地址或 HTTPS 域名"}, status_code=403)
            return await response(scope, receive, send)
        scope.setdefault("state", {})["panel_secure"] = secure
        rate_peer = (scope.get("client") or ("unknown", 0))[0]
        # Only the already verified public-host loopback proxy may supply its
        # overwritten, single-hop client IP. Local direct requests cannot spoof
        # their limiter identity with an X-Forwarded-For header.
        raw_headers = scope.get("headers", [])
        host = next((v.decode("latin1").lower() for k, v in raw_headers if k.lower() == b"host"), "")
        forwarded_for = [v.decode("latin1") for k, v in raw_headers if k.lower() == b"x-forwarded-for"]
        if host in self.public_hosts and len(forwarded_for) == 1:
            try:
                rate_peer = str(ipaddress.ip_address(forwarded_for[0]))
            except ValueError:
                pass
        scope["state"]["panel_rate_peer"] = rate_peer
        async def secured_send(message):
            if message["type"] == "http.response.start":
                extra = [(b"x-content-type-options", b"nosniff"),
                         (b"referrer-policy", b"no-referrer"),
                         (b"x-frame-options", b"DENY"),
                         (b"cache-control", b"no-store"),
                         (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                         (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]
                if secure:
                    extra.append((b"strict-transport-security", b"max-age=31536000"))
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)
        await self.app(scope, receive, secured_send)


def safe_path(root: Path, *parts: str) -> Path:
    base = root.resolve()
    candidate = root.joinpath(*parts)
    try:
        # Refuse symlinks and Windows junction/reparse entries, including parents
        # below the archive root, even if they happen to resolve back inside it.
        current = root
        for part in (None,) + parts:
            if part is not None:
                current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("链接文件不允许访问")
        resolved = candidate.resolve(strict=True)
        if resolved != base and base not in resolved.parents:
            raise ValueError("文件路径无效")
        return resolved
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "记录或文件不存在") from exc


def archive_time(record_id: str) -> str:
    try:
        return datetime.fromtimestamp(int(record_id) / 1000, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def validate_value(field: dict, value):
    kind = field.get("type")
    if field.get("readOnly") or field.get("readonly") or field.get("disabled"):
        raise HTTPException(403, "此字段为只读")
    choices = field.get("enumEnum")
    if kind == 'multi_enum':
        if not isinstance(choices, list) or not isinstance(value, list) or not max(1, field.get('minItems', 1)) <= len(value) <= len(choices):
            raise HTTPException(422, '请选择至少一个允许的选项')
        if any(type(item) is not str or item not in choices for item in value) or len(set(value)) != len(value):
            raise HTTPException(422, '多选值必须来自列表且不能重复')
        return 'multi_enum'
    if isinstance(choices, list):
        if type(value) not in (str, bool, int, float) or (type(value) is float and not math.isfinite(value)):
            raise HTTPException(422, "此枚举类型暂不支持修改")
        if not any(type(value) is type(choice) and value == choice for choice in choices):
            raise HTTPException(422, "请选择列表中允许的值")
        if isinstance(value, str):
            reject_ambiguous_string(value)
        # Infer the wire type rather than trusting a client-supplied type.
        return "boolean" if type(value) is bool else "integer" if type(value) is int else "number" if type(value) is float else "string"
    if kind == "boolean":
        valid = type(value) is bool
    elif kind == "integer":
        valid = type(value) is int and abs(value) <= 2**53 - 1
    elif kind == "number":
        valid = type(value) in (int, float) and math.isfinite(value) and abs(value) <= 2**53 - 1
    elif kind in {"string", "multi_line", "date_time", "time", "time_delta"}:
        valid = isinstance(value, str) and len(value) <= 16384 and "\x00" not in value
        if valid and kind in {"date_time", "time"}:
            fmt = "%Y-%m-%d %H:%M:%S" if kind == "date_time" else "%H:%M:%S"
            try:
                valid = datetime.strptime(value, fmt).strftime(fmt) == value
            except ValueError:
                valid = False
        if valid and kind == "time_delta":
            match = re.fullmatch(r"(\d{2}) ([0-2]\d):([0-5]\d):([0-5]\d)", value)
            valid = bool(match and int(match[1]) <= 31 and int(match[2]) <= 23)
            if not valid:
                raise HTTPException(422, "时间间隔需为 DD HH:mm:ss，当前OAS 后端仅支持此面板写入 00–31 天")
    else:
        raise HTTPException(422, "此字段类型暂不支持网页修改")
    if not valid:
        raise HTTPException(422, "设置值类型或格式不正确")
    if kind in {"string", "multi_line"}:
        reject_ambiguous_string(value)
    if type(value) in (int, float):
        for key, compare in (("minimum", lambda n: value >= n), ("maximum", lambda n: value <= n),
                             ("exclusiveMinimum", lambda n: value > n), ("exclusiveMaximum", lambda n: value < n)):
            bound = field.get(key)
            if type(bound) in (int, float) and not compare(bound):
                raise HTTPException(422, "设置值超出允许范围")
    return kind


def reject_ambiguous_string(value: str):
    """Don't let mainline's unconditional coercion corrupt a text field."""
    if value in {"true", "false"}:
        raise HTTPException(422, "OAS 后端会自动转换此文字为布尔值，此文字字段暂不能保存该值")
    formats = {8: "%H:%M:%S", 11: "%d %H:%M:%S", 19: "%Y-%m-%d %H:%M:%S"}
    fmt = formats.get(len(value))
    if fmt:
        try:
            datetime.strptime(value, fmt)
        except ValueError:
            return
        raise HTTPException(422, "OAS 后端会自动转换此文字为时间，此文字字段暂不能保存该值")


def create_app(settings: dict, *, client=None, auth=None, statistics=None) -> FastAPI:
    app = FastAPI(title="OAS 网页面板", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(OriginGuard, settings=settings)
    store = auth or AuthStore(Path(settings.get("state_dir", ROOT / "state")))
    authentication_required = settings.get("authentication_required", True)
    if type(authentication_required) is not bool:
        raise ValueError("authentication_required must be boolean")
    anonymous_session = Session("anonymous", "免登录", secrets.token_urlsafe(32), False, math.inf)
    oas = client or OasClient(settings["backend_url"])
    backend_root = Path(settings["backend_root"])
    # The web panel can read stats while the live OAS backend is busy and has
    # not yet reloaded the new routes. Both clients use the same parser.
    stats = statistics if statistics is not None else client if client is not None else LocalStatisticsReader(backend_root)
    error_root = backend_root / "log" / "error"
    legacy_error_owners = ArchiveLogOwners(backend_root)
    daily_feedback = DailyFeedbackReader(backend_root)
    locks: dict[str, asyncio.Lock] = {}
    action_cooldown: dict[str, float] = {}
    event_counts: dict[str, int] = {}
    app.state.auth = store
    app.state.oas = oas

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        detail = {404: "页面、记录或配置不存在", 405: "此接口不支持该请求方法"}.get(exc.status_code, exc.detail)
        return JSONResponse({"detail": detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "请求参数格式不正确"}, status_code=422)

    @app.exception_handler(UpstreamError)
    async def upstream_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unknown_error(request, exc):
        return JSONResponse({"detail": "处理请求失败，请稍后重试"}, status_code=500)

    def require_session(request: Request, *, write=False, changing_password=False) -> Session:
        session = store.resolve(request.cookies.get(COOKIE_NAME)) if authentication_required else anonymous_session
        if not session:
            raise HTTPException(401, "请先登录")
        if write:
            supplied = request.headers.get("x-csrf-token", "")
            if not supplied or not supplied.isascii() or not hmac.compare_digest(supplied, session.csrf):
                raise HTTPException(403, "安全令牌无效，请刷新页面")
            if session.must_change_password and not changing_password:
                raise HTTPException(403, "首次登录请先修改初始密码")
        return session

    async def body(request: Request, fields: set[str], max_bytes=MAX_BODY) -> dict:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise HTTPException(415, "请使用 JSON 请求")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > max_bytes:
                raise HTTPException(413, "请求内容过大")
        try:
            result = json.loads(raw)
        except (ValueError, TypeError):
            raise HTTPException(400, "JSON 格式不正确")
        if not isinstance(result, dict) or set(result) != fields:
            raise HTTPException(422, "请求字段不正确")
        return result

    async def check_account(name):
        try:
            account_identifier(name)
        except ValueError:
            raise HTTPException(404, "配置不存在")
        if name not in await oas.accounts():
            raise HTTPException(404, "配置不存在")

    async def check_task(name, task):
        await check_account(name)
        try:
            identifier(task)
        except ValueError:
            raise HTTPException(404, "任务不存在")
        menu = await oas.menu()
        if task not in {item for group in menu.values() for item in group}:
            raise HTTPException(404, "任务不存在")

    def feedback_date(day):
        try:
            return statistics_date(day) if day is not None else daily_feedback.today()
        except ValueError as exc:
            raise HTTPException(422, '日期需为有效的 YYYY-MM-DD') from exc

    def read_feedback(operation, *args):
        try:
            return getattr(daily_feedback, operation)(*args)
        except FeedbackNotFound as exc:
            raise HTTPException(404, '验收记录、配置或图片不存在') from exc
        except ValueError as exc:
            raise HTTPException(422, '验收请求格式无效') from exc

    @app.get('/api/daily-feedback')
    async def feedback_day(request: Request, day: str | None = Query(None, alias='date')):
        require_session(request)
        return await asyncio.to_thread(read_feedback, 'day', feedback_date(day))

    @app.get('/api/daily-feedback/{name}')
    async def feedback_account(request: Request, name: str, day: str | None = Query(None, alias='date')):
        require_session(request)
        return await asyncio.to_thread(read_feedback, 'account_day', name, feedback_date(day))

    @app.get('/api/daily-feedback/{name}/images/{image}')
    async def feedback_image(request: Request, name: str, image: str,
                             day: str | None = Query(None, alias='date')):
        require_session(request)
        payload = await asyncio.to_thread(read_feedback, 'image', name, feedback_date(day), image)
        return Response(payload, media_type='image/png')

    def labels():
        result = {}
        # OASX exports the complete dictionary here; assets/i18n only contains
        # additions. Load additions last so new and custom task labels win.
        for path in (backend_root / "module" / "config" / "i18n" / "zh-CN.json",
                     backend_root / "assets" / "i18n" / "zh-CN.json"):
            try:
                if path.stat().st_size > 2 * 1024 * 1024:
                    continue
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    result.update({key: value for key, value in data.items()
                                   if isinstance(key, str) and isinstance(value, str)})
            except (OSError, ValueError):
                continue
        return result

    @app.get("/api/session")
    async def session_view(request: Request):
        session = store.resolve(request.cookies.get(COOKIE_NAME)) if authentication_required else anonymous_session
        result = session.public() if session else {"authenticated": False, "username": None, "csrf": None, "must_change_password": False}
        return {**result, "login_required": authentication_required}

    @app.get('/api/transfer/capabilities')
    async def transfer_capabilities(request: Request):
        require_session(request)
        return await oas.transfer_capabilities()

    @app.get('/api/accounts/{name}/export')
    async def export_configuration(request: Request, name: str, mode: str = Query('backup', pattern='^(backup|share)$')):
        require_session(request)
        await check_account(name)
        result = await oas.transfer_export(name, mode)
        return Response(json.dumps(result, ensure_ascii=False, indent=2), media_type='application/json',
                        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(name + '-' + mode + '.json', safe='')}"})

    @app.post('/api/config/import')
    async def import_configuration(request: Request):
        require_session(request, write=True)
        data = await body(request, {'name', 'json_text'}, max_bytes=MAX_TRANSFER_BYTES * 2 + 4096)
        try:
            name = account_identifier(data['name'])
            if name == 'template':
                raise ValueError('不能替换模板配置')
            document = transfer_document(data['json_text'])
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if name in await oas.accounts():
            raise HTTPException(409, '名称已存在，请为导入的配置使用新名称')
        async with locks.setdefault(name, asyncio.Lock()):
            return await oas.transfer_import(name, document)

    @app.get('/api/accounts/{name}/tasks/{task}/export')
    async def export_task_configuration(request: Request, name: str, task: str,
                                        mode: str = Query('backup', pattern='^(backup|share)$')):
        require_session(request)
        await check_task(name, task)
        result = await oas.transfer_export(name, mode, task)
        return Response(json.dumps(result, ensure_ascii=False, indent=2), media_type='application/json',
                        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(name + '-' + task + '-' + mode + '.json', safe='')}"})

    @app.post('/api/accounts/{name}/tasks/{task}/import')
    async def import_task_configuration(request: Request, name: str, task: str):
        require_session(request, write=True)
        data = await body(request, {'json_text'}, max_bytes=MAX_TRANSFER_BYTES * 2 + 4096)
        await check_task(name, task)
        try:
            document = transfer_document(data['json_text'])
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        async with locks.setdefault(name, asyncio.Lock()):
            snapshot = await oas.snapshot(name)
            if not snapshot.get('connected') or snapshot.get('state') != 0:
                raise HTTPException(409, '请先停止该配置，再导入任务参数')
            return await oas.transfer_import(name, document, task)

    @app.post("/api/login")
    async def login(request: Request):
        if not authentication_required:
            raise HTTPException(404, "此面板已关闭账号登录")
        data = await body(request, {"username", "password"})
        username, password = data["username"], data["password"]
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 80 or not 1 <= len(password) <= 256:
            raise HTTPException(401, "用户名或密码错误")
        peer = request.state.panel_rate_peer
        if not store.check_login_rate(peer):
            raise HTTPException(429, "登录尝试过多，请稍后重试")
        result = await asyncio.to_thread(store.authenticate, username, password)
        if not result:
            raise HTTPException(401, "用户名或密码错误")
        token, session = result
        response = JSONResponse(session.public())
        response.set_cookie(COOKIE_NAME, token, httponly=True, secure=request.state.panel_secure,
                            samesite="strict", max_age=store.session_seconds, path="/")
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        if not authentication_required:
            raise HTTPException(404, "此面板无需退出登录")
        session = require_session(request, write=True, changing_password=True)
        store.logout(session)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=request.state.panel_secure, samesite="strict")
        return response

    @app.post("/api/password")
    async def password(request: Request):
        if not authentication_required:
            raise HTTPException(404, "此面板已关闭账号登录")
        session = require_session(request, write=True, changing_password=True)
        data = await body(request, {"current_password", "new_password"})
        current, new = data["current_password"], data["new_password"]
        if not isinstance(current, str) or len(current) > 256 or not isinstance(new, str) or not 6 <= len(new) <= 256:
            raise HTTPException(422, "新密码长度需为 6–256 个字符")
        if current == new:
            raise HTTPException(422, "新密码不能与当前密码相同")
        peer = request.state.panel_rate_peer
        if not store.check_login_rate(peer + ":password"):
            raise HTTPException(429, "密码验证尝试过多，请稍后重试")
        if not await asyncio.to_thread(store.change_password, session, current, new):
            raise HTTPException(403, "当前密码不正确")
        return store.resolve(request.cookies.get(COOKIE_NAME)).public()

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request):
        session = require_session(request)
        online = True
        try:
            accounts, menu = await asyncio.gather(oas.accounts(), oas.menu())
        except UpstreamError:
            accounts, menu, online = [], {}, False
        return {**session.public(), "login_required": authentication_required, "accounts": accounts, "menu": menu, "labels": await asyncio.to_thread(labels),
                "backend_online": online, "backend_name": "OAS 后端", "domain": settings["public_origin"]}

    @app.get("/api/accounts/{name}/snapshot")
    async def snapshot(request: Request, name: str):
        require_session(request)
        await check_account(name)
        try:
            return await oas.snapshot(name)
        except UpstreamError:
            return {"state": None, "schedule": {}, "connected": False}

    @app.get("/api/accounts/{name}/statistics/dates")
    async def statistics_dates(request: Request, name: str):
        require_session(request)
        await check_account(name)
        return await stats.statistics_dates(name)

    @app.get("/api/accounts/{name}/statistics")
    async def statistics_day(request: Request, name: str, day: str = Query(..., alias="date")):
        require_session(request)
        try:
            statistics_date(day)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        await check_account(name)
        return await stats.statistics_day(name, day)

    @app.get("/api/accounts/{name}/settings/{task}")
    async def task_settings(request: Request, name: str, task: str):
        require_session(request)
        await check_task(name, task)
        result = await oas.settings(name, task)
        if task == "Restart":
            for row in result.get("task_config", []):
                if isinstance(row, dict) and row.get("name") == "reset_task_datetime_enable":
                    row["readOnly"] = True
                    row["description"] = "该OAS 后端选项会批量修改所有任务时间，请在本机 OASX 操作。"
        return result

    @app.put("/api/accounts/{name}/settings/{task}/{group}/{argument}")
    async def put_setting(request: Request, name: str, task: str, group: str, argument: str):
        require_session(request, write=True)
        data = await body(request, {"value", "expected_value"})
        await check_task(name, task)
        if not IDENTIFIER.fullmatch(group) or not IDENTIFIER.fullmatch(argument):
            raise HTTPException(404, "设置字段不存在")
        if (task, group, argument) == ("Restart", "task_config", "reset_task_datetime_enable"):
            raise HTTPException(403, "此选项会批量修改其他任务时间，请在本机 OASX 操作")
        lock = locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "此配置正在处理另一项操作，请稍后重试")
        async with lock:
            current = await oas.settings(name, task)
            rows = current.get(group, [])
            field = next((row for row in rows if isinstance(row, dict) and row.get("name") == argument), None) if isinstance(rows, list) else None
            if not field or "value" not in field:
                raise HTTPException(404, "设置字段不存在")
            old = field["value"]
            expected = data["expected_value"]
            unchanged = type(old) is type(expected) and old == expected
            # JSON/JavaScript represent 1.0 as 1, so numeric fields compare by
            # finite numeric value while bool/integer fields remain strict.
            if field.get("type") == "number" and type(old) in (int, float) and type(expected) in (int, float):
                unchanged = math.isfinite(old) and math.isfinite(expected) and old == expected
            if not unchanged:
                raise HTTPException(409, "此设置已在其他界面改变，请刷新后再修改")
            kind = validate_value(field, data["value"])
            await oas.set_value(name, task, group, argument, data["value"], kind)
            updated = await oas.settings(name, task)
            actual = next((row.get("value") for row in updated.get(group, []) if isinstance(row, dict) and row.get("name") == argument), None)
            confirmed = type(actual) is type(data["value"]) and actual == data["value"]
            if kind == "number" and type(actual) in (int, float):
                confirmed = math.isfinite(actual) and actual == data["value"]
            if not confirmed:
                return JSONResponse({"detail": "后端返回的当前值与提交值不同，保存未确认，请刷新检查", "current_value": actual}, status_code=409)
            return {"ok": True, "value": actual}

    @app.post("/api/accounts/{name}/actions")
    async def actions(request: Request, name: str):
        require_session(request, write=True)
        data = await body(request, {"action"})
        if data["action"] not in ("start", "stop"):
            raise HTTPException(422, "只支持启动或停止当前配置")
        await check_account(name)
        lock = locks.setdefault(name, asyncio.Lock())
        if lock.locked() or time.monotonic() - action_cooldown.get(name, -100) < 2:
            raise HTTPException(409, "正在处理该配置，请稍后刷新状态")
        async with lock:
            action_cooldown[name] = time.monotonic()
            return await oas.action(name, data["action"])

    @app.websocket("/api/accounts/{name}/events")
    async def events(ws: WebSocket, name: str):
        token = ws.cookies.get(COOKIE_NAME)
        session = store.resolve(token) if authentication_required else anonymous_session
        if not session or event_counts.get(session.token_hash, 0) >= 8:
            await ws.close(code=1008)
            return
        # Reserve synchronously before the first await to make the cap atomic
        # across simultaneous handshakes for the same session.
        event_counts[session.token_hash] = event_counts.get(session.token_hash, 0) + 1
        def release_slot():
            event_counts[session.token_hash] -= 1
            if not event_counts[session.token_hash]:
                event_counts.pop(session.token_hash, None)
        try:
            await check_account(name)
            await ws.accept()
        except BaseException as exc:
            release_slot()
            if isinstance(exc, asyncio.CancelledError):
                raise
            try:
                await ws.close(code=1008)
            except (WebSocketDisconnect, RuntimeError):
                pass
            return
        async def consume_browser():
            # This socket is read-only. Never forward browser messages upstream.
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                payload = message.get("text", message.get("bytes", b""))
                if len(payload) > 4096:
                    await ws.close(code=1009)
                    return
        async def check_expiry():
            while True:
                await asyncio.sleep(1)
                if authentication_required and not store.resolve(token):
                    await ws.close(code=1008, reason="登录已过期，请重新登录")
                    return
        async def relay():
            try:
                async with oas.connection(name) as upstream:
                    await ws.send_json({"type": "connection", "connected": True})
                    async for raw in upstream:
                        await ws.send_json(oas.event(raw))
            except (UpstreamError, OSError):
                await ws.send_json({"type": "connection", "connected": False, "error": "OAS 实时连接断开，请重连"})
        tasks = [asyncio.create_task(f()) for f in (consume_browser, check_expiry, relay)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            release_slot()
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass

    def archive(record_id):
        match = ARCHIVE_ID.fullmatch(record_id)
        if not match or (match.group("config") is not None and not ACCOUNT_IDENTIFIER.fullmatch(match.group("config"))):
            raise HTTPException(404, "错误记录不存在")
        # Verify log/ as well, so an ancestor junction cannot redirect the
        # otherwise-safe numeric archive to a different directory tree.
        safe_path(backend_root, "log", "error")
        path = safe_path(error_root, record_id)
        if not path.is_dir():
            raise HTTPException(404, "错误记录不存在")
        return path

    async def error_owners(config):
        try:
            accounts = await oas.accounts()
        except UpstreamError:
            # Error evidence remains readable if OAS has stopped. Accept only
            # real configuration files, never deploy/template or log guesses.
            accounts = []
            try:
                config_root = safe_path(backend_root, "config")
                for candidate in config_root.glob("*.json"):
                    name = candidate.stem
                    if name in {"deploy", "template"} or not ACCOUNT_IDENTIFIER.fullmatch(name):
                        continue
                    if safe_path(config_root, candidate.name).is_file():
                        accounts.append(name)
            except (HTTPException, OSError):
                pass
        if config is not None and (not ACCOUNT_IDENTIFIER.fullmatch(config) or config not in accounts):
            raise HTTPException(404, "配置不存在")
        return await asyncio.to_thread(legacy_error_owners.read, accounts)

    def archive_info(record_id, config=None, legacy_owners=None):
        folder = archive(record_id)
        match = ARCHIVE_ID.fullmatch(record_id)
        owner, task = match.group("config"), None
        timestamp_ms = int(match.group("timestamp"))
        # New archives identify their exact configuration without guessing from
        # game logs. A malformed existing sidecar remains unassigned rather than
        # being shown under an account inferred from its directory name.
        if (folder / "metadata.json").exists():
            owner = None
            try:
                path = safe_path(error_root, record_id, "metadata.json")
                with path.open("rb") as stream:
                    raw = stream.read(16385)
                metadata = json.loads(raw) if len(raw) <= 16384 else None
                if isinstance(metadata, dict) and metadata.get("version") == 1:
                    candidate = metadata.get("config_name")
                    if isinstance(candidate, str) and ACCOUNT_IDENTIFIER.fullmatch(candidate):
                        owner = candidate
                    candidate_task = metadata.get("task")
                    if isinstance(candidate_task, str) and IDENTIFIER.fullmatch(candidate_task):
                        task = candidate_task
                    candidate_time = metadata.get("timestamp_ms")
                    if type(candidate_time) is int and 10**9 <= candidate_time < 10**17:
                        timestamp_ms = candidate_time
            except (OSError, ValueError, TypeError, HTTPException):
                pass
        elif owner is None:
            owner = (legacy_owners or {}).get(record_id)
        if config is not None and owner != config:
            raise HTTPException(404, "错误记录不存在")
        return {"id": record_id, "config_name": owner, "task": task,
                "timestamp_ms": timestamp_ms, "created_at": archive_time(str(timestamp_ms))}

    def available_images(record_id):
        folder = archive(record_id)
        result = []
        total = 0
        for path in sorted(folder.iterdir(), key=lambda p: p.name):
            if not IMAGE_NAME.fullmatch(path.name):
                continue
            try:
                checked = safe_path(error_root, record_id, path.name)
                size = checked.stat().st_size
                if not checked.is_file() or size > MAX_IMAGE or total + size > MAX_IMAGES_TOTAL:
                    continue
                total += size
                result.append(path.name)
            except (OSError, HTTPException):
                continue
            if len(result) >= 12:
                break
        return result

    @app.get("/api/errors")
    async def error_list(request: Request, config: str | None = Query(default=None)):
        require_session(request)
        legacy_owners = await error_owners(config)
        def read():
            records = []
            try:
                candidates = sorted(error_root.iterdir(), key=lambda p: p.name, reverse=True)
            except OSError:
                return records
            for path in candidates:
                if ARCHIVE_ID.fullmatch(path.name):
                    try:
                        records.append(archive_info(path.name, config, legacy_owners))
                    except HTTPException:
                        pass
            return sorted(records, key=lambda row: row["timestamp_ms"], reverse=True)[:500]
        return {"records": await asyncio.to_thread(read), "scope": config if config is not None else "全部配置",
                "config_name": config}

    @app.get("/api/errors/{record_id}")
    async def error_detail(request: Request, record_id: str, config: str | None = Query(default=None)):
        require_session(request)
        legacy_owners = await error_owners(config)
        def read():
            info = archive_info(record_id, config, legacy_owners)
            content, truncated = "", False
            try:
                path = safe_path(error_root, record_id, "log.txt")
                with path.open("rb") as stream:
                    size = path.stat().st_size
                    truncated = size > MAX_LOG
                    stream.seek(max(0, size - MAX_LOG))
                    content = stream.read(MAX_LOG).decode("utf-8", errors="replace")
            except (OSError, HTTPException):
                pass
            query = "?config=" + quote(config, safe="") if config is not None else ""
            images = [{"name": name, "url": f"/api/errors/{quote(record_id, safe='')}/images/{quote(name)}{query}"}
                      for name in available_images(record_id)]
            return {**info, "log": content,
                    "truncated": truncated, "images": images}
        return await asyncio.to_thread(read)

    @app.get("/api/errors/{record_id}/images/{filename}")
    async def error_image(request: Request, record_id: str, filename: str, config: str | None = Query(default=None)):
        require_session(request)
        legacy_owners = await error_owners(config)
        await asyncio.to_thread(archive_info, record_id, config, legacy_owners)
        if not IMAGE_NAME.fullmatch(filename) or filename not in await asyncio.to_thread(available_images, record_id):
            raise HTTPException(404, "图片不存在")
        path = safe_path(error_root, record_id, filename)
        def read_limited():
            with path.open("rb") as stream:
                return stream.read(MAX_IMAGE + 1)
        data = await asyncio.to_thread(read_limited)
        if len(data) > MAX_IMAGE:
            raise HTTPException(413, "图片过大")
        return Response(data, media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream")

    @app.get("/")
    async def index():
        path = ROOT / "static" / "index.html"
        if not path.is_file():
            return Response("OAS 面板界面尚未就绪", media_type="text/plain", status_code=503)
        return FileResponse(path)

    @app.get('/daily-feedback')
    async def daily_feedback_page():
        return FileResponse(ROOT / 'static' / 'daily-feedback.html')

    (ROOT / "static").mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="OAS 网页面板")
    parser.add_argument("--settings", type=Path, default=ROOT / "settings.json")
    arguments = parser.parse_args()
    settings = load_settings(arguments.settings)
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings["port"],
                proxy_headers=False, access_log=False, ws_max_size=65536, server_header=False)


if __name__ == "__main__":
    main()
