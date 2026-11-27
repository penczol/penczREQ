import hashlib
import shutil
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from deploy.truenas import sqlite_snapshot as diag


def test_live_directory_is_rejected_before_open(tmp_path):
    with patch.object(sqlite3, "connect", side_effect=AssertionError("opened source")), pytest.raises(ValueError):
        diag.inspect_snapshot(tmp_path)


def test_snapshot_diagnostics_keeps_wal_only_commits_without_writing_input(tmp_path, monkeypatch):
    snap = tmp_path / ".zfs" / "snapshot" / "example"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for relative in diag.DATABASES:
        target = snap / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # No concurrent writer while copying the fixture. Keep the connection
        # open so the committed schema and rows exist only in the WAL.
        with closing(sqlite3.connect(tmp_path / target.name)) as src:
            assert src.execute("pragma journal_mode=wal").fetchone() == ("wal",)
            if target.name == ".migrations.db":
                src.executescript("CREATE TABLE installation(id); INSERT INTO installation VALUES ('test');"
                                  "CREATE TABLE runs(id,target,phase); INSERT INTO runs VALUES(1,'build','ready');")
            else:
                src.executescript("CREATE TABLE penczreq_component(name,installation); PRAGMA user_version=2;")
                src.execute("INSERT INTO penczreq_component VALUES(?, 'test')", (target.stem,))
            src.commit()
            for suffix in ("", "-wal"):
                shutil.copyfile(tmp_path / (target.name + suffix), target.with_name(target.name + suffix))
    def files():
        return {p: (hashlib.sha256(p.read_bytes()).digest(), p.stat().st_mtime_ns)
                for p in snap.rglob("*") if p.is_file()}

    before = files()
    # The actual ZFS mount and tmpfs checks run on TrueNAS. Reuse this
    # immutable fixture on runners without ZFS or /dev/shm.
    monkeypatch.setattr(diag.subprocess, "check_output",
                        lambda args, **kw: "tmpfs" if args[-1] == "/dev/shm" else "zfs pool/test@example")
    monkeypatch.setattr(diag.os, "ST_RDONLY", 1, raising=False)
    monkeypatch.setattr(diag.os, "statvfs", lambda path: SimpleNamespace(f_flag=1), raising=False)
    tmpdir = diag.tempfile.TemporaryDirectory
    monkeypatch.setattr(diag.tempfile, "TemporaryDirectory", lambda **kw: tmpdir(dir=scratch))
    report = diag.inspect_snapshot(snap)
    assert report == {"app/app.db": {"schema": 2}, "control/control.db": {"schema": 2},
                      "control/.migrations.db": {"run": 1, "release": "build", "phase": "ready"}}
    assert files() == before and not list(scratch.iterdir())


def test_unexpected_dataset_is_rejected_before_sqlite(tmp_path, monkeypatch):
    root = tmp_path / ".zfs" / "snapshot" / "sample"
    root.mkdir(parents=True)
    (root / "app.db").touch()
    monkeypatch.setattr(diag.subprocess, "check_output", lambda *a, **kw: "zfs pool/other@sample")
    with pytest.raises(ValueError, match="dataset"):
        diag.copy_database(root, "app.db", tmp_path, "pool/fixture/app")


def test_foreign_uid_is_rejected_before_opening_even_a_readonly_connection(tmp_path, monkeypatch):
    from request_app import sqlite_access
    path = tmp_path / "app.db"
    path.touch()
    monkeypatch.setattr(sqlite_access.os, "geteuid", lambda: path.stat().st_uid + 1, raising=False)
    with patch.object(sqlite3, "connect", side_effect=AssertionError("opened live file")), pytest.raises(PermissionError):
        sqlite_access.open_database(path, "ro")
    assert list(tmp_path.iterdir()) == [path]


def test_runtime_integrity_does_not_change_journal_mode_or_create_a_missing_db(tmp_path):
    from request_app.database import Database
    from request_app.maintenance import integrity_report, _backup_one
    path = tmp_path / "app.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE preserved(value)")
    original = path.read_bytes()
    assert integrity_report(Database(path), tmp_path / "control.db") == {"app": "ok"}
    assert path.read_bytes() == original
    with pytest.raises(sqlite3.OperationalError):
        _backup_one(tmp_path / "missing.db", tmp_path / "backup.db")
    assert list(tmp_path.iterdir()) == [path]

def test_snapshot_mount_triggers_lazy_zfs_mount_before_findmnt(tmp_path, monkeypatch):
    root = tmp_path / ".zfs" / "snapshot" / "lazy"
    root.mkdir(parents=True)
    child = root / "app.db"
    child.touch()
    from pathlib import Path
    original_lstat = Path.lstat
    mounted = False
    def lookup(path):
        nonlocal mounted
        if path == child:
            mounted = True
        return original_lstat(path)
    monkeypatch.setattr(Path, "lstat", lookup)
    monkeypatch.setattr(diag.subprocess, "check_output", lambda *a, **kw:
                        "zfs pool/fixture@lazy" if mounted else "zfs pool/fixture")
    monkeypatch.setattr(diag.os, "ST_RDONLY", 1, raising=False)
    monkeypatch.setattr(diag.os, "statvfs", lambda path: SimpleNamespace(f_flag=1), raising=False)
    assert diag.snapshot_mount(root, "app.db", "pool/fixture") == root
