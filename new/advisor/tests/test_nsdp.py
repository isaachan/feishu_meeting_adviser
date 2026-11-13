"""NSDP gate/prefetch (advisor API) + supervisor hold/nag/resume unit tests.

Scenario per aligned design (2026-11-11):
- organizer-surface commands hard-block without an NSDP token (submit link
  in the error); subscriber surface never gates
- join prefetches NSDP material (fetch doubles as implicit token validation)
- mid-session 401 -> hold (all decisions suspended) + nag DM via outbox,
  re-submission resumes decisions
"""
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, request

from advisor.api import AdvisorApi, CalendarError, StubCalendarValidator, build_api_server
from advisor.auth_client import AuthorizationRequired
from advisor.models import Caption, caption_to_dict
from advisor.nsdp import (NsdpService, NsdpUnavailable, NsdpUnauthorized,
                          StubNsdpClient, extract_clues)
from advisor.rule_backend import RuleBackend
from advisor.settings import AdviserSettings
from advisor.store import SessionStore
from advisor.supervisor import SessionSupervisor

OPENER = request.build_opener(request.ProxyHandler({}))


class FakeAuth:
    def __init__(self, mode='ok'):
        self.mode = mode
        self.invalidated = []

    def get_token(self, open_id, *, rejected_token=None):
        if self.mode == 'missing':
            raise AuthorizationRequired('https://auth.internal/oauth/start?state=x')
        return {'access_token': 'u-x', 'expires_at': time.time() + 3600, 'scopes': []}

    def authorize_url(self, open_id, intent=''):
        return f'https://auth.internal/oauth/start?intent={intent}'

    def nsdp_submit_url(self, open_id):
        return f'https://auth.internal/nsdp/submit?state=for-{open_id}'

    def get_nsdp_token(self, open_id):
        if self.mode == 'missing' or open_id in self.invalidated:
            raise AuthorizationRequired(self.nsdp_submit_url(open_id), provider='nsdp')
        return {'token': 'nsdp-token', 'updated_at': 1.0}

    def invalidate_nsdp(self, open_id):
        self.invalidated.append(open_id)


class FakeCalendar(StubCalendarValidator):
    def __init__(self, code=None, description='版本 NT3 BL2.0.0 与票 NT3VFTVDR-111482'):
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


