from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .database import Database, utc_now


_SENSITIVE_FRAGMENTS = ("password", "token", "secret", "cookie", "authorization", "p256dh", "auth")
logger = logging.getLogger(__name__)


def _sanitize(value: Any, key: str = "") -> Any:
    if any(fragment in key.casefold() for fragment in _SENSITIVE_FRAGMENTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return value[:1000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


class SecurityAudit:
    def __init__(self, db: Database, logs_dir: Path):
        self.db = db
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(
        self,
        event_type: str,
        *,
        severity: str = "info",
        actor_type: str = "system",
        actor_id: int | None = None,
        username: str | None = None,
        ip_address: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if severity not in {"info", "warning", "critical"}:
            severity = "warning"
        timestamp = utc_now()
        safe_details = _sanitize(details or {})
        record = {
            "occurred_at": timestamp,
            "event_type": event_type[:100],
            "severity": severity,
            "actor_type": actor_type[:50],
            "actor_id": actor_id,
            "username": username[:64] if username else None,
            "ip_address": ip_address[:128] if ip_address else None,
            "details": safe_details,
        }
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO security_events
                (occurred_at, event_type, severity, actor_type, actor_id, username, ip_address, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp, record["event_type"], severity, record["actor_type"], actor_id,
                    record["username"], record["ip_address"],
                    json.dumps(safe_details, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        path = self.logs_dir / f"security-{datetime.now(UTC).date().isoformat()}.jsonl"
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._lock:
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
        except OSError:
            # The same event is already durable in SQLite. Do not turn a completed
            # security action into a 500 merely because the mirror log is unavailable.
            logger.exception("Nie udało się zapisać lustrzanego dziennika JSONL.")

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM security_events ORDER BY occurred_at DESC, id DESC LIMIT ?",
                (min(max(limit, 1), 1000),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json"))
            except (TypeError, ValueError):
                item["details"] = {}
                item.pop("details_json", None)
            result.append(item)
        return result

    def prune(self, retention_days: int) -> None:
        cutoff_date = date.today() - timedelta(days=max(7, min(retention_days, 365)))
        cutoff_time = datetime.combine(cutoff_date, datetime.min.time(), tzinfo=UTC).isoformat()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM security_events WHERE occurred_at < ?", (cutoff_time,))
        for path in self.logs_dir.glob("security-????-??-??.jsonl"):
            try:
                file_date = date.fromisoformat(path.stem.removeprefix("security-"))
            except ValueError:
                continue
            if file_date < cutoff_date:
                path.unlink(missing_ok=True)
