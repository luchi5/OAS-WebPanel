"""Offline / loopback-fixture tests. Nothing here accesses the real OAS port."""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app, validate_value, safe_path, load_settings, MAX_LOG, ArchiveLogOwners
from auth import AuthStore, COOKIE_NAME, token_hash
from oas_client import OasClient, UpstreamError, NoRedirect
from fastapi import HTTPException
from websockets.legacy.server import serve


class FakeUpstreamSocket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.queue.put_nowait(json.dumps({"state": 0}))
        self.queue.put_nowait(json.dumps({"schedule": {"pending": []}}))
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()

    async def send(self, value):
        self.sent.append(value)


class FakeOas:
    def __init__(self):
        self.values = {"chess_config": [
            {"name": "run_count", "title": "执行次数", "type": "integer", "value": 1},
            {"name": "enabled", "title": "启用", "type": "boolean", "value": False},
            {"name": "lineup", "title": "阵容", "type": "enum", "value": "a", "enumEnum": ["a", "b"]},
            {"name": "readonly", "type": "string", "value": "x", "readOnly": True},
            {"name": "interval", "type": "number", "value": 1.0},
        ]}
        self.writes = []
        self.actions = []
        self.socket = None
        self.statistics_reads = []

    async def accounts(self):
        return ["oas1"]

    async def menu(self):
        return {"Weekly Task": ["Chess"]}

    async def settings(self, account, task):
        assert account == "oas1" and task == "Chess"
        return copy.deepcopy(self.values)

    async def set_value(self, account, task, group, argument, value, kind):
        self.writes.append((account, task, group, argument, value, kind))
        for row in self.values[group]:
            if row["name"] == argument:
                row["value"] = value

    async def snapshot(self, name):
        return {"state": 0, "schedule": {"pending": []}, "connected": True}

    async def statistics_dates(self, name):
        self.statistics_reads.append((name, None))
        return {"script_name": name, "dates": ["2026-10-05"]}

    async def statistics_day(self, name, day):
        self.statistics_reads.append((name, day))
        return {"script_name": name, "tasks": {"Chess": {"run_count": 1}}, "total_task_run_count": 1}

    async def action(self, name, action):
        self.actions.append((name, action))
        return {"ok": True, "state": 1 if action == "start" else 0, "changed": True}

    @asynccontextmanager
    async def connection(self, name):
        self.socket = FakeUpstreamSocket()
        yield self.socket

    event = staticmethod(OasClient.event)


