from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_season_strip_wraps_the_unchanged_main_card():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    card_function = script[
        script.index("function cardMarkup"):
        script.index("function sortedAndFiltered")
    ]
    group_function = script[
        script.index("function groupCardMarkup"):
        script.index("function render")
    ]
    assert 'function cardMarkup(item)' in card_function
    assert "seasonTabs" not in card_function
    assert 'class="request-season-group"' in group_function
    assert "${seasonTabsMarkup(group, activeItem)}${cardMarkup(activeItem)}" in group_function


def test_attached_strip_spans_the_whole_card_without_resizing_its_body():
    css = (ROOT / "request_app" / "static" / "v0311.css").read_text(encoding="utf-8")
    assert ".request-season-group > .season-tabs" in css
    assert "width: 100%" in css
    assert "max-width: 100%" in css
    assert "border-bottom: 0" in css
    assert ".request-season-group > .request-card" in css
    assert "height:" not in css


def test_mobile_strip_uses_full_width_and_internal_horizontal_scrolling():
    css = (ROOT / "request_app" / "static" / "v0311.css").read_text(encoding="utf-8")
    base = (ROOT / "request_app" / "templates" / "base.html").read_text(encoding="utf-8")
    mobile = css[css.index("@media (max-width: 680px)"):]
    assert "overflow-x: auto" in mobile
    assert "overflow-y: hidden" in mobile
    assert "-webkit-overflow-scrolling: touch" in mobile
    assert "v0311.css" in base
