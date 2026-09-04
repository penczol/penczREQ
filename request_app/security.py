from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .database import Database, utc_now


USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
LOWER_RE = re.compile(r"[a-z]")
UPPER_RE = re.compile(r"[A-Z]")
DIGIT_RE = re.compile(r"[0-9]")
COMMON_PASSWORDS = {
    "administrator123",
    "passwordpassword1",
    "qwertyuiop123",
    "haslohaslo123",
    "adminadminadmin1",
}

password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
_dummy_password_hash = password_hasher.hash(secrets.token_urlsafe(32))


def normalize_username(username: str) -> str:
    return username.strip().lower()


def validate_username(username: str) -> str | None:
    if not USERNAME_RE.fullmatch(normalize_username(username)):
        return "Login musi mieć 3–32 znaki i może zawierać małe litery, cyfry, kropkę, myślnik lub podkreślenie."
    return None


def validate_password(password: str, *, username: str = "", current: str | None = None) -> str | None:
    if len(password) < 15:
        return "Hasło musi mieć co najmniej 15 znaków."
    if len(password) > 128:
        return "Hasło może mieć maksymalnie 128 znaków."
    if not password.isascii():
        return "Hasło może zawierać wyłącznie znaki ASCII — bez polskich liter."
    if not LOWER_RE.search(password) or not UPPER_RE.search(password) or not DIGIT_RE.search(password):
        return "Hasło musi zawierać małą literę, wielką literę i cyfrę."
    lowered = password.casefold()
    if lowered in COMMON_PASSWORDS or lowered == normalize_username(username) * 2:
        return "To hasło jest zbyt popularne lub łatwe do odgadnięcia."
    if current is not None and hmac.compare_digest(password.encode("utf-8"), current.encode("utf-8")):
        return "Nowe hasło nie może być takie samo jak obecne."
    return None


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def dummy_verify_password(password: str) -> None:
    """Run the same expensive hash verification for an unknown or inactive account."""
    verify_password(_dummy_password_hash, password)


def token_hash(raw_token: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw_token.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class NewSession:
    raw_token: str
    csrf_token: str
    expires_at: datetime


def create_session(db: Database, user_id: int, *, secret: str, days: int) -> NewSession:
    raw = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    now = datetime.now(UTC).replace(microsecond=0)
    expires = now + timedelta(days=days)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions (id, token_hash, user_id, csrf_token, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (secrets.token_hex(16), token_hash(raw, secret), user_id, csrf, now.isoformat(), expires.isoformat(), now.isoformat()),
        )
    return NewSession(raw, csrf, expires)


def get_session_user(db: Database, raw_token: str | None, *, secret: str, idle_minutes: int = 43_200):
    if not raw_token:
        return None
    hashed = token_hash(raw_token, secret)
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT u.*, s.id AS session_id, s.csrf_token, s.expires_at, s.last_seen_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1
            """,
            (hashed, now),
        ).fetchone()
        if row:
            last_seen = datetime.fromisoformat(row["last_seen_at"])
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=UTC)
            current = datetime.now(UTC).replace(microsecond=0)
            if last_seen + timedelta(minutes=idle_minutes) <= current:
                conn.execute("DELETE FROM sessions WHERE id = ?", (row["session_id"],))
                return None
            if last_seen + timedelta(seconds=60) <= current:
                conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now, row["session_id"]))
        return row


def delete_session(db: Database, raw_token: str | None, *, secret: str) -> None:
    if not raw_token:
        return
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(raw_token, secret),))


def delete_other_sessions(db: Database, user_id: int, keep_session_id: str | None = None) -> None:
    with db.transaction() as conn:
        if keep_session_id:
            conn.execute("DELETE FROM sessions WHERE user_id = ? AND id <> ?", (user_id, keep_session_id))
        else:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def prune_expired_sessions(db: Database, idle_minutes: int = 43_200) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_now(),))
        idle_cutoff = (datetime.now(UTC) - timedelta(minutes=idle_minutes)).replace(microsecond=0).isoformat()
        conn.execute("DELETE FROM sessions WHERE last_seen_at <= ?", (idle_cutoff,))
