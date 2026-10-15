import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import unittest
from urllib import error, parse, request

from feishu_meeting_adviser.client import ApiError, Credentials, ErrorKind
from feishu_meeting_adviser.context_probe import UserContextClient
from feishu_meeting_adviser.oauth import (
    AUTHORIZE_URL, CODE_TOKEN_URL, TOKEN_URL, SCOPES, OAuthError, OAuthManager, OAuthTransport,
    _NoRedirect, authorization_parameters, callback_code, login,
)


def token_result(access='private-access', refresh='private-refresh', scopes=SCOPES, expiry=7200):
    return {'code': 0, 'access_token': access, 'refresh_token': refresh, 'expires_in': expiry,
            'refresh_token_expires_in': 604800, 'scope': ' '.join(scopes), 'token_type': 'Bearer'}


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.result = token_result()
        self.refresh_result = token_result('next-private-access', 'next-private-refresh')

    def exchange(self, code, redirect_uri, verifier):
        self.calls.append(('exchange', code, redirect_uri, verifier))
        return self.result

    def subject(self, token):
        self.calls.append(('subject', token))
        return 'synthetic-subject-hash'

    def refresh(self, token):
        self.calls.append(('refresh', token))
        if isinstance(self.refresh_result, BaseException):
            raise self.refresh_result
        return self.refresh_result


