from __future__ import annotations

import re
import sqlite3
import json
from pathlib import Path
from string import Formatter
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from request_app.changelog import changelog_for
from request_app.database import Database
from request_app.i18n import EN_BY_PL, client_translations, translator
from request_app.http_security import (
    AllowedHostsMiddleware,
    ControlNetworkMiddleware,
    RequestBodyLimitMiddleware,
)
from request_app.repository import Repository
from request_app.i18n import localize_message


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "request_app" / "templates"
STATIC = ROOT / "request_app" / "static"


def _render_public(language: str, *, is_admin: bool) -> str:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
    )
    environment.globals["url_for"] = lambda _name, path: f"/static{path}"
    environment.globals["changelog_for"] = changelog_for
    return environment.get_template("index.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/")),
        user={"username": "audit-user"},
        is_admin=is_admin,
        is_real_admin=is_admin,
        csrf_token="csrf",
        app_env="test",
        app_version="0.5.0",
        language=language,
        t=translator(language),
        client_translations=client_translations(language),
    )


def _render_standalone(name: str, language: str) -> str:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
    )
    environment.globals["url_for"] = lambda _name, path: f"/static{path}"
    return environment.get_template(name).render(
        request=SimpleNamespace(url=SimpleNamespace(path="/")),
        user={"username": "audit-user"},
        control_user={"username": "audit-control"},
        csrf_token="csrf",
        app_env="test",
        app_version="0.5.0",
        language=language,
        t=translator(language),
        client_translations=client_translations(language),
        error=None,
    )


def test_mobile_and_desktop_tab_labels_render_from_the_active_catalog():
    english = _render_public("en", is_admin=False)
    polish = _render_public("pl", is_admin=False)

    assert 'data-state="active" data-mobile-label="Requests"' in english
    assert 'data-state="upcoming" data-mobile-label="Upcoming"' in english
    assert 'data-state="completed" data-mobile-label="Completed"' in english
    assert ">Requests <span" in english
    assert ">Upcoming <span" in english
    assert ">Completed <span" in english

    assert 'data-state="active" data-mobile-label="Requesty"' in polish
    assert 'data-state="upcoming" data-mobile-label="Premiery"' in polish
    assert 'data-state="completed" data-mobile-label="Gotowe"' in polish
    assert ">Requesty <span" in polish
    assert ">Przed premierą <span" in polish
    assert ">Zrealizowane <span" in polish

    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert 'content: attr(data-mobile-label)' in css
    assert 'content: "Requesty"' not in css
    assert 'content: "Premiery"' not in css
    assert 'content: "Gotowe"' not in css


def test_rendered_public_controls_cover_both_languages_and_roles():
    english_user = _render_public("en", is_admin=False)
    polish_user = _render_public("pl", is_admin=False)
    english_admin = _render_public("en", is_admin=True)

    for expected in (
        "Status",
        "All",
        "Pending",
        "Sort",
        "Date added — newest",
        "Per page",
        "Previous",
        "Next",
        "+ Add request",
        "Find a movie or TV show",
        "Notifications",
        "My account",
    ):
        assert expected in english_user
    for expected in (
        "Wszystkie",
        "Oczekujący",
        "Sortowanie",
        "Data dodania — najnowsze",
        "Na stronie",
        "Poprzednia",
        "Następna",
        "+ Dodaj request",
        "Znajdź film lub serial",
        "Powiadomienia",
        "Moje konto",
    ):
        assert expected in polish_user
    assert "data-open-search" not in english_admin
    assert "Permanently delete request" in english_admin


def test_authentication_and_control_templates_render_in_both_languages():
    public_login_en = _render_standalone("login.html", "en")
    public_force_en = _render_standalone("force_password.html", "en")
    control_login_en = _render_standalone("control_login.html", "en")
    control_force_en = _render_standalone("control_force_password.html", "en")
    control_en = _render_standalone("control_index.html", "en")
    control_pl = _render_standalone("control_index.html", "pl")

    assert '<html lang="en">' in public_login_en
    assert "Sign in" in public_login_en
    assert "Set your own password" in public_force_en
    assert "Local access" in control_login_en
    assert "Set your own Control password" in control_force_en
    for expected in (
        "local management center",
        "LAN only",
        "Overview",
        "Lockouts and logs",
        "Public accounts",
        "Configuration",
        "Backups and integrity",
        "Control account",
        "Notification for everyone",
    ):
        assert expected in control_en
    for expected in (
        "lokalne centrum zarządzania",
        "Tylko LAN",
        "Przegląd",
        "Blokady i logi",
        "Konta publiczne",
        "Konfiguracja",
        "Kopie i integralność",
        "Konto Control",
        "Powiadomienie dla wszystkich",
    ):
        assert expected in control_pl


