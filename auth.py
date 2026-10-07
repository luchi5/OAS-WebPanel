"""Local administrator identity and revocable, hashed sessions."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path

COOKIE_NAME = "oas_panel_session"
SESSION_SECONDS = 8 * 60 * 60
PBKDF2_ITERATIONS = 600_000


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or not 100_000 <= int(rounds) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Session:
    token_hash: str
    username: str
    csrf: str
    must_change_password: bool
    expires: float

    def public(self) -> dict:
        return {"authenticated": True, "username": self.username, "csrf": self.csrf,
                "must_change_password": self.must_change_password}


class AuthStore:
    def __init__(self, state_dir: Path, *, session_seconds: int = SESSION_SECONDS):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "auth.sqlite3"
        self.session_seconds = session_seconds
        self._lock = threading.RLock()
        self._attempts: dict[str, list[float]] = {}
        self._dummy_hash = password_hash(secrets.token_urlsafe(32))
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
                    must_change INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                    csrf TEXT NOT NULL, expires REAL NOT NULL
                );
            """)
            if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                password = secrets.token_urlsafe(27)
                db.execute("INSERT INTO users VALUES (?, ?, 1)", ("admin", password_hash(password)))
                # Never print the bootstrap credential or include it in API/log output.
                path = self.state_dir / "initial-login.txt"
                path.write_text(
                    "OAS 网页面板初始登录\n用户名：admin\n密码：" + password
                    + "\n首次登录后必须修改密码；修改后本文件会自动删除。\n", encoding="utf-8")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def check_login_rate(self, peer: str) -> bool:
        # Caller accepts proxy client IP only after validating the local proxy.
        now = time.monotonic()
        with self._lock:
            self._attempts = {key: [t for t in values if now - t < 900]
                              for key, values in self._attempts.items() if any(now - t < 900 for t in values)}
            recent = self._attempts.setdefault(peer, [])
            if sum(now - stamp < 60 for stamp in recent) >= 5 or len(recent) >= 20:
                return False
            recent.append(now)
            return True

    def authenticate(self, username: str, password: str) -> tuple[str, Session] | None:
        with self._lock, self._db() as db:
            user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            encoded = user["password_hash"] if user else self._dummy_hash
            correct = password_matches(password, encoded)
            if not user or not correct:
                return None
            token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            expires = time.time() + self.session_seconds
            db.execute("DELETE FROM sessions WHERE expires <= ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (token_hash(token), username, csrf, expires))
            # Bound stored sessions while preserving the newest logins.
            db.execute("DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions ORDER BY expires DESC LIMIT -1 OFFSET 32)")
            return token, Session(token_hash(token), username, csrf, bool(user["must_change"]), expires)

    def resolve(self, token: str | None) -> Session | None:
        if not token or len(token) > 128 or not token.isascii():
            return None
        with self._lock, self._db() as db:
            row = db.execute("""SELECT s.*, u.must_change FROM sessions s JOIN users u
                                ON u.username=s.username WHERE s.token_hash=? AND s.expires>?""",
                             (token_hash(token), time.time())).fetchone()
            if not row:
                return None
            return Session(row["token_hash"], row["username"], row["csrf"], bool(row["must_change"]), row["expires"])

    def logout(self, session: Session) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (session.token_hash,))

    def change_password(self, session: Session, current: str, new: str) -> bool:
        with self._lock, self._db() as db:
            user = db.execute("SELECT * FROM users WHERE username=?", (session.username,)).fetchone()
            if not user or not password_matches(current, user["password_hash"]):
                return False
            db.execute("UPDATE users SET password_hash=?, must_change=0 WHERE username=?", (password_hash(new), session.username))
            db.execute("DELETE FROM sessions WHERE username=? AND token_hash<>?", (session.username, session.token_hash))
        try:
            (self.state_dir / "initial-login.txt").unlink(missing_ok=True)
        except OSError:
            pass
        return True
