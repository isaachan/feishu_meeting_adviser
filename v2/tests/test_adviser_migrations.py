from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from feishu_meeting_adviser.adviser_store import AdviserStore
from feishu_meeting_adviser.adapters.persistence.adviser_migrations import ADVISER_SCHEMA_VERSION, RUNNER
from feishu_meeting_adviser.adapters.persistence.sqlite import Migration, StorageError


FIXTURES = Path(__file__).parent / 'fixtures'


class AdviserMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data' / 'adviser.sqlite3'
        self.path.parent.mkdir(mode=0o700)

    def materialize(self, name):
        db = sqlite3.connect(self.path)
        db.executescript((FIXTURES / name).read_text())
        db.close()
        self.path.chmod(0o600)

    def assert_current(self):
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], ADVISER_SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT generation FROM meetings WHERE meeting_id='synthetic-meeting'").fetchone()[0], 1)
            self.assertIn('material_version', {row[1] for row in db.execute('PRAGMA table_info(meetings)')})
            self.assertIn('context_version', {row[1] for row in db.execute('PRAGMA table_info(outbox)')})
        finally:
            db.close()

    def test_every_verified_unversioned_fixture_upgrades_and_preserves_data(self):
        for fixture in ('adviser_legacy_base.sql','adviser_join_outbox.sql','adviser_materials_unversioned.sql'):
            with self.subTest(fixture=fixture):
                if self.path.exists():
                    self.path.unlink()
                self.materialize(fixture)
                store = AdviserStore(self.path)
                store.close()
                self.assert_current()

    def test_readonly_old_schema_refuses_without_migrating(self):
        self.materialize('adviser_legacy_base.sql')
        with self.assertRaises(StorageError):
            AdviserStore.open_readonly(self.path)
        db = sqlite3.connect(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
        self.assertNotIn('outbox', {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})

    def test_current_schema_reopens_idempotently(self):
        self.materialize('adviser_materials_unversioned.sql')
        for _ in range(2):
            store = AdviserStore(self.path)
            store.close()
        self.assert_current()

    def test_future_and_damaged_schema_fail_without_repair(self):
        for setup in ('future','damaged'):
            with self.subTest(setup=setup):
                if self.path.exists():
                    self.path.unlink()
                db = sqlite3.connect(self.path)
                if setup == 'future':
                    db.execute('PRAGMA user_version=99')
                else:
                    db.execute('CREATE TABLE meetings(unexpected TEXT)')
                db.close()
                self.path.chmod(0o600)
                with self.assertRaises(StorageError):
                    AdviserStore(self.path)
                check = sqlite3.connect(self.path)
                tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                version = check.execute('PRAGMA user_version').fetchone()[0]
                check.close()
                self.assertEqual(version, 99 if setup == 'future' else 0)
                self.assertEqual(tables, set() if setup == 'future' else {'meetings'})

    def test_failed_second_migration_rolls_back_first_migration(self):
        self.materialize('adviser_join_outbox.sql')
        db = sqlite3.connect(self.path)
        db.execute('PRAGMA user_version=1')
        db.commit()
        db.close()
        original = RUNNER.migrations[1]

        def fail(connection):
            original.apply(connection)
            raise RuntimeError('synthetic migration interruption')

        with patch.dict(RUNNER.migrations, {1: Migration(1, 2, fail)}):
            with self.assertRaises(RuntimeError):
                AdviserStore(self.path)
        check = sqlite3.connect(self.path)
        self.addCleanup(check.close)
        self.assertEqual(check.execute('PRAGMA user_version').fetchone()[0], 1)
        self.assertNotIn('meeting_materials', {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")})

    def test_concurrent_startup_runs_each_migration_once(self):
        self.materialize('adviser_legacy_base.sql')
        calls = []
        lock = threading.Lock()
        wrapped = {}
        for version, migration in RUNNER.migrations.items():
            def apply(connection, migration=migration, version=version):
                with lock:
                    calls.append(version)
                time.sleep(0.05)
                migration.apply(connection)
            wrapped[version] = Migration(migration.from_version, migration.to_version, apply)

        def open_store():
            store = AdviserStore(self.path)
            store.close()

        with patch.dict(RUNNER.migrations, wrapped, clear=True):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: open_store(), range(2)))
        self.assertEqual(calls, [0, 1])
        self.assert_current()

    def test_two_processes_can_start_against_one_legacy_database(self):
        self.materialize('adviser_legacy_base.sql')
        code = ('from pathlib import Path; '
                'from feishu_meeting_adviser.adviser_store import AdviserStore; '
                f's=AdviserStore(Path({str(self.path)!r})); s.close()')
        processes = [subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for _ in range(2)]
        results = [process.communicate(timeout=10) + (process.returncode,) for process in processes]
        self.assertEqual(results, [(b'', b'', 0), (b'', b'', 0)])
        self.assert_current()
