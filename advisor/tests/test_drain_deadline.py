"""SM-11: absolute drain deadline, reply validity, and bounded terminal states."""
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from advisor.errors import StateError
from advisor.models import Caption
from advisor.persistence import meeting_instance_key
from advisor.settings import AdviserSettings
from advisor.store import SessionStore
from advisor.supervisor import SessionSupervisor

from fakes import MockNsdp

NOW = 1_762_800_000.0
SETTINGS = AdviserSettings(poll_interval=0.02, drain_deadline_s=600,
                           reply_validity_s=3600)


def make_caption(session_id, key, text='内容'):
    return Caption(meeting_id=session_id, sentence_key=key, text=text,
                   received_at='2026-11-16T10:00:00+08:00',
                   event_time=int(NOW * 1000), start_time=int((NOW - 2) * 1000),
                   end_time=int(NOW * 1000))


class DrainBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.now = NOW
        self.settings = AdviserSettings(poll_interval=0.02,
                                        drain_deadline_s=600,
                                        reply_validity_s=3600)
        session, _ = self.store.create_session(
            owner_open_id='ou_A', meeting_no='123456789', title='会',
            event_calendar_id='cal', event_id='evt', now=self.now,
            instance_key=meeting_instance_key('123456789', 'cal', 'evt', 1762800000))
        self.sid = session['session_id']
        self.clock = [self.now]
        self.supervisor = SessionSupervisor(
            self.store, self.settings, nsdp=MockNsdp(), backend_factory=lambda: None,
            close_ingest=lambda _sid, **_kw: None,
            source_connected=lambda *_a: True, clock=lambda: self.clock[0])
        self.store.create_subscription_request(self.sid, 'ou_B', now=self.now)

    def seed_reply(self, recipient, state, *, created_ns, deadline):
        with self.store.unit_of_work() as tx:
            tx.outbox.save_reply(
                message_uuid=f'u-{recipient}-{created_ns}', session_id=self.sid,
                content='回答', evidence='{}', generation=1, created_ns=created_ns,
                context_version=None, recipients=(recipient,), now=created_ns / 1e9)
            row = tx.connection.execute(
                'SELECT uuid FROM outbox WHERE session_id=? AND recipient_id=? '
                "AND channel='im'", (self.sid, recipient)).fetchone()
            tx.connection.execute(
                'UPDATE outbox SET state=?,delivery_deadline_at=? WHERE uuid=?',
                (state, deadline, row['uuid']))
            return row['uuid']


