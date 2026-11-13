import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from auth.config import FeishuAppCredentials
from auth.errors import AuthError
from auth.service import AuthService
from auth.transport import FeishuTransport  # noqa: F401  (coverage source)

APP = FeishuAppCredentials('cli_test', 'sec_test')
SCOPES = ('calendar:calendar:read', 'calendar:calendar.event:read', 'offline_access')


def make_token(access, refresh, *, expires_in=7200, refresh_expires_in=2592000, scope=None):
    return {'access_token': access, 'refresh_token': refresh, 'token_type': 'Bearer',
            'expires_in': expires_in, 'refresh_token_expires_in': refresh_expires_in,
            'scope': ' '.join(scope if scope is not None else SCOPES)}


class FakeTransport:
    def __init__(self, open_id='ou_A', *, refresh_delay=0.0):
        self.open_id = open_id
        self.refresh_delay = refresh_delay
        self.refresh_calls = 0
        self.exchange_calls = 0
        self.subject_calls = 0
        self.fail_refresh = False
        self.no_rotation = False
        self.fail_exchange = False

    def exchange(self, code, redirect_uri, verifier):
        self.exchange_calls += 1
        if self.fail_exchange:
            raise AuthError('oauth_token_rejected', stage='code_exchange')
        self.last_verifier = verifier
        self.last_redirect = redirect_uri
        return make_token('u-first', 'r-first')

    def refresh(self, refresh_token, *, timeout=None):
        self.refresh_calls += 1
        if self.refresh_delay:
            time.sleep(self.refresh_delay)
        if self.fail_refresh:
            raise AuthError('oauth_request_failed', stage='refresh')
        self.last_refresh_input = refresh_token
        new_refresh = refresh_token if self.no_rotation else f'r-rot-{self.refresh_calls}'
        return make_token(f'u-{self.refresh_calls}', new_refresh)

    def subject(self, token):
        self.subject_calls += 1
        return self.open_id

    def user_info(self, token):
        self.subject_calls += 1
        return {'open_id': self.open_id, 'name': 'Kai Han',
                'en_name': 'Kai Han', 'email': 'kai.han@nio.com'}


class ManualClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_service(tmp, transport=None, clock=None):
    return AuthService(APP, Path(tmp), base_url='https://auth.internal',
                       transport=transport, clock=clock or ManualClock())


def authorize(service, transport, *, intent='join:123456789'):
    start = service.authorize_start('ou_A', intent)
    from urllib import parse
    state = parse.parse_qs(parse.urlsplit(start['authorize_url']).query)['state'][0]
    query = parse.urlencode({'code': 'c-abc', 'state': state})
    return start, service.callback(query)


