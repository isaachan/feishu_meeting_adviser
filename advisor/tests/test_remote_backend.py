"""RemoteModelBackend against an in-process contract stub server."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from feishu_meeting_adviser.llm import Decision, LLMSettings, ModelError
from feishu_meeting_adviser.remote_backend import RemoteModelBackend, remote_identity


def settings(url, timeout=3):
    return LLMSettings(backend='remote', remote_url=url, remote_backend='fake',
                       timeout_seconds=timeout)


class StubGateway:
    """Minimal contract server with scripted behaviours."""

    def __init__(self):
        self.requests = []
        self.cancels = []
        self.behavior = {'state': 'succeeded',
                         'decision': {'control': 'none', 'control_sentence_keys': [],
                                      'reply': '远端回答', 'reply_sentence_keys': ['s:1']}}
        self.drop_first_post = False
        self.fail_posts = 0
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                return

            def reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                payload = json.loads(self.rfile.read(length) or b'{}')
                with outer.lock:
                    if self.path.endswith('/cancel'):
                        outer.cancels.append(payload if isinstance(payload, str) else self.path)
                        return self.reply(200, {'request_id': 'x', 'state': 'cancelled'})
                    if outer.drop_first_post:
                        outer.drop_first_post = False
                        self.close_connection = True
                        return
                    if outer.fail_posts:
                        outer.fail_posts -= 1
                        return self.reply(429, {'error': {'code': 'quota_exceeded',
                                                           'retry_after_seconds': 1}})
                    outer.requests.append(payload)
                    state = outer.behavior['state']
                    body = {'request_id': payload['request_id'], 'state': state,
                            'request': {'content_hash': payload['content_hash'],
                                        'backend': payload['backend'],
                                        'model': None, 'metadata': payload.get('metadata', {})}}
                    if state == 'succeeded':
                        body['result'] = {'output': 'decision-v1',
                                          'decision': outer.behavior['decision'],
                                          'timings': {'queued_ms': 1, 'execution_ms': 2}}
                    elif state == 'failed':
                        body['error'] = {'code': outer.behavior.get('code', 'backend_unavailable')}
                    return self.reply(202, body)

            def do_GET(self):
                with outer.lock:
                    behavior = dict(outer.behavior)
                    submitted = bool(outer.requests)
                if not submitted:
                    return self.reply(404, {'error': {'code': 'not_found'}})
                body = {'request_id': self.path.rsplit('/', 1)[-1],
                        'state': behavior['state'],
                        'request': {'content_hash': 'x', 'backend': 'fake',
                                    'model': None, 'metadata': {}}}
                if behavior['state'] == 'succeeded':
                    body['result'] = {'output': 'decision-v1',
                                      'decision': behavior['decision'],
                                      'timings': {'queued_ms': 1, 'execution_ms': 2}}
                elif behavior['state'] == 'failed':
                    body['error'] = {'code': behavior.get('code', 'backend_unavailable')}
                return self.reply(200, body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever,
                         kwargs={'poll_interval': 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class RemoteBackendTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubGateway()
        self.addCleanup(self.stub.close)

    def backend(self, timeout=3, stop_event=None):
        return RemoteModelBackend(settings(self.stub.url, timeout), None,
                                  stop_event=stop_event or threading.Event())

    def test_success_returns_decision_with_timings(self):
        backend = self.backend()
        decision = backend.decide({'input': 1}, 'role')
        self.assertEqual((decision.reply, decision.reply_sentence_keys),
                         ('远端回答', ('s:1',)))
        self.assertEqual(backend.diagnostic_metadata()['remote_timings'],
                         {'queued_ms': 1, 'execution_ms': 2})

    def test_request_identity_is_deterministic_per_content(self):
        first = remote_identity('role', {'input': 1}, settings(self.stub.url))
        second = remote_identity('role', {'input': 1}, settings(self.stub.url))
        other = remote_identity('role', {'input': 2}, settings(self.stub.url))
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_dropped_connection_reattaches_the_same_request(self):
        self.stub.drop_first_post = True
        backend = self.backend()
        decision = backend.decide({'input': 1}, 'role')
        self.assertEqual(decision.reply, '远端回答')
        self.assertEqual(len(self.stub.requests), 1)  # same task, not a second execution

    def test_error_codes_map_to_model_errors(self):
        for code, reason in (('execution_timeout', 'model_timeout'),
                             ('output_invalid', 'model_invalid_decision'),
                             ('backend_unavailable', 'model_unavailable'),
                             ('quota_exceeded', 'model_unavailable')):
            with self.subTest(code=code):
                self.stub.behavior = {'state': 'failed', 'code': code}
                with self.assertRaises(ModelError) as caught:
                    self.backend().decide({'input': 1}, 'role')
                self.assertEqual(caught.exception.reason, reason)

    def test_submit_quota_error_is_mapped_immediately(self):
        self.stub.fail_posts = 1
        with self.assertRaises(ModelError) as caught:
            self.backend().decide({'input': 1}, 'role')
        self.assertEqual(caught.exception.reason, 'model_unavailable')

    def test_stop_event_cancels_the_remote_task(self):
        self.stub.behavior = {'state': 'running'}
        stop = threading.Event()

        def blocker():
            time.sleep(0.2)
            stop.set()

        threading.Thread(target=blocker, daemon=True).start()
        with self.assertRaises(ModelError) as caught:
            self.backend(stop_event=stop).decide({'input': 1}, 'role')
        self.assertEqual(caught.exception.reason, 'model_stopped')
        self.assertEqual(len(self.stub.cancels), 1)

    def test_poll_deadline_maps_to_model_timeout(self):
        self.stub.behavior = {'state': 'running'}
        with self.assertRaises(ModelError) as caught:
            self.backend(timeout=1).decide({'input': 1}, 'role')
        self.assertEqual(caught.exception.reason, 'model_timeout')


if __name__ == '__main__':
    unittest.main()
