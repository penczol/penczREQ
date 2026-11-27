from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


PUBLIC_START = "PublicStartPassword99Z"
PUBLIC_FINAL = "PublicFinalPassword99Z"
CONTROL_START = "ControlStartPassword99Z"
CONTROL_FINAL = "ControlFinalPassword99Z"
TMDB_SENTINEL = "runtime-smoke-tmdb-secret-987654321"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def csrf_from_html(html: str) -> str:
    match = re.search(r'(?:name="csrf_token" value|name="csrf-token" content)="([^"]+)"', html)
    require(bool(match), "Brak tokenu CSRF w odpowiedzi HTML.")
    return match.group(1)


def login_csrf_from_html(html: str) -> str:
    match = re.search(r'name="login_csrf_token" value="([^"]+)"', html)
    require(bool(match), "Brak tokenu CSRF logowania w odpowiedzi HTML.")
    return match.group(1)


def assert_headers(response) -> None:
    require(response.headers.get("x-frame-options") == "DENY", "Brak X-Frame-Options.")
    require(response.headers.get("x-content-type-options") == "nosniff", "Brak nosniff.")
    require("default-src 'self'" in response.headers.get("content-security-policy", ""), "Brak CSP.")


def check_request_flow(public, main, csrf):
    """Exercise the HTTP handlers with a deterministic TMDB response."""
    import httpx
    from unittest.mock import patch

    def respond(request):
        title = "Film testowy" if request.url.params.get("language") == "pl-PL" else "Test film"
        movie = {"id": 125, "media_type": "movie", "title": title, "original_title": "Test film",
                 "release_date": "2020-01-01"}
        if request.url.path == "/3/search/multi":
            return httpx.Response(200, json={"results": [movie], "total_pages": 2})
        require(request.url.path == "/3/movie/125", "Unexpected fixture route")
        return httpx.Response(200, json=movie)

    client = main.TMDBClient("fixture", main.settings.posters_dir, transport=httpx.MockTransport(respond))
    with patch.object(main, "tmdb", client):
        for lang, title in (("en", "Test film"), ("pl", "Film testowy")):
            changed = public.post("/language", data={"language": lang, "csrf_token": csrf})
            require(changed.status_code == 303, "Language change failed")
            response = public.get("/api/tmdb/search", params={"q": "test", "page": 2})
            response.raise_for_status()
            results = response.json()
            require(results["page"] == 2 and results["total_pages"] == 2, "Pagination failed")
            require(results["items"][0]["title_" + lang] == title, "Wrong search title")
        headers = {"X-CSRF-Token": csrf}
        movie = {"media_type": "movie", "tmdb_id": 125}
        first = public.post("/api/requests", headers=headers, json=movie)
        first.raise_for_status()
        second = public.post("/api/requests", headers=headers, json=movie)
        second.raise_for_status()
        require(not first.json()["duplicate"] and second.json()["duplicate"], "Duplicate handling failed")
        require(first.json()["id"] == second.json()["id"], "Duplicate produced another request")
        items = public.get("/api/requests", params={"state": "active"}).json()["items"]
        require(len(items) == 1 and items[0]["tmdb_id"] == 125, "Submitted movie missing")


