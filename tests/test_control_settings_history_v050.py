from __future__ import annotations

from request_app.database import Database
from request_app.secure_config import (
    CONFIG_HISTORY_CHANGED,
    CONFIG_HISTORY_CONFIGURED,
    CONFIG_HISTORY_MISSING,
    SecureConfigStore,
    localize_settings_history,
)


def test_new_secret_history_uses_semantic_values(tmp_path):
    db = Database(tmp_path / "history.db")
    db.initialize()
    store = SecureConfigStore(db, "m" * 48)

    store.set_secret("tmdb_token", "first-test-token", changed_by="test")
    store.set_secret("tmdb_token", "second-test-token", changed_by="test")

    history = list(reversed(store.settings_history()))
    assert [
        (item["previous_value"], item["new_value"]) for item in history
    ] == [
        (CONFIG_HISTORY_MISSING, CONFIG_HISTORY_CHANGED),
        (CONFIG_HISTORY_CONFIGURED, CONFIG_HISTORY_CHANGED),
    ]
    assert not {"brak", "ustawiony", "zmieniony"}.intersection(
        {value for item in history for value in (item["previous_value"], item["new_value"])}
    )


def test_settings_history_localizes_exact_semantic_and_legacy_secret_values():
    items = [
        {"key": "tmdb_token", "previous_value": CONFIG_HISTORY_MISSING, "new_value": CONFIG_HISTORY_CHANGED},
        {"key": "tmdb_token", "previous_value": CONFIG_HISTORY_CONFIGURED, "new_value": CONFIG_HISTORY_CHANGED},
        {"key": "tmdb_token", "previous_value": "brak", "new_value": "zmieniony"},
        {"key": "tmdb_token", "previous_value": "ustawiony", "new_value": "zmieniony"},
    ]

    polish = localize_settings_history(items, "pl")
    english = localize_settings_history(items, "en")

    assert [(item["previous_value"], item["new_value"]) for item in polish] == [
        ("brak", "zmieniony"),
        ("ustawiony", "zmieniony"),
        ("brak", "zmieniony"),
        ("ustawiony", "zmieniony"),
    ]
    assert [(item["previous_value"], item["new_value"]) for item in english] == [
        ("none", "changed"),
        ("set", "changed"),
        ("none", "changed"),
        ("set", "changed"),
    ]


def test_settings_history_does_not_translate_unknown_or_arbitrary_values():
    arbitrary_values = (
        "",
        "changed",
        "enabled",
        "custom ustawiony value",
        "https://example.invalid/path?state=brak",
        "user-name",
        "192.0.2.10/32",
    )
    items = [
        {"key": "known_proxies", "previous_value": value, "new_value": value}
        for value in arbitrary_values
    ]
    items.append(
        {"key": "tmdb_token", "previous_value": "Brak", "new_value": "ZMIENIONY"}
    )

    assert localize_settings_history(items, "en") == items