class StoreTests(unittest.TestCase):
    def test_directory_and_file_permissions(self):
        with TemporaryDirectory() as tmp:
            service = make_service(tmp)
            service.store.write('ou_A', service.store.marker('ou_A', 'ready', None))
            credentials_dir = Path(tmp) / 'credentials'
            record = credentials_dir / 'ou_A.json'
            self.assertEqual(credentials_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(record.stat().st_mode & 0o777, 0o600)
            leftovers = [p.name for p in credentials_dir.iterdir() if p.name.startswith('.auth-')]
            self.assertEqual(leftovers, [])

    def test_rejects_invalid_open_ids(self):
        with TemporaryDirectory() as tmp:
            service = make_service(tmp)
            for bad in ('', 'ou_A.json', '../ou_A', 'u_B', 'ou_A/x', None, 7):
                with self.assertRaises(AuthError, msg=repr(bad)):
                    service.store.read(bad)


class AuthorizeFlowTests(unittest.TestCase):
    def test_authorize_start_builds_pkce_url_and_persists_state(self):
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = make_service(tmp, transport)
            result = service.authorize_start('ou_A', 'join:123')
            from urllib import parse
            query = parse.parse_qs(parse.urlsplit(result['authorize_url']).query)
            self.assertEqual(query['client_id'], ['cli_test'])
            self.assertEqual(query['code_challenge_method'], ['S256'])
            self.assertEqual(query['prompt'], ['consent'])
            self.assertEqual(query['redirect_uri'], ['https://auth.internal/oauth/callback'])
            self.assertTrue((Path(tmp) / 'pending_states.json').exists())

    def test_callback_success_writes_ready_record(self):
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = make_service(tmp, transport)
            _, outcome = authorize(service, transport)
            self.assertEqual(outcome['outcome'], 'success')
            self.assertEqual(outcome['intent'], 'join:123456789')
            record = service.store.read('ou_A')
            self.assertEqual(record['state'], 'ready')
            self.assertEqual(record['access_token'], 'u-first')
            self.assertEqual(record['refresh_token'], 'r-first')
            self.assertTrue(record['subject_hash'])

    def test_callback_subject_mismatch_rejects_without_record(self):
        with TemporaryDirectory() as tmp:
            transport = FakeTransport(open_id='ou_SOMEONE_ELSE')
            service = make_service(tmp, transport)
            _, outcome = authorize(service, transport)
            self.assertEqual(outcome['outcome'], 'failed')
            self.assertEqual(outcome['stage'], 'identity')
            self.assertFalse(service.store.exists('ou_A'))

    def test_callback_denied_releases_state(self):
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = make_service(tmp, transport)
            start = service.authorize_start('ou_A', 'join:1')
            from urllib import parse
            state = parse.parse_qs(parse.urlsplit(start['authorize_url']).query)['state'][0]
            outcome = service.callback(parse.urlencode({'error': 'access_denied', 'state': state}))
            self.assertEqual(outcome['outcome'], 'denied')
            with self.assertRaises(AuthError):
                service.states.take(state)
            self.assertFalse(service.store.exists('ou_A'))

    def test_callback_replay_of_used_state_is_invalid(self):
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = make_service(tmp, transport)
            start, _ = authorize(service, transport)
            from urllib import parse
            state = parse.parse_qs(parse.urlsplit(start['authorize_url']).query)['state'][0]
            again = service.callback(parse.urlencode({'code': 'c-abc', 'state': state}))
            self.assertEqual(again['outcome'], 'invalid')
            self.assertEqual(transport.exchange_calls, 1)

    def test_callback_unknown_state_is_invalid(self):
        with TemporaryDirectory() as tmp:
            service = make_service(tmp, FakeTransport())
            self.assertEqual(service.callback('code=c-1&state=nope')['outcome'], 'invalid')

    def test_state_expiry(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            service = make_service(tmp, FakeTransport(), clock)
            start = service.authorize_start('ou_A', 'x')
            clock.advance(601)
            from urllib import parse
            state = parse.parse_qs(parse.urlsplit(start['authorize_url']).query)['state'][0]
            self.assertEqual(service.callback(f'code=c&state={state}')['outcome'], 'invalid')


class DiscoveryLoginTests(unittest.TestCase):
    """GUI 扫码登录（CONTRACT_AUTH v1.2）：discovery start + 采纳 + status。"""

    def gui_authorize(self, service, transport):
        from urllib import parse
        start = service.authorize_start(None, 'gui:login')
        state = parse.parse_qs(parse.urlsplit(start['authorize_url']).query)['state'][0]
        return start, state

    def complete_login(self, service, transport, clock=None):
        from urllib import parse
        start, state = self.gui_authorize(service, transport)
        outcome = service.callback(parse.urlencode({'code': 'c-gui', 'state': state}))
        return start, state, outcome

    def test_start_allows_null_open_id_only_for_gui_intent(self):
        with TemporaryDirectory() as tmp:
            service = make_service(tmp, FakeTransport())
            result = service.authorize_start(None, 'gui:login')
            self.assertTrue(result['authorize_url'].startswith('https://'))
            # bound intents still require an open_id
            with self.assertRaises(AuthError):
                service.authorize_start(None, 'join:123')
            # gui:login with a bound open_id is rejected (advisor sends null)
            with self.assertRaises(AuthError):
                service.authorize_start('ou_A', 'gui:login')

    def test_callback_adopts_scanned_identity(self):
        with TemporaryDirectory() as tmp:
            transport = FakeTransport(open_id='ou_KAI')
            service = make_service(tmp, transport)
            _, _, outcome = self.complete_login(service, transport)
            self.assertEqual(outcome['outcome'], 'success')
            self.assertEqual(outcome['open_id'], 'ou_KAI')
            record = service.store.read('ou_KAI')
            self.assertEqual(record['state'], 'ready')
            self.assertTrue(record['subject_hash'])

    def test_status_pending_then_completed_with_summary(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            transport = FakeTransport(open_id='ou_KAI')
            service = make_service(tmp, transport, clock)
            _, state, outcome = self.complete_login(service, transport)
            self.assertEqual(outcome['outcome'], 'success')
            result = service.authorize_status(state)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['user']['open_id'], 'ou_KAI')
            self.assertEqual(result['user']['email'], 'kai.han@nio.com')
            # red line: no token material in the status payload
            self.assertNotIn('access_token', result)
            self.assertNotIn('refresh_token', str(result))

    def test_status_pending_before_callback_and_after_failure(self):
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = make_service(tmp, transport)
            _, state = self.gui_authorize(service, transport)
            self.assertEqual(service.authorize_status(state), {'status': 'pending'})
            # consumed-but-failed (exchange error) stays pending until TTL
            transport.fail_exchange = True
            from urllib import parse
            outcome = service.callback(parse.urlencode({'code': 'c-bad', 'state': state}))
            self.assertEqual(outcome['outcome'], 'failed')
            self.assertEqual(service.authorize_status(state), {'status': 'pending'})

    def test_status_unknown_or_expired_state_is_not_found(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = make_service(tmp, transport, clock)
            with self.assertRaises(AuthError) as ctx:
                service.authorize_status('nope')
            self.assertEqual(ctx.exception.reason, 'oauth_unknown_state')
            _, state = self.gui_authorize(service, transport)
            clock.advance(601)
            with self.assertRaises(AuthError):
                service.authorize_status(state)

    def test_non_discovery_identity_mismatch_still_rejects(self):
        with TemporaryDirectory() as tmp:
            transport = FakeTransport(open_id='ou_SOMEONE_ELSE')
            service = make_service(tmp, transport)
            _, outcome = authorize(service, transport)
            self.assertEqual(outcome['outcome'], 'failed')
            self.assertEqual(outcome['stage'], 'identity')
            self.assertFalse(service.store.exists('ou_A'))

    def test_non_discovery_success_also_lands_summary(self):
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = make_service(tmp, transport)
            from urllib import parse
            start = service.authorize_start('ou_A', 'join:9')
            state = parse.parse_qs(parse.urlsplit(start['authorize_url']).query)['state'][0]
            self.assertEqual(service.callback(
                parse.urlencode({'code': 'c-abc', 'state': state}))['outcome'], 'success')
            result = service.authorize_status(state)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['user']['open_id'], 'ou_A')


class TokenTests(unittest.TestCase):
    def prepare(self, tmp, transport, clock):
        service = make_service(tmp, transport, clock)
        authorize(service, transport)
        return service

    def test_fresh_token_returned_without_transport_call(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = self.prepare(tmp, transport, clock)
            token = service.get_valid_token('ou_A')
            self.assertEqual(token['access_token'], 'u-first')
            self.assertEqual(transport.refresh_calls, 0)

    def test_token_read_backfills_missing_profile(self):
        """Pre-v1.3 users get their profile lazily on the next token read."""
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = self.prepare(tmp, transport, clock)
            for path in (Path(tmp) / 'profiles').glob('*.json'):
                path.unlink()
            token = service.get_valid_token('ou_A')
            self.assertEqual(token['access_token'], 'u-first')
            profile = service.user_profile('ou_A')
            self.assertEqual(profile['name'], 'Kai Han')
            self.assertEqual(profile['email'], 'kai.han@nio.com')

    def test_near_expiry_refreshes_once_with_rotation(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = self.prepare(tmp, transport, clock)
            clock.advance(7200 - 100 + 1)
            token = service.get_valid_token('ou_A')
            self.assertEqual(token['access_token'], 'u-1')
            self.assertEqual(transport.refresh_calls, 1)
            self.assertEqual(transport.last_refresh_input, 'r-first')

    def test_rejected_token_forces_refresh(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = self.prepare(tmp, transport, clock)
            token = service.get_valid_token('ou_A', rejected_token='u-first')
            self.assertEqual(token['access_token'], 'u-1')
            self.assertEqual(transport.refresh_calls, 1)

    def test_refresh_must_rotate_otherwise_reauth(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            transport.no_rotation = True
            service = self.prepare(tmp, transport, clock)
            clock.advance(7101)
            with self.assertRaises(AuthError) as ctx:
                service.get_valid_token('ou_A')
            # The surfaced reason is the provider response fault; the stored
            # record must already be fail-closed reauthorization_required.
            self.assertEqual(ctx.exception.reason, 'oauth_invalid_response')
            self.assertEqual(service.store.read('ou_A')['state'], 'reauthorization_required')

    def test_network_failure_mid_refresh_is_fail_closed(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            transport.fail_refresh = True
            service = self.prepare(tmp, transport, clock)
            clock.advance(7101)
            with self.assertRaises(AuthError):
                service.get_valid_token('ou_A')
            record = service.store.read('ou_A')
            self.assertEqual(record['state'], 'reauthorization_required')
            transport.fail_refresh = False
            with self.assertRaises(AuthError):
                service.get_valid_token('ou_A')
            self.assertEqual(transport.refresh_calls, 1)

    def test_crash_marker_residue_requires_reauth_without_post(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = self.prepare(tmp, transport, clock)
            service.store.write('ou_A', service.store.marker('ou_A', 'refreshing', 'h'))
            with self.assertRaises(AuthError) as ctx:
                service.get_valid_token('ou_A')
            self.assertEqual(ctx.exception.reason, 'oauth_reauthorization_required')
            self.assertEqual(transport.refresh_calls, 0)

    def test_refresh_token_expiry_short_circuits(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            transport = FakeTransport()
            service = self.prepare(tmp, transport, clock)
            clock.advance(2592001)
            with self.assertRaises(AuthError):
                service.get_valid_token('ou_A')
            self.assertEqual(transport.refresh_calls, 0)
            self.assertEqual(service.store.read('ou_A')['state'], 'reauthorization_required')

    def test_missing_record_is_not_authorized(self):
        with TemporaryDirectory() as tmp:
            service = make_service(tmp, FakeTransport())
            with self.assertRaises(AuthError) as ctx:
                service.get_valid_token('ou_A')
            self.assertEqual(ctx.exception.reason, 'oauth_not_authorized')

    def test_concurrent_refresh_is_single_flight(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            transport = FakeTransport(refresh_delay=0.15)
            service = self.prepare(tmp, transport, clock)
            clock.advance(7101)
            results = []
            errors = []

            def worker():
                try:
                    results.append(service.get_valid_token('ou_A'))
                except AuthError as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(transport.refresh_calls, 1)
            self.assertEqual({r['access_token'] for r in results}, {'u-1'})


class RecordValidationTests(unittest.TestCase):
    def test_token_record_rejects_bad_payloads(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            service = make_service(tmp, FakeTransport(), clock)
            bad_cases = [
                make_token('u', 'r', expires_in=0),
                make_token('u', 'r', refresh_expires_in=367 * 86400),
                make_token('u', 'r', scope=('calendar:calendar:read',)),
                make_token('u', 'r', scope=SCOPES + ('weird:scope',)),
                {'access_token': 'u', 'refresh_token': 'r', 'token_type': 'Bearer',
                 'expires_in': 100, 'refresh_token_expires_in': 100, 'scope': ''},
            ]
            for payload in bad_cases:
                with self.assertRaises(AuthError):
                    service.store.token_record(payload, started=0, open_id='ou_A', subject_hash=None)

    def test_record_roundtrip_via_json(self):
        clock = ManualClock()
        with TemporaryDirectory() as tmp:
            service = make_service(tmp, FakeTransport(), clock)
            record = service.store.token_record(
                make_token('u', 'r'), started=10, open_id='ou_A', subject_hash='h')
            service.store.write('ou_A', record)
            self.assertEqual(json.loads((Path(tmp) / 'credentials' / 'ou_A.json').read_text())['open_id'], 'ou_A')


if __name__ == '__main__':
    unittest.main()
