import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, parse, request

from auth.api import build_server
from auth.config import FeishuAppCredentials
from auth.errors import AuthError
from auth.service import AuthService

APP = FeishuAppCredentials('cli_test', 'sec_test')


class ManualClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeTransport:
    def __init__(self, open_id='ou_A'):
        self.open_id = open_id

    def exchange(self, code, redirect_uri, verifier):
        return {'access_token': 'u-first', 'refresh_token': 'r-first', 'token_type': 'Bearer',
                'expires_in': 7200, 'refresh_token_expires_in': 2592000,
                'scope': 'calendar:calendar:read calendar:calendar.event:read offline_access'}

    def refresh(self, refresh_token, *, timeout=None):
        raise AssertionError('not used in api tests')

    def subject(self, token):
        return self.open_id


OPENER = request.build_opener(request.ProxyHandler({}))  # bypass macOS system proxy


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.transport = FakeTransport()
        self.clock = ManualClock()
        self.service = AuthService(APP, Path(self.tmp.name), base_url='https://auth.internal',
                                   transport=self.transport, clock=self.clock)
        self.server = build_server(self.service, host='127.0.0.1', port=0)
        self.port = self.server.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.tmp.cleanup)

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def get(self, path):
        req = request.Request(self.url(path), method='GET')
        with OPENER.open(req, timeout=5) as response:
            return response.status, response.read().decode('utf-8'), dict(response.headers)

    def post(self, path, body):
        data = json.dumps(body).encode()
        req = request.Request(self.url(path), data=data, method='POST',
                              headers={'Content-Type': 'application/json'})
        with OPENER.open(req, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_healthz(self):
        status, body, _ = self.get('/healthz')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['status'], 'ok')

    def test_full_flow_over_http(self):
        status, start = self.post('/authorize/start', {'open_id': 'ou_A', 'intent': 'join:9'})
        self.assertEqual(status, 200)
        state = parse.parse_qs(parse.urlsplit(start['authorize_url']).query)['state'][0]
        status, page, headers = self.get('/oauth/callback?' + parse.urlencode({'code': 'c-1', 'state': state}))
        self.assertEqual(status, 200)
        self.assertIn('授权成功', page)
        self.assertIn('join:9', page)
        self.assertEqual(headers.get('Cache-Control'), 'no-store')

        status, token, _ = self.get('/credentials/ou_A/token')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(token)['access_token'], 'u-first')

    def test_authorize_start_rejects_bad_bodies(self):
        req = request.Request(self.url('/authorize/start'), data=b'not json', method='POST')
        try:
            OPENER.open(req, timeout=5)
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertEqual(json.loads(exc.read())['error']['code'], 'bad_message')

    def test_token_mapping(self):
        try:
            self.get('/credentials/ou_A/token')
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 404)  # not_authorized
        # denied flow → 403 page
        status, start = self.post('/authorize/start', {'open_id': 'ou_A', 'intent': ''})
        state = parse.parse_qs(parse.urlsplit(start['authorize_url']).query)['state'][0]
        try:
            self.get('/oauth/callback?' + parse.urlencode({'error': 'access_denied', 'state': state}))
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 403)
            self.assertIn('已取消', exc.read().decode('utf-8'))

    def test_reauthorization_required_maps_to_409(self):
        self.service.store.write('ou_A', self.service.store.marker('ou_A', 'refreshing', None))
        try:
            self.get('/credentials/ou_A/token')
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 409)
            self.assertEqual(json.loads(exc.read())['error']['code'], 'reauthorization_required')

    def test_unknown_path_is_404(self):
        try:
            self.get('/nope')
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

    def test_callback_replay_over_http_is_invalid(self):
        _, start = self.post('/authorize/start', {'open_id': 'ou_A', 'intent': ''})
        state = parse.parse_qs(parse.urlsplit(start['authorize_url']).query)['state'][0]
        query = parse.urlencode({'code': 'c-1', 'state': state})
        _, page, _ = self.get('/oauth/callback?' + query)
        self.assertIn('授权成功', page)
        try:
            self.get('/oauth/callback?' + query)
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertIn('无效', exc.read().decode('utf-8'))


class ConfigTests(unittest.TestCase):
    def test_env_wins_over_file(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / 'config.json').write_text(
                json.dumps({'app_id': 'file_id', 'app_secret': 'file_secret'}))
            os.environ['AUTH_FEISHU_APP_ID'] = 'env_id'
            os.environ['AUTH_FEISHU_APP_SECRET'] = 'env_secret'
            try:
                from auth.config import load_credentials
                credentials = load_credentials(Path(tmp))
                self.assertEqual((credentials.app_id, credentials.app_secret), ('env_id', 'env_secret'))
            finally:
                del os.environ['AUTH_FEISHU_APP_ID']
                del os.environ['AUTH_FEISHU_APP_SECRET']

    def test_missing_credentials_raise(self):
        with TemporaryDirectory() as tmp:
            from auth.config import load_credentials
            with self.assertRaises(AuthError):
                load_credentials(Path(tmp))


if __name__ == '__main__':
    unittest.main()
