from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .audit import SecurityAudit
from .database import Database, utc_now


WINDOW_MINUTES = 10
IP_FAILURE_THRESHOLD = 10
BLOCK_MINUTES = (15, 60, 1440)
ESCALATION_HOURS = 24


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def client_scope(ip_address: str) -> str:
    try:
        address = ipaddress.ip_address(ip_address)
    except ValueError:
        return ip_address[:128]
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


@dataclass(frozen=True, slots=True)
class LoginGate:
    blocked: bool
    retry_after: int = 0
    delay_seconds: int = 0


class LoginProtection:
    def __init__(self, db: Database, audit: SecurityAudit):
        self.db = db
        self.audit = audit

    def migrate_legacy_lockouts(self) -> int:
        with self.db.transaction() as conn:
            marker = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'legacy_lockouts_migrated'"
            ).fetchone()
            if marker:
                return 0
            count = int(conn.execute(
                "SELECT COUNT(*) FROM users WHERE security_locked = 1 OR temporary_lock_until IS NOT NULL"
            ).fetchone()[0])
            conn.execute(
                """
                UPDATE users SET failed_login_count = 0, temporary_lock_until = NULL,
                    lockout_cycles = 0, security_locked = 0, security_locked_at = NULL
                """
            )
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('legacy_lockouts_migrated', '1', ?)",
                (utc_now(),),
            )
        if count:
            self.audit.emit("legacy_lockouts_cleared", severity="warning", details={"accounts": count})
        return count

    def check(self, ip_address: str, username: str) -> LoginGate:
        now = datetime.now(UTC)
        ip_key = client_scope(ip_address)
        account_key = username.strip().lower()[:64]
        with self.db.connect() as conn:
            ip_row = conn.execute(
                "SELECT * FROM auth_throttles WHERE scope = 'ip' AND key = ?", (ip_key,)
            ).fetchone()
            account_row = conn.execute(
                "SELECT * FROM auth_throttles WHERE scope = 'account' AND key = ?", (account_key,)
            ).fetchone()
        if ip_row:
            blocked_until = _parse_datetime(ip_row["blocked_until"])
            if blocked_until and blocked_until > now:
                return LoginGate(True, max(1, int((blocked_until - now).total_seconds())))
        delay = 0
        if account_row:
            window = _parse_datetime(account_row["window_started_at"])
            if window and window > now - timedelta(minutes=WINDOW_MINUTES):
                failures = int(account_row["failures"] or 0)
                delay = min(15, 2 ** max(0, failures - 3)) if failures >= 3 else 0
        return LoginGate(False, delay_seconds=delay)

    @staticmethod
    def _upsert_failure(conn, scope: str, key: str, display_key: str, now: datetime):
        row = conn.execute(
            "SELECT * FROM auth_throttles WHERE scope = ? AND key = ?", (scope, key)
        ).fetchone()
        window = _parse_datetime(row["window_started_at"]) if row else None
        if not row or not window or window <= now - timedelta(minutes=WINDOW_MINUTES):
            failures = 1
            window_started = now.isoformat()
        else:
            failures = int(row["failures"] or 0) + 1
            window_started = row["window_started_at"]
        conn.execute(
            """
            INSERT INTO auth_throttles
            (scope, key, display_key, failures, window_started_at, last_failure_at,
             blocked_until, block_level, escalation_until, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?)
            ON CONFLICT(scope, key) DO UPDATE SET
                display_key = excluded.display_key, failures = excluded.failures,
                window_started_at = excluded.window_started_at,
                last_failure_at = excluded.last_failure_at, updated_at = excluded.updated_at
            """,
            (scope, key, display_key, failures, window_started, now.isoformat(), now.isoformat()),
        )
        return conn.execute(
            "SELECT * FROM auth_throttles WHERE scope = ? AND key = ?", (scope, key)
        ).fetchone()

    def record_failure(self, ip_address: str, username: str) -> LoginGate:
        current_gate = self.check(ip_address, username)
        if current_gate.blocked:
            return current_gate
        now = datetime.now(UTC).replace(microsecond=0)
        ip_key = client_scope(ip_address)
        account_key = username.strip().lower()[:64]
        block_level = 0
        blocked_until = None
        with self.db.transaction() as conn:
            self._upsert_failure(conn, "account", account_key, account_key, now)
            ip_row = self._upsert_failure(conn, "ip", ip_key, ip_key, now)
            if int(ip_row["failures"] or 0) >= IP_FAILURE_THRESHOLD:
                previous_escalation = _parse_datetime(ip_row["escalation_until"])
                previous_level = int(ip_row["block_level"] or 0)
                block_level = min(3, previous_level + 1) if previous_escalation and previous_escalation > now else 1
                blocked_until = now + timedelta(minutes=BLOCK_MINUTES[block_level - 1])
                conn.execute(
                    """
                    UPDATE auth_throttles SET failures = 0, blocked_until = ?, block_level = ?,
                        escalation_until = ?, updated_at = ? WHERE scope = 'ip' AND key = ?
                    """,
                    (
                        blocked_until.isoformat(), block_level,
                        (now + timedelta(hours=ESCALATION_HOURS)).isoformat(), now.isoformat(), ip_key,
                    ),
                )
        self.audit.emit(
            "login_failure", severity="warning", username=account_key, ip_address=ip_address,
            details={"ip_scope": ip_key, "blocked": bool(blocked_until), "block_level": block_level},
        )
        if blocked_until:
            self.audit.emit(
                "ip_temporarily_blocked", severity="critical", username=account_key,
                ip_address=ip_address,
                details={"ip_scope": ip_key, "level": block_level, "until": blocked_until.isoformat()},
            )
            return LoginGate(True, int((blocked_until - now).total_seconds()))
        return self.check(ip_address, account_key)

    def record_success(self, ip_address: str, username: str) -> None:
        ip_key = client_scope(ip_address)
        account_key = username.strip().lower()[:64]
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM auth_throttles WHERE scope = 'account' AND key = ?", (account_key,))
            conn.execute(
                """
                UPDATE auth_throttles SET failures = MAX(0, failures - 2), updated_at = ?
                WHERE scope = 'ip' AND key = ? AND (blocked_until IS NULL OR blocked_until <= ?)
                """,
                (utc_now(), ip_key, utc_now()),
            )

    def list_states(self, limit: int = 500) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM auth_throttles ORDER BY blocked_until IS NOT NULL DESC, updated_at DESC LIMIT ?",
                (min(max(limit, 1), 2000),),
            ).fetchall()
        return [dict(row) for row in rows]

    def reset(self, scope: str, key: str) -> bool:
        if scope not in {"ip", "account"}:
            return False
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM auth_throttles WHERE scope = ? AND key = ?", (scope, key))
        return cursor.rowcount == 1

    def prune(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=7)).replace(microsecond=0).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM auth_throttles WHERE updated_at < ? AND (blocked_until IS NULL OR blocked_until < ?)",
                (cutoff, utc_now()),
            )
            conn.execute(
                """
                DELETE FROM auth_throttles WHERE rowid IN (
                    SELECT rowid FROM auth_throttles ORDER BY updated_at DESC LIMIT -1 OFFSET 10000
                )
                """
            )
