import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from feishu_meeting_adviser.adapters.persistence.adviser_store import AdviserStore
from feishu_meeting_adviser.adapters.persistence.factory import StorageFactory
from feishu_meeting_adviser.adviser import AdviserService
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.infrastructure.errors import LifecycleError, LockBusy
from feishu_meeting_adviser.llm import Decision
from feishu_meeting_adviser.local_sessions import LocalSessionManager
from feishu_meeting_adviser.models import Caption, digest
from feishu_meeting_adviser.storage import Store
from test_adviser import FakeBackend

NOW = 1791849601.0


class LocalAdviserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.yaml'
        self.path.write_text('app:\n  id: synthetic\n  secret: synthetic\n')
        self.path.chmod(0o600)
        self.config = load_config(self.path)
        self.config = replace(self.config, llm=replace(
            self.config.llm, backend='opencode', batch_seconds=0, introduction=False))
        StorageFactory(self.config).initialize_source()
        StorageFactory(self.config).initialize_adviser()
        self.source = Store(self.config.data_directory / 'source.sqlite3')
        self.addCleanup(self.source.close)
        self.data = AdviserStore(self.config.data_directory / 'adviser.sqlite3')
        self.addCleanup(self.data.close)

    def session(self, **kwargs):
        created = self.data.create_local_session(now=NOW - 30, **kwargs)
        self.source.ensure_meeting(created['meeting_id'], {})
        return created

    def service(self, session, backend=None):
        manager = LocalSessionManager(self.data, self.config, session['meeting_id'])
        return AdviserService(
            self.source, manager, backend=backend or FakeBackend(), clock=lambda: NOW)

    def caption(self, mid, key='s:1', text='本地合成发言'):
        return Caption(mid, key, text, '2026-10-13T00:00:01Z',
                       event_time='2026-10-13T00:00:00Z', speaker_id='local-speaker')

    def test_two_local_sessions_run_independently_with_separate_leases(self):
        first, second = self.session(title='会议 A'), self.session(title='会议 B')
        self.source.commit_page(first['meeting_id'], [self.caption(first['meeting_id'])], {})
        self.source.commit_page(second['meeting_id'],
                                [self.caption(second['meeting_id'], text='B 的发言')], {})
        backend_a = FakeBackend()
        service_a = self.service(first, backend=backend_a)
        # Service A holds its lease while service B starts and finishes its own meeting.
        service_a.lock.acquire()
        try:
            with self.assertRaises(LockBusy):
                self.service(first)
            service_b = self.service(second)
            result_b = service_b.run()
            service_b.close()
        finally:
            service_a.lock.release()
        self.assertEqual((result_b['decisions'], result_b['session_phase']), (1, 'recording'))
        self.assertEqual(service_a.data.require_meeting(second['meeting_id'])['generation'], 1)
        result_a = service_a.run()
        service_a.close()
        self.assertEqual((result_a['decisions'], len(backend_a.calls)), (1, 1))
        locks = self.config.data_directory / 'locks'
        self.assertEqual(
            (locks / ('local-' + digest(first['meeting_id']) + '.lock')).name,
            service_a.lock.path.name)

    def test_ended_session_refuses_to_start(self):
        session = self.session()
        self.data.set_local_session_phase(session['meeting_id'], 'ended', now=NOW)
        with self.assertRaises(LifecycleError):
            self.service(session)

    def test_draining_session_ends_after_inputs_and_replies_settle(self):
        session = self.session()
        mid = session['meeting_id']
        self.source.commit_page(mid, [self.caption(mid), self.caption(mid, key='s:2', text='第二个问题')],
                                {})
        self.data.set_local_session_phase(mid, 'draining', now=NOW)
        backend = FakeBackend()
        backend.reply = Decision('none', (), '排空后的最终回答', ('s:1',))
        service = self.service(session, backend=backend)
        result = service.run()
        service.close()
        self.assertEqual(result['session_phase'], 'ended')
        self.assertEqual(result['decisions'], 1)
        self.assertEqual(result['replied'], 1)
        self.assertEqual(self.data.require_local_session(mid)['phase'], 'ended')
        with self.assertRaises(LifecycleError):
            self.service(session)  # ended sessions never restart

    def test_watch_mode_drains_and_exits_with_ended_phase(self):
        session = self.session()
        mid = session['meeting_id']
        self.data.set_local_session_phase(mid, 'draining', now=NOW)
        self.source.commit_page(mid, [self.caption(mid)], {})
        service = self.service(session)
        events = []
        result = service.run(watch=True, notify=events.append)
        service.close()
        self.assertEqual(result['session_phase'], 'ended')
        self.assertTrue(events and events[-1]['session_phase'] == 'ended')
        self.assertEqual(self.data.require_local_session(mid)['phase'], 'ended')

    def test_session_phase_change_is_not_a_session_identity_change(self):
        session = self.session()
        self.source.commit_page(session['meeting_id'], [self.caption(session['meeting_id'])], {})
        service = self.service(session)
        backend = service.executor.backend
        backend.release.clear()
        service.ingest()
        service.step()
        self.assertTrue(backend.entered.wait(1))
        self.data.set_local_session_phase(session['meeting_id'], 'draining', now=NOW)
        backend.release.set()
        service.step()  # draining must not invalidate the running consumer
        service.close()
        self.assertEqual(self.data.require_meeting(session['meeting_id'])['generation'], 1)

    def test_feishu_manager_keeps_full_session_comparison_semantics(self):
        # The legacy path must still detect an external session mutation.
        class Manager:
            def __init__(self, config, session):
                self.config, self._session = config, session

            def session(self, store=None):
                return dict(self._session)

            def saved_meeting(self, *, require_joined=False):
                return dict(self._session)

            @property
            def client(self):
                # Read during construction; calling it would fail this test.
                return None

        session = {'meeting_id': 'm1', 'phase': 'joined', 'join_instance': 'i1',
                   'joined_at': NOW, 'bot_user_id': None}
        self.source.ensure_meeting('m1', {})
        manager = Manager(self.config, session)
        service = AdviserService(self.source, manager, backend=FakeBackend(), clock=lambda: NOW)
        self.addCleanup(service.close)
        service.guard()
        manager._session['phase'] = 'left'
        with self.assertRaises(LifecycleError):
            service.guard()

    def test_local_reply_transport_is_never_called_for_silence(self):
        session = self.session()
        self.source.commit_page(session['meeting_id'], [self.caption(session['meeting_id'])], {})
        service = self.service(session)
        result = service.run()
        service.close()
        self.assertEqual((result['replied'], result['decisions']), (0, 1))

    def test_local_description_becomes_meeting_context_without_feishu(self):
        session = self.session(description='本次会议讨论方案 B 的落地计划。')
        self.source.commit_page(session['meeting_id'], [self.caption(session['meeting_id'])], {})
        backend = FakeBackend()
        service = self.service(session, backend=backend)
        result = service.run()
        service.close()
        self.assertEqual(result['material_state'], 'ready')
        context = backend.calls[0]['meeting_context']
        self.assertEqual(context['identity'], 'local')
        self.assertEqual(context['description'], '本次会议讨论方案 B 的落地计划。')
        self.assertEqual(context['documents'], [])
        # Restarting with the same stored description keeps the material version
        # stable, so no extra model generation is scheduled by reloads.
        meeting = self.data.require_meeting(session['meeting_id'])
        version = meeting['material_version']
        second = self.service(session, backend=FakeBackend())
        second._ensure_materials()
        second.close()
        self.assertEqual(
            self.data.require_meeting(session['meeting_id'])['material_version'], version)

    def test_local_session_without_description_runs_with_materials_disabled(self):
        session = self.session(description='   ')
        self.source.commit_page(session['meeting_id'], [self.caption(session['meeting_id'])], {})
        service = self.service(session, backend=FakeBackend())
        result = service.run()
        service.close()
        self.assertEqual(result['material_state'], 'disabled')
        snapshot_materials = self.data.materials(session['meeting_id'])
        self.assertEqual(snapshot_materials['state'], 'disabled')

    def test_local_service_consumes_recorder_spool_end_to_end(self):
        import json

        session = self.session()
        mid = session['meeting_id']
        stream = f'{mid}:st1'
        spool = self.config.data_directory / 'recorder' / 'spool' / mid / stream
        spool.mkdir(mode=0o700, parents=True)
        # The stream started long after session-new; wall_clock_at anchors the
        # caption into the freshness window of the service clock (NOW).
        spool.joinpath('000001.jsonl').write_text(
            json.dumps({'schema_version': 1, 'session_id': mid, 'stream_id': stream,
                        'sequence': 1, 'event_id': f'{stream}:1', 'kind': 'caption',
                        'utterance_id': 'u-000001', 'revision': 1,
                        'text': 'spool 来的提问', 'speaker_label': '张三',
                        'start_offset_ms': 30000, 'end_offset_ms': 30500,
                        'wall_clock_at': '2026-10-13T00:00:00+00:00'}, ensure_ascii=False)
            + '\n'
            + json.dumps({'schema_version': 1, 'session_id': mid, 'stream_id': stream,
                          'sequence': 2, 'event_id': f'{stream}:2', 'kind': 'stream_end',
                          'last_sequence': 1,
                          'wall_clock_at': '2026-10-13T00:00:01+00:00'}, ensure_ascii=False)
            + '\n', encoding='utf-8')
        backend = FakeBackend()
        backend.reply = Decision('none', (), 'spool 回答', ('u-000001',))
        service = self.service(session, backend=backend)
        result = service.run()
        service.close()
        self.assertEqual(result['decisions'], 1)
        rows = self.data.list_replies(mid, show_text=True)
        self.assertEqual([(row['channel'], row['state'], row['text']) for row in rows],
                         [('local', 'sent', 'spool 回答')])
        ack = (self.config.data_directory / 'recorder' / 'ack' / mid
               / (stream + '.json'))
        self.assertEqual(json.loads(ack.read_text()), {'acked_sequence': 2})

    def test_local_reply_is_published_to_local_channel_without_transport(self):
        session = self.session()
        self.source.commit_page(session['meeting_id'], [self.caption(session['meeting_id'])], {})
        backend = FakeBackend()
        backend.reply = Decision('none', (), '本地保存的回答', ('s:1',))
        service = self.service(session, backend=backend)
        result = service.run()
        service.close()
        self.assertEqual(result['replied'], 1)
        rows = self.data.list_replies(session['meeting_id'], show_text=True)
        self.assertEqual([(row['channel'], row['state'], row['text']) for row in rows],
                         [('local', 'sent', '本地保存的回答')])
        # Re-running never duplicates the locally published answer.
        service = self.service(session, backend=FakeBackend())
        rerun = service.run()
        service.close()
        self.assertEqual((rerun['replied'], rerun['decisions']), (0, 0))
        self.assertEqual(len(self.data.list_replies(session['meeting_id'])), 1)


if __name__ == '__main__':
    unittest.main()
