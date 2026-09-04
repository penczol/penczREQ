from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_multiseason_picker_supports_batch_selection_and_progress():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "data-season-checkbox" in script
    assert 'data-search-action="toggle-seasons"' in script
    assert 'data-search-action="add-seasons"' in script
    assert "async function addSelectedSeasons" in script
    assert 'tr("Dodawanie {current}/{total}…", { current: index + 1, total: selected.length })' in script
    assert "await loadAll();" in script
    assert 'data-search-action="add-season"' not in script


def test_multiseason_picker_has_separate_desktop_and_mobile_layouts():
    css = (ROOT / "request_app" / "static" / "v039.css").read_text(encoding="utf-8")
    base = (ROOT / "request_app" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "grid-template-columns: repeat(auto-fill, minmax(150px, 1fr))" in css
    assert "@media (max-width: 680px)" in css
    mobile = css[css.index("@media (max-width: 680px)"):]
    assert "grid-template-columns: 1fr" in mobile
    assert "min-height: 52px" in mobile
    assert "position: sticky" in mobile
    assert "v039.css" in base
