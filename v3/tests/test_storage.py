import tempfile
import unittest
from pathlib import Path

from feishu_spectator.models import ChatMessage, TranscriptItem
from feishu_spectator.storage import CapacityError, Store, StorageError


def transcript(key='s:1', text='hello'):
    return TranscriptItem(meeting_id='737', item_key=key, text=text, received_at='now',
                          sentence_id=key[2:] if key.startswith('s:') else None)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'spectator.sqlite3'

    def tearDown(self):
        self.tmp.cleanup()

    def test_initialize_is_idempotent_and_private(self):
        Store.initialize(self.path)
        Store.initialize(self.path)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)

    def test_requires_existing_database(self):
        with self.assertRaises(StorageError):
            Store(self.path)

    def test_checkpoint_roundtrip(self):
        Store.initialize(self.path)
        store = Store(self.path)
        try:
            initial = store.ensure_meeting('737', {'schema': 1, 'page_token': None})
            self.assertIsNone(initial['page_token'])
            commit = store.commit_page('737', [transcript()], {'schema': 1, 'page_token': 't1'})
            self.assertEqual(commit.new_items, 1)
            self.assertEqual(store.checkpoint('737')['page_token'], 't1')
        finally:
            store.close()

    def test_commit_is_idempotent(self):
        Store.initialize(self.path)
        store = Store(self.path)
        try:
            store.ensure_meeting('737', {'schema': 1})
            first = store.commit_page('737', [transcript()], {'schema': 1})
            second = store.commit_page('737', [transcript()], {'schema': 1})
            self.assertEqual((first.new_items, first.duplicates), (1, 0))
            self.assertEqual((second.new_items, second.duplicates), (0, 1))
            self.assertEqual(store.statistics('737')['stored_items']['transcript'], 1)
        finally:
            store.close()

    def test_unknown_meeting_commit_rejected(self):
        Store.initialize(self.path)
        store = Store(self.path)
        try:
            with self.assertRaises(StorageError):
                store.commit_page('missing', [], {'schema': 1})
        finally:
            store.close()

    def test_capacity_rolls_back(self):
        Store.initialize(self.path)
        store = Store(self.path, max_database_bytes=1)
        try:
            store.ensure_meeting('737', {'schema': 1})
            with self.assertRaises(CapacityError):
                store.commit_page('737', [transcript()], {'schema': 1})
            self.assertEqual(store.statistics('737')['stored_items'], {})
            self.assertEqual(store.checkpoint('737')['schema'], 1)
        finally:
            store.close()

    def test_readonly_rejects_writes(self):
        Store.initialize(self.path)
        store = Store(self.path)
        store.ensure_meeting('737', {'schema': 1})
        store.close()
        readonly = Store(self.path, readonly=True)
        try:
            with self.assertRaises(StorageError):
                readonly.commit_page('737', [transcript()], {'schema': 1})
            items = readonly.recent_items('737')
            self.assertEqual(items, [])
        finally:
            readonly.close()

    def test_mixed_kinds_and_recent_items(self):
        Store.initialize(self.path)
        store = Store(self.path)
        try:
            store.ensure_meeting('737', {'schema': 1})
            chat = ChatMessage(meeting_id='737', item_key='m:1', content='hi', received_at='now')
            store.commit_page('737', [transcript(), chat], {'schema': 1})
            items = store.recent_items('737')
            self.assertEqual(len(items), 2)
            kinds = {item['kind'] for item in items}
            self.assertEqual(kinds, {'transcript', 'chat'})
        finally:
            store.close()

    def test_statistics_hashes_meeting_id(self):
        Store.initialize(self.path)
        store = Store(self.path)
        try:
            store.ensure_meeting('737', {'schema': 1})
            store.commit_page('737', [transcript()], {'schema': 1})
            stats = store.statistics('737')
            self.assertNotIn('737', stats['meeting_hash'])
            self.assertEqual(stats['stored_items']['transcript'], 1)
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
