import json
from pathlib import Path
import tempfile
import time
import unittest

from local_meeting_recorder.oauth import (
    SCOPES, OAuthError, OAuthManager, authorization_parameters, callback_code,
    load_credentials, stt_auth_provider,
)


def token_result(access='private-access', refresh='private-refresh',
                 scopes=SCOPES, expiry=7200):
    return {'code': 0, 'access_token': access, 'refresh_token': refresh,
            'expires_in': expiry, 'refresh_token_expires_in': 604800,
            'scope': ' '.join(scopes), 'token_type': 'Bearer'}


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.result = token_result()
        self.refresh_result: object = token_result('next-private-access',
                                                   'next-private-refresh')

    def exchange(self, code, redirect_uri, verifier):
        self.calls.append(('exchange', code))
        return self.result

    def subject(self, token):
        self.calls.append(('subject', token))
        return 'synthetic-subject-hash'

    def refresh(self, refresh_token, *, timeout=None):
        self.calls.append(('refresh', refresh_token))
        if isinstance(self.refresh_result, BaseException):
            raise self.refresh_result
        return self.refresh_result


class OAuthManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'data'
        from local_meeting_recorder.oauth import FeishuAppCredentials

        self.credentials = FeishuAppCredentials('synthetic-app', 'private-secret')
        self.transport = FakeTransport()
        self.now = [1000.0]
        self.manager = OAuthManager(self.credentials, self.directory,
                                    transport=self.transport,
                                    clock=lambda: self.now[0])

    def authorize(self):
        return self.manager.authorize('code-1', 'http://127.0.0.1:8765/oauth/callback',
                                      'verifier')

    def test_authorize_status_and_reuse(self):
        self.assertEqual(self.authorize()['authorized'], True)
        self.assertEqual(self.manager.status()['state'], 'ready')
        self.assertEqual(self.manager.access_token(), 'private-access')
        self.assertNotIn(('refresh', 'private-refresh'), self.transport.calls)

    def test_rejected_token_forces_one_refresh(self):
        self.authorize()
        token = self.manager.access_token(rejected_token='private-access')
        self.assertEqual(token, 'next-private-access')
        self.assertIn(('refresh', 'private-refresh'), self.transport.calls)

    def test_refresh_failure_requires_reauthorization(self):
        self.authorize()
        self.transport.refresh_result = OAuthError('oauth_request_failed')
        with self.assertRaises(OAuthError):
            self.manager.access_token(rejected_token='private-access')
        self.assertEqual(self.manager.status()['state'],
                         'reauthorization_required')
        with self.assertRaises(OAuthError) as caught:
            self.manager.access_token()
        self.assertEqual(caught.exception.reason, 'oauth_reauthorization_required')

    def test_expired_refresh_token_skips_transport(self):
        self.now[0] = 1000.0
        self.authorize()
        self.now[0] += 604800 + 10  # beyond refresh_token_expires_in
        with self.assertRaises(OAuthError) as caught:
            self.manager.access_token(rejected_token='private-access')
        self.assertEqual(caught.exception.reason, 'oauth_reauthorization_required')
        self.assertNotIn(('refresh', 'private-refresh'), self.transport.calls)

    def test_missing_scopes_rejected(self):
        self.transport.result = token_result(
            scopes=('calendar:calendar:read', 'offline_access'))
        with self.assertRaises(OAuthError) as caught:
            self.authorize()
        self.assertEqual(caught.exception.reason, 'oauth_missing_scopes')

    def test_logout_clears_state(self):
        self.authorize()
        self.assertEqual(self.manager.logout()['authorized'], False)
        self.assertEqual(self.manager.status()['state'], 'not_authorized')


class CallbackTests(unittest.TestCase):
    def parameters(self):
        return authorization_parameters('synthetic-app', 8765)

    def test_authorization_parameters_pkce_shape(self):
        from urllib.parse import parse_qs, urlsplit

        parameters = self.parameters()
        self.assertEqual(parameters['redirect_uri'],
                         'http://127.0.0.1:8765/oauth/callback')
        query = parse_qs(urlsplit(parameters['url']).query)
        self.assertEqual(query['scope'], [' '.join(SCOPES)])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['redirect_uri'], [parameters['redirect_uri']])

    def test_callback_code_accepts_valid_query(self):
        parameters = self.parameters()
        from urllib.parse import urlencode

        code = callback_code(
            '/oauth/callback?' + urlencode({'code': 'abc-def_1',
                                            'state': parameters['state']}),
            parameters['state'])
        self.assertEqual(code, 'abc-def_1')

    def test_callback_rejects_denied_and_state_mismatch(self):
        from urllib.parse import urlencode

        parameters = self.parameters()
        with self.assertRaises(OAuthError) as caught:
            callback_code(
                '/oauth/callback?' + urlencode(
                    {'error': 'access_denied', 'state': parameters['state']}),
                parameters['state'])
        self.assertEqual(caught.exception.reason, 'oauth_denied')
        with self.assertRaises(OAuthError) as caught:
            callback_code('/oauth/callback?code=abc&state=other',
                          parameters['state'])
        self.assertEqual(caught.exception.reason, 'oauth_invalid_callback')


class LoadCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self._saved = tuple(
            __import__('os').environ.get(key) for key in
            ('LMR_FEISHU_APP_ID', 'LMR_FEISHU_APP_SECRET'))

    def tearDown(self):
        import os

        for key, value in zip(('LMR_FEISHU_APP_ID', 'LMR_FEISHU_APP_SECRET'),
                              self._saved):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_reads_config_json(self):
        (self.directory / 'config.json').write_text(
            json.dumps({'app_id': 'cli_a', 'app_secret': 's'}))
        credentials = load_credentials(self.directory)
        self.assertEqual((credentials.app_id, credentials.app_secret),
                         ('cli_a', 's'))

    def test_env_vars_win_over_config(self):
        import os

        os.environ['LMR_FEISHU_APP_ID'] = 'cli_env'
        os.environ['LMR_FEISHU_APP_SECRET'] = 's2'
        (self.directory / 'config.json').write_text(
            json.dumps({'app_id': 'cli_a', 'app_secret': 's'}))
        self.assertEqual(load_credentials(self.directory).app_id, 'cli_env')

    def test_missing_credentials_raise(self):
        with self.assertRaises(OAuthError):
            load_credentials(self.directory)


class SttAuthProviderTests(unittest.TestCase):
    def test_payload_and_expired_hook_use_refresh_rotation(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        from local_meeting_recorder.oauth import FeishuAppCredentials

        transport = FakeTransport()
        now = [1000.0]
        manager = OAuthManager(FeishuAppCredentials('app', 'secret'),
                               Path(temp.name), transport=transport,
                               clock=lambda: now[0])
        manager.authorize('code-1', 'http://127.0.0.1:8765/oauth/callback', 'v')
        payload, on_expired = stt_auth_provider(manager, 'cal_a/evt_b')
        self.assertEqual(payload(), {'user_access_token': 'private-access',
                                     'calendar_event_id': 'cal_a/evt_b'})
        on_expired()  # server said auth_expired: silently rotate
        self.assertEqual(payload()['user_access_token'],
                         'next-private-access')


if __name__ == '__main__':
    unittest.main()
