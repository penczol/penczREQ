from __future__ import annotations

from typing import Any

from .i18n import normalize_language, translator


def manifest_document(language: str) -> dict[str, Any]:
    """Return the one canonical, localized Public PWA manifest."""
    normalized_language = normalize_language(language)
    t = translator(normalized_language)
    return {
        "id": "/",
        "name": "penczREQ",
        "short_name": "penczREQ",
        "description": t("Prywatna lista requestów filmów i seriali."),
        "lang": normalized_language,
        "dir": "ltr",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "orientation": "any",
        "background_color": "#080d14",
        "theme_color": "#0b111a",
        "categories": ["entertainment"],
        "prefer_related_applications": False,
        "icons": [
            {
                "src": "/static/icons/pwa-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/static/icons/pwa-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/static/icons/pwa-maskable-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
