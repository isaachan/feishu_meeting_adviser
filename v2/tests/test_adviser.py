from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr
import io
import json

from feishu_meeting_adviser.adviser import AdviserRunner
from feishu_meeting_adviser.__main__ import main
from feishu_meeting_adviser.client import ApiError, ErrorKind
from feishu_meeting_adviser.collector import utc_now
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
        self.sent = []
        self.send_failure = None
        def send(mid,text,message_uuid):
            self.sent.append((mid,text,message_uuid))
            if self.send_failure:
                raise self.send_failure
        self.api.send_message = send
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

    def test_model_reply_is_sent_once_and_silence_sends_nothing(self):
        self.backend.reply=Decision('none',(), '合成模型回复',('s:1',))
        self.commit(self.caption)
        result=self.runner.run()
        self.assertEqual(result['replied'],1)
        self.assertEqual(self.sent[0][:2],(MID,'合成模型回复'))
        self.runner.run()
        self.assertEqual(len(self.sent),1)
        self.backend.reply=Decision('none',(),None,())
        self.commit(replace(self.caption,sentence_key='s:2',text='普通讨论'))
        self.runner.run()
        self.assertEqual(len(self.sent),1)

    def test_unrelated_evidence_is_rejected_without_sending(self):
        self.backend.reply=Decision('none',(),'合成回复',('another-meeting-sentence',))
        self.commit(self.caption)
        result=self.runner.run()
        self.assertEqual(result['model_error'],'model_invalid_evidence')
        self.assertEqual(self.sent,[])

    def test_unknown_send_result_does_not_retry_or_block_new_input(self):
        self.send_failure=ApiError(ErrorKind.NETWORK)
        self.backend.reply=Decision('none',(),'合成回复',('s:1',))
        self.commit(self.caption)
        result=self.runner.run()
        self.assertEqual(result['uncertain_replies'],1)
        self.runner.run()
        self.assertEqual(len(self.sent),1)
        self.backend.reply=Decision('none',(),None,())
        self.commit(replace(self.caption,sentence_key='s:2',text='后续发言'))
        self.runner.run()
        self.assertEqual(len(self.backend.calls),2)
        self.assertEqual(self.source.backlog(MID,'adviser'),0)

    def test_crash_after_post_before_persistence_becomes_uncertain_on_restart(self):
        self.backend.reply=Decision('none',(),'合成回复',('s:1',))
        self.commit(self.caption)
        with patch.object(self.runner.data,'mark_sent',side_effect=StorageError('synthetic after POST crash')):
            with self.assertRaises(StorageError):
                self.runner.run()
        self.runner.close()
        resumed=AdviserRunner(self.source,self.manager,backend=self.backend,clock=lambda:NOW)
        self.addCleanup(resumed.close)
        result=resumed.run()
        self.assertEqual(result['uncertain_replies'],1)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(len(self.backend.calls),1)

    def test_cli_codex_backend_uses_separate_consumer_and_reports_reply(self):
        self.backend.reply=Decision('none',(),'合成 CLI 回复',('s:1',))
        stamp=utc_now()
        self.commit(replace(self.caption,event_time=stamp,received_at=stamp))
        self.path.write_text(self.path.read_text()+'llm:\n  batch_seconds: 0\n  introduction: false\n')
        output=io.StringIO()
        with patch('feishu_meeting_adviser.__main__.FeishuClient',return_value=self.api), \
             patch('feishu_meeting_adviser.adviser.CodexBackend',return_value=self.backend), \
             redirect_stdout(output),redirect_stderr(io.StringIO()):
            result=main(['--config',str(self.path),'consume','--backend','codex'])
        self.assertEqual(result,0)
        self.assertEqual(json.loads(output.getvalue())['replied'],1)
        self.assertEqual(self.source.backlog(MID,'adviser'),0)
        self.assertGreater(self.source.backlog(MID,'fake-qa'),0)

    def test_any_participant_can_pause_and_resume_and_paused_model_keeps_listening(self):
        self.backend.reply=Decision('pause',('s:1',),None,())
        self.commit(replace(self.caption,text='你先别发言，继续听我们讨论'))
        self.runner.run()
        self.assertEqual(self.runner.data.meeting(MID)['participation'],'paused')
        self.backend.reply=Decision('none',(),'暂停期间不应发出的回答',('s:2',))
        self.commit(replace(self.caption,sentence_key='s:2',text='普通讨论中的问题',speaker_id='another-participant'))
        self.runner.run()
        self.assertEqual(self.sent,[])
        self.assertEqual(self.backend.calls[-1]['participation'],'paused')
        self.backend.reply=Decision('resume',('s:3',),'恢复后的合成回答',('s:3',))
        self.commit(replace(self.caption,sentence_key='s:3',text='可以继续发言了',speaker_id='third-participant'))
        self.runner.run()
        self.assertEqual(self.runner.data.meeting(MID)['participation'],'active')
        self.assertEqual(self.sent[0][1],'恢复后的合成回答')
        self.assertEqual(len(self.backend.calls),3)

    def test_cancel_discards_unsent_reply_without_entering_pause(self):
        self.runner.data.db.execute('UPDATE meetings SET cooldown_until=? WHERE meeting_id=?',(NOW+100,MID))
        self.backend.reply=Decision('none',(),'尚未发送的回答',('s:1',))
        self.commit(self.caption)
        self.runner.run()
        self.assertEqual(self.runner.data.reply_statistics(MID)['pending_replies'],1)
        self.backend.reply=Decision('cancel',('s:2',),None,())
        self.commit(replace(self.caption,sentence_key='s:2',text='刚才那个回答不用发了'))
        self.runner.run()
        self.assertEqual(self.runner.data.meeting(MID)['participation'],'active')
        self.assertEqual(self.runner.data.reply_statistics(MID)['pending_replies'],0)
        self.assertEqual(self.sent,[])

    def test_old_control_reference_cannot_change_current_participation(self):
        self.commit(replace(self.caption,event_time='2026-10-12T00:00:00Z',text='历史暂停要求'))
        self.runner.run()
        self.assertEqual(len(self.backend.calls),0)
        self.backend.reply=Decision('pause',('s:1',),None,())
        self.commit(replace(self.caption,sentence_key='s:2',text='当前讨论'))
        result=self.runner.run()
        self.assertEqual(result['model_error'],'model_invalid_evidence')
        self.assertEqual(self.runner.data.meeting(MID)['participation'],'active')

    def test_cancel_cannot_claim_to_retract_an_uncertain_post(self):
        self.send_failure=ApiError(ErrorKind.NETWORK)
        self.backend.reply=Decision('none',(),'不确定的合成发送',('s:1',))
        self.commit(self.caption)
        self.runner.run()
        self.backend.reply=Decision('cancel',('s:2',),None,())
        self.commit(replace(self.caption,sentence_key='s:2',text='取消刚才的回答'))
        result=self.runner.run()
        self.assertEqual(result['uncertain_replies'],1)
        self.assertEqual(len(self.sent),1)

    def test_stopping_consumer_does_not_leave_meeting_or_clear_unfinished_job(self):
        self.backend.release.clear()
        self.commit(self.caption)
        self.runner.step()
        self.assertTrue(self.backend.entered.wait(1))
        self.runner.stop_event.set()
        self.backend.release.set()
        self.runner.worker.join(timeout=2)
        self.assertEqual(self.manager.session()['phase'],'joined')
        self.assertEqual(self.api.leaves,0)
        self.assertIn(MID,self.runner.data.protected_meetings())
