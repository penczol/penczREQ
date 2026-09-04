from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .notification_i18n import migrate_legacy_notifications


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    must_change_password INTEGER NOT NULL DEFAULT 1 CHECK (must_change_password IN (0, 1)),
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    last_login_ip TEXT,
    password_changed_at TEXT,
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    temporary_lock_until TEXT,
    lockout_cycles INTEGER NOT NULL DEFAULT 0,
    security_locked INTEGER NOT NULL DEFAULT 0 CHECK (security_locked IN (0, 1)),
    security_locked_at TEXT,
    language TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'pl'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id INTEGER NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
    season_number INTEGER,
    imdb_id TEXT,
    title_pl TEXT NOT NULL,
    title_en TEXT,
    title_original TEXT NOT NULL,
    release_year INTEGER,
    series_start_year INTEGER,
    series_end_year INTEGER,
    series_status TEXT,
    release_date TEXT,
    world_theatrical_date TEXT,
    world_digital_date TEXT,
    world_physical_date TEXT,
    pl_theatrical_date TEXT,
    pl_digital_date TEXT,
    pl_physical_date TEXT,
    release_data_refreshed_at TEXT,
    original_language TEXT,
    poster_path TEXT,
    state TEXT NOT NULL CHECK (state IN ('upcoming', 'active', 'completed')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'translation', 'in_progress', 'missing')),
    requested_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    completed_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_request_media
ON requests(media_type, tmdb_id, IFNULL(season_number, -1));
CREATE INDEX IF NOT EXISTS idx_requests_state ON requests(state);
CREATE INDEX IF NOT EXISTS idx_requests_release_date ON requests(release_date);

CREATE TABLE IF NOT EXISTS likes (
    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (request_id, user_id)
);

CREATE TABLE IF NOT EXISTS like_notification_history (
    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    liker_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (request_id, liker_user_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL,
    release_version TEXT,
    event_key TEXT,
    event_payload_json TEXT,
    created_at TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_user_created
ON notifications(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS request_withdrawals (
    request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    withdrawn_at TEXT NOT NULL,
    PRIMARY KEY (request_id, user_id)
);

CREATE TABLE IF NOT EXISTS notification_preferences (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    PRIMARY KEY (user_id, type)
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    start_notification_id INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user
ON push_subscriptions(user_id);

CREATE TABLE IF NOT EXISTS push_deliveries (
    notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    subscription_id INTEGER NOT NULL REFERENCES push_subscriptions(id) ON DELETE CASCADE,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    delivered_at TEXT,
    last_error TEXT,
    PRIMARY KEY (notification_id, subscription_id)
);
CREATE INDEX IF NOT EXISTS idx_push_deliveries_pending
ON push_deliveries(delivered_at, attempts);

CREATE TABLE IF NOT EXISTS login_attempts (
    key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    first_failure_at TEXT NOT NULL,
    blocked_until TEXT
);

CREATE TABLE IF NOT EXISTS auth_throttles (
    scope TEXT NOT NULL CHECK (scope IN ('ip', 'account')),
    key TEXT NOT NULL,
    display_key TEXT NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    window_started_at TEXT NOT NULL,
    last_failure_at TEXT NOT NULL,
    blocked_until TEXT,
    block_level INTEGER NOT NULL DEFAULT 0,
    escalation_until TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
CREATE INDEX IF NOT EXISTS idx_auth_throttles_blocked
ON auth_throttles(blocked_until);
CREATE INDEX IF NOT EXISTS idx_auth_throttles_updated
ON auth_throttles(updated_at);

CREATE TABLE IF NOT EXISTS security_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    actor_type TEXT NOT NULL DEFAULT 'system',
    actor_id INTEGER,
    username TEXT,
    ip_address TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_security_events_occurred
ON security_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_security_events_type
ON security_events(event_type, occurred_at DESC);

CREATE TABLE IF NOT EXISTS app_secrets (
    key TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    previous_value TEXT,
    new_value TEXT,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settings_history_changed
ON settings_history(changed_at DESC);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


MIGRATION_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "last_login_ip": "TEXT",
        "failed_login_count": "INTEGER NOT NULL DEFAULT 0",
        "temporary_lock_until": "TEXT",
        "lockout_cycles": "INTEGER NOT NULL DEFAULT 0",
        "security_locked": "INTEGER NOT NULL DEFAULT 0",
        "security_locked_at": "TEXT",
        "language": "TEXT NOT NULL DEFAULT 'pl' CHECK (language IN ('en', 'pl'))",
    },
    "requests": {
        "title_en": "TEXT",
        "world_theatrical_date": "TEXT",
        "world_digital_date": "TEXT",
        "world_physical_date": "TEXT",
        "pl_theatrical_date": "TEXT",
        "pl_digital_date": "TEXT",
        "pl_physical_date": "TEXT",
        "release_data_refreshed_at": "TEXT",
        "series_start_year": "INTEGER",
        "series_end_year": "INTEGER",
        "series_status": "TEXT",
    },
    "notifications": {
        "release_version": "TEXT",
        "event_key": "TEXT",
        "event_payload_json": "TEXT",
    },
}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for table, columns in MIGRATION_COLUMNS.items():
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for column, definition in columns.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            migrate_legacy_notifications(conn)
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_release
                ON notifications(user_id, release_version)
                WHERE type = 'app_update' AND release_version IS NOT NULL
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value, updated_at) VALUES ('known_proxies', '', ?)",
                (utc_now(),),
            )
            defaults = {
                "public_base_url": "",
                "security_log_retention_days": "30",
                "backup_retention_days": "30",
                "last_backup_at": "",
                "security_schema_version": "2",
            }
            conn.executemany(
                "INSERT OR IGNORE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                [(key, value, utc_now()) for key, value in defaults.items()],
            )
            marker = conn.execute(
                "SELECT 1 FROM app_settings WHERE key = 'session_absolute_180_migrated'"
            ).fetchone()
            if not marker:
                now = datetime.now(UTC).replace(microsecond=0)
                for session in conn.execute(
                    "SELECT id, created_at, expires_at FROM sessions"
                ).fetchall():
                    created = datetime.fromisoformat(session["created_at"])
                    expires = datetime.fromisoformat(session["expires_at"])
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=UTC)
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=UTC)
                    if expires > now:
                        absolute_expiry = created + timedelta(days=180)
                        conn.execute(
                            "UPDATE sessions SET expires_at = ? WHERE id = ?",
                            (absolute_expiry.replace(microsecond=0).isoformat(), session["id"]),
                        )
                conn.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES ('session_absolute_180_migrated', '1', ?)",
                    (utc_now(),),
                )

    def quick_check(self) -> str:
        with self.connect() as conn:
            return str(conn.execute("PRAGMA quick_check").fetchone()[0])

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
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
