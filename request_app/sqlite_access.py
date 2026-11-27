"""SQLite entry points used by the service account, including its CLI."""
import os
import sqlite3
from pathlib import Path


def require_owner(path: Path) -> None:
    if not hasattr(os, "geteuid"):
        return
    uid = os.geteuid()
    candidates = [path if path.exists() else path.parent]
    candidates.extend(path.with_name(path.name + ext) for ext in ("-wal", "-shm", "-journal"))
    for candidate in candidates:
        try:
            owner = candidate.stat().st_uid
        except FileNotFoundError:
            continue
        if owner != uid:
            raise PermissionError(f"SQLite owner mismatch: {candidate.name}; use the application account")


def open_database(path: Path, mode: str, **options):
    require_owner(path)
    return sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode, uri=True, **options)
