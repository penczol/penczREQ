"""Inspect copies of the three databases from one immutable TrueNAS ZFS snapshot."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile


DATABASES = ("app/app.db", "control/control.db", "control/.migrations.db")


def inspect_snapshot(root):
    root = Path(root).resolve(strict=True)
    if root.parent.name != "snapshot" or root.parent.parent.name != ".zfs":
        raise ValueError("Expected the root of a named ZFS snapshot")
    mount = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE,SOURCE", "-T", str(root)],
                                    text=True).split()
    if len(mount) != 2 or mount[0] != "zfs" or not mount[1].endswith("@" + root.name):
        raise ValueError("Expected a mounted ZFS snapshot of this name")
    if not os.statvfs(root).f_flag & os.ST_RDONLY:
        raise ValueError("Snapshot must be mounted read-only")
    if subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "-T", "/dev/shm"],
                               text=True).strip() != "tmpfs":
        raise ValueError("Private scratch space must be on tmpfs")

    results = {}
    with tempfile.TemporaryDirectory(dir="/dev/shm", prefix="penczreq-diag-") as scratch:
        for relative in DATABASES:
            source = root / relative
            local = Path(scratch) / source.name
            for suffix in ("", "-wal", "-journal"):
                part = source.with_name(source.name + suffix)
                if suffix and not part.exists() and not part.is_symlink():
                    continue
                if part.resolve(strict=True) != part or not stat.S_ISREG(part.stat().st_mode):
                    raise ValueError(f"Unexpected input: {relative}{suffix}")
                if part.stat().st_dev != root.stat().st_dev:
                    raise ValueError("All databases and journals must belong to the same snapshot")
                shutil.copyfile(part, local.with_name(local.name + suffix))
            # Recovery, WAL index creation and any checkpoint are confined to tmpfs.
            with closing(sqlite3.connect(local.as_uri() + "?mode=rw", uri=True)) as db:
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError(f"Integrity check failed in {relative}")
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError(f"Foreign key check failed in {relative}")
                if source.name == ".migrations.db":
                    run, release, phase = db.execute(
                        "SELECT id, target, phase FROM runs ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    installation, = db.execute("SELECT id FROM installation").fetchone()
                    results[relative] = dict(run=run, release=release, phase=phase)
                else:
                    name, identity = db.execute("SELECT name, installation FROM penczreq_component").fetchone()
                    if name != source.stem:
                        raise ValueError(f"Unexpected component identity for {relative}")
                    results[relative] = dict(schema=db.execute("PRAGMA user_version").fetchone()[0],
                                             installation=identity)
        if any(results[name].pop("installation") != installation for name in DATABASES[:2]):
            raise ValueError("Components belong to different installations")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot_root", type=Path)
    print(json.dumps(inspect_snapshot(parser.parse_args().snapshot_root), indent=2))
