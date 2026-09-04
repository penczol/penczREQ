from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from request_app.database import Database, SCHEMA, utc_now
from request_app.notification_i18n import encode_event_payload
from request_app.repository import Repository
from request_app.title_backfill import backfill_english_titles
from request_app.titles import localized_title, original_title_secondary, titles_match
from request_app.tmdb import MediaDetails, TMDBClient, TMDBError, TMDBNotFoundError


ROOT = Path(__file__).resolve().parents[1]


def test_fresh_install_schema_contains_nullable_english_title(tmp_path):
    db = Database(tmp_path / "fresh.db")
    db.initialize()

    with db.connect() as conn:
        columns = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(requests)").fetchall()
        }
        assert "title_en" in columns
        assert columns["title_en"]["type"] == "TEXT"
        assert columns["title_en"]["notnull"] == 0


def test_cli_refuses_production_network_backfill_without_separate_gate(monkeypatch):
    from request_app import cli

    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(app_env="production"))
    monkeypatch.setattr(sys, "argv", ["request_app.cli", "backfill-english-titles"])
    assert cli.main() == 2


@pytest.mark.parametrize(
    ("media_type", "title_pl", "title_en", "title_original"),
    (
        ("movie", "Spirited Away: W krainie bogów", "Spirited Away", "千と千尋の神隠し"),
        ("tv", "Dom z papieru", "Money Heist", "La Casa de Papel"),
    ),
)
def test_movie_and_tv_titles_follow_locale_then_original_contract(
    media_type, title_pl, title_en, title_original
):
    item = {
        "media_type": media_type,
        "title_pl": title_pl,
        "title_en": title_en,
        "title_original": title_original,
    }
    assert localized_title(item, "pl") == title_pl
    assert localized_title(item, "en") == title_en
    assert original_title_secondary(item, "pl") == title_original
    assert original_title_secondary(item, "en") == title_original


def test_title_fallback_never_uses_other_locale_and_secondary_is_original_only():
    item = {
        "title_pl": "Wyłącznie polski",
        "title_en": None,
        "title_original": "Original title",
    }
    assert localized_title(item, "en") == "Original title"
    assert original_title_secondary(item, "en") == ""

    no_original = {"title_pl": "Wyłącznie polski", "title_en": None, "title_original": None}
    assert localized_title(no_original, "en") == "—"
    assert original_title_secondary(no_original, "en") == ""


