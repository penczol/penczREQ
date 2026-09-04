from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from request_app import server
from request_app.config import load_settings
from request_app.http_security import ControlNetworkMiddleware
from request_app.proxy_trust import (
    EFFECTIVE_TRUSTED_PROXIES_ENV,
    ProxyTrustError,
    RuntimeProxyResolution,
    resolve_runtime_proxy,
)


ROUTE_HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
ROOT = Path(__file__).resolve().parents[1]


def route_table(gateway_hex: str, network_hex: str, *, interface: str = "eth0") -> str:
    return (
        ROUTE_HEADER
        + f"{interface}\t00000000\t{gateway_hex}\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        + f"{interface}\t{network_hex}\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    )


def write_route(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "route"
    path.write_text(content, encoding="ascii")
    return path


def public_environment(**overrides: str) -> dict[str, str]:
    result = {
        "PUBLIC_ACCESS_MODE": "reverse-proxy",
        "PUBLIC_TRUSTED_PROXIES": "",
        "FORWARDED_ALLOW_IPS": "",
        "AUTO_TRUST_RUNTIME_GATEWAY": "true",
        "DATA_DIR": "/data",
    }
    result.update(overrides)
    return result


def create_manual_proxy_database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute(
            "INSERT INTO app_settings (key, value) VALUES ('known_proxies', ?)",
            (value,),
        )


def test_recreated_network_uses_current_gateway_without_persisting_it(tmp_path):
    database = tmp_path / "app.db"
    create_manual_proxy_database(database, "192.168.50.20/32")
    before = database.read_bytes()
    environment = public_environment(
        PUBLIC_TRUSTED_PROXIES="10.0.0.9,10.0.0.9/32"
    )
    start_a_route = write_route(tmp_path, route_table("010610AC", "000610AC"))

    start_a = resolve_runtime_proxy(
        "public",
        environ=environment,
        route_path=start_a_route,
        database_path=database,
    )

    assert start_a.manual_trusted_proxies == "10.0.0.9/32,192.168.50.20/32"
    assert start_a.runtime_gateway == "172.16.6.1/32"
    assert start_a.effective_trusted_proxies == (
        "10.0.0.9/32,192.168.50.20/32,172.16.6.1/32"
    )
    assert database.read_bytes() == before

    start_b_route = write_route(tmp_path, route_table("010510AC", "000510AC"))
    start_b = resolve_runtime_proxy(
        "public",
        environ=environment,
        route_path=start_b_route,
        database_path=database,
    )

    assert start_b.manual_trusted_proxies == start_a.manual_trusted_proxies
    assert start_b.runtime_gateway == "172.16.5.1/32"
    assert "172.16.6.1/32" not in start_b.effective_trusted_proxies
    assert start_b.effective_trusted_proxies.endswith("172.16.5.1/32")
    assert database.read_bytes() == before


def test_manual_and_runtime_entries_are_canonicalized_and_deduplicated(tmp_path):
    route = write_route(tmp_path, route_table("010610AC", "000610AC"))
    resolution = resolve_runtime_proxy(
        "control",
        environ={
            "CONTROL_ACCESS_MODE": "reverse-proxy",
            "CONTROL_TRUSTED_PROXIES": "172.16.6.1, 10.0.0.8/32",
            "FORWARDED_ALLOW_IPS": "10.0.0.8",
            "AUTO_TRUST_RUNTIME_GATEWAY": "true",
        },
        route_path=route,
    )

    assert resolution.manual_trusted_proxies == "172.16.6.1/32,10.0.0.8/32"
    assert resolution.effective_trusted_proxies == "172.16.6.1/32,10.0.0.8/32"


def test_32_manual_entries_can_be_combined_with_one_runtime_gateway(tmp_path):
    route = write_route(tmp_path, route_table("010610AC", "000610AC"))
    manual = ",".join(f"10.0.0.{index}/32" for index in range(1, 33))

    resolution = resolve_runtime_proxy(
        "control",
        environ={
            "CONTROL_ACCESS_MODE": "reverse-proxy",
            "CONTROL_TRUSTED_PROXIES": manual,
            "FORWARDED_ALLOW_IPS": "",
            "AUTO_TRUST_RUNTIME_GATEWAY": "true",
        },
        route_path=route,
    )

    assert len(resolution.manual_trusted_proxies.split(",")) == 32
    assert len(resolution.effective_trusted_proxies.split(",")) == 33
    assert resolution.effective_trusted_proxies.endswith("172.16.6.1/32")


@pytest.mark.parametrize(
    ("environment", "expected_auto"),
    (
        (public_environment(AUTO_TRUST_RUNTIME_GATEWAY="false"), False),
        (
            public_environment(
                AUTO_TRUST_RUNTIME_GATEWAY="true", PUBLIC_ACCESS_MODE="lan"
            ),
            True,
        ),
    ),
)
def test_auto_gateway_is_disabled_without_both_opt_in_and_reverse_proxy(
    tmp_path, environment, expected_auto
):
    missing_route = tmp_path / "missing-route"

    resolution = resolve_runtime_proxy(
        "public",
        environ=environment,
        route_path=missing_route,
        database_path=tmp_path / "missing.db",
    )

    assert resolution.auto_trust_enabled is expected_auto
    assert resolution.runtime_gateway == ""
    assert resolution.effective_trusted_proxies == ""
    assert resolution.warnings == ()


@pytest.mark.parametrize(
    "content",
    (
        "",
        ROUTE_HEADER + "eth0\tbroken\n",
        route_table("010610AC", "000510AC"),
        route_table("0100007F", "0000007F"),
        route_table("010000CB", "000000CB"),
        route_table("FFFFFFFF", "000000FF"),
        route_table("010610AC", "000610AC")
        + "eth1\t00000000\t010510AC\t0003\t0\t0\t1\t00000000\t0\t0\t0\n"
        + "eth1\t000510AC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n",
    ),
)
def test_invalid_or_ambiguous_gateway_never_expands_trust(tmp_path, content):
    route = write_route(tmp_path, content)
    resolution = resolve_runtime_proxy(
        "control",
        environ={
            "CONTROL_ACCESS_MODE": "reverse-proxy",
            "CONTROL_TRUSTED_PROXIES": "10.0.0.9/32",
            "FORWARDED_ALLOW_IPS": "",
            "AUTO_TRUST_RUNTIME_GATEWAY": "true",
        },
        route_path=route,
    )

    assert resolution.runtime_gateway == ""
    assert resolution.effective_trusted_proxies == "10.0.0.9/32"
    assert len(resolution.warnings) == 1


def test_missing_route_never_expands_trust_and_emits_warning(tmp_path):
    resolution = resolve_runtime_proxy(
        "control",
        environ={
            "CONTROL_ACCESS_MODE": "reverse-proxy",
            "CONTROL_TRUSTED_PROXIES": "10.0.0.9/32",
            "FORWARDED_ALLOW_IPS": "",
            "AUTO_TRUST_RUNTIME_GATEWAY": "true",
        },
        route_path=tmp_path / "missing",
    )

    assert resolution.effective_trusted_proxies == "10.0.0.9/32"
    assert "odczytać" in resolution.warnings[0]


@pytest.mark.parametrize("value", ("*", "0.0.0.0/0", "not-an-address"))
def test_malformed_or_broad_manual_proxy_fails_closed(tmp_path, value):
    with pytest.raises(ProxyTrustError):
        resolve_runtime_proxy(
            "control",
            environ={
                "CONTROL_ACCESS_MODE": "lan",
                "CONTROL_TRUSTED_PROXIES": value,
                "FORWARDED_ALLOW_IPS": "",
                "AUTO_TRUST_RUNTIME_GATEWAY": "false",
            },
            route_path=tmp_path / "unused",
        )


def test_launcher_supplies_the_same_effective_set_before_uvicorn_configuration(
    monkeypatch, tmp_path
):
    effective = "10.0.0.9/32,172.16.5.1/32"

    def fake_prepare(component):
        assert component == "control"
        monkeypatch.setenv(EFFECTIVE_TRUSTED_PROXIES_ENV, effective)
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", effective)
        return RuntimeProxyResolution(
            component="control",
            access_mode="reverse-proxy",
            auto_trust_enabled=True,
            manual_trusted_proxies="10.0.0.9/32",
            runtime_gateway="172.16.5.1/32",
            effective_trusted_proxies=effective,
        )

    observed = {}

    def fake_uvicorn_run(application, **kwargs):
        observed["application"] = application
        observed.update(kwargs)
        settings = load_settings()
        assert settings.runtime_proxy_resolved is True
        assert settings.control_effective_trusted_proxies == effective
        assert kwargs["forwarded_allow_ips"] == settings.control_effective_trusted_proxies

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_COMPONENT", "control")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path / "control"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CONTROL_ALLOWED_NETWORKS", "10.0.0.0/24")
    monkeypatch.setenv("CONTROL_SESSION_SECRET", "c" * 48)
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "e" * 48)
    monkeypatch.setattr(server, "prepare_runtime_proxy_environment", fake_prepare)
    monkeypatch.setattr(server.uvicorn, "run", fake_uvicorn_run)

    server.run("control", host="0.0.0.0", port=8001)

    assert observed["application"] == "request_app.control:app"
    assert observed["proxy_headers"] is True
    assert observed["server_header"] is False


