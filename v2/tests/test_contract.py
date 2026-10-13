from dataclasses import replace
import unittest
from feishu_meeting_adviser.models import (
    Caption, ChangeKind, Delivery, FakeConsumer, IngestMode, SourceState, Status, epoch_ns,
)


class ContractTests(unittest.TestCase):
    def caption(self, **kwargs):
        return Caption('meeting-a', 's:1', 'synthetic original', '2026-10-13T00:00:00Z', **kwargs)

    def test_precision_offsets_and_unknown_time(self):
        one = epoch_ns('2026-10-13T08:00:00.100000001+08:00')
        two = epoch_ns('2026-10-13T00:00:00.100000009Z')
        self.assertEqual(two - one, 8)
        self.assertEqual(epoch_ns('1000', numeric_milliseconds=True), 1_000_000_000)
        for value in ('no timezone', '2026-10-13T00:00:00', True, 'bad', 123):
            self.assertIsNone(epoch_ns(value))

    def test_replay_dedup_update_conflict_and_meeting_isolation(self):
        consumer = FakeConsumer()
        first = Delivery(1, 'id-1', ChangeKind.ADDED, self.caption(mode=IngestMode.INITIAL))
        consumer.accept(first)
        self.assertFalse(consumer.accept(first))
        self.assertEqual(consumer.fresh_inputs, [])
        update = Delivery(2, 'id-2', ChangeKind.UPDATED, replace(first.caption, text='corrected', source_revision=2))
        consumer.accept(update)
        consumer.accept(Delivery(3, 'id-3', ChangeKind.CONFLICT, replace(first.caption, text='unordered')))
        consumer.accept(Delivery(4, 'id-4', ChangeKind.ADDED, replace(first.caption, meeting_id='meeting-b')))
        self.assertEqual(consumer.context[('meeting-a', 's:1')].text, 'corrected')
        self.assertEqual(len(consumer.context), 2)
        self.assertEqual(len(consumer.conflicts), 1)

    def test_serialization_retains_raw_times_status_and_historical_flag(self):
        row = Delivery(1, 'id', ChangeKind.ADDED, self.caption(end_time='2026-10-13T00:00:00.123456789Z'), replayed=True)
        self.assertEqual(Delivery.from_dict(row.to_dict()), row)
        self.assertTrue(row.historical)
        status = Delivery(2, 'status-id', ChangeKind.STATUS,
                          status=Status('meeting-a', SourceState.INCOMPLETE, 'cursor_invalid', 'now', 1, 2))
        self.assertEqual(Delivery.from_dict(status.to_dict()), status)

    def test_metadata_correction_changes_version_identity(self):
        original = self.caption(speaker_name='synthetic speaker', end_time='1000')
        self.assertNotEqual(original.fingerprint, replace(original, speaker_name='corrected speaker').fingerprint)
        self.assertNotEqual(original.fingerprint, replace(original, end_time='2000').fingerprint)
