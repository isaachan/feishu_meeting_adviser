from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from feishu_meeting_adviser.adapters.persistence import adviser_schema
from feishu_meeting_adviser.adapters.persistence.sqlite import StorageError
from feishu_meeting_adviser.adviser_store import AdviserStore


class AdviserSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data' / 'adviser.sqlite3'
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
            AdviserStore(self.path)
        self.assertFalse(self.path.exists())

        AdviserStore.initialize(self.path)
        store = AdviserStore(self.path)
        store.close()
        self.assertEqual(self.schema_state()[0], adviser_schema.ADVISER_SCHEMA_VERSION)

    def test_explicit_initialization_is_idempotent(self):
        AdviserStore.initialize(self.path)
        before = self.schema_state()
        AdviserStore.initialize(self.path)
        self.assertEqual(self.schema_state(), before)

    def test_v2_upgrade_preserves_existing_reply(self):
        old_outbox = next(statement for statement in adviser_schema.CURRENT_SCHEMA
                          if statement.startswith('CREATE TABLE outbox'))
        old_outbox = old_outbox.replace(
            ",\n        answer_uuid TEXT NOT NULL, channel TEXT NOT NULL DEFAULT 'meeting',\n        recipient_id TEXT", '')
        db = sqlite3.connect(self.path)
        for statement in adviser_schema.CURRENT_SCHEMA:
            if statement.startswith('CREATE INDEX outbox_answer'):
                continue
            db.execute(old_outbox if statement.startswith('CREATE TABLE outbox') else statement)
        db.execute('PRAGMA user_version=2')
        db.execute('''INSERT INTO outbox(uuid,meeting_id,kind,content,evidence,generation,created_ns)
                      VALUES ('synthetic-uuid','m1','reply','synthetic answer','{}',1,1)''')
        db.commit()
        db.close()
        self.path.chmod(0o600)
        AdviserStore.initialize(self.path)
        store = AdviserStore(self.path)
        try:
            row = store.db.execute('SELECT * FROM outbox WHERE uuid=?', ('synthetic-uuid',)).fetchone()
            self.assertEqual((row['answer_uuid'], row['channel'], row['recipient_id'], row['content']),
                             ('synthetic-uuid', 'meeting', None, 'synthetic answer'))
        finally:
            store.close()

    def test_runtime_reopen_never_calls_initializer(self):
        AdviserStore.initialize(self.path)
        with patch.object(adviser_schema, 'initialize_adviser_schema',
                          side_effect=AssertionError('runtime attempted schema initialization')):
            store = AdviserStore(self.path)
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
                    db.execute(f'PRAGMA user_version={adviser_schema.ADVISER_SCHEMA_VERSION}')
                db.close()
                self.path.chmod(0o600)
                before = self.schema_state()
                with self.assertRaises(StorageError):
                    AdviserStore.initialize(self.path)
                self.assertEqual(self.schema_state(), before)
                with self.assertRaises(StorageError):
                    AdviserStore(self.path)
                self.assertEqual(self.schema_state(), before)

    def test_interrupted_initialization_rolls_back_every_schema_change(self):
        broken = adviser_schema.CURRENT_SCHEMA + ('CREATE TABLE',)
        with patch.object(adviser_schema, 'CURRENT_SCHEMA', broken):
            with self.assertRaises(StorageError):
                AdviserStore.initialize(self.path)
        self.assertEqual(self.schema_state(), (0, ()))
        AdviserStore.initialize(self.path)
        store = AdviserStore(self.path)
        store.close()

    def test_concurrent_explicit_initialization_produces_one_current_schema(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: AdviserStore.initialize(self.path), range(2)))
        store = AdviserStore(self.path)
        store.close()
        self.assertEqual(self.schema_state()[0], adviser_schema.ADVISER_SCHEMA_VERSION)


if __name__ == '__main__':
    unittest.main()
