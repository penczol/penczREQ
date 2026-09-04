#!/usr/bin/env python3
"""penczREQ 0.5.2 TrueNAS configurator and guarded installer.

The default mode is a non-mutating dry run. TrueNAS changes require the explicit
``--execute`` switch and root privileges on the NAS. Secrets are never accepted
as command-line arguments and are stored only in host-side files with mode 0600.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


VERSION = "0.5.2"
APP_NAME_PATTERN = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
DATASET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*(/[A-Za-z0-9][A-Za-z0-9_.:-]*)+$")
ACCESS_MODES = {"lan", "reverse-proxy"}
SECRET_KEYS = {
    "SESSION_SECRET",
    "CONTROL_SESSION_SECRET",
    "CONFIG_ENCRYPTION_KEY",
    "PUBLIC_ADMIN_BOOTSTRAP_PASSWORD",
    "CONTROL_BOOTSTRAP_PASSWORD",
}
REQUIRED_APP_SCHEMA_COLUMNS = {"requests": {"title_en"}}
ROOT_FILE_OWNERSHIP = (0, 0)


class InstallerError(RuntimeError):
    pass


def _url(value: str, *, label: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InstallerError(f"{label} musi być pełnym adresem HTTP lub HTTPS.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InstallerError(
            f"{label} nie może zawierać danych logowania, zapytania ani fragmentu."
        )
    if parsed.path not in {"", "/"}:
        raise InstallerError(f"{label} nie może zawierać dodatkowej ścieżki.")
    return candidate


def _access_mode(value: str, *, label: str) -> str:
    candidate = value.strip().lower().replace("_", "-")
    if candidate not in ACCESS_MODES:
        raise InstallerError(f"{label} musi mieć wartość lan albo reverse-proxy.")
    return candidate


def _port(value: Any, *, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InstallerError(f"{label} musi być liczbą całkowitą.") from exc
    if result < 1024 or result > 65535:
        raise InstallerError(f"{label} musi mieścić się w zakresie 1024–65535.")
    return result


def _networks(value: str) -> str:
    result: list[str] = ["127.0.0.0/8", "::1/128"]
    for raw in value.split(","):
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            network = ipaddress.ip_network(candidate, strict=False)
        except ValueError as exc:
            raise InstallerError(
                f"Nieprawidłowa sieć dozwolona dla Control: {candidate}"
            ) from exc
        if network.prefixlen == 0 or network.is_global:
            raise InstallerError(
                "Sieci Control muszą być prywatne/lokalne i nie mogą obejmować Internetu."
            )
        rendered = str(network)
        if rendered not in result:
            result.append(rendered)
    if len(result) == 2:
        raise InstallerError("Podaj co najmniej jedną prywatną sieć LAN dla Control.")
    return ",".join(result)


@dataclass(frozen=True, slots=True)
class InstallConfig:
    mode: str
    app_name: str
    image: str
    root_dataset: str
    nas_ip: str
    public_port: int
    control_port: int
    public_access_mode: str
    public_url: str
    control_access_mode: str
    control_url: str
    control_allowed_networks: str
    timezone: str
    vapid_subject: str
    public_admin_username: str
    control_admin_username: str

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "InstallConfig":
        mode = str(data.get("mode", "fresh")).strip().lower()
        if mode not in {"fresh", "upgrade"}:
            raise InstallerError("Tryb instalatora musi mieć wartość fresh albo upgrade.")

        app_name = str(data.get("app_name", "penczreq")).strip().lower()
        if not APP_NAME_PATTERN.fullmatch(app_name) or len(app_name) > 40:
            raise InstallerError("Nazwa aplikacji ma nieprawidłowy format TrueNAS.")

        image = str(data.get("image", "")).strip()
        if not image or any(character.isspace() for character in image):
            raise InstallerError("Podaj pełną nazwę wersjonowanego obrazu OCI.")
        if image.endswith(":latest") or ":" not in image.rsplit("/", 1)[-1]:
            raise InstallerError("Obraz musi używać jawnego taga wersji albo stable, nie latest.")

        root_dataset = str(data.get("root_dataset", "")).strip().strip("/")
        if not DATASET_PATTERN.fullmatch(root_dataset) or ".." in root_dataset.split("/"):
            raise InstallerError("Podaj dataset w postaci POOL/ścieżka/penczreq.")

        try:
            nas_address = ipaddress.ip_address(str(data.get("nas_ip", "")).strip())
        except ValueError as exc:
            raise InstallerError("Podaj prawidłowy adres IPv4 NAS-a.") from exc
        if nas_address.version != 4 or nas_address.is_unspecified or nas_address.is_loopback:
            raise InstallerError("Adres NAS-a musi być konkretnym adresem IPv4 w LAN.")
        nas_ip = str(nas_address)

        public_port = _port(data.get("public_port", 18000), label="Port Public")
        control_port = _port(data.get("control_port", 18001), label="Port Control")
        if public_port == control_port:
            raise InstallerError("Porty Public i Control muszą być różne.")

        public_access_mode = _access_mode(
            str(data.get("public_access_mode", "lan")), label="Tryb Public"
        )
        control_access_mode = _access_mode(
            str(data.get("control_access_mode", "lan")), label="Tryb Control"
        )
        public_url = _url(str(data.get("public_url", "")), label="Public URL")
        control_url = _url(str(data.get("control_url", "")), label="Control URL")
        cls._validate_url_mode(
            public_url, public_access_mode, nas_ip, public_port, label="Public"
        )
        cls._validate_url_mode(
            control_url, control_access_mode, nas_ip, control_port, label="Control"
        )

        networks = _networks(str(data.get("control_allowed_networks", "")))
        timezone = str(data.get("timezone", "Europe/Warsaw")).strip()
        if not timezone or any(character.isspace() for character in timezone):
            raise InstallerError("Strefa czasowa ma nieprawidłowy format.")
        vapid_subject = str(
            data.get("vapid_subject", "mailto:webpush@example.invalid")
        ).strip()
        if not (vapid_subject.startswith("mailto:") or vapid_subject.startswith("https://")):
            raise InstallerError("VAPID subject musi być adresem mailto: albo HTTPS URL.")

        public_admin = str(data.get("public_admin_username", "admin")).strip().lower()
        control_admin = str(
            data.get("control_admin_username", "control-admin")
        ).strip().lower()
        username_pattern = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
        if not username_pattern.fullmatch(public_admin):
            raise InstallerError("Login administratora Public ma nieprawidłowy format.")
        if not username_pattern.fullmatch(control_admin):
            raise InstallerError("Login administratora Control ma nieprawidłowy format.")

        return cls(
            mode=mode,
            app_name=app_name,
            image=image,
            root_dataset=root_dataset,
            nas_ip=nas_ip,
            public_port=public_port,
            control_port=control_port,
            public_access_mode=public_access_mode,
            public_url=public_url,
            control_access_mode=control_access_mode,
            control_url=control_url,
            control_allowed_networks=networks,
            timezone=timezone,
            vapid_subject=vapid_subject,
            public_admin_username=public_admin,
            control_admin_username=control_admin,
        )

    @staticmethod
    def _validate_url_mode(
        value: str, mode: str, nas_ip: str, port: int, *, label: str
    ) -> None:
        parsed = urlsplit(value)
        if mode == "lan":
            if parsed.scheme != "http":
                raise InstallerError(f"Tryb LAN {label} wymaga adresu HTTP.")
            if parsed.hostname != nas_ip or (parsed.port or 80) != port:
                raise InstallerError(
                    f"Adres LAN {label} musi wskazywać podany adres NAS-a i port."
                )
        elif parsed.scheme != "https":
            raise InstallerError(f"Tryb reverse-proxy {label} wymaga adresu HTTPS.")

    @property
    def root_path(self) -> Path:
        return Path("/mnt") / self.root_dataset


def _portal(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return parsed.scheme, str(parsed.hostname), int(parsed.port or (443 if parsed.scheme == "https" else 80))


def render_compose(
    template: str, config: InstallConfig, *, local_image: bool = False
) -> str:
    public_scheme, public_host, public_port = _portal(config.public_url)
    control_scheme, control_host, control_port = _portal(config.control_url)
    replacements = {
        "REPLACE_IMAGE": config.image,
        "REPLACE_APP_ROOT": config.root_path.as_posix(),
        "REPLACE_BIND_IP": config.nas_ip,
        "REPLACE_PUBLIC_PORT": str(config.public_port),
        "REPLACE_CONTROL_PORT": str(config.control_port),
        "REPLACE_PUBLIC_PORTAL_SCHEME": public_scheme,
        "REPLACE_PUBLIC_PORTAL_HOST": public_host,
        "REPLACE_PUBLIC_PORTAL_PORT": str(public_port),
        "REPLACE_CONTROL_PORTAL_SCHEME": control_scheme,
        "REPLACE_CONTROL_PORTAL_HOST": control_host,
        "REPLACE_CONTROL_PORTAL_PORT": str(control_port),
    }
    pull_policy_line = "  pull_policy: never\n" if local_image else ""
    rendered = template.replace("# REPLACE_PULL_POLICY_LINE\n", pull_policy_line)
    for source in sorted(replacements, key=len, reverse=True):
        target = replacements[source]
        rendered = rendered.replace(source, target)
    unresolved = sorted(set(re.findall(r"REPLACE_[A-Z0-9_]+", rendered)))
    if unresolved:
        raise InstallerError(f"Nierozwiązane pola Compose: {', '.join(unresolved)}")
    return rendered


def _generated_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@%_-"
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(28))
        if any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value):
            return value


def generate_fresh_secrets() -> dict[str, str]:
    return {
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "CONTROL_SESSION_SECRET": secrets.token_urlsafe(48),
        "CONFIG_ENCRYPTION_KEY": secrets.token_urlsafe(48),
        "PUBLIC_ADMIN_BOOTSTRAP_PASSWORD": _generated_password(),
        "CONTROL_BOOTSTRAP_PASSWORD": _generated_password(),
    }


def _hosts(url: str) -> str:
    hostname = urlsplit(url).hostname
    return ",".join(dict.fromkeys([str(hostname), "127.0.0.1", "localhost"]))


def environment_values(
    config: InstallConfig, secrets_map: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    shared = {
        "APP_ENV": "production",
        "APP_BASE_URL": config.public_url,
        "CONTROL_BASE_URL": config.control_url,
        "PUBLIC_ACCESS_MODE": config.public_access_mode,
        "CONTROL_ACCESS_MODE": config.control_access_mode,
        "ALLOWED_HOSTS": _hosts(config.public_url),
        "PUBLIC_TRUSTED_PROXIES": "",
        "TZ": config.timezone,
        "SECURITY_LOG_RETENTION_DAYS": "30",
        "MAX_REQUEST_BODY_BYTES": "262144",
        "POSTER_MAX_BYTES": "8388608",
    }
    public = {
        **shared,
        "APP_COMPONENT": "public",
        "COOKIE_SECURE": "true" if config.public_access_mode == "reverse-proxy" else "false",
        "DATA_DIR": "/data",
        "LOG_DIR": "/data/logs",
        "AUTO_TRUST_RUNTIME_GATEWAY": (
            "true" if config.public_access_mode == "reverse-proxy" else "false"
        ),
        "FORWARDED_ALLOW_IPS": "",
        "SESSION_SECRET": secrets_map["SESSION_SECRET"],
        "CONFIG_ENCRYPTION_KEY": secrets_map["CONFIG_ENCRYPTION_KEY"],
        "PUBLIC_ADMIN_USERNAME": config.public_admin_username,
        "PUBLIC_ADMIN_BOOTSTRAP_PASSWORD": secrets_map.get(
            "PUBLIC_ADMIN_BOOTSTRAP_PASSWORD", ""
        ),
        "VAPID_SUBJECT": config.vapid_subject,
        "SESSION_DAYS": "180",
        "SESSION_IDLE_MINUTES": "43200",
        "TMDB_REFRESH_HOURS": "6",
    }
    control = {
        **shared,
        "APP_COMPONENT": "control",
        "COOKIE_SECURE": "true" if config.control_access_mode == "reverse-proxy" else "false",
        "DATA_DIR": "/data",
        "CONTROL_DATA_DIR": "/control-data",
        "BACKUP_DIR": "/backups",
        "LOG_DIR": "/data/logs",
        "CONTROL_ALLOWED_HOSTS": _hosts(config.control_url),
        "CONTROL_ALLOWED_NETWORKS": config.control_allowed_networks,
        "CONTROL_TRUSTED_PROXIES": "",
        "AUTO_TRUST_RUNTIME_GATEWAY": (
            "true" if config.control_access_mode == "reverse-proxy" else "false"
        ),
        "FORWARDED_ALLOW_IPS": "",
        "CONTROL_SESSION_SECRET": secrets_map["CONTROL_SESSION_SECRET"],
        "CONFIG_ENCRYPTION_KEY": secrets_map["CONFIG_ENCRYPTION_KEY"],
        "PUBLIC_ADMIN_USERNAME": config.public_admin_username,
        "PUBLIC_ADMIN_BOOTSTRAP_PASSWORD": secrets_map.get(
            "PUBLIC_ADMIN_BOOTSTRAP_PASSWORD", ""
        ),
        "CONTROL_ADMIN_USERNAME": config.control_admin_username,
        "CONTROL_BOOTSTRAP_PASSWORD": secrets_map.get(
            "CONTROL_BOOTSTRAP_PASSWORD", ""
        ),
        "CONTROL_RECOVERY_NONCE": "",
        "CONTROL_RECOVERY_PASSWORD": "",
        "CONTROL_SESSION_HOURS": "8",
        "CONTROL_IDLE_MINUTES": "20",
        "BACKUP_RETENTION_DAYS": "30",
        "TMDB_TOKEN": "",
    }
    return public, control


def render_env(values: dict[str, str], *, redact: bool = False) -> str:
    lines = []
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise InstallerError(f"Wartość {key} zawiera niedozwolony znak nowej linii.")
        rendered = "<generated-during-execution>" if redact and key in SECRET_KEYS else value
        lines.append(f"{key}={rendered}")
    return "\n".join(lines) + "\n"


def _set_file_metadata(
    path: Path,
    *,
    mode: int,
    ownership: tuple[int, int] | None = None,
) -> None:
    if ownership is not None:
        if os.name != "nt":
            chown = getattr(os, "chown", None)
            if chown is None:
                raise InstallerError("System nie obsługuje wymaganego ownership pliku.")
            chown(path, *ownership)
    os.chmod(path, mode)


def _atomic_write(
    path: Path,
    content: str,
    mode: int = 0o600,
    *,
    ownership: tuple[int, int] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    _set_file_metadata(temporary, mode=mode, ownership=ownership)
    os.replace(temporary, path)
    _set_file_metadata(path, mode=mode, ownership=ownership)


def _read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return lines, values


def _update_env(
    path: Path,
    updates: dict[str, str],
    *,
    ownership: tuple[int, int] | None = None,
) -> None:
    lines, _ = _read_env(path)
    pending = dict(updates)
    result: list[str] = []
    for raw in lines:
        if "=" not in raw or raw.lstrip().startswith("#"):
            result.append(raw)
            continue
        key = raw.split("=", 1)[0].strip()
        if key in pending:
            result.append(f"{key}={pending.pop(key)}")
        else:
            result.append(raw)
    if pending:
        if result and result[-1]:
            result.append("")
        result.extend(f"{key}={value}" for key, value in pending.items())
    content = "\n".join(result) + "\n"
    if path.read_text(encoding="utf-8") == content:
        _set_file_metadata(path, mode=0o600, ownership=ownership)
        return
    _atomic_write(path, content, ownership=ownership)


def _run(command: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=capture,
    )


def _midclt(method: str, *arguments: Any, job: bool = False) -> str:
    command = ["midclt", "call"]
    if job:
        command.append("--job")
    command.append(method)
    command.extend(json.dumps(argument, separators=(",", ":")) for argument in arguments)
    return _run(command).stdout.strip()


def _app_instance(name: str) -> dict[str, Any] | None:
    raw = _midclt("app.query", [["name", "=", name]])
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InstallerError("TrueNAS zwrócił nieprawidłową listę aplikacji.") from exc
    if not isinstance(items, list) or len(items) > 1:
        raise InstallerError("TrueNAS zwrócił niejednoznaczny stan aplikacji.")
    return items[0] if items else None


def _ensure_truenas_runtime() -> None:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise InstallerError("--execute wymaga uruchomienia jako root na TrueNAS.")
    version_file = Path("/etc/version")
    if not version_file.exists() or "25.10" not in version_file.read_text(
        encoding="utf-8", errors="replace"
    ):
        raise InstallerError("Ten instalator jest przeznaczony dla TrueNAS SCALE 25.10.x.")
    for command in ("midclt", "docker", "zfs", "runuser"):
        if not shutil.which(command):
            raise InstallerError(f"Brak wymaganej komendy TrueNAS: {command}")


def _ensure_local_image_available(image: str) -> None:
    try:
        _run(["docker", "image", "inspect", image])
    except subprocess.CalledProcessError as exc:
        raise InstallerError(
            f"Brak lokalnego obrazu {image} w Docker image store. "
            "Tryb --local-image nie pobiera obrazu; najpierw wykonaj: "
            "docker load -i <docker-loadable-archive>."
        ) from exc


def _ensure_datasets(config: InstallConfig) -> None:
    parent = config.root_dataset.rsplit("/", 1)[0]
    if subprocess.run(
        ["zfs", "list", "-H", "-o", "name", parent], capture_output=True
    ).returncode:
        raise InstallerError(f"Nie istnieje nadrzędny dataset {parent}.")
    for suffix in ("", "/app", "/control", "/backups"):
        dataset = config.root_dataset + suffix
        exists = subprocess.run(
            ["zfs", "list", "-H", "-o", "name", dataset], capture_output=True
        ).returncode == 0
        if not exists:
            _run(
                [
                    "zfs", "create", "-o", "compression=lz4", "-o", "atime=off",
                    "-o", "acltype=nfsv4", "-o", "aclmode=passthrough",
                    "-o", "aclinherit=passthrough", dataset,
                ]
            )
        mountpoint = Path(
            _run(["zfs", "get", "-H", "-o", "value", "mountpoint", dataset]).stdout.strip()
        )
        expected = Path("/mnt") / dataset
        if mountpoint != expected:
            raise InstallerError(
                f"Nieoczekiwany mountpoint {mountpoint}; oczekiwano {expected}."
            )
        os.chown(mountpoint, 568, 568)
        os.chmod(mountpoint, 0o770)
        probe = mountpoint / ".penczreq-write-test"
        _run(
            [
                "runuser", "-u", "apps", "--", "sh", "-c",
                'umask 077; printf "ok\\n" > "$1"; test -s "$1"; rm -f "$1"',
                "_", str(probe),
            ]
        )
        if probe.exists():
            raise InstallerError(f"Pozostał plik testu zapisu: {probe}")


def _database_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise InstallerError(f"Brak wymaganej bazy: {path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    if quick != "ok" or foreign:
        raise InstallerError(f"Baza {path} nie przeszła kontroli integralności.")
    return {"path": str(path), "quick_check": quick, "foreign_key_violations": 0}


def _application_schema_report(path: Path) -> dict[str, Any]:
    report = _database_report(path)
    uri = f"file:{path.as_posix()}?mode=ro"
    missing: dict[str, list[str]] = {}
    with sqlite3.connect(uri, uri=True) as connection:
        for table, required in REQUIRED_APP_SCHEMA_COLUMNS.items():
            existing = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            absent = sorted(required - existing)
            if absent:
                missing[table] = absent
    if missing:
        rendered = ", ".join(
            f"{table}.{column}"
            for table, columns in sorted(missing.items())
            for column in columns
        )
        raise InstallerError(
            f"Baza aplikacji nie osiągnęła schematu {VERSION}; brak: {rendered}."
        )
    return {
        **report,
        "required_columns": {
            table: sorted(columns)
            for table, columns in sorted(REQUIRED_APP_SCHEMA_COLUMNS.items())
        },
    }


def _wait_for_post_start_databases(
    config: InstallConfig, *, timeout_seconds: float = 60.0, poll_seconds: float = 1.0
) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_error: Exception | None = None
    while True:
        try:
            return {
                "app": _application_schema_report(
                    config.root_path / "app" / "app.db"
                ),
                "control": _database_report(
                    config.root_path / "control" / "control.db"
                ),
            }
        except (InstallerError, sqlite3.Error) as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            raise InstallerError(
                f"Bazy po uruchomieniu {VERSION} nie osiągnęły oczekiwanego stanu: "
                f"{last_error}"
            ) from last_error
        time.sleep(max(0.01, poll_seconds))


def _backup_database(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as input_db, sqlite3.connect(destination) as output_db:
        input_db.backup(output_db)
    os.chmod(destination, 0o600)
    _database_report(destination)


def _prepare_upgrade_rollback(config: InstallConfig) -> dict[str, Any]:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_root = config.root_path / "backups" / f"pre-{VERSION}-{timestamp}"
    backup_root.mkdir(mode=0o700)
    sources = {
        "app": config.root_path / "app" / "app.db",
        "control": config.root_path / "control" / "control.db",
    }
    reports = {name: _database_report(path) for name, path in sources.items()}
    backups: dict[str, str] = {}
    for name, source in sources.items():
        destination = backup_root / f"{name}.db"
        _backup_database(source, destination)
        backups[name] = str(destination)
    snapshot = f"{config.root_dataset}@pre-{VERSION}-{timestamp}"
    _run(["zfs", "snapshot", "-r", snapshot])
    marker = {
        "version": VERSION,
        "created_at": timestamp,
        "snapshot": snapshot,
        "backups": backups,
        "source_integrity": reports,
    }
    _atomic_write(
        backup_root / "ROLLBACK.json",
        json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
    )
    return marker


def _compose_payload(config: InstallConfig, compose: str) -> dict[str, Any]:
    return {
        "app_name": config.app_name,
        "custom_app": True,
        "custom_compose_config_string": compose,
    }


def _validate_app_target(config: InstallConfig) -> dict[str, Any] | None:
    existing = _app_instance(config.app_name)
    if config.mode == "fresh" and existing:
        raise InstallerError("Aplikacja o tej nazwie już istnieje; użyj trybu upgrade.")
    if config.mode == "upgrade" and not existing:
        raise InstallerError("Nie znaleziono istniejącej aplikacji do aktualizacji.")
    if existing and not existing.get("custom_app"):
        raise InstallerError(
            "Upgrade wymaga istniejącej Custom App zarządzanej przez Compose."
        )
    return existing


def _install_or_update(
    config: InstallConfig, compose: str, existing: dict[str, Any] | None
) -> None:
    if config.mode == "fresh":
        if existing is not None:
            raise InstallerError("Stan aplikacji zmienił się podczas instalacji.")
        _midclt("app.create", _compose_payload(config, compose), job=True)
    else:
        if existing is None:
            raise InstallerError("Stan aplikacji zmienił się podczas aktualizacji.")
        _midclt(
            "app.update",
            config.app_name,
            {"custom_compose_config_string": compose},
            job=True,
        )


def _check_running(
    config: InstallConfig,
    *,
    timeout_seconds: float = 60.0,
    poll_seconds: float = 1.0,
    start_if_stopped: bool = False,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_state = "unknown"
    first_observation = True
    while True:
        raw = _midclt("app.get_instance", config.app_name)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InstallerError("TrueNAS zwrócił nieprawidłowy stan aplikacji.") from exc
        state = result.get("state", "unknown")
        if state == "RUNNING":
            return result
        if first_observation and start_if_stopped and state == "STOPPED":
            first_observation = False
            _midclt("app.start", config.app_name, job=True)
            continue
        first_observation = False
        last_state = str(state)
        if time.monotonic() >= deadline:
            raise InstallerError(
                f"Aplikacja nie osiągnęła stanu RUNNING: {last_state}"
            )
        time.sleep(max(0.01, poll_seconds))


def _fresh_files(config: InstallConfig) -> tuple[dict[str, str], dict[str, str]]:
    if any(
        path.exists()
        for path in (
            config.root_path / "public.env",
            config.root_path / "control.env",
            config.root_path / "app" / "app.db",
            config.root_path / "control" / "control.db",
        )
    ):
        raise InstallerError("Fresh install odmawia użycia istniejących env lub baz danych.")
    generated = generate_fresh_secrets()
    public, control = environment_values(config, generated)
    _atomic_write(
        config.root_path / "public.env",
        render_env(public),
        ownership=ROOT_FILE_OWNERSHIP,
    )
    _atomic_write(
        config.root_path / "control.env",
        render_env(control),
        ownership=ROOT_FILE_OWNERSHIP,
    )
    credentials = (
        f"penczREQ {VERSION} — hasła startowe (zmień przy pierwszym logowaniu)\n"
        f"Public username: {config.public_admin_username}\n"
        f"Public temporary password: {generated['PUBLIC_ADMIN_BOOTSTRAP_PASSWORD']}\n"
        f"Control username: {config.control_admin_username}\n"
        f"Control temporary password: {generated['CONTROL_BOOTSTRAP_PASSWORD']}\n"
    )
    _atomic_write(config.root_path / "bootstrap-credentials.txt", credentials)
    return public, control


def _upgrade_files(config: InstallConfig) -> tuple[dict[str, str], dict[str, str]]:
    public_path = config.root_path / "public.env"
    control_path = config.root_path / "control.env"
    if not public_path.is_file() or not control_path.is_file():
        raise InstallerError("Upgrade wymaga istniejących public.env i control.env.")
    _, existing_public = _read_env(public_path)
    _, existing_control = _read_env(control_path)
    required = {
        "SESSION_SECRET": existing_public.get("SESSION_SECRET", ""),
        "CONTROL_SESSION_SECRET": existing_control.get("CONTROL_SESSION_SECRET", ""),
        "CONFIG_ENCRYPTION_KEY": existing_public.get("CONFIG_ENCRYPTION_KEY", ""),
    }
    control_key = existing_control.get("CONFIG_ENCRYPTION_KEY", "")
    if any(len(value) < 32 for value in required.values()):
        raise InstallerError("Istniejące pliki env nie zawierają wymaganych sekretów.")
    if control_key != required["CONFIG_ENCRYPTION_KEY"]:
        raise InstallerError("Klucz CONFIG_ENCRYPTION_KEY Public i Control nie jest zgodny.")
    public, control = environment_values(config, required)
    for key in ("PUBLIC_TRUSTED_PROXIES", "FORWARDED_ALLOW_IPS"):
        public[key] = existing_public.get(key, public[key])
    for key in (
        "PUBLIC_TRUSTED_PROXIES",
        "CONTROL_TRUSTED_PROXIES",
        "FORWARDED_ALLOW_IPS",
    ):
        control[key] = existing_control.get(key, control[key])
    public_updates = {key: value for key, value in public.items() if key not in SECRET_KEYS}
    control_updates = {key: value for key, value in control.items() if key not in SECRET_KEYS}
    _update_env(public_path, public_updates, ownership=ROOT_FILE_OWNERSHIP)
    _update_env(control_path, control_updates, ownership=ROOT_FILE_OWNERSHIP)
    return public, control


def execute(
    config: InstallConfig, template: str, *, local_image: bool = False
) -> dict[str, Any]:
    _ensure_truenas_runtime()
    if "<owner>" in config.image or "OWNER" in config.image:
        raise InstallerError("Przed --execute zastąp placeholder właściciela obrazu.")
    existing = _validate_app_target(config)
    if local_image:
        _ensure_local_image_available(config.image)
    _ensure_datasets(config)
    rollback = None
    if config.mode == "upgrade":
        rollback = _prepare_upgrade_rollback(config)
        _upgrade_files(config)
    else:
        _fresh_files(config)

    compose = render_compose(template, config, local_image=local_image)
    compose_path = config.root_path / f"compose-{VERSION}.yaml"
    _atomic_write(compose_path, compose)
    _run(["docker", "compose", "-f", str(compose_path), "config", "-q"])
    _install_or_update(config, compose, existing)

    state = _check_running(config, start_if_stopped=True)
    post_start_databases = _wait_for_post_start_databases(config)
    result = {
        "ok": True,
        "version": VERSION,
        "mode": config.mode,
        "app_name": config.app_name,
        "local_image": local_image,
        "state": state["state"],
        "public_url": config.public_url,
        "control_url": config.control_url,
        "runtime_gateway_auto_trust": {
            "public": config.public_access_mode == "reverse-proxy",
            "control": config.control_access_mode == "reverse-proxy",
        },
        "rollback": rollback,
        "post_start_databases": post_start_databases,
    }
    _atomic_write(
        config.root_path / f"install-result-{VERSION}.json",
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
    )
    return result


def dry_run(
    config: InstallConfig,
    template: str,
    output_dir: Path,
    *,
    local_image: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    placeholders = {
        "SESSION_SECRET": "x" * 48,
        "CONTROL_SESSION_SECRET": "y" * 48,
        "CONFIG_ENCRYPTION_KEY": "z" * 48,
        "PUBLIC_ADMIN_BOOTSTRAP_PASSWORD": "GeneratedPublicPassword2026",  # pragma: allowlist secret
        "CONTROL_BOOTSTRAP_PASSWORD": "GeneratedControlPassword2026",  # pragma: allowlist secret
    }
    public, control = environment_values(config, placeholders)
    compose = render_compose(template, config, local_image=local_image)
    (output_dir / "compose.yaml").write_text(compose, encoding="utf-8", newline="\n")
    (output_dir / "public.env.example").write_text(
        render_env(public, redact=True), encoding="utf-8", newline="\n"
    )
    (output_dir / "control.env.example").write_text(
        render_env(control, redact=True), encoding="utf-8", newline="\n"
    )
    summary = {
        "ok": True,
        "dry_run": True,
        "version": VERSION,
        "mode": config.mode,
        "app_name": config.app_name,
        "local_image": local_image,
        "public_url": config.public_url,
        "control_url": config.control_url,
        "output_dir": str(output_dir.resolve()),
        "secrets_generated": False,
        "mutations_performed": False,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return summary


def _prompt(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def interactive_answers() -> dict[str, Any]:
    mode = _prompt("Tryb (fresh/upgrade)", "fresh")
    nas_ip = _prompt("Adres IPv4 NAS-a", "192.0.2.10")
    public_port = _prompt("Port Public", "18000")
    control_port = _prompt("Port Control", "18001")
    public_mode = _prompt("Public (lan/reverse-proxy)", "lan")
    default_public = (
        f"http://{nas_ip}:{public_port}"
        if public_mode.replace("_", "-") == "lan"
        else "https://requests.example.com"
    )
    control_mode = _prompt("Control (lan/reverse-proxy)", "lan")
    default_control = (
        f"http://{nas_ip}:{control_port}"
        if control_mode.replace("_", "-") == "lan"
        else "https://control.example.internal"
    )
    return {
        "mode": mode,
        "app_name": _prompt("Nazwa aplikacji TrueNAS", "penczreq"),
        "image": _prompt("Wersjonowany obraz OCI", "ghcr.io/<owner>/penczreq:0.5.2"),
        "root_dataset": _prompt("Dataset główny", "POOL/apps/penczreq"),
        "nas_ip": nas_ip,
        "public_port": public_port,
        "control_port": control_port,
        "public_access_mode": public_mode,
        "public_url": _prompt("Public URL", default_public),
        "control_access_mode": control_mode,
        "control_url": _prompt("Control URL", default_control),
        "control_allowed_networks": _prompt("Prywatne sieci Control", "192.0.2.0/24"),
        "timezone": _prompt("Strefa czasowa", "Europe/Warsaw"),
        "vapid_subject": _prompt("VAPID subject", "mailto:webpush@example.invalid"),
        "public_admin_username": _prompt("Login administratora Public", "admin"),
        "control_admin_username": _prompt("Login administratora Control", "control-admin"),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="penczREQ 0.5.2 TrueNAS installer")
    parser.add_argument("--answers", type=Path, help="JSON z odpowiedziami instalatora")
    parser.add_argument("--template", type=Path, help="Szablon Compose")
    parser.add_argument(
        "--local-image",
        action="store_true",
        help="Zabroń pobierania obrazu z registry (tylko lokalny RC/UAT)",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path.cwd() / "penczreq-installer-dry-run"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Tylko wygeneruj podgląd")
    mode.add_argument("--execute", action="store_true", help="Wykonaj na TrueNAS")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.answers:
            answers = json.loads(args.answers.read_text(encoding="utf-8"))
            if not isinstance(answers, dict):
                raise InstallerError("Plik odpowiedzi musi zawierać obiekt JSON.")
        else:
            answers = interactive_answers()
        config = InstallConfig.from_mapping(answers)
        template_path = args.template or Path(__file__).with_name("compose.yaml.example")
        template = template_path.read_text(encoding="utf-8")
        if args.execute:
            result = execute(config, template, local_image=args.local_image)
        else:
            result = dry_run(
                config, template, args.output_dir, local_image=args.local_image
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (InstallerError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"BŁĄD: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
