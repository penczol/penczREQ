from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_compact_layout_is_loaded_after_v02():
    base = (ROOT / "request_app" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "v021.css" in base
    assert base.index("v02.css") < base.index("v021.css")


def test_mobile_layout_resets_stretched_poster_and_negative_margins():
    css = (ROOT / "request_app" / "static" / "v021.css").read_text(encoding="utf-8")
    assert "aspect-ratio: 2 / 3" in css
    assert "@media (max-width: 680px)" in css
    assert "height: 111px" in css
    assert "margin-left: 0" in css


def test_status_select_is_separate_from_button_actions():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "data-status-select" in script
    assert 'data-action="status-select"' not in script
