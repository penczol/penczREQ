import asyncio

import httpx
import pytest

from request_app.tmdb import TMDBClient


@pytest.mark.asyncio
async def test_search_preserves_every_requestable_title_and_tmdb_order(tmp_path):
    calls = []
    in_flight = peak = 0

    async def respond(request):
        nonlocal in_flight, peak
        calls.append(request.url.path)
        if request.url.path.endswith("/search/multi"):
            assert dict(request.url.params) == {
                "query": "Galaxy", "language": "en-US", "include_adult": "false", "page": "2",
            }
            rows = [
                {"id": i, "media_type": "movie" if i % 2 else "tv", "popularity": i}
                for i in range(1, 20)
            ]
            rows.insert(4, {"id": 84, "media_type": "person"})
            return httpx.Response(200, json={"results": rows, "total_pages": 3})
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.002 if request.url.path.endswith("/1") else 0)
        in_flight -= 1
        if request.url.path.endswith("/2"):
            return httpx.Response(404, json={})
        return httpx.Response(200, json={})

    client = TMDBClient("fixture", tmp_path, transport=httpx.MockTransport(respond))
    result = await client.search(" Galaxy ", "en", 2)
    assert result["page"] == 2 and result["total_pages"] == 3
    assert [item["tmdb_id"] for item in result["items"]] == list(range(1, 20))
    assert len(calls) == 20
    assert 1 < peak <= 5


@pytest.mark.asyncio
@pytest.mark.parametrize(("page", "reported", "accessible"), [(1, 0, 0), (3, 4, 4), (500, 510, 500)])
async def test_empty_movie_tv_page_keeps_upstream_navigation(tmp_path, page, reported, accessible):
    def respond(request):
        assert request.url.path.endswith("/search/multi")
        assert request.url.params["page"] == str(page)
        people = [{"id": 1, "media_type": "person"}] if reported else []
        return httpx.Response(200, json={"results": people, "total_pages": reported})

    client = TMDBClient("fixture", tmp_path, transport=httpx.MockTransport(respond))
    assert await client.search("popular", page=page) == {"items": [], "page": page, "total_pages": accessible}
