from __future__ import annotations

import ast
import re
from pathlib import Path

from request_app.changelog import CHANGELOG, changelog_for, update_notification_bodies
from request_app.control_store import ControlStore
from request_app.database import Database, utc_now
from request_app.i18n import EN_BY_PL, localize_message, looks_polish, normalize_language, translate
from request_app.push import PushService
from request_app.repository import Repository
from request_app.security import hash_password
from request_app.tmdb import MediaDetails
from request_app.updates import notify_upcoming_request, notify_users_about_update


ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "BezpieczneHaslo2026"  # pragma: allowlist secret


def _media() -> MediaDetails:
    return MediaDetails(
        tmdb_id=990_001,
        media_type="movie",
        season_number=None,
        imdb_id="tt0990001",
        title_pl="Polski tytuł testowy",
        title_en="English localized test title",
        title_original="Original test title",
        release_year=2026,
        release_date="2026-01-01",
        original_language="en",
        poster_remote_path=None,
        world_theatrical_date="2026-01-01",
        pl_theatrical_date="2026-02-01",
    )


def _repository(tmp_path: Path) -> tuple[Repository, int, int]:
    db = Database(tmp_path / "i18n.db")
    db.initialize()
    password_hash = hash_password(PASSWORD)
    with db.transaction() as conn:
        polish = int(conn.execute(
            """
            INSERT INTO users
            (username, password_hash, role, is_active, must_change_password, language, created_at)
            VALUES ('polish', ?, 'user', 1, 0, 'pl', ?)
            """,
            (password_hash, utc_now()),
        ).lastrowid)
        english = int(conn.execute(
            """
            INSERT INTO users
            (username, password_hash, role, is_active, must_change_password, language, created_at)
            VALUES ('english', ?, 'admin', 1, 0, 'en', ?)
            """,
            (password_hash, utc_now()),
        ).lastrowid)
    return Repository(db), polish, english


def test_translation_fallback_and_normalization_are_deterministic():
    assert normalize_language("PL") == "pl"
    assert normalize_language("de") == "en"
    assert translate("Requesty", "en") == "Requests"
    assert translate("Requesty", "pl") == "Requesty"
    assert translate("Nieznany tekst", "en") == "Nieznany tekst"
    assert localize_message("TMDB zwróciło błąd HTTP 401.", "en") == "TMDB returned HTTP error 401."


def test_templates_and_javascript_reference_only_complete_translation_entries():
    files = list((ROOT / "request_app" / "templates").glob("*.html"))
    files += list((ROOT / "request_app" / "static").glob("*.js"))
    pattern = re.compile(r"\b(?:t|tr)\(\s*[\"']([^\"']+)[\"']")
    keys: set[str] = set()
    for path in files:
        keys.update(pattern.findall(path.read_text(encoding="utf-8")))
    normalized_keys = {key.replace("\\n", "\n") for key in keys}
    assert normalized_keys <= EN_BY_PL.keys()

    templates = "\n".join(path.read_text(encoding="utf-8") for path in files if path.suffix == ".html")
    assert '<html lang="pl">' not in templates
    assert '<html lang="{{ language }}">' in templates


def test_user_facing_exception_and_http_detail_literals_have_english_translations():
    exception_names = {
        "RepositoryError", "ControlError", "TMDBError", "SecureConfigError", "HTTPException"
    }
    missing: list[tuple[str, int, str]] = []
    for path in (ROOT / "request_app").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in exception_names:
                continue
            sources = list(node.args[:1])
            sources.extend(item.value for item in node.keywords if item.arg == "detail")
            for source in sources:
                if isinstance(source, ast.Constant) and isinstance(source.value, str):
                    if looks_polish(source.value) and source.value not in EN_BY_PL:
                        missing.append((path.name, node.lineno, source.value))
    assert missing == []


def test_complete_changelog_and_update_summary_are_available_in_english():
    sources = [
        change
        for entry in CHANGELOG
        for bucket in ("public", "admin")
        for change in entry[bucket]
    ] + ["Inne poprawki administracyjne."]
    assert set(sources) <= EN_BY_PL.keys()
    english = changelog_for(True, "en")
    polish = changelog_for(True, "pl")
    assert english[0]["changes"] != polish[0]["changes"]
    public_en, admin_en = update_notification_bodies("0.5.0", "en")
    assert "Version 0.5.0 was deployed." in public_en
    assert "recipient's language" in admin_en


