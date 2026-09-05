from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from request_app.config import load_settings
from request_app.http_security import ControlNetworkMiddleware


ROOT = Path(__file__).resolve().parents[1]


def configure_production(monkeypatch, tmp_path: Path, component: str) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_COMPONENT", component)
    monkeypatch.setenv("APP_BASE_URL", "https://penczreq.example")
    monkeypatch.setenv("PUBLIC_ACCESS_MODE", "reverse-proxy")
    monkeypatch.setenv("CONTROL_ACCESS_MODE", "lan")
    monkeypatch.setenv("CONTROL_BASE_URL", "http://192.0.2.10:18001")
    monkeypatch.setenv("COOKIE_SECURE", "false" if component == "control" else "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path / "control"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "app" / "logs"))
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "e" * 48)
    monkeypatch.setenv("ALLOWED_HOSTS", "penczreq.example,127.0.0.1")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.invalid")


def test_public_component_does_not_require_or_create_control_storage(monkeypatch, tmp_path):
    configure_production(monkeypatch, tmp_path, "public")
    monkeypatch.setenv("SESSION_SECRET", "s" * 48)
    monkeypatch.delenv("CONTROL_SESSION_SECRET", raising=False)
    monkeypatch.delenv("CONTROL_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("CONTROL_ALLOWED_NETWORKS", raising=False)

    settings = load_settings()

    assert settings.app_component == "public"
    assert settings.control_session_secret == ""
    assert not settings.control_data_dir.exists()
    assert not settings.backup_dir.exists()
    assert (settings.data_dir / "posters").is_dir()


def test_control_component_has_own_secret_and_storage(monkeypatch, tmp_path):
    configure_production(monkeypatch, tmp_path, "control")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("CONTROL_SESSION_SECRET", "c" * 48)
    monkeypatch.setenv("CONTROL_ALLOWED_HOSTS", "control.example.internal,127.0.0.1")
    monkeypatch.setenv("CONTROL_ALLOWED_NETWORKS", "127.0.0.0/8,10.0.0.0/24")
    monkeypatch.setenv("CONTROL_TRUSTED_PROXIES", "172.20.0.0/24")

    settings = load_settings()

    assert settings.app_component == "control"
    assert settings.session_secret == ""
    assert settings.control_database_path == tmp_path / "control" / "control.db"
    assert settings.control_data_dir.is_dir()
    assert settings.backups_dir.is_dir()


def test_production_access_modes_validate_scheme_and_cookie_policy(monkeypatch, tmp_path):
    configure_production(monkeypatch, tmp_path, "public")
    monkeypatch.setenv("SESSION_SECRET", "s" * 48)

    reverse_proxy = load_settings()
    assert reverse_proxy.public_access_mode == "reverse-proxy"
    assert reverse_proxy.cookie_secure is True

    monkeypatch.setenv("PUBLIC_ACCESS_MODE", "lan")
    monkeypatch.setenv("APP_BASE_URL", "http://192.0.2.10:18000")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    lan = load_settings()
    assert lan.public_access_mode == "lan"
    assert lan.cookie_secure is False

    monkeypatch.setenv("COOKIE_SECURE", "true")
    with pytest.raises(RuntimeError, match="COOKIE_SECURE=false"):
        load_settings()

    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("APP_BASE_URL", "http://192.0.2.10:18000/subpath")
    with pytest.raises(RuntimeError, match="dodatkowej ścieżki"):
        load_settings()


def test_control_lan_and_reverse_proxy_modes_have_separate_contracts(monkeypatch, tmp_path):
    configure_production(monkeypatch, tmp_path, "control")
    monkeypatch.setenv("CONTROL_SESSION_SECRET", "c" * 48)
    monkeypatch.setenv("CONTROL_ALLOWED_HOSTS", "192.0.2.10")
    monkeypatch.setenv("CONTROL_ALLOWED_NETWORKS", "192.0.2.0/24")

    lan = load_settings()
    assert lan.control_access_mode == "lan"
    assert lan.control_base_url == "http://192.0.2.10:18001"
    assert "192.0.2.10" in lan.control_allowed_hosts

    monkeypatch.setenv("CONTROL_ACCESS_MODE", "reverse-proxy")
    monkeypatch.setenv("CONTROL_BASE_URL", "https://control.example.internal")
    monkeypatch.setenv("CONTROL_ALLOWED_HOSTS", "control.example.internal")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    proxied = load_settings()
    assert proxied.control_access_mode == "reverse-proxy"
    assert proxied.control_base_url == "https://control.example.internal"


def test_static_proxy_networks_are_normalized_and_reject_global_wildcards(monkeypatch, tmp_path):
    configure_production(monkeypatch, tmp_path, "public")
    monkeypatch.setenv("SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("PUBLIC_TRUSTED_PROXIES", "172.31.0.1, 172.31.0.1/32")

    settings = load_settings()
    assert settings.public_trusted_proxies == "172.31.0.1/32"

    monkeypatch.setenv("PUBLIC_TRUSTED_PROXIES", "0.0.0.0/0")
    with pytest.raises(RuntimeError, match=r"\(/0\)"):
        load_settings()


@pytest.mark.asyncio
async def test_control_proxy_chain_is_trusted_only_from_declared_proxy():
    observed = []

    async def inner(scope, receive, send):
        observed.append(scope["client"][0])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        return None

    middleware = ControlNetworkMiddleware(
        inner,
        "10.0.0.0/24",
        trusted_proxies="172.20.0.0/24",
    )
    base_scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "server": ("control", 443),
    }

    await middleware(
        {
            **base_scope,
            "headers": [(b"host", b"control"), (b"x-forwarded-for", b"10.0.0.25")],
            "client": ("172.20.0.8", 50123),
        },
        receive,
        send,
    )
    assert observed == ["10.0.0.25"]

    observed.clear()
    sent = []

    async def capture(message):
        sent.append(message)

    await middleware(
        {
            **base_scope,
            "headers": [(b"host", b"control"), (b"x-forwarded-for", b"10.0.0.25")],
            "client": ("203.0.113.9", 50123),
        },
        receive,
        capture,
    )
    assert observed == []
    assert next(item for item in sent if item["type"] == "http.response.start")["status"] == 403


@pytest.mark.asyncio
async def test_uvicorn_forwarded_boundary_and_control_network_reject_spoofed_peer():
    observed = []

    async def inner(scope, receive, send):
        observed.append(scope["client"][0])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        return None

    app = ProxyHeadersMiddleware(
        ControlNetworkMiddleware(inner, "10.10.0.0/16"),
        trusted_hosts="172.31.0.1",
    )
    base_scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "server": ("control", 18001),
        "headers": [(b"host", b"control"), (b"x-forwarded-for", b"10.10.2.25")],
    }

    await app({**base_scope, "client": ("172.31.0.1", 50123)}, receive, send)
    assert observed == ["10.10.2.25"]

    observed.clear()
    sent = []

    async def capture(message):
        sent.append(message)

    await app({**base_scope, "client": ("203.0.113.8", 50123)}, receive, capture)
    assert observed == []
    assert next(item for item in sent if item["type"] == "http.response.start")["status"] == 403


