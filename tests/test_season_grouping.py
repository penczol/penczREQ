from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_tv_requests_are_grouped_after_existing_sort_and_filter():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "const activeSeasonByGroup = new Map();" in script
    assert "function groupSortedItems(items)" in script
    assert 'item.media_type !== "tv"' in script
    assert "const groups = groupSortedItems(items);" in script
    assert "groups.map(groupCardMarkup)" in script
    assert "paginationByState[state].total_all_items" in script


def test_season_tabs_keep_each_request_independent():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'data-action="switch-season"' in script
    assert 'data-request-id="${item.id}"' in script
    assert "activeSeasonByGroup.set(seasonGroupKey(item), item.id);" in script
    assert 'class="request-season-group"' in script
    assert "likeMarkup(item)" in script
    assert "statusMarkup(item)" in script
    assert "item.created_at" in script


def test_season_tabs_have_exact_desktop_and_mobile_labels_without_likes():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert '<span class="season-tab-desktop">${tr("Sezon {season}", { season })}</span>' in script
    assert '<span class="season-tab-mobile">S${mobileSeason}</span>' in script
    tabs_function = script[
        script.index("function seasonTabsMarkup"):
        script.index("function groupCardMarkup")
    ]
    assert "like_count" not in tabs_function


def test_season_tabs_have_separate_compact_desktop_and_touch_mobile_styles():
    css = (ROOT / "request_app" / "static" / "v0310.css").read_text(encoding="utf-8")
    base = (ROOT / "request_app" / "templates" / "base.html").read_text(encoding="utf-8")
    assert ".season-tab-desktop" in css
    assert ".season-tab-mobile" in css
    assert "@media (max-width: 680px)" in css
    mobile = css[css.index("@media (max-width: 680px)"):]
    assert "min-width: 48px" in mobile
    assert "min-height: 38px" in mobile
    assert "v0310.css" in base
