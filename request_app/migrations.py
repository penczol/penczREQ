"""Coordinate both SQLite stores before any service accepts application writes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from .sqlite_access import open_database
from . import __version__
from .audit import SecurityAudit
from .auth_protection import LoginProtection
from .repository import Repository
from .control_store import ControlStore
from .database import Database
from .changelog import update_notification_bodies
from .updates import notify_users_about_update

SCHEMA_VERSION = 2
# Official numbered images are immutable. UAT builds also have distinct versions.
_revision = Path(__file__).with_name("_build_revision")
RELEASE = __version__ + ("@" + _revision.read_text().strip() if _revision.exists() else "")
STORES = ("app", "control")


class MigrationError(RuntimeError):
    pass


def sync_directory(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


@contextmanager
def gate(directory, *, exclusive=False):
    # A separate, rollback-journal SQLite file supplies OS process locks on
    # Windows and Linux. Do not enable WAL on this file or unlink it.
    with closing(open_database(directory / ".runtime-lock", "rwc", timeout=5, isolation_level=None)) as conn:
        conn.execute("BEGIN EXCLUSIVE" if exclusive else "BEGIN")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        yield


def ready_path(settings):
    return settings.data_dir / ".runtime-ready.json"


def current(settings, release):
    path = ready_path(settings)
    return path.exists() and json.loads(path.read_text(encoding="utf-8")) == [release, SCHEMA_VERSION]


@contextmanager
def writer_session(settings, release=RELEASE):
    with gate(settings.data_dir):
        if not current(settings, release):
            raise MigrationError("Control must finish migration for this release before writes")
        yield


async def writer_thread(settings, func, *args, **kwargs):
    # Cancellation of the awaiter does not stop its worker thread.
    def run():
        with writer_session(settings):
            return func(*args, **kwargs)
    return await asyncio.to_thread(run)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def database(path, mode):
    with closing(open_database(path, mode, timeout=5)) as conn:
        yield conn


def check(conn):
    if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise MigrationError("Database integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone():
        raise MigrationError("Database foreign key check failed")


def schema(path):
    with database(path, "ro") as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def backup_file(source, target):
    with database(source, "ro") as src, database(target, "rwc") as dst:
        src.backup(dst)
        # A self-contained backup must not depend on WAL sidecars.
        dst.execute("PRAGMA journal_mode=DELETE")
        check(dst)


def restore_file(source, target):
    with database(source, "ro") as src, database(target, "rw") as dst:
        src.backup(dst)
        check(dst)


def apply_schema(path, name, secret, installation):
    version = schema(path) if path.exists() else 0
    if not 0 <= version <= SCHEMA_VERSION:
        raise MigrationError("Unsupported database schema")
    # Sequential steps allow a release to skip any intermediate image.
    if version == 0:
        if name == "app":
            Database(path).initialize()
        else:
            ControlStore(path, secret).initialize()
        with database(path, "rw") as conn:
            conn.execute("PRAGMA user_version=1")
        version = 1
    if version == 1:
        # Store the component identity alongside the schema revision so that
        # mounting the other service's database fails before admitting writes.
        with database(path, "rw") as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE penczreq_component (name TEXT PRIMARY KEY, installation TEXT NOT NULL)")
            conn.execute("INSERT INTO penczreq_component VALUES (?,?)", (name, installation))
            conn.execute("PRAGMA user_version=2")


def validate_pair(paths, installation):
    for name, path in paths.items():
        with database(path, "ro") as conn:
            check(conn)
            if conn.execute("PRAGMA user_version").fetchone() != (SCHEMA_VERSION,):
                raise MigrationError("Unexpected schema version")
            if conn.execute("SELECT name,installation FROM penczreq_component").fetchall() != [(name, installation)]:
                raise MigrationError("Database mounted for the wrong component")


def transition(journal, run, phase, hashes=None):
    with journal:
        journal.execute("UPDATE runs SET phase=?, hashes=COALESCE(?,hashes) WHERE id=?",
                        (phase, json.dumps(hashes) if hashes else None, run))
        journal.execute("INSERT INTO events(run,phase) VALUES (?,?)", (run, phase))


def recover(journal, run, folder, paths, hashes):
    transition(journal, run, "restoring")
    if not hashes or set(hashes) != set(STORES):
        raise MigrationError("Incomplete recovery manifest")
    for name in STORES:
        if digest(folder / (name + ".db")) != hashes[name]:
            raise MigrationError("Damaged recovery set; keep both services stopped")
    for name in STORES:
        restore_file(folder / (name + ".db"), paths[name])
    transition(journal, run, "failed")


def coordinate(settings, release=RELEASE):
    paths = {"app": settings.database_path, "control": settings.control_database_path}
    journal_path = settings.control_data_dir / ".migrations.db"
    # A Control-only restart must coexist with the current Public process.
    with gate(settings.data_dir):
        if current(settings, release):
            with database(journal_path, "ro") as journal:
                row = journal.execute("SELECT target,phase FROM runs ORDER BY id DESC LIMIT 1").fetchone()
                installation = journal.execute("SELECT id FROM installation").fetchone()[0]
            if row != (release, "ready"):
                raise MigrationError("Recovery journal does not match the ready release")
            validate_pair(paths, installation)
            return
    if ready_path(settings).exists() and not journal_path.is_file():
        raise MigrationError("Missing Control journal for an enrolled installation")
    with gate(settings.data_dir, exclusive=True), database(journal_path, "rwc") as journal:
        journal.row_factory = sqlite3.Row
        journal.execute("PRAGMA synchronous=FULL")
        journal.executescript("""
            CREATE TABLE IF NOT EXISTS installation (id TEXT PRIMARY KEY);
            INSERT INTO installation SELECT lower(hex(randomblob(16))) WHERE NOT EXISTS (SELECT 1 FROM installation);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, target TEXT NOT NULL, origin TEXT NOT NULL,
                phase TEXT NOT NULL, hashes TEXT);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, run INTEGER NOT NULL, phase TEXT NOT NULL,
                created TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
        installation = journal.execute("SELECT id FROM installation").fetchone()[0]
        row = journal.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if row and row["phase"] == "ready" and row["target"] == release:
            validate_pair(paths, installation)
            write_json(ready_path(settings), [release, SCHEMA_VERSION])
            return
        ready_path(settings).unlink(missing_ok=True)
        sync_directory(settings.data_dir)
        if row and row["phase"] not in ("ready", "failed") and row["target"] != release:
            raise MigrationError("Resume the interrupted release before changing images")
        if row is None or row["phase"] in ("ready", "failed"):
            existing = [p.is_file() for p in paths.values()]
            if any(existing) != all(existing) or (row and not all(existing)):
                raise MigrationError("Incomplete database pair")
            if row and row["phase"] == "failed" and row["target"] == release:
                permit = settings.control_data_dir / f".retry-migration-{row['id']}"
                if not permit.is_file():
                    raise MigrationError(f"Migration {row['id']} restored. Inspect failure before authorizing retry.")
                permit.unlink()
            origin = "fresh" if not any(existing) else ("legacy" if not row else "update")
            if origin == "legacy":
                if not (settings.control_data_dir / ".legacy-writers-stopped").is_file():
                    raise MigrationError("Stop all legacy writers and confirm the offline handover")
                for name, table in (("app", "requests"), ("control", "control_users")):
                    with database(paths[name], "ro") as conn:
                        if schema(paths[name]) != 0 or not conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                        ).fetchone():
                            raise MigrationError("Unrecognized legacy database")
            if origin == "update":
                revisions = {schema(path) for path in paths.values()}
                if len(revisions) != 1 or max(revisions) > SCHEMA_VERSION:
                    raise MigrationError("Inconsistent or newer schemas require manual inspection")
                if revisions == {SCHEMA_VERSION}:
                    validate_pair(paths, installation)
            with journal:
                run = journal.execute("INSERT INTO runs(target,origin,phase) VALUES (?,?,?)",
                                      (release, origin, "preparing")).lastrowid
                journal.execute("INSERT INTO events(run,phase) VALUES (?,?)", (run, "preparing"))
            row = journal.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
        run, phase = row["id"], row["phase"]
        folder = settings.backups_dir / "pre-update" / f"{installation}-{run}"
        if phase not in ("preparing", "applying", "restoring"):
            raise MigrationError("Unknown migration phase; inspect the Control journal")
        if phase in ("applying", "restoring"):
            recover(journal, run, folder, paths, json.loads(row["hashes"]))
            raise MigrationError("Interrupted migration restored; review before retry")
        if row["origin"] != "fresh" and phase == "preparing":
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            sync_directory(folder.parent.parent)
            sync_directory(folder.parent)
            hashes = {}
            for name, path in paths.items():
                target = folder / (name + ".db")
                backup_file(path, target)
                hashes[name] = digest(target)
            previous = journal.execute(
                "SELECT target FROM runs WHERE phase='ready' AND id < ? ORDER BY id DESC LIMIT 1",
                (run,),
            ).fetchone()
            manifest = {"target_release": release, "source_release": previous[0] if previous else "legacy",
                        "run": run, "installation": installation, "sha256": hashes,
                        "source_schemas": {name: schema(folder / (name + ".db")) for name in STORES}}
            write_json(folder / "manifest.json", manifest)
            transition(journal, run, "applying", hashes)
        try:
            for name, path in paths.items():
                apply_schema(path, name, settings.control_session_secret, installation)
            app_db = Database(paths["app"])
            audit = SecurityAudit(app_db, settings.logs_dir)
            LoginProtection(app_db, audit).migrate_legacy_lockouts()
            upgraded = Repository(app_db).require_password_policy_upgrade()
            if upgraded:
                audit.emit("password_policy_upgrade_required", severity="warning",
                           details={"accounts": upgraded, "minimum_length": 15})
            validate_pair(paths, installation)
            if row["origin"] != "fresh":
                # Both stores are backed up and writers excluded until ready.
                # Recovery restores these inbox entries with the rest of the pair.
                version = release.partition("@")[0]
                notify_users_about_update(
                    app_db, version, *update_notification_bodies(version, "pl"),
                    *update_notification_bodies(version, "en"), release_id=release,
                )
        except Exception:
            if row["origin"] != "fresh":
                recover(journal, run, folder, paths, hashes)
            raise
        transition(journal, run, "ready")
        write_json(ready_path(settings), [release, SCHEMA_VERSION])
        (settings.control_data_dir / ".legacy-writers-stopped").unlink(missing_ok=True)
