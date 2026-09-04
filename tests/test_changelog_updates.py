from request_app.changelog import changelog_for, update_notification_bodies
from request_app.database import Database, utc_now
from request_app.updates import notify_upcoming_request, notify_users_about_update


def test_public_changelog_hides_admin_details():
    public_036 = next(item for item in changelog_for(False) if item["version"] == "0.3.6")
    admin_036 = next(item for item in changelog_for(True) if item["version"] == "0.3.6")
    assert "Inne poprawki administracyjne." in public_036["changes"]
    assert not any("pasek akcji" in change for change in public_036["changes"])
    assert any("pasek akcji" in change for change in admin_036["changes"])


def test_update_notification_mentions_history_and_public_admin_summary():
    public, admin = update_notification_bodies("0.3.6")
    assert "Pełna historia zmian" in public
    assert "Pełna historia zmian" in admin
    assert "Inne poprawki administracyjne." in public
    assert "pasek akcji" not in public
    assert "pasek akcji" in admin


def test_update_notification_is_inserted_once_per_user(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active, must_change_password, created_at) VALUES ('admin', 'x', 'admin', 1, 0, ?)",
            (utc_now(),),
        )
        conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active, must_change_password, language, created_at) VALUES ('jan', 'x', 'user', 1, 0, 'pl', ?)",
            (utc_now(),),
        )
    assert notify_users_about_update(db, "9.9", "PUBLIC", "ADMIN", "PUBLIC EN", "ADMIN EN") == 2
    assert notify_users_about_update(db, "9.9", "PUBLIC", "ADMIN", "PUBLIC EN", "ADMIN EN") == 0
    assert notify_users_about_update(db, "10.0", "PUBLIC 10", "ADMIN 10", "PUBLIC 10 EN", "ADMIN 10 EN") == 2
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT u.role, n.body
            FROM notifications n JOIN users u ON u.id = n.user_id
            WHERE n.type = 'app_update' AND n.release_version = '9.9'
            """
        ).fetchall()
    assert {row["role"]: row["body"] for row in rows} == {
        "user": "PUBLIC",
        "admin": "ADMIN EN",
    }


def test_upcoming_notification_honors_preferences_and_deduplicates(tmp_path):
    db = Database(tmp_path / "test.db")
    db.initialize()
    with db.transaction() as conn:
        first = conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active, must_change_password, language, created_at) VALUES ('jan', 'x', 'user', 1, 0, 'pl', ?)",
            (utc_now(),),
        ).lastrowid
        second = conn.execute(
            "INSERT INTO users (username, password_hash, role, is_active, must_change_password, language, created_at) VALUES ('anna', 'x', 'user', 1, 0, 'pl', ?)",
            (utc_now(),),
        ).lastrowid
        conn.execute(
            "INSERT INTO notification_preferences (user_id, type, enabled) VALUES (?, 'request_changes', 0)",
            (second,),
        )
    assert notify_upcoming_request(db, int(first), "Film testowy", None) is True
    assert notify_upcoming_request(db, int(first), "Film testowy", None) is False
    assert notify_upcoming_request(db, int(second), "Film testowy", None) is False
    with db.connect() as conn:
        row = conn.execute(
            "SELECT title, body FROM notifications WHERE user_id = ?",
            (first,),
        ).fetchone()
    assert row["title"] == "Request przed premierą"
    assert "karcie „Przed premierą”" in row["body"]
