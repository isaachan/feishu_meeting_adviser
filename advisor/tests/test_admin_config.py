"""Admin runtime config face (CONTRACT_ADVISOR_API §9.5): idle_timeout_s.

Semantics pinned here: the stored value applies to **new sessions only**
(mirroring §9.4's no-retroactivity rule); existing sessions keep the value
persisted at creation time.
"""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, request

from advisor.api import AdvisorApi, build_api_server
from advisor.store import SessionStore

OPENER = request.build_opener(request.ProxyHandler({}))


class FakeAuth:
    def get_token(self, open_id, **_kwargs):
        return {'access_token': 't'}

    def get_nsdp_token(self, open_id):
        return {'token': 'x'}

    def get_jira_token(self, open_id):
        return {'token': 'x'}

    def user_profile(self, open_id):
        return None

    def authorize_url(self, open_id, intent=''):
        return 'https://auth.internal/oauth/start'


class FakeCalendar:
    def validate(self, owner_open_id, meeting_ref):
        return {'event_calendar_id': 'cal_x', 'event_id': 'evt_y', 'title': 't',
                'description': '', 'organizer': owner_open_id, 'attendees': set(),
                'start': 1762800000, 'end': 1762803600}


class SupervisorSpy:
    def __init__(self):
        self.started = []

    def start_session(self, session_id):
        self.started.append(session_id)


class ConfigBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.api = AdvisorApi(self.store, SupervisorSpy(), auth_client=FakeAuth(),
                              calendar_validator=FakeCalendar(),
                              ingest_url='ws://127.0.0.1:8400/ingest')
        self.server: ThreadingHTTPServer = build_api_server(self.api, host='127.0.0.1',
                                                            port=0)
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

    def join(self, *, owner='ou_A', no='123456789'):
        self.counter += 1
        return self.post('/sessions', {'owner_open_id': owner,
                                       'meeting_ref': {'no': no}},
                         key=f'join-{self.counter}')


class AdminConfigTests(ConfigBase):
    def test_get_defaults_before_any_write(self):
        status, body = self.get('/admin/config')
        self.assertEqual(status, 200)
        self.assertEqual(body['idle_timeout_s'], 900)
        self.assertEqual(body['default_idle_timeout_s'], 900)
        self.assertIsNone(body['updated_at'])
        self.assertIsNone(body['updated_by'])

    def test_put_persists_and_get_reflects(self):
        status, body = self.post('/admin/config', {'idle_timeout_s': 1200}, key='c1')
        self.assertEqual(status, 200)
        self.assertEqual((body['idle_timeout_s'], body['default_idle_timeout_s']),
                         (1200, 900))
        self.assertEqual(body['updated_by'], 'admin')
        self.assertIsNotNone(body['updated_at'])
        _, current = self.get('/admin/config')
        self.assertEqual(current['idle_timeout_s'], 1200)

    def test_put_rejects_non_int_and_out_of_range(self):
        for bad in ('900', 90.5, True, None, 59, 86401, 0, -1):
            status, body = self.post_error('/admin/config', {'idle_timeout_s': bad},
                                           key=f'c-bad-{bad!r}')
            self.assertEqual((status, body['error']['code']), (400, 'bad_message'),
                             msg=repr(bad))

    def test_put_missing_idempotency_key_rejected(self):
        status, body = self.post_error('/admin/config', {'idle_timeout_s': 1200})
        self.assertEqual((status, body['error']['code']), (400, 'bad_message'))

    def test_boundary_values_accepted(self):
        for good in (60, 86400):
            status, body = self.post('/admin/config', {'idle_timeout_s': good},
                                     key=f'c-ok-{good}')
            self.assertEqual((status, body['idle_timeout_s']), (200, good))

    def test_config_survives_restart(self):
        self.post('/admin/config', {'idle_timeout_s': 1800}, key='c2')
        self.store.close()
        self.store = SessionStore(self.path)
        self.api.store = self.store
        _, current = self.get('/admin/config')
        self.assertEqual(current['idle_timeout_s'], 1800)


class ConfigAppliesToNewSessionsTests(ConfigBase):
    def test_new_sessions_persist_configured_value(self):
        _, body = self.join(no='111111111')
        first = self.store.require_session(body['session_id'])
        self.assertEqual(first['idle_timeout_s'], 900)
        self.post('/admin/config', {'idle_timeout_s': 1200}, key='c3')
        _, second = self.join(owner='ou_B', no='222222222')
        row = self.store.require_session(second['session_id'])
        self.assertEqual(row['idle_timeout_s'], 1200)

    def test_existing_sessions_are_not_retroactively_changed(self):
        _, body = self.join(no='111111111')
        session_id = body['session_id']
        self.post('/admin/config', {'idle_timeout_s': 3600}, key='c4')
        row = self.store.require_session(session_id)
        self.assertEqual(row['idle_timeout_s'], 900)  # kept at creation value


if __name__ == '__main__':
    unittest.main()
