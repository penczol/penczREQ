from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import re
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "deploy" / "truenas" / "installer.py"
TEMPLATE_PATH = ROOT / "deploy" / "truenas" / "compose.yaml.example"

SPEC = importlib.util.spec_from_file_location("penczreq_truenas_installer", INSTALLER_PATH)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)

BUILDER_PATH = ROOT / "tools" / "build_installer.py"
BUILDER_SPEC = importlib.util.spec_from_file_location("penczreq_installer_builder", BUILDER_PATH)
assert BUILDER_SPEC and BUILDER_SPEC.loader
builder = importlib.util.module_from_spec(BUILDER_SPEC)
sys.modules[BUILDER_SPEC.name] = builder
BUILDER_SPEC.loader.exec_module(builder)


def answers(**overrides):
    result = {
        "mode": "fresh",
        "app_name": "penczreq",
        "image": "ghcr.io/example/penczreq:0.5.2",
        "root_dataset": "tank/apps/penczreq",
        "nas_ip": "192.168.50.10",
        "public_port": 18000,
        "control_port": 18001,
        "public_access_mode": "lan",
        "public_url": "http://192.168.50.10:18000",
        "control_access_mode": "lan",
        "control_url": "http://192.168.50.10:18001",
        "control_allowed_networks": "192.168.50.0/24",
        "timezone": "Europe/Warsaw",
        "vapid_subject": "mailto:webpush@example.invalid",
        "public_admin_username": "admin",
        "control_admin_username": "control-admin",
    }
    result.update(overrides)
    return result


@pytest.fixture
def no_root_chown(monkeypatch):
    monkeypatch.setattr(installer.os, "chown", lambda *_args: None, raising=False)


def stub_execute_lifecycle(
    monkeypatch,
    tmp_path,
    config,
    *,
    initial_state: str,
    start_fails: bool = False,
):
    events = []
    runtime = {
        "state": initial_state,
        "network_exists": initial_state == "RUNNING",
    }
    existing = (
        None
        if config.mode == "fresh"
        else {"name": config.app_name, "custom_app": True, "state": initial_state}
    )
    mutation_method = "app.create" if config.mode == "fresh" else "app.update"

    monkeypatch.setattr(
        installer.InstallConfig, "root_path", property(lambda self: tmp_path)
    )
    monkeypatch.setattr(installer, "_ensure_truenas_runtime", lambda: None)
    monkeypatch.setattr(installer, "_validate_app_target", lambda _config: existing)
    monkeypatch.setattr(installer, "_ensure_datasets", lambda _config: None)
    monkeypatch.setattr(
        installer, "_prepare_upgrade_rollback", lambda _config: {"snapshot": "test"}
    )
    monkeypatch.setattr(installer, "_upgrade_files", lambda _config: None)
    monkeypatch.setattr(installer, "_fresh_files", lambda _config: None)
    monkeypatch.setattr(
        installer,
        "_run",
        lambda _command, **_kwargs: SimpleNamespace(stdout="", returncode=0),
    )

    def fake_midclt(method, *arguments, job=False):
        if method == mutation_method:
            assert job is True
            events.append(method)
            return ""
        if method == "app.get_instance":
            assert arguments == (config.app_name,)
            assert job is False
            events.append(f"state:{runtime['state']}")
            return json.dumps({"name": config.app_name, "state": runtime["state"]})
        if method == "app.start":
            assert arguments == (config.app_name,)
            assert job is True
            events.append(method)
            if start_fails:
                raise installer.subprocess.CalledProcessError(
                    1, ["midclt", "call", "--job", "app.start", config.app_name]
                )
            runtime["state"] = "RUNNING"
            runtime["network_exists"] = True
            return ""
        raise AssertionError(f"Unexpected middleware call: {method}")

    def fake_database_gate(_config):
        assert runtime["state"] == "RUNNING"
        events.append("databases")
        return {"app": {"quick_check": "ok"}, "control": {"quick_check": "ok"}}

    monkeypatch.setattr(installer, "_midclt", fake_midclt)
    monkeypatch.setattr(installer, "_wait_for_post_start_databases", fake_database_gate)
    return events


