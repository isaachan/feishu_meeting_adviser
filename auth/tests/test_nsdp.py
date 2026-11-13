"""NSDP token submission flow (CONTRACT_AUTH §8): one-shot link, form POST,
per-open_id storage, invalidation. Tokens must never appear in pages."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, parse, request

from auth.api import build_server
from auth.config import FeishuAppCredentials
from auth.service import AuthService

APP = FeishuAppCredentials('cli_test', 'sec_test')
OPENER = request.build_opener(request.ProxyHandler({}))  # bypass macOS system proxy


class NsdpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.service = AuthService(APP, Path(self.tmp.name), base_url='https://auth.internal')
        self.server = build_server(self.service, host='127.0.0.1', port=0)
        self.port = self.server.server_address[1]
        import threading
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.tmp.cleanup)

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def post_json(self, path, body):
        req = request.Request(self.url(path), data=json.dumps(body).encode(), method='POST',
                              headers={'Content-Type': 'application/json'})
        with OPENER.open(req, timeout=5) as response:
            return response.status, json.loads(response.read())

    def post_form(self, path, fields):
        data = parse.urlencode(fields).encode()
        req = request.Request(self.url(path), data=data, method='POST',
                              headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with OPENER.open(req, timeout=5) as response:
            return response.status, response.read().decode('utf-8')

    def get(self, path):
        with OPENER.open(request.Request(self.url(path)), timeout=5) as response:
            return response.status, response.read().decode('utf-8')

    def start_submission(self, open_id='ou_A'):
        status, payload = self.post_json('/token-submissions/start',
                                         {'open_id': open_id, 'provider': 'nsdp'})
        self.assertEqual(status, 200)
        self.assertTrue(payload['submit_url'].startswith('https://auth.internal/nsdp/submit?state='))
        return payload

    def test_full_submission_flow(self):
        submission = self.start_submission()
        state = parse.parse_qs(parse.urlsplit(submission['submit_url']).query)['state'][0]

        status, page = self.get(f'/nsdp/submit?state={state}')
        self.assertEqual(status, 200)
        self.assertIn('NSDP token', page)
        self.assertNotIn('nsdp-secret', page)

        status, page = self.post_form(f'/nsdp/submit?state={state}', {'token': 'nsdp-secret-1'})
        self.assertEqual(status, 200)
        self.assertIn('已保存', page)
        self.assertNotIn('nsdp-secret-1', page)  # never echo the token

        status, payload = self.get('/credentials/ou_A/token?provider=nsdp')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)['token'], 'nsdp-secret-1')

    def test_state_is_one_shot_and_replay_shows_invalid(self):
        submission = self.start_submission()
        path = submission['submit_url'][len('https://auth.internal'):]
        status, _ = self.post_form(path, {'token': 'nsdp-secret-1'})
        self.assertEqual(status, 200)
        with self.assertRaises(error.HTTPError) as caught:
            self.post_form(path, {'token': 'nsdp-secret-2'})
        self.assertEqual(caught.exception.code, 400)
        # replay did not overwrite the stored token
        status, payload = self.get('/credentials/ou_A/token?provider=nsdp')
        self.assertEqual(json.loads(payload)['token'], 'nsdp-secret-1')

    def test_unknown_state_shows_invalid_page(self):
        with self.assertRaises(error.HTTPError) as caught:
            self.get('/nsdp/submit?state=nope')
        self.assertEqual(caught.exception.code, 400)
        self.assertIn('无效', caught.exception.read().decode('utf-8'))

    def test_invalid_token_format_rejected(self):
        for bad in ('', '   ', 'sh', 'x' * 513, 'has space'):
            submission = self.start_submission()
            path = submission['submit_url'][len('https://auth.internal'):]
            with self.assertRaises(error.HTTPError) as caught:
                self.post_form(path, {'token': bad})
            self.assertEqual(caught.exception.code, 400)
            with self.assertRaises(error.HTTPError) as caught:
                self.get('/credentials/ou_A/token?provider=nsdp')
            self.assertEqual(caught.exception.code, 404)  # nothing saved

    def test_read_without_submission_is_not_authorized(self):
        with self.assertRaises(error.HTTPError) as caught:
            self.get('/credentials/ou_A/token?provider=nsdp')
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(json.loads(caught.exception.read())['error']['code'], 'not_authorized')

    def test_invalidate_deletes_and_is_idempotent(self):
        self.start_submission()
        self.post_json('/token-submissions/start', {'open_id': 'ou_A', 'provider': 'nsdp'})
        # direct store write for setup brevity
        self.service.nsdp_store.write('ou_A', 'nsdp-secret-1', now=1.0)
        status, payload = self.post_json('/credentials/ou_A/invalidate', {'provider': 'nsdp'})
        self.assertEqual((status, payload['ok']), (200, True))
        with self.assertRaises(error.HTTPError) as caught:
            self.get('/credentials/ou_A/token?provider=nsdp')
        self.assertEqual(caught.exception.code, 404)
        status, _ = self.post_json('/credentials/ou_A/invalidate', {'provider': 'nsdp'})
        self.assertEqual(status, 200)

    def test_provider_validation(self):
        with self.assertRaises(error.HTTPError) as caught:
            self.post_json('/token-submissions/start', {'open_id': 'ou_A', 'provider': 'jira'})
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(error.HTTPError) as caught:
            self.get('/credentials/ou_A/token?provider=jira')
        self.assertEqual(caught.exception.code, 400)

    def test_feishu_read_still_works_without_provider_param(self):
        with self.assertRaises(error.HTTPError) as caught:
            self.get('/credentials/ou_A/token')
        self.assertEqual(caught.exception.code, 404)  # feishu path unchanged

    def test_state_ttl_expiry(self):
        submission = self.start_submission()
        self.service.states.clock = lambda: submission['expires_at'] + 1  # past TTL
        with self.assertRaises(error.HTTPError) as caught:
            self.get(f'/nsdp/submit?state={submission["state"]}')
        self.assertEqual(caught.exception.code, 400)


if __name__ == '__main__':
    unittest.main()
