from pathlib import Path

from request_app.database import Database, utc_now
from request_app.repository import Repository
from request_app.tmdb import MediaDetails, TMDBClient


ROOT = Path(__file__).resolve().parents[1]


def test_tmdb_series_lifecycle_distinguishes_ended_ongoing_and_unknown():
    assert TMDBClient._series_lifecycle(
        {
            "status": "Ended",
            "in_production": False,
            "first_air_date": "1993-09-10",
            "last_air_date": "2018-03-21",
        }
    ) == (1993, 2018, "ended")
    assert TMDBClient._series_lifecycle(
        {
            "status": "Returning Series",
            "in_production": True,
            "first_air_date": "2022-02-20",
            "last_air_date": "2026-06-28",
        }
    ) == (2022, None, "ongoing")
    assert TMDBClient._series_lifecycle(
        {"first_air_date": "2024-01-01"}
    ) == (2024, None, "unknown")


def test_repository_persists_series_lifecycle_per_independent_season(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    repository = Repository(database)
    with database.transaction() as connection:
        user_id = connection.execute(
            """
            INSERT INTO users
            (username, password_hash, role, is_active, must_change_password, created_at)
            VALUES ('admin', 'test', 'admin', 1, 0, ?)
            """,
            (utc_now(),),
        ).lastrowid
    media = MediaDetails(
        tmdb_id=124364,
        media_type="tv",
        season_number=1,
        imdb_id="tt9813792",
        title_pl="Stamtąd",
        title_en="From",
        title_original="FROM",
        release_year=2022,
        release_date="2022-02-20",
        original_language="en",
        poster_remote_path=None,
        series_start_year=2022,
        series_end_year=None,
        series_status="ongoing",
    )
    repository.create_request(media, None, int(user_id))
    item = repository.list_requests("active", int(user_id), True)[0]
    assert item["series_start_year"] == 2022
    assert item["series_end_year"] is None
    assert item["series_status"] == "ongoing"


def test_database_migration_defines_series_lifecycle_columns():
    database_source = (ROOT / "request_app" / "database.py").read_text(encoding="utf-8")
    assert '"series_start_year": "INTEGER"' in database_source
    assert '"series_end_year": "INTEGER"' in database_source
    assert '"series_status": "TEXT"' in database_source


def test_frontend_formats_movie_and_series_year_labels():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function releasePeriodLabel(item)" in script
    assert 'if (item.media_type === "movie") return item.release_year || tr("Rok nieznany");' in script
    assert 'if (item.series_status === "ongoing") return `${start}-${tr("trwa")}`;' in script
    assert 'if (item.series_status === "ended") return `${start}-${item.series_end_year || "????"}`;' in script
    assert 'return `${start}-????`;' in script
    assert "<span>${releasePeriodLabel(item)}</span>" in script


def test_completed_tv_requests_remain_lifecycle_refresh_targets():
    repository_source = (ROOT / "request_app" / "repository.py").read_text(encoding="utf-8")
    assert "state IN ('active', 'upcoming') OR media_type = 'tv'" in repository_source