def test_lan_config_renders_two_portals_and_secret_free_compose():
    config = installer.InstallConfig.from_mapping(answers())
    rendered = installer.render_compose(TEMPLATE_PATH.read_text(encoding="utf-8"), config)
    compose = yaml.safe_load(rendered)

    assert compose["x-portals"] == [
        {
            "name": "Public",
            "scheme": "http",
            "host": "192.168.50.10",
            "port": 18000,
            "path": "/",
        },
        {
            "name": "Control",
            "scheme": "http",
            "host": "192.168.50.10",
            "port": 18001,
            "path": "/",
        },
    ]
    assert compose["services"]["public"]["env_file"] == [
        "/mnt/tank/apps/penczreq/public.env"
    ]
    assert compose["services"]["control"]["env_file"] == [
        "/mnt/tank/apps/penczreq/control.env"
    ]
    assert "environment" not in compose["services"]["public"]
    assert "environment" not in compose["services"]["control"]
    assert "pull_policy" not in compose["services"]["public"]
    assert "pull_policy" not in compose["services"]["control"]
    assert not any(secret in rendered for secret in installer.SECRET_KEYS)
    assert "REPLACE_" not in rendered


def test_local_image_mode_sets_fail_closed_pull_policy_for_both_services():
    config = installer.InstallConfig.from_mapping(answers())
    rendered = installer.render_compose(
        TEMPLATE_PATH.read_text(encoding="utf-8"), config, local_image=True
    )
    compose = yaml.safe_load(rendered)

    assert compose["services"]["public"]["pull_policy"] == "never"
    assert compose["services"]["control"]["pull_policy"] == "never"
    assert "REPLACE_" not in rendered


def test_local_image_cli_is_explicit_and_rejects_values():
    assert installer.parse_args([]).local_image is False
    assert installer.parse_args(["--local-image"]).local_image is True
    with pytest.raises(SystemExit):
        installer.parse_args(["--local-image=auto"])


def test_midclt_builds_exact_argv_for_regular_and_job_calls(monkeypatch):
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="result\n", returncode=0)

    monkeypatch.setattr(installer, "_run", fake_run)

    assert installer._midclt("app.query", [["name", "=", "penczreq"]]) == "result"
    assert (
        installer._midclt(
            "app.create",
            {"app_name": "penczreq", "custom_compose_config_string": "x"},
            job=True,
        )
        == "result"
    )
    assert installer._midclt("app.start", "penczreq", job=True) == "result"

    assert commands == [
        ["midclt", "call", "app.query", '[["name","=","penczreq"]]'],
        [
            "midclt",
            "call",
            "--job",
            "app.create",
            '{"app_name":"penczreq","custom_compose_config_string":"x"}',
        ],
        ["midclt", "call", "--job", "app.start", '"penczreq"'],
    ]
    assert all("-job" not in command for command in commands)


@pytest.mark.parametrize("local_image", (False, True))
@pytest.mark.parametrize(
    ("service", "url"),
    (
        ("public", "http://127.0.0.1:8000/internal/health"),
        ("control", "http://127.0.0.1:8001/internal/health"),
    ),
)
def test_rendered_healthchecks_use_only_plain_loopback_urls(
    service, url, local_image
):
    config = installer.InstallConfig.from_mapping(answers())
    rendered = installer.render_compose(
        TEMPLATE_PATH.read_text(encoding="utf-8"), config, local_image=local_image
    )
    healthcheck = yaml.safe_load(rendered)["services"][service]["healthcheck"]

    assert healthcheck["test"] == [
        "CMD",
        "python",
        "-c",
        f'import urllib.request; urllib.request.urlopen("{url}", timeout=3)',
    ]
    assert "](" not in healthcheck["test"][-1]


def test_access_modes_drive_cookie_proxy_and_forwarded_contracts():
    config = installer.InstallConfig.from_mapping(
        answers(
            public_access_mode="reverse-proxy",
            public_url="https://requests.example.com",
            control_access_mode="reverse-proxy",
            control_url="https://control.example.internal",
        )
    )
    generated = installer.generate_fresh_secrets()
    public, control = installer.environment_values(config, generated)

    assert public["COOKIE_SECURE"] == "true"
    assert public["PUBLIC_TRUSTED_PROXIES"] == ""
    assert public["FORWARDED_ALLOW_IPS"] == ""
    assert public["AUTO_TRUST_RUNTIME_GATEWAY"] == "true"
    assert control["COOKIE_SECURE"] == "true"
    assert control["CONTROL_TRUSTED_PROXIES"] == ""
    assert control["FORWARDED_ALLOW_IPS"] == ""
    assert control["AUTO_TRUST_RUNTIME_GATEWAY"] == "true"
    assert public["SESSION_DAYS"] == "180"
    assert public["SESSION_IDLE_MINUTES"] == "43200"
    assert control["CONTROL_SESSION_HOURS"] == "8"
    assert control["CONTROL_IDLE_MINUTES"] == "20"


