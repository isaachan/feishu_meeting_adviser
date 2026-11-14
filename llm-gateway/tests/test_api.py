"""End-to-end HTTP tests over a real localhost server (contract sections 3-6)."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from llm_gateway.api import GatewayApp, create_server
from llm_gateway.backends.fake import FakeBackend, SleepingBackend
from llm_gateway.scheduler import ExecutionScheduler
from llm_gateway.service import GatewayService, canonical_hash
from llm_gateway.store import GatewayStore


def payload(request_id='req-0001', input_value=None, metadata=None):
    body = {'schema_version': 1, 'request_id': request_id, 'backend': 'fake',
            'model': None, 'system': '角色', 'input': input_value or {'input': 1},
            'output': 'decision-v1', 'deadline_seconds': 30}
    body['content_hash'] = canonical_hash(body['system'], body['input'], body['output'],
                                          body['backend'], body['model'])
    if metadata is not None:
        body['metadata'] = metadata
    return body


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / 'tasks.sqlite3'
        self.backend = None
        self.server = self.start_server()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.scheduler.stop)

    def start_server(self, *, workers=2):
        self.store = GatewayStore(self.database)
        self.service = GatewayService(self.store)
        self.scheduler = ExecutionScheduler(
            self.store, lambda backend: self.backend or FakeBackend(), workers=workers)
        self.scheduler.start()
        server = create_server(GatewayApp(self.service, self.scheduler),
                               host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        return server

    def request(self, method, path, body=None):
        url = f'http://127.0.0.1:{self.server.server_address[1]}{path}'
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
        # Localhost must bypass any configured corporate proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def wait_state(self, request_id, state, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, body = self.request('GET', f'/v1/inference-jobs/{request_id}')
            if body.get('state') == state:
                return body
            time.sleep(0.02)
        self.fail(f'task did not reach {state}')

    def test_health_and_full_success_flow(self):
        status, body = self.request('GET', '/healthz')
        self.assertEqual((status, body['status']), (200, 'ok'))
        status, body = self.request('POST', '/v1/inference-jobs', payload(
            metadata={'fake': {'behavior': 'ok', 'reply': 'HTTP 回答', 'keys': ['s:1']}}))
        self.assertEqual(status, 202)
        done = self.wait_state('req-0001', 'succeeded')
        self.assertEqual(done['result']['decision']['reply'], 'HTTP 回答')
        self.assertIn('queued_ms', done['result']['timings'])

    def test_session_prewarm_endpoint_binds_owner(self):
        class Catalogs:
            def __init__(self):
                self.calls = []

            def prepare(self, session_id, owner):
                self.calls.append((session_id, owner))

        catalogs = Catalogs()
        self.service.catalogs = catalogs
        status, _ = self.request('POST', '/v1/context-sessions',
                                 {'session_id': 's-a', 'owner_open_id': 'ou_A'})
        self.assertEqual(status, 202)
        self.assertEqual(catalogs.calls, [('s-a', 'ou_A')])
        status, _ = self.request('POST', '/v1/context-sessions',
                                 {'session_id': '../bad', 'owner_open_id': 'ou_A'})
        self.assertEqual(status, 422)

    def test_idempotent_resubmit_conflict_and_not_found(self):
        self.request('POST', '/v1/inference-jobs', payload())
        status, body = self.request('POST', '/v1/inference-jobs', payload())
        self.assertEqual(status, 200)
        status, body = self.request('POST', '/v1/inference-jobs',
                                    payload(input_value={'input': 2}))
        self.assertEqual((status, body['error']['code']), (409, 'content_conflict'))
        status, body = self.request('GET', '/v1/inference-jobs/absent-0001')
        self.assertEqual((status, body['error']['code']), (404, 'not_found'))

    def test_validation_and_cancel_over_http(self):
        status, body = self.request('POST', '/v1/inference-jobs', {'schema_version': 9})
        self.assertEqual((status, body['error']['code']), (422, 'invalid_request'))
        self.request('POST', '/v1/inference-jobs', payload('req-cancel-1'))
        status, body = self.request('POST', '/v1/inference-jobs/req-cancel-1/cancel')
        self.assertEqual((status, body['state']), (200, 'cancelled'))
        status, body = self.request('POST', '/v1/inference-jobs/req-cancel-1/cancel')
        self.assertEqual((status, body['state']), (200, 'cancelled'))  # idempotent

    def test_running_cancel_interrupts_slow_execution(self):
        self.backend = SleepingBackend()
        self.request('POST', '/v1/inference-jobs', payload('req-slow'))
        self.assertTrue(self.backend.entered.wait(2))
        status, body = self.request('POST', '/v1/inference-jobs/req-slow/cancel')
        self.assertEqual((status, body['state']), (200, 'running'))
        self.wait_state('req-slow', 'cancelled')
        self.backend.release.set()

    def test_restart_recovery_fails_interrupted_tasks(self):
        self.backend = SleepingBackend()
        self.request('POST', '/v1/inference-jobs', payload('req-restart'))
        self.assertTrue(self.backend.entered.wait(2))
        self.scheduler.stop()
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        # A fresh gateway process on the same database recovers the task.
        store = GatewayStore(self.database)
        try:
            self.assertEqual(store.recover_interrupted(), 1)
            self.assertEqual(store.get('req-restart')['error_code'], 'backend_unavailable')
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
