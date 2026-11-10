"""Plan-1.2 acceptance: ported session-domain semantics (decision idempotency,
outbox state machine, draining, ended late results) plus plan-C additions
(leases, subscriptions per STATE_MACHINES, forward-only fan-out)."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from advisor.errors import StateError, StorageError
from advisor.model_backend import Decision, load_role
from advisor.models import Caption
from advisor.settings import AdviserSettings
from advisor.store import SessionStore

NOW = 2_000_000_000.0
SETTINGS = AdviserSettings()


def make_caption(session_id, key, text, *, end_offset_ms=0):
    return Caption(
        meeting_id=session_id, sentence_key=key, text=text,
        received_at='2026-11-10T14:00:00+08:00',
        event_time=int((NOW + end_offset_ms / 1000) * 1000),
        start_time=int((NOW + (end_offset_ms - 3000) / 1000) * 1000),
        end_time=int((NOW + end_offset_ms / 1000) * 1000))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.now = NOW

    def create(self, *, description='讨论预算', meeting_no='123456789', owner='ou_A'):
        return self.store.create_session(
            owner_open_id=owner, meeting_no=meeting_no, title='周会',
            event_calendar_id='cal_x', event_id='evt_y',
            description=description, now=self.now)

    def ingest(self, session_id, key, text, *, end_offset_ms=0):
        caption = make_caption(session_id, key, text, end_offset_ms=end_offset_ms)
        assert self.store.ingest_caption(session_id, caption, now=self.now)
        return caption

    def prepare(self, session_id):
        role = load_role(SETTINGS)
        job = self.store.prepare_job(session_id, SETTINGS, role[1], now=self.now)
        if job is None:
            self.fail('expected a prepared job')
        return job

    def decide(self, job, reply, keys):
        return self.store.finish_job(
            job, Decision('none', (), reply, tuple(keys)),
            now=self.now, settings=SETTINGS)

    def rows(self, session_id, channel=None, state=None):
        query = f"SELECT * FROM outbox WHERE session_id='{session_id}'"
        if channel:
            query += f" AND channel='{channel}'"
        if state:
            query += f" AND state='{state}'"
        return [dict(row) for row in self.store.db.execute(query)]


class SessionLifecycleTests(Base):
    def test_create_session_material_and_token(self):
        session = self.create()
        self.assertEqual(session['phase'], 'recording')
        self.assertTrue(session['session_id'].startswith('s-'))
        token_row = self.store.session_token(session['session_id'])
        self.assertTrue(token_row['stream_token'].startswith('st-'))
        material = self.store.materials(session['session_id'])
        self.assertEqual(material['state'], 'ready')
        self.assertEqual(material['description'], '讨论预算')
        self.assertIn('meeting_context', self.store.snapshot(
            session['session_id'], SETTINGS, now=self.now))

    def test_empty_description_disables_material(self):
        session = self.create(description='')
        self.assertEqual(self.store.materials(session['session_id'])['state'], 'disabled')
        self.assertNotIn('meeting_context', self.store.snapshot(
            session['session_id'], SETTINGS, now=self.now))

    def test_duplicate_active_session_rejected_then_allowed_after_ended(self):
        self.create()
        with self.assertRaises(StateError):
            self.create()
        self.store.set_phase(self.store.list_sessions('ou_A')[0]['session_id'],
                             'ended', now=self.now)
        second = self.create()
        self.assertNotEqual(second['session_id'], self.store.list_sessions()[0])

    def test_phase_transitions(self):
        session = self.create()
        sid = session['session_id']
        self.assertEqual(self.store.set_phase(sid, 'draining', now=self.now)['phase'], 'draining')
        self.assertEqual(self.store.set_phase(sid, 'ended', now=self.now)['phase'], 'ended')
        with self.assertRaises(StateError):
            self.store.set_phase(sid, 'recording', now=self.now)
        with self.assertRaises(StateError):
            self.store.set_phase(sid, 'draining', now=self.now)

    def test_active_by_meeting_no(self):
        self.create(meeting_no='111111111')
        self.assertIsNotNone(self.store.active_by_meeting_no('111111111'))
        self.assertIsNone(self.store.active_by_meeting_no('222222222'))

    def test_unknown_session_rejected(self):
        with self.assertRaises(StorageError):
            self.store.require_session('s-nope')


class LeaseTests(Base):
    def test_second_consumer_rejected_until_expiry(self):
        sid = self.create()['session_id']
        self.assertTrue(self.store.acquire_lease(sid, 'consumer-1', ttl_s=30, now=self.now))
        self.assertFalse(self.store.acquire_lease(sid, 'consumer-2', ttl_s=30, now=self.now))
        self.assertTrue(self.store.acquire_lease(sid, 'consumer-1', ttl_s=30, now=self.now + 10))
        # renewal extended the lease to now+40
        self.assertFalse(self.store.acquire_lease(sid, 'consumer-2', ttl_s=30, now=self.now + 31))
        self.assertTrue(self.store.acquire_lease(sid, 'consumer-2', ttl_s=30, now=self.now + 41))
        self.store.release_lease(sid, 'consumer-2')
        self.assertTrue(self.store.acquire_lease(sid, 'consumer-3', now=self.now + 42))


class IngestTests(Base):
    def test_ingest_dedups_and_counts_generation(self):
        sid = self.create()['session_id']
        caption = make_caption(sid, 'k1', '第一句')
        self.assertTrue(self.store.ingest_caption(sid, caption, now=self.now))
        self.assertFalse(self.store.ingest_caption(sid, caption, now=self.now))
        session = self.store.require_session(sid)
        self.assertEqual(session['generation'], 1)
        self.assertEqual(self.store.context_count(sid), 1)

    def test_ingest_rejects_mismatched_session_and_non_recording(self):
        sid = self.create()['session_id']
        with self.assertRaises(StorageError):
            self.store.ingest_caption('s-other', make_caption('s-other', 'k', 'x'), now=self.now)
        self.store.set_phase(sid, 'draining', now=self.now)
        with self.assertRaises(StateError):
            self.store.ingest_caption(sid, make_caption(sid, 'k1', '迟到'), now=self.now)

    def test_sources_drain_tracking(self):
        sid = self.create()['session_id']
        self.store.ensure_source(sid, 'loopback', now=self.now)
        self.assertFalse(self.store.sources_ready_for_drain(sid))
        self.store.source_ack(sid, 'loopback', 10, now=self.now)
        self.store.mark_source_drained(sid, 'loopback', 10)
        self.assertTrue(self.store.sources_ready_for_drain(sid))
        self.store.ensure_source(sid, 'mic', now=self.now)
        self.store.mark_source_lost(sid, 'mic', lost_from=1, lost_to=9)
        self.assertTrue(self.store.sources_ready_for_drain(sid))


class DecisionFlowTests(Base):
    def test_decision_writes_local_and_im_rows_idempotently(self):
        sid = self.create()['session_id']
        self.ingest(sid, 'k1', '我们聊聊预算')
        job = self.prepare(sid)
        self.assertTrue(self.decide(job, '建议补充预算数据', ['k1']))
        local = self.rows(sid, channel='local')
        im = self.rows(sid, channel='im')
        self.assertEqual(len(local), 1)
        self.assertEqual(local[0]['state'], 'sent')
        self.assertEqual(len(im), 1)
        self.assertEqual(im[0]['state'], 'pending')
        self.assertEqual(im[0]['recipient_id'], 'ou_A')
        # 决策幂等：重放同任务不重复落库
        self.assertFalse(self.decide(job, '建议补充预算数据', ['k1']))
        self.assertEqual(len(self.rows(sid)), 2)

    def test_ended_rejects_late_results(self):
        sid = self.create()['session_id']
        self.ingest(sid, 'k1', '我们聊聊预算')
        job = self.prepare(sid)
        self.store.set_phase(sid, 'ended', now=self.now)
        self.assertFalse(self.decide(job, '迟到回答', ['k1']))
        self.assertEqual(self.rows(sid), [])
        state = self.store.db.execute(
            'SELECT state FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()['state']
        self.assertEqual(state, 'abandoned')

    def test_draining_allows_inflight_settlement(self):
        sid = self.create()['session_id']
        self.ingest(sid, 'k1', '我们聊聊预算')
        job = self.prepare(sid)
        self.store.set_phase(sid, 'draining', now=self.now)
        self.assertTrue(self.decide(job, '排空中的回答', ['k1']))
        self.assertEqual(len(self.rows(sid, channel='im', state='pending')), 1)

    def test_invalid_evidence_raises(self):
        from advisor.errors import ModelError
        sid = self.create()['session_id']
        self.ingest(sid, 'k1', '我们聊聊预算')
        job = self.prepare(sid)
        with self.assertRaises(ModelError):
            self.decide(job, '证据不存在', ['missing-key'])


class OutboxTests(Base):
    def settled_reply(self, subscriber=None):
        sid = self.create()['session_id']
        if subscriber:
            request = self.store.create_subscription_request(sid, subscriber, now=self.now)
            self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        self.ingest(sid, 'k1', '我们聊聊预算')
        job = self.prepare(sid)
        self.decide(job, '回答', ['k1'])
        return sid

    def test_send_state_machine(self):
        sid = self.settled_reply()
        row = self.store.next_reply(sid, now=self.now)
        self.store.mark_sending(row['uuid'])
        self.store.mark_sent(row['uuid'], now=self.now, interval=10)
        self.assertEqual(self.rows(sid, channel='im')[0]['state'], 'sent')
        with self.assertRaises(StorageError):
            self.store.mark_sending(row['uuid'])  # no longer pending

    def test_send_error_uncertain_and_resolve(self):
        sid = self.settled_reply()
        row = self.store.next_reply(sid, now=self.now)
        self.store.mark_sending(row['uuid'])
        self.store.mark_send_error(row['uuid'], 'timeout', uncertain=True)
        self.assertEqual(self.rows(sid, channel='im')[0]['state'], 'uncertain')
        self.store.resolve_reply(sid, row['uuid'], 'retry', now=self.now)
        self.assertEqual(self.rows(sid, channel='im')[0]['state'], 'pending')

    def test_cooldown_gates_next_reply(self):
        sid = self.settled_reply()
        row = self.store.next_reply(sid, now=self.now)
        self.store.mark_sending(row['uuid'])
        self.store.mark_sent(row['uuid'], now=self.now, interval=10)
        self.ingest(sid, 'k2', '第二个话题')
        job = self.prepare(sid)
        self.decide(job, '第二条回答', ['k2'])
        pending = self.rows(sid, channel='im', state='pending')
        self.assertEqual(len(pending), 1)
        self.assertIsNone(self.store.next_reply(sid, now=self.now + 5))
        self.assertEqual(self.store.next_reply(sid, now=self.now + 11)['uuid'], pending[0]['uuid'])

    def test_recover_sending_marks_uncertain(self):
        sid = self.settled_reply()
        row = self.store.next_reply(sid, now=self.now)
        self.store.mark_sending(row['uuid'])
        self.store.recover()
        self.assertEqual(self.rows(sid, channel='im')[0]['state'], 'uncertain')

    def test_discard_expired_replies(self):
        sid = self.settled_reply()
        self.ingest(sid, 'k2', '第二个话题')
        job = self.prepare(sid)
        self.decide(job, '第二条回答', ['k2'])
        # 新决策取消旧 pending（最新回答取代旧回答），当前仅一条 pending
        self.now += SETTINGS.reply_evidence_seconds + 10
        cancelled = self.store.discard_expired_replies(sid, SETTINGS, now=self.now)
        self.assertEqual(cancelled, 1)
        self.assertEqual(len(self.rows(sid, channel='im', state='cancelled')), 2)  # 旧答被取代 + 过期


class DrainTests(Base):
    def test_drain_requires_sources_jobs_and_outbox(self):
        sid = self.create()['session_id']
        self.assertTrue(self.store.drain_ready(sid))  # never-connected session
        self.store.ensure_source(sid, 'loopback', now=self.now)
        self.assertFalse(self.store.drain_ready(sid))
        self.store.mark_source_drained(sid, 'loopback', 0)
        self.ingest(sid, 'k1', '话题')
        job = self.prepare(sid)
        self.store.mark_job_running(job['job_id'])
        self.assertFalse(self.store.drain_ready(sid))
        self.store.fail_job(job, 'model_unavailable', retry_at=self.now + 5, now=self.now)
        self.assertFalse(self.store.drain_ready(sid))  # pending job remains
        self.assertTrue(self.decide(job, '回答', ['k1']))
        self.assertFalse(self.store.drain_ready(sid))  # pending im row blocks
        row = self.store.next_reply(sid, now=self.now + 6)
        self.store.mark_sending(row['uuid'])
        self.store.mark_sent(row['uuid'], now=self.now + 6, interval=0)
        self.assertTrue(self.store.drain_ready(sid))


class SubscriptionTests(Base):
    def full_flow(self):
        sid = self.create()['session_id']
        return sid, self.store.create_subscription_request(sid, 'ou_B', now=self.now)

    def test_lifecycle_T1_T2_and_idempotent_decide(self):
        sid, request = self.full_flow()
        self.assertEqual(request['state'], 'requested')
        self.assertEqual(request['owner_open_id'], 'ou_A')
        result = self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        self.assertEqual(result['state'], 'approved')
        again = self.store.decide_subscription(request['request_no'], 'ou_A', 'deny', now=self.now)
        self.assertEqual(again['state'], 'approved')  # 幂等回显

    def test_denied_then_new_request_T3_T5(self):
        sid, request = self.full_flow()
        self.store.decide_subscription(request['request_no'], 'ou_A', 'deny', now=self.now)
        second = self.store.create_subscription_request(sid, 'ou_B', now=self.now)
        self.assertEqual(second['state'], 'requested')
        self.assertEqual(second['previous']['state'], 'denied')
        self.assertGreater(second['request_no'], request['request_no'])

    def test_approved_reapply_is_idempotent_existing(self):
        sid, request = self.full_flow()
        self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        again = self.store.create_subscription_request(sid, 'ou_B', now=self.now)
        self.assertTrue(again['existing'])
        self.assertEqual(again['state'], 'approved')

    def test_revoke_terminal_and_reapply_rejected(self):
        sid, request = self.full_flow()
        self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        result = self.store.revoke_subscription(request['request_no'], 'ou_A', now=self.now)
        self.assertEqual(result['state'], 'revoked')
        with self.assertRaises(StateError):
            self.store.create_subscription_request(sid, 'ou_B', now=self.now)
        with self.assertRaises(StateError):
            self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        with self.assertRaises(StateError):
            self.store.revoke_subscription(request['request_no'], 'ou_A', now=self.now)

    def test_owner_checks_and_wrong_states(self):
        sid, request = self.full_flow()
        with self.assertRaises(StateError):
            self.store.decide_subscription(request['request_no'], 'ou_C', 'approve', now=self.now)
        with self.assertRaises(StateError):
            self.store.revoke_subscription(request['request_no'], 'ou_A', now=self.now)  # requested

    def test_draining_rejects_new_requests(self):
        sid, request = self.full_flow()
        self.store.set_phase(sid, 'draining', now=self.now)
        with self.assertRaises(StateError):
            self.store.create_subscription_request(sid, 'ou_C', now=self.now)


class FanOutTests(Base):
    def decide_reply(self, sid, key, text):
        self.ingest(sid, key, text)
        job = self.prepare(sid)
        self.assertTrue(self.decide(job, f'回答:{key}', [key]))

    def test_forward_only_fanout_and_revoke_stops_bleeding(self):
        sid = self.create()['session_id']
        self.decide_reply(sid, 'k1', '第一句')
        self.assertEqual([r['recipient_id'] for r in self.rows(sid, channel='im', state='pending')],
                         ['ou_A'])

        request = self.store.create_subscription_request(sid, 'ou_B', now=self.now)
        self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        self.decide_reply(sid, 'k2', '第二句')
        # 新决策会取消旧 pending（取代语义）：当前 pending = k2 的双收件人
        self.assertEqual(sorted(r['recipient_id'] for r in self.rows(sid, channel='im', state='pending')),
                         ['ou_A', 'ou_B'])
        # 仅前向：k1 的回答从未生成 ou_B 行（无补发）
        self.assertEqual([r['recipient_id'] for r in self.rows(sid, channel='im')
                          if r['generation'] == 1], ['ou_A'])

        # 撤销即刻止血：ou_B 的 pending 行被 cancel，不再进入后续 fan-out
        self.store.revoke_subscription(request['request_no'], 'ou_A', now=self.now)
        pending_b = [r for r in self.rows(sid, channel='im', state='pending')
                     if r['recipient_id'] == 'ou_B']
        self.assertEqual(pending_b, [])
        cancelled_b = [r for r in self.rows(sid, channel='im', state='cancelled')
                       if r['recipient_id'] == 'ou_B']
        self.assertEqual(len(cancelled_b), 1)
        self.decide_reply(sid, 'k3', '第三句')
        k3 = self.rows(sid, channel='im', state='pending')
        self.assertEqual([r['recipient_id'] for r in k3], ['ou_A'])


if __name__ == '__main__':
    unittest.main()
