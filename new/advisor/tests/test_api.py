"""Plan-1.4 acceptance (API surface): CONTRACT_ADVISOR_API over HTTP with
injected auth/calendar stubs."""
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, parse, request

from advisor.api import AdvisorApi, CalendarError, StubCalendarValidator, build_api_server
from advisor.auth_client import AuthorizationRequired
from advisor.store import SessionStore

OPENER = request.build_opener(request.ProxyHandler({}))


class FakeAuth:
    def __init__(self, mode='ok'):
        self.mode = mode

    def get_token(self, open_id, *, rejected_token=None):
        if self.mode == 'missing':
            raise AuthorizationRequired('https://auth.internal/oauth/start?state=x')
        return {'access_token': 'u-x', 'expires_at': time.time() + 3600, 'scopes': []}

    def authorize_url(self, open_id, intent=''):
        return f'https://auth.internal/oauth/start?intent={intent}'


class FakeCalendar(StubCalendarValidator):
    def __init__(self, code=None, description='日历描述'):
        super().__init__(description)
        self.code = code

    def validate(self, owner_open_id, meeting_ref):
        if self.code:
            raise CalendarError(self.code)
        return super().validate(owner_open_id, meeting_ref)


class SupervisorSpy:
    def __init__(self):
        self.started = []

    def start_session(self, session_id):
        self.started.append(session_id)


class ApiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.auth = FakeAuth()
        self.calendar = FakeCalendar()
        self.spy = SupervisorSpy()
        self.api = AdvisorApi(self.store, self.spy, auth_client=self.auth,
                              calendar_validator=self.calendar,
                              ingest_url='ws://127.0.0.1:8400/ingest')
        self.server = build_api_server(self.api, host='127.0.0.1', port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.counter = 0

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def get(self, path):
        with OPENER.open(request.Request(self.url(path), method='GET'), timeout=5) as response:
            return response.status, json.loads(response.read())

    def post(self, path, body, key=None):
        self.counter += 1
        headers = {'Content-Type': 'application/json'}
        if key is not None:
            headers['X-Idempotency-Key'] = key
        req = request.Request(self.url(path), data=json.dumps(body).encode(),
                              method='POST', headers=headers)
        with OPENER.open(req, timeout=5) as response:
            return response.status, json.loads(response.read())

    def post_error(self, path, body, key=None):
        try:
            self.post(path, body, key)
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def join(self, *, key=None, owner='ou_A', no='123456789', mic=False):
        return self.post('/sessions', {'owner_open_id': owner,
                                       'meeting_ref': {'no': no}, 'mic_enabled': mic},
                         key=key or f'join-{self.counter}-{no}')


class CreateSessionTests(ApiBase):
    def test_happy_path_returns_command_args_and_starts_supervisor(self):
        status, body = self.join()
        self.assertEqual(status, 200)
        self.assertEqual(body['phase'], 'recording')
        self.assertFalse(body['existing'])
        args = body['recorder_command_args']
        self.assertEqual(args['session'], body['session_id'])
        self.assertEqual(args['event'], 'cal_stub/evt_123456789')
        self.assertEqual(args['url'], 'ws://127.0.0.1:8400/ingest')
        self.assertTrue(args['token'].startswith('st-'))
        self.assertEqual(self.spy.started, [body['session_id']])
        record = self.store.materials(body['session_id'])
        self.assertEqual(record['description'], '日历描述')

    def test_authorization_required_carries_url(self):
        self.auth.mode = 'missing'
        status, body = self.post_error('/sessions', {'owner_open_id': 'ou_A',
                                                     'meeting_ref': {'no': '123456789'}},
                                       key='auth-1')
        self.assertEqual(status, 409)
        self.assertEqual(body['error']['code'], 'authorization_required')
        self.assertIn('authorize_url', body['error'])
        self.assertIn('intent=join:123456789', body['error']['authorize_url'])

    def test_four_rejection_codes_map(self):
        for code in ('meeting_not_found', 'not_organizer', 'outside_window'):
            self.calendar.code = code
            status, body = self.post_error('/sessions', {'owner_open_id': 'ou_A',
                                                         'meeting_ref': {'no': '123456789'}},
                                           key=f'rej-{code}')
            self.assertEqual((status, body['error']['code']), (409, code))
        status, body = self.post_error('/sessions', {'owner_open_id': 'x', 'meeting_ref': {}},
                                       key='rej-bad')
        self.assertEqual((status, body['error']['code']), (400, 'bad_message'))

    def test_duplicate_join_is_idempotent_on_both_levels(self):
        _, first = self.join(key='join-fixed')
        _, again = self.join(key='join-fixed')  # idempotency replay
        self.assertEqual(first['session_id'], again['session_id'])
        self.assertEqual(self.spy.started, [first['session_id']])
        _, third = self.join(key='join-other')  # new key, same active meeting
        self.assertTrue(third['existing'])
        self.assertEqual(third['session_id'], first['session_id'])
        self.assertEqual(self.spy.started, [first['session_id']])


class SessionOpsTests(ApiBase):
    def created(self):
        _, body = self.join()
        return body

    def test_list_and_status(self):
        body = self.created()
        status, listing = self.get(f"/sessions?owner_open_id=ou_A")
        self.assertEqual([s['session_id'] for s in listing['sessions']], [body['session_id']])
        status, state = self.get(f"/sessions/{body['session_id']}/status")
        self.assertEqual(state['phase'], 'recording')
        self.assertEqual(state['material_present'], True)

    def test_end_owner_only_and_idempotent(self):
        body = self.created()
        status, result = self.post_error(f"/sessions/{body['session_id']}/end",
                                         {'owner_open_id': 'ou_B'}, key='end-b')
        self.assertEqual((status, result['error']['code']), (409, 'not_owner'))
        status, result = self.post(f"/sessions/{body['session_id']}/end",
                                   {'owner_open_id': 'ou_A'}, key='end-a')
        self.assertEqual(result['phase'], 'draining')
        self.store.set_phase(body['session_id'], 'ended')
        status, result = self.post(f"/sessions/{body['session_id']}/end",
                                   {'owner_open_id': 'ou_A'}, key='end-a2')
        self.assertEqual(result['phase'], 'ended')

    def test_unknown_session_404(self):
        status, body = self.post_error('/sessions/s-nope/end', {'owner_open_id': 'ou_A'}, key='x')
        self.assertEqual((status, body['error']['code']), (404, 'not_found'))


class SubscriptionApiTests(ApiBase):
    def created(self):
        _, body = self.join()
        return body

    def subscribe(self, session, subscriber='ou_B', key=None):
        return self.post('/subscription-requests',
                         {'subscriber_open_id': subscriber,
                          'meeting_no': session['meeting_no']}, key=key or f'sub-{self.counter}')

    def test_full_subscription_lifecycle(self):
        session = self.created()
        status, request_row = self.subscribe(session)
        self.assertEqual((status, request_row['state']), (200, 'requested'))
        self.assertEqual(request_row['owner_open_id'], 'ou_A')
        status, decided = self.post(
            f"/subscription-requests/{request_row['request_no']}/decide",
            {'owner_open_id': 'ou_A', 'decision': 'approve'}, key='decide-1')
        self.assertEqual(decided['state'], 'approved')
        status, revoked = self.post(
            f"/subscriptions/{request_row['request_no']}/revoke",
            {'owner_open_id': 'ou_A'}, key='revoke-1')
        self.assertEqual(revoked['state'], 'revoked')
        status, body = self.post_error('/subscription-requests',
                                       {'subscriber_open_id': 'ou_B',
                                        'meeting_no': session['meeting_no']}, key='sub-2')
        self.assertEqual((status, body['error']['code']), (409, 'invalid_state'))

    def test_unknown_meeting_404(self):
        status, body = self.post_error('/subscription-requests',
                                       {'subscriber_open_id': 'ou_B',
                                        'meeting_no': '999999999'}, key='sub-x')
        self.assertEqual((status, body['error']['code']), (404, 'not_found'))


class OutboxApiTests(ApiBase):
    def test_pending_and_result_roundtrip(self):
        _, session = self.join()
        with self.store.unit_of_work() as tx:
            tx.outbox.save_reply(message_uuid='uuid-1', session_id=session['session_id'],
                                 content='回答文本', evidence='{}', generation=1,
                                 created_ns=int(time.time() * 1e9), context_version=None,
                                 recipients=('ou_A',), now=time.time())
        status, pending = self.get('/outbox/pending?limit=5')
        self.assertEqual(len(pending['rows']), 1)
        row = pending['rows'][0]
        self.assertEqual(row['recipient_open_id'], 'ou_A')
        self.assertEqual(row['payload']['text'], '回答文本')
        status, result = self.post(f"/outbox/{row['uuid']}/result",
                                   {'status': 'sent'}, key='send-1')
        self.assertEqual(result['state'], 'acknowledged')
        state = self.store.db.execute('SELECT state FROM outbox WHERE uuid=?',
                                      (row['uuid'],)).fetchone()['state']
        self.assertEqual(state, 'sent')
        status, empty = self.get('/outbox/pending?limit=5')
        self.assertEqual(empty['rows'], [])


if __name__ == '__main__':
    unittest.main()
