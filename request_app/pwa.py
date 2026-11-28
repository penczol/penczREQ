from __future__ import annotations

from typing import Any

from .i18n import normalize_language, translator


def manifest_document(language: str, deployment: str = "production") -> dict[str, Any]:
    """Return the one canonical, localized Public PWA manifest."""
    normalized_language = normalize_language(language)
    t = translator(normalized_language)
    uat = deployment == "uat"
    name = "penczREQ UAT" if uat else "penczREQ"
    icon = "/static/icons/uat-" if uat else "/static/icons/"
    return {
        "id": "/penczreq-uat" if uat else "/",
        "name": name,
        "short_name": name,
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
                "src": icon + "pwa-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": icon + "pwa-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": icon + "pwa-maskable-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
