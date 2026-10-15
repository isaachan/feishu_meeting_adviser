import unittest

from feishu_meeting_adviser.llm import Decision, ModelError
from feishu_meeting_adviser.model_jobs import ModelJobExecutor
from feishu_meeting_adviser.storage import encode


class FakeBackend:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []
        self.closed = False

    def decide(self, snapshot, role):
        self.calls.append((snapshot, role))
        if self.error:
            raise self.error
        return self.result

    def diagnostic_metadata(self):
        return {'model_stage': 'generation'}

    def close(self):
        self.closed = True


class ModelJobExecutorTests(unittest.TestCase):
    def test_executes_one_job_and_returns_structured_result(self):
        decision = Decision('none', (), None, ())
        backend = FakeBackend(result=decision)
        observed = []
        executor = ModelJobExecutor(backend, 'synthetic role', input_observer=observed.append)
        job = {'job_id': 'job-1', 'snapshot': encode({'generation': 1})}
        executor.start(job)
        executor.worker.join(timeout=1)
        result = executor.take_finished()
        self.assertEqual(result.job, job)
        self.assertIs(result.decision, decision)
        self.assertIsNone(result.error)
        self.assertGreaterEqual(result.elapsed_ms, 0)
        self.assertEqual(observed, [{'generation': 1}])
        self.assertEqual(backend.calls, [({'generation': 1}, 'synthetic role')])
        self.assertEqual(executor.diagnostic_metadata(), {'model_stage': 'generation'})
        self.assertTrue(executor.close(timeout=1))
        self.assertTrue(backend.closed)

    def test_sanitizes_known_and_unexpected_backend_failures(self):
        for error, expected in (
                (ModelError('model_timeout'), 'model_timeout'),
                (RuntimeError('private backend detail'), 'model_failed')):
            with self.subTest(expected=expected):
                executor = ModelJobExecutor(FakeBackend(error=error), 'role')
                executor.start({'job_id': 'job-1', 'snapshot': '{}'})
                executor.worker.join(timeout=1)
                self.assertEqual(executor.take_finished().error, expected)

    def test_rejects_overlapping_workers(self):
        executor = ModelJobExecutor(FakeBackend(result=Decision('none', (), None, ())), 'role')
        executor.start({'job_id': 'job-1', 'snapshot': '{}'})
        with self.assertRaisesRegex(RuntimeError, 'already has a worker'):
            executor.start({'job_id': 'job-2', 'snapshot': '{}'})
        executor.worker.join(timeout=1)
        executor.take_finished()


if __name__ == '__main__':
    unittest.main()
