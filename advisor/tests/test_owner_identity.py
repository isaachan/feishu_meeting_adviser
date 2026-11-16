"""SM-09: meeting organizer and advisor owner have distinct authority."""
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory

from advisor.errors import StateError
from advisor.model_backend import Decision, RemoteModelBackend, load_role
from advisor.models import Caption
from advisor.persistence import meeting_instance_key
from advisor.settings import AdviserSettings
from advisor.store import SessionStore
from advisor.supervisor import SessionSupervisor


class OwnerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(path)
        self.store = SessionStore(path)
        self.addCleanup(self.store.close)
        self.now = float(int(time.time()))
        self.settings = AdviserSettings()
        self.session, _ = self.store.create_session(
            owner_open_id='ou_C', organizer_open_id='ou_A', meeting_no='123456789',
            title='会议', event_calendar_id='cal', event_id='evt', now=self.now,
            instance_key=meeting_instance_key('123456789', 'cal', 'evt', int(self.now)))
        self.sid = self.session['session_id']

    def test_organizer_cannot_subscribe_or_approve(self):
        with self.assertRaises(StateError) as caught:
            self.store.create_subscription_request(self.sid, 'ou_A', now=self.now)
        self.assertEqual(caught.exception.args[0], 'not_owner')
        request = self.store.create_subscription_request(self.sid, 'ou_B', now=self.now)
        with self.assertRaises(StateError) as caught:
            self.store.decide_subscription(request['request_no'], 'ou_A', 'approve',
                                           now=self.now)
        self.assertEqual(caught.exception.args[0], 'not_owner')
        self.store.decide_subscription(request['request_no'], 'ou_C', 'approve',
                                       now=self.now)

    def test_c_and_approved_b_get_answers_not_a(self):
        request = self.store.create_subscription_request(self.sid, 'ou_B', now=self.now)
        self.store.decide_subscription(request['request_no'], 'ou_C', 'approve',
                                       now=self.now)
        caption = Caption(meeting_id=self.sid, sentence_key='k1', text='讨论预算',
                          received_at='2026-11-16T10:00:00+08:00',
                          event_time=int(self.now * 1000),
                          end_time=int(self.now * 1000))
        self.store.ingest_caption(self.sid, caption, now=self.now)
        job = self.store.prepare_job(self.sid, self.settings, load_role(self.settings)[1],
                                     now=self.now)
        self.assertIsNotNone(job)
        self.store.finish_job(job, Decision('none', (), '建议补充预算数据', ('k1',)),
                              now=self.now, settings=self.settings)
        recipients = {r['recipient_id'] for r in self.store.db.execute(
            "SELECT recipient_id FROM outbox WHERE session_id=? AND channel='im'",
            (self.sid,))}
        self.assertEqual(recipients, {'ou_C', 'ou_B'})

    def test_supervisor_binds_gateway_preheat_to_c(self):
        prepared = threading.Event()
        backends = []

        class Backend:
            owner_open_id = None
            session_id = None

            def prepare_session(self):
                prepared.set()

            def decide(self, _snapshot, _role):
                raise AssertionError('no captions to decide')

            def close(self):
                pass

        def factory():
            backend = Backend()
            backends.append(backend)
            return backend

        supervisor = SessionSupervisor(self.store, AdviserSettings(poll_interval=0.02),
                                       backend_factory=factory)
        supervisor.start_session(self.sid)
        self.addCleanup(supervisor.stop)
        self.assertTrue(prepared.wait(2))
        self.assertEqual((backends[0].owner_open_id, backends[0].session_id),
                         ('ou_C', self.sid))

    def test_request_hash_and_metadata_bind_c_across_retries(self):
        backend = RemoteModelBackend(self.settings)
        backend.owner_open_id, backend.session_id = 'ou_C', self.sid
        bodies = []

        def submit(body):
            bodies.append(body)
            return {'state': 'succeeded', 'result': {'decision': {
                'control': 'none', 'control_sentence_keys': [],
                'reply': None, 'reply_sentence_keys': []}}}

        backend._submit = submit
        snapshot = {'generation': 1}
        backend.decide(snapshot, 'role')
        backend.decide(snapshot, 'role')
        self.assertEqual(bodies[0]['request_id'], bodies[1]['request_id'])
        self.assertEqual(bodies[0]['metadata']['owner_open_id'], 'ou_C')
        self.assertEqual(bodies[0]['metadata']['session_id'], self.sid)
        backend.owner_open_id = 'ou_A'
        backend.decide(snapshot, 'role')
        self.assertNotEqual(bodies[0]['request_id'], bodies[2]['request_id'])

    def test_paused_discards_late_gateway_notification_and_answer(self):
        caption = Caption(meeting_id=self.sid, sentence_key='k1', text='讨论预算',
                          received_at='2026-11-16T10:00:00+08:00',
                          event_time=int(self.now * 1000),
                          end_time=int(self.now * 1000))
        self.store.ingest_caption(self.sid, caption, now=self.now)
        job = self.store.prepare_job(self.sid, self.settings, load_role(self.settings)[1],
                                     now=self.now)
        self.store.mark_job_running(job['job_id'])
        self.store.apply_observation(self.sid, owner_presence='present', now=self.now)
        self.store.apply_observation(self.sid, owner_presence='left',
                                     observed_at=self.now + 1, now=self.now + 1)
        notifications = []
        notifier = SimpleNamespace(notify_owner=lambda *args, **kw: notifications.append(args))
        supervisor = SessionSupervisor(self.store, self.settings,
                                       backend_factory=lambda: None, nsdp=notifier)
        result = SimpleNamespace(job=job,
                                 decision=Decision('none', (), '迟到回答', ('k1',)),
                                 error=None, elapsed_ms=1)
        supervisor._apply_result(result, self.now + 2, {
            'remote_timings': {'tool_events': ['nsdp_authorization_required']}})
        self.assertEqual(notifications, [])
        self.assertEqual(self.store.pending_im_rows(), [])


if __name__ == '__main__':
    unittest.main()