class ForceEndTests(DrainBase):
    def test_deadline_forces_bounded_ended_with_diagnostics(self):
        self.store.ensure_source(self.sid, 'loopback', now=self.now)  # stays live
        self.store.ingest_caption(self.sid, make_caption(self.sid, 'k1'), now=self.now)
        from advisor.model_backend import load_role
        job = self.store.prepare_job(self.sid, self.settings, load_role(self.settings)[1],
                                     now=self.now)
        self.store.mark_job_running(job['job_id'])
        pending_uuid = self.seed_reply('ou_A', 'pending', created_ns=int(NOW * 1e9),
                                       deadline=NOW + 3600)
        sending_uuid = self.seed_reply('ou_B', 'sending', created_ns=int(NOW * 1e9),
                                       deadline=NOW + 3600)
        self.store.set_phase(self.sid, 'draining', now=self.now,
                             drain_deadline_at=self.now + 600)
        self.clock[0] = self.now + 601
        diagnostic = self.store.force_end(self.sid, now=self.clock[0])
        self.assertEqual(diagnostic['reason'], 'drain_deadline')
        self.assertEqual((diagnostic['lost_sources'], diagnostic['abandoned_jobs'],
                          diagnostic['cancelled_im'], diagnostic['uncertain_im']),
                         (1, 1, 1, 1))
        row = self.store.require_session(self.sid)
        self.assertEqual(row['phase'], 'ended')
        states = {r['uuid']: r['state'] for r in self.store.db.execute(
            'SELECT uuid,state FROM outbox WHERE session_id=?', (self.sid,))}
        self.assertEqual(states[pending_uuid], 'cancelled')
        self.assertEqual(states[sending_uuid], 'uncertain')  # never faked as sent
        self.assertIsNone(self.store.active_claim('ou_A'))
        self.assertIsNone(self.store.active_claim('ou_B'))
        # already-ended force_end is idempotent: replays the stored diagnostics
        self.assertEqual(self.store.force_end(self.sid, now=self.clock[0]), diagnostic)

    def test_supervisor_enforces_deadline_and_does_not_reset_it(self):
        # a connected live source keeps S5 unready so only the deadline can end it
        self.store.ensure_source(self.sid, 'loopback', now=self.now)
        self.store.set_phase(self.sid, 'draining', now=self.now,
                             drain_deadline_at=self.now + 600)
        self.supervisor.start_session(self.sid)
        self.addCleanup(self.supervisor.stop, timeout=3)
        deadline = self.store.require_session(self.sid)['drain_deadline_at']
        self.assertEqual(deadline, self.now + 600)
        self.clock[0] = self.now + 300
        time.sleep(0.1)
        self.assertEqual(self.store.require_session(self.sid)['phase'], 'draining')
        self.clock[0] = self.now + 601
        deadline_reached = time.monotonic() + 5
        while time.monotonic() < deadline_reached:
            if self.store.require_session(self.sid)['phase'] == 'ended':
                break
            time.sleep(0.02)
        row = self.store.require_session(self.sid)
        self.assertEqual(row['phase'], 'ended')
        self.assertEqual(row['drain_deadline_at'], self.now + 600)  # preserved

    def test_pre_v8_draining_row_gets_deadline_on_first_pass(self):
        with self.store.unit_of_work() as tx:
            tx.connection.execute(
                'UPDATE sessions SET drain_deadline_at=NULL WHERE session_id=?',
                (self.sid,))
        self.store.set_phase(self.sid, 'draining', now=self.now,
                             drain_deadline_at=self.now + 600)
        with self.store.unit_of_work() as tx:
            tx.connection.execute(
                'UPDATE sessions SET drain_deadline_at=NULL WHERE session_id=?',
                (self.sid,))
        deadline = self.store.ensure_drain_deadline(
            self.sid, now=self.now, duration_s=self.settings.drain_deadline_s)
        self.assertEqual(deadline, self.now + 600)
        # second call keeps the original deadline
        self.assertEqual(self.store.ensure_drain_deadline(
            self.sid, now=self.now + 100, duration_s=999), self.now + 600)


class ReplyValidityTests(DrainBase):
    def test_pending_reply_past_validity_is_cancelled(self):
        uuid = self.seed_reply('ou_A', 'pending', created_ns=int(NOW * 1e9),
                               deadline=NOW + 10)
        cancelled = self.store.discard_expired_replies(
            self.sid, self.settings, now=NOW + 11)
        self.assertEqual(cancelled, 1)
        state = self.store.db.execute(
            'SELECT state,error FROM outbox WHERE uuid=?', (uuid,)).fetchone()
        self.assertEqual(tuple(state), ('cancelled', 'reply_validity_expired'))

    def test_stuck_sending_becomes_uncertain_not_sent(self):
        self.seed_reply('ou_B', 'sending', created_ns=int(NOW * 1e9),
                        deadline=NOW + 10)
        self.assertEqual(
            self.store.expire_stuck_sending(self.sid, now=NOW + 11), 1)
        state = self.store.db.execute(
            "SELECT state,error FROM outbox WHERE session_id=? AND channel='im'",
            (self.sid,)).fetchone()
        self.assertEqual(tuple(state), ('uncertain', 'delivery_budget_exhausted'))

    def test_drain_ready_blocks_on_sending(self):
        self.seed_reply('ou_B', 'sending', created_ns=int(NOW * 1e9),
                        deadline=NOW + 3600)
        self.store.set_phase(self.sid, 'draining', now=self.now,
                             drain_deadline_at=self.now + 600)
        self.assertFalse(self.store.drain_ready(self.sid))
        self.store.expire_stuck_sending(self.sid, now=NOW + 3601)
        self.assertTrue(self.store.drain_ready(self.sid))


if __name__ == '__main__':
    unittest.main()
