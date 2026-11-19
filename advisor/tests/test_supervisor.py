"""Plan-1.4 acceptance: supervisor auto-consume, drain-to-ended, restart
recovery, N concurrent sessions."""
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from advisor.model_backend import Decision
from advisor.models import Caption
from advisor.settings import AdviserSettings
from advisor.store import SessionStore
from advisor.supervisor import SessionSupervisor

SETTINGS = AdviserSettings(poll_interval=0.02)


class FakeBackend:
    context_overhead_characters = 0

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = 0

    def decide(self, snapshot, role):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        fresh = snapshot.get('fresh_input_keys') or []
        if not fresh:
            return Decision('none', (), None, ())
        latest = [c for c in snapshot['captions'] if c['sentence_key'] == fresh[-1]][0]
        return Decision('none', (), f'回答：{latest["text"]}', tuple(fresh))

    def diagnostic_metadata(self):
        return {}

    def close(self):
        return None


class SupervisorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.backend = FakeBackend()
        self.activity = {}
        self.settings = AdviserSettings(poll_interval=0.02)
        self.supervisor = SessionSupervisor(
            self.store, self.settings, backend_factory=lambda: self.backend,
            ingest_activity=lambda: self.activity)
        self.addCleanup(self.supervisor.stop)

    def create(self, meeting_no='123456789', idle=900, owner='ou_A', event_id='evt_y'):
        return self.store.create_session(
            owner_open_id=owner, meeting_no=meeting_no, title='会',
            event_calendar_id='cal_x', event_id=event_id, idle_timeout_s=idle)[0]

    def say(self, session_id, key, text):
        now = time.time()
        caption = Caption(meeting_id=session_id, sentence_key=key, text=text,
                          received_at='now', event_time=int(now * 1000),
                          start_time=int((now - 3) * 1000), end_time=int((now + 0.01) * 1000))
        assert self.store.ingest_caption(session_id, caption, now=now)

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def im_rows(self, session_id):
        return [dict(r) for r in self.store.db.execute(
            "SELECT * FROM outbox WHERE session_id=? AND channel='im'", (session_id,))]


class AutoConsumeTests(SupervisorBase):
    def test_jira_authorization_notifies_even_without_nsdp(self):
        from types import SimpleNamespace
        from advisor.nsdp import NsdpService

        session = self.create()
        auth = SimpleNamespace(jira_submit_url=lambda owner: 'https://auth/jira/submit?state=x')
        self.supervisor.jira_notifier = NsdpService(auth, None, self.store, clock=lambda: 1000)
        self.store.insert_owner_notice(session['session_id'], 'ou_A', 'NSDP notice', now=1000)
        self.store.fail_job = lambda *args, **kwargs: None
        result = SimpleNamespace(job={'session_id': session['session_id'], 'job_id': 'job'},
                                 error='model_failed', decision=None, elapsed_ms=1)
        self.supervisor._apply_result(result, 1000,
            {'remote_timings': {'tool_events': ['jira_authorization_required']}})
        rows = self.im_rows(session['session_id'])
        self.assertEqual(len(rows), 2)
        self.assertTrue(any('Jira PAT' in row['content'] and 'jira/submit' in row['content']
                            for row in rows))

    def test_tool_authorization_event_notifies_organizer(self):
        from types import SimpleNamespace

        session = self.create()
        notices = []
        self.supervisor.nsdp = SimpleNamespace(
            notify_owner=lambda sid, owner, **kw: notices.append((sid, owner)))
        self.store.fail_job = lambda *args, **kwargs: None
        result = SimpleNamespace(job={'session_id': session['session_id'], 'job_id': 'job'},
                                 error='model_failed', decision=None, elapsed_ms=1)
        self.supervisor._apply_result(result, time.time(),
            {'remote_timings': {'tool_events': ['nsdp_authorization_required']}})
        self.assertEqual(notices, [(session['session_id'], 'ou_A')])

    def test_remote_timings_are_persisted_per_job(self):
        from types import SimpleNamespace

        session = self.create()
        self.store.fail_job = lambda *args, **kwargs: None
        with self.store.unit_of_work() as tx:
            tx.connection.execute(
                "INSERT INTO jobs(job_id,session_id,generation,snapshot,role_version,"
                "state,created_ns) VALUES ('job-t','" + session['session_id'] +
                "',1,'{}','v1','running',1)")
        result = SimpleNamespace(job={'session_id': session['session_id'], 'job_id': 'job-t'},
                                 error='model_failed', decision=None, elapsed_ms=1)
        self.supervisor._apply_result(result, time.time(),
            {'remote_timings': {'execution_ms': 9, 'process_start_ms': 2,
                                'end_to_end_ms': 12},
             'remote_request_id': 'req-0001'})
        row = self.store.db.execute(
            "SELECT timings FROM jobs WHERE job_id='job-t'").fetchone()
        timings = json.loads(row['timings'])
        self.assertEqual(timings['process_start_ms'], 2)
        self.assertEqual(timings['end_to_end_ms'], 12)
        self.assertEqual(timings['request_id'], 'req-0001')

    def test_create_then_captions_produce_replies_automatically(self):
        session = self.create()
        self.supervisor.start_session(session['session_id'])
        self.say(session['session_id'], 'k1', '我们聊聊预算')
        self.assertTrue(self.wait_until(lambda: len(self.im_rows(session['session_id'])) >= 1))
        self.assertGreaterEqual(self.backend.calls, 1)
        row = self.im_rows(session['session_id'])[0]
        self.assertIn('预算', row['content'])
        self.activity[session['session_id']] = time.monotonic()  # keep alive

    def test_end_command_drains_to_ended(self):
        session = self.create()
        self.supervisor.start_session(session['session_id'])
        self.say(session['session_id'], 'k1', '话题')
        self.wait_until(lambda: len(self.im_rows(session['session_id'])) >= 1)
        # bot transport settles the pending IM row, then the organizer ends
        row = self.im_rows(session['session_id'])[0]
        self.store.mark_sending(row['uuid'])
        self.store.mark_sent(row['uuid'], now=time.time(), interval=0)
        self.store.set_phase(session['session_id'], 'draining')
        self.assertTrue(self.wait_until(
            lambda: self.store.require_session(session['session_id'])['phase'] == 'ended'))
        self.supervisor.stop()

    def test_idle_timeout_drains_to_ended(self):
        session = self.create(idle=0)
        self.supervisor.start_session(session['session_id'])
        self.assertTrue(self.wait_until(
            lambda: self.store.require_session(session['session_id'])['phase'] == 'ended',
            timeout=5))
        self.supervisor.stop()


