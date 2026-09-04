from __future__ import annotations

import os
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .database import Database, utc_now


class MaintenanceError(RuntimeError):
    pass


def _backup_one(source_path: Path, target_path: Path) -> None:
    temporary = target_path.with_suffix(target_path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source = sqlite3.connect(source_path, timeout=30)
    destination = sqlite3.connect(temporary, timeout=30)
    try:
        source.backup(destination)
        result = str(destination.execute("PRAGMA quick_check").fetchone()[0])
        if result != "ok":
            raise MaintenanceError(f"Kontrola kopii {source_path.name} nie powiodła się: {result}")
    finally:
        destination.close()
        source.close()
    os.replace(temporary, target_path)


def create_backup(
    db: Database,
    control_database_path: Path,
    backups_dir: Path,
    *,
    retention_days: int,
) -> dict[str, str]:
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    app_target = backups_dir / f"penczreq-{stamp}.db"
    _backup_one(db.path, app_target)
    result = {"app": app_target.name}
    if control_database_path.exists():
        control_target = backups_dir / f"control-{stamp}.db"
        _backup_one(control_database_path, control_target)
        result["control"] = control_target.name
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at) VALUES ('last_backup_at', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (utc_now(), utc_now()),
        )
    prune_backups(backups_dir, retention_days)
    return result


def backup_due(db: Database) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'last_backup_at'").fetchone()
    if not row or not row["value"]:
        return True
    try:
        last = datetime.fromisoformat(str(row["value"]))
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
    except ValueError:
        return True
    return last.date() < datetime.now(UTC).date()


def prune_backups(backups_dir: Path, retention_days: int) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=max(3, min(retention_days, 365)))
    for path in backups_dir.glob("*.db"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if modified < cutoff:
            path.unlink(missing_ok=True)


def list_backups(backups_dir: Path) -> list[dict]:
    items = []
    for path in sorted(backups_dir.glob("*.db"), key=lambda item: item.stat().st_mtime, reverse=True):
        stat = path.stat()
        items.append({
            "name": path.name,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).replace(microsecond=0).isoformat(),
        })
    return items[:200]


def integrity_report(db: Database, control_database_path: Path) -> dict[str, str]:
    report = {"app": db.quick_check()}
    if control_database_path.exists():
        conn = sqlite3.connect(control_database_path, timeout=10)
        try:
            report["control"] = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            conn.close()
    return report