def test_fresh_secrets_are_distinct_and_bootstrap_passwords_follow_policy():
    generated = installer.generate_fresh_secrets()
    values = list(generated.values())

    assert len(values) == len(set(values))
    for key in ("SESSION_SECRET", "CONTROL_SESSION_SECRET", "CONFIG_ENCRYPTION_KEY"):
        assert len(generated[key]) >= 32
    for key in ("PUBLIC_ADMIN_BOOTSTRAP_PASSWORD", "CONTROL_BOOTSTRAP_PASSWORD"):
        password = generated[key]
        assert 15 <= len(password) <= 128
        assert password.isascii()
        assert any(character.islower() for character in password)
        assert any(character.isupper() for character in password)
        assert any(character.isdigit() for character in password)


@pytest.mark.parametrize(
    "changes",
    (
        {"public_access_mode": "lan", "public_url": "https://requests.example.com"},
        {"control_access_mode": "reverse-proxy", "control_url": "http://192.168.50.10:18001"},
        {"control_allowed_networks": "0.0.0.0/0"},
        {"control_allowed_networks": "8.8.8.0/24"},
        {"image": "ghcr.io/example/penczreq:latest"},
        {"root_dataset": "../unsafe"},
        {"public_port": 18001},
    ),
)
def test_invalid_or_public_exposure_answers_are_rejected(changes):
    with pytest.raises(installer.InstallerError):
        installer.InstallConfig.from_mapping(answers(**changes))


def test_dry_run_writes_only_redacted_examples_and_performs_no_mutation(tmp_path):
    config = installer.InstallConfig.from_mapping(answers())
    summary = installer.dry_run(
        config,
        TEMPLATE_PATH.read_text(encoding="utf-8"),
        tmp_path,
    )

    assert summary["secrets_generated"] is False
    assert summary["mutations_performed"] is False
    assert summary["local_image"] is False
    assert yaml.safe_load((tmp_path / "compose.yaml").read_text(encoding="utf-8"))
    public_env = (tmp_path / "public.env.example").read_text(encoding="utf-8")
    control_env = (tmp_path / "control.env.example").read_text(encoding="utf-8")
    assert "SESSION_SECRET=<generated-during-execution>" in public_env
    assert "CONTROL_SESSION_SECRET=<generated-during-execution>" in control_env
    assert "GeneratedPublicPassword2026" not in public_env + control_env
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))[
        "dry_run"
    ] is True


def test_local_image_dry_run_renders_never_without_generating_secrets(tmp_path):
    config = installer.InstallConfig.from_mapping(answers())
    summary = installer.dry_run(
        config,
        TEMPLATE_PATH.read_text(encoding="utf-8"),
        tmp_path,
        local_image=True,
    )
    compose = yaml.safe_load((tmp_path / "compose.yaml").read_text(encoding="utf-8"))

    assert summary["local_image"] is True
    assert summary["secrets_generated"] is False
    assert summary["mutations_performed"] is False
    assert compose["services"]["public"]["pull_policy"] == "never"
    assert compose["services"]["control"]["pull_policy"] == "never"


def test_upgrade_env_update_preserves_existing_secrets(tmp_path):
    path = tmp_path / "public.env"
    original_secret = "s" * 48
    path.write_text(
        f"SESSION_SECRET={original_secret}\nAPP_BASE_URL=https://old.example\n",
        encoding="utf-8",
    )

    installer._update_env(
        path,
        {
            "APP_BASE_URL": "https://new.example",
            "PUBLIC_ACCESS_MODE": "reverse-proxy",
        },
    )

    _, values = installer._read_env(path)
    assert values["SESSION_SECRET"] == original_secret
    assert values["APP_BASE_URL"] == "https://new.example"
    assert values["PUBLIC_ACCESS_MODE"] == "reverse-proxy"


def test_generic_atomic_write_keeps_ownership_opt_in(monkeypatch, tmp_path):
    metadata = []

    def fake_metadata(path, *, mode, ownership=None):
        metadata.append((path.name, mode, ownership))

    monkeypatch.setattr(installer, "_set_file_metadata", fake_metadata)
    path = tmp_path / "generic.json"
    installer._atomic_write(path, "{}\n")

    assert path.read_text(encoding="utf-8") == "{}\n"
    assert metadata == [
        (f".generic.json.{os.getpid()}.tmp", 0o600, None),
        ("generic.json", 0o600, None),
    ]


