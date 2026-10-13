"""Fault sequences and downstream contracts across the complete local pipeline."""
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import threading
import unittest

from feishu_meeting_adviser.client import ApiError, ErrorKind
from feishu_meeting_adviser.collector import Collector, PageFormatError, ScanSettings, parse_page
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.lifecycle import MeetingSource, SessionManager
from feishu_meeting_adviser.models import Caption, ChangeKind, IngestMode, SourceState, Status
from feishu_meeting_adviser.recovery import RecoveryLoop
from feishu_meeting_adviser.storage import DurableFakeConsumer, StorageError, Store
from test_collector import FakeAPI, event, page
from test_lifecycle import BotAPI, MID


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'data'/'source.sqlite3')
        self.addCleanup(lambda:self.store.close())

    def test_fixture_formats_are_synthetic_and_chat_is_excluded(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/transcript_shapes.json').read_text())
        parsed = parse_page(fixture['response'],'fixture-meeting',received_at='2026-10-13T00:00:20Z',mode=IngestMode.INITIAL)
        self.assertEqual(len(parsed.captions),2)
        self.assertTrue(all(c.end_ns is not None for c in parsed.captions))
        self.assertEqual(parsed.captions[0].event_ns % 1_000_000_000,123456789)
        self.assertNotIn('Synthetic chat',[c.text for c in parsed.captions])

    def test_bad_fields_do_not_partially_commit_a_page_or_leak_content(self):
        malformed = [
            {'data':{'events':'bad','has_more':False}},
            page([None]), page([{'event_type':'transcript_received','payload':'invalid JSON private'}]),
            page([event(language=1)]), page([event(start_time_ms={'private':'value'})]),
            page([{'event_type':'transcript_received','payload':{'transcript_received_items':{}}}]),
            page([],True,123), {'data':{'events':[],'has_more':0}},
            page([{'event_type':'meeting.activity','payload':{'meeting':{'id':'other'},'activity_event_type':'transcript_received','text':'private'}}]),
        ]
        for index,bad in enumerate(malformed):
            with self.subTest(index=index):
                mid = 'malformed-'+str(index)
                collector = Collector(self.store,mid,FakeAPI([bad]),clock=lambda:1_800_000_000)
                original = self.store.checkpoint(mid)
                with self.assertRaises(PageFormatError) as caught:
                    collector.cycle()
                self.assertNotIn('private',str(caught.exception))
                self.assertEqual(self.store.checkpoint(mid),original)
                self.assertEqual(self.store.backlog(mid,'qa'),0)

    def test_partial_scan_network_failure_and_resume_deliver_all_once_to_context(self):
        batches = [[event(str(i),text=f'synthetic-{i}') for i in range(start,start+100)] for start in (0,100,200)]
        api = FakeAPI([page(batches[0],True,'A'), ApiError(ErrorKind.NETWORK),
                       page(batches[0]+batches[1],True,'B'), page(batches[2]), page(batches[0])])
        class Outcomes:
            def events(self, query):
                result = api.events(query)
                if isinstance(result,Exception):
                    raise result
                return result
        collector = Collector(self.store,'m',Outcomes(),settings=ScanSettings(max_pages_per_cycle=2),clock=lambda:1_800_000_000)
        loop = RecoveryLoop(collector,jitter=lambda:0)
        loop.step()
        self.assertEqual(self.store.checkpoint('m')['cursor'],'A')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM captions WHERE meeting_id="m"').fetchone()[0],100)
        loop.step()
        loop.step()
        consumer = DurableFakeConsumer(self.root/'data'/'consumer.sqlite3')
        self.addCleanup(consumer.close)
        while rows := self.store.read('m','qa',limit=100):
            for row in rows:
                consumer.accept(row)
            self.store.acknowledge('m','qa',rows[-1].sequence)
        self.assertEqual(len(consumer.context('m')),300)
        self.assertEqual(self.store.backlog('m','qa'),0)
        self.assertEqual(self.store.current_state('m').state,SourceState.INCOMPLETE)
        self.assertEqual(api.queries[1]['page_token'],'A')
        self.assertEqual(api.queries[2]['page_token'],'A')

    def test_more_than_spike_cache_limit_deduplicates_after_restart(self):
        self.store.ensure_meeting('large',{})
        rows = [Caption('large',f's:{i}',f'synthetic {i}','2026-10-13T00:00:00Z') for i in range(5100)]
        self.store.commit_page('large',rows,{})
        path = self.store.path
        self.store.close()
        self.store = Store(path)
        counts = self.store.commit_page('large',[rows[0],rows[-1]],{})
        self.assertEqual(counts['duplicates'],2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM deliveries WHERE meeting_id="large"').fetchone()[0],5100)

    def test_consumed_conflict_resolution_replaces_existing_context(self):
        self.store.ensure_meeting('m',{})
        original = Caption('m','s:1','original','2026-10-13T00:00:00Z')
        corrected = replace(original,text='manual correction')
        self.store.commit_page('m',[original,corrected],{})
        self.store.publish_status(Status('m',SourceState.INCOMPLETE,'recovery_completeness_unverified','now',1,2))
        consumer = DurableFakeConsumer(self.root/'data'/'consumer.sqlite3')
        self.addCleanup(consumer.close)
        rows = self.store.read('m','qa')
        for row in rows:
            consumer.accept(row)
        self.store.acknowledge('m','qa',rows[-1].sequence)
        conflict = next(row for row in rows if row.kind==ChangeKind.CONFLICT)
        self.store.resolve_conflict('m',conflict.delivery_id,at='now')
        for row in self.store.read('m','qa'):
            consumer.accept(row)
        self.assertEqual([c.text for c in consumer.context('m')],['manual correction'])
        self.assertEqual(self.store.current_state('m').reason,'recovery_completeness_unverified')

    def source(self, diagnose=None):
        path = self.root/'config.yaml'
        path.write_text('app:\n  id: synthetic\n  secret: synthetic-secret\nmeeting:\n  meeting_no: "012345678"\n')
        path.chmod(0o600)
        config = load_config(path)
        api = BotAPI()
        manager = SessionManager(self.store,config,api)
        manager.join()
        return MeetingSource(self.store,manager,api,diagnose=diagnose)

    def test_diagnostic_failure_preserves_already_committed_page(self):
        def diagnose(record):
            if record['kind']=='page':
                raise StorageError('simulated diagnostic disk failure')
        source = self.source(diagnose)
        self.addCleanup(source.stop)
        source.start(max_successful_cycles=1)
        source.thread.join(timeout=5)
        self.assertFalse(source.running)
        self.assertEqual(source.state().state,SourceState.ACTION_REQUIRED)
        self.assertIsNotNone(self.store.caption(MID,'s:s1'))
        self.assertIsNotNone(self.store.checkpoint(MID)['last_success'])

    def test_downstream_can_read_and_ack_from_another_thread(self):
        source = self.source()
        self.addCleanup(source.stop)
        source.start(max_successful_cycles=1)
        source.thread.join(timeout=5)
        result = []
        def consumer_thread():
            try:
                rows = source.read('qa')
                source.acknowledge('qa',rows[-1].sequence)
                result.append(source.state().state)
            except Exception as exc:
                result.append(type(exc).__name__)
        thread = threading.Thread(target=consumer_thread)
        thread.start()
        thread.join(timeout=5)
        self.assertEqual(result,[SourceState.STOPPED])
        self.assertEqual(source.read('qa'),[])

    def test_corrupt_stored_record_fails_closed_without_body_in_error(self):
        self.store.ensure_meeting('m',{'cursor':'preserved'})
        self.store.commit_page('m',[Caption('m','s:1','synthetic','now')],{'cursor':'preserved'})
        body = json.loads(self.store.db.execute('SELECT body FROM deliveries WHERE meeting_id="m"').fetchone()[0])
        body['mode'] = 'private-content-value'
        self.store.db.execute('UPDATE deliveries SET body=? WHERE meeting_id="m"',(json.dumps(body),))
        with self.assertRaises(StorageError) as caught:
            self.store.read('m','qa')
        self.assertNotIn('private-content-value',str(caught.exception))
        self.assertEqual(self.store.checkpoint('m')['cursor'],'preserved')
        self.assertEqual(self.store.backlog('m','qa'),1)

    def test_invalid_checkpoint_is_not_silently_reset(self):
        self.store.ensure_meeting('m',{})
        self.store.db.execute('UPDATE meetings SET checkpoint=? WHERE meeting_id="m"',('private-content-value',))
        with self.assertRaises(StorageError) as caught:
            self.store.checkpoint('m')
        self.assertNotIn('private-content-value',str(caught.exception))
        self.assertEqual(self.store.db.execute('SELECT checkpoint FROM meetings WHERE meeting_id="m"').fetchone()[0],'private-content-value')

    def test_confirmed_bot_absence_can_reset_a_stale_previously_joined_session(self):
        source = self.source()
        manager = source.manager
        api = manager.client
        api.failure = ApiError(ErrorKind.PARAMETER,code=121105)  # Saved meeting no longer exists.
        with self.assertRaises(ApiError):
            manager.leave()
        with self.assertRaises(Exception):
            manager.reset_uncertain(confirmed_bot_left=False)
        manager.reset_uncertain(confirmed_bot_left=True)
        self.assertEqual(manager.session()['phase'],'left')
        api.failure = None
        manager.join()
        self.assertEqual(manager.session()['phase'],'joined')
