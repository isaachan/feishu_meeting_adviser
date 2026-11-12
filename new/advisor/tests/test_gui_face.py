"""Plan-G3 acceptance: GUI face (CONTRACT_ADVISOR_API §8) over HTTP.

Covers device-flow login (start/poll/one-shot), device_token auth, session
discovery with recorder_command_args, draining null-args, logout idempotence
and the credential red line (no user OAuth tokens in any GUI response).
"""
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, parse, request

from advisor.api import AdvisorApi, StubCalendarValidator, build_api_server
from advisor.auth_client import AuthorizationRequired, AuthUnavailable
from advisor.store import SessionStore

OPENER = request.build_opener(request.ProxyHandler({}))


class FakeAuth:
    def __init__(self):
        self.status = {'status': 'pending'}
        self.start_calls = 0
        self.expires_at = None  # injectable for TTL tests; None = wall clock

    def get_token(self, open_id, *, rejected_token=None):
        return {'access_token': 'u-x', 'expires_at': time.time() + 3600, 'scopes': []}

    def authorize_url(self, open_id, intent=''):
        raise AuthorizationRequired('https://auth.internal/oauth/start?state=x')

    def gui_authorize_start(self):
        self.start_calls += 1
        expires = self.expires_at if self.expires_at is not None else time.time() + 600
        return {'authorize_url': 'https://accounts.feishu.cn/open-apis/authen/v1/authorize?state=st-1',
                'state': 'st-1', 'expires_at': expires}

    def authorize_status(self, state):
        return dict(self.status)


class ManualClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SupervisorSpy:
    def __init__(self):
        self.started = []

    def start_session(self, session_id):
        self.started.append(session_id)


class GuiFaceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.auth = FakeAuth()
        self.clock = ManualClock()
        self.api = AdvisorApi(self.store, SupervisorSpy(), auth_client=self.auth,
                              calendar_validator=StubCalendarValidator(),
                              ingest_url='ws://127.0.0.1:8400/ingest',
                              clock=self.clock)
        self.server = build_api_server(self.api, host='127.0.0.1', port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def get(self, path, token=None):
        headers = {}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        req = request.Request(self.url(path), method='GET', headers=headers)
        with OPENER.open(req, timeout=5) as response:
            return response.status, json.loads(response.read())

    def post(self, path, body=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        req = request.Request(self.url(path), data=json.dumps(body or {}).encode(),
                              method='POST', headers=headers)
        with OPENER.open(req, timeout=5) as response:
            return response.status, json.loads(response.read())

    def get_error(self, path, token=None):
        try:
            self.get(path, token)
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def login(self):
        """Full device-flow: returns (device_token, user)."""
        _, start = self.post('/gui/login/start')
        self.auth.status = {'status': 'completed', 'user': {
            'open_id': 'ou_A', 'name': 'Kai Han', 'en_name': 'Kai Han',
            'email': 'kai.han@nio.com'}}
        _, poll = self.get('/gui/login/poll?device_code=' + parse.quote(start['device_code']))
        return start, poll

    def create_session(self, meeting_no='123456789'):
        return self.api.create_session({'owner_open_id': 'ou_A',
                                        'meeting_ref': {'no': meeting_no}})


class LoginFlowTests(GuiFaceBase):
    def test_start_returns_qr_url_and_pending_poll(self):
        status, start = self.post('/gui/login/start')
        self.assertEqual(status, 200)
        self.assertTrue(start['login_url'].startswith('https://accounts.feishu.cn/'))
        self.assertTrue(start['device_code'].startswith('dc-'))
        self.assertGreater(start['expires_at'], self.clock.now)
        status, poll = self.get('/gui/login/poll?device_code=' + parse.quote(start['device_code']))
        self.assertEqual(poll, {'status': 'pending'})

    def test_completion_issues_one_shot_device_token(self):
        start, poll = self.login()
        self.assertEqual(poll['status'], 'completed')
        self.assertTrue(poll['device_token'].startswith('dt-'))
        self.assertEqual(poll['user'], {'open_id': 'ou_A', 'name': 'Kai Han',
                                        'email': 'kai.han@nio.com'})
        # one-shot: the same device_code cannot mint a second token
        code, body = self.get_error('/gui/login/poll?device_code=' + parse.quote(start['device_code']))
        self.assertEqual(code, 404)
        self.assertEqual(body['error']['code'], 'unknown_device')

    def test_unknown_or_malformed_device_code(self):
        for code_value in ('dc-nope', 'nope', '', 'dc-' + 'x' * 200):
            code, body = self.get_error('/gui/login/poll?device_code=' + parse.quote(code_value))
            self.assertEqual(code, 404, code_value)
            self.assertEqual(body['error']['code'], 'unknown_device')

    def test_auth_expired_state_maps_410(self):
        _, start = self.post('/gui/login/start')
        self.auth.status = {'status': 'expired'}
        code, body = self.get_error('/gui/login/poll?device_code=' + parse.quote(start['device_code']))
        self.assertEqual(code, 410)
        self.assertEqual(body['error']['code'], 'expired')

    def test_device_code_ttl_expiry_maps_410(self):
        self.auth.expires_at = self.clock.now + 600  # tie to the manual clock
        _, start = self.post('/gui/login/start')
        self.clock.advance(601)
        code, _ = self.get_error('/gui/login/poll?device_code=' + parse.quote(start['device_code']))
        self.assertEqual(code, 410)

    def test_auth_unavailable_maps_409(self):
        def unavailable(state):
            raise AuthUnavailable('network')
        self.auth.authorize_status = unavailable
        _, start = self.post('/gui/login/start')
        code, body = self.get_error('/gui/login/poll?device_code=' + parse.quote(start['device_code']))
        self.assertEqual(code, 409)
        self.assertEqual(body['error']['code'], 'auth_unavailable')


class SessionsFaceTests(GuiFaceBase):
    def red_line(self, payload):
        """No user OAuth credentials anywhere in GUI responses."""
        raw = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('u-x', raw)
        self.assertNotIn('access_token', raw)
        self.assertNotIn('refresh_token', raw)

    def test_discovery_returns_user_and_full_command_args(self):
        _, poll = self.login()
        token = poll['device_token']
        created = self.create_session()
        status, body = self.get('/gui/sessions', token)
        self.assertEqual(status, 200)
        self.assertEqual(body['user'], {'open_id': 'ou_A', 'name': 'Kai Han',
                                        'email': 'kai.han@nio.com'})
        self.assertEqual(len(body['sessions']), 1)
        item = body['sessions'][0]
        self.assertEqual(item['session_id'], created['session_id'])
        self.assertEqual(item['phase'], 'recording')
        args = item['recorder_command_args']
        self.assertEqual(args['session'], created['session_id'])
        self.assertEqual(args['event'], created['recorder_command_args']['event'])
        self.assertEqual(args['url'], 'ws://127.0.0.1:8400/ingest')
        self.assertEqual(args['token'], created['recorder_command_args']['token'])
        self.assertFalse(args['mic'])
        self.assertEqual(set(args), {'session', 'event', 'url', 'token', 'mic'})
        self.red_line(body)

    def test_other_owners_sessions_are_invisible(self):
        _, poll = self.login()
        self.create_session('111222333')  # owner ou_A via create_session
        self.api.create_session({'owner_open_id': 'ou_B',
                                 'meeting_ref': {'no': '999888777'}})
        _, body = self.get('/gui/sessions', poll['device_token'])
        self.assertEqual({s['meeting_no'] for s in body['sessions']}, {'111222333'})

    def test_draining_session_has_null_args(self):
        _, poll = self.login()
        created = self.create_session()
        self.store.set_phase(created['session_id'], 'draining', now=self.clock())
        _, body = self.get('/gui/sessions', poll['device_token'])
        self.assertEqual(body['sessions'][0]['phase'], 'draining')
        self.assertIsNone(body['sessions'][0]['recorder_command_args'])

    def test_bad_or_missing_token_is_401(self):
        code, body = self.get_error('/gui/sessions', 'dt-forged')
        self.assertEqual(code, 401)
        self.assertEqual(body['error']['code'], 'bad_token')
        code, body = self.get_error('/gui/sessions')
        self.assertEqual(code, 401)

    def test_logout_is_idempotent_and_revokes(self):
        _, poll = self.login()
        token = poll['device_token']
        status, body = self.post('/gui/logout', {}, token)
        self.assertEqual((status, body), (200, {'ok': True}))
        code, _ = self.get_error('/gui/sessions', token)
        self.assertEqual(code, 401)
        status, body = self.post('/gui/logout', {}, token)  # idempotent
        self.assertEqual((status, body), (200, {'ok': True}))


class DeviceStoreTests(unittest.TestCase):
    def test_device_rows_store_hash_only(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'adviser.sqlite3'
            SessionStore.initialize(path)
            store = SessionStore(path)
            self.addCleanup(store.close)
            store.register_device('dt-secret', 'ou_A', name='Kai Han',
                                  email='k@nio.com', now=1.0)
            row = store.device('dt-secret', now=2.0)
            self.assertEqual(row['open_id'], 'ou_A')
            self.assertEqual(row['user_email'], 'k@nio.com')
            import hashlib
            self.assertEqual(row['token_sha256'],
                             hashlib.sha256(b'dt-secret').hexdigest())
            self.assertIsNone(store.device('dt-other'))
            store.drop_device('dt-secret')
            self.assertIsNone(store.device('dt-secret'))


if __name__ == '__main__':
    unittest.main()
