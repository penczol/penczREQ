from pathlib import Path

from starlette.requests import Request

from request_app.main import admin_view_enabled, is_real_admin


ROOT = Path(__file__).resolve().parents[1]


def request_with_cookie(cookie: str = "") -> Request:
    headers = [(b"cookie", cookie.encode("ascii"))] if cookie else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
        }
    )


def test_view_cookie_can_only_downgrade_real_admin():
    admin = {"role": "admin", "username": "admin"}
    user = {"role": "user", "username": "jan"}
    renamed_admin = {"role": "admin", "username": "jan"}

    assert is_real_admin(admin) is True
    assert admin_view_enabled(request_with_cookie(), admin) is True
    assert admin_view_enabled(
        request_with_cookie("request_admin_view=user"),
        admin,
    ) is False
    assert admin_view_enabled(request_with_cookie(), user) is False
    assert admin_view_enabled(request_with_cookie(), renamed_admin) is True


def test_diagnostic_mode_is_session_scoped_and_csrf_protected():
    main = (ROOT / "request_app" / "main.py").read_text(encoding="utf-8")
    assert '@app.post("/api/account/admin-view")' in main
    assert "admin=Depends(require_admin)" in main
    assert "verify_csrf(request, admin)" in main
    assert 'response.set_cookie(\n            ADMIN_VIEW_COOKIE,\n            "user"' in main
    assert "httponly=True" in main
    assert "response.delete_cookie(ADMIN_VIEW_COOKIE, path=\"/\")" in main


def test_diagnostic_request_list_uses_user_privacy_shape():
    main = (ROOT / "request_app" / "main.py").read_text(encoding="utf-8")
    route = main[
        main.index('@app.get("/api/requests")'):
        main.index('@app.post("/api/requests")')
    ]
    assert "admin_view_enabled(request, user)" in route
    assert 'user["role"] == "admin"' not in route


def test_admin_add_button_and_toggle_follow_presentation_mode():
    base = (ROOT / "request_app" / "templates" / "base.html").read_text(encoding="utf-8")
    index = (ROOT / "request_app" / "templates" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    common = (ROOT / "request_app" / "static" / "common.js").read_text(encoding="utf-8")

    assert "{% if not is_admin %}" in index
    assert 'data-open-search' in index
    assert 'document.querySelector("[data-open-search]")?.addEventListener' in app
    assert "{% if is_real_admin %}" in base
    assert "data-switch-admin-view" in base
    assert 'api("/api/account/admin-view"' in common
    assert 'window.location.assign("/")' in common


def test_v041_changelog_keeps_admin_details_private():
    changelog = (ROOT / "request_app" / "changelog.py").read_text(encoding="utf-8")
    assert '"version": "0.4.1"' in changelog
    assert '"public": ()' in changelog