async def request(app, method, path, *, data=None, token=None, csrf=None,
                  host="127.0.0.1:22300", origin="auto", extra_headers=None, peer="127.0.0.1", query=None):
    headers = [(b"host", host.encode())]
    if origin == "auto":
        origin = "http://" + host
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if token:
        headers.append((b"cookie", f"{COOKIE_NAME}={token}".encode()))
    if csrf:
        headers.append((b"x-csrf-token", csrf.encode()))
    if extra_headers:
        headers.extend((key.encode(), value.encode()) for key, value in extra_headers)
    raw = json.dumps(data).encode() if data is not None else b""
    if data is not None:
        headers.append((b"content-type", b"application/json"))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": urlencode(query or {}).encode(), "root_path": "",
             "headers": headers, "client": (peer, 11111), "server": ("127.0.0.1", 22300)}
    sent = []
    delivered = False
    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.Future()
    async def send(message):
        sent.append(message)
    await asyncio.wait_for(app(scope, receive, send), 10)
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    response_headers = next(item["headers"] for item in sent if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    try:
        content = json.loads(raw)
    except (ValueError, UnicodeError):
        content = raw
    return status, content, response_headers


class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = AuthStore(self.root / "state")
        text = (self.root / "state" / "initial-login.txt").read_text(encoding="utf-8")
        self.initial = text.split("密码：", 1)[1].splitlines()[0]
        self.client = FakeOas()
        self.settings = {"backend_url": "http://127.0.0.1:22289", "backend_root": str(self.root / "fake-backend"),
                         "public_origin": "https://panel.example.com", "host": "127.0.0.1", "port": 22300}
        self.app = create_app(self.settings, client=self.client, auth=self.auth)
        self.token, self.session = self.auth.authenticate("admin", self.initial)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def ready(self):
        self.assertTrue(self.auth.change_password(self.session, self.initial, "A-new-password-for-tests-123"))
        self.session = self.auth.resolve(self.token)

    async def call(self, method, path, data=None, **kwargs):
        return await request(self.app, method, path, data=data, token=self.token,
                             csrf=self.session.csrf, **kwargs)

    async def test_requires_authentication_and_rejects_forged_token(self):
        for token in (None, "forged-token", "非ascii"):
            status, data, _ = await request(self.app, "GET", "/api/bootstrap", token=token if token != "非ascii" else None)
            self.assertEqual(status, 401)
        self.assertIsNone(self.auth.resolve("非ascii"))
        self.assertIsNone(self.auth.resolve("x" * 129))

    async def test_bootstrap_merges_full_and_additional_translations(self):
        root = Path(self.settings['backend_root'])
        base = root / 'module' / 'config' / 'i18n' / 'zh-CN.json'
        extra = root / 'assets' / 'i18n' / 'zh-CN.json'
        base.parent.mkdir(parents=True)
        extra.parent.mkdir(parents=True)
        base.write_text(json.dumps({'Script': '脚本', 'GlobalGame': '全局配置',
                                    'Orochi': '八岐大蛇', 'costume_main_type': '旧庭院', 'invalid': {}}), encoding='utf-8')
        extra.write_text(json.dumps({'Chess': '百鬼棋局', 'costume_main_type': '庭院皮肤', 'invalid_extra': 1}), encoding='utf-8')
        status, data, _ = await self.call('GET', '/api/bootstrap')
        self.assertEqual(status, 200)
        self.assertEqual(data['labels']['Script'], '脚本')
        self.assertEqual(data['labels']['GlobalGame'], '全局配置')
        self.assertEqual(data['labels']['Orochi'], '八岐大蛇')
        self.assertEqual(data['labels']['Chess'], '百鬼棋局')
        self.assertEqual(data['labels']['costume_main_type'], '庭院皮肤')
        self.assertNotIn('invalid', data['labels'])
        self.assertNotIn('invalid_extra', data['labels'])
        base.write_text('{invalid', encoding='utf-8')
        _, data, _ = await self.call('GET', '/api/bootstrap')
        self.assertEqual(data['labels']['Chess'], '百鬼棋局')
        self.assertNotIn('Script', data['labels'])

    async def test_session_token_only_stored_hashed(self):
        with closing(sqlite3.connect(self.auth.db_path)) as db:
            tokens = [row[0] for row in db.execute("SELECT token_hash FROM sessions")]
            encoded = db.execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertIn(token_hash(self.token), tokens)
        self.assertNotIn(self.token, tokens)
        self.assertNotIn(self.initial, encoded)
        self.assertTrue(encoded.startswith("pbkdf2_sha256$600000$"))

    async def test_login_cookie_and_first_change(self):
        status, data, headers = await request(self.app, "POST", "/api/login", data={"username": "admin", "password": self.initial})
        self.assertEqual(status, 200)
        self.assertTrue(data["must_change_password"])
        cookie = next(value.decode() for key, value in headers if key == b"set-cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        status, _, _ = await self.call("POST", "/api/accounts/oas1/actions", {"action": "start"})
        self.assertEqual(status, 403)
        self.assertFalse(self.client.actions)

    async def test_password_change_revokes_other_sessions_and_deletes_initial(self):
        other, _ = self.auth.authenticate("admin", self.initial)
        status, data, _ = await self.call("POST", "/api/password", {"current_password": self.initial, "new_password": "easy789"})
        self.assertEqual(status, 200)
        self.assertFalse(data["must_change_password"])
        self.assertIsNone(self.auth.resolve(other))
        self.assertIsNotNone(self.auth.resolve(self.token))
        self.assertFalse((self.root / "state" / "initial-login.txt").exists())
        status, _, _ = await request(self.app, "POST", "/api/login", data={"username": "admin", "password": self.initial})
        self.assertEqual(status, 401)
        status, data, _ = await request(self.app, "POST", "/api/login", data={"username": "admin", "password": "easy789"})
        self.assertEqual(status, 200)
        self.assertFalse(data["must_change_password"])

    async def test_simple_password_length_and_wrong_current_password(self):
        for new in ("", "short", 123456, "x" * 257):
            status, _, _ = await self.call("POST", "/api/password", {"current_password": self.initial, "new_password": new})
            self.assertEqual(status, 422)
        status, _, _ = await self.call("POST", "/api/password", {"current_password": "incorrect", "new_password": "simple"})
        self.assertEqual(status, 403)
        status, data, _ = await self.call("POST", "/api/password", {"current_password": self.initial, "new_password": "simple"})
        self.assertEqual(status, 200)
        self.assertFalse(data["must_change_password"])

    async def test_csrf_required_for_logout_password_and_writes(self):
        self.ready()
        cases = [("POST", "/api/logout", {}), ("POST", "/api/password", {}),
                 ("POST", "/api/accounts/oas1/actions", {"action": "start"}),
                 ("PUT", "/api/accounts/oas1/settings/Chess/chess_config/run_count", {"value": 2, "expected_value": 1})]
        for method, path, data in cases:
            status, _, _ = await request(self.app, method, path, token=self.token, data=data)
            self.assertEqual(status, 403)
        self.assertFalse(self.client.actions)
        self.assertFalse(self.client.writes)

    async def test_host_origin_and_public_https(self):
        cases = [dict(host="evil.example"), dict(origin="https://evil.example"),
                 dict(host="panel.example.com", origin="https://panel.example.com"),
                 dict(host="panel.example.com", origin="https://panel.example.com", peer="203.0.113.5", extra_headers=[("x-forwarded-proto", "https")]),
                 dict(extra_headers=[("host", "evil.example")]),
                 dict(extra_headers=[("sec-fetch-site", "cross-site")])]
        for args in cases:
            status, _, _ = await request(self.app, "GET", "/api/session", **args)
            self.assertEqual(status, 403, args)
        status, _, _ = await request(self.app, "POST", "/api/login", origin=None, data={"username": "admin", "password": self.initial})
        self.assertEqual(status, 403)
        status, _, headers = await request(self.app, "POST", "/api/login", host="panel.example.com", origin="https://panel.example.com",
                                           extra_headers=[("x-forwarded-proto", "https")], data={"username": "admin", "password": self.initial})
        self.assertEqual(status, 200)
        cookie = next(value.decode() for key, value in headers if key == b"set-cookie")
        self.assertIn("Secure", cookie)

    async def test_login_rate_limit(self):
        for _ in range(5):
            status, _, _ = await request(self.app, "POST", "/api/login", data={"username": "admin", "password": "wrong"})
            self.assertEqual(status, 401)
        status, _, _ = await request(self.app, "POST", "/api/login", data={"username": "admin", "password": self.initial})
        self.assertEqual(status, 429)

    async def test_only_verified_proxy_can_supply_limiter_ip(self):
        # Exercise limiter state without spending five password hashes.
        for _ in range(5):
            self.auth.check_login_rate("203.0.113.10")
            self.auth.check_login_rate("127.0.0.1")
        common = {"host": "panel.example.com", "origin": "https://panel.example.com"}
        data = {"username": "admin", "password": "wrong"}
        status, _, _ = await request(self.app, "POST", "/api/login", data=data, **common,
                                     extra_headers=[("x-forwarded-proto", "https"), ("x-forwarded-for", "203.0.113.10")])
        self.assertEqual(status, 429)
        status, _, _ = await request(self.app, "POST", "/api/login", data=data, **common,
                                     extra_headers=[("x-forwarded-proto", "https"), ("x-forwarded-for", "203.0.113.11")])
        self.assertEqual(status, 401)
        # A local client and ambiguous proxy header keep the transport limiter.
        status, _, _ = await request(self.app, "POST", "/api/login", data=data,
                                     extra_headers=[("x-forwarded-for", "203.0.113.12")])
        self.assertEqual(status, 429)
        status, _, _ = await request(self.app, "POST", "/api/login", data=data, **common,
                                     extra_headers=[("x-forwarded-proto", "https"), ("x-forwarded-for", "203.0.113.12, 127.0.0.1")])
        self.assertEqual(status, 429)

    async def test_readonly_routes_and_unknown_proxy_routes(self):
        for path in ("/api/bootstrap", "/api/accounts/oas1/snapshot", "/api/accounts/oas1/settings/Chess", "/api/errors"):
            status, _, _ = await self.call("GET", path)
            self.assertEqual(status, 200)
        for path in ("/home/kill_server", "/home/execute_update", "/oas1/start", "/api/proxy/home/kill_server", "/settings.json", "/state/initial-login.txt", "/docs", "/openapi.json"):
            status, _, _ = await self.call("GET", path)
            self.assertEqual(status, 404)
        self.assertFalse(self.client.actions)

    async def test_statistics_requires_session_known_account_and_valid_date(self):
        base = "/api/accounts/oas1/statistics"
        for path in (base + "/dates", base):
            status, _, _ = await request(self.app, "GET", path, query={"date": "2026-10-05"})
            self.assertEqual(status, 401)
        for name in ("xy", "..", "C:secret"):
            status, _, _ = await self.call("GET", "/api/accounts/" + name + "/statistics/dates")
            self.assertEqual(status, 404)
        for day in ("2026-1-05", "2026-02-30", "2026-10-05&action=start", "../log", "", "0000-01-01"):
            status, _, _ = await self.call("GET", base, query={"date": day})
            self.assertEqual(status, 422)
        self.assertEqual(self.client.statistics_reads, [])
        status, data, _ = await self.call("GET", base + "/dates")
        self.assertEqual(status, 200)
        self.assertEqual(data["dates"], ["2026-10-05"])
        status, data, _ = await self.call("GET", base, query={"date": "2026-10-05"})
        self.assertEqual(status, 200)
        self.assertEqual(data["tasks"]["Chess"]["run_count"], 1)
        self.assertEqual(self.client.statistics_reads, [("oas1", None), ("oas1", "2026-10-05")])
        self.assertFalse(self.client.actions)
        self.assertFalse(self.client.writes)

    async def test_statistics_backend_error_is_visible(self):
        async def fail(*args):
            raise UpstreamError("无法读取统计接口")
        self.client.statistics_dates = fail
        status, data, _ = await self.call("GET", "/api/accounts/oas1/statistics/dates")
        self.assertEqual(status, 502)
        self.assertIn("统计接口", data["detail"])

    async def test_fields_types_enum_and_conflict(self):
        self.ready()
        base = "/api/accounts/oas1/settings/Chess/chess_config/"
        cases = [("missing", 2, 1, 404), ("run_count", True, 1, 422), ("run_count", "2", 1, 422),
                 ("run_count", 2, 9, 409), ("lineup", "unknown", "a", 422), ("readonly", "y", "x", 403)]
        for field, value, expected, code in cases:
            status, _, _ = await self.call("PUT", base + field, {"value": value, "expected_value": expected})
            self.assertEqual(status, code)
        self.assertFalse(self.client.writes)
        status, data, _ = await self.call("PUT", base + "run_count", {"value": 2, "expected_value": 1})
        self.assertEqual(status, 200)
        self.assertEqual(data["value"], 2)
        self.assertEqual(self.client.writes[0], ("oas1", "Chess", "chess_config", "run_count", 2, "integer"))

    async def test_multi_enum_save_conflict_and_reject_empty_unknown_duplicates(self):
        self.ready()
        self.client.values['chess_config'].append({'name': 'courtyards', 'type': 'multi_enum', 'enumEnum': ['a', 'b'], 'minItems': 1, 'value': ['a']})
        path = '/api/accounts/oas1/settings/Chess/chess_config/courtyards'
        for value in ([], ['unknown'], ['a', 'a'], 'a', [True]):
            status, _, _ = await self.call('PUT', path, {'value': value, 'expected_value': ['a']})
            self.assertEqual(status, 422)
        status, data, _ = await self.call('PUT', path, {'value': ['a', 'b'], 'expected_value': ['a']})
        self.assertEqual(status, 200)
        self.assertEqual(data['value'], ['a', 'b'])
        self.assertEqual(self.client.writes[-1][-1], 'multi_enum')
        status, _, _ = await self.call('PUT', path, {'value': ['b'], 'expected_value': ['a']})
        self.assertEqual(status, 409)

    async def test_public_custom_port_keeps_origin_proxy_and_cookie_checks(self):
        settings = dict(self.settings, public_origin='https://panel.example.com:4443')
        app = create_app(settings, client=self.client, auth=self.auth)
        for host, origin, headers, expected in (
            ('panel.example.com:4443', 'https://panel.example.com:4443', [('x-forwarded-proto', 'https')], 200),
            ('panel.example.com:4443', 'https://panel.example.com', [('x-forwarded-proto', 'https')], 403),
            ('panel.example.com:443', 'https://panel.example.com', [('x-forwarded-proto', 'https')], 403),
            ('panel.example.com:4443', 'https://panel.example.com:4443', [], 403),
        ):
            status, _, _ = await request(app, 'GET', '/api/session', token=self.token, host=host, origin=origin, extra_headers=headers)
            self.assertEqual(status, expected)

    async def test_account_task_and_control_whitelist(self):
        self.ready()
        for path in ("/api/accounts/xy/actions", "/api/accounts/oas1/settings/Hidden/chess_config/run_count"):
            method = "POST" if path.endswith("actions") else "PUT"
            data = {"action": "start"} if method == "POST" else {"value": 2, "expected_value": 1}
            status, _, _ = await self.call(method, path, data)
            self.assertEqual(status, 404)
        status, _, _ = await self.call("POST", "/api/accounts/oas1/actions", {"action": "kill_server"})
        self.assertEqual(status, 422)
        self.assertFalse(self.client.actions)

    async def test_save_requires_matching_readback(self):
        self.ready()
        async def ignore_save(*args):
            pass
        self.client.set_value = ignore_save
        status, data, _ = await self.call("PUT", "/api/accounts/oas1/settings/Chess/chess_config/run_count", {"value": 2, "expected_value": 1})
        self.assertEqual(status, 409)
        self.assertEqual(data["current_value"], 1)
        self.assertNotIn("ok", data)

    async def test_number_conflict_check_accepts_javascript_integral_float(self):
        self.ready()
        status, data, _ = await self.call("PUT", "/api/accounts/oas1/settings/Chess/chess_config/interval", {"value": 1.5, "expected_value": 1})
        self.assertEqual(status, 200)
        self.assertEqual(data["value"], 1.5)

    async def test_action_rate_lock(self):
        self.ready()
        status, data, _ = await self.call("POST", "/api/accounts/oas1/actions", {"action": "start"})
        self.assertEqual(status, 200)
        self.assertEqual(data["state"], 1)
        status, _, _ = await self.call("POST", "/api/accounts/oas1/actions", {"action": "stop"})
        self.assertEqual(status, 409)
        self.assertEqual(self.client.actions, [("oas1", "start")])

    async def test_archive_auth_traversal_caps_and_whitelist(self):
        archive = self.root / "fake-backend" / "log" / "error" / "1759650000000"
        archive.mkdir(parents=True)
        (archive / "log.txt").write_bytes(b"a" * (MAX_LOG + 100))
        (archive / "screen.png").write_bytes(b"fake fixture")
        (archive / "secret.txt").write_text("not exposed")
        status, data, _ = await self.call("GET", "/api/errors/1759650000000")
        self.assertEqual(status, 200)
        self.assertTrue(data["truncated"])
        self.assertEqual(len(data["log"]), MAX_LOG)
        self.assertEqual([item["name"] for item in data["images"]], ["screen.png"])
        for path in ("/api/errors/../state", "/api/errors/1759650000000/images/secret.txt", "/api/errors/1759650000000/images/../log.txt"):
            status, _, _ = await self.call("GET", path)
            self.assertEqual(status, 404)
        status, _, _ = await request(self.app, "GET", "/api/errors/1759650000000/images/screen.png")
        self.assertEqual(status, 401)

    def error_fixture(self, record_id, owner=None, *, metadata=None):
        archive = self.root / "fake-backend" / "log" / "error" / record_id
        archive.mkdir(parents=True)
        (archive / "log.txt").write_text("fixture error", encoding="utf-8")
        (archive / "screen.png").write_bytes(b"fake fixture")
        if owner is not None:
            metadata = {"version": 1, "config_name": owner, "task": "Chess", "timestamp_ms": 1759650000000}
        if metadata is not None:
            (archive / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        return archive

    async def test_error_lists_use_exact_account_and_keep_global_api_compatible(self):
        async def accounts():
            return ["oas1", "oas2", "01-测试"]
        self.client.accounts = accounts
        self.error_fixture("1759650000001", "oas1")
        self.error_fixture("1759650000002", "oas2")
        self.error_fixture("1759650000003")
        self.error_fixture("1759650000004", "01-测试")
        for owner, expected in (("oas1", "1759650000001"), ("oas2", "1759650000002"), ("01-测试", "1759650000004")):
            status, data, _ = await self.call("GET", "/api/errors", query={"config": owner})
            self.assertEqual(status, 200)
            self.assertEqual(data["scope"], owner)
            self.assertEqual(data["config_name"], owner)
            self.assertEqual([row["id"] for row in data["records"]], [expected])
            self.assertEqual(data["records"][0]["config_name"], owner)
            self.assertEqual(data["records"][0]["task"], "Chess")
        status, data, _ = await self.call("GET", "/api/errors")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["records"]), 4)
        self.assertEqual(data["scope"], "全部配置")
        status, _, _ = await request(self.app, "GET", "/api/errors", query={"config": "oas1"})
        self.assertEqual(status, 401)

    async def test_error_details_and_images_cannot_cross_selected_account(self):
        async def accounts():
            return ["oas1", "oas2"]
        self.client.accounts = accounts
        self.error_fixture("1759650000001", "oas1")
        self.error_fixture("1759650000002")
        detail = "/api/errors/1759650000001"
        image = detail + "/images/screen.png"
        status, data, _ = await self.call("GET", detail, query={"config": "oas1"})
        self.assertEqual(status, 200)
        self.assertEqual(data["config_name"], "oas1")
        self.assertEqual(data["images"][0]["url"], image + "?config=oas1")
        status, data, _ = await self.call("GET", image, query={"config": "oas1"})
        self.assertEqual((status, data), (200, b"fake fixture"))
        for path in (detail, image, "/api/errors/1759650000002", "/api/errors/1759650000002/images/screen.png"):
            status, _, _ = await self.call("GET", path, query={"config": "oas2"})
            self.assertEqual(status, 404, path)
        for selected in ("", "unknown", "../oas1"):
            for path in ("/api/errors", detail, image):
                status, _, _ = await self.call("GET", path, query={"config": selected})
                self.assertEqual(status, 404, (path, selected))

    async def test_error_metadata_overrides_legacy_prefix_and_unknown_is_not_assigned(self):
        self.error_fixture("oas1_1759650000001")
        self.error_fixture("oas1-1759650000002", metadata={"version": 1, "config_name": "other", "timestamp_ms": 1759650000002})
        self.error_fixture("oas1_1759650000003", metadata={"version": 1, "config_name": "../oas1"})
        bad = self.error_fixture("oas1_1759650000004")
        (bad / "metadata.json").write_text("not json", encoding="utf-8")
        self.error_fixture("1759650000005")
        status, data, _ = await self.call("GET", "/api/errors", query={"config": "oas1"})
        self.assertEqual(status, 200)
        self.assertEqual([row["id"] for row in data["records"]], ["oas1_1759650000001"])
        self.assertTrue(data["records"][0]["created_at"])
        for record in ("oas1-1759650000002", "oas1_1759650000003", "oas1_1759650000004", "1759650000005"):
            status, _, _ = await self.call("GET", "/api/errors/" + record, query={"config": "oas1"})
            self.assertEqual(status, 404, record)

    async def test_error_lists_sort_legacy_prefixes_by_timestamp(self):
        self.error_fixture("oas1_1759650000001")
        self.error_fixture("1759650000002", "oas1")
        self.error_fixture("oas1-1759650000003")
        status, data, _ = await self.call("GET", "/api/errors", query={"config": "oas1"})
        self.assertEqual(status, 200)
        self.assertEqual([row["id"] for row in data["records"]],
                         ["oas1-1759650000003", "oas1_1759650000001", "1759650000002"])

    async def test_legacy_worker_errors_gain_ownership_from_incremental_account_logs(self):
        async def accounts():
            return ["oas1", "oas2"]
        self.client.accounts = accounts
        self.error_fixture("1759650000001")
        self.error_fixture("1759650000002")
        malformed = self.error_fixture("1759650000003")
        (malformed / "metadata.json").write_text("invalid", encoding="utf-8")
        log = self.root / "fake-backend" / "log" / "2026-10-06_oas1.txt"
        log.write_text("WARNING | Saving error: ./log/error/1759650000001\n", encoding="utf-8")
        status, data, _ = await self.call("GET", "/api/errors", query={"config": "oas1"})
        self.assertEqual((status, [row["id"] for row in data["records"]]), (200, ["1759650000001"]))
        with log.open("a", encoding="utf-8") as stream:
            stream.write("WARNING | Saving error: ./log/error/1759650000002")
        _, data, _ = await self.call("GET", "/api/errors", query={"config": "oas1"})
        self.assertEqual([row["id"] for row in data["records"]], ["1759650000001"])
        with log.open("a", encoding="utf-8") as stream:
            stream.write("\nWARNING | Saving error: ./log/error/1759650000003\n")
        _, data, _ = await self.call("GET", "/api/errors", query={"config": "oas1"})
        self.assertEqual([row["id"] for row in data["records"]], ["1759650000002", "1759650000001"])
        status, data, _ = await self.call("GET", "/api/errors/1759650000002", query={"config": "oas1"})
        self.assertEqual((status, data["config_name"]), (200, "oas1"))
        status, _, _ = await self.call("GET", "/api/errors/1759650000002/images/screen.png", query={"config": "oas1"})
        self.assertEqual(status, 200)
        conflict = log.with_name("2026-10-06_oas2.txt")
        conflict.write_text("WARNING | Saving error: ./log/error/1759650000001\n", encoding="utf-8")
        for owner in ("oas1", "oas2"):
            status, _, _ = await self.call("GET", "/api/errors/1759650000001", query={"config": owner})
            self.assertEqual(status, 404, owner)
            status, _, _ = await self.call("GET", "/api/errors/1759650000001/images/screen.png", query={"config": owner})
            self.assertEqual(status, 404, owner)
        conflict.write_text("", encoding="utf-8")
        status, data, _ = await self.call("GET", "/api/errors/1759650000001", query={"config": "oas1"})
        self.assertEqual((status, data["config_name"]), (200, "oas1"))

    async def test_error_log_cache_avoids_unchanged_reads_and_forgets_removed_logs(self):
        root = self.root / "fake-backend"
        log_root = root / "log"
        log_root.mkdir(parents=True)
        log = log_root / "2026-10-06_oas1.txt"
        log.write_text("WARNING | Saving error: ./log/error/1759650000001\n", encoding="utf-8")
        (log_root / "2026-10-06_unknown.txt").write_text("WARNING | Saving error: ./log/error/1759650000002\n", encoding="utf-8")
        (log_root / "2026-10-06_oas1-extra.txt").write_text("WARNING | Saving error: ./log/error/1759650000003\n", encoding="utf-8")
        reader = ArchiveLogOwners(root)
        expected = {"1759650000001": "oas1"}
        self.assertEqual(reader.read(["oas1"]), expected)
        with patch.object(Path, "open", side_effect=AssertionError("unchanged logs must not be reopened")):
            self.assertEqual(reader.read(["oas1"]), expected)
        log.unlink()
        self.assertEqual(reader.read(["oas1"]), {})

    async def test_error_ownership_falls_back_to_real_configs_when_backend_is_offline(self):
        async def accounts():
            raise UpstreamError("offline")
        self.client.accounts = accounts
        self.error_fixture("1759650000001", "oas1")
        config = self.root / "fake-backend" / "config"
        config.mkdir()
        for name in ("oas1", "template", "deploy"):
            (config / (name + ".json")).write_text("{}", encoding="utf-8")
        status, data, _ = await self.call("GET", "/api/errors", query={"config": "oas1"})
        self.assertEqual((status, len(data["records"])), (200, 1))
        for name in ("template", "deploy", "oas2"):
            status, _, _ = await self.call("GET", "/api/errors", query={"config": name})
            self.assertEqual(status, 404, name)

    async def test_session_expiry_and_logout(self):
        with closing(sqlite3.connect(self.auth.db_path)) as db:
            db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
            db.commit()
        self.assertIsNone(self.auth.resolve(self.token))
        status, _, _ = await self.call("GET", "/api/bootstrap")
        self.assertEqual(status, 401)

    async def test_websocket_is_readonly_and_revoked_on_logout(self):
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        scope = {"type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "ws",
                 "path": "/api/accounts/oas1/events", "raw_path": b"/api/accounts/oas1/events", "query_string": b"", "root_path": "",
                 "headers": [(b"host", b"127.0.0.1:22300"), (b"origin", b"http://127.0.0.1:22300"),
                             (b"cookie", f"{COOKIE_NAME}={self.token}".encode())],
                 "client": ("127.0.0.1", 11223), "server": ("127.0.0.1", 22300), "subprotocols": []}
        task = asyncio.create_task(self.app(scope, incoming.get, outgoing.put))
        first = await asyncio.wait_for(outgoing.get(), 2)
        self.assertEqual(first["type"], "websocket.accept")
        await incoming.put({"type": "websocket.receive", "text": "start"})
        await asyncio.sleep(0.05)
        self.assertFalse(self.client.actions)
        self.assertFalse(self.client.socket.sent)
        self.auth.logout(self.session)
        closed = None
        while closed is None:
            item = await asyncio.wait_for(outgoing.get(), 2)
            if item["type"] == "websocket.close":
                closed = item
        self.assertEqual(closed["code"], 1008)
        await asyncio.wait_for(task, 2)

    async def test_concurrent_websockets_reserve_only_eight_slots(self):
        async def delayed_accounts():
            await asyncio.sleep(0.05)
            return ["oas1"]
        self.client.accounts = delayed_accounts
        incoming = [asyncio.Queue() for _ in range(16)]
        outgoing = [asyncio.Queue() for _ in range(16)]
        tasks = []
        for index in range(16):
            scope = {"type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "ws",
                     "path": "/api/accounts/oas1/events", "raw_path": b"/api/accounts/oas1/events", "query_string": b"", "root_path": "",
                     "headers": [(b"host", b"127.0.0.1:22300"), (b"origin", b"http://127.0.0.1:22300"),
                                 (b"cookie", f"{COOKIE_NAME}={self.token}".encode())],
                     "client": ("127.0.0.1", 11223 + index), "server": ("127.0.0.1", 22300), "subprotocols": []}
            await incoming[index].put({"type": "websocket.connect"})
            tasks.append(asyncio.create_task(self.app(scope, incoming[index].get, outgoing[index].put)))
        try:
            first_messages = await asyncio.gather(*(asyncio.wait_for(queue.get(), 2) for queue in outgoing))
            self.assertEqual(sum(item["type"] == "websocket.accept" for item in first_messages), 8)
            self.assertEqual(sum(item["type"] == "websocket.close" for item in first_messages), 8)
        finally:
            for queue in incoming:
                await queue.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(asyncio.gather(*tasks), 3)


class OasProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_statistics_fixed_path_date_validation_and_response_account(self):
        client = OasClient("http://127.0.0.1:22289")
        calls = []
        async def capture(path, **kwargs):
            calls.append((path, kwargs))
            return {"script_name": "测试账号", "dates": ["2026-10-04", "2026-10-05", "2026-10-04"], "tasks": {}}
        client._json = capture
        result = await client.statistics_dates("测试账号")
        self.assertEqual(result["dates"], ["2026-10-05", "2026-10-04"])
        await client.statistics_day("测试账号", "2026-10-05")
        self.assertTrue(all(path.startswith("/stats/%E6%B5%8B") and path.isascii() for path, _ in calls))
        self.assertEqual(calls[-1][1]["query"], {"date": "2026-10-05"})
        for account, day in (("../xy", "2026-10-05"), ("oas1", "2026-2-05"), ("oas1", "2026-02-30")):
            with self.assertRaises(ValueError):
                await client.statistics_day(account, day)
        self.assertEqual(len(calls), 2)
        with self.assertRaises(UpstreamError):
            await client.statistics_dates("oas1")

    async def test_statistics_rejects_malformed_dates_and_documents(self):
        client = OasClient("http://127.0.0.1:22289")
        for result in ([], {"script_name": "oas1", "dates": ["2026-02-30"]},
                       {"script_name": "oas1", "dates": [None]}, {"script_name": "oas1", "dates": "2026-10-05"}):
            async def capture(*args, **kwargs):
                return result
            client._json = capture
            with self.assertRaises(UpstreamError):
                await client.statistics_dates("oas1")
        async def wrong_document(*args, **kwargs):
            return {"script_name": "xy", "tasks": {}}
        client._json = wrong_document
        with self.assertRaises(UpstreamError):
            await client.statistics_day("oas1", "2026-10-05")

    async def test_multi_enum_query_uses_json_not_python_list_text(self):
        client = OasClient('http://127.0.0.1:22289')
        queries = []
        async def capture(path, **kwargs):
            queries.append(kwargs['query'])
            return True
        client._json = capture
        await client.set_value('oas1', 'GlobalGame', 'costume_config', 'costume_main_type', ['costume_main', 'costume_main_17'], 'multi_enum')
        self.assertEqual(queries[0]['types'], 'string')
        self.assertEqual(json.loads(queries[0]['value']), ['costume_main', 'costume_main_17'])

    async def test_chinese_account_is_quoted_for_http(self):
        client = OasClient("http://127.0.0.1:22289")
        calls = []
        async def capture(path, **kwargs):
            calls.append((path, kwargs))
            return True if kwargs.get("method") == "PUT" else {}
        client._json = capture
        await client.settings("测试账号", "Chess")
        await client.set_value("测试账号", "Chess", "chess_config", "run_count", 2, "integer")
        self.assertTrue(all("%E6%B5%8B" in path for path, _ in calls))
        self.assertTrue(all(path.isascii() for path, _ in calls))

    async def test_timedelta_wire_type_does_not_truncate_day_tens(self):
        client = OasClient("http://127.0.0.1:22289")
        kinds = []
        async def capture(path, **kwargs):
            kinds.append(kwargs["query"]["types"])
            return True
        client._json = capture
        for value in ("00 00:30:00", "09 23:59:59", "10 02:03:04", "31 00:00:00"):
            await client.set_value("oas1", "Chess", "chess_config", "limit", value, "time_delta")
        self.assertEqual(kinds, ["time_delta", "time_delta", "string", "string"])

    async def test_real_fixture_websocket_commands_only(self):
        commands = []
        async def upstream(ws, path):
            self.assertEqual(path, "/ws/oas1")
            await ws.send(json.dumps({"state": 0}))
            await ws.send(json.dumps({"schedule": {"pending": []}}))
            async for command in ws:
                commands.append(command)
                await ws.send(json.dumps({"state": 1 if command == "start" else 0}))
        async with serve(upstream, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            self.assertNotEqual(port, 22289)
            client = OasClient("http://127.0.0.1:22289")
            client.ws_base = f"ws://127.0.0.1:{port}"
            result = await client.action("oas1", "start")
            self.assertEqual(result["state"], 1)
            self.assertEqual(commands, ["start"])
            snapshot = await client.snapshot("oas1")
            self.assertTrue(snapshot["connected"])
            self.assertEqual(commands, ["start"])

    async def test_stop_and_idempotent_start(self):
        commands = []
        async def upstream(ws, path):
            await ws.send('{"state":1}')
            await ws.send('{"schedule":{}}')
            async for command in ws:
                commands.append(command)
                await ws.send('{"state":0}')
        async with serve(upstream, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            self.assertNotEqual(port, 22289)
            client = OasClient("http://127.0.0.1:22289")
            client.ws_base = f"ws://127.0.0.1:{port}"
            result = await client.action("oas1", "start")
            self.assertFalse(result["changed"])
            result = await client.action("oas1", "stop")
            self.assertEqual(result["state"], 0)
            self.assertEqual(commands, ["stop"])

    async def test_local_target_and_no_http_control_routes(self):
        for url in ("http://example.invalid:22289", "https://127.0.0.1:22289", "http://127.0.0.1:80", "http://127.0.0.1:22289/extra", "http://user@localhost:22289"):
            with self.assertRaises(ValueError):
                OasClient(url)
        client = OasClient("http://127.0.0.1:22289")
        async def forbidden(*args, **kwargs):
            self.fail("The control path must never call HTTP")
        client._json = forbidden
        with self.assertRaises(ValueError):
            await client.action("oas1", "kill_server")


class ValueTests(unittest.TestCase):
    def test_redirect_handler_never_follows_location(self):
        with self.assertRaises(UpstreamError):
            NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://example.invalid/")

    def test_plain_text_cannot_be_silently_coerced_by_mainline(self):
        for value in ("true", "false", "12:34:56", "10 12:34:56", "2026-10-05 12:34:56"):
            with self.assertRaises(HTTPException):
                validate_value({"type": "string"}, value)
        self.assertEqual(validate_value({"type": "string"}, "a normal string"), "string")

    def test_time_types_and_unsupported_values(self):
        for value in ("00 00:30:00", "09 23:59:59", "10 02:03:04", "31 00:00:00"):
            self.assertEqual(validate_value({"type": "time_delta"}, value), "time_delta")
        for value in ("32 00:00:00", "99 00:00:00", "00 24:00:00", "0 1:00:00"):
            with self.assertRaises(HTTPException):
                validate_value({"type": "time_delta"}, value)
        for value in ("24:00:00", "0:00:00", "12:60:00"):
            with self.assertRaises(HTTPException):
                validate_value({"type": "time"}, value)
        with self.assertRaises(HTTPException):
            validate_value({"type": "number"}, float("nan"))
        with self.assertRaises(HTTPException):
            validate_value({"type": "object"}, {})


if __name__ == "__main__":
    unittest.main()
