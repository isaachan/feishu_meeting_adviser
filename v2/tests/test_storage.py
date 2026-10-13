from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from feishu_meeting_adviser.models import Caption, ChangeKind, SourceState, Status
from feishu_meeting_adviser.storage import CapacityError, DurableFakeConsumer, StorageError, Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'private' / 'source.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.store.ensure_meeting('m1', {'cursor': None, 'start': 1})
        self.first = Caption('m1', 's:1', 'synthetic original', '2026-10-13T00:00:00Z', source_revision=1)

    def test_page_rollback_and_restart_commit_before_delivery(self):
        def fail(stage):
            if stage == 'after_checkpoint':
                raise RuntimeError('simulated crash')
        with self.assertRaises(RuntimeError):
            self.store.commit_page('m1', [self.first], {'cursor': 'next'}, fault=fail)
        self.assertEqual(self.store.checkpoint('m1')['cursor'], None)
        self.assertIsNone(self.store.caption('m1', 's:1'))
        self.assertEqual(self.store.read('m1', 'qa'), [])
        self.store.commit_page('m1', [self.first], {'cursor': 'next'})
        self.store.close()
        self.store = Store(self.path)
        rows = self.store.read('m1', 'qa')
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].replayed)
        self.assertEqual(rows[0].caption.text, self.first.text)
        self.assertEqual(self.store.checkpoint('m1')['cursor'], 'next')

    def test_consumer_crash_before_ack_replays_without_duplicate_context(self):
        self.store.commit_page('m1', [self.first], {})
        path = self.path.parent / 'consumer.sqlite3'
        consumer = DurableFakeConsumer(path)
        row = self.store.read('m1', 'qa')[0]
        self.assertTrue(consumer.accept(row))
        consumer.close()  # Crash after persisting context, before acknowledging source.
        consumer = DurableFakeConsumer(path)
        self.addCleanup(consumer.close)
        replay = self.store.read('m1', 'qa')[0]
        self.assertTrue(replay.replayed)
        self.assertFalse(consumer.accept(replay))
        self.store.acknowledge('m1', 'qa', replay.sequence)
        self.assertEqual(len(consumer.context('m1')), 1)
        self.assertEqual(self.store.read('m1', 'qa'), [])

    def test_duplicate_revision_old_version_and_conflict(self):
        self.store.commit_page('m1', [self.first], {})
        newer = replace(self.first, text='corrected', source_revision=2)
        self.store.commit_page('m1', [newer, self.first, newer], {})
        unknown = replace(newer, text='unknown order', source_revision=None)
        self.store.commit_page('m1', [unknown, unknown], {})
        rows = self.store.read('m1', 'qa')
        self.assertEqual([r.kind for r in rows], [ChangeKind.ADDED, ChangeKind.UPDATED, ChangeKind.CONFLICT, ChangeKind.STATUS])
        self.assertEqual(self.store.caption('m1', 's:1'), newer)
        self.assertEqual(self.store.current_state('m1').state, SourceState.INCOMPLETE)

    def test_capacity_failure_preserves_checkpoint_and_input(self):
        self.store.max_payload_bytes = self.store.payload_bytes() + 10
        with self.assertRaises(CapacityError):
            self.store.commit_page('m1', [self.first], {'cursor': 'new'})
        self.assertEqual(self.store.checkpoint('m1')['cursor'], None)
        self.assertIsNone(self.store.caption('m1', 's:1'))

    def test_ack_cannot_skip_unoffered_records_or_other_meeting(self):
        self.store.commit_page('m1', [self.first, replace(self.first, sentence_key='s:2')], {})
        first = self.store.read('m1', 'qa', limit=1)[0]
        with self.assertRaises(StorageError):
            self.store.acknowledge('m1', 'qa', first.sequence + 1)
        self.store.ensure_meeting('m2', {})
        with self.assertRaises(StorageError):
            self.store.acknowledge('m2', 'qa', first.sequence)
        self.store.acknowledge('m1', 'qa', first.sequence)
        self.assertEqual(self.store.backlog('m1', 'qa'), 1)
        self.assertEqual(len(self.store.read('m1', 'other')), 2)

    def test_meeting_mismatch_rolls_back_whole_page(self):
        with self.assertRaises(StorageError):
            self.store.commit_page('m1', [self.first, replace(self.first, meeting_id='m2')], {'cursor': 'bad'})
        self.assertIsNone(self.store.caption('m1', 's:1'))

    def test_sensitive_file_modes(self):
        self.store.commit_page('m1', [self.first], {})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        for suffix in ('-wal', '-shm'):
            self.assertEqual(Path(str(self.path) + suffix).stat().st_mode & 0o777, 0o600)
