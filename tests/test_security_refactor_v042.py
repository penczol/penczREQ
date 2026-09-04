from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from request_app.audit import SecurityAudit
from request_app.auth_protection import LoginProtection
from request_app.config import load_settings
from request_app.control_store import ControlStore
from request_app.database import Database, utc_now
from request_app.http_security import ControlNetworkMiddleware, RequestBodyLimitMiddleware
from request_app.maintenance import create_backup, integrity_report
from request_app.repository import Repository, RepositoryError
from request_app.secure_config import SecureConfigStore
from request_app.security import create_session, hash_password


ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "BezpieczneHaslo2026"  # pragma: allowlist secret
CONTROL_PASSWORD = "KontrolneHaslo2026"  # pragma: allowlist secret


def make_repo(tmp_path) -> Repository:
    db = Database(tmp_path / "app.db")
    db.initialize()
    return Repository(db)


def insert_user(repo: Repository, username: str, role: str = "user") -> int:
    with repo.db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO users
            (username, password_hash, role, is_active, must_change_password, created_at)
            VALUES (?, ?, ?, 1, 0, ?)
            """,
            (username, hash_password(PASSWORD), role, utc_now()),
        )
        return int(cursor.lastrowid)


def test_admin_role_is_preserved_independently_from_username(tmp_path):
    repo = make_repo(tmp_path)
    regular_user = insert_user(repo, "regular-user", "user")
    anna = insert_user(repo, "anna", "admin")
    repo.enforce_roles("someone-else", "")
    assert repo.user_by_id(anna)["role"] == "admin"
    assert repo.user_by_id(regular_user)["role"] == "user"


def test_admin_can_be_promoted_renamed_and_cannot_be_disabled_last(tmp_path):
    repo = make_repo(tmp_path)
    first = insert_user(repo, "first", "admin")
    second = insert_user(repo, "second", "user")
    with pytest.raises(RepositoryError, match="ostatniego"):
        repo.set_user_active(first, False)
    repo.transfer_admin_role(second)
    repo.rename_user(second, "nowy-admin")
    assert repo.user_by_id(second)["role"] == "admin"
    assert repo.user_by_id(second)["username"] == "nowy-admin"
    assert repo.user_by_id(first)["role"] == "user"


def test_password_policy_upgrade_is_one_time_and_revokes_existing_sessions(tmp_path):
    repo = make_repo(tmp_path)
    user_id = insert_user(repo, "existing-user")
    create_session(repo.db, user_id, secret="z" * 48, days=30)
    assert repo.require_password_policy_upgrade() == 1
    assert repo.user_by_id(user_id)["must_change_password"] == 1
    with repo.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert repo.require_password_policy_upgrade() == 0


def test_tmdb_secret_is_encrypted_at_rest(tmp_path):
    repo = make_repo(tmp_path)
    store = SecureConfigStore(repo.db, "m" * 48)
    token = "sekretny-token-tmdb-1234567890"
    store.set_secret("tmdb_token", token, changed_by="test")
    with repo.db.connect() as conn:
        raw = str(conn.execute("SELECT ciphertext FROM app_secrets").fetchone()[0])
    assert token not in raw
    assert store.get_secret("tmdb_token") == token
    history = store.settings_history()
    assert token not in str(history)


def test_proxy_bootstrap_sets_only_an_empty_migrated_setting(tmp_path):
    db = Database(tmp_path / "proxy-bootstrap.db")
    db.initialize()
    store = SecureConfigStore(db, "m" * 48)

    store.initialize(known_proxies="172.31.0.1/32")
    assert store.get_setting("known_proxies") == "172.31.0.1/32"

    store.initialize(known_proxies="172.31.0.2/32")
    assert store.get_setting("known_proxies") == "172.31.0.1/32"


def test_control_has_separate_account_session_and_one_time_recovery(tmp_path):
    store = ControlStore(tmp_path / "control" / "control.db", "s" * 48)
    store.initialize()
    assert store.bootstrap("panel-admin", CONTROL_PASSWORD, development=False) is True
    user = store.authenticate("panel-admin", CONTROL_PASSWORD, "127.0.0.1")
    assert user is not None and user["must_change_password"] == 1
    token, _, _ = store.create_session(user["id"], "127.0.0.1", 8)
    assert store.session_user(token, "127.0.0.1", 20)["username"] == "panel-admin"
    assert store.session_user(token, "127.0.0.2", 20) is None
    recovery_password = "AwaryjneHasloControl2027"  # pragma: allowlist secret
    assert store.apply_recovery("nonce-1", recovery_password) is True
    assert store.apply_recovery("nonce-1", recovery_password) is False
    assert store.authenticate("panel-admin", recovery_password, "127.0.0.1") is not None


def test_control_throttles_are_visible_and_can_be_reset(tmp_path):
    store = ControlStore(tmp_path / "control" / "control.db", "s" * 48)
    store.initialize()
    for _ in range(5):
        retry_after = store.record_failure("192.0.2.44")
    assert retry_after > 0
    items = store.list_throttles()
    assert items[0]["source"] == "control"
    assert items[0]["scope"] == "ip"
    assert items[0]["blocked_until"] is not None
    assert store.reset_throttle("192.0.2.44") is True
    assert store.list_throttles() == []


def test_ip_block_escalates_but_never_becomes_permanent(tmp_path):
    repo = make_repo(tmp_path)
    audit = SecurityAudit(repo.db, tmp_path / "logs")
    protection = LoginProtection(repo.db, audit)
    state = None
    for cycle in range(3):
        for _ in range(10):
            state = protection.record_failure("2001:db8::1234", "unknown")
        assert state and state.blocked
        assert state.retry_after <= 24 * 3600
        with repo.db.transaction() as conn:
            conn.execute(
                """
                UPDATE auth_throttles SET blocked_until = ?, failures = 9
                WHERE scope = 'ip'
                """,
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )
    ip_row = next(item for item in protection.list_states() if item["scope"] == "ip")
    assert ip_row["block_level"] == 3
    assert ip_row["blocked_until"] is not None


def test_login_attack_does_not_delete_existing_session(tmp_path):
    repo = make_repo(tmp_path)
    user_id = insert_user(repo, "anna")
    create_session(repo.db, user_id, secret="z" * 48, days=30)
    protection = LoginProtection(repo.db, SecurityAudit(repo.db, tmp_path / "logs"))
    for _ in range(10):
        protection.record_failure("127.0.0.1", "anna")
    with repo.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)).fetchone()[0] == 1


def test_legacy_automatic_lockouts_are_migrated_once(tmp_path):
    repo = make_repo(tmp_path)
    user_id = insert_user(repo, "anna")
    with repo.db.transaction() as conn:
        conn.execute(
            "UPDATE users SET security_locked = 1, lockout_cycles = 3, temporary_lock_until = ? WHERE id = ?",
            ((datetime.now(UTC) + timedelta(days=1)).isoformat(), user_id),
        )
    protection = LoginProtection(repo.db, SecurityAudit(repo.db, tmp_path / "logs"))
    assert protection.migrate_legacy_lockouts() == 1
    assert protection.migrate_legacy_lockouts() == 0
    user = repo.user_by_id(user_id)
    assert user["security_locked"] == 0 and user["temporary_lock_until"] is None


def test_online_backup_covers_app_and_control_databases(tmp_path):
    repo = make_repo(tmp_path)
    insert_user(repo, "admin", "admin")
    control = ControlStore(tmp_path / "control" / "control.db", "s" * 48)
    control.initialize()
    control.bootstrap("panel-admin", CONTROL_PASSWORD, development=False)
    result = create_backup(
        repo.db, control.path, tmp_path / "backups", retention_days=30
    )
    assert set(result) == {"app", "control"}
    assert integrity_report(repo.db, control.path) == {"app": "ok", "control": "ok"}


def test_public_surface_no_longer_exposes_sensitive_admin_or_diagnostics():
    main = (ROOT / "request_app" / "main.py").read_text(encoding="utf-8")
    index = (ROOT / "request_app" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "openapi_url=None" in main.replace(" ", "")
    assert 'app.mount("/posters"' not in main
    assert '@app.get("/internal/health"' in main
    assert '@app.get("/health"' not in main
    assert "/api/admin/users" not in main
    assert "/api/admin/settings" not in main
    assert "window.REQUEST_APP" not in index


def test_control_surface_has_separate_network_cookie_and_csrf_guards():
    control = (ROOT / "request_app" / "control.py").read_text(encoding="utf-8")
    assert "ControlNetworkMiddleware" in control
    assert "settings.control_cookie_name" in control
    assert "verify_csrf(request, user)" in control
    assert "require_reauthentication" in control
    assert "openapi_url=None" in control.replace(" ", "")


def test_production_reverse_proxy_configuration_fails_closed_without_https(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_COMPONENT", "public")
    monkeypatch.setenv("PUBLIC_ACCESS_MODE", "reverse-proxy")
    monkeypatch.setenv("APP_BASE_URL", "http://example.invalid")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("CONTROL_SESSION_SECRET", "c" * 48)
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "e" * 48)
    monkeypatch.setenv("CONTROL_ALLOWED_NETWORKS", "10.0.0.0/24")
    monkeypatch.setenv("CONTROL_ALLOWED_HOSTS", "control.example.invalid")
    monkeypatch.setenv("ALLOWED_HOSTS", "example.invalid")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    with pytest.raises(RuntimeError, match="HTTPS"):
        load_settings()


@pytest.mark.asyncio
async def test_control_network_boundary_rejects_non_lan_peer_before_app():
    called = False

    async def inner(scope, receive, send):
        nonlocal called
        called = True

    middleware = ControlNetworkMiddleware(inner, "127.0.0.0/8,10.0.0.0/24")
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(
        {
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/login", "raw_path": b"/login", "query_string": b"",
            "headers": [(b"host", b"testserver")], "client": ("203.0.113.9", 50123),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    assert called is False
    assert next(item for item in sent if item["type"] == "http.response.start")["status"] == 403


@pytest.mark.asyncio
async def test_streamed_body_without_content_length_gets_explicit_413():
    called = False

    async def inner(scope, receive, send):
        nonlocal called
        called = True

    middleware = RequestBodyLimitMiddleware(inner, max_bytes=16)
    chunks = iter([
        {"type": "http.request", "body": b"1234567890", "more_body": True},
        {"type": "http.request", "body": b"abcdefghij", "more_body": False},
    ])
    sent = []

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    await middleware(
        {
            "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/login", "raw_path": b"/login", "query_string": b"",
            "headers": [(b"host", b"testserver"), (b"transfer-encoding", b"chunked")],
            "client": ("127.0.0.1", 50123), "server": ("testserver", 80),
        },
        receive,
        send,
    )
    assert called is False
    assert next(item for item in sent if item["type"] == "http.response.start")["status"] == 413
