from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from request_app.database import Database, utc_now
from request_app.audit import SecurityAudit
from request_app.auth_protection import LoginProtection
from request_app.repository import Repository, RepositoryError
from request_app.security import hash_password
from request_app.tmdb import MediaDetails


TEST_PASSWORD = "BezpieczneHaslo2026"  # pragma: allowlist secret


@pytest.fixture
def repo(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    repository = Repository(database)
    password_hash = hash_password(TEST_PASSWORD)
    with database.transaction() as conn:
        for username, role in (("adam", "user"), ("anna", "admin"), ("bartek", "user")):
            conn.execute(
                "INSERT INTO users (username, password_hash, role, is_active, must_change_password, created_at) VALUES (?, ?, ?, 1, 0, ?)",
                (username, password_hash, role, utc_now()),
            )
    repository.enforce_roles()
    return repository


def user_id(repo: Repository, username: str) -> int:
    return int(repo.user_by_username(username)["id"])


def media(tmdb_id: int = 101, release_date: str = "2020-05-10") -> MediaDetails:
    return MediaDetails(
        tmdb_id=tmdb_id, media_type="movie", season_number=None, imdb_id=f"tt{tmdb_id:07d}",
        title_pl=f"Film {tmdb_id}", title_en=f"English movie {tmdb_id}", title_original=f"Movie {tmdb_id}", release_year=int(release_date[:4]),
        release_date=release_date, original_language="en", poster_remote_path=None,
        world_theatrical_date=release_date, pl_theatrical_date=None,
    )


def tv_season(tmdb_id: int, season_number: int) -> MediaDetails:
    return MediaDetails(
        tmdb_id=tmdb_id, media_type="tv", season_number=season_number, imdb_id=f"tt{tmdb_id:07d}",
        title_pl=f"Serial {tmdb_id}", title_en=f"English series {tmdb_id}", title_original=f"Series {tmdb_id}", release_year=2020,
        release_date="2020-05-10", original_language="en", poster_remote_path=None,
        world_theatrical_date=None, pl_theatrical_date=None,
    )


def test_roles_privacy_and_initial_author_like(repo: Repository):
    anna = user_id(repo, "anna")
    request_id, duplicate, state = repo.create_request(media(), None, anna)
    assert (duplicate, state) == (False, "active")
    user_item = repo.list_requests("active", anna, False)[0]
    assert user_item["id"] == request_id and user_item["author_like"] is True
    assert {"requester_username", "requested_by", "completed_by"}.isdisjoint(user_item)
    assert user_item["imdb_id"].startswith("tt")
    admin_item = repo.list_requests("active", user_id(repo, "adam"), True)[0]
    assert admin_item["requester_username"] == "anna" and admin_item["imdb_id"].startswith("tt")
    assert admin_item["requested_by"] == anna
    assert "completed_by" in admin_item


@pytest.mark.parametrize("state", ("active", "upcoming", "completed"))
def test_all_request_tabs_hide_internal_user_ids_from_ordinary_users(
    repo: Repository, state: str
):
    author = user_id(repo, "bartek")
    admin = user_id(repo, "anna")
    viewer = user_id(repo, "adam")
    release_date = "2099-01-01" if state == "upcoming" else "2020-05-10"
    request_id, _, initial_state = repo.create_request(
        media(2200 + ("active", "upcoming", "completed").index(state), release_date),
        None,
        author,
    )
    if state == "completed":
        assert initial_state == "active"
        repo.complete_request(request_id, admin)
    else:
        assert initial_state == state

    ordinary_item = repo.paginated_requests(state, viewer, False)["items"][0]
    assert {"requester_username", "requested_by", "completed_by"}.isdisjoint(
        ordinary_item
    )

    admin_item = repo.paginated_requests(state, admin, True)["items"][0]
    assert admin_item["requester_username"] == "bartek"
    assert admin_item["requested_by"] == author
    assert "completed_by" in admin_item


def test_notifications_do_not_expose_recipient_user_id(repo: Repository):
    author = user_id(repo, "bartek")
    admin = user_id(repo, "anna")
    request_id, _, _ = repo.create_request(media(2250), None, author)
    repo.complete_request(request_id, admin)

    notifications = repo.notifications(author)["items"]
    assert notifications
    assert all("user_id" not in item for item in notifications)


def test_author_like_is_permanent_and_voluntary_like_has_ten_second_window(repo: Repository):
    anna, bartek = user_id(repo, "anna"), user_id(repo, "bartek")
    request_id, _, _ = repo.create_request(media(), None, anna)
    with pytest.raises(RepositoryError, match="autora"):
        repo.toggle_like(request_id, anna)
    added = repo.toggle_like(request_id, bartek)
    assert added["liked"] is True and added["can_unlike"] is True
    assert repo.toggle_like(request_id, bartek)["liked"] is False
    repo.toggle_like(request_id, bartek)
    old = (datetime.now(UTC) - timedelta(seconds=11)).replace(microsecond=0).isoformat()
    with repo.db.transaction() as conn:
        conn.execute("UPDATE likes SET created_at = ? WHERE request_id = ? AND user_id = ?", (old, request_id, bartek))
    with pytest.raises(RepositoryError, match="10 sekund"):
        repo.toggle_like(request_id, bartek)


def test_author_withdrawal_deletes_request_when_no_one_else_is_interested(repo: Repository):
    anna = user_id(repo, "anna")
    request_id, _, _ = repo.create_request(media(301), None, anna)

    assert repo.withdraw_request(request_id, anna) == "deleted"
    assert repo.list_requests("active", anna, False) == []


def test_author_withdrawal_preserves_request_and_stops_future_notifications(repo: Repository):
    anna, bartek = user_id(repo, "anna"), user_id(repo, "bartek")
    request_id, _, _ = repo.create_request(media(302), None, anna)
    repo.toggle_like(request_id, bartek)
    with repo.db.transaction() as conn:
        conn.execute("DELETE FROM notifications")

    assert repo.withdraw_request(request_id, anna) == "participation_removed"
    author_view = repo.list_requests("active", anna, False)[0]
    assert author_view["liked_by_me"] is False
    assert author_view["can_withdraw"] is False
    assert {"requester_username", "requested_by", "completed_by"}.isdisjoint(author_view)
    with repo.db.connect() as conn:
        row = conn.execute(
            "SELECT requested_by FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        assert int(row["requested_by"]) == anna
        assert conn.execute(
            "SELECT 1 FROM request_withdrawals WHERE request_id = ? AND user_id = ?",
            (request_id, anna),
        ).fetchone()

    repo.set_status(request_id, "in_progress")
    assert repo.notifications(anna)["items"] == []
    assert any(item["type"] == "request_changes" for item in repo.notifications(bartek)["items"])
    with pytest.raises(RepositoryError, match="Wycofanego udziału"):
        repo.toggle_like(request_id, anna)


def test_user_cannot_withdraw_someone_elses_request(repo: Repository):
    anna, bartek = user_id(repo, "anna"), user_id(repo, "bartek")
    request_id, _, _ = repo.create_request(media(303), None, anna)
    with pytest.raises(RepositoryError, match="wyłącznie własny"):
        repo.withdraw_request(request_id, bartek)
    assert repo.list_requests("active", anna, False)[0]["like_count"] == 1


def test_completed_pagination_uses_grouped_items_and_clamps_last_page(repo: Repository):
    anna = user_id(repo, "anna")
    completed_ids = []
    for number in range(25):
        request_id, _, _ = repo.create_request(media(400 + number), None, anna)
        repo.complete_request(request_id, anna)
        completed_ids.append(request_id)
    for season in (1, 2):
        request_id, _, _ = repo.create_request(tv_season(900, season), None, anna)
        repo.complete_request(request_id, anna)
        completed_ids.append(request_id)

    first = repo.paginated_requests("completed", anna, False, page=1, page_size=25)
    second = repo.paginated_requests("completed", anna, False, page=2, page_size=25)
    assert first["pagination"] == {
        "page": 1, "page_size": 25, "total_items": 26,
        "total_all_items": 26, "total_pages": 2,
    }
    assert len(first["items"]) == 26  # 25 grouped positions, including both seasons.
    assert len(second["items"]) == 1

    repo.restore_request(int(second["items"][0]["id"]))
    clamped = repo.paginated_requests("completed", anna, False, page=2, page_size=25)
    assert clamped["pagination"]["page"] == 1
    assert clamped["pagination"]["total_pages"] == 1


def test_active_pagination_clamps_last_page_after_withdrawal_deletes_item(repo: Repository):
    anna = user_id(repo, "anna")
    for number in range(26):
        repo.create_request(media(950 + number), None, anna)

    last_page = repo.paginated_requests("active", anna, False, page=2, page_size=25)
    assert last_page["pagination"]["page"] == 2
    assert len(last_page["items"]) == 1

    repo.withdraw_request(int(last_page["items"][0]["id"]), anna)
    clamped = repo.paginated_requests("active", anna, False, page=2, page_size=25)
    assert clamped["pagination"]["page"] == 1
    assert clamped["pagination"]["total_items"] == 25
    assert clamped["pagination"]["total_pages"] == 1


def test_completed_pagination_rejects_unapproved_page_size(repo: Repository):
    with pytest.raises(RepositoryError, match="25, 50 albo 100"):
        repo.paginated_requests("completed", user_id(repo, "anna"), False, page_size=10)


@pytest.mark.parametrize(
    ("state", "release_date"),
    (("active", "2020-05-10"), ("upcoming", "2099-05-10")),
)
def test_pagination_is_available_for_active_and_upcoming(repo: Repository, state: str, release_date: str):
    anna = user_id(repo, "anna")
    for number in range(26):
        _, _, created_state = repo.create_request(media(1_000 + number, release_date), None, anna)
        assert created_state == state

    first = repo.paginated_requests(state, anna, False, page=1, page_size=25)
    second = repo.paginated_requests(state, anna, False, page=2, page_size=25)
    assert first["pagination"] == {
        "page": 1, "page_size": 25, "total_items": 26,
        "total_all_items": 26, "total_pages": 2,
    }
    assert len(first["items"]) == 25
    assert len(second["items"]) == 1


def test_active_pagination_applies_status_filter_before_counting_pages(repo: Repository):
    anna = user_id(repo, "anna")
    request_ids = [repo.create_request(media(1_100 + number), None, anna)[0] for number in range(26)]
    for request_id in request_ids[:13]:
        repo.set_status(request_id, "in_progress")

    result = repo.paginated_requests(
        "active", anna, False, page=1, page_size=25, status_filter="in_progress"
    )
    assert len(result["items"]) == 13
    assert result["pagination"]["total_items"] == 13
    assert result["pagination"]["total_all_items"] == 26
    assert result["pagination"]["total_pages"] == 1


@pytest.mark.parametrize(
    ("state", "release_date"),
    (
        ("active", "2020-05-10"),
        ("upcoming", "2099-05-10"),
        ("completed", "2020-05-10"),
    ),
)
async def test_public_api_always_returns_pagination_for_empty_and_multi_page_lists(
    monkeypatch, repo: Repository, state: str, release_date: str
):
    from request_app import main as public_app

    anna = user_id(repo, "anna")
    request = SimpleNamespace(cookies={})
    user = {"id": anna, "role": "user"}
    monkeypatch.setattr(public_app, "repo", repo)

    empty = await public_app.api_requests(
        request, state=state, page=1, page_size=25, sort="newest",
        status_filter="all", user=user,
    )
    assert empty == {
        "items": [],
        "pagination": {
            "page": 1,
            "page_size": 25,
            "total_items": 0,
            "total_all_items": 0,
            "total_pages": 1,
        },
    }

    for number in range(26):
        request_id, _, created_state = repo.create_request(
            media(1_200 + number, release_date), None, anna
        )
        if state == "completed":
            repo.complete_request(request_id, anna)
        else:
            assert created_state == state

    first = await public_app.api_requests(
        request, state=state, page=1, page_size=25, sort="newest",
        status_filter="all", user=user,
    )
    second = await public_app.api_requests(
        request, state=state, page=2, page_size=25, sort="newest",
        status_filter="all", user=user,
    )
    assert first["pagination"] == {
        "page": 1,
        "page_size": 25,
        "total_items": 26,
        "total_all_items": 26,
        "total_pages": 2,
    }
    assert len(first["items"]) == 25
    assert second["pagination"]["page"] == 2
    assert len(second["items"]) == 1


@pytest.mark.parametrize(
    ("state", "release_date"),
    (
        ("active", "2020-05-10"),
        ("upcoming", "2099-05-10"),
        ("completed", "2020-05-10"),
    ),
)
async def test_public_api_single_page_contract_for_zero_one_and_twenty_five_items(
    monkeypatch, repo: Repository, state: str, release_date: str
):
    from request_app import main as public_app

    anna = user_id(repo, "anna")
    request = SimpleNamespace(cookies={})
    user = {"id": anna, "role": "user"}
    monkeypatch.setattr(public_app, "repo", repo)
    created = 0

    for expected_items in (0, 1, 25):
        for number in range(created, expected_items):
            request_id, _, created_state = repo.create_request(
                media(1_300 + number, release_date), None, anna
            )
            if state == "completed":
                repo.complete_request(request_id, anna)
            else:
                assert created_state == state
        created = expected_items

        result = await public_app.api_requests(
            request, state=state, page=1, page_size=25, sort="newest",
            status_filter="all", user=user,
        )
        assert len(result["items"]) == expected_items
        assert result["pagination"] == {
            "page": 1,
            "page_size": 25,
            "total_items": expected_items,
            "total_all_items": expected_items,
            "total_pages": 1,
        }


def test_promotion_and_status_changes_use_unified_notification(repo: Repository):
    anna = user_id(repo, "anna")
    request_id, _, state = repo.create_request(media(202, "2032-03-14"), None, anna)
    assert state == "upcoming"
    assert repo.promote_due_requests(date(2032, 3, 14)) == 1
    assert any(item["type"] == "request_changes" for item in repo.notifications(anna)["items"])
    repo.set_status(request_id, "translation")
    item = repo.list_requests("active", anna, False)[0]
    assert item["status_label"] == "W oczekiwaniu na premierę Blu-ray/VOD"


def test_single_notification_read_and_delete(repo: Repository):
    anna = user_id(repo, "anna")
    repo.broadcast(user_id(repo, "adam"), "Komunikat", "Treść")
    notification = repo.notifications(anna, "unread")["items"][0]
    repo.mark_notification_read(anna, notification["id"])
    assert repo.notifications(anna, "read")["read"] == 1
    repo.delete_notification(anna, notification["id"])
    assert repo.notifications(anna)["items"] == []


def test_password_guessing_blocks_ip_without_locking_account(repo: Repository, tmp_path):
    protection = LoginProtection(repo.db, SecurityAudit(repo.db, tmp_path / "logs"))
    for _ in range(9):
        state = protection.record_failure("127.0.0.1", "anna")
        assert state.blocked is False
    state = protection.record_failure("127.0.0.1", "anna")
    assert state.blocked is True
    assert 1 <= state.retry_after <= 15 * 60
    assert repo.user_by_username("anna")["security_locked"] == 0
    assert repo.user_by_username("anna")["is_active"] == 1
    assert any(item["scope"] == "ip" and item["blocked_until"] for item in protection.list_states())
