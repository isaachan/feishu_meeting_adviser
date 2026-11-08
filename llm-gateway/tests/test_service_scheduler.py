import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from llm_gateway.backends.base import BackendError
from llm_gateway.backends.fake import FakeBackend, SleepingBackend
from llm_gateway.decisions import InvalidDecision, validate
from llm_gateway.scheduler import ExecutionScheduler
from llm_gateway.service import GatewayService, Limits, RequestError, canonical_hash
from llm_gateway.store import GatewayStore


def decision_body(reply=None, keys=()):
    return {'control': 'none', 'control_sentence_keys': [],
            'reply': reply, 'reply_sentence_keys': list(keys)}


class DecisionValidationTests(unittest.TestCase):
    def test_valid_decisions_pass_and_normalize(self):
        self.assertEqual(validate(decision_body('回答', ['s:1'])),
                         decision_body('回答', ['s:1']))
        self.assertEqual(validate(decision_body())['reply'], None)

    def test_invalid_decisions_are_rejected(self):
        for bad in (
                {},
                decision_body() | {'control': 'shout'},
                decision_body() | {'extra': 1},
                decision_body('x', ['s:1', 's:1']),
                decision_body('x', ['']),
                decision_body('x', ['k' * 257]),
                decision_body('x', ['a'] * 101),
                decision_body('  '),
                decision_body('x' * 4001),
                decision_body('x', ['s:1']) | {'control': 'pause'},
                decision_body(None, ['s:1']),
                {'control': 'pause', 'control_sentence_keys': [],
                 'reply': '话', 'reply_sentence_keys': []},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidDecision):
                    validate(bad)


def payload(request_id='req-0001', backend='fake', system='角色', input_value=None,
            metadata=None, deadline=30, content_hash=None):
    body = {'schema_version': 1, 'request_id': request_id, 'backend': backend,
            'model': None, 'system': system, 'input': input_value or {'input': 1},
            'output': 'decision-v1', 'deadline_seconds': deadline,
            'content_hash': content_hash or canonical_hash(
                system, input_value or {'input': 1}, 'decision-v1', backend, None)}
    if metadata is not None:
        body['metadata'] = metadata
    return body


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = GatewayStore(Path(self.tmp.name) / 'tasks.sqlite3')
        self.addCleanup(self.store.close)
        self.service = GatewayService(self.store)

    def test_submit_persists_and_represents_queued_task(self):
        status, body = self.service.submit(payload())
        self.assertEqual((status, body['state']), (202, 'queued'))
        self.assertEqual(body['request']['content_hash'],
                         payload()['content_hash'])

    def test_idempotent_resubmit_returns_current_state_without_requeue(self):
        self.service.submit(payload())
        status, body = self.service.submit(payload())
        self.assertEqual((status, body['state']), (200, 'queued'))
        self.store.claim('req-0001')
        status, body = self.service.submit(payload())
        self.assertEqual((status, body['state']), (200, 'running'))

    def test_content_conflict_and_validation_errors(self):
        self.service.submit(payload())
        # Same request_id carrying different (but internally consistent) content.
        with self.assertRaises(RequestError) as caught:
            self.service.submit(payload(input_value={'input': 2}))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, 'content_conflict'))
        with self.assertRaises(RequestError) as caught:
            self.service.submit(payload(content_hash='different'))
        self.assertEqual((caught.exception.status, caught.exception.code), (422, 'invalid_request'))
        for broken in (payload() | {'schema_version': 2},
                       payload() | {'backend': 'codex'},
                       payload(request_id='short'),
                       payload() | {'output': 'other-v1'},
                       payload() | {'deadline_seconds': 0},
                       payload() | {'system': ''},
                       payload(input_value=[1]),
                       payload() | {'content_hash': 'mismatch'}):
            with self.subTest(broken=broken):
                with self.assertRaises(RequestError) as error:
                    self.service.submit(broken)
                self.assertEqual(error.exception.status, 422)

    def test_queue_limit_returns_429(self):
        service = GatewayService(self.store, limits=Limits(queue=1))
        service.submit(payload('req-0001'))
        with self.assertRaises(RequestError) as caught:
            service.submit(payload('req-0002'))
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (429, 'quota_exceeded'))

    def test_failed_task_resubmit_requeues_and_unknown_task_is_404(self):
        self.service.submit(payload())
        self.store.claim('req-0001')
        self.store.finish('req-0001', 'failed', error_code='execution_timeout')
        status, body = self.service.submit(payload())
        self.assertEqual((status, body['state']), (202, 'queued'))
        with self.assertRaises(RequestError):
            self.service.task('absent-id')
        with self.assertRaises(RequestError):
            self.service.cancel('absent-id')


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = GatewayStore(Path(self.tmp.name) / 'tasks.sqlite3')
        self.addCleanup(self.store.close)
        self.service = GatewayService(self.store)

    def wait_state(self, request_id, state, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.store.get(request_id)
            if row and row['state'] == state:
                return row
            time.sleep(0.02)
        self.fail(f'task did not reach {state}')

    def scheduler(self, factory):
        scheduler = ExecutionScheduler(self.store, factory, workers=2)
        scheduler.start()
        self.addCleanup(lambda: scheduler.stop())
        return scheduler

    def test_fake_backend_success_and_representation(self):
        self.scheduler(lambda backend: FakeBackend())
        self.service.submit(payload(metadata={'fake': {'behavior': 'ok',
                                                       'reply': '网关回答', 'keys': ['s:1']}}))
        row = self.wait_state('req-0001', 'succeeded')
        body = self.service.represent(row)
        self.assertEqual(body['result']['decision']['reply'], '网关回答')
        self.assertIn('queued_ms', body['result']['timings'])
        self.assertIn('execution_ms', body['result']['timings'])

    def test_invalid_output_is_recorded_as_output_invalid(self):
        self.scheduler(lambda backend: FakeBackend())
        self.service.submit(payload(metadata={'fake': {'behavior': 'invalid'}}))
        row = self.wait_state('req-0001', 'failed')
        self.assertEqual(row['error_code'], 'output_invalid')

    def test_backend_error_maps_to_failure_code(self):
        self.scheduler(lambda backend: FakeBackend())
        self.service.submit(payload(metadata={'fake': {'behavior': 'error'}}))
        row = self.wait_state('req-0001', 'failed')
        self.assertEqual(row['error_code'], 'backend_unavailable')

    def test_running_cancel_interrupts_execution_and_completion_races_to_cancelled(self):
        sleeping = SleepingBackend()
        scheduler = self.scheduler(lambda backend: sleeping)
        self.service.submit(payload())
        self.assertTrue(sleeping.entered.wait(2))
        self.service.cancel('req-0001')
        scheduler.notify_cancel('req-0001')  # the API layer wires both together
        row = self.wait_state('req-0001', 'cancelled')
        self.assertEqual(row['error_code'], 'cancelled')
        sleeping.release.set()

    def test_global_concurrency_is_bounded_by_worker_count(self):
        sleeping = SleepingBackend()
        scheduler = ExecutionScheduler(self.store, lambda backend: sleeping, workers=1)
        scheduler.start()
        self.addCleanup(lambda: (sleeping.release.set(), scheduler.stop()))
        for index in range(2):
            self.service.submit(payload(f'req-000{index + 1}'))
        self.assertTrue(sleeping.entered.wait(2))
        time.sleep(0.2)
        self.assertEqual(self.store.get('req-0002')['state'], 'queued')
        sleeping.release.set()
        self.wait_state('req-0002', 'succeeded')


if __name__ == '__main__':
    unittest.main()
