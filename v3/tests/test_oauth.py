import tempfile
import unittest
from pathlib import Path

from feishu_spectator.client import Credentials
from feishu_spectator.oauth import (OAuthError, OAuthManager, OAuthTransport,
                                    authorization_parameters, callback_code)

CREDENTIALS = Credentials(app_id='cli_test', app_secret='secret_test')


def token_result(**overrides):
    result = {'access_token': 'at-1', 'refresh_token': 'rt-1', 'token_type': 'Bearer',
              'expires_in': 7200, 'refresh_token_expires_in': 30 * 86400,
              'scope': 'vc:meeting.meetingevent:read offline_access'}
    result.update(overrides)
    return result


class FakeTransport(OAuthTransport):
    def __init__(self, exchange_results=(), refresh_results=()):
        super().__init__(CREDENTIALS)
        self.exchange_results = list(exchange_results)
        self.refresh_results = list(refresh_results)
        self.exchange_calls = []
        self.refresh_calls = []

    def exchange(self, code, redirect_uri, verifier):
        self.exchange_calls.append(code)
        step = self.exchange_results.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def refresh(self, refresh_token, *, timeout=None):
        self.refresh_calls.append(refresh_token)
        step = self.refresh_results.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class OAuthManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self, transport):
        return OAuthManager(CREDENTIALS, self.directory, transport=transport, clock=self.clock)

    def test_authorize_persists_and_reports_scope(self):
        manager = self.manager(FakeTransport(exchange_results=[token_result()]))
        result = manager.authorize('code-1', 'http://127.0.0.1:8766/oauth/callback', 'v')
        self.assertTrue(result['authorized'])
        self.assertIn('vc:meeting.meetingevent:read', result['scopes'])
        status = manager.status()
        self.assertTrue(status['authorized'])
        self.assertEqual(status['state'], 'ready')

    def test_missing_scope_rejected(self):
        transport = FakeTransport(exchange_results=[token_result(
            scope='offline_access')])
        manager = self.manager(transport)
        with self.assertRaises(OAuthError) as ctx:
            manager.authorize('code-1', 'http://127.0.0.1:8766/oauth/callback', 'v')
        self.assertEqual(ctx.exception.reason, 'oauth_missing_scopes')

    def test_cached_token_returned_without_network(self):
        manager = self.manager(FakeTransport(exchange_results=[token_result()]))
        manager.authorize('code-1', 'http://127.0.0.1:8766/oauth/callback', 'v')
        transport = manager.transport
        self.assertEqual(manager.access_token(), 'at-1')
        self.assertEqual(transport.refresh_calls, [])

    def test_expiry_margin_forces_rotation(self):
        manager = self.manager(FakeTransport(
            exchange_results=[token_result()],
            refresh_results=[token_result(access_token='at-2', refresh_token='rt-2')]))
        manager.authorize('code-1', 'http://127.0.0.1:8766/oauth/callback', 'v')
        self.clock.now += 7200  # past expires_at - margin
        self.assertEqual(manager.access_token(), 'at-2')
        self.assertEqual(manager.transport.refresh_calls, ['rt-1'])
        self.assertEqual(manager.status()['state'], 'ready')

    def test_rejected_token_forces_rotation(self):
        manager = self.manager(FakeTransport(
            exchange_results=[token_result()],
            refresh_results=[token_result(access_token='at-2', refresh_token='rt-2')]))
        manager.authorize('code-1', 'http://127.0.0.1:8766/oauth/callback', 'v')
        self.assertEqual(manager.access_token(rejected_token='at-1'), 'at-2')

    def test_refresh_failure_requires_reauthorization(self):
        manager = self.manager(FakeTransport(
            exchange_results=[token_result()],
            refresh_results=[OAuthError('oauth_token_rejected', stage='refresh')]))
        manager.authorize('code-1', 'http://127.0.0.1:8766/oauth/callback', 'v')
        self.clock.now += 7200
        with self.assertRaises(OAuthError) as ctx:
            manager.access_token()
        self.assertEqual(ctx.exception.reason, 'oauth_token_rejected')
        self.assertEqual(manager.status()['state'], 'reauthorization_required')
        with self.assertRaises(OAuthError) as again:
            manager.access_token()
        self.assertEqual(again.exception.reason, 'oauth_reauthorization_required')

    def test_reused_refresh_token_rejected(self):
        manager = self.manager(FakeTransport(
            exchange_results=[token_result()],
            refresh_results=[token_result()]))  # same rt-1 returned
        manager.authorize('code-1', 'http://127.0.0.1:8766/oauth/callback', 'v')
        self.clock.now += 7200
        with self.assertRaises(OAuthError):
            manager.access_token()
        self.assertEqual(manager.status()['state'], 'reauthorization_required')

    def test_not_authorized_before_login(self):
        manager = self.manager(FakeTransport())
        self.assertEqual(manager.status(), {'authorized': False, 'state': 'not_authorized'})
        with self.assertRaises(OAuthError):
            manager.access_token()

    def test_logout_forgets_credentials(self):
        manager = self.manager(FakeTransport(exchange_results=[token_result()]))
        manager.authorize('code-1', 'http://127.0.0.1:8766/oauth/callback', 'v')
        manager.logout()
        self.assertEqual(manager.status()['state'], 'not_authorized')

    def test_provider_wires_into_client(self):
        manager = self.manager(FakeTransport(exchange_results=[token_result()]))
        manager.authorize('code-1', 'http://127.0.0.1:8766/oauth/callback', 'v')
        self.assertEqual(manager.provider()(), 'at-1')


class CallbackTests(unittest.TestCase):
    def test_parameters_and_url_shape(self):
        parameters = authorization_parameters('cli_test', 8766)
        self.assertIn('code_challenge_method=S256', parameters['url'])
        self.assertEqual(parameters['redirect_uri'], 'http://127.0.0.1:8766/oauth/callback')

    def test_callback_code_roundtrip(self):
        parameters = authorization_parameters('cli_test', 8766)
        from urllib import parse
        query = parse.urlencode({'state': parameters['state'], 'code': 'abc123'})
        self.assertEqual(callback_code(f'/oauth/callback?{query}', parameters['state']), 'abc123')

    def test_state_mismatch_rejected(self):
        with self.assertRaises(OAuthError):
            callback_code('/oauth/callback?state=other&code=abc', 'expected')

    def test_denied(self):
        from urllib import parse
        query = parse.urlencode({'state': 'expected', 'error': 'access_denied'})
        with self.assertRaises(OAuthError) as ctx:
            callback_code(f'/oauth/callback?{query}', 'expected')
        self.assertEqual(ctx.exception.reason, 'oauth_denied')


if __name__ == '__main__':
    unittest.main()
