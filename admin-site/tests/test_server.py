"""Admin-site HTTP surface: advisor proxy passthrough + SSE log streaming."""
import http.client
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, request

from admin_site.advisor_client import AdvisorAdminClient
from admin_site.auth import AdminSessions
from admin_site.server import build_server

OPENER = request.build_opener(request.ProxyHandler({}))


class FakeAuthClient:
    def __init__(self):
        self.state = 'st-fake'
        self.start_calls = 0
        self.status = {'status': 'completed', 'user': {
            'open_id': 'ou_KAI', 'name': 'Kai Han', 'user_id': 'kai.han'}}

    def authorize_start(self):
        self.start_calls += 1
        return {'authorize_url': ('https://accounts.feishu.cn/open-apis/authen/v1/'
                                  f'authorize?state={self.state}'),
                'state': self.state, 'expires_at': 0}

    def authorize_status(self, state):
        return dict(self.status)


class StubAdvisorHandler(BaseHTTPRequestHandler):
    calls = []  # shared per test via handler_class attribute

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == '/admin/sessions':
            body = {'sessions': [{'session_id': 's-1', 'owner_open_id': 'ou_A',
                                  'meeting_no': '123', 'title': '周会',
                                  'phase': 'running', 'started_at': 1762800000.0,
                                  'ended_at': None, 'nsdp_hold': False}]}
            self._reply(body)
        elif self.path == '/admin/sessions/s-1/transcript':
            self._reply({'session_id': 's-1', 'meeting_no': '123', 'title': '周会',
                         'records': [
                             {'type': 'human', 'time': 1762800010.0, 'speaker': None,
                              'text': '我们过一下预算'},
                             {'type': 'llm', 'time': 1762800015.0, 'speaker': 'LLM',
                              'text': '建议补充预算数据'}]})
        elif self.path == '/admin/sessions/s-1/timings':
            self._reply({'session_id': 's-1', 'meeting_no': '123', 'title': '周会',
                         'jobs': [
                             {'job_id': 'job-1', 'generation': 1, 'state': 'succeeded',
                              'created_ns': 1762800010000000000,
                              'timings': {'execution_ms': 900, 'process_start_ms': 300,
                                          'decision_wait_ms': 500, 'request_id': 'req-1',
                                          'end_to_end_ms': 1200}},
                             {'job_id': 'job-2', 'generation': 2, 'state': 'pending',
                              'created_ns': 1762800020000000000, 'timings': None}]})
        elif self.path == '/admin/supervisors':
            self._reply({'supervisors': [{'login_name': 'kai.han', 'open_id': 'ou_C',
                                          'enabled': True, 'updated_at': 1762800000.0,
                                          'updated_by': 'admin'}]})
        elif self.path == '/admin/config':
            self._reply({'idle_timeout_s': 900, 'default_idle_timeout_s': 900,
                         'updated_at': None, 'updated_by': None})
        else:
            self._reply({'error': {'code': 'not_found', 'message': 'nope'}}, 404)

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        type(self).calls.append(('POST', self.path, raw.decode(),
                                 self.headers.get('X-Idempotency-Key')))
        if self.path == '/admin/sessions/s-1/end':
            self._reply({'session_id': 's-1', 'phase': 'draining'})
        elif self.path == '/admin/supervisors':
            body = json.loads(raw)
            self._reply({'login_name': body['login_name'], 'open_id': 'ou_C',
                         'enabled': body['enabled'], 'updated_at': 1762800000.0,
                         'updated_by': 'admin'})
        elif self.path == '/admin/config':
            body = json.loads(raw)
            if getattr(type(self), 'reject_config', False):
                self._reply({'error': {'code': 'bad_message',
                                       'message': 'idle_timeout_s out of range'}}, 400)
            else:
                self._reply({'idle_timeout_s': body['idle_timeout_s'],
                             'default_idle_timeout_s': 900,
                             'updated_at': 1762800000.0, 'updated_by': 'admin'})
        else:
            self._reply({'error': {'code': 'not_found', 'message': 'nope'}}, 404)

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AdminSiteBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = Path(self.tmp.name)
        (self.logs / 'advisor.log').write_text('line-1\nline-2\n', encoding='utf-8')

        handler = type('Handler', (StubAdvisorHandler,), {'calls': []})
        self.stub_calls = handler.calls
        self.stub = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.stub_port = self.stub.server_address[1]
        threading.Thread(target=self.stub.serve_forever, daemon=True).start()
        self.addCleanup(self.stub.shutdown)
        self.addCleanup(self.stub.server_close)

        self.fake_auth = FakeAuthClient()
        self.server = build_server(AdvisorAdminClient(f'http://127.0.0.1:{self.stub_port}'),
                                   self.logs, admin_users=('kai.han',),
                                   auth_client=self.fake_auth,
                                   host='127.0.0.1', port=0,
                                   bootstrap_bytes=1024, poll_s=0.05)
        self.port = self.server.server_address[1]
        self.session_token = self.server.admin_sessions.issue(
            'ou_KAI', 'kai.han', 'Kai Han')
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def open(self, req, *, timeout=5, token=None):
        if isinstance(req, str):
            req = request.Request(req)
        req.add_header('Cookie', f'admin_session={token or self.session_token}')
        return OPENER.open(req, timeout=timeout)

    def raw(self, path, *, method='GET', cookie=None, port=None):
        headers = {'Cookie': cookie} if cookie else {}
        conn = http.client.HTTPConnection('127.0.0.1', port or self.port, timeout=5)
        try:
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            body = response.read()
            return (response.status, body, response.getheader('Location'),
                    response.headers.get_all('Set-Cookie') or [])
        finally:
            conn.close()

    def get(self, path):
        with self.open(request.Request(self.url(path), method='GET')) as response:
            return response.status, json.loads(response.read()), response.headers

    def get_error(self, path):
        try:
            self.get(path)
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def post(self, path, body=b'', *, token=None):
        req = request.Request(self.url(path), data=body, method='POST')
        with self.open(req, token=token) as response:
            return response.status, json.loads(response.read())

    def post_error(self, path, body=b''):
        req = request.Request(self.url(path), data=body, method='POST')
        try:
            with self.open(req) as response:
                self.fail('expected HTTPError')
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class BasicTests(AdminSiteBase):
    def test_healthz(self):
        status, body, _ = self.get('/healthz')
        self.assertEqual((status, body['status']), (200, 'ok'))

    def test_index_served_as_html(self):
        with self.open(self.url('/')) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('text/html', response.headers['Content-Type'])
            self.assertIn('运维台', response.read().decode('utf-8'))

    def test_index_api_and_log_requests_work_under_admin_prefix(self):
        with self.open(self.url('/')) as response:
            html = response.read().decode('utf-8')
        for path in ('api/sessions', 'api/services', 'api/logs/'):
            self.assertIn(path, html)
            self.assertNotIn('/' + path, html)

    def test_services_lists_five_with_existence(self):
        status, body, _ = self.get('/api/services')
        keys = [s['key'] for s in body['services']]
        self.assertEqual(keys, ['advisor', 'bot', 'stt-server', 'gateway', 'auth'])
        by_key = {s['key']: s for s in body['services']}
        self.assertTrue(by_key['advisor']['exists'])
        self.assertEqual(by_key['advisor']['size'], len('line-1\nline-2\n'.encode()))
        self.assertFalse(by_key['bot']['exists'])


