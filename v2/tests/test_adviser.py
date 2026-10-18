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

from feishu_meeting_adviser.adviser import AdviserRunner, AdviserService
from feishu_meeting_adviser.__main__ import main
from feishu_meeting_adviser.client import ApiError, ErrorKind
from feishu_meeting_adviser.collector import utc_now
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.adapters.persistence.factory import StorageFactory
from feishu_meeting_adviser.lifecycle import LifecycleError, SessionManager
from feishu_meeting_adviser.llm import Decision, ModelError
from feishu_meeting_adviser.models import Caption, SourceState, Status
from feishu_meeting_adviser.storage import StorageError, Store
from feishu_meeting_adviser.diagnostics import sanitize
from feishu_meeting_adviser.maintenance import Maintenance
from feishu_meeting_adviser.reply_sender import introduce
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
        StorageFactory(self.config).initialize_source()
        StorageFactory(self.config).initialize_adviser()
        self.source = Store(self.config.data_directory / 'source.sqlite3')
        self.addCleanup(self.source.close)
        self.api = BotAPI()
        self.manager = SessionManager(self.source, self.config, self.api, clock=lambda:NOW-10)
        self.manager.join()
        self.source.ensure_meeting(MID,{})
        self.backend = FakeBackend()
        self.model_inputs = []
        self.model_decisions = []
        self.sent = []
        self.send_failure = None
        def send(mid,text,message_uuid):
            self.sent.append((mid,text,message_uuid))
            if self.send_failure:
                raise self.send_failure
        self.api.send_message = send
        self.runner = AdviserRunner(self.source, self.manager, backend=self.backend, clock=lambda:NOW,
            model_input_observer=self.model_inputs.append,decision_observer=self.model_decisions.append)
        self.addCleanup(self.runner.close)
        self.caption = Caption(MID, 's:1', '合成会议发言', '2026-10-13T00:00:01Z',
                               event_time='2026-10-13T00:00:00Z', speaker_id='synthetic-speaker')

    def test_runner_name_remains_a_service_compatibility_alias(self):
        self.assertIs(AdviserRunner, AdviserService)
        self.assertIsInstance(self.runner, AdviserService)

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

    def test_harness_observers_see_the_actual_model_input_and_decision(self):
        self.backend.reply = Decision('none',(), '合成回答',('s:1',))
        self.commit(self.caption)
        self.runner.step()
        self.finish()
        self.assertEqual(self.model_inputs,self.backend.calls)
        self.assertEqual(self.model_decisions,[self.backend.reply])
        self.assertEqual([item[1] for item in self.sent],['合成回答'])

    def test_reliability_contract_ack_failure_replays_saved_inbox_without_duplicate_model_input(self):
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

    def test_reliability_contract_inbox_failure_never_acknowledges_source(self):
        self.commit(self.caption)
        with patch.object(self.runner.data, 'ingest', side_effect=StorageError('synthetic inbox failure')), \
             patch.object(self.source, 'acknowledge', wraps=self.source.acknowledge) as acknowledge:
            with self.assertRaises(StorageError):
                self.runner.ingest()
        acknowledge.assert_not_called()
        self.assertEqual(self.source.backlog(MID, 'adviser'), 1)

    def test_failed_model_job_stays_recoverable_without_hot_retry(self):
        self.backend.failure=ModelError('model_failed')
        self.commit(self.caption)
        self.runner.run()
        self.assertEqual(self.runner.data.meeting(MID)['error'],'model_failed')
        for _ in range(5):
            self.runner.step()
        self.assertEqual(len(self.backend.calls),1)
        self.assertIn(MID,self.runner.data.protected_meetings())

    def test_failure_diagnostics_include_phase_and_counts_without_tool_or_meeting_text(self):
        records = []
        self.runner.diagnose = records.append
        self.backend.failure = ModelError('model_tools_unavailable')
        self.backend.diagnostic_metadata = lambda: {'model_stage':'mcp_verify', 'mcp_servers':2,
            'mcp_unverified_servers':1, 'mcp_catalog_tools':3, 'model_stage_ms':10,
            'server_name':'PRIVATE SERVER', 'prompt':'PRIVATE MEETING'}
        self.commit(self.caption)
        result = self.runner.run()
        self.assertEqual(result['model_stage'], 'mcp_verify')
        self.assertEqual(records[-1]['error_kind'], 'model_tools_unavailable')
        self.assertEqual(records[-1]['mcp_unverified_servers'], 1)
        self.assertNotIn('PRIVATE', json.dumps(records))
        self.backend.diagnostic_metadata = lambda: {'model_stage':'PRIVATE STAGE', 'reasoning_effort':'PRIVATE EFFORT'}
        self.assertNotIn('model_stage', self.runner.statistics())

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
        records=[]
        self.runner.diagnose=records.append
        self.backend.reply=Decision('none',(),'合成回复',('another-meeting-sentence',))
        self.commit(self.caption)
        result=self.runner.run()
        self.assertEqual(result['model_error'],'model_invalid_evidence')
        self.assertEqual(self.sent,[])
        self.assertEqual(records[-1]['error_kind'],'model_invalid_evidence')
        self.assertIs(records[-1]['decision_saved'],False)

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

    def test_reliability_contract_post_before_persistence_becomes_uncertain_without_resend(self):
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
             patch('feishu_meeting_adviser.adviser.create_backend',return_value=self.backend), \
             redirect_stdout(output),redirect_stderr(io.StringIO()):
            result=main(['--config',str(self.path),'consume','--backend','codex'])
        self.assertEqual(result,0)
        self.assertEqual(json.loads(output.getvalue())['replied'],1)
        self.assertEqual(self.source.backlog(MID,'adviser'),0)
        self.assertGreater(self.source.backlog(MID,'fake-qa'),0)

    def test_cli_opencode_backend_uses_the_same_durable_adviser_consumer(self):
        self.backend.reply=Decision('none',(),'合成 GLM CLI 回复',('s:1',))
        stamp=utc_now()
        self.commit(replace(self.caption,event_time=stamp,received_at=stamp))
        self.path.write_text(self.path.read_text()+'llm:\n  batch_seconds: 0\n  introduction: false\n')
        output=io.StringIO()
        with patch('feishu_meeting_adviser.__main__.FeishuClient',return_value=self.api), \
             patch('feishu_meeting_adviser.adviser.create_backend',return_value=self.backend) as factory, \
             redirect_stdout(output),redirect_stderr(io.StringIO()):
            result=main(['--config',str(self.path),'consume','--backend','opencode'])
        self.assertEqual(result,0)
        self.assertEqual(factory.call_args.args[0].backend,'opencode')
        summary=json.loads(output.getvalue())
        self.assertEqual(summary['backend'],'opencode')
        self.assertEqual(summary['replied'],1)
        self.assertEqual(self.source.backlog(MID,'adviser'),0)
        self.assertEqual(len(self.sent),1)

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

    def test_caption_arriving_after_model_completion_blocks_send_until_rechecked(self):
        self.backend.reply=Decision('none',(),'原有待发回答',('s:1',))
        self.commit(self.caption)
        self.runner.step()
        self.runner.worker.join(timeout=2)
        original=self.runner._finish_job
        def finish_then_new_input(job,decision,elapsed):
            original(job,decision,elapsed)
            self.commit(replace(self.caption,sentence_key='s:2',text='刚才的问题取消了'))
        with patch.object(self.runner,'_finish_job',side_effect=finish_then_new_input):
            self.runner.step()
        self.assertEqual(self.sent,[])
        self.backend.reply=Decision('cancel',('s:2',),None,())
        self.runner.run()
        self.assertEqual(self.sent,[])
        self.assertEqual(self.runner.data.reply_statistics(MID)['pending_replies'],0)

    def test_expired_candidate_is_discarded_without_new_model_call(self):
        self.runner.data.db.execute('UPDATE meetings SET cooldown_until=? WHERE meeting_id=?',(NOW+1000,MID))
        self.backend.reply=Decision('none',(),'将过时的回答',('s:1',))
        self.commit(self.caption)
        self.runner.run()
        self.assertEqual(self.runner.data.reply_statistics(MID)['pending_replies'],1)
        self.runner.clock=lambda:NOW+121
        self.runner.step()
        self.assertEqual(self.runner.data.reply_statistics(MID)['pending_replies'],0)
        self.assertEqual(self.sent,[])
        self.assertEqual(len(self.backend.calls),1)

    def test_unavailable_source_stops_consumer_before_sending(self):
        self.runner.data.db.execute('UPDATE meetings SET cooldown_until=? WHERE meeting_id=?',(NOW+100,MID))
        self.backend.reply=Decision('none',(),'待发回答',('s:1',))
        self.commit(self.caption)
        self.runner.run()
        self.source.publish_status(Status(MID,SourceState.STOPPED,'polling_stopped_bot_remains','2026-10-13T00:00:01Z'))
        with self.assertRaises(LifecycleError):
            self.runner.step()
        self.assertEqual(self.sent,[])

    def test_reliability_contract_manual_retry_rechecks_context_and_preserves_message_uuid(self):
        self.send_failure=ApiError(ErrorKind.NETWORK)
        self.backend.reply=Decision('none',(),'不确定回复',('s:1',))
        self.commit(self.caption)
        self.runner.run()
        message_uuid=self.sent[0][2]
        self.runner.data.resolve_reply(MID,message_uuid,'retry',now=NOW)
        self.send_failure=None
        self.runner.run()
        self.assertEqual(len(self.backend.calls),2)
        self.assertEqual(self.sent[0],self.sent[1])
        self.assertEqual(self.runner.data.reply_statistics(MID)['uncertain_replies'],0)
        self.assertEqual(self.runner.data.list_replies(MID)[0]['state'],'sent')

    def test_introduction_is_saved_once_and_rejoin_uses_new_identity(self):
        self.assertTrue(introduce(self.runner.data,self.manager,clock=lambda:NOW))
        self.assertTrue(introduce(self.runner.data,self.manager,clock=lambda:NOW))
        self.assertEqual(len(self.sent),1)
        self.assertIn('字幕会保存在本机',self.sent[0][1])
        self.assertIn('LLM',self.sent[0][1])
        first_uuid=self.sent[0][2]
        self.runner.data.db.execute('UPDATE meetings SET participation=\'paused\' WHERE meeting_id=?',(MID,))
        self.manager.leave()
        self.manager.join()
        self.assertTrue(introduce(self.runner.data,self.manager,clock=lambda:NOW))
        self.assertNotEqual(first_uuid,self.sent[1][2])
        self.assertEqual(self.runner.data.meeting(MID)['participation'],'active')

    def test_failed_introduction_blocks_model_until_operator_confirms(self):
        self.runner.settings=replace(self.runner.settings,introduction=True)
        self.send_failure=ApiError(ErrorKind.NETWORK)
        self.commit(self.caption)
        with self.assertRaises(LifecycleError):
            self.runner.run()
        self.assertEqual(self.backend.calls,[])
        self.assertEqual(self.source.backlog(MID,'adviser'),1)
        self.runner.data.resolve_reply(MID,self.sent[0][2],'sent',now=NOW)
        self.send_failure=None
        self.runner.run()
        self.assertEqual(len(self.sent),1)
        self.assertEqual(len(self.backend.calls),1)

    def test_configured_join_sends_intro_once_without_starting_model(self):
        self.path.write_text(self.path.read_text()+'llm:\n  backend: codex\n')
        with patch('feishu_meeting_adviser.__main__.FeishuClient',return_value=self.api), \
             redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--config',str(self.path),'join']),0)
            self.assertEqual(main(['--config',str(self.path),'join']),0)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.backend.calls,[])

    def test_automatic_rejoin_refreshes_intro_identity_and_resets_old_pause(self):
        self.assertTrue(introduce(self.runner.data, self.manager, clock=lambda: NOW))
        first_uuid = self.sent[0][2]
        self.runner.data.db.execute('UPDATE meetings SET participation=\'paused\' WHERE meeting_id=?', (MID,))
        self.path.write_text(self.path.read_text() + 'llm:\n  backend: codex\n')
        self.api.event_failure = ApiError(ErrorKind.BOT_ABSENT, code=120004)
        with patch('feishu_meeting_adviser.__main__.FeishuClient', return_value=self.api), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--config', str(self.path), 'join']), 0)
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[1][2], first_uuid)
        self.assertEqual(self.runner.data.meeting(MID)['participation'], 'active')
        self.assertEqual(self.api.leaves, 0)
        self.assertEqual(self.backend.calls, [])

    def test_status_and_model_diagnostics_do_not_expose_content(self):
        self.commit(self.caption)
        self.runner.ingest()
        self.path.write_text(self.path.read_text() + 'llm:\n  backend: opencode\n')
        output=io.StringIO()
        with patch('feishu_meeting_adviser.__main__.FeishuClient',return_value=self.api),redirect_stdout(output):
            self.assertEqual(main(['--config',str(self.path),'status']),0)
        value=json.loads(output.getvalue())
        self.assertEqual(value['adviser']['backend'],'opencode')
        self.assertEqual(value['adviser']['model_state'],'waiting')
        self.assertEqual(value['adviser']['pending_model_inputs'],1)
        self.assertNotIn(self.caption.text,output.getvalue())
        safe=sanitize({'kind':'model','model_ms':12,'decision_saved':True,'backend':'codex',
                       'text':self.caption.text,'prompt':'private prompt','reply':'private answer','model_error':'private server'})
        self.assertEqual(safe,{'kind':'model','backend':'codex','model_ms':12,'decision_saved':True})
        self.assertEqual(ModelError('private server').reason,'unknown_model_error')

    def test_explicit_delete_cleans_model_context_jobs_and_replies(self):
        self.backend.reply=Decision('none',(),'私有合成回复',('s:1',))
        self.commit(self.caption)
        self.runner.run()
        Maintenance(self.source,self.config).delete_meeting(MID)
        for table in ('inputs','context','jobs','outbox','meetings'):
            self.assertEqual(self.runner.data.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0],0)
        self.assertNotIn(self.caption.text.encode(),self.runner.data.path.read_bytes())
        self.assertNotIn('私有合成回复'.encode(),self.runner.data.path.read_bytes())
        with self.assertRaises(LifecycleError):
            self.runner.step()

    def test_pending_model_work_protects_expired_source_until_decided(self):
        self.commit(self.caption)
        self.runner.ingest()
        self.source.db.execute('UPDATE deliveries SET created_ns=1')
        self.source.save_session(self.manager.app_key,self.manager.session()|{'phase':'left','left_at':'2026-10-01T00:00:00Z'})
        maintenance=Maintenance(self.source,self.config,clock=lambda:NOW)
        self.assertEqual(maintenance.prune()['expired_meetings'],0)
        self.assertIsNotNone(self.source.caption(MID,'s:1'))
        job=self.runner.data.prepare_job(MID,self.runner.settings,self.runner.role_version,now=NOW,joined_at=NOW-10)
        self.runner.data.finish_job(job,Decision('none',(),None,()),now=NOW)
        self.assertEqual(maintenance.prune()['expired_meetings'],1)
        self.assertIsNone(self.runner.data.meeting(MID))

    def test_active_consumer_blocks_second_consumer_and_explicit_delete(self):
        from feishu_meeting_adviser.lifecycle import FileLock, LockBusy
        self.commit(self.caption)
        self.runner.ingest()
        with self.runner.lock:
            with self.assertRaises(LockBusy):
                with FileLock(self.config.data_directory / 'locks' / 'adviser.lock'):
                    self.fail('second consumer acquired the lease')
            with self.assertRaises(LockBusy):
                Maintenance(self.source, self.config).delete_meeting(MID)
        self.assertIsNotNone(self.source.caption(MID, 's:1'))
        self.assertIsNotNone(self.runner.data.meeting(MID))

    def test_consumer_waits_for_collector_maintenance_and_continues(self):
        self.backend.reply = Decision('none', (), '合成回复', ('s:1',))
        self.commit(self.caption)
        held = self.runner.maintenance.lock()
        held.acquire()
        timer = threading.Timer(0.15, held.release)
        timer.start()
        try:
            result = self.runner.run()
        finally:
            timer.join()
            held.release()
        self.assertEqual(result['decisions'], 1)
        self.assertEqual(result['replied'], 1)
        self.assertEqual(self.source.backlog(MID, 'adviser'), 0)
        self.assertEqual(self.manager.session()['phase'], 'joined')

    def test_model_completion_survives_contention_before_result_is_saved(self):
        self.backend.reply = Decision('none', (), '合成回复', ('s:1',))
        self.commit(self.caption)
        self.runner.step()
        self.runner.worker.join(timeout=2)
        self.assertFalse(self.runner.worker.is_alive())
        original = self.runner.maintenance.lock
        calls = 0
        timer = None
        held = None
        def competing_lock():
            nonlocal calls, timer, held
            calls += 1
            if calls == 3:  # Ingest, expiry, then saving the completed model result.
                held = original()
                held.acquire()
                timer = threading.Timer(0.15, held.release)
                timer.start()
            return original()
        try:
            with patch.object(self.runner.maintenance, 'lock', side_effect=competing_lock):
                result = self.runner.step()
        finally:
            if timer:
                timer.join()
                held.release()
        self.assertGreaterEqual(calls, 3)
        self.assertEqual(result['decisions'], 1)
        self.assertEqual(result['replied'], 1)
        self.assertTrue(self.runner.results.empty())
        self.runner.run()
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(self.sent), 1)

    def test_stop_interrupts_maintenance_wait_without_leaving_or_losing_input(self):
        self.commit(self.caption)
        timer = threading.Timer(0.1, self.runner.stop_event.set)
        with self.runner.maintenance.lock():
            timer.start()
            started = time.monotonic()
            try:
                self.runner.run(watch=True)
            finally:
                timer.join()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.manager.session()['phase'], 'joined')
        self.assertEqual(self.api.leaves, 0)
        self.assertEqual(self.source.backlog(MID, 'adviser'), 1)
        self.assertEqual(self.backend.calls, [])
        with self.runner.lock:
            pass  # The exclusive consumer lease was released on stop.
