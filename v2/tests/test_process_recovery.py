"""OS process exits and signals, independent of exceptions in the parent test."""
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import unittest

from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.models import Caption, SourceState, digest
from feishu_meeting_adviser.storage import DurableFakeConsumer, Store

HELPER = Path(__file__).parent / 'helpers' / 'process_worker.py'
MID = '987654321012345678'


class ProcessRecoveryTests(unittest.TestCase):
    def test_real_exit_before_and_after_transaction_commit(self):
        for stage in ('after_captions','after_checkpoint','after_commit'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run([sys.executable,'-I',str(HELPER),'crash-page',tmp,stage],capture_output=True,timeout=10)
                self.assertEqual(result.returncode,97,result.stderr.decode())
                store = Store(Path(tmp)/'data'/'source.sqlite3')
                try:
                    records = store.read('synthetic-meeting','qa')
                    if stage == 'after_commit':
                        self.assertEqual(len(records),1)
                        self.assertTrue(records[0].replayed)
                        self.assertEqual(store.checkpoint('synthetic-meeting')['cursor'],'next')
                    else:
                        self.assertEqual(records,[])
                        self.assertEqual(store.checkpoint('synthetic-meeting')['cursor'],None)
                finally:
                    store.close()

    def test_real_consumer_exit_after_persist_before_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root/'data'/'source.sqlite3')
            store.ensure_meeting('synthetic-meeting',{})
            store.commit_page('synthetic-meeting',[Caption('synthetic-meeting','s:1','synthetic input','now')],{})
            result = subprocess.run([sys.executable,'-I',str(HELPER),'crash-consumer',tmp],capture_output=True,timeout=10)
            self.assertEqual(result.returncode,98,result.stderr.decode())
            consumer = DurableFakeConsumer(root/'data'/'consumer.sqlite3')
            try:
                row = store.read('synthetic-meeting','qa')[0]
                self.assertTrue(row.replayed)
                self.assertFalse(consumer.accept(row))
                store.acknowledge('synthetic-meeting','qa',row.sequence)
                self.assertEqual(len(consumer.context('synthetic-meeting')),1)
                self.assertEqual(store.read('synthetic-meeting','qa'),[])
            finally:
                consumer.close()
                store.close()

    def run_signal(self, signum):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.Popen([sys.executable,'-I',str(HELPER),'source',tmp],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                ready,_,_ = select.select([proc.stdout],[],[],8)
                self.assertTrue(ready,'child did not become ready')
                self.assertEqual(proc.stdout.readline().strip(),'READY')
                proc.send_signal(signum)
                output,err = proc.communicate(timeout=8)
                self.assertEqual(proc.returncode,0 if signum==signal.SIGTERM else -signal.SIGKILL,err)
                self.assertFalse((Path(tmp)/'unexpected-leave').exists())
                store = Store(Path(tmp)/'data'/'source.sqlite3')
                config = load_config(Path(tmp)/'config.yaml')
                try:
                    self.assertEqual(store.session(digest(config.credentials.app_id))['phase'],'joined')
                    if signum == signal.SIGTERM:
                        self.assertEqual(store.current_state(MID).state,SourceState.STOPPED)
                finally:
                    store.close()
                # New process must obtain the dead process's lock, resume and retain one canonical sentence.
                resumed = subprocess.run([sys.executable,'-I',str(HELPER),'source-once',tmp],capture_output=True,text=True,timeout=8)
                self.assertEqual(resumed.returncode,0,resumed.stderr)
                store = Store(Path(tmp)/'data'/'source.sqlite3')
                try:
                    self.assertEqual(store.db.execute('SELECT count(*) FROM captions WHERE meeting_id=?',(MID,)).fetchone()[0],1)
                    self.assertGreater(store.backlog(MID,'qa'),0)
                finally:
                    store.close()
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
                proc.stdout.close()
                proc.stderr.close()

    def test_sigterm_stops_without_leaving_and_releases_lock(self):
        self.run_signal(signal.SIGTERM)

    def test_sigkill_preserves_checkpoint_and_os_releases_lock(self):
        self.run_signal(signal.SIGKILL)