def test_title_deduplication_is_unicode_safe_but_not_aggressive():
    assert titles_match("  Café  ", "Cafe\u0301") is True
    assert titles_match("FROM", "From") is False
    assert titles_match("Wall·E", "WALL-E") is False

    same_original = {
        "title_pl": "Inny",
        "title_en": " Cafe\u0301 ",
        "title_original": "Café",
    }
    assert original_title_secondary(same_original, "en") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media_type", "localized_key", "original_key", "pl", "en", "original"),
    (
        ("movie", "title", "original_title", "W krainie bogów", "Spirited Away", "千と千尋の神隠し"),
        ("tv", "name", "original_name", "Dom z papieru", "Money Heist", "La Casa de Papel"),
    ),
)
async def test_tmdb_fetches_pl_and_en_titles_with_correct_movie_tv_fields(
    tmp_path, media_type, localized_key, original_key, pl, en, original
):
    observed_languages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        language = request.url.params["language"]
        observed_languages.append(language)
        localized = pl if language == "pl-PL" else en
        return httpx.Response(
            200,
            request=request,
            json={localized_key: localized, original_key: original},
        )

    client = TMDBClient(
        "test-read-token",  # pragma: allowlist secret
        tmp_path,
        transport=httpx.MockTransport(handler),
    )
    titles = await client.localized_titles(media_type, 123)
    assert titles == {
        "title_pl": pl,
        "title_en": en,
        "title_original": original,
    }
    assert observed_languages == ["pl-PL", "en-US"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "localized", "localized_field", "missing_field"),
    (
        ("pl", "Ruchomy zamek Hauru", "title_pl", "title_en"),
        ("en", "Howl's Moving Castle", "title_en", "title_pl"),
    ),
)
async def test_tmdb_search_uses_active_ui_language(
    tmp_path, language, localized, localized_field, missing_field
):
    observed: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tmdb_language = request.url.params["language"]
        observed.append((request.url.path, tmdb_language))
        if request.url.path.endswith("/search/multi"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "results": [
                        {
                            "id": 4935,
                            "media_type": "movie",
                            "title": localized,
                            "original_title": "ハウルの動く城",
                            "release_date": "2004-09-05",
                            "popularity": 100,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={"production_countries": [], "credits": {"crew": [], "cast": []}},
        )

    client = TMDBClient(
        "test-read-token",  # pragma: allowlist secret
        tmp_path,
        transport=httpx.MockTransport(handler),
    )
    item = (await client.search("zamek", language))[0]
    expected_language = "pl-PL" if language == "pl" else "en-US"
    assert observed == [
        ("/3/search/multi", expected_language),
        ("/3/movie/4935", expected_language),
    ]
    assert item[localized_field] == localized
    assert item[missing_field] is None
    assert item["title_original"] == "ハウルの動く城"


@pytest.mark.asyncio
async def test_new_movie_request_fetches_and_returns_both_localizations(tmp_path):
    observed_languages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        language = request.url.params["language"]
        observed_languages.append(language)
        title = "W krainie bogów" if language == "pl-PL" else "Spirited Away"
        return httpx.Response(
            200,
            request=request,
            json={
                "title": title,
                "original_title": "千と千尋の神隠し",
                "release_date": "2001-07-20",
                "original_language": "ja",
                "external_ids": {"imdb_id": "tt0245429"},
                "images": {"posters": []},
                "release_dates": {"results": []},
            },
        )

    client = TMDBClient(
        "test-read-token",  # pragma: allowlist secret
        tmp_path,
        transport=httpx.MockTransport(handler),
    )
    media = await client.media_for_request("movie", 129, None)
    assert media.title_pl == "W krainie bogów"
    assert media.title_en == "Spirited Away"
    assert media.title_original == "千と千尋の神隠し"
    assert observed_languages == ["pl-PL", "en-US"]


@pytest.mark.asyncio
async def test_tv_detail_season_fallback_follows_active_language(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "name": "Series",
                "original_name": "Series",
                "seasons": [
                    {"season_number": 1, "name": "", "air_date": None, "episode_count": 8}
                ],
            },
        )

    client = TMDBClient(
        "test-read-token",  # pragma: allowlist secret
        tmp_path,
        transport=httpx.MockTransport(handler),
    )
    assert (await client.title_details("tv", 42, "en"))["seasons"][0]["name"] == "Season 1"
    assert (await client.title_details("tv", 42, "pl"))["seasons"][0]["name"] == "Sezon 1"


def _legacy_database(path: Path) -> Database:
    legacy_schema = SCHEMA.replace("    title_en TEXT,\n", "")
    with sqlite3.connect(path) as conn:
        conn.executescript(legacy_schema)
        conn.execute(
            """
            INSERT INTO users
            (username, password_hash, role, is_active, must_change_password, created_at)
            VALUES ('legacy', 'x', 'user', 1, 0, ?)
            """,
            (utc_now(),),
        )
        for season in (1, 2):
            conn.execute(
                """
                INSERT INTO requests
                (tmdb_id, media_type, season_number, title_pl, title_original,
                 release_date, state, status, requested_by, created_at)
                VALUES (321, 'tv', ?, 'Dom z papieru', 'La Casa de Papel',
                        '2020-01-01', 'active', 'pending', 1, ?)
                """,
                (season, utc_now()),
            )
    return Database(path)


@pytest.mark.asyncio
async def test_existing_records_migrate_and_explicit_backfill_is_dry_run_then_atomic(tmp_path):
    db = _legacy_database(tmp_path / "legacy.db")
    db.initialize()
    repository = Repository(db)

    with db.transaction() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}
        assert "title_en" in columns
        assert conn.execute("SELECT COUNT(*) FROM requests WHERE title_en IS NULL").fetchone()[0] == 2
        conn.execute(
            "UPDATE users SET language = 'en' WHERE id = 1"
        )
        conn.execute(
            """
            INSERT INTO notifications
            (user_id, type, title, body, request_id, event_key, event_payload_json, created_at)
            VALUES (1, 'request_changes', 'legacy', 'legacy', 1,
                    'request.completed', ?, ?)
            """,
            (
                encode_event_payload(
                    {
                        "title_pl": "Dom z papieru",
                        "title_original": "La Casa de Papel",
                        "season_number": 1,
                    }
                ),
                utc_now(),
            ),
        )

    class ClientStub:
        calls: list[tuple[str, int]] = []

        async def localized_titles(self, media_type: str, tmdb_id: int):
            self.calls.append((media_type, tmdb_id))
            return {
                "title_pl": "Dom z papieru",
                "title_en": "Money Heist",
                "title_original": "La Casa de Papel",
            }

    client = ClientStub()
    dry_run = await backfill_english_titles(repository, client, apply=False)
    assert dry_run.target_titles == 1
    assert dry_run.skipped_titles == 0
    assert dry_run.affected_requests == 2
    assert dry_run.mutations_performed is False
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM requests WHERE title_en IS NULL").fetchone()[0] == 2

    applied = await backfill_english_titles(repository, client, apply=True)
    assert applied.target_titles == 1
    assert applied.skipped_titles == 0
    assert applied.affected_requests == 2
    assert applied.mutations_performed is True
    with db.connect() as conn:
        rows = conn.execute("SELECT id, title_pl, title_en, title_original FROM requests ORDER BY id").fetchall()
        assert [row["title_en"] for row in rows] == ["Money Heist", "Money Heist"]
        assert [row["title_pl"] for row in rows] == ["Dom z papieru", "Dom z papieru"]
        assert [row["title_original"] for row in rows] == ["La Casa de Papel", "La Casa de Papel"]
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    notification = repository.notifications(1)["items"][0]
    assert "Money Heist — season 1" in notification["body"]
    assert client.calls == [("tv", 321), ("tv", 321)]