def test_public_process_has_no_control_database_or_backup_access():
    main = (ROOT / "request_app" / "main.py").read_text(encoding="utf-8")
    control = (ROOT / "request_app" / "control.py").read_text(encoding="utf-8")

    assert "settings.control_database_path" not in main
    assert "create_backup" not in main
    assert "backup_maintenance_loop" in control
    assert "settings.control_database_path" in control


def test_compose_is_two_service_least_privilege_deployment():
    compose = yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))
    public = compose["services"]["public"]
    control = compose["services"]["control"]

    assert set(compose["services"]) == {"public", "control"}
    assert public["read_only"] is True
    assert control["read_only"] is True
    assert public["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in public["security_opt"]
    assert {volume["target"] for volume in public["volumes"]} == {"/data"}
    assert {volume["target"] for volume in control["volumes"]} == {
        "/data",
        "/control-data",
        "/backups",
    }
    assert "CONTROL_SESSION_SECRET" not in public["environment"]
    assert "SESSION_SECRET" not in control["environment"]
    assert "CONTROL_SESSION_HOURS" in control["environment"]
    assert "CONTROL_IDLE_MINUTES" in control["environment"]
    assert public["environment"]["SESSION_DAYS"] == "${SESSION_DAYS:-180}"
    assert public["environment"]["SESSION_IDLE_MINUTES"] == "${SESSION_IDLE_MINUTES:-43200}"
    assert public["depends_on"]["control"]["condition"] == "service_healthy"


def test_truenas_template_preserves_security_boundaries():
    compose = yaml.safe_load(
        (ROOT / "deploy" / "truenas" / "compose.yaml.example").read_text(encoding="utf-8")
    )
    public = compose["services"]["public"]
    control = compose["services"]["control"]

    assert public["read_only"] is True
    assert control["read_only"] is True
    assert {volume["target"] for volume in public["volumes"]} == {"/data"}
    assert {volume["target"] for volume in control["volumes"]} == {
        "/data",
        "/control-data",
        "/backups",
    }
    assert "environment" not in public
    assert "environment" not in control
    assert public["env_file"] == ["REPLACE_APP_ROOT/public.env"]
    assert control["env_file"] == ["REPLACE_APP_ROOT/control.env"]
    assert public["ports"] == ["REPLACE_BIND_IP:REPLACE_PUBLIC_PORT:8000"]
    assert control["ports"] == ["REPLACE_BIND_IP:REPLACE_CONTROL_PORT:8001"]
    assert {portal["name"] for portal in compose["x-portals"]} == {"Public", "Control"}


def test_image_runs_as_unprivileged_user_with_pinned_runtime():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert (
        "FROM python:3.12.14-slim-trixie@sha256:"
        "78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"  # pragma: allowlist secret
    ) in dockerfile
    assert "USER 568:568" in dockerfile
    assert "HEALTHCHECK " in dockerfile
    assert "VOLUME " not in dockerfile
    assert "pip install --no-cache-dir --requirement requirements.lock" in dockerfile
    assert "site-packages/pip" in dockerfile
    assert "release" in dockerignore.splitlines()
    assert "fastapi==0.139.2" in requirements
    assert "cryptography==50.0.0" in requirements
