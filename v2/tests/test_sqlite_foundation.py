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
from feishu_meeting_adviser.adapters.persistence.sqlite import SQLiteDatabase, StorageError
from feishu_meeting_adviser.config import load_config


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

        writable = SQLiteDatabase.initialize(path)
        writable.connection.execute('CREATE TABLE sample(value TEXT)')
        writable.close()
        readonly = SQLiteDatabase.open_readonly(path)
        self.addCleanup(readonly.close)
        self.assertEqual(readonly.connection.execute('SELECT count(*) FROM sample').fetchone()[0], 0)
        with self.assertRaises(sqlite3.OperationalError):
            readonly.connection.execute('INSERT INTO sample VALUES (\'forbidden\')')

    def test_current_open_requires_an_existing_database(self):
        path = self.root / 'private' / 'source.sqlite3'
        with self.assertRaises(StorageError):
            SQLiteDatabase.open_current(path)
        self.assertFalse(path.exists())

    def test_factory_carries_all_storage_limits(self):
        config = replace(self.config(), database_capacity_bytes=2_000_000, content_capacity_bytes=12345)
        factory = StorageFactory(config)
        factory.initialize_source()
        store = factory.source()
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
