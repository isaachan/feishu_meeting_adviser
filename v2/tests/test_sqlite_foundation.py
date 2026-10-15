from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout

from feishu_meeting_adviser.__main__ import main
from feishu_meeting_adviser.adapters.persistence.factory import StorageFactory
from feishu_meeting_adviser.adapters.persistence.sqlite import Migration, MigrationRunner, SQLiteDatabase, StorageError
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.infrastructure.lock_policy import LockRank, MIGRATION_LOCK_ORDER, validate_lock_order


class SQLiteFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def config(self):
        path = self.root / 'config.yaml'
        path.write_text('app:\n  id: synthetic\n  secret: synthetic\nmeeting:\n  meeting_no: "012345678"\n')
        path.chmod(0o600)
        return load_config(path)

    def test_readonly_open_never_creates_or_writes_database(self):
        path = self.root / 'private' / 'source.sqlite3'
        with self.assertRaises(StorageError):
            SQLiteDatabase.open_readonly(path)
        self.assertFalse(path.exists())

        writable = SQLiteDatabase.open_and_migrate(path)
        writable.connection.execute('CREATE TABLE sample(value TEXT)')
        writable.close()
        readonly = SQLiteDatabase.open_readonly(path)
        self.addCleanup(readonly.close)
        self.assertEqual(readonly.connection.execute('SELECT count(*) FROM sample').fetchone()[0], 0)
        with self.assertRaises(sqlite3.OperationalError):
            readonly.connection.execute('INSERT INTO sample VALUES (\'forbidden\')')

    def test_migration_runner_rolls_back_entire_chain(self):
        path = self.root / 'private' / 'migration.sqlite3'
        database = SQLiteDatabase.open_and_migrate(path)
        self.addCleanup(database.close)

        def fail(connection):
            connection.execute('CREATE TABLE incomplete(value TEXT)')
            raise RuntimeError('synthetic migration failure')

        runner = MigrationRunner([Migration(0, 1, lambda db: db.execute('CREATE TABLE first(value TEXT)')),
                                  Migration(1, 2, fail)])
        with self.assertRaises(RuntimeError):
            runner.migrate(database, current_version=0, target_version=2)
        self.assertEqual(database.connection.execute('PRAGMA user_version').fetchone()[0], 0)
        self.assertEqual(database.connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('first','incomplete')").fetchone()[0], 0)

    def test_factory_carries_all_storage_limits(self):
        config = replace(self.config(), database_capacity_bytes=2_000_000, content_capacity_bytes=12345)
        store = StorageFactory(config).source()
        self.addCleanup(store.close)
        self.assertEqual(store.max_database_bytes, 2_000_000)
        self.assertEqual(store.max_payload_bytes, 12345)

    def test_status_without_database_is_readonly(self):
        config = self.config()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--config', str(config.path), 'status']), 0)
        self.assertEqual(json.loads(output.getvalue()), {'session_phase':'none'})
        self.assertFalse(config.data_directory.exists())

    def test_migration_lock_policy_has_one_global_order(self):
        for ranks in MIGRATION_LOCK_ORDER.values():
            validate_lock_order(*ranks)
        with self.assertRaises(ValueError):
            validate_lock_order(LockRank.MAINTENANCE, LockRank.ADVISER)
