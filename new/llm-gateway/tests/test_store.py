"""llm-gateway: local inference execution per the frozen remote contract."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from llm_gateway.store import GatewayStore, REQUEUEABLE, StorageError


def row(store, request_id):
    return store.get(request_id)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data' / 'tasks.sqlite3'
        self.store = GatewayStore(self.path)
        self.addCleanup(self.store.close)

    def submit(self, request_id='r1', content_hash='h1', backend='fake'):
        return self.store.insert(request_id=request_id, content_hash=content_hash,
                                 backend=backend, model=None, system='role',
                                 input_json='{"input": 1}', output='decision-v1',
                                 deadline_seconds=30, metadata_json='{}')

    def test_submit_persists_queued_before_any_execution(self):
        task = self.submit()
        self.assertEqual((task['state'], task['attempts'], task['cancel_requested']),
                         ('queued', 0, 0))

    def test_lifecycle_running_to_succeeded_keeps_timings(self):
        self.submit()
        claimed = self.store.claim('r1')
        self.assertEqual(claimed['state'], 'running')
        finished = self.store.finish('r1', 'succeeded', result_json='{"control":"none"}',
                                     timings_json='{"execution_ms": 5}')
        self.assertEqual(finished['state'], 'succeeded')
        self.assertIsNotNone(finished['finished_ns'])

    def test_cancel_queued_is_immediate_and_running_needs_worker_finish(self):
        self.submit('a')
        self.assertEqual(self.store.request_cancel('a')['state'], 'cancelled')
        self.submit('b')
        self.store.claim('b')
        task = self.store.request_cancel('b')
        self.assertEqual(task['state'], 'running')
        self.assertTrue(task['cancel_requested'])
        # Completion racing the cancel still lands on cancelled.
        landed = self.store.finish('b', 'succeeded', result_json='{}')
        self.assertEqual(landed['state'], 'cancelled')

    def test_requeue_is_limited_to_retryable_terminal_states(self):
        self.submit()
        self.store.claim('r1')
        self.store.finish('r1', 'failed', error_code='execution_timeout')
        task = self.store.requeue('r1')
        self.assertEqual((task['state'], task['attempts']), ('queued', 1))
        self.store.claim('r1')
        self.store.finish('r1', 'succeeded', result_json='{}')
        with self.assertRaises(StorageError):
            self.store.requeue('r1')
        self.assertEqual(REQUEUEABLE, ('failed', 'cancelled', 'expired'))

    def test_next_queued_pops_oldest_and_skips_cancelled(self):
        self.submit('new')
        self.submit('old')
        self.store.request_cancel('new')
        self.assertEqual(self.store.next_queued()['request_id'], 'old')
        self.assertEqual(self.store.next_queued(), None)

    def test_startup_recovery_fails_interrupted_tasks(self):
        self.submit('q')
        self.submit('r')
        self.store.claim('r')
        self.assertEqual(self.store.recover_interrupted(), 2)
        self.assertEqual({self.store.get(r)['state'] for r in ('q', 'r')}, {'failed'})
        self.assertEqual(self.store.get('q')['error_code'], 'backend_unavailable')

    def test_expire_clears_details_of_old_terminal_tasks_only(self):
        self.submit('done')
        self.store.claim('done')
        self.store.finish('done', 'succeeded', result_json='{"r": 1}')
        self.submit('fresh')
        old = time.time() - 48 * 3600
        self.store._db.execute('UPDATE tasks SET finished_ns=? WHERE request_id=?',
                               (int(old * 1e9), 'done'))
        self.assertEqual(self.store.expire(retention_hours=24), 1)
        expired = self.store.get('done')
        self.assertEqual(expired['state'], 'expired')
        self.assertIsNone(expired['result'])
        self.assertIsNone(expired['system'])
        self.assertEqual(self.store.get('fresh')['state'], 'queued')
        # An expired task can be requeued under the same request identity.
        self.assertEqual(self.store.requeue('done')['state'], 'queued')

    def test_readonly_open_requires_existing_current_database(self):
        with self.assertRaises(StorageError):
            GatewayStore.open_readonly(self.path.with_name('absent.sqlite3'))
        readonly = GatewayStore.open_readonly(self.path)
        self.addCleanup(readonly.close)
        self.assertEqual(readonly.get('missing'), None)
        with self.assertRaises(StorageError):
            readonly.insert(request_id='x', content_hash='h', backend='fake', model=None,
                            system='s', input_json='{}', output='decision-v1',
                            deadline_seconds=1, metadata_json='{}')


if __name__ == '__main__':
    unittest.main()
