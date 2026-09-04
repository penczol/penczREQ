from __future__ import annotations

from datetime import UTC, datetime, timedelta

from request_app.database import Database, utc_now
from request_app.security import create_session, get_session_user


def test_session_migration_extends_only_still_valid_sessions_to_180_days(tmp_path):
    db = Database(tmp_path / "app.db")
    db.initialize()
    with db.transaction() as conn:
        user_id = int(conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active, must_change_password, created_at) "
            "VALUES ('session-user', 'x', 'user', 1, 0, ?)",
            (utc_now(),),
        ).lastrowid)
        conn.execute("DELETE FROM app_settings WHERE key = 'session_absolute_180_migrated'")
    session = create_session(db, user_id, secret="s" * 48, days=90)

    db.initialize()

    with db.connect() as conn:
        row = conn.execute("SELECT created_at, expires_at FROM sessions").fetchone()
    created = datetime.fromisoformat(row["created_at"])
    expires = datetime.fromisoformat(row["expires_at"])
    assert expires - created == timedelta(days=180)
    assert get_session_user(
        db, session.raw_token, secret="s" * 48, idle_minutes=43_200
    ) is not None


def test_existing_user_language_migrates_to_polish_and_fresh_user_defaults_to_english(tmp_path):
    db = Database(tmp_path / "app.db")
    db.initialize()
    with db.transaction() as conn:
        existing = int(conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active, must_change_password, created_at, language) "
            "VALUES ('existing-user', 'x', 'user', 1, 0, ?, 'pl')",
            (utc_now(),),
        ).lastrowid)
        fresh = int(conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active, must_change_password, created_at) "
            "VALUES ('fresh-user', 'x', 'user', 1, 0, ?)",
            (utc_now(),),
        ).lastrowid)
    with db.connect() as conn:
        assert conn.execute("SELECT language FROM users WHERE id = ?", (existing,)).fetchone()[0] == "pl"
        assert conn.execute("SELECT language FROM users WHERE id = ?", (fresh,)).fetchone()[0] == "en"


def test_public_session_defaults_are_declared_as_30_day_idle_and_180_day_absolute():
    config = open("request_app/config.py", encoding="utf-8").read()
    assert '_bounded_int("SESSION_DAYS", 180, 1, 180)' in config
    assert '_bounded_int("SESSION_IDLE_MINUTES", 43_200, 15, 43_200)' in config
