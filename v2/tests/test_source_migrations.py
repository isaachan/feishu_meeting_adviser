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

from feishu_meeting_adviser.adapters.persistence.source_migrations import (
    RUNNER,
    SOURCE_SCHEMA_VERSION,
    TRACKED_COLUMNS,
)
from feishu_meeting_adviser.adapters.persistence.sqlite import Migration, StorageError
from feishu_meeting_adviser.storage import Store


FIXTURES = Path(__file__).parent / 'fixtures'


class SourceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data' / 'source.sqlite3'
        self.path.parent.mkdir(mode=0o700)

    def materialize(self, name: str) -> None:
        db = sqlite3.connect(self.path)
        db.executescript((FIXTURES / name).read_text())
        db.close()
        self.path.chmod(0o600)

    def assert_current(self) -> None:
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SOURCE_SCHEMA_VERSION)
            self.assertEqual(
                db.execute("SELECT checkpoint FROM meetings WHERE meeting_id='synthetic-meeting'").fetchone()[0],
                '{}',
            )
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            self.assertIn('accounting', tables)
            triggers = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertEqual(len(triggers), len(TRACKED_COLUMNS) * 3)
            self.assertGreaterEqual(db.execute(
                'SELECT bytes FROM accounting WHERE singleton=1').fetchone()[0], 2)
        finally:
            db.close()

    def test_every_supported_fixture_upgrades_and_preserves_data(self):
        for version in range(1, SOURCE_SCHEMA_VERSION + 1):
            with self.subTest(version=version):
                self.path = self.path.with_name(f'source-v{version}.sqlite3')
                self.materialize(f'source_v{version}.sql')
                store = Store(self.path)
                store.close()
                self.assert_current()
                if version == 3:
                    check = sqlite3.connect(self.path)
                    try:
                        self.assertEqual(check.execute(
                            "SELECT active FROM memberships WHERE meeting_id='synthetic-meeting'").fetchone()[0], 1)
                    finally:
                        check.close()

    def test_current_schema_reopens_idempotently(self):
        self.materialize('source_v5.sql')
        for _ in range(2):
            store = Store(self.path)
            store.close()
        self.assert_current()

    def test_readonly_old_schema_refuses_without_migrating(self):
        self.materialize('source_v1.sql')
        with self.assertRaises(StorageError):
            Store.open_readonly(self.path)
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertNotIn('accounting', {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")})
        finally:
            db.close()

    def test_future_and_damaged_schema_fail_without_repair(self):
        for setup in ('future', 'damaged'):
            with self.subTest(setup=setup):
                self.path = self.path.with_name(f'{setup}.sqlite3')
                db = sqlite3.connect(self.path)
                if setup == 'future':
                    db.execute('PRAGMA user_version=99')
                else:
                    db.execute('CREATE TABLE meetings(unexpected TEXT)')
                db.close()
                self.path.chmod(0o600)
                with self.assertRaises(StorageError):
                    Store(self.path)
                check = sqlite3.connect(self.path)
                try:
                    self.assertEqual(check.execute('PRAGMA user_version').fetchone()[0],
                                     99 if setup == 'future' else 0)
                    self.assertEqual(
                        {row[0] for row in check.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")},
                        set() if setup == 'future' else {'meetings'},
                    )
                finally:
                    check.close()

    def test_damaged_current_schema_is_not_silently_repaired(self):
        self.materialize('source_v5.sql')
        db = sqlite3.connect(self.path)
        db.execute('DROP TRIGGER account_meetings_insert')
        db.close()
        with self.assertRaises(StorageError):
            Store(self.path)
        check = sqlite3.connect(self.path)
        try:
            self.assertNotIn('account_meetings_insert', {row[0] for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")})
        finally:
            check.close()

    def test_damaged_legacy_schema_fails_before_any_migration(self):
        self.materialize('source_v1.sql')
        db = sqlite3.connect(self.path)
        db.execute('DROP INDEX deliveries_meeting')
        db.close()
        with self.assertRaises(StorageError):
            Store(self.path)
        check = sqlite3.connect(self.path)
        try:
            self.assertEqual(check.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertNotIn('risks', {row[0] for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")})
        finally:
            check.close()

    def test_interrupted_migration_rolls_back_schema_and_version(self):
        self.materialize('source_v4.sql')
        original = RUNNER.migrations[4]

        def fail(connection):
            original.apply(connection)
            raise RuntimeError('synthetic migration interruption')

        with patch.dict(RUNNER.migrations, {4: Migration(4, 5, fail)}):
            with self.assertRaises(RuntimeError):
                Store(self.path)
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertNotIn('accounting', {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")})
        finally:
            db.close()

    def test_concurrent_startup_runs_each_needed_migration_once(self):
        self.materialize('source_v1.sql')
        calls = []
        lock = threading.Lock()
        wrapped = {}
        for version, migration in RUNNER.migrations.items():
            def apply(connection, migration=migration, version=version):
                with lock:
                    calls.append(version)
                time.sleep(0.02)
                migration.apply(connection)
            wrapped[version] = Migration(migration.from_version, migration.to_version, apply)

        def open_store(_):
            store = Store(self.path)
            store.close()

        with patch.dict(RUNNER.migrations, wrapped, clear=True):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(open_store, range(2)))
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assert_current()

    def test_two_processes_can_start_against_one_legacy_database(self):
        self.materialize('source_v1.sql')
        code = ('from pathlib import Path; '
                'from feishu_meeting_adviser.storage import Store; '
                f's=Store(Path({str(self.path)!r})); s.close()')
        processes = [subprocess.Popen(
            [sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(2)]
        results = [process.communicate(timeout=10) + (process.returncode,) for process in processes]
        self.assertEqual(results, [(b'', b'', 0), (b'', b'', 0)])
        self.assert_current()


if __name__ == '__main__':
    unittest.main()
