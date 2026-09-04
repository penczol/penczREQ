from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .i18n import normalize_language

from .security import (
    dummy_verify_password,
    hash_password,
    normalize_username,
    validate_password,
    validate_username,
    verify_password,
)


CONTROL_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS control_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 1 CHECK (must_change_password IN (0, 1)),
    created_at TEXT NOT NULL,
    password_changed_at TEXT,
    last_login_at TEXT,
    last_login_ip TEXT,
    language TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'pl'))
);

CREATE TABLE IF NOT EXISTS control_sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES control_users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ip_address TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_control_sessions_token ON control_sessions(token_hash);

CREATE TABLE IF NOT EXISTS control_throttles (
    ip_key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    window_started_at TEXT NOT NULL,
    blocked_until TEXT,
    block_level INTEGER NOT NULL DEFAULT 0,
    escalation_until TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _token_hash(token: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


class ControlError(RuntimeError):
    pass


class ControlStore:
    def __init__(self, path: Path, session_secret: str):
        self.path = path
        self.session_secret = session_secret
        self.bootstrap_file = path.parent / "CONTROL-FIRST-LOGIN.txt"

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(CONTROL_SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(control_users)")}
            if "language" not in columns:
                conn.execute(
                    "ALTER TABLE control_users ADD COLUMN language TEXT NOT NULL DEFAULT 'pl' "
                    "CHECK (language IN ('en', 'pl'))"
                )

    def bootstrap(self, username: str, password: str, *, development: bool) -> bool:
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM control_users LIMIT 1").fetchone():
                return False
        username = normalize_username(username)
        generated = False
        if not password and development:
            password = f"C{secrets.token_urlsafe(18)}9a"
            generated = True
        if error := validate_username(username):
            raise ControlError(error)
        if error := validate_password(password, username=username):
            raise ControlError("Nie można utworzyć administratora Control. " + error)
        now = _now().isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO control_users
                (username, password_hash, must_change_password, created_at, password_changed_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (username, hash_password(password), now, now),
            )
        if generated:
            self.bootstrap_file.write_text(
                f"penczREQ Control — JEDNORAZOWE DANE STARTOWE\nLogin: {username}\nHasło: {password}\n"
                "Plik zostanie usunięty po zmianie hasła.\n",
                encoding="utf-8",
            )
            try:
                self.bootstrap_file.chmod(0o600)
            except OSError:
                pass
        return True

    def apply_recovery(self, nonce: str, new_password: str) -> bool:
        if not nonce:
            return False
        nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        with self.transaction() as conn:
            used = conn.execute("SELECT value FROM control_meta WHERE key = 'recovery_nonce_hash'").fetchone()
            if used and hmac.compare_digest(str(used["value"]), nonce_hash):
                return False
            user = conn.execute("SELECT * FROM control_users ORDER BY id LIMIT 1").fetchone()
            if not user:
                raise ControlError("Najpierw utwórz konto Control.")
            if error := validate_password(new_password, username=user["username"]):
                raise ControlError("Hasło odzyskiwania jest nieprawidłowe. " + error)
            now = _now().isoformat()
            conn.execute(
                "UPDATE control_users SET password_hash = ?, must_change_password = 1, password_changed_at = ? WHERE id = ?",
                (hash_password(new_password), now, user["id"]),
            )
            conn.execute("DELETE FROM control_sessions")
            conn.execute("DELETE FROM control_throttles")
            conn.execute(
                """
                INSERT INTO control_meta (key, value, updated_at) VALUES ('recovery_nonce_hash', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (nonce_hash, now),
            )
        return True

    def authenticate(self, username: str, password: str, ip_address: str):
        normalized = normalize_username(username)
        with self.connect() as conn:
            user = conn.execute(
                "SELECT * FROM control_users WHERE username = ? COLLATE NOCASE", (normalized,)
            ).fetchone()
        if not user:
            dummy_verify_password(password)
            return None
        if not verify_password(user["password_hash"], password):
            return None
        with self.transaction() as conn:
            conn.execute(
                "UPDATE control_users SET last_login_at = ?, last_login_ip = ? WHERE id = ?",
                (_now().isoformat(), ip_address, user["id"]),
            )
        return self.user_by_id(int(user["id"]))

    def user_by_id(self, user_id: int):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM control_users WHERE id = ?", (user_id,)).fetchone()

    def set_language(self, user_id: int, language: str) -> None:
        if language not in {"en", "pl"}:
            raise ControlError("Nieprawidłowy język interfejsu.")
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE control_users SET language = ? WHERE id = ?",
                (normalize_language(language), user_id),
            )
            if cursor.rowcount != 1:
                raise ControlError("Nie znaleziono konta Control.")

    def verify_current_password(self, user_id: int, password: str) -> bool:
        user = self.user_by_id(user_id)
        return bool(user and verify_password(user["password_hash"], password))

    def rename_user(self, user_id: int, current_password: str, new_username: str) -> None:
        user = self.user_by_id(user_id)
        if not user or not verify_password(user["password_hash"], current_password):
            raise ControlError("Hasło panelu Control jest nieprawidłowe.")
        username = normalize_username(new_username)
        if error := validate_username(username):
            raise ControlError(error)
        try:
            with self.transaction() as conn:
                conn.execute("UPDATE control_users SET username = ? WHERE id = ?", (username, user_id))
        except sqlite3.IntegrityError as exc:
            raise ControlError("Taki login panelu Control już istnieje.") from exc

    def change_password(self, user_id: int, current_password: str, new_password: str) -> None:
        user = self.user_by_id(user_id)
        if not user or not verify_password(user["password_hash"], current_password):
            raise ControlError("Obecne hasło panelu jest nieprawidłowe.")
        if error := validate_password(new_password, username=user["username"], current=current_password):
            raise ControlError(error)
        with self.transaction() as conn:
            conn.execute(
                "UPDATE control_users SET password_hash = ?, must_change_password = 0, password_changed_at = ? WHERE id = ?",
                (hash_password(new_password), _now().isoformat(), user_id),
            )
            conn.execute("DELETE FROM control_sessions WHERE user_id = ?", (user_id,))
        self.bootstrap_file.unlink(missing_ok=True)

    def create_session(self, user_id: int, ip_address: str, hours: int) -> tuple[str, str, datetime]:
        raw_token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        now = _now()
        expires = now + timedelta(hours=hours)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO control_sessions
                (id, token_hash, user_id, csrf_token, created_at, expires_at, last_seen_at, ip_address)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    secrets.token_hex(16), _token_hash(raw_token, self.session_secret), user_id, csrf,
                    now.isoformat(), expires.isoformat(), now.isoformat(), ip_address,
                ),
            )
        return raw_token, csrf, expires

    def session_user(self, raw_token: str | None, ip_address: str, idle_minutes: int):
        if not raw_token:
            return None
        now = _now()
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT u.*, s.id AS session_id, s.csrf_token, s.expires_at,
                       s.last_seen_at, s.ip_address AS session_ip
                FROM control_sessions s JOIN control_users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ?
                """,
                (_token_hash(raw_token, self.session_secret), now.isoformat()),
            ).fetchone()
            if not row:
                return None
            last_seen = _parse(row["last_seen_at"])
            if not last_seen or last_seen + timedelta(minutes=idle_minutes) <= now:
                conn.execute("DELETE FROM control_sessions WHERE id = ?", (row["session_id"],))
                return None
            if not hmac.compare_digest(str(row["session_ip"]), ip_address):
                conn.execute("DELETE FROM control_sessions WHERE id = ?", (row["session_id"],))
                return None
            if last_seen + timedelta(seconds=60) <= now:
                conn.execute(
                    "UPDATE control_sessions SET last_seen_at = ? WHERE id = ?",
                    (now.isoformat(), row["session_id"]),
                )
            return row

    def delete_session(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM control_sessions WHERE token_hash = ?",
                (_token_hash(raw_token, self.session_secret),),
            )

    def login_gate(self, ip_key: str) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT blocked_until FROM control_throttles WHERE ip_key = ?", (ip_key,)).fetchone()
        blocked_until = _parse(row["blocked_until"]) if row else None
        if blocked_until and blocked_until > _now():
            return max(1, int((blocked_until - _now()).total_seconds()))
        return 0

    def record_failure(self, ip_key: str) -> int:
        existing_retry = self.login_gate(ip_key)
        if existing_retry:
            return existing_retry
        now = _now()
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM control_throttles WHERE ip_key = ?", (ip_key,)).fetchone()
            window = _parse(row["window_started_at"]) if row else None
            failures = int(row["failures"] or 0) + 1 if window and window > now - timedelta(minutes=10) else 1
            level = int(row["block_level"] or 0) if row else 0
            escalation = _parse(row["escalation_until"]) if row else None
            blocked_until = None
            if failures >= 5:
                level = min(3, level + 1) if escalation and escalation > now else 1
                blocked_until = now + timedelta(minutes=(15, 60, 1440)[level - 1])
                failures = 0
            conn.execute(
                """
                INSERT INTO control_throttles
                (ip_key, failures, window_started_at, blocked_until, block_level, escalation_until, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ip_key) DO UPDATE SET failures = excluded.failures,
                    window_started_at = excluded.window_started_at, blocked_until = excluded.blocked_until,
                    block_level = excluded.block_level, escalation_until = excluded.escalation_until,
                    updated_at = excluded.updated_at
                """,
                (
                    ip_key, failures, now.isoformat(), blocked_until.isoformat() if blocked_until else None,
                    level, (now + timedelta(hours=24)).isoformat() if blocked_until else (row["escalation_until"] if row else None),
                    now.isoformat(),
                ),
            )
        return int((blocked_until - now).total_seconds()) if blocked_until else 0

    def clear_failures(self, ip_key: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM control_throttles WHERE ip_key = ?", (ip_key,))

    def list_throttles(self, limit: int = 500) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM control_throttles ORDER BY blocked_until IS NOT NULL DESC, updated_at DESC LIMIT ?",
                (min(max(limit, 1), 2000),),
            ).fetchall()
        return [
            {
                "source": "control", "scope": "ip", "key": row["ip_key"],
                "display_key": row["ip_key"], "failures": int(row["failures"] or 0),
                "window_started_at": row["window_started_at"],
                "last_failure_at": row["updated_at"], "blocked_until": row["blocked_until"],
                "block_level": int(row["block_level"] or 0),
                "escalation_until": row["escalation_until"], "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def reset_throttle(self, ip_key: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM control_throttles WHERE ip_key = ?", (ip_key,))
        return cursor.rowcount == 1

    def prune(self) -> None:
        now = _now()
        with self.transaction() as conn:
            conn.execute("DELETE FROM control_sessions WHERE expires_at <= ?", (now.isoformat(),))
            conn.execute(
                "DELETE FROM control_throttles WHERE updated_at < ? AND (blocked_until IS NULL OR blocked_until < ?)",
                ((now - timedelta(days=7)).isoformat(), now.isoformat()),
            )
