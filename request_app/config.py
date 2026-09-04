from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from .proxy_trust import (
    EFFECTIVE_PROXY_LIMIT,
    EFFECTIVE_TRUSTED_PROXIES_ENV,
    ProxyTrustError,
    normalize_proxy_networks,
)


AccessMode = Literal["lan", "reverse-proxy"]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _boolean(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} musi być liczbą całkowitą.") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} musi mieścić się w zakresie {minimum}–{maximum}.")
    return value


def _access_mode(name: str, default: AccessMode) -> AccessMode:
    value = os.getenv(name, default).strip().lower().replace("_", "-")
    if value not in {"lan", "reverse-proxy"}:
        raise RuntimeError(f"{name} musi mieć wartość lan albo reverse-proxy.")
    return value  # type: ignore[return-value]


def _base_url(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"{name} musi być pełnym adresem HTTP lub HTTPS.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError(
            f"{name} nie może zawierać danych logowania, zapytania ani fragmentu."
        )
    if parsed.path not in {"", "/"}:
        raise RuntimeError(f"{name} nie może zawierać dodatkowej ścieżki.")
    return value


def _validate_access_contract(
    *, component: str, mode: AccessMode, base_url: str, cookie_secure: bool
) -> None:
    scheme = urlsplit(base_url).scheme
    if mode == "lan":
        if scheme != "http":
            raise RuntimeError(f"Tryb LAN usługi {component} wymaga adresu HTTP.")
        if cookie_secure:
            raise RuntimeError(
                f"Tryb LAN usługi {component} wymaga COOKIE_SECURE=false."
            )
        return
    if scheme != "https":
        raise RuntimeError(
            f"Tryb reverse-proxy usługi {component} wymaga adresu HTTPS."
        )
    if not cookie_secure:
        raise RuntimeError(
            f"Tryb reverse-proxy usługi {component} wymaga COOKIE_SECURE=true."
        )


def _proxy_networks(name: str, value: str, *, max_networks: int = 32) -> str:
    try:
        return normalize_proxy_networks(value, max_networks=max_networks)
    except ProxyTrustError as exc:
        raise RuntimeError(f"{name}: {exc}") from exc


def _development_secret(data_dir: Path, filename: str) -> str:
    path = data_dir / filename
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def _secret(name: str, *, env: str, data_dir: Path, development_filename: str) -> str:
    value = os.getenv(name, "").strip()
    if not value and env == "development":
        value = _development_secret(data_dir, development_filename)
    if len(value) < 32:
        raise RuntimeError(f"{name} musi mieć co najmniej 32 znaki.")
    return value


def _hosts(value: str, base_url: str, *, development: bool) -> tuple[str, ...]:
    result = {item.strip().lower() for item in value.split(",") if item.strip()}
    hostname = urlsplit(base_url).hostname
    if hostname:
        result.add(hostname.lower())
    if development:
        result.update({"127.0.0.1", "localhost", "testserver"})
    if not result:
        raise RuntimeError("ALLOWED_HOSTS nie może być puste.")
    if "*" in result and not development:
        raise RuntimeError("ALLOWED_HOSTS nie może zawierać * w środowisku produkcyjnym.")
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    app_env: str
    app_component: str
    app_base_url: str
    control_base_url: str
    public_access_mode: AccessMode
    control_access_mode: AccessMode
    allowed_hosts: tuple[str, ...]
    data_dir: Path
    control_data_dir: Path
    backup_dir: Path
    logs_dir: Path
    tmdb_token: str
    session_secret: str
    control_session_secret: str
    config_encryption_key: str
    timezone: str
    cookie_secure: bool
    session_days: int
    session_idle_minutes: int
    control_session_hours: int
    control_idle_minutes: int
    tmdb_refresh_hours: int
    vapid_subject: str
    control_allowed_networks: str
    control_trusted_proxies: str
    public_trusted_proxies: str
    control_effective_trusted_proxies: str
    public_effective_trusted_proxies: str
    runtime_proxy_resolved: bool
    control_allowed_hosts: tuple[str, ...]
    control_admin_username: str
    control_bootstrap_password: str
    control_recovery_nonce: str
    control_recovery_password: str
    public_admin_username: str
    public_admin_bootstrap_password: str
    security_log_retention_days: int
    backup_retention_days: int
    max_request_body_bytes: int
    poster_max_bytes: int

    @property
    def database_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def control_database_path(self) -> Path:
        return self.control_data_dir / "control.db"

    @property
    def posters_dir(self) -> Path:
        return self.data_dir / "posters"

    @property
    def backups_dir(self) -> Path:
        return self.backup_dir

    @property
    def vapid_private_key_path(self) -> Path:
        return self.data_dir / ".vapid-private.pem"

    @property
    def public_cookie_name(self) -> str:
        return "__Host-penczreq_session" if self.cookie_secure else "penczreq_session"

    @property
    def control_cookie_name(self) -> str:
        return "__Host-penczreq_control" if self.cookie_secure else "penczreq_control"


