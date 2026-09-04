from __future__ import annotations

import unicodedata
from typing import Any, Mapping

from .i18n import normalize_language


NEUTRAL_TITLE_FALLBACK = "—"


def clean_title(value: Any) -> str:
    """Return a display-safe title without changing its meaning."""

    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value).strip())


def titles_match(left: Any, right: Any) -> bool:
    """Deduplicate only canonical Unicode/outer-whitespace equivalents."""

    normalized_left = clean_title(left)
    normalized_right = clean_title(right)
    return bool(normalized_left and normalized_left == normalized_right)


def localized_title(source: Mapping[str, Any], language: str) -> str:
    normalized = normalize_language(language)
    localized = clean_title(source.get(f"title_{normalized}"))
    original = clean_title(source.get("title_original"))
    return localized or original or NEUTRAL_TITLE_FALLBACK


def original_title_secondary(source: Mapping[str, Any], language: str) -> str:
    original = clean_title(source.get("title_original"))
    primary = localized_title(source, language)
    if not original or titles_match(original, primary):
        return ""
    return original