def _http_scope(*, cookie: str = "", host: str = "testserver", client: str = "127.0.0.1"):
    headers = [(b"host", host.encode("ascii"))]
    if cookie:
        headers.append((b"cookie", cookie.encode("ascii")))
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/audit",
        "raw_path": b"/audit",
        "query_string": b"",
        "headers": headers,
        "client": (client, 50123),
        "server": ("testserver", 80),
    }


async def _middleware_response(middleware, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return status, body


@pytest.mark.asyncio
async def test_early_http_security_errors_follow_the_language_cookie():
    async def inner(scope, receive, send):
        raise AssertionError("blocked request reached the application")

    body_limit = RequestBodyLimitMiddleware(inner, 1)
    en_scope = _http_scope(cookie="penczreq_language=en")
    en_scope["headers"].append((b"content-length", b"2"))
    status, body = await _middleware_response(body_limit, en_scope)
    assert status == 413
    assert json.loads(body)["detail"] == "The request is too large."

    pl_scope = _http_scope(cookie="penczreq_language=pl")
    pl_scope["headers"].append((b"content-length", b"2"))
    _, body = await _middleware_response(body_limit, pl_scope)
    assert json.loads(body)["detail"] == "Żądanie jest zbyt duże."

    hosts = AllowedHostsMiddleware(inner, ("allowed.invalid",))
    _, body = await _middleware_response(
        hosts, _http_scope(cookie="penczreq_language=en", host="blocked.invalid")
    )
    assert body.decode() == "Invalid host."

    control = ControlNetworkMiddleware(inner, "127.0.0.0/8")
    _, body = await _middleware_response(
        control,
        _http_scope(
            cookie="penczreq_control_language=pl",
            client="203.0.113.10",
        ),
    )
    assert body.decode() == "Panel Control jest dostępny wyłącznie lokalnie."


def test_framework_default_404_and_405_use_the_localized_starlette_handler():
    audit_app = FastAPI()

    @audit_app.middleware("http")
    async def language_context(request: Request, call_next):
        request.state.language = request.headers.get("x-language", "en")
        return await call_next(request)

    @audit_app.exception_handler(StarletteHTTPException)
    async def localized_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if exc.status_code == 404 and detail == "Not Found":
            detail = "Nie znaleziono strony."
        elif exc.status_code == 405 and detail == "Method Not Allowed":
            detail = "Metoda nie jest dozwolona."
        return JSONResponse(
            {"detail": localize_message(detail, request.state.language)},
            status_code=exc.status_code,
        )

    @audit_app.get("/known")
    async def known():
        return {"ok": True}

    with TestClient(audit_app) as client:
        assert client.get("/missing", headers={"x-language": "en"}).json() == {
            "detail": "Page not found."
        }
        assert client.post("/known", headers={"x-language": "en"}).json() == {
            "detail": "Method not allowed."
        }
        assert client.get("/missing", headers={"x-language": "pl"}).json() == {
            "detail": "Nie znaleziono strony."
        }


@pytest.mark.parametrize(
    ("source", "english"),
    (
        ("Nieprawidłowy język interfejsu.", "Invalid interface language."),
        ("Zaloguj się ponownie.", "Sign in again."),
        ("Nieprawidłowy token formularza. Odśwież stronę.", "Invalid form token. Refresh the page."),
        ("Nie znaleziono strony.", "Page not found."),
        ("Metoda nie jest dozwolona.", "Method not allowed."),
        ("Nieprawidłowe dane żądania.", "Invalid request data."),
        ("Zbyt wiele operacji. Spróbuj ponownie później.", "Too many operations. Try again later."),
        ("Wewnętrzny błąd serwera.", "Internal server error."),
    ),
)
def test_http_error_surfaces_have_deterministic_english(source, english):
    assert localize_message(source, "en") == english
    assert localize_message(source, "pl") == source


def test_css_does_not_generate_alphabetic_ui_copy_from_string_literals():
    findings: list[tuple[str, str]] = []
    pattern = re.compile(r"content\s*:\s*([\"'])(.*?)\1", re.DOTALL)
    for path in STATIC.glob("*.css"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            if re.search(r"[A-Za-ząćęłńóśźż]", match.group(2)):
                findings.append((path.name, match.group(2)))
    assert findings == []


def test_public_and_control_localize_network_and_timeout_failures():
    for name in ("common.js", "control.js"):
        script = (STATIC / name).read_text(encoding="utf-8")
        assert 'error?.name === "AbortError"' in script
        assert 'tr("Przekroczono czas oczekiwania na odpowiedź serwera.")' in script
        assert 'tr("Nie udało się połączyć z serwerem.")' in script


def test_template_literal_visible_copy_is_limited_to_brand_and_technical_tokens():
    allowed = {
        "penczREQ",
        "penczREQ Control",
        "EN",
        "PL",
        "Pr",
        "TMDB",
        "APP_BASE_URL",
    }
    findings: list[tuple[str, str]] = []
    pattern = re.compile(r">([^<{]+)<")
    for path in TEMPLATES.glob("*.html"):
        for raw in pattern.findall(path.read_text(encoding="utf-8")):
            value = re.sub(r"\s+", " ", raw).strip(" |\n\t")
            if value and re.search(r"[A-Za-ząćęłńóśźż]", value) and value not in allowed:
                findings.append((path.name, value))
    assert findings == []


def test_catalog_has_language_and_placeholder_parity():
    assert EN_BY_PL
    for polish, english in EN_BY_PL.items():
        assert polish.strip() and english.strip()
        polish_fields = {name for _, name, _, _ in Formatter().parse(polish) if name}
        english_fields = {name for _, name, _, _ in Formatter().parse(english) if name}
        assert english_fields == polish_fields, polish
        assert not re.search(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]", english), polish
    assert set(client_translations("en")) == set(EN_BY_PL)
    assert client_translations("pl") == {}


def _legacy_database(path: Path) -> Database:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                is_active INTEGER NOT NULL,
                must_change_password INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                season_number INTEGER,
                title_pl TEXT NOT NULL,
                title_original TEXT NOT NULL,
                state TEXT NOT NULL,
                status TEXT NOT NULL,
                release_date TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                read_at TEXT
            );
            INSERT INTO users
                (id, username, password_hash, role, is_active, must_change_password, created_at)
            VALUES (1, 'legacy-user', 'x', 'user', 1, 0, '2026-01-01T00:00:00+00:00');
            INSERT INTO requests
                (id, tmdb_id, media_type, season_number, title_pl, title_original,
                 state, status, release_date, created_at)
            VALUES
                (1, 9001, 'tv', 2, 'Tytuł historyczny', 'Legacy Original Title',
                 'active', 'pending', '2026-01-01', '2026-01-01T00:00:00+00:00');
            """
        )
        legacy_private_push_test = bytes.fromhex(
            "506f776961646f6d69656e69612073797374656d6f77652050454e435a464c4958"
            "20726571756573747920647a6961c582616ac48520707261776964c5826f776f2e"
        ).decode("utf-8")
        records = (
            ("app_update", "Aktualizacja 0.4.3", "Wdrożono wersję 0.4.3.\nPełna historia zmian jest dostępna po kliknięciu numeru wersji.", None),
            ("admin_new_request", "Nowy request", "Użytkownik legacy-user dodał request „Tytuł historyczny — sezon 2”.", 1),
            ("own_request_liked", "Nowe polubienie", "Ktoś polubił Twój request „Tytuł historyczny — sezon 2”.", 1),
            ("request_changes", "Zmiana statusu", "Pozycja „Tytuł historyczny — sezon 2” ma teraz status: W trakcie realizacji.", 1),
            ("request_changes", "Pozycja dostępna", "Pozycja „Tytuł historyczny — sezon 2” została zrealizowana i jest już dostępna.", 1),
            ("request_changes", "Pozycja przywrócona", "Pozycja „Tytuł historyczny — sezon 2” wróciła do aktywnych requestów.", 1),
            ("request_changes", "Request usunięty", "Request „Usunięty tytuł” został usunięty. Powód: ręczna przyczyna", None),
            ("request_changes", "Pozycja po premierze", "Pozycja „Tytuł historyczny — sezon 2” miała premierę i została przeniesiona do aktywnych requestów.", 1),
            ("request_changes", "Request przed premierą", "Pozycja „Zapowiedziany tytuł” została sklasyfikowana jako przed premierą i umieszczona w karcie „Przed premierą”.", None),
            ("system", "Test powiadomień", legacy_private_push_test, None),
            ("admin_messages", "Ręczny tytuł administratora", "Dowolna treść użytkownika — pozostaw bez zmian.", None),
        )
        conn.executemany(
            """
            INSERT INTO notifications
                (user_id, type, title, body, request_id, created_at, read_at)
            VALUES (1, ?, ?, ?, ?, '2026-02-03T04:05:06+00:00', '2026-02-04T05:06:07+00:00')
            """,
            records,
        )
    return Database(path)


def test_legacy_043_notifications_migrate_idempotently_and_follow_current_language(tmp_path):
    db = _legacy_database(tmp_path / "legacy-043.db")
    db.initialize()
    repo = Repository(db)

    with db.connect() as conn:
        raw_before = conn.execute(
            "SELECT id, title, body, created_at, read_at, event_key, event_payload_json FROM notifications ORDER BY id"
        ).fetchall()
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert sum(row["event_key"] is not None for row in raw_before) == 10
        unknown = raw_before[-1]
        assert unknown["event_key"] is None

    polish = repo.notifications(1)["items"]
    assert next(item for item in polish if item["id"] == 1)["title"] == "Aktualizacja 0.4.3"
    assert next(item for item in polish if item["id"] == 4)["body"].endswith("W trakcie realizacji.")

    repo.set_user_language(1, "en")
    english = repo.notifications(1)["items"]
    assert next(item for item in english if item["id"] == 1)["title"] == "Update 0.4.3"
    assert next(item for item in english if item["id"] == 2)["body"] == (
        "User legacy-user added the request “Legacy Original Title — season 2”."
    )
    assert next(item for item in english if item["id"] == 4)["body"].endswith("In progress.")
    assert next(item for item in english if item["id"] == 7)["body"] == (
        "The request “Usunięty tytuł” was deleted. Reason: ręczna przyczyna"
    )
    assert next(item for item in english if item["id"] == 10)["title"] == "Notification test"
    unknown_en = next(item for item in english if item["id"] == 11)
    assert unknown_en["title"] == "Ręczny tytuł administratora"
    assert unknown_en["body"] == "Dowolna treść użytkownika — pozostaw bez zmian."

    repo.set_user_language(1, "pl")
    polish_again = repo.notifications(1)["items"]
    assert next(item for item in polish_again if item["id"] == 2)["body"] == (
        "Użytkownik legacy-user dodał request „Tytuł historyczny — sezon 2”."
    )

    db.initialize()
    with db.connect() as conn:
        raw_after = conn.execute(
            "SELECT id, title, body, created_at, read_at, event_key, event_payload_json FROM notifications ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in raw_after] == [tuple(row) for row in raw_before]


def test_structured_bilingual_broadcast_switches_but_literal_legacy_message_does_not(tmp_path):
    db = Database(tmp_path / "broadcast.db")
    db.initialize()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO users
                (username, password_hash, role, is_active, must_change_password, language, created_at)
            VALUES ('recipient', 'x', 'user', 1, 0, 'pl', '2026-01-01T00:00:00+00:00')
            """
        )
    repo = Repository(db)
    user_id = int(repo.user_by_username("recipient")["id"])
    repo.broadcast_localized(
        0,
        title_en="English broadcast",
        body_en="English body",
        title_pl="Polski broadcast",
        body_pl="Polska treść",
    )
    repo.broadcast(0, "Jednojęzyczny literal", "Nie tłumacz arbitralnie")

    repo.set_user_language(user_id, "en")
    english = repo.notifications(user_id)["items"]
    assert {item["title"] for item in english} == {
        "English broadcast",
        "Jednojęzyczny literal",
    }
    repo.set_user_language(user_id, "pl")
    polish = repo.notifications(user_id)["items"]
    assert {item["title"] for item in polish} == {
        "Polski broadcast",
        "Jednojęzyczny literal",
    }
