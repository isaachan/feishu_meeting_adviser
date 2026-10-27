from dataclasses import replace
from pathlib import Path
import json
import tempfile
import time
import unittest

from feishu_meeting_adviser.collector import Collector
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.diagnostics import Diagnostics, sanitize
from feishu_meeting_adviser.maintenance import Maintenance
from feishu_meeting_adviser.models import Caption, ChangeKind, digest
from feishu_meeting_adviser.storage import CapacityError, DurableFakeConsumer, StorageError, Store
from test_collector import FakeAPI, event, page


class DiagnosticsRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / 'config.yaml'
        self.config_path.write_text('app:\n  id: synthetic\n  secret: private-secret\n')
        self.config_path.chmod(0o600)
        self.config = load_config(self.config_path)
        Store.initialize(self.config.data_directory / 'source.sqlite3')
        self.store = Store(self.config.data_directory / 'source.sqlite3')
        self.addCleanup(self.store.close)
        self.store.ensure_meeting('m1', {'cursor':None})
        self.caption = Caption('m1','s:1','SYNTHETIC PRIVATE CONTENT','2026-10-01T00:00:00Z')

    def test_rotation_is_bounded_and_whitelist_excludes_content(self):
        log = Diagnostics(self.config.data_directory / 'diagnostics', max_bytes=500, backups=2)
        for _ in range(50):
            log.write({'kind':'page','event_count':0,'has_more':False,'text':self.caption.text,
                       'token':'private-token','payload':{'text':self.caption.text},
                       'request_ids':{'request_id':'sensitive-trace-id'},'scan_mode':{'bad':'shape'}})
        files = list(log.directory.glob('diagnostics.jsonl*'))
        self.assertLessEqual(len(files),3)
        for path in files:
            self.assertLessEqual(path.stat().st_size,500)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            content = path.read_text()
            for secret in (self.caption.text,'private-token','sensitive-trace-id'):
                self.assertNotIn(secret, content)
            for line in content.splitlines():
                json.loads(line)

    def test_first_observation_delays_exclude_duplicates_and_keep_skew(self):
        now = 1_800_000_000
        from datetime import datetime, timezone
        stamp = datetime.fromtimestamp(now-15,timezone.utc).isoformat()
        finish = datetime.fromtimestamp(now-20,timezone.utc).isoformat()
        api = FakeAPI([page([event(timestamp=stamp,end_time_ms=finish)]),page([event(timestamp=stamp,end_time_ms=finish)])])
        collector = Collector(self.store,'metrics-meeting',api,clock=lambda:now)
        first = collector.cycle()[0]
        second = collector.cycle()[0]
        self.assertEqual(first['new_event_receive_seconds']['median'],15)
        self.assertEqual(first['new_end_receive_seconds']['median'],20)
        self.assertEqual(second['new_event_receive_seconds']['count'],0)
        self.assertEqual(second['latest_caption_end_age_seconds'],20)

    def test_retention_protects_unacknowledged_and_recovery_identity(self):
        self.store.commit_page('m1',[self.caption,replace(self.caption,sentence_key='s:2')],{'cursor':'pending'})
        self.store.db.execute('UPDATE deliveries SET created_ns=1')
        self.assertEqual(self.store.prune(time.time_ns(),at='2026-10-13T00:00:00Z')['pruned_deliveries'],0)
        rows = self.store.read('m1','qa')
        self.store.acknowledge('m1','qa',rows[0].sequence)
        result = self.store.prune(time.time_ns(),at='2026-10-13T00:00:00Z')
        self.assertEqual(result['pruned_deliveries'],1)
        self.assertEqual(self.store.checkpoint('m1')['cursor'],'pending')
        self.assertIsNotNone(self.store.caption('m1','s:1'))
        self.store.commit_page('m1',[self.caption],{'cursor':'pending'})
        pending = self.store.read('m1','qa')
        self.assertEqual(len([r for r in pending if r.caption]),1)
        self.assertEqual(pending[0].caption.sentence_key,'s:2')

    def test_slowest_consumer_and_unresolved_conflict_block_pruning(self):
        self.store.commit_page('m1',[self.caption],{})
        fast = self.store.read('m1','fast')
        self.store.read('m1','slow')
        self.store.acknowledge('m1','fast',fast[-1].sequence)
        self.store.db.execute('UPDATE deliveries SET created_ns=1')
        self.assertEqual(self.store.prune(time.time_ns(),at='now')['pruned_deliveries'],0)
        self.store.commit_page('m1',[replace(self.caption,text='unordered correction')],{})
        slow = self.store.read('m1','slow')
        fast = self.store.read('m1','fast')
        self.store.acknowledge('m1','slow',slow[-1].sequence)
        self.store.acknowledge('m1','fast',fast[-1].sequence)
        self.store.db.execute('UPDATE deliveries SET created_ns=1')
        self.store.prune(time.time_ns(),at='now')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM conflicts WHERE resolved=0').fetchone()[0],1)
        self.assertEqual(len([r for r in self.store.db.execute("SELECT * FROM deliveries WHERE kind='conflict'")]),1)

    def test_explicit_delete_cleans_consumer_copy_even_at_content_capacity(self):
        self.store.commit_page('m1',[self.caption],{})
        self.store.save_session('app',{'phase':'joined','meeting_id':'m1','meeting_no':'012345678'})
        row = self.store.read('m1','qa')[0]
        path = self.config.data_directory / 'fake_consumers' / 'consumer.sqlite3'
        consumer = DurableFakeConsumer(path)
        consumer.accept(row)
        consumer.close()
        self.store.max_payload_bytes = 1
        Maintenance(self.store,self.config).delete_meeting('m1')
        self.assertIsNone(self.store.caption('m1','s:1'))
        self.assertEqual(self.store.session('app')['phase'],'joined')
        consumer = DurableFakeConsumer(path)
        self.assertEqual(consumer.context('m1'),[])
        self.assertEqual(consumer.db.execute('SELECT count(*) FROM accepted').fetchone()[0],0)
        consumer.close()
        self.assertNotIn(self.caption.text.encode(),path.read_bytes())
        self.assertNotIn(self.caption.text.encode(),self.store.path.read_bytes())
        with self.assertRaises(StorageError):
            self.store.ensure_meeting('m1',{})

    def test_expired_inactive_acknowledged_meeting_removes_managed_copy(self):
        self.store.commit_page('m1',[self.caption],{})
        row = self.store.read('m1','qa')[0]
        path = self.config.data_directory / 'fake_consumers' / 'consumer.sqlite3'
        consumer = DurableFakeConsumer(path)
        consumer.accept(row)
        consumer.close()
        self.store.acknowledge('m1','qa',row.sequence)
        self.store.save_session('app',{'phase':'left','meeting_id':'m1','left_at':'2026-10-01T00:00:00Z'})
        cutoff = __import__('datetime').datetime(2026,10,13,tzinfo=__import__('datetime').timezone.utc).timestamp()
        result = Maintenance(self.store,self.config,clock=lambda:cutoff).prune()
        self.assertEqual(result['expired_meetings'],1)
        consumer = DurableFakeConsumer(path)
        self.assertEqual(consumer.context('m1'),[])
        consumer.close()

    def test_physical_database_limit_rolls_back_without_advancing_cursor(self):
        Store.initialize(self.config.data_directory / 'small.sqlite3', max_database_bytes=262144)
        small = Store(self.config.data_directory / 'small.sqlite3', max_database_bytes=262144)
        try:
            small.ensure_meeting('m',{'cursor':None})
            huge = Caption('m','s:1','x'*300000,'2026-10-13T00:00:00Z')
            with self.assertRaises(CapacityError):
                small.commit_page('m',[huge],{'cursor':'new'})
            self.assertEqual(small.checkpoint('m')['cursor'],None)
            self.assertIsNone(small.caption('m','s:1'))
        finally:
            small.close()

    def test_sensitive_database_and_diagnostic_symlinks_are_rejected(self):
        link = self.config.data_directory / 'link.sqlite3'
        link.symlink_to(self.store.path)
        with self.assertRaises(StorageError):
            Store(link)
        log = Diagnostics(self.config.data_directory / 'diagnostics')
        log.path.symlink_to(self.store.path)
        with self.assertRaises(StorageError):
            log.write({'kind':'page'})

    def test_old_meeting_expiration_survives_switch_to_a_new_session(self):
        self.store.commit_page('m1',[self.caption],{})
        row = self.store.read('m1','qa')[0]
        self.store.acknowledge('m1','qa',row.sequence)
        self.store.save_session('app',{'phase':'left','meeting_id':'m1','left_at':'2026-10-01T00:00:00Z'})
        self.store.save_session('app',{'phase':'joined','meeting_id':'new-meeting'})
        from datetime import datetime, timezone
        cutoff = datetime(2026,10,13,tzinfo=timezone.utc).timestamp()
        self.assertEqual(Maintenance(self.store,self.config,clock=lambda:cutoff).prune()['expired_meetings'],1)
        self.assertEqual(self.store.session('app')['meeting_id'],'new-meeting')

    def test_rotation_respects_lowered_limits(self):
        directory = self.config.data_directory / 'diagnostics'
        old = Diagnostics(directory,max_bytes=2000,backups=3)
        for _ in range(100):
            old.write({'kind':'page','event_count':0})
        lower = Diagnostics(directory,max_bytes=500,backups=1)
        lower.write({'kind':'page','event_count':0})
        paths = list(directory.glob('diagnostics.jsonl*'))
        self.assertLessEqual(len(paths),2)
        self.assertTrue(all(p.stat().st_size<=500 for p in paths))