def test_explicit_ownership_is_applied_before_mode_on_posix(monkeypatch, tmp_path):
    calls = []
    path = tmp_path / "public.env"
    path.write_text("fixture\n", encoding="utf-8")

    monkeypatch.setattr(installer.os, "name", "posix")
    monkeypatch.setattr(
        installer.os,
        "chown",
        lambda target, uid, gid: calls.append(("chown", target.name, uid, gid)),
        raising=False,
    )
    monkeypatch.setattr(
        installer.os,
        "chmod",
        lambda target, mode: calls.append(("chmod", target.name, mode)),
    )

    installer._set_file_metadata(path, mode=0o600, ownership=(0, 0))

    assert calls == [
        ("chown", "public.env", 0, 0),
        ("chmod", "public.env", 0o600),
    ]


def test_fresh_env_files_request_root_ownership_without_affecting_credentials(
    monkeypatch, tmp_path
):
    config = installer.InstallConfig.from_mapping(answers())
    monkeypatch.setattr(installer.InstallConfig, "root_path", property(lambda self: tmp_path))
    for directory in ("app", "control", "backups"):
        (tmp_path / directory).mkdir()
    metadata = []

    def fake_metadata(path, *, mode, ownership=None):
        metadata.append((path.name, mode, ownership))

    monkeypatch.setattr(installer, "_set_file_metadata", fake_metadata)
    installer._fresh_files(config)

    final_metadata = {
        name: (mode, ownership)
        for name, mode, ownership in metadata
        if not name.startswith(".")
    }
    assert final_metadata["public.env"] == (0o600, (0, 0))
    assert final_metadata["control.env"] == (0o600, (0, 0))
    assert final_metadata["bootstrap-credentials.txt"] == (0o600, None)


def test_upgrade_repairs_env_metadata_without_rewriting_or_rotating_secrets(
    monkeypatch, tmp_path
):
    config = installer.InstallConfig.from_mapping(answers(mode="upgrade"))
    monkeypatch.setattr(installer.InstallConfig, "root_path", property(lambda self: tmp_path))
    generated = {
        "SESSION_SECRET": "s" * 48,
        "CONTROL_SESSION_SECRET": "c" * 48,
        "CONFIG_ENCRYPTION_KEY": "e" * 48,
    }
    public, control = installer.environment_values(config, generated)
    public_path = tmp_path / "public.env"
    control_path = tmp_path / "control.env"
    public_path.write_text(installer.render_env(public), encoding="utf-8", newline="\n")
    control_path.write_text(installer.render_env(control), encoding="utf-8", newline="\n")
    before = {path.name: path.read_bytes() for path in (public_path, control_path)}
    metadata = []

    def fake_metadata(path, *, mode, ownership=None):
        metadata.append((path.name, mode, ownership))

    def unexpected_atomic_write(*_args, **_kwargs):
        raise AssertionError("metadata-only repair must not replace unchanged env content")

    monkeypatch.setattr(installer, "_set_file_metadata", fake_metadata)
    monkeypatch.setattr(installer, "_atomic_write", unexpected_atomic_write)
    installer._upgrade_files(config)

    assert {path.name: path.read_bytes() for path in (public_path, control_path)} == before
    assert metadata == [
        ("public.env", 0o600, (0, 0)),
        ("control.env", 0o600, (0, 0)),
    ]


