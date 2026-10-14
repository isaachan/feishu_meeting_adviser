from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from feishu_meeting_adviser.adviser import AdviserRunner
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.lifecycle import SessionManager
from feishu_meeting_adviser.llm import Decision, ModelError
from feishu_meeting_adviser.models import Caption
from feishu_meeting_adviser.storage import StorageError, Store
from test_adviser_store import NOW
from test_lifecycle import BotAPI, MID


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.reply = Decision('none', (), None, ())
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.failure = None
    def decide(self, snapshot, role):
        self.calls.append(snapshot)
        self.entered.set()
        self.release.wait(2)
        if self.failure:
            raise self.failure
        return self.reply
    def close(self):
        self.release.set()


class AdviserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.yaml'
        self.path.write_text('app:\n  id: synthetic\n  secret: synthetic\nmeeting:\n  meeting_no: "012345678"\n')
        self.path.chmod(0o600)
        self.config = load_config(self.path)
        self.config = replace(self.config, llm=replace(self.config.llm, backend='codex', batch_seconds=0, introduction=False))
        self.source = Store(self.config.data_directory / 'source.sqlite3')
        self.addCleanup(self.source.close)
        self.api = BotAPI()
        self.manager = SessionManager(self.source, self.config, self.api, clock=lambda:NOW-10)
        self.manager.join()
        self.source.ensure_meeting(MID,{})
        self.backend = FakeBackend()
        self.runner = AdviserRunner(self.source, self.manager, backend=self.backend, clock=lambda:NOW)
        self.addCleanup(self.runner.close)
        self.caption = Caption(MID, 's:1', '合成会议发言', '2026-10-13T00:00:01Z',
                               event_time='2026-10-13T00:00:00Z', speaker_id='synthetic-speaker')

    def commit(self, *captions):
        self.source.commit_page(MID,list(captions),{})

    def finish(self):
        deadline=time.monotonic()+2
        while self.runner.worker and self.runner.worker.is_alive() and time.monotonic()<deadline:
            self.runner.worker.join(timeout=0.05)
        return self.runner.step()

    def test_input_persists_and_advances_while_model_is_busy(self):
        self.backend.release.clear()
        self.commit(self.caption)
        self.runner.step()
        self.assertTrue(self.backend.entered.wait(1))
        self.commit(replace(self.caption,sentence_key='s:2',text='模型运行期间的新发言'))
        self.runner.step()
        self.assertEqual(self.runner.data.meeting(MID)['generation'],2)
        self.assertEqual(self.source.backlog(MID,'adviser'),0)
        self.assertEqual(len(self.backend.calls),1)
        self.backend.release.set()
        self.finish()
        self.finish()
        self.assertEqual(len(self.backend.calls),2)

    def test_ack_failure_replays_saved_inbox_without_duplicate_model_input(self):
        self.commit(self.caption)
        with patch.object(self.source,'acknowledge',side_effect=StorageError('synthetic lost ack')):
            with self.assertRaises(StorageError):
                self.runner.ingest()
        self.assertEqual(self.runner.data.meeting(MID)['generation'],1)
        self.assertEqual(self.source.backlog(MID,'adviser'),1)
        self.runner.run()
        self.assertEqual(len(self.backend.calls),1)
        self.assertEqual(self.runner.data.meeting(MID)['generation'],1)
        self.assertEqual(self.source.backlog(MID,'adviser'),0)

    def test_failed_model_job_stays_recoverable_without_hot_retry(self):
        self.backend.failure=ModelError('model_failed')
        self.commit(self.caption)
        self.runner.run()
        self.assertEqual(self.runner.data.meeting(MID)['error'],'model_failed')
        for _ in range(5):
            self.runner.step()
        self.assertEqual(len(self.backend.calls),1)
        self.assertIn(MID,self.runner.data.protected_meetings())
