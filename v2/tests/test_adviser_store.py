from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from feishu_meeting_adviser.adviser_store import AdviserStore
from feishu_meeting_adviser.llm import Decision, LLMSettings
from feishu_meeting_adviser.models import Caption, ChangeKind, Delivery, IngestMode, SourceState, Status
from feishu_meeting_adviser.storage import CapacityError, StorageError

NOW = 1791849601


def delivery(sequence=1, key='s:1', text='合成测试发言', **kwargs):
    caption = Caption('m1', key, text, '2026-10-13T00:00:01Z', event_time='2026-10-13T00:00:00Z',
                      speaker_id='synthetic-speaker', speaker_name='合成说话人', **kwargs)
    return Delivery(sequence, f'synthetic-{sequence}', ChangeKind.ADDED, caption=caption)


class AdviserStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data' / 'adviser.db'
        self.store = AdviserStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.settings = LLMSettings()

    def snapshot(self, mid='m1', **kwargs):
        return self.store.snapshot(mid, self.settings, now=NOW, joined_at=NOW-10, **kwargs)

    def test_durable_inbox_replay_and_meeting_isolation(self):
        row = delivery()
        self.assertEqual(self.store.ingest('m1', [row]), 1)
        self.store.close()
        self.store = AdviserStore(self.path)
        self.assertEqual(self.store.ingest('m1', [replace(row, replayed=True)]), 0)
        self.assertEqual(self.store.meeting('m1')['generation'], 1)
        other = replace(row, delivery_id='other', caption=replace(row.caption, meeting_id='m2'))
        self.store.ingest('m2', [other])
        self.assertEqual(len(self.snapshot()['captions']), 1)
        self.assertEqual(len(self.snapshot('m2')['captions']), 1)

    def test_partial_page_failure_and_capacity_leave_inbox_unchanged(self):
        with self.assertRaises(StorageError):
            self.store.ingest('m1', [delivery(), replace(delivery(2), caption=replace(delivery(2).caption, meeting_id='m2'))])
        self.assertIsNone(self.store.meeting('m1'))
        self.store.max_payload_bytes = 1
        with self.assertRaises(CapacityError):
            self.store.ingest('m1', [delivery()])
        self.assertIsNone(self.store.meeting('m1'))

    def test_initial_replay_can_be_fresh_but_unknown_old_and_self_input_cannot(self):
        first = delivery(mode=IngestMode.INITIAL)
        unknown = replace(delivery(2, 's:2'), caption=replace(first.caption, sentence_key='s:2', event_time=None))
        old = replace(delivery(3, 's:3'), caption=replace(first.caption, sentence_key='s:3', event_time='2026-10-12T00:00:00Z'))
        own = replace(delivery(4, 's:4'), caption=replace(first.caption, sentence_key='s:4', speaker_id='synthetic-bot'))
        self.store.ingest('m1', [first, unknown, old, own])
        self.assertEqual(self.snapshot(bot_id='synthetic-bot')['fresh_input_keys'], ['s:1'])
        self.assertEqual(len(self.snapshot()['captions']), 4)

    def test_conflict_suppresses_evidence_until_accepted_revision(self):
        self.store.ingest('m1', [delivery()])
        conflict = replace(delivery(2, text='candidate'), kind=ChangeKind.CONFLICT)
        self.store.ingest('m1', [conflict])
        self.assertEqual(self.snapshot()['fresh_input_keys'], [])
        self.assertTrue(self.snapshot()['captions'][0]['conflicted'])
        self.store.ingest('m1', [replace(delivery(3, text='accepted'), kind=ChangeKind.UPDATED)])
        self.assertEqual(self.snapshot()['fresh_input_keys'], ['s:1'])
        self.assertEqual(self.snapshot()['captions'][0]['text'], 'accepted')

    def test_job_is_recoverable_and_saved_decision_is_not_repeated(self):
        self.store.ingest('m1', [delivery()])
        job = self.store.prepare_job('m1', self.settings, 'role-v1', now=NOW, joined_at=NOW-10)
        self.assertIn('m1', self.store.protected_meetings())
        self.store.close()
        self.store = AdviserStore(self.path)
        resumed = self.store.prepare_job('m1', self.settings, 'role-v1', now=NOW, joined_at=NOW-10)
        self.assertEqual(job['job_id'], resumed['job_id'])
        decision = Decision('none', (), None, ())
        self.assertTrue(self.store.finish_job(job, decision))
        self.assertFalse(self.store.finish_job(job, decision))
        self.assertIsNone(self.store.prepare_job('m1', self.settings, 'role-v1', now=NOW, joined_at=NOW-10))

    def test_context_budget_keeps_whole_sentences_and_deletion_removes_jobs(self):
        self.store.ingest('m1', [delivery(text='x'*1000), delivery(2,'s:2',text='y'*1000)])
        limited = replace(self.settings, context_characters=1500)
        snapshot = self.store.snapshot('m1', limited, now=NOW, joined_at=NOW-10)
        self.assertEqual(len(snapshot['captions']), 1)
        self.assertEqual(len(snapshot['captions'][0]['text']), 1000)
        self.assertTrue(snapshot['context_omitted'])
        self.store.prepare_job('m1', limited, 'role-v1', now=NOW, joined_at=NOW-10)
        self.store.forget_meeting('m1')
        for table in ('inputs','context','jobs','meetings'):
            self.assertEqual(self.store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
