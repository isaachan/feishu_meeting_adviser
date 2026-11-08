import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from feishu_meeting_adviser.adapters.persistence.adviser_store import AdviserStore
from feishu_meeting_adviser.storage import Store

HELPER = Path(__file__).parent / 'helpers' / 'model_process.py'


class ModelProcessTests(unittest.TestCase):
    def stop_peer(self, root):
        path = root / 'peer.pid'
        if path.exists():
            try:
                os.kill(int(path.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass

    def signal_case(self, signum):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mid = None
            proc = subprocess.Popen([sys.executable, '-I', str(HELPER), 'watch', tmp],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 8
                ready = False
                while time.monotonic() < deadline and proc.poll() is None:
                    path = root / 'data' / 'adviser.sqlite3'
                    if path.exists() and (root / 'peer.pid').exists() and (root / 'session-id').exists():
                        data = AdviserStore(path)
                        try:
                            ready = bool(data.db.execute(
                                'SELECT 1 FROM jobs WHERE state=\'running\'').fetchone())
                        finally:
                            data.close()
                        if ready:
                            break
                    time.sleep(0.05)
                self.assertTrue(ready, 'model child not ready')
                mid = (root / 'session-id').read_text()
                proc.send_signal(signum)
                _, errors = proc.communicate(timeout=8)
                self.assertEqual(proc.returncode, 0 if signum == signal.SIGTERM else -signal.SIGKILL, errors)
                self.stop_peer(root)
                source = Store(root / 'data' / 'source.sqlite3')
                try:
                    self.assertEqual(source.backlog(mid, 'adviser'), 0)
                    self.assertIsNotNone(source.caption(mid, 's:1'))
                finally:
                    source.close()
                data = AdviserStore(root / 'data' / 'adviser.sqlite3')
                try:
                    self.assertEqual(data.require_local_session(mid)['phase'], 'recording')
                finally:
                    data.close()
                resumed = subprocess.run([sys.executable, '-I', str(HELPER), 'once', tmp],
                                         capture_output=True, text=True, timeout=8)
                self.assertEqual(resumed.returncode, 0, resumed.stderr)
                self.assertEqual(len((root / 'sent.jsonl').read_text().splitlines()), 1)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=5)
                self.stop_peer(root)
                proc.stdout.close()
                proc.stderr.close()

    def test_sigterm_interrupts_model_and_preserves_input_without_ending_session(self):
        self.signal_case(signal.SIGTERM)

    def test_sigkill_releases_lease_and_resumes_saved_model_job(self):
        self.signal_case(signal.SIGKILL)

    def test_post_publication_gap_is_uncertain_and_never_blindly_republished(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            try:
                result = subprocess.run([sys.executable, '-I', str(HELPER), 'crash-post', tmp],
                                        capture_output=True, text=True, timeout=8)
                self.assertEqual(result.returncode, 99, result.stderr)
                mid = (root / 'session-id').read_text()
                resumed = subprocess.run([sys.executable, '-I', str(HELPER), 'once', tmp],
                                         capture_output=True, text=True, timeout=8)
                self.assertEqual(resumed.returncode, 1, resumed.stderr)
                self.assertEqual(json.loads(resumed.stdout)['uncertain_replies'], 1)
                self.assertEqual(len((root / 'sent.jsonl').read_text().splitlines()), 1)
                data = AdviserStore(root / 'data' / 'adviser.sqlite3')
                try:
                    self.assertEqual(data.list_replies(mid)[0]['state'], 'uncertain')
                    self.assertEqual(data.list_replies(mid)[0]['channel'], 'local')
                finally:
                    data.close()
            finally:
                self.stop_peer(root)
