from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from request_app.database import Database, utc_now
from request_app.repository import Repository, RepositoryError
from request_app.security import create_session, get_session_user


ADMIN_PASSWORD = "AdminDeletionPassword99Z"  # pragma: allowlist secret
TARGET_PASSWORD = "TargetDeletionPassword99Z"  # pragma: allowlist secret
OTHER_PASSWORD = "OtherDeletionPassword99Z"  # pragma: allowlist secret


def _request(
    db: Database,
    tmdb_id: int,
    *,
    requested_by: int,
    state: str = "active",
    completed_by: int | None = None,
) -> int:
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO requests
            (tmdb_id, media_type, title_pl, title_en, title_original, state, status,
             requested_by, created_at, completed_at, completed_by)
            VALUES (?, 'movie', ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                tmdb_id,
                f"Tytuł {tmdb_id}",
                f"Title {tmdb_id}",
                f"Original {tmdb_id}",
                state,
                requested_by,
                utc_now(),
                utc_now() if state == "completed" else None,
                completed_by,
            ),
        )
        return int(cursor.lastrowid)


def _like(db: Database, request_id: int, user_id: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO likes (request_id, user_id, created_at) VALUES (?, ?, ?)",
            (request_id, user_id, utc_now()),
        )


def _repository(tmp_path: Path) -> tuple[Database, Repository, int, int, int]:
    db = Database(tmp_path / "app.db")
    db.initialize()
    repo = Repository(db)
    repo.enforce_roles("delete-admin", ADMIN_PASSWORD)
    target = repo.create_user("delete-target", TARGET_PASSWORD)
    other = repo.create_user("delete-other", OTHER_PASSWORD)
    admin = repo.user_by_username("delete-admin")
    return db, repo, int(admin["id"]), int(target["id"]), int(other["id"])


