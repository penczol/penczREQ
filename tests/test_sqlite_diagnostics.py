import hashlib
import shutil
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import diagnose_sqlite_snapshot as diag


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