@pytest.mark.asyncio
async def test_uvicorn_and_control_share_current_gateway_and_reject_old_gateway():
    observed = []

    async def inner(scope, receive, send):
        observed.append((scope["client"][0], scope["scheme"]))

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def ignore_send(_message):
        return None

    effective = "172.16.5.1/32"
    application = ProxyHeadersMiddleware(
        ControlNetworkMiddleware(
            inner,
            "10.0.0.0/24",
            trusted_proxies=effective,
        ),
        trusted_hosts=effective,
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "server": ("control", 8001),
        "headers": [
            (b"host", b"control"),
            (b"x-forwarded-for", b"10.0.0.25"),
            (b"x-forwarded-proto", b"https"),
        ],
    }

    await application(
        {**scope, "client": ("172.16.5.1", 50123)}, receive, ignore_send
    )
    assert observed == [("10.0.0.25", "https")]

    observed.clear()
    sent = []

    async def capture_send(message):
        sent.append(message)

    await application(
        {**scope, "client": ("172.16.6.1", 50123)}, receive, capture_send
    )
    assert observed == []
    assert next(item for item in sent if item["type"] == "http.response.start")[
        "status"
    ] == 403


def test_all_container_start_paths_use_the_runtime_launcher():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))
    truenas_template = yaml.safe_load(
        (ROOT / "deploy" / "truenas" / "compose.yaml.example").read_text(
            encoding="utf-8"
        )
    )

    assert 'CMD ["python", "-m", "request_app.server", "public"' in dockerfile
    for document in (compose, truenas_template):
        assert document["services"]["public"]["command"][:4] == [
            "python",
            "-m",
            "request_app.server",
            "public",
        ]
        assert document["services"]["control"]["command"][:4] == [
            "python",
            "-m",
            "request_app.server",
            "control",
        ]