@pytest.mark.asyncio
async def test_backfill_skips_removed_tmdb_titles_but_does_not_mask_other_errors(tmp_path):
    db = Database(tmp_path / "missing.db")
    db.initialize()
    repository = Repository(db)
    with db.transaction() as conn:
        user_id = int(conn.execute(
            """
            INSERT INTO users
            (username, password_hash, role, is_active, must_change_password, created_at)
            VALUES ('fixture', 'x', 'user', 1, 0, ?)
            """,
            (utc_now(),),
        ).lastrowid)
    media = MediaDetails(
        tmdb_id=700_004,
        media_type="movie",
        season_number=None,
        imdb_id=None,
        title_pl="Fixture",
        title_en="",
        title_original="Fixture original",
        release_year=2020,
        release_date="2020-01-01",
        original_language="en",
        poster_remote_path=None,
    )
    repository.create_request(media, None, user_id)

    class MissingClient:
        async def localized_titles(self, _media_type: str, _tmdb_id: int):
            raise TMDBNotFoundError("missing")

    report = await backfill_english_titles(repository, MissingClient(), apply=True)
    assert report.target_titles == 1
    assert report.skipped_titles == 1
    assert report.affected_requests == 0
    assert report.mutations_performed is False
    with db.connect() as conn:
        assert conn.execute("SELECT title_en FROM requests").fetchone()[0] == ""


