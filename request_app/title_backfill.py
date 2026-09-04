from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .repository import Repository
from .tmdb import TMDBClient, TMDBNotFoundError


@dataclass(frozen=True, slots=True)
class TitleBackfillReport:
    target_titles: int
    skipped_titles: int
    affected_requests: int
    mutations_performed: bool


async def backfill_english_titles(
    repository: Repository,
    tmdb: TMDBClient,
    *,
    apply: bool = False,
) -> TitleBackfillReport:
    """Plan or atomically apply an explicit English-title backfill.

    All TMDB reads finish before the first database write. Rendering never calls
    this function; operators invoke it explicitly after backing up a database.
    """

    targets = repository.title_backfill_targets()
    semaphore = asyncio.Semaphore(5)

    async def fetch(target: dict) -> tuple[dict, tuple[str, int, str] | None]:
        async with semaphore:
            media_type = str(target["media_type"])
            tmdb_id = int(target["tmdb_id"])
            try:
                titles = await tmdb.localized_titles(media_type, tmdb_id)
            except TMDBNotFoundError:
                return target, None
            return target, (media_type, tmdb_id, titles["title_en"])

    results = list(await asyncio.gather(*(fetch(target) for target in targets)))
    updates = [update for _target, update in results if update is not None]

    affected = sum(
        int(target["request_count"])
        for target, update in results
        if update is not None
    )
    if apply:
        affected = repository.apply_english_title_backfill(updates)
    return TitleBackfillReport(
        target_titles=len(targets),
        skipped_titles=len(targets) - len(updates),
        affected_requests=affected,
        mutations_performed=apply and bool(updates),
    )
