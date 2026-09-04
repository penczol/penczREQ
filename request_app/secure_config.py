from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .database import Database, utc_now
from .i18n import translate


CONFIG_HISTORY_MISSING = "secret:missing"
CONFIG_HISTORY_CONFIGURED = "secret:configured"
CONFIG_HISTORY_CHANGED = "secret:changed"

_SETTINGS_HISTORY_SYSTEM_SOURCES = {
    "tmdb_token": {
        CONFIG_HISTORY_MISSING: "brak",
        CONFIG_HISTORY_CONFIGURED: "ustawiony",
        CONFIG_HISTORY_CHANGED: "zmieniony",
        # Exact legacy values written before semantic history values were introduced.
        "brak": "brak",
        "ustawiony": "ustawiony",
        "zmieniony": "zmieniony",
    },
}


class SecureConfigError(RuntimeError):
    pass


def localize_settings_history(
    items: list[dict], language: str
) -> list[dict]:
    """Localize only exact, application-controlled settings history values."""
    localized: list[dict] = []
    for item in items:
        rendered = dict(item)
        known_values = _SETTINGS_HISTORY_SYSTEM_SOURCES.get(str(item.get("key")), {})
        for field in ("previous_value", "new_value"):
            value = item.get(field)
            source = known_values.get(value) if isinstance(value, str) else None
            if source is not None:
                rendered[field] = translate(source, language)
        localized.append(rendered)
    return localized


class SecureConfigStore:
    """Encrypted-at-rest application secrets and audited non-secret settings."""

    def __init__(self, db: Database, master_key: str):
        self.db = db
        self._cipher = AESGCM(hashlib.sha256(master_key.encode("utf-8")).digest())

    def initialize(
        self,
        *,
        tmdb_token: str = "",
        public_base_url: str = "",
        known_proxies: str = "",
    ) -> None:
        if tmdb_token and not self.has_secret("tmdb_token"):
            self.set_secret("tmdb_token", tmdb_token, changed_by="environment-bootstrap")
        if public_base_url and not self.get_setting("public_base_url"):
            self.set_setting("public_base_url", public_base_url, changed_by="environment-bootstrap")
        if known_proxies and not self.get_setting("known_proxies"):
            self.set_setting("known_proxies", known_proxies, changed_by="environment-bootstrap")

    def has_secret(self, key: str) -> bool:
        with self.db.connect() as conn:
            return conn.execute("SELECT 1 FROM app_secrets WHERE key = ?", (key,)).fetchone() is not None

    def get_secret(self, key: str, default: str = "") -> str:
        with self.db.connect() as conn:
            row = conn.execute("SELECT ciphertext FROM app_secrets WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            payload = base64.urlsafe_b64decode(str(row["ciphertext"]).encode("ascii"))
            nonce, ciphertext = payload[:12], payload[12:]
            return self._cipher.decrypt(nonce, ciphertext, key.encode("utf-8")).decode("utf-8")
        except Exception as exc:
            raise SecureConfigError(f"Nie można odszyfrować sekretu {key}.") from exc

    def set_secret(self, key: str, value: str, *, changed_by: str) -> None:
        value = value.strip()
        if not value:
            raise SecureConfigError("Sekret nie może być pusty.")
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, value.encode("utf-8"), key.encode("utf-8"))
        encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        with self.db.transaction() as conn:
            existed = conn.execute("SELECT 1 FROM app_secrets WHERE key = ?", (key,)).fetchone()
            conn.execute(
                """
                INSERT INTO app_secrets (key, ciphertext, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET ciphertext = excluded.ciphertext, updated_at = excluded.updated_at
                """,
                (key, encoded, utc_now()),
            )
            conn.execute(
                "INSERT INTO settings_history (key, previous_value, new_value, changed_by, changed_at) VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    CONFIG_HISTORY_CONFIGURED if existed else CONFIG_HISTORY_MISSING,
                    CONFIG_HISTORY_CHANGED,
                    changed_by,
                    utc_now(),
                ),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str, *, changed_by: str) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            previous = str(row["value"]) if row else None
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, utc_now()),
            )
            conn.execute(
                "INSERT INTO settings_history (key, previous_value, new_value, changed_by, changed_at) VALUES (?, ?, ?, ?, ?)",
                (key, previous, value, changed_by, utc_now()),
            )

    def settings_history(self, limit: int = 100) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM settings_history ORDER BY changed_at DESC, id DESC LIMIT ?",
                (min(max(limit, 1), 500),),
            ).fetchall()
        return [dict(row) for row in rows]

    def tmdb_token(self) -> str:
        return self.get_secret("tmdb_token")