class NsdpApiBase(unittest.TestCase):
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
        self.client = StubNsdpClient(text='NSDP 版本计划（桩）概览文本')
        self.nsdp = NsdpService(self.auth, self.client, self.store)
        self.api = AdvisorApi(self.store, self.spy, auth_client=self.auth,
                              calendar_validator=self.calendar,
                              ingest_url='ws://127.0.0.1:8400/ingest', nsdp=self.nsdp)
        self.server = build_api_server(self.api, host='127.0.0.1', port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.counter = 0

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

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

    def get(self, path):
        req = request.Request(self.url(path), method='GET')
        with OPENER.open(req, timeout=5) as response:
            return response.status, json.loads(response.read())

    def get_error(self, path):
        try:
            self.get(path)
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def join(self, *, key=None, owner='ou_A', no='123456789'):
        return self.post('/sessions', {'owner_open_id': owner,
                                       'meeting_ref': {'no': no}},
                         key=key or f'join-{self.counter}-{no}')


class NsdpGateTests(NsdpApiBase):
    def test_join_blocked_without_nsdp_token(self):
        self.auth.mode = 'missing'
        status, body = self.post_error('/sessions',
                                       {'owner_open_id': 'ou_A',
                                        'meeting_ref': {'no': '123456789'}}, key='g-1')
        self.assertEqual(status, 409)
        self.assertEqual(body['error']['code'], 'authorization_required')
        self.assertEqual(body['error']['provider'], 'nsdp')
        self.assertEqual(body['error']['submit_url'],
                         'https://auth.internal/nsdp/submit?state=for-ou_A')

    def test_every_organizer_command_returns_a_fresh_link(self):
        self.auth.mode = 'missing'
        checks = [
            ('POST', '/sessions', {'owner_open_id': 'ou_A',
                                   'meeting_ref': {'no': '123456789'}}, 'k1'),
            ('POST', '/sessions/xyz/end', {'owner_open_id': 'ou_A'}, 'k2'),
            ('POST', '/subscription-requests/1/decide',
             {'owner_open_id': 'ou_A', 'decision': 'approve'}, 'k3'),
            ('POST', '/subscriptions/1/revoke', {'owner_open_id': 'ou_A'}, 'k4'),
        ]
        for method, path, body, key in checks:
            status, payload = self.post_error(path, body, key=key)
            self.assertEqual((status, payload['error']['provider']), (409, 'nsdp'), path)
        for path in ('/meetings?owner_open_id=ou_A', '/sessions?owner_open_id=ou_A'):
            status, payload = self.get_error(path)
            self.assertEqual((status, payload['error']['provider']), (409, 'nsdp'), path)

    def test_join_prefetches_nsdp_material_with_clues(self):
        status, body = self.join()
        self.assertEqual(status, 200)
        material = self.store.materials(body['session_id'])
        documents = material['documents']
        self.assertEqual([d['kind'] for d in documents], ['nsdp'])
        self.assertIn('概览文本', documents[0]['text'])
        self.assertEqual(material['state'], 'ready')
        self.assertEqual(self.client.calls[0][1],
                         '版本 NT3 BL2.0.0 与票 NT3VFTVDR-111482')

    def test_dead_token_blocks_join_with_submit_link(self):
        class RejectingClient(StubNsdpClient):
            def brief(self, token, clues_text='', *, timeout_s=None):
                self.calls.append((token, clues_text))
                raise NsdpUnauthorized()

        self.nsdp.client = RejectingClient()
        status, body = self.post_error('/sessions',
                                       {'owner_open_id': 'ou_A',
                                        'meeting_ref': {'no': '123456789'}}, key='g-3')
        self.assertEqual(status, 409)
        self.assertEqual(body['error']['provider'], 'nsdp')
        self.assertEqual(self.auth.invalidated, ['ou_A'])  # invalidated on 401

    def test_nsdp_unavailable_degrades_join(self):
        """T7（v1.2a）：NSDP 取数不可达 → 无材料建会，不再拒绝。"""
        class DownClient(StubNsdpClient):
            def __init__(self):
                super().__init__()
                self.calls = []

            def brief(self, token, clues_text='', *, timeout_s=None):
                self.calls.append(clues_text)
                raise NsdpUnavailable('timeout')

        down = DownClient()
        self.nsdp.client = down
        status, body = self.post('/sessions',
                                 {'owner_open_id': 'ou_A',
                                  'meeting_ref': {'no': '123456789'}}, key='g-4')
        self.assertEqual(status, 200)
        self.assertTrue(body['session_id'].startswith('s-'))
        self.assertEqual(len(self.spy.started), 1)
        # material present (calendar description) but without the nsdp document
        material = self.store.materials(body['session_id'])
        documents = material.get('documents') if material else None
        self.assertEqual(documents, [])
        self.assertEqual(down.calls, ['版本 NT3 BL2.0.0 与票 NT3VFTVDR-111482'])

    def test_join_uses_short_nsdp_budget(self):
        """T7：建会取数用短预算（默认 12s）快速降级，后台刷新才用默认 60s。"""
        status, body = self.post('/sessions',
                                 {'owner_open_id': 'ou_A',
                                  'meeting_ref': {'no': '123456789'}}, key='g-budget')
        self.assertEqual(status, 200)
        self.assertEqual(self.client.timeouts, [12.0])

    def test_subscriber_surface_never_gates(self):
        status, body = self.join()
        self.assertEqual(status, 200)
        self.auth.mode = 'missing'
        status, body = self.post('/subscription-requests',
                                 {'subscriber_open_id': 'ou_B',
                                  'meeting_no': '123456789'}, key='sub-1')
        self.assertIn(status, (200, 201))  # not blocked by the NSDP gate
        self.assertIn('request_no', body)
        status, body = self.get('/subscriptions?subscriber_open_id=ou_B')
        self.assertEqual(status, 200)
        status, body = self.post_error('/subscriptions/1/revoke',
                                       {'subscriber_open_id': 'ou_B'}, key='rev-1')
        self.assertNotEqual(body.get('error', {}).get('code'), 'authorization_required')

    def test_status_reports_nsdp_hold(self):
        status, body = self.join()
        self.assertFalse(body.get('nsdp_hold'))  # session response has no field
        session_id = body['session_id']
        self.store.set_nsdp_hold(session_id, True)
        status, status_body = self.get(f'/sessions/{session_id}/status')
        self.assertTrue(status_body['nsdp_hold'])


class ClueTests(unittest.TestCase):
    def test_extract_clues_prefers_versions_then_platforms(self):
        text = 'NT3 平台 BL2.0.0 与 BL1.5.0，另见 NT2；重复 BL2.0.0'
        self.assertEqual(extract_clues(text), ['BL2.0.0', 'BL1.5.0', 'NT3'])

    def test_extract_clues_empty(self):
        self.assertEqual(extract_clues(''), [])
        self.assertEqual(extract_clues(None), [])


class SupervisorHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.now = [1000.0]
        self.settings = AdviserSettings(poll_interval=0.02,
                                        nsdp_refresh_seconds=5,
                                        nsdp_retry_seconds=1,
                                        nsdp_nag_seconds=3)
        self.auth = FakeAuth()
        self.client = StubNsdpClient(text='刷新后的版本计划')
        self.nsdp = NsdpService(self.auth, self.client, self.store,
                                clock=lambda: self.now[0])

    def _wait(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _make_supervisor(self):
        return SessionSupervisor(
            self.store, self.settings, backend_factory=lambda: RuleBackend(),
            ingest_activity=lambda: {}, close_ingest=lambda _sid: None,
            source_connected=lambda *_: True,
            clock=lambda: self.now[0], monotonic=lambda: self.now[0],
            nsdp=self.nsdp)

    def _create_session(self):
        session = self.store.create_session(
            owner_open_id='ou_A', meeting_no='123456789', title='t',
            event_calendar_id='cal', event_id='evt', description='NT3 BL2.0.0',
            now=self.now[0], nsdp_text='初始版本计划')
        return session['session_id']

    def _job_rows(self, session_id):
        return self.store.db.execute(
            'SELECT count(*) FROM jobs WHERE session_id=?', (session_id,)).fetchone()[0]

    def _push_caption(self, session_id, text):
        caption = Caption(meeting_id='m', sentence_key=f'k-{time.monotonic()}',
                          text=text, received_at='2026-11-11T10:00:00+08:00',
                          end_time=str(int(self.now[0] * 1000)))
        with self.store.unit_of_work() as tx:
            tx.connection.execute(
                'INSERT INTO context(session_id,sentence_key,body,suppressed,generation) '
                'VALUES (?,?,?,?,?)',
                (session_id, caption.sentence_key,
                 json.dumps(caption_to_dict(caption), ensure_ascii=False), 0, 1))
            tx.connection.execute(
                'UPDATE sessions SET generation=generation+1 WHERE session_id=?',
                (session_id,))

    def test_degraded_join_backfills_nsdp_material(self):
        """T7：降级建会（无 NSDP 材料）→ supervisor 首个维护周期自动补取。"""
        session_id = self.store.create_session(
            owner_open_id='ou_A', meeting_no='123456789', title='t',
            event_calendar_id='cal', event_id='evt', description='NT3 BL2.0.0',
            now=self.now[0], nsdp_text='')['session_id']
        material = self.store.materials(session_id)
        self.assertEqual(material.get('documents'), [])  # degraded: no nsdp doc
        supervisor = self._make_supervisor()
        supervisor.start_session(session_id)
        self.addCleanup(supervisor.stop, timeout=2)
        self.assertTrue(
            self._wait(lambda: (self.store.materials(session_id) or {}).get('documents')),
            'nsdp material never backfilled')
        document = self.store.materials(session_id)['documents'][0]
        self.assertEqual(document['kind'], 'nsdp')
        self.assertEqual(document['text'], '刷新后的版本计划')

    def test_hold_nag_resume_cycle(self):
        session_id = self._create_session()
        supervisor = self._make_supervisor()
        supervisor.start_session(session_id)
        self.addCleanup(supervisor.stop, timeout=2)

        # healthy: first maintenance refresh runs without holding
        self.assertTrue(self._wait(lambda: self.client.calls),
                        'first NSDP refresh never ran')
        self.assertFalse(self.store.session(session_id)['nsdp_hold'])

        # token dies server-side -> next refresh holds + nags the organizer
        self.auth.invalidated.append('ou_A')
        self.now[0] += self.settings.nsdp_refresh_seconds + 1
        self.assertTrue(self._wait(lambda: self.store.status(session_id)['nsdp_hold']),
                        'session never went on hold')
        rows = self.store.pending_im_rows(limit=10)
        self.assertTrue(any(r['recipient_id'] == 'ou_A'
                            and '/nsdp/submit?' in r['content'] for r in rows),
                        rows)

        # held sessions launch no model jobs even with fresh captions
        before = self._job_rows(session_id)
        self._push_caption(session_id, '新字幕')
        self.now[0] += 0.1
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self._job_rows(session_id), before, 'job launched while held')

        # re-submission -> resume + refreshed material
        self.auth.invalidated.clear()
        self.now[0] += self.settings.nsdp_retry_seconds + 1
        self.assertTrue(self._wait(lambda: not self.store.status(session_id)['nsdp_hold']),
                        'session never resumed')
        material = self.store.materials(session_id)
        self.assertIn('刷新后的版本计划', material['documents'][0]['text'])

        # decisions flow again after resume
        self._push_caption(session_id, '恢复后的字幕')
        self.assertTrue(self._wait(lambda: self._job_rows(session_id) > before),
                        'no job launched after resume')

    def test_nag_is_rate_limited(self):
        session_id = self._create_session()
        self.settings = AdviserSettings(poll_interval=0.02,
                                        nsdp_refresh_seconds=5,
                                        nsdp_retry_seconds=1,
                                        nsdp_nag_seconds=10)
        self.nsdp = NsdpService(self.auth, self.client, self.store,
                                clock=lambda: self.now[0])
        supervisor = self._make_supervisor()
        supervisor.start_session(session_id)
        self.addCleanup(supervisor.stop, timeout=2)
        self.assertTrue(self._wait(lambda: self.client.calls))
        self.auth.invalidated.append('ou_A')
        self.now[0] += self.settings.nsdp_refresh_seconds + 1
        self.assertTrue(self._wait(lambda: self.store.status(session_id)['nsdp_hold']))
        first = len(self.store.pending_im_rows(limit=50))
        self.assertGreaterEqual(first, 1)
        # two more retry ticks inside the nag window -> no extra DMs
        for _ in range(2):
            self.now[0] += self.settings.nsdp_retry_seconds + 1
            time.sleep(0.3)
        self.assertEqual(len(self.store.pending_im_rows(limit=50)), first)

    def test_nsdp_outage_does_not_hold(self):
        class DownClient(StubNsdpClient):
            def __init__(self):
                super().__init__()
                self.ran = False

            def brief(self, token, clues_text='', *, timeout_s=None):
                self.ran = True
                raise NsdpUnavailable('exit=1')

        down = DownClient()
        self.nsdp.client = down
        session_id = self._create_session()
        supervisor = self._make_supervisor()
        supervisor.start_session(session_id)
        self.addCleanup(supervisor.stop, timeout=2)
        self.assertTrue(self._wait(lambda: down.ran))
        time.sleep(0.2)
        self.assertFalse(self.store.status(session_id)['nsdp_hold'])


class NsdpClientTests(unittest.TestCase):
    """brief() command selection: targeted releases beat the slow catalog walk."""

    @staticmethod
    def client_with(calls, *, rc=0, stdout='plan text', stderr=''):
        import subprocess

        from advisor.nsdp import NsdpClient

        def fake_run(argv, env, timeout=None):
            calls.append(tuple(argv[1:]))
            assert 'NSDP_TOKEN' in env and 'token' not in ' '.join(argv)
            return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)
        return NsdpClient(noam_bin='noam', run=fake_run)

    def test_clues_skip_whole_catalog_info(self):
        calls = []
        client = NsdpClientTests.client_with(calls)
        text = client.brief('tok', '版本 BL2.1.0 与平台 NT3')
        self.assertEqual(calls, [('nsdp', 'releases', '--release', 'BL2.1.0'),
                                 ('nsdp', 'releases', '--release', 'NT3')])
        self.assertIn('BL2.1.0', text)
        self.assertIn('$ noam nsdp releases', text)
        self.assertNotIn('nsdp info', text)

    def test_no_clues_fall_back_to_info(self):
        calls = []
        client = NsdpClientTests.client_with(calls)
        client.brief('tok', '没有线索的描述')
        self.assertEqual(calls, [('nsdp', 'info')])

    def test_timeout_maps_to_unavailable(self):
        import subprocess
        from advisor.nsdp import NsdpClient

        def fake_run(argv, env, timeout=None):
            raise subprocess.TimeoutExpired(argv, 60)
        client = NsdpClient(noam_bin='noam', run=fake_run)
        with self.assertRaises(NsdpUnavailable) as ctx:
            client.brief('tok')
        self.assertIn('timeout', str(ctx.exception))

    def test_exit_401_shape_maps_to_unauthorized(self):
        import json as _json
        calls = []
        client = NsdpClientTests.client_with(
            calls, rc=1, stdout='', stderr=_json.dumps({'status_code': 401}))
        with self.assertRaises(NsdpUnauthorized):
            client.brief('tok')

    def test_successful_brief_is_cached_per_token_and_clues(self):
        from advisor.nsdp import NsdpClient

        calls = []
        now = [1000.0]

        def fake_run(argv, env, timeout=None):
            calls.append(tuple(argv[1:]))
            import subprocess
            return subprocess.CompletedProcess(argv, 0, stdout='plan', stderr='')

        client = NsdpClient(noam_bin='noam', cache_ttl_s=600,
                            clock=lambda: now[0], run=fake_run)
        first = client.brief('tok', 'NT3 线索')
        second = client.brief('tok', 'NT3 线索')
        self.assertIs(first, second)  # cache hit, no extra subprocess
        self.assertEqual(len(calls), 1)
        # different clues → separate entry; different token → separate entry
        client.brief('tok', 'BL2.0 线索')
        client.brief('tok-2', 'NT3 线索')
        self.assertEqual(len(calls), 3)
        # TTL expiry → refetch
        now[0] += 601
        client.brief('tok', 'NT3 线索')
        self.assertEqual(len(calls), 4)

    def test_failed_brief_is_never_cached(self):
        import subprocess

        class FailingThenOk:
            def __init__(self):
                self.calls = 0

            def __call__(self, argv, env, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(argv, 60)
                return subprocess.CompletedProcess(argv, 0, stdout='plan', stderr='')

        runner = FailingThenOk()
        from advisor.nsdp import NsdpClient
        client = NsdpClient(noam_bin='noam', run=runner, clock=lambda: 0.0)
        with self.assertRaises(NsdpUnavailable):
            client.brief('tok')
        client.brief('tok')  # second attempt runs again and succeeds
        self.assertEqual(runner.calls, 2)

    def test_timeout_budget_passes_through_to_run(self):
        """Join 路径短预算（T7）：brief(timeout_s=) 逐调用覆盖默认。"""
        import subprocess

        from advisor.nsdp import NsdpClient
        seen = []

        def fake_run(argv, env, timeout=None):
            seen.append(timeout)
            return subprocess.CompletedProcess(argv, 0, stdout='plan', stderr='')

        client = NsdpClient(noam_bin='noam', timeout_s=60, run=fake_run,
                            clock=lambda: 0.0)
        client.brief('tok-a', timeout_s=5)
        client.brief('tok-b')  # default budget
        self.assertEqual(seen, [5, 60])


if __name__ == '__main__':
    unittest.main()
