from io import BytesIO
from pathlib import Path
import json
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from feishu_meeting_adviser.client import ApiError, Credentials, ErrorKind, FeishuClient
from feishu_meeting_adviser.collector import Collector, ScanSettings
from feishu_meeting_adviser.models import SourceState
from feishu_meeting_adviser.recovery import RecoveryLoop
from feishu_meeting_adviser.storage import Store
from test_collector import event, page


class Response(BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(json.dumps(body).encode())
        self.status = status
        self.headers = headers or {}


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.requests = []
    def client(self, responses):
        items = iter(responses)
        def opener(req, timeout):
            self.requests.append(req)
            result = next(items)
            if isinstance(result, Exception):
                raise result
            return result
        def wait(seconds):
            self.now[0] += seconds
            return False
        return FeishuClient(Credentials('synthetic-app', 'private-secret'), opener=opener,
                            monotonic=lambda: self.now[0], clock=lambda: self.now[0], wait=wait)
    def token(self, token='private-token', expire=7200):
        return Response({'code': 0, 'tenant_access_token': token, 'expire': expire})
    def success(self):
        return Response({'code': 0, **page()})

    def test_token_refresh_on_actual_expiry_and_one_auth_replay(self):
        client = self.client([self.token(expire=10), self.success(), self.token('new-token'),
                              Response({'code':99991663}), self.token('fresh-token'), self.success()])
        client.events({'meeting_id':'synthetic-meeting'})
        self.now[0] += 6
        client.events({'meeting_id':'synthetic-meeting'})
        self.assertEqual(len(self.requests), 6)
        starts = [req.full_url.split('?')[0] for req in self.requests]
        self.assertEqual(sum(url.endswith('/internal') for url in starts), 3)

    def test_http_and_business_limit_classification_and_no_content_leak(self):
        client = self.client([self.token(), Response({'code':99991400, 'msg':'private-secret private-token synthetic-meeting'},
                                                    headers={'Retry-After':'60','X-Request-Id':'safe-id'})])
        with self.assertRaises(ApiError) as caught:
            client.events({'meeting_id':'synthetic-meeting'})
        exc = caught.exception
        self.assertEqual(exc.kind, ErrorKind.RATE_LIMIT)
        self.assertEqual(exc.retry_after, 60)
        for value in ('private-secret','private-token','synthetic-meeting'):
            self.assertNotIn(value, str(exc) + json.dumps(exc.safe_details()))
        client = self.client([self.token(), HTTPError('https://example.invalid', 503, 'secret message', {}, BytesIO(b'private body'))])
        with self.assertRaises(ApiError) as caught:
            client.events({})
        self.assertTrue(caught.exception.recoverable)

    def test_repeated_auth_failure_and_mutation_timeout_are_not_blindly_retried(self):
        client = self.client([self.token(), Response({'code':99991663}), self.token(), Response({'code':99991663})])
        with self.assertRaises(ApiError):
            client.events({})
        self.assertEqual(len(self.requests), 4)
        self.requests.clear()
        client = self.client([self.token(), URLError('private network details')])
        with self.assertRaises(ApiError):
            client.join('012345678')
        self.assertEqual(len(self.requests), 2)

    def test_endpoint_manual_action_overrides_http_500(self):
        client = self.client([self.token(), Response({'code':121001}, status=500)])
        with self.assertRaises(ApiError) as caught:
            client.join('012345678')
        self.assertFalse(caught.exception.recoverable)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'data'/'source.db')
        self.addCleanup(self.store.close)
        self.clock = lambda: 1_800_000_000
    def loop(self, outcomes, **kwargs):
        items = iter(outcomes)
        class API:
            def events(self, query):
                result = next(items)
                if isinstance(result, Exception):
                    raise result
                return result
        collector = Collector(self.store, 'm1', API(), settings=ScanSettings(max_pages_per_cycle=1), clock=self.clock)
        return RecoveryLoop(collector, jitter=lambda:0, **kwargs)

    def test_long_outage_recovers_and_gap_state_is_sticky(self):
        loop = self.loop([ApiError(ErrorKind.NETWORK)] * 8 + [page([event()]), page()])
        waits = [loop.step() for _ in range(8)]
        self.assertFalse(loop.terminal)
        self.assertEqual(waits[-1], 30)
        self.assertEqual(self.store.current_state('m1').state, SourceState.RECOVERING)
        loop.step()
        self.assertEqual(self.store.current_state('m1').state, SourceState.INCOMPLETE)
        loop.step()
        self.assertEqual(self.store.current_state('m1').reason, 'recovery_completeness_unverified')
        self.assertIsNotNone(self.store.caption('m1', 's:s1'))

    def test_permissions_and_bot_absence_stop_without_rejoin(self):
        for kind in (ErrorKind.DENIED, ErrorKind.BOT_ABSENT):
            loop = self.loop([ApiError(kind)])
            loop.step()
            self.assertTrue(loop.terminal)
            self.assertEqual(self.store.current_state('m1').state, SourceState.ACTION_REQUIRED)

    def test_parameter_error_resets_only_a_pending_cursor_once(self):
        loop = self.loop([page([event()], True, 'old'), ApiError(ErrorKind.PARAMETER, code=120001),
                          page([event()], True, 'new'), ApiError(ErrorKind.PARAMETER, code=120001)])
        loop.step()
        original = self.store.checkpoint('m1')
        loop.step()
        self.assertIsNone(self.store.checkpoint('m1')['cursor'])
        self.assertEqual(original['query_start'], self.store.checkpoint('m1')['query_start'])
        loop.step()
        loop.step()
        self.assertTrue(loop.terminal)
        self.assertEqual(len([r for r in self.store.read('m1', 'qa') if r.caption]), 1)

    def test_stop_interrupts_wait(self):
        import threading
        stop = threading.Event()
        diagnostics = []
        def diagnose(record):
            diagnostics.append(record)
            stop.set()
        loop = self.loop([ApiError(ErrorKind.RATE_LIMIT, retry_after=3600)], stop_event=stop, diagnose=diagnose)
        loop.run()
        self.assertTrue(stop.is_set())
        self.assertEqual(len(diagnostics), 1)

    def test_restart_during_recovery_retains_pending_gap(self):
        loop = self.loop([ApiError(ErrorKind.NETWORK)])
        loop.step()
        self.assertIsNotNone(self.store.checkpoint('m1').get('recovery_start'))
        restarted = self.loop([page([event()], True, 'next'), page()])
        restarted.step()
        self.assertIsNotNone(self.store.checkpoint('m1').get('recovery_start'))
        restarted.step()
        self.assertIsNone(self.store.checkpoint('m1').get('recovery_start'))
        self.assertEqual(self.store.current_state('m1').state, SourceState.INCOMPLETE)
