from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
import sys

from feishu_meeting_adviser.adviser import AdviserRunner
from feishu_meeting_adviser.client import ApiError, ErrorKind
from feishu_meeting_adviser.collector import utc_now
from feishu_meeting_adviser.config import ConfigError, MeetingContextSettings, load_config
from feishu_meeting_adviser.lifecycle import SessionManager
from feishu_meeting_adviser.llm import Decision
from feishu_meeting_adviser.maintenance import Maintenance
from feishu_meeting_adviser.materials import ContentPreviews, MaterialError, load_materials
from feishu_meeting_adviser.models import Caption, digest
from feishu_meeting_adviser.opencode_backend import OpenCodeBackend
from feishu_meeting_adviser.storage import Store, encode
from test_adviser import FakeBackend
from test_adviser_store import NOW
from test_lifecycle import BotAPI, MID


WIKI = 'https://synthetic.feishu.cn/wiki/WikiToken123'


class MaterialsAPI:
    def __init__(self):
        self.calls = []
        self.timeout = 37
        self.request_timeouts = []
        self.text = '合成材料记载：阶段完成日期是十月二十日。'
        self.failure = None
        self.object_type = 'docx'
        self.event_value = {'event_id':'event_0','summary':'合成日程',
            'description':'参考资料：合成文档', 'description_rich':f'参考资料：[合成文档]({WIKI})',
            'vchat':{'meeting_url':'https://vc.feishu.cn/j/012345678'},
            'start_time':{'timestamp':str(NOW-3600)}, 'end_time':{'timestamp':str(NOW+3600)},
            'attachments':[]}

    def event(self, calendar_id, event_id, *, timeout=None):
        self.request_timeouts.append(timeout)
        self.calls.append(('event',calendar_id,event_id))
        if self.failure:
            raise self.failure
        return self.event_value

    def node(self, token, *, timeout=None):
        self.request_timeouts.append(timeout)
        self.calls.append(('node',token))
        return {'obj_type':self.object_type,'obj_token':'ActualDoc123','title':'合成文档'}

    def document(self, token, *, timeout=None):
        self.request_timeouts.append(timeout)
        self.calls.append(('metadata',token))
        return {'title':'合成文档','revision_id':1}

    def document_text(self, token, *, timeout=None):
        self.request_timeouts.append(timeout)
        self.calls.append(('body',token))
        return self.text


class MaterialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'config.yaml'
        self.path.write_text('app:\n  id: synthetic\n  secret: synthetic\nmeeting:\n  meeting_no: "012345678"\n')
        self.path.chmod(0o600)
        self.config = load_config(self.path)
        role_path = self.path.parent/'synthetic-role.md'
        role_path.write_text('合成测试角色：只根据当前有效字幕及背景资料决定是否回答。')
        self.config = replace(self.config,
            llm=replace(self.config.llm,backend='codex',batch_seconds=0,introduction=False,role_file=role_path),
            meeting_context=MeetingContextSettings('calendar','event_0','application'))
        self.source = Store(self.config.data_directory/'source.sqlite3')
        self.addCleanup(self.source.close)
        self.api, self.materials = BotAPI(), MaterialsAPI()
        self.sent, self.previews = [], []
        self.api.send_message = lambda mid,text,message_uuid: self.sent.append((mid,text,message_uuid))
        self.manager = SessionManager(self.source,self.config,self.api,clock=lambda:NOW-10)
        self.session = self.manager.join()
        self.source.ensure_meeting(MID,{})
        self.runners = []
        self.addCleanup(lambda:[runner.close() for runner in self.runners])

    def runner(self, *, now=NOW):
        backend = FakeBackend()
        runner = AdviserRunner(self.source,self.manager,backend=backend,clock=lambda:now,
            material_client=self.materials,preview_display=self.previews.append)
        self.runners.append(runner)
        return runner, backend

    def commit_question(self, text='根据参考资料，阶段完成日期是什么？'):
        stamp = utc_now(lambda:NOW)
        caption = Caption(MID,'s:1',text,stamp,event_time=stamp,speaker_id='synthetic-person')
        self.source.commit_page(MID,[caption],{})

    def finish(self, runner):
        if runner.worker:
            runner.worker.join(timeout=2)
        runner.step()

    def test_actual_rich_description_shape_loads_full_body_independently_of_preview_and_read_time(self):
        self.materials.text = '合成背景。'*600
        result = load_materials(self.config,self.session,client=self.materials,emit=self.previews.append,clock=lambda:NOW)
        later = load_materials(self.config,self.session,client=self.materials,clock=lambda:NOW+100)
        self.assertEqual(result['context_version'],later['context_version'])
        self.assertEqual(len(result['documents'][0]['text']),3000)
        preview = next(item for item in self.previews if item['kind']=='document')
        self.assertEqual(len(preview['preview']),2000)
        self.assertEqual(preview['characters'],3000)
        self.assertTrue(preview['truncated'])
        self.assertEqual(result['documents'][0]['source_fields'],['description_rich'])
        self.assertEqual(result['description'],self.materials.event_value['description_rich'])
        self.assertEqual(result['description_source'],'description_rich')
        references = next(item for item in self.previews if item['kind']=='reference_list')
        self.assertEqual(references['count'],1)
        self.assertEqual(references['items'][0]['source'],WIKI)
        self.assertIn(('body','ActualDoc123'),self.materials.calls)

    def test_request_deadline_does_not_mutate_shared_client_timeout(self):
        load_materials(self.config, self.session, client=self.materials)
        self.assertEqual(self.materials.timeout, 37)
        self.assertTrue(self.materials.request_timeouts)
        self.assertTrue(all(0 < timeout <= self.config.timeout_seconds
                            for timeout in self.materials.request_timeouts))

    def test_explicit_binding_rejects_other_meeting_old_instance_and_recurring_series(self):
        for change in ({'vchat':{'meeting_url':'https://vc.feishu.cn/j/999999999'}},
                       {'end_time':{'timestamp':str(NOW-100000)},'start_time':{'timestamp':str(NOW-200000)}},
                       {'recurrence':'FREQ=WEEKLY'}, {'event_id':'other_0'}):
            original = self.materials.event_value
            self.materials.event_value = original | change
            with self.assertRaises(MaterialError):
                load_materials(self.config,self.session,client=self.materials)
            self.assertFalse(any(call[0]=='body' for call in self.materials.calls))
            self.materials.event_value = original

    def test_non_docx_and_uploads_are_explicitly_skipped_without_recursive_document_fetch(self):
        self.materials.object_type = 'sheet'
        self.materials.event_value['attachments'] = [{'name':'合成.pdf','file_token':'NotADocxToken'}]
        result = load_materials(self.config,self.session,client=self.materials,emit=self.previews.append)
        self.assertEqual(result['state'],'partial')
        self.assertEqual(result['documents'][0]['status'],'unsupported')
        self.assertFalse(any(call[0]=='body' for call in self.materials.calls))
        self.assertEqual(result['attachment_count'],1)
        self.assertTrue(any(item['kind']=='attachment_list' and item['count']==1 for item in self.previews))

    def test_source_count_and_body_budgets_are_explicit_and_materials_do_not_follow_nested_links(self):
        self.materials.event_value['description_rich'] = ' '.join(f'https://synthetic.feishu.cn/docx/Doc{n}' for n in range(4))
        self.materials.text = ('较长背景'*500)+' https://synthetic.feishu.cn/wiki/UnrelatedLink'
        config = replace(self.config,meeting_context=replace(self.config.meeting_context,max_documents=2,context_characters=1000))
        result = load_materials(config,self.session,client=self.materials)
        self.assertEqual(result['sources_omitted'],2)
        self.assertEqual(len(result['documents']),2)
        self.assertEqual(sum(call[0]=='body' for call in self.materials.calls),2)
        self.assertLessEqual(sum(len(item['text']) for item in result['documents'])+len(result['description']),1000)
        self.assertTrue(result['documents'][0]['truncated'])

    def test_materials_only_or_material_change_cannot_trigger_historical_reply_or_clear_pause(self):
        runner, backend = self.runner()
        runner.run()
        self.assertEqual(backend.calls,[])
        runner.data.db.execute('UPDATE meetings SET participation=\'paused\' WHERE meeting_id=?',(MID,))
        first = runner.data.meeting(MID)['material_version']
        self.materials.text += ' 材料后续修正。'
        restarted, second_backend = self.runner(now=NOW+1)
        restarted.run()
        self.assertEqual(second_backend.calls,[])
        self.assertNotEqual(first,restarted.data.meeting(MID)['material_version'])
        self.assertEqual(restarted.data.meeting(MID)['participation'],'paused')
        self.assertEqual(self.sent,[])

    def test_model_receives_background_and_new_subtitle_evidence_with_a_single_startup_read(self):
        self.commit_question()
        runner, backend = self.runner()
        backend.reply = Decision('none',(),'根据合成文档，日期为十月二十日。',('s:1',))
        runner.run()
        self.assertEqual(len(backend.calls),1)
        snapshot = backend.calls[0]
        self.assertEqual(snapshot['meeting_context']['documents'][0]['text'],self.materials.text)
        self.assertEqual(snapshot['fresh_input_keys'],['s:1'])
        self.assertEqual(len(self.sent),1)
        for _ in range(3):
            runner.step()
        self.assertEqual(sum(call[0]=='event' for call in self.materials.calls),1)
        self.assertEqual(sum(call[0]=='body' for call in self.materials.calls),1)
        self.assertLessEqual(len(encode(snapshot))+len(runner.role),runner.settings.context_characters)

    def test_load_failure_preserves_source_inputs_and_cannot_invoke_model_or_send(self):
        self.commit_question()
        self.materials.failure = ApiError(ErrorKind.DENIED,http_status=403,code=1770001)
        runner, backend = self.runner()
        with self.assertRaises(MaterialError):
            runner.run()
        self.assertEqual(backend.calls,[])
        self.assertEqual(self.sent,[])
        self.assertEqual(self.source.backlog(MID,'adviser'),1)
        self.assertEqual(runner.data.meeting(MID)['material_state'],'unavailable')
        self.assertIsNone(runner.data.next_reply(MID,now=NOW))
        self.assertTrue(any(item.get('status')=='unavailable' for item in self.previews))

    def test_opencode_http_transport_receives_materials_and_sends_one_durable_reply(self):
        self.commit_question()
        self.config=replace(self.config,llm=replace(self.config.llm,backend='opencode',timeout_seconds=5))
        self.manager.config=self.config
        capture=Path(self.temp.name)/'http-capture.jsonl'
        peer=Path(__file__).parent/'helpers/fake_opencode_server.py'
        runtime=self.config.data_directory/'model_runtime'/'opencode'/digest(MID)
        backend=OpenCodeBackend(self.config.llm,runtime,command=[sys.executable,str(peer),'reply',str(capture)])
        runner=AdviserRunner(self.source,self.manager,backend=backend,clock=lambda:NOW,
            material_client=self.materials,preview_display=self.previews.append)
        self.runners.append(runner)
        self.assertEqual(runner.run()['replied'],1)
        records=[json.loads(line) for line in capture.read_text().splitlines()]
        payload=next(r['payload'] for r in records if r['path'].endswith('prompt_async'))
        snapshot=json.loads(payload['parts'][0]['text'])
        self.assertEqual(snapshot['meeting_context']['documents'][0]['text'],self.materials.text)
        self.assertEqual(snapshot['fresh_input_keys'],['s:1'])
        runner.run()
        self.assertEqual(len(self.sent),1)
        runner.close();self.runners.remove(runner)
        abandoned=runtime/'run-abandoned';abandoned.mkdir(mode=0o700)
        (abandoned/'synthetic.db').write_text('synthetic material body')
        Maintenance(self.source,self.config).delete_meeting(MID)
        self.assertFalse(abandoned.exists())

    def test_revoked_document_access_does_not_reuse_cached_materials_after_restart(self):
        runner, _ = self.runner()
        runner.run()
        self.assertTrue(runner.data.materials(MID)['documents'])
        self.commit_question()
        def rejected(token, *, timeout=None):
            raise ApiError(ErrorKind.DENIED,http_status=403,code=1770001)
        self.materials.document_text = rejected
        restarted, backend = self.runner(now=NOW+1)
        with self.assertRaises(MaterialError):
            restarted.run()
        self.assertEqual(restarted.data.materials(MID)['documents'],[])
        self.assertEqual(backend.calls,[])
        self.assertEqual(self.sent,[])
        self.assertTrue(any(item.get('kind')=='document' and item.get('status')=='failed'
                            and item.get('source')==WIKI and item.get('code')==1770001 for item in self.previews))

    def test_loading_deadline_and_capacity_failure_stop_before_model_or_source_ack(self):
        now = [0.0]
        original_event = self.materials.event
        def delayed(*args, **kwargs):
            now[0] = 61
            return original_event(*args, **kwargs)
        self.materials.event = delayed
        with self.assertRaises(MaterialError):
            load_materials(self.config,self.session,client=self.materials,monotonic=lambda:now[0])
        self.assertFalse(any(call[0]=='body' for call in self.materials.calls))
        self.materials.event = original_event
        self.commit_question()
        runner, backend = self.runner()
        runner.data.max_payload_bytes = 1
        from feishu_meeting_adviser.storage import CapacityError
        with self.assertRaises(CapacityError):
            runner.run()
        self.assertEqual(backend.calls,[])
        self.assertEqual(self.source.backlog(MID,'adviser'),1)

    def test_preview_retention_preserves_newer_records_and_diagnostics_have_no_content(self):
        import os
        from feishu_meeting_adviser.diagnostics import sanitize
        root = self.config.data_directory/'content_previews'
        old = ContentPreviews(root,'old-meeting',display=lambda record:None)
        new = ContentPreviews(root,'new-meeting',display=lambda record:None)
        record = {'kind':'document','status':'ok','preview':'私有材料正文','characters':6,'truncated':False}
        old.write(record)
        new.write(record)
        os.utime(old.path,ns=(1,1))
        ContentPreviews.clean(root,cutoff_ns=2)
        self.assertFalse(old.path.exists())
        self.assertTrue(new.path.exists())
        self.assertNotIn('私有材料正文',json.dumps(sanitize(record),ensure_ascii=False))

    def test_material_change_invalidates_inflight_control_and_reply(self):
        self.commit_question()
        runner, _ = self.runner()
        runner._ensure_materials()
        runner.ingest()
        job = runner.data.prepare_job(MID,runner.settings,runner.role_version,now=NOW,joined_at=NOW-10)
        updated = runner.data.materials(MID) | {'context_version':'synthetic-new-version'}
        runner.data.set_materials(MID,updated,now=NOW+1)
        self.assertFalse(runner.data.finish_job(job,Decision('pause',('s:1',),None,()),
            now=NOW+1,settings=runner.settings,joined_at=NOW-10))
        self.assertEqual(runner.data.meeting(MID)['participation'],'active')
        self.assertIsNone(runner.data.next_reply(MID,now=NOW+1))

    def test_changed_material_rechecks_pending_candidate_and_preserves_uuid(self):
        self.commit_question()
        runner, backend = self.runner()
        backend.reply = Decision('none',(),'合成回答。',('s:1',))
        runner.step()
        runner.data.db.execute('UPDATE meetings SET cooldown_until=? WHERE meeting_id=?',(NOW+1000,MID))
        self.finish(runner)
        uuid = runner.data.db.execute('SELECT uuid FROM outbox WHERE kind=\'reply\'').fetchone()[0]
        self.materials.text += ' 更新。'
        restarted, second_backend = self.runner(now=NOW+1)
        second_backend.reply = backend.reply
        restarted._ensure_materials()
        self.assertIsNone(restarted.data.next_reply(MID,now=NOW+1))
        restarted.run()
        self.assertEqual(len(second_backend.calls),1)
        self.assertEqual(second_backend.calls[0]['pending_reply']['uuid'],uuid)
        restarted.data.db.execute('UPDATE meetings SET cooldown_until=0 WHERE meeting_id=?',(MID,))
        restarted.step()
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0][2],uuid)

    def test_full_budget_keeps_new_question_before_large_material_body(self):
        self.commit_question('当前合成问题：'+'q'*1000)
        self.materials.text = 'b'*10000
        runner, backend = self.runner()
        runner.settings = replace(runner.settings,context_characters=3500)
        runner.run()
        self.assertEqual(len(backend.calls),1)
        snapshot = backend.calls[0]
        self.assertEqual(snapshot['fresh_input_keys'],['s:1'])
        self.assertTrue(snapshot['meeting_context']['documents'][0]['truncated'])
        self.assertLessEqual(len(encode(snapshot))+len(runner.role),3500)

    def test_private_preview_rotation_and_meeting_deletion_cover_material_cache(self):
        runner, _ = self.runner()
        runner.run()
        root = self.config.data_directory/'content_previews'
        logger = ContentPreviews(root,MID,max_bytes=10000,backups=1,display=lambda record:None)
        other = ContentPreviews(root,'other-meeting',display=lambda record:None)
        for _ in range(5):
            logger.write({'kind':'document','status':'ok','characters':3000,'preview':'中'*2000,'truncated':True})
        other.write({'kind':'description','preview':'其他会议资料','characters':6,'truncated':False})
        self.assertLessEqual(sum(path.stat().st_size for path in logger.directory.glob('materials.jsonl*')),20000)
        self.assertEqual(logger.path.stat().st_mode & 0o777,0o600)
        Maintenance(self.source,self.config,clock=lambda:NOW).delete_meeting(MID)
        self.assertIsNone(runner.data.materials(MID))
        self.assertEqual(list(logger.directory.glob('materials.jsonl*')),[])
        self.assertTrue(other.path.exists())

    def test_config_rejects_partial_or_invalid_bindings_and_legacy_is_disabled(self):
        self.assertFalse(load_config(self.path).meeting_context.enabled)
        original = self.path.read_text()
        for suffix in ('meeting_context:\n  calendar_id: calendar\n',
                       'meeting_context:\n  calendar_id: calendar\n  event_id: event_0\n  identity: unknown\n',
                       'meeting_context:\n  calendar_id: ../outside\n  event_id: event_0\n'):
            self.path.write_text(original+suffix)
            with self.assertRaises(ConfigError):
                load_config(self.path)


if __name__ == '__main__':
    unittest.main()