class AuthenticationTests(AdminSiteBase):
    @staticmethod
    def cookie_value(headers, name):
        for header in headers:
            first = header.split(';', 1)[0]
            if first.startswith(name + '='):
                return first[len(name) + 1:]
        return None

    def test_unauthenticated_page_redirects_to_login(self):
        status, _, location, _ = self.raw('/')
        self.assertEqual(status, 302)
        self.assertEqual(location, '/login')

    def test_unauthenticated_api_is_401(self):
        for path in ('/api/sessions', '/api/me'):
            status, body, _, _ = self.raw(path)
            self.assertEqual(status, 401, msg=path)
            self.assertEqual(json.loads(body)['error']['code'], 'unauthorized')

    def test_unauthenticated_log_stream_is_401(self):
        status, body, _, _ = self.raw('/api/logs/advisor/stream')
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)['error']['code'], 'unauthorized')

    def test_login_sets_state_cookie_and_redirects_to_feishu(self):
        status, _, location, headers = self.raw('/login')
        self.assertEqual(status, 302)
        self.assertTrue(location.startswith('https://accounts.feishu.cn/'))
        state = self.cookie_value(headers, 'admin_oauth_state')
        self.assertEqual(state, 'st-fake')
        cookie_header = next(h for h in headers if h.startswith('admin_oauth_state='))
        self.assertIn('HttpOnly', cookie_header)
        self.assertIn('SameSite=Lax', cookie_header)

    def test_callback_creates_session_and_redirects_home(self):
        _, _, _, login_headers = self.raw('/login')
        state = self.cookie_value(login_headers, 'admin_oauth_state')
        status, _, location, headers = self.raw(
            f'/oauth/callback?state={state}',
            cookie=f'admin_oauth_state={state}')
        self.assertEqual(status, 302)
        self.assertEqual(location, '/')
        token = self.cookie_value(headers, 'admin_session')
        self.assertTrue(token)
        status, page, _, _ = self.raw('/', cookie=f'admin_session={token}')
        self.assertEqual(status, 200)
        self.assertIn('运维台', page.decode('utf-8'))
        status, body, _, _ = self.raw('/api/me', cookie=f'admin_session={token}')
        self.assertEqual(status, 200)
        me = json.loads(body)
        self.assertEqual((me['open_id'], me['user_id'], me['name']),
                         ('ou_KAI', 'kai.han', 'Kai Han'))

    def test_callback_rejects_state_mismatch(self):
        status, page, _, _ = self.raw(
            '/oauth/callback?state=evil', cookie='admin_oauth_state=st-fake')
        self.assertEqual(status, 400)
        self.assertIn('无效', page.decode('utf-8'))

    def test_callback_denies_non_whitelisted_user(self):
        self.fake_auth.status['user']['user_id'] = 'pengw.wang'
        _, _, _, login_headers = self.raw('/login')
        state = self.cookie_value(login_headers, 'admin_oauth_state')
        status, page, _, headers = self.raw(
            f'/oauth/callback?state={state}',
            cookie=f'admin_oauth_state={state}')
        self.assertEqual(status, 403)
        self.assertIn('pengw.wang', page.decode('utf-8'))
        self.assertIsNone(self.cookie_value(headers, 'admin_session'))

    def test_logout_revokes_session(self):
        status, _, location, _ = self.raw(
            '/logout', cookie=f'admin_session={self.session_token}')
        self.assertEqual(status, 302)
        self.assertEqual(location, '/login')
        status, _, _, _ = self.raw(
            '/api/sessions', cookie=f'admin_session={self.session_token}')
        self.assertEqual(status, 401)

    def test_public_prefix_applies_to_redirects_and_cookie_path(self):
        server = build_server(AdvisorAdminClient(f'http://127.0.0.1:{self.stub_port}'),
                              self.logs, admin_users=('kai.han',),
                              auth_client=self.fake_auth, public_prefix='/admin',
                              host='127.0.0.1', port=0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        status, _, location, _ = self.raw('/', port=port)
        self.assertEqual(status, 302)
        self.assertEqual(location, '/admin/login')
        _, _, _, headers = self.raw('/login', port=port)
        cookie_header = next(h for h in headers if h.startswith('admin_oauth_state='))
        self.assertIn('Path=/admin;', cookie_header)


class SessionStoreTests(unittest.TestCase):
    def test_expiry_and_revoke(self):
        now = [1000.0]
        sessions = AdminSessions(ttl_s=10, clock=lambda: now[0])
        token = sessions.issue('ou_KAI', 'kai.han', 'Kai Han')
        self.assertEqual(sessions.get(token)['user_id'], 'kai.han')
        now[0] += 11
        self.assertIsNone(sessions.get(token))
        token = sessions.issue('ou_KAI', 'kai.han')
        sessions.revoke(token)
        self.assertIsNone(sessions.get(token))


class SessionProxyTests(AdminSiteBase):
    def test_sessions_passthrough(self):
        status, body, _ = self.get('/api/sessions')
        self.assertEqual(status, 200)
        self.assertEqual(body['sessions'][0]['session_id'], 's-1')

    def test_end_posts_to_admin_face_without_body(self):
        status, body = self.post('/api/sessions/s-1/end')
        self.assertEqual((status, body['phase']), (200, 'draining'))
        self.assertIn(('POST', '/admin/sessions/s-1/end', '', None), self.stub_calls)

    def test_advisor_error_status_passthrough(self):
        status, body = self.post_error('/api/sessions/s-unknown/end')
        self.assertEqual(status, 404)
        self.assertEqual(body['error']['code'], 'not_found')

    def test_transcript_proxied(self):
        status, body, _ = self.get('/api/sessions/s-1/transcript')
        self.assertEqual(status, 200)
        self.assertEqual(body['session_id'], 's-1')
        self.assertEqual([r['type'] for r in body['records']], ['human', 'llm'])
        self.assertEqual(body['records'][0]['speaker'], None)

    def test_transcript_unknown_session_passthrough(self):
        status, body = self.get_error('/api/sessions/s-nope/transcript')
        self.assertEqual(status, 404)
        self.assertEqual(body['error']['code'], 'not_found')

    def test_timings_proxied(self):
        status, body, _ = self.get('/api/sessions/s-1/timings')
        self.assertEqual(status, 200)
        self.assertEqual(len(body['jobs']), 2)
        self.assertEqual(body['jobs'][0]['timings']['decision_wait_ms'], 500)
        self.assertIsNone(body['jobs'][1]['timings'])

    def test_timings_unknown_session_passthrough(self):
        status, body = self.get_error('/api/sessions/s-nope/timings')
        self.assertEqual(status, 404)
        self.assertEqual(body['error']['code'], 'not_found')

    def test_advisor_unreachable_maps_to_502(self):
        server = build_server(AdvisorAdminClient('http://127.0.0.1:1'),
                              self.logs, admin_users=('kai.han',),
                              auth_client=self.fake_auth, host='127.0.0.1', port=0)
        port = server.server_address[1]
        token = server.admin_sessions.issue('ou_KAI', 'kai.han')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        req = request.Request(f'http://127.0.0.1:{port}/api/sessions', method='GET')
        try:
            with self.open(req, token=token) as response:
                self.fail('expected HTTPError')
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 502)
            self.assertEqual(json.loads(exc.read())['error']['code'], 'advisor_unreachable')


