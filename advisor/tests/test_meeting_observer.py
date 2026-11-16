"""SM-08: owner-token meeting snapshots; unknown is never inferred as left."""
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError

from advisor.meeting_observer import FeishuMeetingObserver, VC_SCOPE
from advisor.persistence import meeting_instance_key
from advisor.store import SessionStore

NOW = 1_762_800_000


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def success(data):
    return Reply(json.dumps({'code': 0, 'data': data}).encode())


class Auth:
    def __init__(self):
        self.scopes = [VC_SCOPE]
        self.refreshes = 0

    def get_token(self, owner, *, rejected_token=None):
        if rejected_token:
            self.refreshes += 1
        return {'access_token': 'user-token', 'scopes': self.scopes}

    def authorize_url(self, owner, intent):
        return 'https://auth/renew'


class MeetingApi:
    def __init__(self):
        self.active = True
        self.participants = [{'id': 'ou_A', 'status': 2}]
        self.status = 2
        self.detail_error = False
        self.number = '123456789'
        self.lookups = 0

    def __call__(self, req, *, timeout):
        path = req.full_url
        self.assert_token(req)
        if '/authen/v1/user_info' in path:
            return success({'open_id': 'ou_A', 'user_id': 'ou_A_user'})
        if '/meetings/list_by_no' in path:
            self.lookups += 1
            raise HTTPError(path, 400, 'scope', {},
                            Reply(b'{"code":99991679,"msg":"scope"}'))
        if '/bots/user_active_meeting' in path:
            return success({'meetings': [
                {'meeting_no': self.number, 'meeting_id': 'vc-1'}] if self.active else []})
        if '/meetings/vc-1' in path:
            if self.detail_error:
                raise HTTPError(path, 403, 'unknown', {},
                                Reply(b'{"code":1234,"msg":"unavailable"}'))
            return success({'meeting': {'meeting_no': self.number,
                                        'status': self.status,
                                        'start_time': str(NOW),
                                        'participants': self.participants}})
        raise AssertionError(f'unexpected request {path}')

    @staticmethod
    def assert_token(req):
        assert req.get_header('Authorization') == 'Bearer user-token'


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(path)
        self.store = SessionStore(path)
        self.addCleanup(self.store.close)
        self.now = NOW
        self.auth = Auth()
        self.api = MeetingApi()
        self.observer = FeishuMeetingObserver(
            self.store, self.auth, opener=self.api, clock=lambda: self.now)
        self.sid = self.create_session('ou_A', '123456789')

    def create_session(self, owner, number):
        session, _ = self.store.create_session(
            owner_open_id=owner, meeting_no=number, title='会',
            event_calendar_id='cal', event_id='cal/evt-' + owner, now=NOW,
            instance_key=meeting_instance_key(number, 'cal', 'evt-' + owner, NOW))
        return session['session_id']

    def tick(self):
        self.observer.observe(self.store.require_session(self.sid))
        self.now += 3
        return self.store.require_session(self.sid)

    def test_resolves_while_owner_present_then_explicit_leave_and_return(self):
        row = self.tick()
        self.assertEqual((row['phase'], row['vc_meeting_id'], row['owner_presence']),
                         ('running', 'vc-1', 'present'))
        self.api.active = False  # ID already bound; details still queryable
        self.api.participants = [{'id': 'ou_A', 'status': 4}]
        self.assertEqual(self.tick()['phase'], 'paused')
        self.api.participants = [{'id': 'ou_A', 'status': 2}]
        self.assertEqual(self.tick()['phase'], 'running')
        self.assertEqual(self.api.lookups, 1)

    def test_absent_active_meeting_is_unknown_not_left(self):
        self.api.active = False
        self.assertEqual(self.tick()['phase'], 'running')
        self.assertIsNone(self.store.require_session(self.sid)['vc_meeting_id'])

    def test_after_binding_details_failure_is_unknown_not_left(self):
        self.tick()
        self.api.detail_error = True
        self.api.active = False
        self.assertEqual(self.tick()['phase'], 'running')

    def test_non_owner_left_does_not_pause(self):
        self.api.participants = [{'id': 'ou_A', 'status': 2},
                                 {'id': 'ou_B', 'status': 4}]
        self.tick()
        self.assertEqual(self.tick()['phase'], 'running')

    def test_explicit_meeting_end_drains(self):
        self.tick()
        self.api.status = 3
        self.api.participants = []
        self.assertEqual(self.tick()['phase'], 'draining')

    def test_reused_number_mismatch_does_not_bind(self):
        self.api.number = '999888777'
        self.assertIsNone(self.tick()['vc_meeting_id'])

    def test_second_active_occurrence_with_same_number_is_ambiguous(self):
        self.create_session('ou_B', '123456789')
        self.assertIsNone(self.tick()['vc_meeting_id'])

    def test_bound_meeting_survives_observer_restart(self):
        self.api.participants = [{'id': 'ou_A_user', 'status': 2}]
        self.assertEqual(self.tick()['vc_meeting_id'], 'vc-1')
        self.api.active = False
        self.api.participants = [{'id': 'ou_A_user', 'status': 4}]
        restarted = FeishuMeetingObserver(
            self.store, self.auth, opener=self.api, clock=lambda: self.now)
        restarted.observe(self.store.require_session(self.sid))
        self.assertEqual(self.store.require_session(self.sid)['phase'], 'paused')

    def test_old_oauth_scope_is_notified_without_false_observation(self):
        self.auth.scopes = ['calendar:calendar.event:read']
        self.tick()
        self.assertIsNone(self.store.require_session(self.sid)['vc_meeting_id'])
        count = self.store.db.execute(
            'SELECT count(*) FROM outbox WHERE session_id=?', (self.sid,)).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == '__main__':
    unittest.main()