@pytest.mark.usefixtures("no_root_chown")
def test_upgrade_preserves_all_legacy_manual_proxy_values(monkeypatch, tmp_path):
    config = installer.InstallConfig.from_mapping(
        answers(
            mode="upgrade",
            public_access_mode="reverse-proxy",
            public_url="https://requests.example.com",
            control_access_mode="reverse-proxy",
            control_url="https://control.example.internal",
        )
    )
    monkeypatch.setattr(installer.InstallConfig, "root_path", property(lambda self: tmp_path))
    shared_key = "e" * 48
    public_path = tmp_path / "public.env"
    control_path = tmp_path / "control.env"
    public_path.write_text(
        "\n".join(
            (
                f"SESSION_SECRET={'s' * 48}",
                f"CONFIG_ENCRYPTION_KEY={shared_key}",
                "PUBLIC_TRUSTED_PROXIES=172.16.6.1/32,10.0.0.9/32",
                "FORWARDED_ALLOW_IPS=172.16.6.1/32",
                "",
            )
        ),
        encoding="utf-8",
    )
    control_path.write_text(
        "\n".join(
            (
                f"CONTROL_SESSION_SECRET={'c' * 48}",
                f"CONFIG_ENCRYPTION_KEY={shared_key}",
                "PUBLIC_TRUSTED_PROXIES=172.16.6.1/32",
                "CONTROL_TRUSTED_PROXIES=172.16.6.1/32,10.0.0.10/32",
                "FORWARDED_ALLOW_IPS=172.16.6.1/32",
                "",
            )
        ),
        encoding="utf-8",
    )

    installer._upgrade_files(config)

    _, public = installer._read_env(public_path)
    _, control = installer._read_env(control_path)
    assert public["PUBLIC_TRUSTED_PROXIES"] == "172.16.6.1/32,10.0.0.9/32"
    assert public["FORWARDED_ALLOW_IPS"] == "172.16.6.1/32"
    assert control["PUBLIC_TRUSTED_PROXIES"] == "172.16.6.1/32"
    assert control["CONTROL_TRUSTED_PROXIES"] == "172.16.6.1/32,10.0.0.10/32"
    assert control["FORWARDED_ALLOW_IPS"] == "172.16.6.1/32"
    assert public["AUTO_TRUST_RUNTIME_GATEWAY"] == "true"
    assert control["AUTO_TRUST_RUNTIME_GATEWAY"] == "true"


def test_env_content_update_forwards_explicit_root_ownership(monkeypatch, tmp_path):
    path = tmp_path / "public.env"
    path.write_text("PUBLIC_PORT=18000\n", encoding="utf-8")
    writes = []

    def fake_atomic_write(target, content, mode=0o600, *, ownership=None):
        writes.append((target, content, mode, ownership))

    monkeypatch.setattr(installer, "_atomic_write", fake_atomic_write)
    installer._update_env(
        path,
        {"PUBLIC_PORT": "28000"},
        ownership=installer.ROOT_FILE_OWNERSHIP,
    )

    assert writes == [(path, "PUBLIC_PORT=28000\n", 0o600, (0, 0))]


@pytest.mark.usefixtures("no_root_chown")
def test_fresh_files_create_separate_0600_env_and_refuse_reuse(monkeypatch, tmp_path):
    config = installer.InstallConfig.from_mapping(answers())
    monkeypatch.setattr(installer.InstallConfig, "root_path", property(lambda self: tmp_path))
    (tmp_path / "app").mkdir()
    (tmp_path / "control").mkdir()
    (tmp_path / "backups").mkdir()

    installer._fresh_files(config)

    public_path = tmp_path / "public.env"
    control_path = tmp_path / "control.env"
    _, public = installer._read_env(public_path)
    _, control = installer._read_env(control_path)
    assert public["SESSION_SECRET"] != control["CONTROL_SESSION_SECRET"]
    assert public["CONFIG_ENCRYPTION_KEY"] == control["CONFIG_ENCRYPTION_KEY"]
    if os.name == "posix":
        assert public_path.stat().st_mode & 0o777 == 0o600
        assert control_path.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "bootstrap-credentials.txt").stat().st_mode & 0o777 == 0o600
    with pytest.raises(installer.InstallerError, match="Fresh install odmawia"):
        installer._fresh_files(config)


@pytest.mark.usefixtures("no_root_chown")
def test_upgrade_preserves_secrets_and_creates_checked_pair_backup_and_snapshot(
    monkeypatch, tmp_path
):
    config = installer.InstallConfig.from_mapping(answers(mode="upgrade"))
    monkeypatch.setattr(installer.InstallConfig, "root_path", property(lambda self: tmp_path))
    for directory in ("app", "control", "backups"):
        (tmp_path / directory).mkdir()
    for relative, value in (("app/app.db", "public-row"), ("control/control.db", "control-row")):
        with installer.sqlite3.connect(tmp_path / relative) as connection:
            connection.execute("CREATE TABLE preserved (value TEXT NOT NULL)")
            connection.execute("INSERT INTO preserved VALUES (?)", (value,))

    shared_key = "e" * 48
    (tmp_path / "public.env").write_text(
        f"SESSION_SECRET={'s' * 48}\nCONFIG_ENCRYPTION_KEY={shared_key}\n",
        encoding="utf-8",
    )
    (tmp_path / "control.env").write_text(
        f"CONTROL_SESSION_SECRET={'c' * 48}\nCONFIG_ENCRYPTION_KEY={shared_key}\n",
        encoding="utf-8",
    )
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(installer, "_run", fake_run)

    installer._upgrade_files(config)
    marker = installer._prepare_upgrade_rollback(config)

    _, public = installer._read_env(tmp_path / "public.env")
    _, control = installer._read_env(tmp_path / "control.env")
    assert public["SESSION_SECRET"] == "s" * 48
    assert control["CONTROL_SESSION_SECRET"] == "c" * 48
    assert public["CONFIG_ENCRYPTION_KEY"] == shared_key
    assert control["CONFIG_ENCRYPTION_KEY"] == shared_key
    assert public["SESSION_DAYS"] == "180"
    assert [command[:3] for command in commands] == [["zfs", "snapshot", "-r"]]
    assert marker["source_integrity"]["app"]["quick_check"] == "ok"
    assert marker["source_integrity"]["control"]["foreign_key_violations"] == 0
    for backup in marker["backups"].values():
        with installer.sqlite3.connect(backup) as connection:
            assert connection.execute("SELECT COUNT(*) FROM preserved").fetchone()[0] == 1
    rollback = Path(next(iter(marker["backups"].values()))).parent / "ROLLBACK.json"
    assert rollback.is_file()
    assert shared_key not in rollback.read_text(encoding="utf-8")


