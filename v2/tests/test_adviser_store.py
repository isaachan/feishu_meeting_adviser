from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from feishu_meeting_adviser.adviser_store import AdviserStore
from feishu_meeting_adviser.llm import Decision, LLMSettings
from feishu_meeting_adviser.models import (
    Caption,
    ChangeKind,
    Delivery,
    IngestMode,
    JobState,
    MaterialState,
    ParticipationState,
    ReplyState,
    SourceState,
    Status,
)
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

    def test_persisted_state_types_keep_existing_values(self):
        self.assertEqual({state.value for state in JobState}, {'pending', 'running', 'complete', 'abandoned'})
        self.assertEqual(
            {state.value for state in ReplyState},
            {'pending', 'sending', 'sent', 'uncertain', 'rejected', 'cancelled'},
        )
        self.assertEqual(
            {state.value for state in MaterialState},
            {'disabled', 'loading', 'ready', 'partial', 'unavailable'},
        )
        self.assertEqual({state.value for state in ParticipationState}, {'active', 'paused'})

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

    def test_reliability_contract_finish_job_updates_are_atomic(self):
        self.store.ingest('m1', [delivery()])
        first = self.store.prepare_job('m1', self.settings, 'role-v1', now=NOW, joined_at=NOW-10)
        self.store.finish_job(first, Decision('none', (), '旧候选', ('s:1',)), now=NOW)
        old_uuid = self.store.next_reply('m1', now=NOW)['uuid']

        self.store.ingest('m1', [delivery(2, 's:2')])
        job = self.store.prepare_job('m1', self.settings, 'role-v1', now=NOW, joined_at=NOW-10)
        self.store.mark_job_running(job['job_id'])
        self.store.db.execute('''
            CREATE TRIGGER fail_job_completion BEFORE UPDATE OF state ON jobs
            WHEN NEW.job_id = '%s' AND NEW.state = 'complete'
            BEGIN SELECT RAISE(ABORT, 'synthetic finish failure'); END
        ''' % job['job_id'])

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish_job(job, Decision('none', (), '新候选', ('s:2',)), now=NOW)

        meeting = self.store.meeting('m1')
        self.assertEqual(meeting['decided_generation'], 1)
        self.assertEqual(meeting['participation'], 'active')
        saved_job = self.store.db.execute('SELECT state FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
        self.assertEqual(saved_job['state'], 'running')
        replies = self.store.db.execute('SELECT uuid,state FROM outbox ORDER BY created_ns').fetchall()
        self.assertEqual([(row['uuid'], row['state']) for row in replies], [(old_uuid, 'pending')])

    def test_finish_job_rolls_back_at_every_repository_write(self):
        self.store.ingest('m1', [delivery()])
        first = self.store.prepare_job('m1', self.settings, 'role-v1', now=NOW, joined_at=NOW-10)
        self.store.finish_job(first, Decision('none', (), '旧候选', ('s:1',)), now=NOW)
        old_uuid = self.store.next_reply('m1', now=NOW)['uuid']
        self.store.ingest('m1', [delivery(2, 's:2')])
        job = self.store.prepare_job('m1', self.settings, 'role-v1', now=NOW, joined_at=NOW-10)
        self.store.mark_job_running(job['job_id'])

        def saved_state():
            meeting = self.store.db.execute(
                'SELECT decided_generation,participation FROM meetings WHERE meeting_id=\'m1\'').fetchone()
            jobs = [tuple(row) for row in self.store.db.execute(
                'SELECT job_id,state,result FROM jobs ORDER BY generation')]
            replies = [tuple(row) for row in self.store.db.execute(
                'SELECT uuid,state FROM outbox ORDER BY created_ns')]
            return tuple(meeting), jobs, replies

        before = saved_state()
        points = (
            'after_old_replies_cancelled',
            'after_reply_saved',
            'after_job_completed',
            'after_meeting_updated',
        )
        for point in points:
            with self.subTest(point=point):
                def fail(stage, point=point):
                    if stage == point:
                        raise RuntimeError('synthetic decision persistence failure')

                with self.assertRaises(RuntimeError):
                    self.store.finish_job(
                        job, Decision('none', (), '新候选', ('s:2',)), now=NOW, fault=fail)
                self.assertEqual(saved_state(), before)

        self.assertTrue(self.store.finish_job(
            job, Decision('none', (), '新候选', ('s:2',)), now=NOW))
        meeting, jobs, replies = saved_state()
        self.assertEqual(meeting, (2, 'active'))
        self.assertEqual(jobs[-1][1], 'complete')
        self.assertEqual(replies[0], (old_uuid, 'cancelled'))
        self.assertEqual(replies[1][1], 'pending')

    def test_finish_job_rejects_unknown_persisted_state_without_writes(self):
        self.store.ingest('m1', [delivery()])
        job = self.store.prepare_job('m1', self.settings, 'role-v1', now=NOW, joined_at=NOW-10)
        self.store.db.execute('UPDATE jobs SET state=\'unexpected\' WHERE job_id=?', (job['job_id'],))
        with self.assertRaisesRegex(StorageError, 'invalid adviser job state'):
            self.store.finish_job(job, Decision('none', (), '候选', ('s:1',)), now=NOW)
        self.assertEqual(
            self.store.db.execute('SELECT state FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()[0],
            'unexpected',
        )
        self.assertEqual(self.store.reply_statistics('m1')['pending_replies'], 0)

    def test_context_budget_keeps_whole_sentences_and_deletion_removes_jobs(self):
        self.store.ingest('m1', [delivery(text='x'*1000), delivery(2,'s:2',text='y'*1000)])
        limited = replace(self.settings, context_characters=2000)
        snapshot = self.store.snapshot('m1', limited, now=NOW, joined_at=NOW-10)
        self.assertEqual(len(snapshot['captions']), 1)
        self.assertEqual(len(snapshot['captions'][0]['text']), 1000)
        self.assertTrue(snapshot['context_omitted'])
        from feishu_meeting_adviser.storage import encode
        self.assertLessEqual(len(encode(snapshot)), limited.context_characters)
        self.store.prepare_job('m1', limited, 'role-v1', now=NOW, joined_at=NOW-10)
        self.store.forget_meeting('m1')
        for table in ('inputs','context','jobs','meetings'):
            self.assertEqual(self.store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

    def test_revised_evidence_invalidates_inflight_model_reply(self):
        self.store.ingest('m1',[delivery()])
        job=self.store.prepare_job('m1',self.settings,'role-v1',now=NOW,joined_at=NOW-10)
        self.store.ingest('m1',[replace(delivery(2,text='修正后的事实'),kind=ChangeKind.UPDATED)])
        self.store.finish_job(job,Decision('none',(),'旧事实回答',('s:1',)),now=NOW,settings=self.settings,joined_at=NOW-10)
        self.assertEqual(self.store.reply_statistics('m1')['pending_replies'],0)
        next_job=self.store.prepare_job('m1',self.settings,'role-v1',now=NOW,joined_at=NOW-10)
        self.assertNotEqual(next_job['job_id'],job['job_id'])

    def test_reply_inspection_hides_text_and_manual_resolution_is_meeting_scoped(self):
        self.store.ingest('m1',[delivery()])
        job=self.store.prepare_job('m1',self.settings,'role-v1',now=NOW,joined_at=NOW-10)
        self.store.finish_job(job,Decision('none',(),'私有合成正文',('s:1',)),now=NOW)
        message_uuid=self.store.next_reply('m1',now=NOW)['uuid']
        self.store.mark_sending(message_uuid)
        self.store.recover_sends()
        self.assertNotIn('text',self.store.list_replies('m1')[0])
        self.assertEqual(self.store.list_replies('m1',show_text=True)[0]['text'],'私有合成正文')
        with self.assertRaises(StorageError):
            self.store.resolve_reply('m2',message_uuid,'sent',now=NOW)
        self.store.resolve_reply('m1',message_uuid,'sent',now=NOW)
        self.assertEqual(self.store.reply_statistics('m1')['uncertain_replies'],0)

    def test_completed_job_compacts_prompt_but_keeps_decision_and_recovery_metadata(self):
        self.store.ingest('m1',[delivery(text='x'*2000)])
        job=self.store.prepare_job('m1',self.settings,'role-v1',now=NOW,joined_at=NOW-10)
        self.store.finish_job(job,Decision('none',(),None,()),now=NOW)
        row=self.store.db.execute('SELECT snapshot,result,state FROM jobs').fetchone()
        self.assertNotIn('x'*2000,row['snapshot'])
        self.assertIn('snapshot_hash',row['snapshot'])
        self.assertEqual(row['state'],'complete')
        self.assertIsNotNone(row['result'])
        self.assertEqual(self.snapshot()['captions'][0]['text'],'x'*2000)

    def test_history_pruning_keeps_intro_identity_for_long_active_meeting(self):
        session={'meeting_id':'m1','join_instance':'synthetic-join'}
        row=self.store.ensure_introduction(session,'synthetic intro',now=NOW-1000000)
        self.store.mark_sending(row['uuid'])
        self.store.mark_sent(row['uuid'],now=NOW-1000000,interval=0)
        self.store.prune_history(NOW*1_000_000_000)
        self.assertEqual(self.store.ensure_introduction(session,'synthetic intro',now=NOW)['state'],'sent')

    def test_expired_recovered_job_becomes_context_without_leaving_a_prompt_pending(self):
        self.store.ingest('m1', [delivery(text='private synthetic prompt')])
        job = self.store.prepare_job('m1', self.settings, 'role-v1', now=NOW, joined_at=NOW-10)
        self.store.mark_job_running(job['job_id'])
        self.store.recover_sends()
        self.assertIsNone(self.store.prepare_job('m1', self.settings, 'role-v1', now=NOW+1000, joined_at=NOW-10))
        row = self.store.db.execute('SELECT state,snapshot FROM jobs').fetchone()
        self.assertEqual(row['state'], 'abandoned')
        self.assertNotIn('private synthetic prompt', row['snapshot'])
        self.assertNotIn('m1', self.store.protected_meetings())

    def test_changed_role_rechecks_pending_reply_before_it_can_send(self):
        self.store.ingest('m1', [delivery()])
        self.store.ensure_role('m1', 'role-v1')
        job = self.store.prepare_job('m1', self.settings, 'role-v1', now=NOW, joined_at=NOW-10)
        self.store.finish_job(job, Decision('none', (), '旧角色生成的候选', ('s:1',)), now=NOW)
        self.store.ensure_role('m1', 'role-v2')
        self.assertIsNone(self.store.next_reply('m1', now=NOW))
        checked = self.store.prepare_job('m1', self.settings, 'role-v2', now=NOW, joined_at=NOW-10)
        self.assertEqual(checked['role_version'], 'role-v2')
        self.store.finish_job(checked, Decision('none', (), None, ()), now=NOW)
        self.assertIsNone(self.store.next_reply('m1', now=NOW))