def run() -> None:
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory(
        prefix="penczreq-security-smoke-", ignore_cleanup_errors=True
    ) as temporary:
        data_dir = Path(temporary)
        os.environ.update(
            {
                "APP_ENV": "test",
                "APP_BASE_URL": "http://testserver",
                "DATA_DIR": str(data_dir),
                "SESSION_SECRET": "s" * 64,
                "CONTROL_SESSION_SECRET": "c" * 64,
                "CONFIG_ENCRYPTION_KEY": "e" * 64,
                "COOKIE_SECURE": "false",
                "ALLOWED_HOSTS": "testserver,127.0.0.1",
                "CONTROL_ALLOWED_HOSTS": "testserver,127.0.0.1",
                "CONTROL_ALLOWED_NETWORKS": "192.0.2.0/24",
                "WEB_PUSH_DELIVERY": "disabled",
                "CONTROL_DATA_DIR": str(data_dir / "control"),
                "BACKUP_DIR": str(data_dir / "backups"),
                "LOG_DIR": str(data_dir / "logs"),
                "PUBLIC_ADMIN_USERNAME": "runtime-admin",
                "PUBLIC_ADMIN_BOOTSTRAP_PASSWORD": PUBLIC_START,
                "CONTROL_ADMIN_USERNAME": "runtime-control",
                "CONTROL_BOOTSTRAP_PASSWORD": CONTROL_START,
                "TMDB_TOKEN": "",
            }
        )

        from request_app import control, main

        client_options = {"base_url": "http://testserver", "follow_redirects": False,
                          "client": ("192.0.2.1", 50000)}
        with TestClient(control.app, **client_options) as panel, TestClient(main.app, **client_options) as public:
            login_page = public.get("/login")
            require(login_page.status_code == 200, "Publiczna strona logowania nie działa.")
            login_csrf = login_csrf_from_html(login_page.text)
            assert_headers(login_page)
            require(login_page.headers.get("cache-control") == "no-store", "Login może być cache'owany.")
            require(public.get("/openapi.json").status_code == 404, "OpenAPI jest publicznie dostępne.")
            require(public.get("/docs").status_code == 404, "Swagger jest publicznie dostępny.")
            require(public.get("/health").status_code == 404, "Stary healthcheck jest dostępny.")
            require(public.get("/internal/health").status_code == 404, "Healthcheck nie jest wewnętrzny.")
            require(public.get("/posters/not-found.jpg").status_code == 401, "Plakaty nie wymagają sesji.")
            require(public.get("/login", headers={"Host": "attacker.invalid"}).status_code == 400, "Host header nie jest filtrowany.")
            oversized = public.post(
                "/login", content=b"x" * 300_000,
                headers={"content-type": "application/octet-stream"},
            )
            require(oversized.status_code == 413, "Limit rozmiaru żądania nie działa.")

            require(
                public.post(
                    "/login", data={"username": "runtime-admin", "password": PUBLIC_START}
                ).status_code == 403,
                "Publiczne logowanie nie wymaga pre-auth CSRF.",
            )
            logged = public.post(
                "/login",
                data={
                    "username": "runtime-admin",
                    "password": PUBLIC_START,
                    "login_csrf_token": login_csrf,
                },
            )
            require(logged.status_code == 303 and logged.headers["location"] == "/force-password", "Publiczny bootstrap logowania nie działa.")
            cookie = logged.headers.get("set-cookie", "")
            require("HttpOnly" in cookie and "SameSite=lax" in cookie, "Publiczne cookie ma złe flagi.")
            forced = public.get("/force-password")
            csrf = csrf_from_html(forced.text)
            changed = public.post(
                "/force-password",
                data={"new_password": PUBLIC_FINAL, "confirm_password": PUBLIC_FINAL, "csrf_token": csrf},
            )
            require(changed.status_code == 303 and changed.headers["location"] == "/", "Wymuszona zmiana hasła publicznego nie działa.")
            home = public.get("/")
            require(home.status_code == 200 and "runtime-admin" in home.text, "Publiczna sesja po zmianie hasła nie działa.")
            require(public.get("/api/control/users").status_code == 404, "Control przecieka do publicznego serwera.")

            check_request_flow(public, main, csrf_from_html(home.text))

            login_page = panel.get("/login")
            require(login_page.status_code == 200, "Strona logowania Control nie działa.")
            login_csrf = login_csrf_from_html(login_page.text)
            assert_headers(login_page)
            require(login_page.headers.get("cache-control") == "no-store", "Control może być cache'owany.")
            require(panel.get("/openapi.json").status_code == 404, "OpenAPI Control jest dostępne.")
            require(panel.get("/internal/health").status_code == 404, "Healthcheck Control nie jest wewnętrzny.")
            require(panel.get("/login", headers={"Host": "attacker.invalid"}).status_code == 400, "Control nie filtruje Host.")

            require(
                panel.post(
                    "/login", data={"username": "runtime-control", "password": CONTROL_START}
                ).status_code == 403,
                "Logowanie Control nie wymaga pre-auth CSRF.",
            )
            logged = panel.post(
                "/login",
                data={
                    "username": "runtime-control",
                    "password": CONTROL_START,
                    "login_csrf_token": login_csrf,
                },
            )
            require(logged.status_code == 303 and logged.headers["location"] == "/force-password", "Bootstrap Control nie działa.")
            cookie = logged.headers.get("set-cookie", "")
            require("HttpOnly" in cookie and "SameSite=strict" in cookie, "Cookie Control ma złe flagi.")
            forced = panel.get("/force-password")
            csrf = csrf_from_html(forced.text)
            changed = panel.post(
                "/force-password",
                data={
                    "current_password": CONTROL_START,
                    "new_password": CONTROL_FINAL,
                    "confirm_password": CONTROL_FINAL,
                    "csrf_token": csrf,
                },
            )
            require(changed.status_code == 303 and changed.headers["location"] == "/login", "Zmiana hasła Control nie działa.")
            login_csrf = login_csrf_from_html(panel.get("/login").text)
            logged = panel.post(
                "/login",
                data={
                    "username": "runtime-control",
                    "password": CONTROL_FINAL,
                    "login_csrf_token": login_csrf,
                },
            )
            require(logged.status_code == 303 and logged.headers["location"] == "/", "Ponowne logowanie Control nie działa.")
            dashboard = panel.get("/")
            require(dashboard.status_code == 200 and "runtime-control" in dashboard.text, "Dashboard Control nie działa.")
            csrf = csrf_from_html(dashboard.text)
            overview = panel.get("/api/control/overview")
            require(overview.status_code == 200 and overview.json()["integrity"] == {"app": "ok", "control": "ok"}, "Kontrola integralności nie działa.")
            denied = panel.post(
                "/api/control/users",
                json={"username": "runtime-user", "temporary_password": "RuntimeUserPassword99Z", "current_password": CONTROL_FINAL},  # pragma: allowlist secret
                headers={"X-CSRF-Token": "wrong"},
            )
            require(denied.status_code == 403, "Operacja Control akceptuje błędny CSRF.")
            created = panel.post(
                "/api/control/users",
                json={"username": "runtime-user", "temporary_password": "RuntimeUserPassword99Z", "current_password": CONTROL_FINAL},  # pragma: allowlist secret
                headers={"X-CSRF-Token": csrf},
            )
            require(created.status_code == 200, f"Tworzenie użytkownika przez Control nie działa: {created.text}")
            require("password_hash" not in created.text, "Hash hasła wyciekł w odpowiedzi API.")
            bad_reauth = panel.post(
                "/api/control/backup", json={"current_password": "wrong-password"},  # pragma: allowlist secret
                headers={"X-CSRF-Token": csrf},
            )
            require(bad_reauth.status_code == 400, "Wrażliwa operacja nie wymaga poprawnego hasła Control.")
            saved = panel.put(
                "/api/control/settings",
                json={
                    "current_password": CONTROL_FINAL,
                    "tmdb_token": TMDB_SENTINEL,
                    "public_base_url": "http://testserver",
                    "known_proxies": "127.0.0.1/32",
                    "security_log_retention_days": 30,
                    "backup_retention_days": 30,
                },
                headers={"X-CSRF-Token": csrf},
            )
            require(saved.status_code == 200, f"Zapis ustawień Control nie działa: {saved.text}")
            settings_response = panel.get("/api/control/settings")
            require(settings_response.status_code == 200 and settings_response.json()["tmdb_configured"], "Zaszyfrowany klucz TMDB nie jest aktywny.")
            require(TMDB_SENTINEL not in settings_response.text, "Klucz TMDB wyciekł przez API.")
            backup = panel.post(
                "/api/control/backup", json={"current_password": CONTROL_FINAL},
                headers={"X-CSRF-Token": csrf},
            )
            require(backup.status_code == 200 and set(backup.json()["files"]) == {"app", "control"}, "Ręczna kopia obu baz nie działa.")

        app_bytes = b"".join(
            path.read_bytes() for path in data_dir.glob("app.db*") if path.is_file()
        )
        require(TMDB_SENTINEL.encode() not in app_bytes, "Klucz TMDB jest zapisany jawnie w bazie.")
        logs = "\n".join(path.read_text(encoding="utf-8") for path in (data_dir / "logs").glob("*.jsonl"))
        for secret in (PUBLIC_START, PUBLIC_FINAL, CONTROL_START, CONTROL_FINAL, TMDB_SENTINEL):
            require(secret not in logs, "Sekret wyciekł do dziennika bezpieczeństwa.")
        with sqlite3.connect(data_dir / "app.db") as conn:
            require(conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "Baza app.db jest uszkodzona.")
        with sqlite3.connect(data_dir / "control" / "control.db") as conn:
            require(conn.execute("PRAGMA quick_check").fetchone()[0] == "ok", "Baza control.db jest uszkodzona.")

        print(json.dumps({"status": "ok", "checks": "public+control+csrf+reauth+secrets+backups+integrity+tmdb+requests+pl-en"}))


if __name__ == "__main__":
    run()
