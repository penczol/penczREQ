from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx


TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p/w342"
TMDB_IMAGE_LARGE = "https://image.tmdb.org/t/p/w500"
logger = logging.getLogger(__name__)
NEUTRAL_TITLE_FALLBACK = "—"


class TMDBError(RuntimeError):
    pass


class TMDBNotFoundError(TMDBError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseMetadata:
    release_year: int | None
    release_date: str | None
    world_theatrical_date: str | None = None
    world_digital_date: str | None = None
    world_physical_date: str | None = None
    pl_theatrical_date: str | None = None
    pl_digital_date: str | None = None
    pl_physical_date: str | None = None
    series_start_year: int | None = None
    series_end_year: int | None = None
    series_status: str | None = None


@dataclass(frozen=True, slots=True)
class MediaDetails:
    tmdb_id: int
    media_type: str
    season_number: int | None
    imdb_id: str | None
    title_pl: str
    title_en: str
    title_original: str
    release_year: int | None
    release_date: str | None
    original_language: str | None
    poster_remote_path: str | None
    world_theatrical_date: str | None = None
    world_digital_date: str | None = None
    world_physical_date: str | None = None
    pl_theatrical_date: str | None = None
    pl_digital_date: str | None = None
    pl_physical_date: str | None = None
    series_start_year: int | None = None
    series_end_year: int | None = None
    series_status: str | None = None


class TMDBClient:
    def __init__(
        self,
        token: str | Callable[[], str],
        posters_dir: Path,
        poster_max_bytes: int = 8_388_608,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._token_source = token
        self.posters_dir = posters_dir
        self.poster_max_bytes = poster_max_bytes
        self._transport = transport

    @property
    def token(self) -> str:
        value = self._token_source() if callable(self._token_source) else self._token_source
        return str(value).strip()

    def _headers(self) -> dict[str, str]:
        token = self.token
        if not token:
            raise TMDBError("Brak tokenu TMDB. Uzupełnij go w lokalnym panelu penczREQ Control.")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    @staticmethod
    def _safe_diagnostic_text(value: str, token: str) -> str:
        result = value.replace(token, "[REDACTED]") if token else value
        result = re.sub(
            r"(?i)(authorization|api[_-]?key|access[_-]?token)(\s*[:=]\s*)([^\s,;]+)",
            r"\1\2[REDACTED]",
            result,
        )
        return " ".join(result.split())[:300] or "-"

    def _log_failure(
        self,
        endpoint: str,
        exc: BaseException,
        *,
        token: str = "",
        response: httpx.Response | None = None,
    ) -> None:
        chain: list[str] = []
        pending: list[BaseException] = [exc]
        seen: set[int] = set()
        while pending and len(chain) < 6:
            current = pending.pop(0)
            if id(current) in seen:
                continue
            seen.add(id(current))
            detail = self._safe_diagnostic_text(str(current), token)
            chain.append(f"{type(current).__name__}: {detail}")
            nested = getattr(current, "exceptions", ())
            if isinstance(nested, (list, tuple)):
                pending.extend(item for item in nested if isinstance(item, BaseException))
            cause = current.__cause__ or current.__context__
            if cause is not None:
                pending.append(cause)
        status = response.status_code if response is not None else "none"
        response_text = "none"
        if response is not None:
            try:
                response_text = response.text
            except httpx.ResponseNotRead:
                response_text = "<response body not read>"
        fragment = self._safe_diagnostic_text(response_text, token)
        logger.warning(
            "TMDB request failed endpoint=%s status=%s exception_chain=%s response=%s",
            endpoint,
            status,
            " <- ".join(chain),
            fragment,
        )

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        endpoint = f"{TMDB_API}{path}"
        response: httpx.Response | None = None
        try:
            headers = self._headers()
        except TMDBError as exc:
            self._log_failure(endpoint, exc)
            raise
        token = headers["Authorization"].removeprefix("Bearer ")
        try:
            async with httpx.AsyncClient(
                timeout=25,
                headers=headers,
                transport=self._transport,
            ) as client:
                response = await client.get(endpoint, params=params)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            self._log_failure(endpoint, exc, token=token, response=exc.response)
            if exc.response.status_code in {401, 403}:
                raise TMDBError("TMDB odrzuciło token API. Sprawdź API Read Access Token.") from exc
            if exc.response.status_code == 404:
                raise TMDBNotFoundError("TMDB nie zawiera już tej pozycji.") from exc
            raise TMDBError(f"TMDB zwróciło błąd HTTP {exc.response.status_code}.") from exc
        except (httpx.HTTPError, ValueError) as exc:
            self._log_failure(endpoint, exc, token=token, response=response)
            raise TMDBError("Nie udało się połączyć z TMDB.") from exc

    @staticmethod
    def _date(value: Any) -> str | None:
        if not isinstance(value, str) or len(value) < 10:
            return None
        candidate = value[:10]
        return candidate if candidate[4:5] == "-" and candidate[7:8] == "-" else None

    @staticmethod
    def _year(value: Any) -> int | None:
        if not isinstance(value, str) or len(value) < 4 or not value[:4].isdigit():
            return None
        return int(value[:4])

    @classmethod
    def _series_lifecycle(cls, details: dict[str, Any]) -> tuple[int | None, int | None, str]:
        status = str(details.get("status") or "").strip().casefold()
        ended_statuses = {"ended", "canceled", "cancelled", "zakończony", "zakończone", "anulowany", "anulowane"}
        ongoing_statuses = {
            "returning series", "in production", "planned", "pilot",
            "powracający serial", "w produkcji", "planowany",
        }
        start_year = cls._year(details.get("first_air_date"))
        if status in ended_statuses:
            return start_year, cls._year(details.get("last_air_date")), "ended"
        if status in ongoing_statuses or details.get("in_production") is True:
            return start_year, None, "ongoing"
        return start_year, None, "unknown"

    @classmethod
    def _release_metadata_from_movie(cls, details: dict[str, Any]) -> ReleaseMetadata:
        world: dict[int, list[str]] = {2: [], 3: [], 4: [], 5: []}
        poland: dict[int, list[str]] = {2: [], 3: [], 4: [], 5: []}
        release_payload = details.get("release_dates", details).get("results", [])
        for country in release_payload:
            country_code = country.get("iso_3166_1")
            for entry in country.get("release_dates", []):
                release_type = entry.get("type")
                release_date = cls._date(entry.get("release_date"))
                if release_type in world and release_date:
                    world[release_type].append(release_date)
                    if country_code == "PL":
                        poland[release_type].append(release_date)

        def earliest(source: dict[int, list[str]], types: tuple[int, ...]) -> str | None:
            values = [value for release_type in types for value in source[release_type]]
            return min(values) if values else None

        primary = cls._date(details.get("release_date"))
        year = int(primary[:4]) if primary else None
        return ReleaseMetadata(
            release_year=year,
            release_date=primary,
            world_theatrical_date=earliest(world, (2, 3)),
            world_digital_date=earliest(world, (4,)),
            world_physical_date=earliest(world, (5,)),
            pl_theatrical_date=earliest(poland, (2, 3)),
            pl_digital_date=earliest(poland, (4,)),
            pl_physical_date=earliest(poland, (5,)),
        )

    async def _enrich_search_item(self, item: dict[str, Any], semaphore: asyncio.Semaphore) -> dict[str, Any]:
        language = str(item.pop("_request_language", "pl"))
        tmdb_language = self._tmdb_language(language)
        media_type = item["media_type"]
        append = "credits" if media_type == "movie" else "aggregate_credits"
        try:
            async with semaphore:
                details = await self._get(
                    f"/{media_type}/{item['id']}",
                    language=tmdb_language,
                    append_to_response=append,
                )
        except TMDBError:
            details = {}

        date_value = item.get("release_date") if media_type == "movie" else item.get("first_air_date")
        year = int(date_value[:4]) if date_value and len(date_value) >= 4 else None
        title_localized = item.get("title") if media_type == "movie" else item.get("name")
        title_original = item.get("original_title") if media_type == "movie" else item.get("original_name")
        if media_type == "movie":
            countries = [country.get("iso_3166_1") for country in details.get("production_countries", []) if country.get("iso_3166_1")]
            credits = details.get("credits", {})
            directors = [person.get("name") for person in credits.get("crew", []) if person.get("job") == "Director" and person.get("name")]
            actors = [person.get("name") for person in credits.get("cast", []) if person.get("name")][:4]
        else:
            countries = details.get("origin_country") or item.get("origin_country") or []
            directors = [person.get("name") for person in details.get("created_by", []) if person.get("name")]
            aggregate = details.get("aggregate_credits", {})
            if not directors:
                directors = [
                    person.get("name")
                    for person in aggregate.get("crew", [])
                    if person.get("name") and any(job.get("job") == "Director" for job in person.get("jobs", []))
                ]
            actors = [person.get("name") for person in aggregate.get("cast", []) if person.get("name")][:4]

        return {
            "tmdb_id": item["id"],
            "media_type": media_type,
            "title_pl": (title_localized or title_original or NEUTRAL_TITLE_FALLBACK) if language == "pl" else None,
            "title_en": (title_localized or title_original or NEUTRAL_TITLE_FALLBACK) if language == "en" else None,
            "title_original": title_original or "",
            "year": year,
            "poster_url": f"{TMDB_IMAGE}{item['poster_path']}" if item.get("poster_path") else None,
            "popularity": item.get("popularity", 0),
            "countries": list(dict.fromkeys(countries))[:4],
            "directors": list(dict.fromkeys(directors))[:3],
            "actors": list(dict.fromkeys(actors))[:4],
        }

    @staticmethod
    def _tmdb_language(language: str) -> str:
        return "pl-PL" if str(language).strip().lower() == "pl" else "en-US"

    @staticmethod
    def _localized_title(details: dict[str, Any], media_type: str) -> str:
        key = "title" if media_type == "movie" else "name"
        original_key = "original_title" if media_type == "movie" else "original_name"
        return details.get(key) or details.get(original_key) or NEUTRAL_TITLE_FALLBACK

    async def search(self, query: str, language: str = "pl") -> list[dict[str, Any]]:
        query = query.strip()
        if len(query) < 2:
            return []
        language = "pl" if str(language).strip().lower() == "pl" else "en"
        payload = await self._get(
            "/search/multi", query=query, language=self._tmdb_language(language), include_adult="false", page=1
        )
        raw = [item for item in payload.get("results", []) if item.get("media_type") in {"movie", "tv"}]
        raw = sorted(raw, key=lambda value: value.get("popularity", 0), reverse=True)[:15]
        for item in raw:
            item["_request_language"] = language
        semaphore = asyncio.Semaphore(5)
        return await asyncio.gather(*(self._enrich_search_item(item, semaphore) for item in raw))

    async def title_details(self, media_type: str, tmdb_id: int, language: str = "pl") -> dict[str, Any]:
        if media_type not in {"movie", "tv"}:
            raise TMDBError("Nieobsługiwany typ pozycji.")
        append = "external_ids,images,release_dates" if media_type == "movie" else "external_ids,images"
        details = await self._get(
            f"/{media_type}/{tmdb_id}",
            language=self._tmdb_language(language),
            append_to_response=append,
            include_image_language="pl,null,en",
        )
        if media_type == "tv":
            season_label = "Sezon" if self._tmdb_language(language) == "pl-PL" else "Season"
            details["seasons"] = [
                {
                    "season_number": season.get("season_number"),
                    "name": season.get("name") or f"{season_label} {season.get('season_number')}",
                    "air_date": season.get("air_date"),
                    "episode_count": season.get("episode_count"),
                }
                for season in details.get("seasons", [])
                if isinstance(season.get("season_number"), int) and season["season_number"] > 0
            ]
        return details

    async def localized_titles(self, media_type: str, tmdb_id: int) -> dict[str, str]:
        if media_type not in {"movie", "tv"}:
            raise TMDBError("Nieobsługiwany typ pozycji.")
        path = f"/{media_type}/{tmdb_id}"
        pl_details, en_details = await asyncio.gather(
            self._get(path, language="pl-PL"),
            self._get(path, language="en-US"),
        )
        original_key = "original_title" if media_type == "movie" else "original_name"
        original = pl_details.get(original_key) or en_details.get(original_key) or ""
        return {
            "title_pl": self._localized_title(pl_details, media_type),
            "title_en": self._localized_title(en_details, media_type),
            "title_original": original,
        }

    @staticmethod
    def _choose_poster(details: dict[str, Any]) -> str | None:
        posters = details.get("images", {}).get("posters", [])
        original_language = details.get("original_language")

        def score(poster: dict[str, Any]) -> tuple[int, float, int]:
            language = poster.get("iso_639_1")
            language_score = 3 if language == original_language else 2 if language is None else 1
            return language_score, float(poster.get("vote_average") or 0), int(poster.get("vote_count") or 0)

        if posters:
            return max(posters, key=score).get("file_path")
        return details.get("poster_path")

    async def release_metadata(self, media_type: str, tmdb_id: int, season_number: int | None = None) -> ReleaseMetadata:
        if media_type == "movie":
            details = await self._get(
                f"/movie/{tmdb_id}", language="pl-PL", append_to_response="release_dates"
            )
            return self._release_metadata_from_movie(details)
        if media_type != "tv" or season_number is None:
            raise TMDBError("Serial musi mieć wybrany numer sezonu.")
        details, season = await asyncio.gather(
            self._get(f"/tv/{tmdb_id}", language="pl-PL"),
            self._get(f"/tv/{tmdb_id}/season/{season_number}", language="pl-PL"),
        )
        release_date = self._date(season.get("air_date"))
        if not release_date and season.get("episodes"):
            release_date = self._date(season["episodes"][0].get("air_date"))
        series_start_year, series_end_year, series_status = self._series_lifecycle(details)
        return ReleaseMetadata(
            release_year=int(release_date[:4]) if release_date else None,
            release_date=release_date,
            series_start_year=series_start_year,
            series_end_year=series_end_year,
            series_status=series_status,
        )

    async def media_for_request(self, media_type: str, tmdb_id: int, season_number: int | None) -> MediaDetails:
        if media_type not in {"movie", "tv"}:
            raise TMDBError("Nieobsługiwany typ pozycji.")
        details, english = await asyncio.gather(
            self.title_details(media_type, tmdb_id, "pl"),
            self._get(f"/{media_type}/{tmdb_id}", language="en-US"),
        )
        if media_type == "movie":
            metadata = self._release_metadata_from_movie(details)
            return MediaDetails(
                tmdb_id=tmdb_id,
                media_type="movie",
                season_number=None,
                imdb_id=details.get("external_ids", {}).get("imdb_id"),
                title_pl=self._localized_title(details, "movie"),
                title_en=self._localized_title(english, "movie"),
                title_original=details.get("original_title") or english.get("original_title") or "",
                release_year=metadata.release_year,
                release_date=metadata.release_date,
                original_language=details.get("original_language"),
                poster_remote_path=self._choose_poster(details),
                world_theatrical_date=metadata.world_theatrical_date,
                world_digital_date=metadata.world_digital_date,
                world_physical_date=metadata.world_physical_date,
                pl_theatrical_date=metadata.pl_theatrical_date,
                pl_digital_date=metadata.pl_digital_date,
                pl_physical_date=metadata.pl_physical_date,
            )
        if season_number is None or season_number < 1:
            raise TMDBError("Wybierz sezon serialu.")
        season = await self._get(f"/tv/{tmdb_id}/season/{season_number}", language="pl-PL")
        release_date = self._date(season.get("air_date"))
        if not release_date and season.get("episodes"):
            release_date = self._date(season["episodes"][0].get("air_date"))
        series_start_year, series_end_year, series_status = self._series_lifecycle(details)
        return MediaDetails(
            tmdb_id=tmdb_id,
            media_type="tv",
            season_number=season_number,
            imdb_id=details.get("external_ids", {}).get("imdb_id"),
            title_pl=self._localized_title(details, "tv"),
            title_en=self._localized_title(english, "tv"),
            title_original=details.get("original_name") or english.get("original_name") or "",
            release_year=int(release_date[:4]) if release_date else None,
            release_date=release_date,
            original_language=details.get("original_language"),
            poster_remote_path=self._choose_poster(details),
            series_start_year=series_start_year,
            series_end_year=series_end_year,
            series_status=series_status,
        )

    async def cache_poster(self, media: MediaDetails) -> str | None:
        if not media.poster_remote_path:
            return None
        remote_path = media.poster_remote_path.strip()
        if not remote_path.startswith("/") or ".." in remote_path or "\\" in remote_path:
            raise TMDBError("TMDB zwróciło nieprawidłową ścieżkę okładki.")
        suffix = Path(remote_path).suffix.lower() or ".jpg"
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise TMDBError("TMDB zwróciło nieobsługiwany format okładki.")
        season = f"-s{media.season_number}" if media.season_number is not None else ""
        filename = f"{media.media_type}-{media.tmdb_id}{season}{suffix}"
        target = self.posters_dir / filename
        if target.exists() and target.stat().st_size > 0:
            return filename
        endpoint = f"{TMDB_IMAGE_LARGE}{remote_path}"
        response: httpx.Response | None = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                async with client.stream("GET", endpoint) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                        raise TMDBError("TMDB zwróciło nieprawidłowy typ pliku okładki.")
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > self.poster_max_bytes:
                        raise TMDBError("Okładka z TMDB przekracza dozwolony rozmiar.")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > self.poster_max_bytes:
                            raise TMDBError("Okładka z TMDB przekracza dozwolony rozmiar.")
                    if not payload:
                        raise TMDBError("TMDB zwróciło pusty plik okładki.")
                    target.write_bytes(payload)
            return filename
        except httpx.HTTPError as exc:
            self._log_failure(endpoint, exc, response=response)
            raise TMDBError("Nie udało się zapisać okładki z TMDB.") from exc