@pytest.mark.asyncio
async def test_backfill_network_failure_aborts_before_any_title_write(tmp_path):
    db = Database(tmp_path / "network-failure.db")
    db.initialize()
    repository = Repository(db)
    with db.transaction() as conn:
        user_id = int(conn.execute(
            """
            INSERT INTO users
            (username, password_hash, role, is_active, must_change_password, created_at)
            VALUES ('fixture', 'x', 'user', 1, 0, ?)
            """,
            (utc_now(),),
        ).lastrowid)
    for tmdb_id in (800_001, 800_002):
        repository.create_request(
            MediaDetails(
                tmdb_id=tmdb_id,
                media_type="movie",
                season_number=None,
                imdb_id=None,
                title_pl=f"PL {tmdb_id}",
                title_en="",
                title_original=f"Original {tmdb_id}",
                release_year=2020,
                release_date="2020-01-01",
                original_language="en",
                poster_remote_path=None,
            ),
            None,
            user_id,
        )

    class FailingClient:
        async def localized_titles(self, _media_type: str, tmdb_id: int):
            if tmdb_id == 800_002:
                raise TMDBError("network failure")
            return {
                "title_pl": "PL",
                "title_en": "Fetched English",
                "title_original": "Original",
            }

    with pytest.raises(TMDBError, match="network failure"):
        await backfill_english_titles(repository, FailingClient(), apply=True)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM requests WHERE length(trim(coalesce(title_en, ''))) = 0"
        ).fetchone()[0] == 2


def test_new_request_persists_all_three_titles_and_language_switch_keeps_identity(tmp_path):
    db = Database(tmp_path / "current.db")
    db.initialize()
    repository = Repository(db)
    with db.transaction() as conn:
        user_id = int(conn.execute(
            """
            INSERT INTO users
            (username, password_hash, role, is_active, must_change_password, language, created_at)
            VALUES ('viewer', 'x', 'user', 1, 0, 'pl', ?)
            """,
            (utc_now(),),
        ).lastrowid)
    media = MediaDetails(
        tmdb_id=777,
        media_type="movie",
        season_number=None,
        imdb_id=None,
        title_pl="W krainie bogów",
        title_en="Spirited Away",
        title_original="千と千尋の神隠し",
        release_year=2001,
        release_date="2001-07-20",
        original_language="ja",
        poster_remote_path=None,
    )
    request_id, _, _ = repository.create_request(media, None, user_id)
    item = repository.list_requests("active", user_id, False)[0]
    assert item["id"] == request_id
    assert localized_title(item, "pl") == "W krainie bogów"
    assert localized_title(item, "en") == "Spirited Away"
    repository.set_user_language(user_id, "en")
    same_item = repository.list_requests("active", user_id, False)[0]
    assert same_item["id"] == request_id
    assert localized_title(same_item, "en") == "Spirited Away"


def test_adding_existing_tv_season_refreshes_titles_for_the_whole_group(tmp_path):
    db = Database(tmp_path / "tv-group.db")
    db.initialize()
    repository = Repository(db)
    with db.transaction() as conn:
        user_id = int(conn.execute(
            """
            INSERT INTO users
            (username, password_hash, role, is_active, must_change_password, created_at)
            VALUES ('viewer', 'x', 'user', 1, 0, ?)
            """,
            (utc_now(),),
        ).lastrowid)

    def season(number: int, english: str) -> MediaDetails:
        return MediaDetails(
            tmdb_id=1396,
            media_type="tv",
            season_number=number,
            imdb_id="tt0903747",
            title_pl="Breaking Bad",
            title_en=english,
            title_original="Breaking Bad",
            release_year=2008,
            release_date="2008-01-20",
            original_language="en",
            poster_remote_path=None,
        )

    repository.create_request(season(1, ""), None, user_id)
    repository.create_request(season(2, ""), None, user_id)
    request_id, duplicate, _state = repository.create_request(
        season(2, "Breaking Bad"), None, user_id
    )
    assert duplicate is True
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, title_en FROM requests WHERE media_type = 'tv' AND tmdb_id = 1396"
        ).fetchall()
    assert request_id in {int(row["id"]) for row in rows}
    assert [row["title_en"] for row in rows] == ["Breaking Bad", "Breaking Bad"]


def test_desktop_and_mobile_share_one_title_renderer_without_tmdb_render_fetches():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert script.count("function cardMarkup(item)") == 1
    assert "function cardMarkupMobile" not in script
    assert "function cardMarkupDesktop" not in script
    card_renderer = script.split("function cardMarkup(item)", 1)[1].split(
        "function sortedAndFiltered", 1
    )[0]
    assert "primaryTitle(item)" in card_renderer
    assert "/api/tmdb/" not in card_renderer
