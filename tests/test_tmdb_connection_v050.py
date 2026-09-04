from __future__ import annotations

import logging

import httpx
import pytest
from starlette.requests import Request

from request_app.database import Database
from request_app.secure_config import SecureConfigStore
from request_app.tmdb import TMDBClient, TMDBError


def mock_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_tmdb_uses_read_access_token_as_bearer_without_api_key(tmp_path):
    token = "tmdb-read-access-" + "a" * 48
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = TMDBClient(token, tmp_path, transport=mock_transport(handler))
    assert await client._get("/configuration") == {"ok": True}

    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == f"Bearer {token}"
    assert "api_key" not in requests[0].url.params


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (401, 403))
async def test_tmdb_rejects_invalid_read_access_token_without_leaking_it(
    tmp_path, caplog, status
):
    token = "tmdb-invalid-" + "b" * 48

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            request=request,
            json={"status_message": "Invalid API key: You must be granted a valid key."},
        )

    caplog.set_level(logging.WARNING, logger="request_app.tmdb")
    client = TMDBClient(token, tmp_path, transport=mock_transport(handler))
    with pytest.raises(TMDBError, match="API Read Access Token"):
        await client._get("/configuration")

    assert f"status={status}" in caplog.text
    assert "endpoint=https://api.themoviedb.org/3/configuration" in caplog.text
    assert "You must be granted a valid key" in caplog.text
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_tmdb_missing_token_fails_before_network(tmp_path, caplog):
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, request=request, json={})

    caplog.set_level(logging.WARNING, logger="request_app.tmdb")
    client = TMDBClient("", tmp_path, transport=mock_transport(handler))
    with pytest.raises(TMDBError, match="Brak tokenu TMDB"):
        await client._get("/configuration")

    assert called is False
    assert "TMDBError" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_type", "message"),
    (
        (httpx.ReadTimeout, "timed out"),
        (httpx.ConnectError, "DNS lookup or network connection failed"),
    ),
)
async def test_tmdb_timeout_and_network_errors_are_diagnostic_but_safe(
    tmp_path, caplog, exception_type, message
):
    token = "tmdb-network-" + "c" * 48

    def handler(request: httpx.Request) -> httpx.Response:
        raise exception_type(message, request=request)

    caplog.set_level(logging.WARNING, logger="request_app.tmdb")
    client = TMDBClient(token, tmp_path, transport=mock_transport(handler))
    with pytest.raises(TMDBError, match="Nie udało się połączyć z TMDB"):
        await client._get("/search/multi", query="safe-title")

    assert exception_type.__name__ in caplog.text
    assert "endpoint=https://api.themoviedb.org/3/search/multi" in caplog.text
    assert "status=none" in caplog.text
    assert token not in caplog.text
    assert "Authorization" not in caplog.text


@pytest.mark.asyncio
async def test_control_save_is_encrypted_and_immediately_visible_to_control_and_public(
    tmp_path, monkeypatch
):
    from request_app import control

    db = Database(tmp_path / "app.db")
    db.initialize()
    store = SecureConfigStore(db, "e" * 48)
    token = "tmdb-current-" + "d" * 48
    observed_headers: list[str] = []
    audit_events: list[dict] = []

    class AuditStub:
        def emit(self, event_type, **kwargs):
            audit_events.append({"event_type": event_type, **kwargs})

    async def allow_reauthentication(_user, _password):
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        observed_headers.append(request.headers["Authorization"])
        return httpx.Response(200, request=request, json={"ok": True})

    monkeypatch.setattr(control, "secure_config", store)
    monkeypatch.setattr(control, "audit", AuditStub())
    monkeypatch.setattr(control, "verify_csrf", lambda _request, _user: None)
    monkeypatch.setattr(control, "require_reauthentication", allow_reauthentication)
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/control/settings",
            "headers": [],
            "client": ("127.0.0.1", 50123),
            "server": ("127.0.0.1", 8001),
            "scheme": "http",
        }
    )
    user = {"id": 1, "username": "control-admin"}
    body = control.SettingsBody(
        current_password="test-only-password",  # pragma: allowlist secret
        tmdb_token=token,
        public_base_url="http://127.0.0.1:8000",
        known_proxies="",
        security_log_retention_days=30,
        backup_retention_days=30,
    )

    response = await control.api_settings_update(body, request, user)
    assert response == {"ok": True}
    assert store.tmdb_token() == token
    assert token.encode() not in (tmp_path / "app.db").read_bytes()
    assert token not in str(audit_events)

    public_client = TMDBClient(
        store.tmdb_token,
        tmp_path,
        transport=mock_transport(handler),
    )
    assert await public_client._get("/configuration") == {"ok": True}
    replacement = "tmdb-replacement-" + "f" * 48
    store.set_secret("tmdb_token", replacement, changed_by="control:test")
    assert await public_client._get("/configuration") == {"ok": True}
    assert observed_headers == [f"Bearer {token}", f"Bearer {replacement}"]

    class TestClientStub:
        def __init__(self, candidate, *_args):
            assert candidate == replacement

        async def search(self, _query):
            return [{"tmdb_id": 1}]

    monkeypatch.setattr(control, "TMDBClient", TestClientStub)
    test_response = await control.api_test_tmdb(
        control.TmdbTestBody(current_password="test-only-password", tmdb_token=""),  # pragma: allowlist secret
        request,
        user,
    )
    assert test_response == {"ok": True, "results": 1}
    assert token not in str(test_response)
    assert replacement not in str(test_response)