class RecoveryTests(SupervisorBase):
    def test_restart_recovers_unended_sessions(self):
        session = self.create()
        self.supervisor.start_session(session['session_id'])
        self.supervisor.stop()
        self.assertEqual(self.store.require_session(session['session_id'])['phase'],
                         'running')
        reborn = SessionSupervisor(self.store, self.settings,
                                   backend_factory=lambda: self.backend,
                                   ingest_activity=lambda: self.activity)
        reborn.start()
        self.addCleanup(reborn.stop)
        self.assertIn(session['session_id'], reborn.active_sessions())
        self.say(session['session_id'], 'k1', '恢复后的话题')
        self.assertTrue(self.wait_until(lambda: len(self.im_rows(session['session_id'])) >= 1))

    def test_ended_sessions_are_not_resumed(self):
        session = self.create()
        self.store.set_phase(session['session_id'], 'draining')
        self.store.set_phase(session['session_id'], 'ended')
        self.supervisor.start()
        self.addCleanup(self.supervisor.stop)
        self.assertEqual(self.supervisor.active_sessions(), [])


class ConcurrencyTests(SupervisorBase):
    def test_two_sessions_run_independently(self):
        first = self.create(meeting_no='111111111', idle=900)
        second = self.create(meeting_no='222222222', idle=900, owner='ou_B',
                             event_id='evt_b')
        for session in (first, second):
            self.supervisor.start_session(session['session_id'])
        self.say(first['session_id'], 'k1', '第一个会话')
        self.say(second['session_id'], 'k1', '第二个会话')
        self.assertTrue(self.wait_until(lambda: len(self.im_rows(first['session_id'])) >= 1))
        self.assertTrue(self.wait_until(lambda: len(self.im_rows(second['session_id'])) >= 1))
        self.assertIn('第一个会话', self.im_rows(first['session_id'])[0]['content'])
        self.assertIn('第二个会话', self.im_rows(second['session_id'])[0]['content'])
        for session in (first, second):
            self.activity[session['session_id']] = time.monotonic()
        self.supervisor.stop()


if __name__ == '__main__':
    unittest.main()
