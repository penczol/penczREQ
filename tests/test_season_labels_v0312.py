from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_tv_kicker_never_repeats_the_season_number():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'const typeLabel = item.media_type === "movie" ? tr("Film") : tr("Serial");' in script
    assert 'Serial · sezon ${item.season_number' not in script


def test_every_tv_card_has_a_season_strip_even_with_one_request():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    tabs_function = script[
        script.index("function seasonTabsMarkup"):
        script.index("function groupCardMarkup")
    ]
    group_function = script[
        script.index("function groupCardMarkup"):
        script.index("function render")
    ]
    assert 'if (group.items.length < 2) return "";' not in tabs_function
    assert 'if (activeItem.media_type !== "tv") return cardMarkup(activeItem);' in group_function
    assert "${seasonTabsMarkup(group, activeItem)}${cardMarkup(activeItem)}" in group_function


def test_season_strip_rules_apply_to_every_main_section():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'const states = { active: [], upcoming: [], completed: [] };' in script
    assert "const groups = groupSortedItems(items);" in script
    assert "groups.map(groupCardMarkup)" in script
