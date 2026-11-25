"""The same fault matrix runs under pytest and in a disposable TrueNAS workload."""
import asyncio
import errno
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from request_app import migrations as m
from request_app.maintenance import prune_backups


def settings(root):
    paths = [root / name for name in ("app", "control", "backups")]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    app, control, backups = paths
    return SimpleNamespace(data_dir=app, database_path=app / "app.db", control_data_dir=control,
                           control_database_path=control / "control.db", backups_dir=backups,
                           logs_dir=app / "logs", control_session_secret=secrets.token_hex(32))


def dump(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("PRAGMA user_version").fetchone(), list(db.iterdump())


def worker_env():
    return dict(os.environ, PYTHONPATH=str(Path(m.__file__).resolve().parents[1]))


def worker(root, fault, release):
    cfg = settings(root)
    original_apply, original_restore = m.apply_schema, m.restore_file
    original_transition, original_backup = m.transition, m.backup_file

    def apply(path, name, *args):
        original_apply(path, name, *args)
        if name == "app" and fault == "apply":
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE incomplete_migration(id INTEGER)")
            os._exit(86)

    def restore(src, dst):
        original_restore(src, dst)
        if dst == cfg.database_path and fault == "restore":
            os._exit(86)

    def transition(db, run, phase, *args):
        original_transition(db, run, phase, *args)
        if fault == phase:
            os._exit(86)

    def backup(src, dst):
        original_backup(src, dst)
        if src == cfg.database_path and fault == "backup":
            os._exit(86)

    with patch.object(m, "apply_schema", apply), patch.object(m, "restore_file", restore), \
         patch.object(m, "transition", transition), patch.object(m, "backup_file", backup):
        m.coordinate(cfg, release)


class StartupMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = settings(self.root)
        self.paths = (self.cfg.database_path, self.cfg.control_database_path)

    def crash(self, fault, release="B"):
        p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker",
                            str(self.root), fault, release], env=worker_env(), timeout=30)
        self.assertEqual(p.returncode, 86)

    def phase(self):
        with m.database(self.cfg.control_data_dir / ".migrations.db", "ro") as db:
            return db.execute("SELECT phase FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]

    def pair(self):
        return [dump(path) for path in self.paths]

    def test_fresh_restart_skip_and_protected_retention(self):
        m.coordinate(self.cfg, "A")
        with m.writer_session(self.cfg, "A"):
            with closing(sqlite3.connect(self.paths[0])) as db, db:
                db.execute("CREATE TABLE user_record(value TEXT)")
                db.execute("INSERT INTO user_record VALUES ('keep')")
            m.coordinate(self.cfg, "A")
        self.assertFalse((self.cfg.backups_dir / "pre-update").exists())
        before = self.pair()
        m.coordinate(self.cfg, "D")
        manifests = list(self.cfg.backups_dir.rglob("manifest.json"))
        self.assertEqual(len(manifests), 1)
        folder = manifests[0].parent
        manifest = json.loads(manifests[0].read_text())
        for name, original in zip(m.STORES, before):
            path = folder / (name + ".db")
            self.assertEqual(dump(path), original)
            self.assertEqual(m.digest(path), manifest["sha256"][name])
            os.utime(path, (1, 1))
        prune_backups(self.cfg.backups_dir, 3)
        self.assertEqual(len(list(folder.glob("*.db"))), 2)
        with self.assertRaises(m.MigrationError), m.writer_session(self.cfg, "A"):
            pass

    def test_legacy_requires_stopped_handover_and_preserves_old_schema(self):
        m.Database(self.paths[0]).initialize()
        m.ControlStore(self.paths[1], self.cfg.control_session_secret).initialize()
        with closing(sqlite3.connect(self.paths[0])) as db, db:
            db.execute("ALTER TABLE requests DROP COLUMN title_en")
        before = self.pair()
        with self.assertRaisesRegex(m.MigrationError, "legacy writers"):
            m.coordinate(self.cfg)
        self.assertEqual(self.pair(), before)
        (self.cfg.control_data_dir / ".legacy-writers-stopped").touch()
        m.coordinate(self.cfg)
        folder = next(self.cfg.backups_dir.rglob("manifest.json")).parent
        self.assertEqual([dump(folder / (n + ".db")) for n in m.STORES], before)
        self.assertEqual([m.schema(p) for p in self.paths], [2, 2])
        with m.database(self.paths[0], "ro") as db:
            self.assertIn("title_en", [r[1] for r in db.execute("PRAGMA table_info(requests)")])

    def test_uat_does_not_deliver_notifications(self):
        from request_app.push import PushService
        service = object.__new__(PushService)
        service._public_key = "fixture"
        with patch.dict(os.environ, WEB_PUSH_DELIVERY="disabled"):
            self.assertEqual(service.deliver_pending(sender=self.fail), 0)

    def test_missing_database(self):
        m.Database(self.paths[0]).initialize()
        with self.assertRaisesRegex(m.MigrationError, "Incomplete"):
            m.coordinate(self.cfg)
        self.assertFalse(self.paths[1].exists())

    def test_crash_while_creating_fresh(self):
        self.crash("apply", "A")
        m.coordinate(self.cfg, "A")
        self.assertEqual(self.phase(), "ready")
        self.assertEqual([m.schema(p) for p in self.paths], [2, 2])

    def test_full_second_backup_and_crash_during_backup(self):
        m.coordinate(self.cfg, "A")
        before = self.pair()
        self.crash("backup")
        original = m.backup_file

        def full(src, dst):
            if src == self.paths[1]:
                raise OSError(errno.ENOSPC, "no space for second backup")
            original(src, dst)
        with patch.object(m, "backup_file", full), self.assertRaises(OSError):
            m.coordinate(self.cfg, "B")
        self.assertEqual(self.phase(), "preparing")
        self.assertEqual(self.pair(), before)
        self.assertFalse(list(self.cfg.backups_dir.rglob("manifest.json")))
        m.coordinate(self.cfg, "B")
        self.assertEqual(self.phase(), "ready")

    @unittest.skipUnless(os.getenv("UAT_SMALL_BACKUP_FS"), "requires a disposable 2 MiB filesystem")
    def test_actual_filesystem_full_on_second_database(self):
        m.coordinate(self.cfg, "A")
        with closing(sqlite3.connect(self.paths[1])) as db, db:
            db.execute("CREATE TABLE payload(value BLOB)")
            db.execute("INSERT INTO payload VALUES (randomblob(3145728))")
        before = self.pair()
        with tempfile.TemporaryDirectory(dir=os.environ["UAT_SMALL_BACKUP_FS"]) as small:
            self.cfg.backups_dir = Path(small)
            with self.assertRaises((OSError, sqlite3.OperationalError)):
                m.coordinate(self.cfg, "B")
            self.assertTrue(list(Path(small).rglob("app.db")))
            self.assertFalse(list(Path(small).rglob("manifest.json")))
            self.assertEqual(self.phase(), "preparing")
            self.assertEqual(before, self.pair())

    def test_partial_migration_and_repeated_interruption_during_restore(self):
        m.coordinate(self.cfg, "A")
        before = self.pair()
        self.crash("apply")
        self.assertNotEqual(self.pair(), before)
        for _ in range(2):
            self.crash("restore")
            self.assertEqual(self.phase(), "restoring")
            with self.assertRaises(m.MigrationError), m.writer_session(self.cfg, "B"):
                pass
        with self.assertRaisesRegex(m.MigrationError, "restored"):
            m.coordinate(self.cfg, "B")
        self.assertEqual(self.pair(), before)
        self.assertEqual(self.phase(), "failed")
        with self.assertRaisesRegex(m.MigrationError, "retry"):
            m.coordinate(self.cfg, "B")
        (self.cfg.control_data_dir / ".retry-migration-2").touch()
        m.coordinate(self.cfg, "B")
        self.assertEqual(self.phase(), "ready")

    def test_crash_after_sealing_pair_before_any_schema_change(self):
        m.coordinate(self.cfg, "A")
        before = self.pair()
        self.crash("applying")
        self.assertEqual(before, self.pair())
        self.crash("failed")
        self.assertEqual(self.phase(), "failed")
        self.assertEqual(before, self.pair())

    def test_second_schema_step_failure_restores_the_original_pair(self):
        m.coordinate(self.cfg, "A")
        before = self.pair()
        original = m.apply_schema

        def fail(path, name, *args):
            original(path, name, *args)
            if name == "app":
                with m.database(path, "rw") as db, db:
                    db.execute("CREATE TABLE not_committed_to_users(id INTEGER)")
            else:
                raise m.MigrationError("test failure")
        with patch.object(m, "apply_schema", fail), self.assertRaisesRegex(m.MigrationError, "test failure"):
            m.coordinate(self.cfg, "B")
        self.assertEqual(self.phase(), "failed")
        self.assertEqual(before, self.pair())

    def test_inconsistent_or_future_store_fails_before_backup(self):
        m.coordinate(self.cfg, "A")
        with m.database(self.paths[1], "rw") as db:
            db.execute("PRAGMA user_version=80")
        before = self.pair()
        with self.assertRaisesRegex(m.MigrationError, "schemas"):
            m.coordinate(self.cfg, "B")
        self.assertFalse(list(self.cfg.backups_dir.rglob("manifest.json")))
        self.assertEqual(before, self.pair())

    def test_damaged_backup_stops_before_restoring_either_file(self):
        m.coordinate(self.cfg, "A")
        self.crash("apply")
        before = self.pair()
        path = next(self.cfg.backups_dir.rglob("control.db"))
        path.write_bytes(b"broken test backup")
        with self.assertRaisesRegex(m.MigrationError, "Damaged"):
            m.coordinate(self.cfg, "B")
        self.assertEqual(self.pair(), before)

    def test_interruption_after_commit_never_rewinds_user_writes(self):
        m.coordinate(self.cfg, "A")
        self.crash("ready")
        m.coordinate(self.cfg, "B")
        with m.writer_session(self.cfg, "B"), closing(sqlite3.connect(self.paths[0])) as conn, conn:
            conn.execute("CREATE TABLE accepted(value TEXT)")
            conn.execute("INSERT INTO accepted VALUES ('keep')")
        before = self.pair()
        m.coordinate(self.cfg, "B")
        self.assertEqual(self.pair(), before)
        self.assertEqual(len(list(self.cfg.backups_dir.rglob("manifest.json"))), 1)

    def test_other_process_cannot_migrate_under_writer(self):
        m.coordinate(self.cfg, "A")
        with m.writer_session(self.cfg, "A"):
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker",
                                     str(self.root), "none", "B"], capture_output=True, text=True, env=worker_env(), timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("locked", result.stderr)
        self.assertTrue(m.current(self.cfg, "A"))

    def test_cli_and_background_thread_refuse_closed_gate(self):
        from request_app import cli
        with patch.object(cli, "load_settings", return_value=self.cfg), \
             patch.object(sys, "argv", ["cli", "init-admin", "foo"]), \
             patch.object(cli, "init_admin") as command, self.assertRaises(m.MigrationError):
            cli.main()
        command.assert_not_called()
        with self.assertRaises(m.MigrationError):
            asyncio.run(m.writer_thread(self.cfg, self.fail, "unfenced background write"))


if __name__ == "__main__":
    if sys.argv[1:2] == ["worker"]:
        worker(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    else:
        unittest.main()
