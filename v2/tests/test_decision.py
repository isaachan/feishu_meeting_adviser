from dataclasses import asdict
import unittest

from feishu_meeting_adviser.decision import DecisionApplier
from feishu_meeting_adviser.llm import Decision, ModelError
from feishu_meeting_adviser.models import Caption, JobState
from feishu_meeting_adviser.storage import encode


class FakeJobs:
    def __init__(self):
        self.state = JobState.RUNNING
        self.completed = None

    def decision_state(self, job_id):
        return self.state

    def abandon(self, job_id):
        self.state = JobState.ABANDONED

    def complete(self, job_id, *, result, snapshot):
        self.state = JobState.COMPLETE
        self.completed = (result, snapshot)


class FakeMeetings:
    def __init__(self):
        self.row = {
            'participation': 'active',
            'material_version': None,
            'material_state': 'disabled',
        }
        self.completed = None

    def require(self, meeting_id):
        return self.row

    def complete_decision(self, meeting_id, *, generation, participation):
        self.completed = (generation, participation.value)


class FakeOutbox:
    def __init__(self):
        self.cancelled = []
        self.saved = []

    def cancel_pending(self, meeting_id):
        self.cancelled.append(meeting_id)

    def save_reply(self, **reply):
        self.saved.append(reply)


class FakeUnitOfWork:
    def __init__(self):
        self.jobs = FakeJobs()
        self.meetings = FakeMeetings()
        self.outbox = FakeOutbox()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeDecisionData:
    def __init__(self, caption):
        self.row = {'body': encode(asdict(caption)), 'suppressed': 0}
        self.uow = FakeUnitOfWork()

    def context_row(self, meeting_id, sentence_key):
        return self.row

    def unit_of_work(self, *, fault=None):
        return self.uow


class DecisionApplierTests(unittest.TestCase):
    def setUp(self):
        self.caption = Caption(
            'm1', 's:1', 'synthetic evidence', '2026-10-13T00:00:01Z',
            event_time='2026-10-13T00:00:01Z', speaker_id='participant')
        self.snapshot = {
            'fresh_input_keys': ['s:1'],
            'reply_allowed_keys': ['s:1'],
            'evidence': {'s:1': self.caption.fingerprint},
        }
        self.job = {
            'job_id': 'job-1',
            'meeting_id': 'm1',
            'role_version': 'role-v1',
            'generation': 1,
            'context_version': None,
            'snapshot': encode(self.snapshot),
        }

    def test_fake_repositories_receive_one_complete_reply_decision(self):
        data = FakeDecisionData(self.caption)
        applied = DecisionApplier(data).apply(
            self.job, Decision('none', (), 'synthetic answer', ('s:1',)), now=1)
        self.assertTrue(applied)
        self.assertEqual(data.uow.outbox.cancelled, ['m1'])
        self.assertEqual(len(data.uow.outbox.saved), 1)
        self.assertEqual(data.uow.outbox.saved[0]['content'], 'synthetic answer')
        self.assertEqual(data.uow.jobs.state, JobState.COMPLETE)
        self.assertEqual(data.uow.meetings.completed, (1, 'active'))

        second = FakeDecisionData(self.caption)
        DecisionApplier(second).apply(
            self.job, Decision('none', (), 'synthetic answer', ('s:1',)), now=2)
        self.assertEqual(
            second.uow.outbox.saved[0]['message_uuid'],
            data.uow.outbox.saved[0]['message_uuid'],
        )

    def test_invalid_declared_evidence_never_enters_unit_of_work(self):
        data = FakeDecisionData(self.caption)
        with self.assertRaisesRegex(ModelError, 'model_invalid_evidence'):
            DecisionApplier(data).apply(
                self.job, Decision('pause', ('not-in-snapshot',), None, ()), now=1)
        self.assertEqual(data.uow.jobs.state, JobState.RUNNING)
        self.assertEqual(data.uow.outbox.cancelled, [])

    def test_revised_evidence_completes_without_saving_stale_reply(self):
        revised = Caption(
            'm1', 's:1', 'revised evidence', '2026-10-13T00:00:02Z',
            event_time='2026-10-13T00:00:02Z', speaker_id='participant')
        data = FakeDecisionData(revised)
        DecisionApplier(data).apply(
            self.job, Decision('none', (), 'stale answer', ('s:1',)), now=1)
        self.assertEqual(data.uow.outbox.saved, [])
        self.assertEqual(data.uow.jobs.state, JobState.COMPLETE)


if __name__ == '__main__':
    unittest.main()