def load_settings() -> Settings:
    root = Path(__file__).resolve().parent.parent
    _load_dotenv(root / ".env.dev")
    _load_dotenv(root / ".env")

    def resolve_directory(value: str, default: Path) -> Path:
        path = Path(value.strip()) if value.strip() else default
        if not path.is_absolute():
            path = root / path
        return path.resolve()

    data_dir = resolve_directory(os.getenv("DATA_DIR", "dev-data"), root / "dev-data")
    control_data_dir = resolve_directory(
        os.getenv("CONTROL_DATA_DIR", ""), data_dir / "control"
    )
    backup_dir = resolve_directory(os.getenv("BACKUP_DIR", ""), data_dir / "backups")
    logs_dir = resolve_directory(os.getenv("LOG_DIR", ""), data_dir / "logs")

    env = os.getenv("APP_ENV", "development").strip().lower()
    if env not in {"development", "production", "test"}:
        raise RuntimeError("APP_ENV musi mieć wartość development, test albo production.")
    development = env != "production"

    cookie_secure = _boolean("COOKIE_SECURE", False)
    app_component = os.getenv("APP_COMPONENT", "combined").strip().lower()
    if app_component not in {"public", "control", "combined"}:
        raise RuntimeError("APP_COMPONENT musi mieć wartość public, control albo combined.")
    if env == "production" and app_component == "combined":
        raise RuntimeError(
            "W produkcji APP_COMPONENT musi jawnie wskazywać public albo control."
        )

    base_url = _base_url("APP_BASE_URL", "http://127.0.0.1:8000")
    control_base_url = _base_url(
        "CONTROL_BASE_URL", "http://127.0.0.1:8001"
    )
    public_access_mode = _access_mode(
        "PUBLIC_ACCESS_MODE", "reverse-proxy" if env == "production" else "lan"
    )
    control_access_mode = _access_mode("CONTROL_ACCESS_MODE", "lan")
    if env == "production":
        if app_component in {"public", "combined"}:
            _validate_access_contract(
                component="Public",
                mode=public_access_mode,
                base_url=base_url,
                cookie_secure=cookie_secure,
            )
        if app_component in {"control", "combined"}:
            _validate_access_contract(
                component="Control",
                mode=control_access_mode,
                base_url=control_base_url,
                cookie_secure=cookie_secure,
            )

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "posters").mkdir(exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    if app_component in {"control", "combined"}:
        control_data_dir.mkdir(parents=True, exist_ok=True)
        backup_dir.mkdir(parents=True, exist_ok=True)

    vapid_subject = os.getenv("VAPID_SUBJECT", "mailto:webpush@example.invalid").strip()
    if not (vapid_subject.startswith("mailto:") or vapid_subject.startswith("https://")):
        raise RuntimeError("VAPID_SUBJECT musi być adresem mailto: albo URL-em https://.")

    session_secret = (
        _secret(
            "SESSION_SECRET",
            env=env,
            data_dir=data_dir,
            development_filename=".dev-session-secret",
        )
        if app_component in {"public", "combined"}
        else ""
    )
    control_session_secret = (
        _secret(
            "CONTROL_SESSION_SECRET",
            env=env,
            data_dir=control_data_dir,
            development_filename=".dev-control-session-secret",
        )
        if app_component in {"control", "combined"}
        else ""
    )
    encryption_key = _secret(
        "CONFIG_ENCRYPTION_KEY", env=env, data_dir=data_dir,
        development_filename=".dev-config-encryption-key",
    )

    control_networks = os.getenv(
        "CONTROL_ALLOWED_NETWORKS",
        "127.0.0.0/8, ::1/128, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16"
        if development else "",
    ).strip()
    control_trusted_proxies = _proxy_networks(
        "CONTROL_TRUSTED_PROXIES", os.getenv("CONTROL_TRUSTED_PROXIES", "")
    )
    public_trusted_proxies = _proxy_networks(
        "PUBLIC_TRUSTED_PROXIES", os.getenv("PUBLIC_TRUSTED_PROXIES", "")
    )
    runtime_effective_source = os.environ.get(EFFECTIVE_TRUSTED_PROXIES_ENV)
    runtime_proxy_resolved = runtime_effective_source is not None
    control_effective_trusted_proxies = control_trusted_proxies
    public_effective_trusted_proxies = public_trusted_proxies
    if runtime_effective_source is not None:
        runtime_effective = _proxy_networks(
            EFFECTIVE_TRUSTED_PROXIES_ENV,
            runtime_effective_source,
            max_networks=EFFECTIVE_PROXY_LIMIT,
        )
        if app_component == "control":
            control_effective_trusted_proxies = runtime_effective
        elif app_component == "public":
            public_effective_trusted_proxies = runtime_effective
    control_hosts_raw = os.getenv(
        "CONTROL_ALLOWED_HOSTS", "*" if development else ""
    ).strip()
    if app_component in {"control", "combined"}:
        if not control_networks:
            raise RuntimeError("CONTROL_ALLOWED_NETWORKS nie może być puste.")
        if env == "production" and (
            not control_hosts_raw or "*" in control_hosts_raw.split(",")
        ):
            raise RuntimeError(
                "W produkcji CONTROL_ALLOWED_HOSTS musi zawierać konkretne hosty panelu."
            )

    return Settings(
        project_root=root,
        app_env=env,
        app_component=app_component,
        app_base_url=base_url,
        control_base_url=control_base_url,
        public_access_mode=public_access_mode,
        control_access_mode=control_access_mode,
        allowed_hosts=_hosts(os.getenv("ALLOWED_HOSTS", ""), base_url, development=development),
        data_dir=data_dir,
        control_data_dir=control_data_dir,
        backup_dir=backup_dir,
        logs_dir=logs_dir,
        tmdb_token=os.getenv("TMDB_TOKEN", "").strip(),
        session_secret=session_secret,
        control_session_secret=control_session_secret,
        config_encryption_key=encryption_key,
        timezone=os.getenv("TZ", "Europe/Warsaw"),
        cookie_secure=cookie_secure,
        session_days=_bounded_int("SESSION_DAYS", 180, 1, 180),
        session_idle_minutes=_bounded_int("SESSION_IDLE_MINUTES", 43_200, 15, 43_200),
        control_session_hours=_bounded_int("CONTROL_SESSION_HOURS", 8, 1, 24),
        control_idle_minutes=_bounded_int("CONTROL_IDLE_MINUTES", 20, 5, 60),
        tmdb_refresh_hours=_bounded_int("TMDB_REFRESH_HOURS", 6, 1, 168),
        vapid_subject=vapid_subject,
        control_allowed_networks=control_networks,
        control_trusted_proxies=control_trusted_proxies,
        public_trusted_proxies=public_trusted_proxies,
        control_effective_trusted_proxies=control_effective_trusted_proxies,
        public_effective_trusted_proxies=public_effective_trusted_proxies,
        runtime_proxy_resolved=runtime_proxy_resolved,
        control_allowed_hosts=_hosts(
            control_hosts_raw, control_base_url, development=development
        ),
        control_admin_username=os.getenv("CONTROL_ADMIN_USERNAME", "control-admin").strip().lower(),
        control_bootstrap_password=os.getenv("CONTROL_BOOTSTRAP_PASSWORD", ""),
        control_recovery_nonce=os.getenv("CONTROL_RECOVERY_NONCE", "").strip(),
        control_recovery_password=os.getenv("CONTROL_RECOVERY_PASSWORD", ""),
        public_admin_username=os.getenv("PUBLIC_ADMIN_USERNAME", "admin").strip().lower(),
        public_admin_bootstrap_password=os.getenv("PUBLIC_ADMIN_BOOTSTRAP_PASSWORD", ""),
        security_log_retention_days=_bounded_int("SECURITY_LOG_RETENTION_DAYS", 30, 7, 365),
        backup_retention_days=_bounded_int("BACKUP_RETENTION_DAYS", 30, 3, 365),
        max_request_body_bytes=_bounded_int("MAX_REQUEST_BODY_BYTES", 262_144, 16_384, 2_097_152),
        poster_max_bytes=_bounded_int("POSTER_MAX_BYTES", 8_388_608, 1_048_576, 20_971_520),
    )