class LogStreamTests(AdminSiteBase):
    def read_events(self, conn, response, want, deadline_s=3.0):
        events, buf = [], b''
        deadline = time.monotonic() + deadline_s
        while len(events) < want and time.monotonic() < deadline:
            try:
                chunk = response.read1(512)  # read(n) would block for n bytes
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            buf += chunk
            while b'\n\n' in buf:
                raw, buf = buf.split(b'\n\n', 1)
                if raw.startswith(b'data: '):
                    events.append(json.loads(raw[len(b'data: '):].decode('utf-8')))
        return events

    def test_stream_bootstraps_then_follows_appends(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        conn.request('GET', '/api/logs/advisor/stream',
                     headers={'Cookie': f'admin_session={self.session_token}'})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn('text/event-stream', response.headers['Content-Type'])
        try:
            first = self.read_events(conn, response, 1)
            self.assertEqual(len(first), 1)
            self.assertIn('line-1\nline-2\n', first[0])
            with open(self.logs / 'advisor.log', 'a', encoding='utf-8') as fh:
                fh.write('line-3\n')
            events = self.read_events(conn, response, 2)
            self.assertTrue(any('line-3\n' in text for text in events),
                            msg=f'line-3 not in {events}')
        finally:
            conn.close()

    def test_unknown_service_404(self):
        try:
            self.get('/api/logs/nope/stream')
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

    def test_missing_log_streams_waiting_note(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        conn.request('GET', '/api/logs/bot/stream',
                     headers={'Cookie': f'admin_session={self.session_token}'})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        try:
            events = self.read_events(conn, response, 1)
            self.assertTrue(any('不存在' in text for text in events))
        finally:
            conn.close()


class SupervisorProxyTests(AdminSiteBase):
    def test_supervisors_listing_proxied(self):
        with self.open(request.Request(
                f'http://127.0.0.1:{self.port}/api/supervisors', method='GET')) as response:
            body = json.loads(response.read())
        self.assertEqual(body['supervisors'][0]['login_name'], 'kai.han')

    def test_put_supervisor_forwards_json_body(self):
        req = request.Request(
            f'http://127.0.0.1:{self.port}/api/supervisors', method='POST',
            data=json.dumps({'login_name': 'kai.han', 'enabled': False}).encode(),
            headers={'Content-Type': 'application/json'})
        with self.open(req) as response:
            body = json.loads(response.read())
        self.assertFalse(body['enabled'])
        forwarded = [c for c in self.stub_calls if c[1] == '/admin/supervisors']
        self.assertEqual(json.loads(forwarded[-1][2]),
                         {'login_name': 'kai.han', 'enabled': False})

    def test_put_supervisor_sends_fresh_idempotency_key(self):
        # CONTRACT_ADVISOR_API: every POST carries X-Idempotency-Key; advisor
        # rejects /admin/supervisors without one (400 bad_message).
        for enabled in (True, False, True):
            self.post('/api/supervisors',
                      json.dumps({'login_name': 'kai.han', 'enabled': enabled}).encode())
        keys = [c[3] for c in self.stub_calls if c[1] == '/admin/supervisors']
        self.assertEqual(len(keys), 3)
        self.assertTrue(all(isinstance(k, str) and 0 < len(k) <= 128 for k in keys))
        self.assertEqual(len(set(keys)), 3, 'each logical request must use a fresh key')


class ConfigProxyTests(AdminSiteBase):
    def test_config_get_proxied(self):
        status, body, _ = self.get('/api/config')
        self.assertEqual(status, 200)
        self.assertEqual((body['idle_timeout_s'], body['default_idle_timeout_s']),
                         (900, 900))
        self.assertIsNone(body['updated_at'])

    def test_config_post_forwards_body_with_fresh_idempotency_key(self):
        for value in (1200, 900):
            req = request.Request(
                f'http://127.0.0.1:{self.port}/api/config', method='POST',
                data=json.dumps({'idle_timeout_s': value}).encode(),
                headers={'Content-Type': 'application/json'})
            with self.open(req) as response:
                body = json.loads(response.read())
            self.assertEqual(body['idle_timeout_s'], value)
        forwarded = [c for c in self.stub_calls if c[1] == '/admin/config']
        self.assertEqual([json.loads(c[2])['idle_timeout_s'] for c in forwarded],
                         [1200, 900])
        keys = [c[3] for c in forwarded]
        self.assertTrue(all(isinstance(k, str) and 0 < len(k) <= 128 for k in keys))
        self.assertEqual(len(set(keys)), 2, 'each save must use a fresh key')

    def test_config_error_passthrough(self):
        # advisor-side validation failure surfaces with status/code intact
        handler = type('Handler', (StubAdvisorHandler,),
                       {'calls': [], 'reject_config': True})
        stub = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        port = stub.server_address[1]
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        self.addCleanup(stub.shutdown)
        self.addCleanup(stub.server_close)
        server = build_server(AdvisorAdminClient(f'http://127.0.0.1:{port}'),
                              self.logs, admin_users=('kai.han',),
                              auth_client=self.fake_auth, host='127.0.0.1', port=0)
        admin_port = server.server_address[1]
        token = server.admin_sessions.issue('ou_KAI', 'kai.han')
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        req = request.Request(
            f'http://127.0.0.1:{admin_port}/api/config', method='POST',
            data=b'{"idle_timeout_s": 5}', headers={'Content-Type': 'application/json'})
        try:
            with self.open(req, token=token) as response:
                self.fail('expected HTTPError')
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertEqual(json.loads(exc.read())['error']['code'], 'bad_message')

    def test_index_has_settings_tab(self):
        with self.open(self.url('/')) as response:
            html = response.read().decode('utf-8')
        for marker in ('tab-settings', 'view-settings', 'api/config', 'idle-save'):
            self.assertIn(marker, html)

    def test_index_shows_current_user_and_logout(self):
        with self.open(self.url('/')) as response:
            html = response.read().decode('utf-8')
        for marker in ('id="admin-user"', 'id="logout"', "fetch('api/me')",
                       "location.href = 'logout'"):
            self.assertIn(marker, html)

    def test_index_has_export_button_and_menu(self):
        with self.open(self.url('/')) as response:
            html = response.read().decode('utf-8')
        for marker in ('导出文字记录', 'export-menu', 'data-format="json"',
                       'data-format="txt"', '/transcript', '未识别'):
            self.assertIn(marker, html)
