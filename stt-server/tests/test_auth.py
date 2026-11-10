import asyncio
import json
import threading
import time
import unittest
from pathlib import Path
from urllib import error

from stt_server.auth import (AuthError, AuthValidator, FeishuAuthTransport,
                             ValidatedAuth, _event_timestamp, _organizer_id)
from stt_server.service import SttService

FIXED_NOW = 1_000_000


def _event(organizer='u_1', *, start=FIXED_NOW - 600, end=FIXED_NOW + 600):
    return {'organizer': {'user_id': organizer},
            'start_time': {'timestamp': str(start)},
            'end_time': {'timestamp': str(end)}}


class FakeTransport:
    def __init__(self, *, event=None, missing_event=False,
                 identities=('u_1', 'ou_1')):
        self.event = event if event is not None else _event()
        self.missing_event = missing_event
        self.identities = identities
        self.user_calls = 0
        self.event_calls = 0

    def user_info(self, token):
        self.user_calls += 1
        if token == 'expired-token':
            raise AuthError('auth_expired', 'rejected')
        return self.identities[0], frozenset(self.identities)

    def calendar_event(self, token, calendar_id, event_id):
        self.event_calls += 1
        if self.missing_event:
            raise AuthError('unauthorized', 'not available')
        return dict(self.event)


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.now = FIXED_NOW
        self.transport = FakeTransport()
        self.validator = AuthValidator(
            transport=self.transport, clock=lambda: self.now)

    def validate(self, token='token-1', event='cal_a/evt_b'):
        return self.validator.validate(token, event)

    def test_success_returns_identity(self):
        validated = self.validate()
        self.assertEqual(validated, ValidatedAuth('u_1', 'cal_a/evt_b'))
        self.assertEqual(self.transport.user_calls, 1)

    def test_success_within_window_slack(self):
        self.transport.event = _event(start=FIXED_NOW + 1700,
                                      end=FIXED_NOW + 3600)
        self.validate()
        self.transport.event = _event(start=FIXED_NOW - 3600,
                                      end=FIXED_NOW - 1700)
        self.validate()

    def test_rejects_outside_window(self):
        for event in (_event(start=FIXED_NOW + 1900, end=FIXED_NOW + 3600),
                      _event(start=FIXED_NOW - 3600, end=FIXED_NOW - 1900)):
            self.transport.event = event
            with self.assertRaises(AuthError) as caught:
                self.validate()
            self.assertEqual(caught.exception.code, 'unauthorized')

    def test_rejects_non_organizer(self):
        self.transport.event = _event(organizer='u_2')
        with self.assertRaises(AuthError) as caught:
            self.validate()
        self.assertEqual(caught.exception.code, 'unauthorized')

    def test_organizer_may_match_open_id(self):
        self.transport.event = _event(organizer='ou_1')
        validated = self.validate()
        self.assertEqual(validated.user_id, 'u_1')

    def test_rejects_all_day_event_without_timestamps(self):
        self.transport.event = {'organizer': {'user_id': 'u_1'},
                                'start_time': {'date': '2026-11-10'},
                                'end_time': {'date': '2026-11-10'}}
        with self.assertRaises(AuthError) as caught:
            self.validate()
        self.assertEqual(caught.exception.code, 'unauthorized')

    def test_rejects_missing_event(self):
        self.transport.missing_event = True
        with self.assertRaises(AuthError) as caught:
            self.validate()
        self.assertEqual(caught.exception.code, 'unauthorized')

    def test_expired_token(self):
        with self.assertRaises(AuthError) as caught:
            self.validate(token='expired-token')
        self.assertEqual(caught.exception.code, 'auth_expired')
        self.assertEqual(self.transport.event_calls, 0)

    def test_rejects_malformed_inputs(self):
        for token, event in ((None, 'cal/evt'), ('', 'cal/evt'),
                             ('bad token!', 'cal/evt'), ('token-1', None),
                             ('token-1', 'no-slash'), ('token-1', '/evt'),
                             ('token-1', 'cal/'), ('token-1', 'cal/evt/x')):
            with self.assertRaises(AuthError) as caught:
                self.validator.validate(token, event)
            self.assertEqual(caught.exception.code,
                             'auth_expired' if event == 'cal/evt' else 'unauthorized')
        self.assertEqual(self.transport.user_calls, 0)

    def test_cache_prevents_second_feishu_round_trip(self):
        self.validate()
        self.validate()
        self.assertEqual(self.transport.user_calls, 1)
        self.assertEqual(self.transport.event_calls, 1)

    def test_cache_expires_and_revalidates(self):
        self.validator.cache_seconds = 100
        self.validate()
        self.now += 101
        self.validate()
        self.assertEqual(self.transport.user_calls, 2)


