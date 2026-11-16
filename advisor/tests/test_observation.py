"""SM-06: observation-driven phase transitions + 900s persisted audio clock."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from advisor.errors import StorageError
from advisor.models import Caption
from advisor.persistence import meeting_instance_key
from advisor.settings import AdviserSettings
from advisor.store import SessionStore

NOW = 1_762_800_000.0
SETTINGS = AdviserSettings()


def make_caption(session_id, key, text='内容'):
    return Caption(meeting_id=session_id, sentence_key=key, text=text,
                   received_at='2026-11-16T10:00:00+08:00',
                   event_time=int(NOW * 1000), start_time=int((NOW - 2) * 1000),
                   end_time=int(NOW * 1000))


class ObservationBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)

    def create(self, owner='ou_A', meeting_no='123456789'):
        session, _existing = self.store.create_session(
            owner_open_id=owner, meeting_no=meeting_no, title='t',
            event_calendar_id='cal_x', event_id=f'evt_{meeting_no}', now=NOW,
            instance_key=meeting_instance_key(meeting_no, 'cal_x',
                                              f'evt_{meeting_no}', 1762800000))
        return session

    def observe(self, session_id, *, presence=None, status=None,
                observed_at=None, now=NOW, max_age_s=60.0):
        return self.store.apply_observation(
            session_id, owner_presence=presence, meeting_status=status,
            observed_at=observed_at, now=now, max_age_s=max_age_s)

    def phase(self, session_id):
        return self.store.require_session(session_id)['phase']


class TransitionTests(ObservationBase):
    def test_present_then_left_pauses(self):
        session = self.create()
        self.assertTrue(self.observe(session['session_id'], presence='present').get('applied'))
        self.assertEqual(self.phase(session['session_id']), 'running')
        result = self.observe(session['session_id'], presence='left',
                              observed_at=NOW + 5, now=NOW + 5)
        self.assertEqual(result['phase'], 'paused')

    def test_return_resumes(self):
        session = self.create()
        self.observe(session['session_id'], presence='present')
        self.observe(session['session_id'], presence='left', observed_at=NOW + 5, now=NOW + 5)
        result = self.observe(session['session_id'], presence='present',
                              observed_at=NOW + 10, now=NOW + 10)
        self.assertEqual(result['phase'], 'running')

    def test_left_without_confirmed_present_never_pauses(self):
        # 已定：创建时不在会不算离会（SM-00）
        session = self.create()
        result = self.observe(session['session_id'], presence='left')
        self.assertEqual(result['phase'], 'running')
        self.assertTrue(result['applied'])
        # left 已确认记录，再来 present 只更新 presence 不迁移
        self.observe(session['session_id'], presence='present', observed_at=NOW + 3,
                     now=NOW + 3)
        self.assertEqual(self.phase(session['session_id']), 'running')
        # 现在 left 才能触发 paused（confirmed present→left 转变）
        result = self.observe(session['session_id'], presence='left',
                              observed_at=NOW + 6, now=NOW + 6)
        self.assertEqual(result['phase'], 'paused')

    def test_unknown_and_stale_never_migrate(self):
        session = self.create()
        self.observe(session['session_id'], presence='present')
        result = self.observe(session['session_id'], presence='unknown',
                              observed_at=NOW + 5, now=NOW + 5)
        self.assertEqual((result['phase'], result['applied']), ('running', True))
        # stale: same or older snapshot
        result = self.observe(session['session_id'], presence='left', observed_at=NOW + 5,
                              now=NOW + 6)
        self.assertEqual((result['phase'], result['applied']), ('running', False))
        # stale: too old vs now
        result = self.observe(session['session_id'], presence='left',
                              observed_at=NOW + 1000, now=NOW + 1200)
        self.assertEqual(result['applied'], False)
        self.assertEqual(self.phase(session['session_id']), 'running')

    def test_meeting_ended_drains_from_both_phases(self):
        session = self.create()
        result = self.observe(session['session_id'], status='ended', observed_at=NOW + 2,
                              now=NOW + 2)
        self.assertEqual(result['phase'], 'draining')
        other = self.create(owner='ou_C', meeting_no='999888777')
        self.observe(other['session_id'], presence='present')
        self.observe(other['session_id'], presence='left', observed_at=NOW + 4, now=NOW + 4)
        self.assertEqual(self.phase(other['session_id']), 'paused')
        result = self.observe(other['session_id'], status='ended', observed_at=NOW + 6,
                              now=NOW + 6)
        self.assertEqual(result['phase'], 'draining')

    def test_paused_discards_inflight_immediately(self):
        session = self.create()
        # in-flight job + pending reply present when the owner leaves
        self.assertTrue(self.store.ingest_caption(
            session['session_id'], make_caption(session['session_id'], 'k1'), now=NOW))
        role = __import__('advisor.model_backend', fromlist=['load_role']).load_role(
            SETTINGS)
        job = self.store.prepare_job(session['session_id'], SETTINGS, role[1], now=NOW)
        self.assertIsNotNone(job)
        self.observe(session['session_id'], presence='present')
        self.observe(session['session_id'], presence='left', observed_at=NOW + 5, now=NOW + 5)
        states = [row['state'] for row in self.store.db.execute(
            "SELECT state FROM jobs WHERE session_id=?", (session['session_id'],))]
        self.assertTrue(all(state == 'abandoned' for state in states))
        self.assertEqual(self.store.reply_statistics(session['session_id']),
                         {'pending_replies': 0, 'uncertain_replies': 0,
                          'rejected_replies': 0})
        # 迟到的模型结果不复活：job 终态为 abandoned
        row = self.store.db.execute(
            "SELECT * FROM jobs WHERE session_id=?", (session['session_id'],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['state'], 'abandoned')

    def test_draining_and_ended_ignore_late_snapshots(self):
        session = self.create()
        self.observe(session['session_id'], status='ended', observed_at=NOW + 2, now=NOW + 2)
        result = self.observe(session['session_id'], presence='present',
                              observed_at=NOW + 4, now=NOW + 4)
        self.assertEqual((result['phase'], result['applied']), ('draining', True))


class IdleClockTests(ObservationBase):
    def test_deadline_from_creation_without_audio(self):
        session = self.store.db.execute(
            'SELECT * FROM sessions').fetchone()
        self.assertIsNone(session)  # sanity: empty
        created = self.create()
        row = self.store.require_session(created['session_id'])
        self.assertEqual(row['last_audio_at'], NOW)  # backfill at creation
        # 建会即起算：无音频 900 秒 → 超时
        supervisor_view = (row['last_audio_at'] or row['started_at']) + row['idle_timeout_s']
        self.assertEqual(supervisor_view, NOW + 900)

    def test_real_audio_extends_control_messages_do_not(self):
        session = self.create()
        self.store.ensure_source(session['session_id'], 'loopback', now=NOW + 100)
        self.store.record_audio_at(session['session_id'], now=NOW + 500)
        self.store.source_ack(session['session_id'], 'loopback', 10, now=NOW + 500)
        row = self.store.require_session(session['session_id'])
        self.assertEqual(row['last_audio_at'], NOW + 500)
        # stream_end/gap can advance a cursor, not the real-audio clock.
        self.store.source_ack(session['session_id'], 'loopback', 11, now=NOW + 600)
        row = self.store.require_session(session['session_id'])
        self.assertEqual(row['last_audio_at'], NOW + 500)

    def test_paused_keeps_counting_via_same_clock(self):
        session = self.create()
        self.observe(session['session_id'], presence='present')
        self.observe(session['session_id'], presence='left', observed_at=NOW + 5, now=NOW + 5)
        row = self.store.require_session(session['session_id'])
        base = row['last_audio_at'] or row['started_at']
        # paused 后 900 秒无音频仍应排空：deadline 不因 phase 改变
        self.assertEqual(base + row['idle_timeout_s'], NOW + 900)


if __name__ == '__main__':
    unittest.main()
