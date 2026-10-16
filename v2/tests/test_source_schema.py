from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from feishu_meeting_adviser.adapters.persistence import source_schema
from feishu_meeting_adviser.adapters.persistence.sqlite import StorageError
from feishu_meeting_adviser.storage import Store


class SourceSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data' / 'source.sqlite3'
        self.path.parent.mkdir(mode=0o700)

    def schema_state(self):
        db = sqlite3.connect(self.path)
        try:
            return (
                db.execute('PRAGMA user_version').fetchone()[0],
                tuple(db.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()),
            )
        finally:
            db.close()

    def test_store_requires_explicit_initialization(self):
        with self.assertRaises(StorageError):
            Store(self.path)
        self.assertFalse(self.path.exists())

        Store.initialize(self.path)
        store = Store(self.path)
        store.close()
        self.assertEqual(self.schema_state()[0], source_schema.SOURCE_SCHEMA_VERSION)

    def test_explicit_initialization_is_idempotent(self):
        Store.initialize(self.path)
        before = self.schema_state()
        Store.initialize(self.path)
        self.assertEqual(self.schema_state(), before)

    def test_runtime_reopen_never_calls_initializer(self):
        Store.initialize(self.path)
        with patch.object(source_schema, 'initialize_source_schema',
                          side_effect=AssertionError('runtime attempted schema initialization')):
            store = Store(self.path)
            store.close()

    def test_noncurrent_and_damaged_schemas_fail_closed_without_changes(self):
        for setup in ('legacy', 'future', 'damaged'):
            with self.subTest(setup=setup):
                self.path = self.path.with_name(f'{setup}.sqlite3')
                db = sqlite3.connect(self.path)
                if setup == 'legacy':
                    db.execute('CREATE TABLE legacy(value TEXT)')
                    db.execute('PRAGMA user_version=1')
                elif setup == 'future':
                    db.execute('PRAGMA user_version=99')
                else:
                    db.execute('CREATE TABLE meetings(unexpected TEXT)')
                    db.execute(f'PRAGMA user_version={source_schema.SOURCE_SCHEMA_VERSION}')
                db.close()
                self.path.chmod(0o600)
                before = self.schema_state()
                with self.assertRaises(StorageError):
                    Store.initialize(self.path)
                self.assertEqual(self.schema_state(), before)
                with self.assertRaises(StorageError):
                    Store(self.path)
                self.assertEqual(self.schema_state(), before)

    def test_interrupted_initialization_rolls_back_every_schema_change(self):
        original = source_schema._create_accounting_triggers

        def fail(connection):
            original(connection)
            raise RuntimeError('synthetic initialization interruption')

        with patch.object(source_schema, '_create_accounting_triggers', side_effect=fail):
            with self.assertRaises(RuntimeError):
                Store.initialize(self.path)
        self.assertEqual(self.schema_state(), (0, ()))
        Store.initialize(self.path)
        store = Store(self.path)
        store.close()

    def test_concurrent_explicit_initialization_produces_one_current_schema(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: Store.initialize(self.path), range(2)))
        store = Store(self.path)
        store.close()
        self.assertEqual(self.schema_state()[0], source_schema.SOURCE_SCHEMA_VERSION)


if __name__ == '__main__':
    unittest.main()