class EventParsingTests(unittest.TestCase):
    def test_organizer_variants(self):
        self.assertEqual(_organizer_id({'organizer': {'user_id': 'u1'}}), 'u1')
        self.assertEqual(_organizer_id({'organizer': {'open_id': 'ou1'}}), 'ou1')
        self.assertEqual(_organizer_id({'creator': {'user_id': 'u1'}}), 'u1')
        self.assertIsNone(_organizer_id({}))
        self.assertIsNone(_organizer_id({'organizer': {'display_name': '张三'}}))

    def test_timestamp_variants(self):
        self.assertEqual(_event_timestamp({'start_time': {'timestamp': '100'}},
                                          'start_time'), 100)
        self.assertEqual(_event_timestamp({'start_time': {'timestamp': 100}},
                                          'start_time'), 100)
        self.assertIsNone(_event_timestamp({'start_time': {'date': '2026-11-10'}},
                                           'start_time'))
        self.assertIsNone(_event_timestamp({}, 'start_time'))


class _StubResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, limit=-1):
        return self._body[:limit] if limit > 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _StubOpener:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        result = self.handler(req)
        if isinstance(result, Exception):
            raise result
        return result


def _reply(status, payload):
    return _StubResponse(status, json.dumps(payload).encode())


class TransportTests(unittest.TestCase):
    def make_transport(self, handler):
        opener = _StubOpener(handler)
        return FeishuAuthTransport(
            base_url='https://feishu.example', opener=opener), opener

    def test_user_info_happy_path(self):
        transport, opener = self.make_transport(
            lambda req: _reply(200, {'code': 0, 'data': {
                'user_id': 'u1', 'open_id': 'ou1'}}))
        primary, identities = transport.user_info('token-1')
        self.assertEqual(primary, 'u1')
        self.assertEqual(identities, frozenset({'u1', 'ou1'}))
        request = opener.requests[0]
        self.assertEqual(request.full_url,
                         'https://feishu.example/open-apis/authen/v1/user_info')
        self.assertEqual(request.get_header('Authorization'), 'Bearer token-1')

    def test_user_info_maps_provider_codes(self):
        transport, _ = self.make_transport(
            lambda req: _reply(401, {'code': 99991668, 'msg': 'invalid'}))
        with self.assertRaises(AuthError) as caught:
            transport.user_info('token-1')
        self.assertEqual(caught.exception.code, 'auth_expired')
        transport, _ = self.make_transport(
            lambda req: _reply(200, {'code': 230001, 'msg': 'other'}))
        with self.assertRaises(AuthError) as caught:
            transport.user_info('token-1')
        self.assertEqual(caught.exception.code, 'auth_unavailable')

    def test_user_info_fail_closed_on_network_error(self):
        transport, _ = self.make_transport(
            lambda req: error.URLError('unreachable'))
        with self.assertRaises(AuthError) as caught:
            transport.user_info('token-1')
        self.assertEqual(caught.exception.code, 'auth_unavailable')
        transport, _ = self.make_transport(lambda req: _reply(200, b'not-json'
                                                              .decode()))
        with self.assertRaises(AuthError) as caught:
            transport.user_info('token-1')
        self.assertEqual(caught.exception.code, 'auth_unavailable')

    def test_calendar_event_happy_path(self):
        transport, opener = self.make_transport(
            lambda req: _reply(200, {'code': 0, 'data': {'event': _event()}}))
        event = transport.calendar_event('token-1', 'cal a', 'evt/b')
        self.assertEqual(event['organizer'], {'user_id': 'u_1'})
        self.assertEqual(opener.requests[0].full_url,
                         'https://feishu.example/open-apis/calendar/v4/'
                         'calendars/cal%20a/events/evt%2Fb')

    def test_calendar_event_permission_denied_is_unauthorized(self):
        transport, _ = self.make_transport(
            lambda req: _reply(200, {'code': 1901004, 'msg': 'no access'}))
        with self.assertRaises(AuthError) as caught:
            transport.calendar_event('token-1', 'cal', 'evt')
        self.assertEqual(caught.exception.code, 'unauthorized')


class _FakeStream:
    def accept_waveform(self, rate, samples):
        pass

    def input_finished(self):
        pass


class _FakeRecognizer:
    """Silent recognizer: accepted sessions create no events."""

    def create_stream(self):
        return _FakeStream()

    def is_ready(self, stream):
        return False

    def decode_stream(self, stream):
        pass

    def get_result(self, stream):
        return ''

    def is_endpoint(self, stream):
        return False

    def reset(self, stream):
        pass


