import io
import json
import unittest

from feishu_spectator.client import (ApiError, ErrorKind, SpectatorClient,
                                     parse_active_meetings)


class FakeResponse:
    def __init__(self, status=200, body=b'{}', headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self, limit=-1):
        return self._body[:limit] if limit >= 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(responses, *, provider=None, waits=None, monotonic=None):
    calls = []

    def token_provider(*, rejected_token=None):
        calls.append(rejected_token)
        return provider() if provider else 'token-1'

    holder = {'client': None, 'calls': calls}
    holder['client'] = SpectatorClient(
        token_provider, timeout=5, request_seconds=0.7,
        opener=FakeOpener(responses), monotonic=monotonic or (lambda: 0.0),
        wait=(waits.append if waits is not None else None) or (lambda s: False))
    return holder['client'], holder['client'].opener, calls


class ActiveMeetingsTests(unittest.TestCase):
    def test_nested_meeting_shape(self):
        response = {'code': 0, 'data': {'meetings': [
            {'meeting': {'id': '1234567890123', 'meeting_no': '123456789', 'topic': '周会'}}]}}
        meetings = parse_active_meetings(response)
        self.assertEqual(meetings[0].meeting_id, '1234567890123')
        self.assertEqual(meetings[0].meeting_no, '123456789')
        self.assertEqual(meetings[0].topic, '周会')

    def test_flat_shape(self):
        response = {'code': 0, 'data': {'meeting_list': [
            {'meeting_id': '9999999999999', 'meeting_no': '999999999', 'meeting_title': 'Sync'}]}}
        meetings = parse_active_meetings(response)
        self.assertEqual(meetings[0].meeting_id, '9999999999999')
        self.assertEqual(meetings[0].topic, 'Sync')

    def test_single_meeting(self):
        response = {'code': 0, 'data': {'meeting': {'id': '1234567890123'}}}
        self.assertEqual(parse_active_meetings(response)[0].meeting_id, '1234567890123')

    def test_short_id_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            parse_active_meetings({'code': 0, 'data': {'meetings': [{'meeting_id': '123456789'}]}})
        self.assertIs(ctx.exception.kind, ErrorKind.RESPONSE)

    def test_missing_data_rejected(self):
        with self.assertRaises(ApiError):
            parse_active_meetings({'code': 0})


class RequestTests(unittest.TestCase):
    def body(self, payload):
        return json.dumps(payload).encode()

    def test_events_carries_user_token_and_query(self):
        ok = FakeResponse(body=self.body({'code': 0, 'data': {'events': [], 'has_more': False}}))
        spectator, opener, calls = client([ok])
        spectator.events({'meeting_id': '1234567890123', 'page_size': 100, 'page_token': 'abc'})
        request = opener.requests[0]
        self.assertIn('Bearer token-1', request.get_header('Authorization'))
        self.assertIn('meeting_id=1234567890123', request.full_url)
        self.assertIn('page_token=abc', request.full_url)
        self.assertEqual(calls, [None])

    def test_events_requires_meeting_id(self):
        spectator, opener, _ = client([])
        with self.assertRaises(ValueError):
            spectator.events({})
        self.assertEqual(opener.requests, [])

    def test_token_rejection_refreshes_once_and_replays(self):
        denied = FakeResponse(status=401, body=self.body({'code': 99991663, 'msg': 'x'}))
        ok = FakeResponse(body=self.body({'code': 0, 'data': {'events': [], 'has_more': False}}))
        spectator, opener, calls = client([denied, ok])
        spectator.events({'meeting_id': '1234567890123'})
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(calls, [None, 'token-1'])

    def test_second_token_rejection_is_terminal(self):
        denied1 = FakeResponse(status=401, body=self.body({'code': 99991663}))
        denied2 = FakeResponse(status=401, body=self.body({'code': 99991663}))
        spectator, _, _ = client([denied1, denied2])
        with self.assertRaises(ApiError) as ctx:
            spectator.events({'meeting_id': '1234567890123'})
        self.assertIs(ctx.exception.kind, ErrorKind.TOKEN)

    def test_error_classification(self):
        cases = [((403, None), ErrorKind.DENIED), ((429, None), ErrorKind.RATE_LIMIT),
                 ((503, None), ErrorKind.SERVER), ((400, None), ErrorKind.PARAMETER),
                 ((404, None), ErrorKind.UNKNOWN), ((200, 121015), ErrorKind.DENIED)]
        for (status, code), expected in cases:
            payload = {'code': code} if code is not None else {}
            response = FakeResponse(status=status, body=self.body(payload))
            spectator, _, _ = client([response])
            with self.subTest(status=status, code=code):
                with self.assertRaises(ApiError) as ctx:
                    spectator.events({'meeting_id': '1234567890123'})
                self.assertIs(ctx.exception.kind, expected)
                self.assertNotIn('token-1', str(ctx.exception))

    def test_network_error(self):
        from urllib import error as urlerror
        spectator, _, _ = client([urlerror.URLError('boom')])
        with self.assertRaises(ApiError) as ctx:
            spectator.events({'meeting_id': '1234567890123'})
        self.assertIs(ctx.exception.kind, ErrorKind.NETWORK)

    def test_rate_limit_spacing(self):
        ok = FakeResponse(body=self.body({'code': 0, 'data': {'events': [], 'has_more': False}}))
        waits = []
        clock = {'now': 0.0}
        spectator = SpectatorClient(lambda **kwargs: 'token-1', timeout=5,
                                    request_seconds=0.7, opener=FakeOpener([ok, ok]),
                                    monotonic=lambda: clock['now'], wait=waits.append)
        spectator.events({'meeting_id': '1234567890123'})
        spectator.events({'meeting_id': '1234567890123'})
        self.assertEqual(waits, [0, 0, 0.7])  # initial check, then per-request spacing


if __name__ == '__main__':
    unittest.main()
