import io
import json
import unittest
from unittest import mock
from urllib import error

from harness.client import GatewayClient, GatewayError


class StubResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StubOpener:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url,
                           req.data.decode() if req.data else None))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        body = json.dumps(step).encode() if step is not None else b''
        return StubResponse(body)


def http_error(code: int, body: dict | None = None) -> error.HTTPError:
    return error.HTTPError('http://gateway', code, 'err', None,
                           io.BytesIO(json.dumps(body or {}).encode()))


def succeeded_task(decision=None) -> dict:
    return {'state': 'succeeded', 'result': {
        'output': 'decision-v1',
        'decision': decision or {'control': 'none', 'control_sentence_keys': [],
                                 'reply': None, 'reply_sentence_keys': []},
        'timings': {'queued_ms': 3}}}


class GatewayClientTests(unittest.TestCase):
    def make_client(self, script, **kwargs):
        opener = StubOpener(script)
        return GatewayClient('http://127.0.0.1:8377/', owner='ou_A',
                             opener=opener, **kwargs), opener

    def test_submit_then_poll_to_success(self):
        client, opener = self.make_client([
            {'state': 'accepted'},
            {'state': 'running'},
            succeeded_task(),
        ])
        task = client.decide({'generation': 1})
        self.assertEqual(task['state'], 'succeeded')
        methods = [call[0] for call in opener.calls]
        self.assertEqual(methods, ['POST', 'GET', 'GET'])
        body = json.loads(opener.calls[0][2])
        self.assertEqual(body['schema_version'], 1)
        self.assertEqual(body['system'], 'meeting-snapshot-v1')
        self.assertEqual(body['output'], 'decision-v1')
        self.assertEqual(body['metadata'], {'client': 'llm-harness', 'owner_open_id': 'ou_A'})
        self.assertEqual(body['content_hash'], client.content_hash({'generation': 1}))
        self.assertTrue(body['request_id'].startswith('rm'))
        self.assertIn(body['request_id'], opener.calls[1][1])

    def test_transient_network_errors_tolerated(self):
        client, _ = self.make_client([
            error.URLError('down'),
            {'state': 'queued', 'transient': True},
            error.URLError('down'),
            succeeded_task(),
        ])
        self.assertEqual(client.decide({})['state'], 'succeeded')

    def test_404_poll_resubmits(self):
        client, opener = self.make_client([
            {'state': 'accepted'},
            http_error(404, {'error': {'code': 'not_found'}}),
            succeeded_task(),
        ])
        self.assertEqual(client.decide({})['state'], 'succeeded')
        self.assertEqual([call[0] for call in opener.calls], ['POST', 'GET', 'POST'])

    def test_expired_reruns_once(self):
        client, opener = self.make_client([
            {'state': 'accepted'},
            {'state': 'expired'},
            succeeded_task(),
        ])
        self.assertEqual(client.decide({})['state'], 'succeeded')
        self.assertEqual([call[0] for call in opener.calls], ['POST', 'GET', 'POST'])

    def test_failed_raises_code(self):
        client, _ = self.make_client([
            {'state': 'accepted'},
            {'state': 'failed', 'error': {'code': 'output_invalid'}},
        ])
        with self.assertRaises(GatewayError) as ctx:
            client.decide({})
        self.assertEqual(ctx.exception.code, 'output_invalid')

    def test_http_submit_error_raises_code(self):
        client, _ = self.make_client([
            http_error(413, {'error': {'code': 'payload_too_large'}}),
        ])
        with self.assertRaises(GatewayError) as ctx:
            client.decide({})
        self.assertEqual(ctx.exception.code, 'payload_too_large')

    def test_cancelled_raises(self):
        client, _ = self.make_client([
            {'state': 'accepted'},
            {'state': 'cancelled'},
        ])
        with self.assertRaises(GatewayError) as ctx:
            client.decide({})
        self.assertEqual(ctx.exception.code, 'cancelled')

    def test_client_deadline(self):
        client, _ = self.make_client([{'state': 'queued'}], deadline_seconds=1)
        clock = iter([0, 100])
        with mock.patch('harness.client.time') as fake_time:
            fake_time.monotonic.side_effect = lambda: next(clock)
            fake_time.sleep.side_effect = lambda _seconds: None
            with self.assertRaises(GatewayError) as ctx:
                client.decide({})
        self.assertEqual(ctx.exception.code, 'client_deadline')

    def test_attempt_suffix_keeps_hash(self):
        client, opener = self.make_client([succeeded_task(), succeeded_task()])
        first = client.decide({'generation': 1})
        second = client.decide({'generation': 1}, attempt=1)
        self.assertEqual(first['state'], second['state'])
        first_body, second_body = (json.loads(call[2]) for call in opener.calls)
        self.assertNotEqual(first_body['request_id'], second_body['request_id'])
        self.assertTrue(second_body['request_id'].endswith('-r1'))
        self.assertEqual(first_body['content_hash'], second_body['content_hash'])

    def test_backend_in_hash(self):
        client, _ = self.make_client([])
        opencode = GatewayClient('http://x', owner='ou_A', backend='opencode',
                                 opener=StubOpener([]))
        fake = GatewayClient('http://x', owner='ou_A', backend='fake', opener=StubOpener([]))
        self.assertNotEqual(opencode.content_hash({}), fake.content_hash({}))
        self.assertEqual(opencode.content_hash({}), client.content_hash({}))

    def test_owner_in_hash_and_required(self):
        first, _ = self.make_client([])
        second = GatewayClient('http://x', owner='ou_B', opener=StubOpener([]))
        self.assertNotEqual(first.content_hash({}), second.content_hash({}))
        with self.assertRaises(TypeError):
            GatewayClient('http://x', opener=StubOpener([]))


if __name__ == '__main__':
    unittest.main()
