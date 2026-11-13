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
from admin_site.server import build_server

OPENER = request.build_opener(request.ProxyHandler({}))


class StubAdvisorHandler(BaseHTTPRequestHandler):
    calls = []  # shared per test via handler_class attribute

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == '/admin/sessions':
            body = {'sessions': [{'session_id': 's-1', 'owner_open_id': 'ou_A',
                                  'meeting_no': '123', 'title': '周会',
                                  'phase': 'recording', 'started_at': 1762800000.0,
                                  'ended_at': None, 'nsdp_hold': False}]}
            self._reply(body)
        else:
            self._reply({'error': {'code': 'not_found', 'message': 'nope'}}, 404)

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length:
            self.rfile.read(length)
        type(self).calls.append(('POST', self.path))
        if self.path == '/admin/sessions/s-1/end':
            self._reply({'session_id': 's-1', 'phase': 'draining'})
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

        self.server = build_server(AdvisorAdminClient(f'http://127.0.0.1:{self.stub_port}'),
                                   self.logs, host='127.0.0.1', port=0,
                                   bootstrap_bytes=1024, poll_s=0.05)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def get(self, path):
        with OPENER.open(request.Request(self.url(path), method='GET'), timeout=5) as response:
            return response.status, json.loads(response.read()), response.headers

    def post(self, path, body=b''):
        req = request.Request(self.url(path), data=body, method='POST')
        with OPENER.open(req, timeout=5) as response:
            return response.status, json.loads(response.read())

    def post_error(self, path, body=b''):
        req = request.Request(self.url(path), data=body, method='POST')
        try:
            with OPENER.open(req, timeout=5) as response:
                self.fail('expected HTTPError')
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class BasicTests(AdminSiteBase):
    def test_healthz(self):
        status, body, _ = self.get('/healthz')
        self.assertEqual((status, body['status']), (200, 'ok'))

    def test_index_served_as_html(self):
        with OPENER.open(self.url('/'), timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('text/html', response.headers['Content-Type'])
            self.assertIn('运维台', response.read().decode('utf-8'))

    def test_services_lists_five_with_existence(self):
        status, body, _ = self.get('/api/services')
        keys = [s['key'] for s in body['services']]
        self.assertEqual(keys, ['advisor', 'bot', 'stt-server', 'gateway', 'auth'])
        by_key = {s['key']: s for s in body['services']}
        self.assertTrue(by_key['advisor']['exists'])
        self.assertEqual(by_key['advisor']['size'], len('line-1\nline-2\n'.encode()))
        self.assertFalse(by_key['bot']['exists'])


class SessionProxyTests(AdminSiteBase):
    def test_sessions_passthrough(self):
        status, body, _ = self.get('/api/sessions')
        self.assertEqual(status, 200)
        self.assertEqual(body['sessions'][0]['session_id'], 's-1')

    def test_end_posts_to_admin_face_without_body(self):
        status, body = self.post('/api/sessions/s-1/end')
        self.assertEqual((status, body['phase']), (200, 'draining'))
        self.assertIn(('POST', '/admin/sessions/s-1/end'), self.stub_calls)

    def test_advisor_error_status_passthrough(self):
        status, body = self.post_error('/api/sessions/s-unknown/end')
        self.assertEqual(status, 404)
        self.assertEqual(body['error']['code'], 'not_found')

    def test_advisor_unreachable_maps_to_502(self):
        server = build_server(AdvisorAdminClient('http://127.0.0.1:1'),
                              self.logs, host='127.0.0.1', port=0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        try:
            with OPENER.open(f'http://127.0.0.1:{port}/api/sessions', timeout=5) as response:
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
        conn.request('GET', '/api/logs/advisor/stream')
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
        conn.request('GET', '/api/logs/bot/stream')
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        try:
            events = self.read_events(conn, response, 1)
            self.assertTrue(any('不存在' in text for text in events))
        finally:
            conn.close()