def test_language_is_persisted_and_new_notifications_use_recipient_language(tmp_path):
    repo, polish, english = _repository(tmp_path)
    repo.set_user_language(polish, "en")
    assert repo.user_by_id(polish)["language"] == "en"
    repo.set_user_language(polish, "pl")

    request_id, _, _ = repo.create_request(_media(), None, polish)
    admin_notification = repo.notifications(english, "unread")["items"][0]
    assert admin_notification["title"] == "New request"
    assert "English localized test title" in admin_notification["body"]

    repo.set_status(request_id, "in_progress")
    polish_notification = repo.notifications(polish, "unread")["items"][0]
    assert polish_notification["title"] == "Zmiana statusu"
    assert "Polski tytuł testowy" in polish_notification["body"]
    assert "W trakcie realizacji" in polish_notification["body"]


def test_control_language_is_persisted(tmp_path):
    store = ControlStore(tmp_path / "control.db", "test-control-session-secret")
    store.initialize()
    assert store.bootstrap("control", PASSWORD, development=True) is True
    user = store.authenticate("control", PASSWORD, "127.0.0.1")
    assert user["language"] == "en"
    store.set_language(int(user["id"]), "pl")
    assert store.user_by_id(int(user["id"]))["language"] == "pl"


def test_broadcast_and_push_test_are_stored_in_each_recipient_language(tmp_path):
    repo, polish, english = _repository(tmp_path)
    assert repo.broadcast_localized(
        0,
        title_en="English title",
        body_en="English body",
        title_pl="Polski tytuł",
        body_pl="Polska treść",
    ) == 2
    assert repo.notifications(english)["items"][0]["title"] == "English title"
    assert repo.notifications(polish)["items"][0]["title"] == "Polski tytuł"

    push = PushService(repo.db, tmp_path / "unused.pem", "mailto:test@example.com")
    push.create_test_notification(english)
    push.create_test_notification(polish)
    english_test = next(
        item for item in repo.notifications(english)["items"] if item["type"] == "system"
    )
    polish_test = next(
        item for item in repo.notifications(polish)["items"] if item["type"] == "system"
    )
    assert english_test["title"] == "Notification test"
    assert polish_test["title"] == "Test powiadomień"


def test_upcoming_and_update_notifications_use_account_language(tmp_path):
    repo, polish, english = _repository(tmp_path)
    assert notify_upcoming_request(
        repo.db,
        english,
        "Polski tytuł testowy",
        None,
        "Original test title",
        "English localized test title",
    ) is True
    english_upcoming = repo.notifications(english)["items"][0]
    assert english_upcoming["title"] == "Upcoming request"
    assert "English localized test title" in english_upcoming["body"]

    assert notify_users_about_update(
        repo.db,
        "9.9-i18n",
        "PUBLIC PL",
        "ADMIN PL",
        "PUBLIC EN",
        "ADMIN EN",
    ) == 2
    english_update = next(
        item for item in repo.notifications(english)["items"] if item["type"] == "app_update"
    )
    polish_update = next(
        item for item in repo.notifications(polish)["items"] if item["type"] == "app_update"
    )
    assert english_update["title"] == "Update 9.9-i18n"
    assert english_update["body"] == "ADMIN EN"
    assert polish_update["title"] == "Aktualizacja 9.9-i18n"
    assert polish_update["body"] == "PUBLIC PL"


def test_public_frontend_en_contract_uses_english_title_and_hides_polish_dates():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'const localized = language === "pl" ? item.title_pl : item.title_en;' in script
    assert 'cleanTitle(localized) || cleanTitle(item.title_original) || "—"' in script
    assert 'language === "pl" ? item.title_original : item.title_pl' not in script
    assert 'if (language === "pl") {' in script
    assert 'item.pl_theatrical_date' in script
    assert 'item.pl_digital_date' in script
    assert 'item.pl_physical_date' in script
