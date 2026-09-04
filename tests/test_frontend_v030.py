from pathlib import Path

from request_app.i18n import translate


ROOT = Path(__file__).resolve().parents[1]


def test_version_is_050():
    main = (ROOT / "request_app" / "main.py").read_text(encoding="utf-8")
    package = (ROOT / "request_app" / "__init__.py").read_text(encoding="utf-8")
    assert 'APP_VERSION = __version__' in main
    assert '__version__ = "0.5.2"' in package


def test_status_select_is_not_an_action_element():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "data-status-select" in script
    assert 'data-action="status-select"' not in script
    assert "button.disabled = true;" in script


def test_service_icons_and_copy_actions_are_present():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'serviceLink("tmdb"' in script
    assert 'serviceLink("imdb"' in script
    assert "/static/icons/${service}.svg" in script
    assert '<span class="copy-label"><span class="copy-prefix">${tr("Kopiuj")}</span> TMDB ID</span>' in script
    assert '<span class="copy-label"><span class="copy-prefix">${tr("Kopiuj")}</span> IMDb ID</span>' in script
    assert 'tr("Kopiuj ")' not in script
    assert translate("Kopiuj", "pl") == "Kopiuj"
    assert translate("Kopiuj", "en") == "Copy"
    assert "card-meta-row" in script
    assert "imdbFact" not in script


def test_assets_are_cache_busted_and_mobile_layout_is_preserved():
    base = (ROOT / "request_app" / "templates" / "base.html").read_text(encoding="utf-8")
    index = (ROOT / "request_app" / "templates" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "request_app" / "static" / "v030.css").read_text(encoding="utf-8")
    assert "v030.css" in base
    assert "?v={{ app_version }}" in base
    assert "?v={{ app_version }}" in index
    assert "@media (max-width: 680px)" in css
    assert "display: contents" in css


def test_desktop_card_uses_compact_four_row_grid():
    css = (ROOT / "request_app" / "static" / "v032.css").read_text(encoding="utf-8")
    assert "@media (min-width: 681px)" in css
    assert "grid-template-rows: auto auto auto minmax(29px, 1fr)" in css


def test_user_view_card_is_locked_to_poster_height_on_desktop():
    base = (ROOT / "request_app" / "templates" / "base.html").read_text(encoding="utf-8")
    css = (ROOT / "request_app" / "static" / "v033.css").read_text(encoding="utf-8")
    assert "admin-view" in base and "user-view" in base
    assert "v033.css" in base
    assert "body.user-view .request-card.request-card-v02" in css
    assert "height: 164px" in css
    assert "@media (min-width: 681px)" in css


def test_user_metadata_is_anchored_above_service_links():
    css = (ROOT / "request_app" / "static" / "v033.css").read_text(encoding="utf-8")
    assert "grid-template-rows: auto minmax(0, 1fr) auto auto" in css
    assert ".card-meta-row" in css
    assert "align-self: end" in css