class FakeValidator:
    def __init__(self):
        self.calls = []

    def validate(self, token, calendar_event_id):
        self.calls.append((token, calendar_event_id))
        if token == 'expired-token':
            raise AuthError('auth_expired', 'rejected')
        if token == 'outsider-token':
            raise AuthError('unauthorized', 'not organizer')
        return ValidatedAuth('u_1', calendar_event_id or 'cal_a/evt_b')


class AuthServiceTests(unittest.TestCase):
    """Auth-enabled shared server; recognizer factory counts creations."""

    recognizers: list = []

    @classmethod
    def setUpClass(cls):
        def factory():
            recognizer = _FakeRecognizer()
            cls.recognizers.append(recognizer)
            return recognizer

        cls.validator = FakeValidator()
        cls.service = SttService(model_dir=Path('.'),
                                 recognizer_factory=factory,
                                 max_sessions=1, idle_timeout=10.0,
                                 auth=cls.validator)
        cls.loop = asyncio.new_event_loop()
        cls._ready = threading.Event()
        cls._port = 0
        cls.thread = threading.Thread(target=cls._serve, daemon=True)
        cls.thread.start()
        cls._ready.wait(timeout=10)
        if not cls._port:
            raise RuntimeError('shared server did not start')

    @classmethod
    def _serve(cls):
        asyncio.set_event_loop(cls.loop)

        async def main():
            import websockets

            async with websockets.serve(cls.service.handle, '127.0.0.1', 0) as ws:
                cls._port = ws.sockets[0].getsockname()[1]
                cls._ready.set()
                await asyncio.Event().wait()

        cls.loop.create_task(main())
        cls.loop.run_forever()

    @classmethod
    def tearDownClass(cls):
        async def shutdown():
            current = asyncio.current_task()
            for task in [t for t in asyncio.all_tasks() if t is not current]:
                task.cancel()

        future = asyncio.run_coroutine_threadsafe(shutdown(), cls.loop)
        try:
            future.result(timeout=5)
        except Exception:  # noqa: BLE001 - best effort teardown
            pass
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.thread.join(timeout=5)
        cls.loop.close()

    def setUp(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.service.state.active:
            time.sleep(0.02)
        if self.service.state.active:
            self.fail('shared server has lingering sessions from a prior test')
        before = len(self.recognizers)
        self.addCleanup(self.assertEqual, len(self.recognizers), before,
                        'recognizers must only be created for accepted sessions')

    def connect(self):
        from websockets.sync.client import connect

        return connect(f'ws://127.0.0.1:{self._port}', open_timeout=5,
                       proxy=None)

    def start_message(self, token: str | None = 'token-1',
                      event: str = 'cal_a/evt_b', protocol: int = 2):
        payload = {'type': 'start', 'protocol': protocol, 'source_id': 'mic',
                   'rate': 16000, 'channels': 1}
        if token is not None:
            payload['auth'] = {'user_access_token': token,
                               'calendar_event_id': event}
        return json.dumps(payload)

    def recv_json(self, ws, timeout=5.0):
        return json.loads(ws.recv(timeout=timeout))

    def test_started_echoes_validated_identity(self):
        with self.connect() as ws:
            ws.send(self.start_message())
            started = self.recv_json(ws)
            self.assertEqual(started['type'], 'started')
            self.assertEqual(started['protocol'], 2)
            self.assertEqual(started['user_id'], 'u_1')
            self.assertEqual(started['calendar_event_id'], 'cal_a/evt_b')

    def test_missing_auth_object(self):
        with self.connect() as ws:
            ws.send(self.start_message(token=None))
            self.assertEqual(self.recv_json(ws)['code'], 'unauthorized')

    def test_protocol_1_rejected_when_auth_enabled(self):
        with self.connect() as ws:
            ws.send(self.start_message(protocol=1))
            self.assertEqual(self.recv_json(ws)['code'], 'bad_protocol')

    def test_outsider_and_expired_map_to_contract_codes(self):
        with self.connect() as ws:
            ws.send(self.start_message(token='outsider-token'))
            self.assertEqual(self.recv_json(ws)['code'], 'unauthorized')
        with self.connect() as ws:
            ws.send(self.start_message(token='expired-token'))
            self.assertEqual(self.recv_json(ws)['code'], 'auth_expired')

    def test_auth_precedes_session_limit(self):
        first = self.connect()
        first.send(self.start_message())
        self.assertEqual(self.recv_json(first)['type'], 'started')
        # auth failures surface even when the single session slot is full
        with self.connect() as ws:
            ws.send(self.start_message(token='outsider-token'))
            self.assertEqual(self.recv_json(ws)['code'], 'unauthorized')
        with self.connect() as ws:
            ws.send(self.start_message())
            self.assertEqual(self.recv_json(ws)['code'], 'overloaded')
        first.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.service.state.active:
            time.sleep(0.05)


if __name__ == '__main__':
    unittest.main()