def test_running_state_passes_on_first_check_without_sleep(monkeypatch):
    config = installer.InstallConfig.from_mapping(answers())
    calls = []

    def fake_midclt(method, *arguments, **kwargs):
        calls.append((method, arguments, kwargs))
        return json.dumps({"name": config.app_name, "state": "RUNNING"})

    monkeypatch.setattr(installer, "_midclt", fake_midclt)
    monkeypatch.setattr(
        installer.time,
        "sleep",
        lambda _seconds: pytest.fail("RUNNING on first check must not sleep"),
    )

    result = installer._check_running(config, start_if_stopped=True)

    assert result["state"] == "RUNNING"
    assert calls == [("app.get_instance", (config.app_name,), {})]


def test_running_state_waits_through_deploying_without_real_sleep(monkeypatch):
    config = installer.InstallConfig.from_mapping(answers())
    states = iter(("DEPLOYING", "RUNNING"))
    clock = {"now": 0.0, "sleeps": []}
    calls = []

    def fake_midclt(method, *_arguments, **_kwargs):
        calls.append(method)
        return json.dumps({"state": next(states)})

    def fake_sleep(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(installer, "_midclt", fake_midclt)
    monkeypatch.setattr(installer.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(installer.time, "sleep", fake_sleep)

    result = installer._check_running(
        config,
        timeout_seconds=5.0,
        poll_seconds=1.0,
        start_if_stopped=True,
    )

    assert result["state"] == "RUNNING"
    assert clock["sleeps"] == [1.0]
    assert calls == ["app.get_instance", "app.get_instance"]


def test_running_state_timeout_reports_last_observed_state(monkeypatch):
    config = installer.InstallConfig.from_mapping(answers())
    clock = {"now": 0.0, "sleeps": []}
    calls = []

    def fake_midclt(method, *_arguments, **_kwargs):
        calls.append(method)
        return json.dumps({"state": "DEPLOYING"})

    monkeypatch.setattr(installer, "_midclt", fake_midclt)
    monkeypatch.setattr(installer.time, "monotonic", lambda: clock["now"])

    def fake_sleep(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(installer.time, "sleep", fake_sleep)

    with pytest.raises(
        installer.InstallerError,
        match="Aplikacja nie osiągnęła stanu RUNNING: DEPLOYING",
    ):
        installer._check_running(
            config,
            timeout_seconds=2.0,
            poll_seconds=1.0,
            start_if_stopped=True,
        )

    assert clock["sleeps"] == [1.0, 1.0]
    assert calls == ["app.get_instance", "app.get_instance", "app.get_instance"]


def test_upgrade_stopped_reverse_proxy_starts_before_database_gate(
    monkeypatch, tmp_path
):
    config = installer.InstallConfig.from_mapping(
        answers(
            mode="upgrade",
            public_access_mode="reverse-proxy",
            public_url="https://requests.example.com",
            control_access_mode="reverse-proxy",
            control_url="https://control.example.internal",
        )
    )
    events = stub_execute_lifecycle(
        monkeypatch, tmp_path, config, initial_state="STOPPED"
    )

    result = installer.execute(config, TEMPLATE_PATH.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["state"] == "RUNNING"
    assert result["runtime_gateway_auto_trust"] == {"public": True, "control": True}
    assert events == [
        "app.update",
        "state:STOPPED",
        "app.start",
        "state:RUNNING",
        "databases",
    ]
    assert (tmp_path / "install-result-0.5.2.json").is_file()


@pytest.mark.parametrize(
    ("mode", "mutation_method"),
    (("fresh", "app.create"), ("upgrade", "app.update")),
)
def test_stopped_lan_app_is_started_before_database_gate(
    monkeypatch, tmp_path, mode, mutation_method
):
    config = installer.InstallConfig.from_mapping(answers(mode=mode))
    events = stub_execute_lifecycle(
        monkeypatch, tmp_path, config, initial_state="STOPPED"
    )

    result = installer.execute(config, TEMPLATE_PATH.read_text(encoding="utf-8"))

    assert result["state"] == "RUNNING"
    assert result["runtime_gateway_auto_trust"] == {"public": False, "control": False}
    assert events == [
        mutation_method,
        "state:STOPPED",
        "app.start",
        "state:RUNNING",
        "databases",
    ]


def test_running_upgrade_does_not_call_app_start(monkeypatch, tmp_path):
    config = installer.InstallConfig.from_mapping(answers(mode="upgrade"))
    events = stub_execute_lifecycle(
        monkeypatch, tmp_path, config, initial_state="RUNNING"
    )

    result = installer.execute(config, TEMPLATE_PATH.read_text(encoding="utf-8"))

    assert result["state"] == "RUNNING"
    assert events == ["app.update", "state:RUNNING", "databases"]
    assert "app.start" not in events


def test_app_start_failure_stops_before_database_and_success_result(
    monkeypatch, tmp_path
):
    config = installer.InstallConfig.from_mapping(
        answers(
            mode="upgrade",
            public_access_mode="reverse-proxy",
            public_url="https://requests.example.com",
        )
    )
    events = stub_execute_lifecycle(
        monkeypatch,
        tmp_path,
        config,
        initial_state="STOPPED",
        start_fails=True,
    )

    with pytest.raises(installer.subprocess.CalledProcessError):
        installer.execute(config, TEMPLATE_PATH.read_text(encoding="utf-8"))

    assert events == ["app.update", "state:STOPPED", "app.start"]
    assert "databases" not in events
    assert not (tmp_path / "install-result-0.5.2.json").exists()


def test_post_start_database_gate_requires_title_en_and_checks_both_databases(
    monkeypatch, tmp_path
):
    config = installer.InstallConfig.from_mapping(answers())
    monkeypatch.setattr(
        installer.InstallConfig, "root_path", property(lambda self: tmp_path)
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "control").mkdir()
    with installer.sqlite3.connect(tmp_path / "app" / "app.db") as connection:
        connection.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY)")
    with installer.sqlite3.connect(tmp_path / "control" / "control.db") as connection:
        connection.execute("CREATE TABLE control_users (id INTEGER PRIMARY KEY)")

    with pytest.raises(installer.InstallerError, match=r"requests\.title_en"):
        installer._wait_for_post_start_databases(config, timeout_seconds=0)

    with installer.sqlite3.connect(tmp_path / "app" / "app.db") as connection:
        connection.execute("ALTER TABLE requests ADD COLUMN title_en TEXT")

    report = installer._wait_for_post_start_databases(
        config, timeout_seconds=0
    )
    assert report["app"]["quick_check"] == "ok"
    assert report["app"]["required_columns"] == {"requests": ["title_en"]}
    assert report["control"]["quick_check"] == "ok"
    assert report["control"]["foreign_key_violations"] == 0


@pytest.mark.parametrize(
    ("mode", "existing", "message"),
    (
        ("fresh", {"name": "penczreq", "custom_app": True}, "już istnieje"),
        ("upgrade", None, "Nie znaleziono"),
        ("upgrade", {"name": "penczreq", "custom_app": False}, "Custom App"),
    ),
)
def test_execute_rejects_invalid_app_target_before_any_mutation(
    monkeypatch, mode, existing, message
):
    config = installer.InstallConfig.from_mapping(answers(mode=mode))
    calls = []
    monkeypatch.setattr(installer, "_ensure_truenas_runtime", lambda: None)
    monkeypatch.setattr(installer, "_app_instance", lambda _name: existing)
    monkeypatch.setattr(installer, "_ensure_datasets", lambda _config: calls.append("dataset"))

    with pytest.raises(installer.InstallerError, match=message):
        installer.execute(config, TEMPLATE_PATH.read_text(encoding="utf-8"))

    assert calls == []


def test_missing_local_image_fails_before_every_execute_mutation(monkeypatch):
    config = installer.InstallConfig.from_mapping(
        answers(
            mode="upgrade",
            image="penczreq:0.5.2-rc-local-test",
        )
    )
    commands = []
    mutations = []

    monkeypatch.setattr(installer, "_ensure_truenas_runtime", lambda: None)
    monkeypatch.setattr(
        installer,
        "_validate_app_target",
        lambda _config: {"name": config.app_name, "custom_app": True},
    )

    def missing_image(command, **_kwargs):
        commands.append(command)
        raise installer.subprocess.CalledProcessError(1, command)

    def record_mutation(name):
        return lambda *_args, **_kwargs: mutations.append(name)

    monkeypatch.setattr(installer, "_run", missing_image)
    for name in (
        "_ensure_datasets",
        "_prepare_upgrade_rollback",
        "_upgrade_files",
        "_fresh_files",
        "_atomic_write",
        "_install_or_update",
        "_check_running",
    ):
        monkeypatch.setattr(installer, name, record_mutation(name))

    with pytest.raises(installer.InstallerError) as error:
        installer.execute(
            config,
            TEMPLATE_PATH.read_text(encoding="utf-8"),
            local_image=True,
        )

    assert config.image in str(error.value)
    assert "docker load -i" in str(error.value)
    assert commands == [["docker", "image", "inspect", config.image]]
    assert mutations == []


def test_existing_local_image_allows_execute_flow(monkeypatch, tmp_path):
    config = installer.InstallConfig.from_mapping(
        answers(image="penczreq:0.5.2-rc-local-test")
    )
    events = stub_execute_lifecycle(
        monkeypatch, tmp_path, config, initial_state="STOPPED"
    )
    commands = []

    def successful_command(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(installer, "_run", successful_command)

    result = installer.execute(
        config,
        TEMPLATE_PATH.read_text(encoding="utf-8"),
        local_image=True,
    )

    assert commands[0] == ["docker", "image", "inspect", config.image]
    assert [
        "docker",
        "compose",
        "-f",
        str(tmp_path / "compose-0.5.2.yaml"),
        "config",
        "-q",
    ] in commands
    assert result["ok"] is True
    assert result["local_image"] is True
    assert events == [
        "app.create",
        "state:STOPPED",
        "app.start",
        "state:RUNNING",
        "databases",
    ]


def test_execute_path_uses_official_middleware_jobs_and_guarded_rollback():
    source = INSTALLER_PATH.read_text(encoding="utf-8")

    assert '"app.create"' in source
    assert '"app.update"' in source
    assert '"app.start"' in source
    assert '"app.query"' in source
    assert '"app.get_instance"' in source
    assert '"zfs", "snapshot", "-r"' in source
    assert "PRAGMA quick_check" in source
    assert "PRAGMA foreign_key_check" in source
    initial_running_check = source.index(
        "state = _check_running(config, start_if_stopped=True)"
    )
    database_check = source.index(
        "post_start_databases = _wait_for_post_start_databases(config)"
    )
    assert initial_running_check < database_check
    assert "_discover_proxy_peer" not in source
    assert '"docker", "network"' not in source
    assert '"app.redeploy"' not in source
    assert '"title_en"' in source
    assert "shell=True" not in source
    assert not re.search(r"172\.16\.[56]\.[12]", source)


def test_installer_archive_is_versioned_complete_and_reproducible(tmp_path):
    first_archive, first_checksum = builder.build(tmp_path / "first")
    second_archive, _ = builder.build(tmp_path / "second")

    assert first_archive.name == "penczreq-installer-0.5.2.tar.gz"
    assert first_archive.read_bytes() == second_archive.read_bytes()
    expected_digest = hashlib.sha256(first_archive.read_bytes()).hexdigest()
    assert first_checksum.read_text(encoding="ascii") == (
        f"{expected_digest}  {first_archive.name}\n"
    )
    with tarfile.open(first_archive, "r:gz") as package:
        members = {member.name: member for member in package.getmembers()}
        compose_template = package.extractfile(
            "penczreq-installer-0.5.2/compose.yaml.example"
        )
        assert compose_template is not None
        packaged_compose = compose_template.read().decode("utf-8")
    prefix = "penczreq-installer-0.5.2/"
    assert set(members) == {prefix + name for name in builder.FILES}
    assert members[prefix + "install.sh"].mode == 0o755
    assert members[prefix + "installer.py"].mode == 0o755
    assert "http://127.0.0.1:8000/internal/health" in packaged_compose
    assert "http://127.0.0.1:8001/internal/health" in packaged_compose
    assert "](http://127.0.0.1:" not in packaged_compose
