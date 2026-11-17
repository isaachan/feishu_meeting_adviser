"""Supervisor grant admin face (CONTRACT_ADVISOR_API §9.3/§9.4, SM-03)."""
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


class FakeCalendar:
    def validate(self, owner_open_id, meeting_ref):
        return {'event_calendar_id': 'cal_x', 'event_id': 'evt_y', 'title': 't',
                'description': '', 'organizer': owner_open_id, 'attendees': set(),
                'start': 1762800000, 'end': 1762803600}


class GrantsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.api = AdvisorApi(self.store, supervisor=None, auth_client=FakeAuth(),
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


class SupervisorGrantTests(GrantsBase):
    def seed_identity(self, open_id='ou_C', email='kai.han@nio.com', name='Kai Han'):
        self.store.upsert_user(open_id, name=name, email=email, now=1.0)

    def test_put_resolves_login_to_unique_open_id(self):
        self.seed_identity()
        status, body = self.post('/admin/supervisors',
                                 {'login_name': 'kai.han', 'enabled': True}, key='g1')
        self.assertEqual(status, 200)
        self.assertEqual((body['open_id'], body['enabled']), ('ou_C', True))
        self.assertEqual(self.store.enabled_supervisor_open_ids(), {'ou_C'})

    def test_put_accepts_full_email_and_open_id(self):
        self.seed_identity()
        _, by_email = self.post('/admin/supervisors',
                                {'login_name': 'kai.han@nio.com', 'enabled': True}, key='g2')
        self.assertEqual(by_email['open_id'], 'ou_C')
        _, by_id = self.post('/admin/supervisors',
                             {'login_name': 'ou_D', 'enabled': True}, key='g3')
        self.assertEqual(by_id['open_id'], 'ou_D')

    def test_unresolved_identity_is_rejected_and_disabled(self):
        status, body = self.post_error('/admin/supervisors',
                                       {'login_name': 'nobody.here', 'enabled': True},
                                       key='g4')
        self.assertEqual((status, body['error']['code']), (409, 'identity_unresolved'))
        _, listing = self.get('/admin/supervisors')
        stored = [r for r in listing['supervisors']
                  if r['login_name'] == 'nobody.here'][0]
        self.assertFalse(stored['enabled'])
        self.assertIsNone(stored['open_id'])

    def test_ambiguous_email_rejected(self):
        self.store.upsert_user('ou_C1', name='Kai', email='dupe@nio.com', now=1.0)
        self.store.upsert_user('ou_C2', name='Kai2', email='dupe@nio.com', now=1.0)
        status, body = self.post_error('/admin/supervisors',
                                       {'login_name': 'dupe@nio.com', 'enabled': True},
                                       key='g5')
        self.assertEqual((status, body['error']['code']), (409, 'identity_unresolved'))

    def test_listing_and_disable_persist(self):
        self.seed_identity()
        self.post('/admin/supervisors', {'login_name': 'kai.han', 'enabled': True}, key='g6')
        self.post('/admin/supervisors', {'login_name': 'kai.han', 'enabled': False}, key='g7')
        _, listing = self.get('/admin/supervisors')
        row = listing['supervisors'][0]
        self.assertEqual((row['login_name'], row['enabled']), ('kai.han', False))
        self.assertEqual(self.store.enabled_supervisor_open_ids(), set())

    def test_bad_body_rejected(self):
        status, body = self.post_error('/admin/supervisors',
                                       {'login_name': 'kai.han'}, key='g8')
        self.assertEqual((status, body['error']['code']), (400, 'bad_message'))

    def test_missing_idempotency_key_rejected(self):
        # contract §"所有 POST 必须携带 X-Idempotency-Key"; the admin-site client
        # regression (2026-11-17) shipped without the header — pin the seam.
        status, body = self.post_error('/admin/supervisors',
                                       {'login_name': 'kai.han', 'enabled': True})
        self.assertEqual((status, body['error']['code']), (400, 'bad_message'))

    def test_grants_survive_restart(self):
        self.seed_identity()
        self.post('/admin/supervisors', {'login_name': 'kai.han', 'enabled': True}, key='g9')
        self.store.close()
        self.store = SessionStore(self.path)
        self.assertEqual(self.store.enabled_supervisor_open_ids(), {'ou_C'})


if __name__ == '__main__':
    unittest.main()
