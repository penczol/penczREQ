from request_app.tmdb import TMDBClient


def test_movie_release_date_buckets():
    metadata = TMDBClient._release_metadata_from_movie({
        "release_date": "2026-06-10",
        "release_dates": {"results": [
            {"iso_3166_1": "US", "release_dates": [
                {"type": 3, "release_date": "2026-06-10T00:00:00.000Z"},
                {"type": 4, "release_date": "2026-07-01T00:00:00.000Z"},
                {"type": 5, "release_date": "2026-08-01T00:00:00.000Z"},
            ]},
            {"iso_3166_1": "PL", "release_dates": [
                {"type": 3, "release_date": "2026-06-19T00:00:00.000Z"},
                {"type": 4, "release_date": "2026-07-15T00:00:00.000Z"},
            ]},
        ]},
    })
    assert metadata.world_theatrical_date == "2026-06-10"
    assert metadata.pl_theatrical_date == "2026-06-19"
    assert metadata.world_physical_date == "2026-08-01"
    assert metadata.pl_physical_date is None