class Response(BytesIO):
    def __init__(self, body, status=200):
        super().__init__(json.dumps(body).encode())
        self.status, self.headers = status, {}


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'data'
        self.credentials = Credentials('synthetic-app', 'private-secret')
        self.transport = FakeTransport()
        self.now = [1000.0]
        self.manager = OAuthManager(self.credentials, self.directory, transport=self.transport, clock=lambda: self.now[0])

    def authorize(self):
        with self.manager.locked():
            self.manager.authorize('private-code', 'http://127.0.0.1:8765/oauth/callback', 'private-verifier')

    def test_pkce_scope_and_callback_state_are_bound_to_one_login(self):
        params = authorization_parameters('synthetic-app', 8765)
        query = parse.parse_qs(parse.urlsplit(params['url']).query)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(params['verifier'].encode()).digest()).decode().rstrip('=')
        self.assertTrue(params['url'].startswith(AUTHORIZE_URL + '?'))
        self.assertEqual(query['code_challenge'], [challenge])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['prompt'], ['consent'])
        self.assertEqual(set(query['scope'][0].split()), set(SCOPES))
        self.assertNotIn('private-secret', params['url'])
        callback = '/oauth/callback?' + parse.urlencode({'code': 'valid_code', 'state': params['state']})
        self.assertEqual(callback_code(callback, params['state']), 'valid_code')
        for path in ('/oauth/callback?code=secret&state=wrong', callback + '&state=duplicate',
                     callback + '&code=other', '/another?' + parse.urlsplit(callback).query,
                     'https://evil.example' + callback):
            with self.assertRaises(OAuthError) as caught:
                callback_code(path, params['state'])
            self.assertNotIn('secret', str(caught.exception))
        with self.assertRaises(OAuthError) as caught:
            callback_code('/oauth/callback?error=access_denied&state=' + params['state'], params['state'])
        self.assertEqual(caught.exception.reason, 'oauth_denied')

    def test_private_atomic_install_status_and_local_logout_do_not_expose_tokens(self):
        self.assertFalse(self.manager.status()['authorized'])
        self.authorize()
        self.assertEqual(self.manager.access_token(), 'private-access')
        self.assertEqual(self.manager.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.directory.stat().st_mode & 0o777, 0o700)
        value = json.loads(self.manager.path.read_text())
        self.assertEqual(value['subject_hash'], 'synthetic-subject-hash')
        self.assertNotIn('private-code', self.manager.path.read_text())
        self.assertNotIn('private-verifier', self.manager.path.read_text())
        safe = json.dumps(self.manager.status())
        for secret in ('private-access', 'private-refresh', 'private-secret'):
            self.assertNotIn(secret, safe)
        self.manager.logout()
        self.assertFalse(self.manager.status()['authorized'])

    def test_refresh_rotates_and_commit_marker_precedes_one_use_post(self):
        self.authorize()
        self.now[0] += 7000
        original = self.transport.refresh
        def checked_refresh(token):
            marker = json.loads(self.manager.path.read_text())
            self.assertEqual(marker['state'], 'refreshing')
            self.assertNotIn('refresh_token', marker)
            return original(token)
        self.transport.refresh = checked_refresh
        self.assertEqual(self.manager.access_token(), 'next-private-access')
        value = json.loads(self.manager.path.read_text())
        self.assertEqual(value['refresh_token'], 'next-private-refresh')
        self.assertEqual(value['state'], 'ready')
        self.assertEqual(self.manager.access_token(), 'next-private-access')
        self.assertEqual(sum(call[0] == 'refresh' for call in self.transport.calls), 1)

    def test_timeout_or_process_interruption_forces_reauthorization_without_replay(self):
        for failure in (OAuthError('oauth_request_failed'), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                self.authorize()
                self.transport.refresh_result = failure
                self.now[0] += 7000
                with self.assertRaises(type(failure)):
                    self.manager.access_token()
                count = len(self.transport.calls)
                restarted = OAuthManager(self.credentials, self.directory, transport=self.transport, clock=lambda: self.now[0])
                with self.assertRaises(OAuthError) as caught:
                    restarted.access_token()
                self.assertEqual(caught.exception.reason, 'oauth_reauthorization_required')
                self.assertEqual(len(self.transport.calls), count)
                self.assertNotIn('private-refresh', restarted.path.read_text())

    def test_scope_pruning_or_inflation_does_not_install_credentials(self):
        for scopes, reason in ((SCOPES[:-1], 'oauth_missing_scopes'),
                               (SCOPES + ('calendar:calendar',), 'oauth_excess_scopes')):
            self.transport.result = token_result(scopes=scopes)
            with self.manager.locked(), self.assertRaises(OAuthError) as caught:
                self.manager.authorize('code', 'redirect', 'verifier')
            self.assertEqual(caught.exception.reason, reason)
            self.assertFalse(self.manager.path.exists())

    def test_parallel_refresh_lease_and_stale_rejection_reuse_the_rotated_token(self):
        self.authorize()
        with self.manager.locked(), self.assertRaises(OAuthError) as caught:
            self.manager.access_token()
        self.assertEqual(caught.exception.reason, 'oauth_busy')
        self.assertEqual(self.manager.access_token(rejected_token='private-access'), 'next-private-access')
        calls = len(self.transport.calls)
        self.assertEqual(self.manager.access_token(rejected_token='private-access'), 'next-private-access')
        self.assertEqual(len(self.transport.calls), calls)

    def test_corrupt_cross_app_and_insecure_files_are_rejected(self):
        self.authorize()
        self.manager.path.chmod(0o644)
        with self.assertRaises(OAuthError):
            self.manager.access_token()
        self.manager.path.chmod(0o600)
        value = json.loads(self.manager.path.read_text())
        value['app_key'] = 'other-app'
        self.manager.path.write_text(json.dumps(value))
        with self.assertRaises(OAuthError):
            self.manager.access_token()
        self.manager.path.unlink()
        self.manager.path.symlink_to(self.directory / 'elsewhere')
        with self.assertRaises(OAuthError):
            self.manager.status()

    def test_transport_uses_documented_pkce_exchange_and_v3_refresh_without_replay(self):
        requests = []
        def opener(req, timeout):
            requests.append(req)
            return Response(token_result())
        transport = OAuthTransport(self.credentials, opener=opener)
        transport.exchange('private-code', 'http://127.0.0.1:8765/oauth/callback', 'private-verifier')
        req = requests[0]
        self.assertEqual(req.full_url, CODE_TOKEN_URL)
        self.assertEqual(req.method, 'POST')
        self.assertEqual(req.get_header('Content-type'), 'application/json; charset=utf-8')
        body = json.loads(req.data)
        self.assertEqual(body['code_verifier'], 'private-verifier')
        self.assertEqual(body['scope'], ' '.join(SCOPES))
        self.assertEqual(body['grant_type'], 'authorization_code')
        self.assertNotIn('private-code', req.full_url)
        transport.refresh('private-refresh')
        self.assertEqual(requests[-1].full_url, TOKEN_URL)
        self.assertEqual(requests[-1].get_header('Content-type'), 'application/x-www-form-urlencoded')
        self.assertEqual(parse.parse_qs(requests[-1].data.decode())['grant_type'], ['refresh_token'])
        def denied(req, timeout):
            requests.append(req)
            return Response({'code': 20064, 'error_description': 'private-refresh private-secret'}, status=400)
        transport.opener = denied
        with self.assertRaises(OAuthError) as caught:
            transport.refresh('private-refresh')
        self.assertEqual(caught.exception.code, 20064)
        self.assertNotIn('private-refresh', str(caught.exception))
        self.assertEqual(caught.exception.stage, 'refresh')
        self.assertEqual(len(requests), 3)

    def test_pkce_rejection_is_reported_at_exchange_without_retrying_another_endpoint(self):
        requests = []
        def opener(req, timeout):
            requests.append(req)
            return Response({'code': 20049, 'error_description': 'private-code private-verifier'}, status=400)
        transport = OAuthTransport(self.credentials, opener=opener)
        with self.assertRaises(OAuthError) as caught:
            transport.exchange('private-code', 'http://127.0.0.1:8765/oauth/callback', 'private-verifier')
        self.assertEqual(caught.exception.stage, 'code_exchange')
        self.assertEqual(caught.exception.code, 20049)
        self.assertEqual([req.full_url for req in requests], [CODE_TOKEN_URL])
        self.assertNotIn('private-code', str(caught.exception))

    def test_real_loopback_callback_rejects_wrong_state_then_installs_once(self):
        with socket.socket() as allocator:
            allocator.bind(('127.0.0.1', 0))
            port = allocator.getsockname()[1]
        messages, outcomes = queue.Queue(), queue.Queue()
        def run():
            try:
                outcomes.put(login(self.manager, port=port, timeout=5, notify=messages.put))
            except BaseException as exc:
                outcomes.put(exc)
        worker = threading.Thread(target=run)
        worker.start()
        self.addCleanup(worker.join, 6)
        messages.get(timeout=2)
        messages.get(timeout=2)
        # Loopback tests must not route local callback/code traffic through inherited proxies.
        opener = request.build_opener(request.ProxyHandler({}), _NoRedirect()).open
        with self.assertRaises(error.HTTPError) as redirect:
            opener(f'http://127.0.0.1:{port}/', timeout=2)
        self.assertEqual(redirect.exception.code, 302)
        location = redirect.exception.headers['Location']
        redirect.exception.close()
        query = parse.parse_qs(parse.urlsplit(location).query)
        state = query['state'][0]
        original_exchange = self.transport.exchange
        def verify_exchange(code, redirect_uri, verifier):
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            self.assertEqual(challenge, query['code_challenge'][0])
            self.assertEqual(query['code_challenge_method'], ['S256'])
            self.assertEqual(redirect_uri, query['redirect_uri'][0])
            return original_exchange(code, redirect_uri, verifier)
        self.transport.exchange = verify_exchange
        with self.assertRaises(error.HTTPError) as bad:
            opener(f'http://127.0.0.1:{port}/oauth/callback?code=private-code&state=wrong', timeout=2)
        self.assertEqual(bad.exception.code, 400)
        bad.exception.close()
        url = f'http://127.0.0.1:{port}/oauth/callback?' + parse.urlencode({'code': 'private-code', 'state': state})
        with opener(url, timeout=2) as response:
            body = response.read().decode()
            self.assertIn('saved locally', body)
            self.assertNotIn('private-code', body)
            self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        outcome = outcomes.get(timeout=2)
        self.assertIsInstance(outcome, dict, str(outcome))
        self.assertTrue(outcome['authorized'])
        self.assertEqual(sum(call[0] == 'exchange' for call in self.transport.calls), 1)
        self.assertEqual(self.manager.access_token(), 'private-access')

    def test_user_context_client_refreshes_once_and_never_uses_tenant_or_vc_endpoints(self):
        self.authorize()
        requests = []
        responses = iter([Response({'code': 99991677}),
            Response({'code': 0, 'data': {'content': '合成资料'}})])
        def opener(req, timeout):
            requests.append(req)
            return next(responses)
        client = UserContextClient(self.credentials, oauth_manager=self.manager,
            opener=opener, wait=lambda seconds: False)
        with self.assertRaises(ValueError):
            client.join('012345678')
        self.assertEqual(requests, [])
        self.assertEqual(client.document_text('DocToken'), '合成资料')
        self.assertEqual(requests[0].get_header('Authorization'), 'Bearer private-access')
        self.assertEqual(requests[1].get_header('Authorization'), 'Bearer next-private-access')
        self.assertTrue(all('/auth/v3/' not in req.full_url for req in requests))
        self.assertEqual(len(requests), 2)

    def test_missing_user_scope_does_not_refresh_or_fall_back_to_application_identity(self):
        self.authorize()
        calls = len(self.transport.calls)
        client = UserContextClient(self.credentials, oauth_manager=self.manager,
            opener=lambda req, timeout: Response({'code': 99991672}), wait=lambda seconds: False)
        with self.assertRaises(ApiError) as caught:
            client.document_text('DocToken')
        self.assertEqual(caught.exception.kind, ErrorKind.DENIED)
        self.assertEqual(len(self.transport.calls), calls)


if __name__ == '__main__':
    unittest.main()
