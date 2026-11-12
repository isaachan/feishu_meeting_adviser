"""GuiApiClient against an in-process fake /gui/* server (contract §8)."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, parse, request

from macos_gui.api import ApiError, GuiApiClient, NetworkError

OPENER = request.build_opener(request.ProxyHandler({}))


class FakeGuiServer:
    """Minimal CONTRACT_ADVISOR_API §8 implementation with canned replies."""

    def __init__(self):
        self.device_codes = {}
        self.bearer = None  # expected Authorization value for protected routes
        self.session_payload = {'user': {'open_id': 'ou_A'}, 'sessions': []}
        self.requests = []
        handler = type('Handler', (_Handler,), {'server_impl': self})
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()


class _Handler(BaseHTTPRequestHandler):
    server_impl: FakeGuiServer
    status = 200
    payload = {}

    def log_message(self, *args):
        pass

    def _reply(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        impl = self.server_impl
        parsed = parse.urlsplit(self.path)
        query = parse.parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == '/gui/login/poll':
            code = query.get('device_code', [None])[0]
            impl.requests.append(('poll', code))
            if code == 'dc-pending':
                self._reply(200, {'status': 'pending'})
            elif code == 'dc-done':
                self._reply(200, {'status': 'completed', 'device_token': 'dt-1',
                                  'user': {'open_id': 'ou_A', 'name': 'Kai Han',
                                           'email': 'kai.han@nio.com'}})
            else:
                self._reply(404, {'error': {'code': 'unknown_device',
                                            'message': 'nope'}})
            return
        if parsed.path == '/gui/sessions':
            impl.requests.append(('sessions', self.headers.get('Authorization')))
            if impl.bearer is not None and \
                    self.headers.get('Authorization') != 'Bearer ' + impl.bearer:
                self._reply(401, {'error': {'code': 'bad_token', 'message': 'x'}})
                return
            self._reply(200, impl.session_payload)
            return
        self._reply(404, {'error': {'code': 'not_found', 'message': 'unknown path'}})

    def do_POST(self):
        impl = self.server_impl
        parsed = parse.urlsplit(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length) or b'{}')
        if parsed.path == '/gui/login/start':
            impl.requests.append(('start', body))
            self._reply(200, {'login_url': 'https://accounts.feishu.cn/x',
                              'device_code': 'dc-pending', 'expires_at': 1.0})
            return
        if parsed.path == '/gui/logout':
            impl.requests.append(('logout', self.headers.get('Authorization')))
            self._reply(200, {'ok': True})
            return
        self._reply(404, {'error': {'code': 'not_found', 'message': 'unknown path'}})


class ApiClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeGuiServer()
        self.addCleanup(self.server.close)
        self.client = GuiApiClient(f'http://127.0.0.1:{self.server.port}/',
                                   opener=OPENER.open)

    def test_base_url_validation(self):
        for bad in ('', 'x://y', 'just-host', None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                GuiApiClient(bad)

    def test_login_start_and_poll(self):
        start = self.client.login_start()
        self.assertEqual(start['device_code'], 'dc-pending')
        self.assertTrue(start['login_url'].startswith('https://'))
        self.assertEqual(self.client.login_poll('dc-pending'), {'status': 'pending'})
        done = self.client.login_poll('dc-done')
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['user']['open_id'], 'ou_A')

    def test_unknown_device_maps_to_api_error(self):
        with self.assertRaises(ApiError) as ctx:
            self.client.login_poll('dc-nope')
        self.assertEqual((ctx.exception.status, ctx.exception.code),
                         (404, 'unknown_device'))

    def test_sessions_sends_bearer_token(self):
        self.server.bearer = 'dt-good'
        payload = self.client.sessions('dt-good')
        self.assertEqual(payload['user']['open_id'], 'ou_A')
        self.assertEqual(self.server.requests[-1], ('sessions', 'Bearer dt-good'))

    def test_bad_token_maps_to_401_api_error(self):
        self.server.bearer = 'dt-good'
        with self.assertRaises(ApiError) as ctx:
            self.client.sessions('dt-forged')
        self.assertEqual((ctx.exception.status, ctx.exception.code),
                         (401, 'bad_token'))

    def test_logout(self):
        result = self.client.logout('dt-x')
        self.assertEqual(result, {'ok': True})
        self.assertEqual(self.server.requests[-1], ('logout', 'Bearer dt-x'))

    def test_network_error_on_dead_port(self):
        dead = GuiApiClient('http://127.0.0.1:1', opener=OPENER.open, timeout=0.5)
        with self.assertRaises(NetworkError):
            dead.login_start()


if __name__ == '__main__':
    unittest.main()