def test_delete_user_cleans_runtime_relations_and_applies_last_participant_contract(
    tmp_path: Path,
):
    db, repo, _, target_id, other_id = _repository(tmp_path)

    shared_request = _request(db, 101, requested_by=target_id)
    sole_request = _request(db, 102, requested_by=target_id)
    completed_shared = _request(
        db, 103, requested_by=target_id, state="completed", completed_by=target_id
    )
    completed_sole = _request(
        db, 104, requested_by=target_id, state="completed", completed_by=target_id
    )
    withdrawn_request = _request(db, 105, requested_by=target_id)

    for request_id in (shared_request, sole_request, completed_shared, completed_sole):
        _like(db, request_id, target_id)
    _like(db, shared_request, other_id)
    _like(db, completed_shared, other_id)
    _like(db, withdrawn_request, other_id)

    session = create_session(db, target_id, secret="S" * 48, days=1)
    with db.transaction() as conn:
        target_notification = conn.execute(
            """
            INSERT INTO notifications
            (user_id, type, title, body, request_id, created_at)
            VALUES (?, 'request_update', 'Target', 'Target body', ?, ?)
            """,
            (target_id, shared_request, utc_now()),
        ).lastrowid
        other_notification = conn.execute(
            """
            INSERT INTO notifications
            (user_id, type, title, body, request_id, created_at)
            VALUES (?, 'request_update', 'Other', 'Other body', ?, ?)
            """,
            (other_id, sole_request, utc_now()),
        ).lastrowid
        subscription = conn.execute(
            """
            INSERT INTO push_subscriptions
            (user_id, endpoint, p256dh, auth, start_notification_id, created_at, updated_at)
            VALUES (?, 'https://push.example/delete-target', ?, ?, 0, ?, ?)
            """,
            (target_id, "B" + "A" * 86, "A" * 22, utc_now(), utc_now()),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO push_deliveries
            (notification_id, subscription_id, attempts)
            VALUES (?, ?, 0)
            """,
            (target_notification, subscription),
        )
        conn.execute(
            """
            INSERT INTO like_notification_history
            (request_id, liker_user_id, created_at) VALUES (?, ?, ?)
            """,
            (shared_request, target_id, utc_now()),
        )
        conn.execute(
            """
            INSERT INTO request_withdrawals
            (request_id, user_id, withdrawn_at) VALUES (?, ?, ?)
            """,
            (withdrawn_request, target_id, utc_now()),
        )
        conn.execute(
            """
            INSERT INTO security_events
            (occurred_at, event_type, severity, actor_type, actor_id, username, details_json)
            VALUES (?, 'historical_target_event', 'warning', 'public_user', ?, ?, '{}')
            """,
            (utc_now(), target_id, "delete-target"),
        )

    result = repo.delete_user(target_id)

    assert result == {
        "target_user_id": target_id,
        "target_username": "delete-target",
        "sessions_revoked": 1,
        "push_subscriptions_removed": 1,
        "notifications_removed": 1,
        "participations_removed": 2,
        "requests_deleted": 2,
        "requested_by_anonymized": 3,
        "completed_by_anonymized": 1,
    }
    assert repo.user_by_id(target_id) is None
    assert repo.user_by_username("delete-target") is None
    assert repo.authenticate("delete-target", TARGET_PASSWORD, "127.0.0.1") is None
    assert get_session_user(
        db, session.raw_token, secret="S" * 48, idle_minutes=43_200
    ) is None
    assert all(int(row["id"]) != target_id for row in repo.list_users())

    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE token_hash IS NOT NULL AND user_id = ?",
            (target_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ?", (target_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM push_deliveries WHERE subscription_id = ?", (subscription,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ?", (target_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM notification_preferences WHERE user_id = ?", (target_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM likes WHERE user_id = ?", (target_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM like_notification_history WHERE liker_user_id = ?",
            (target_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM request_withdrawals WHERE user_id = ?", (target_id,)
        ).fetchone()[0] == 0

        assert conn.execute(
            "SELECT COUNT(*) FROM requests WHERE id IN (?, ?)",
            (sole_request, completed_sole),
        ).fetchone()[0] == 0
        for request_id in (shared_request, completed_shared, withdrawn_request):
            request_row = conn.execute(
                "SELECT requested_by, completed_by FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
            assert request_row is not None
            assert request_row["requested_by"] is None
        assert conn.execute(
            "SELECT completed_by FROM requests WHERE id = ?", (completed_shared,)
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT user_id FROM likes WHERE request_id = ?", (shared_request,)
        ).fetchall()[0][0] == other_id
        assert conn.execute(
            "SELECT request_id FROM notifications WHERE id = ?", (other_notification,)
        ).fetchone()[0] is None

        historical = conn.execute(
            "SELECT actor_id, username FROM security_events WHERE event_type = 'historical_target_event'"
        ).fetchone()
        assert dict(historical) == {"actor_id": target_id, "username": "delete-target"}
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_delete_user_refuses_protected_administrator(tmp_path: Path):
    db, repo, admin_id, _, _ = _repository(tmp_path)

    with pytest.raises(RepositoryError, match="Najpierw przekaż rolę administratora"):
        repo.delete_user(admin_id)

    assert repo.user_by_id(admin_id)["role"] == "admin"
    assert db.quick_check() == "ok"
    with db.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_delete_user_rolls_back_every_change_when_final_delete_fails(tmp_path: Path):
    db, repo, _, target_id, _ = _repository(tmp_path)
    sole_request = _request(db, 201, requested_by=target_id)
    _like(db, sole_request, target_id)
    create_session(db, target_id, secret="S" * 48, days=1)
    with db.connect() as conn:
        conn.execute(
            f"""
            CREATE TRIGGER force_delete_failure
            BEFORE DELETE ON users
            WHEN OLD.id = {target_id}
            BEGIN
                SELECT RAISE(ABORT, 'forced delete failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced delete failure"):
        repo.delete_user(target_id)

    assert repo.user_by_id(target_id) is not None
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM requests WHERE id = ?", (sole_request,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM likes WHERE request_id = ? AND user_id = ?",
            (sole_request, target_id),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (target_id,)
        ).fetchone()[0] == 1
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
