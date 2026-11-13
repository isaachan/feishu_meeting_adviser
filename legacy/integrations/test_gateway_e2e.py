"""M1 end to end: fake-recorder -> advisor -> localhost llm-gateway -> local decision."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from feishu_meeting_adviser.__main__ import main as advisor_main
from feishu_meeting_adviser.adapters.persistence.adviser_store import AdviserStore
from local_meeting_recorder.__main__ import main as recorder_main

GATEWAY = [sys.executable, '-m', 'llm_gateway']
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_listening(process, output, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = output.readline()
        if line.strip():
            try:
                return json.loads(line)
            except ValueError:
                continue
    process.kill()
    raise AssertionError('gateway did not report its listening line')


class GatewayEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def start_gateway(self, database):
        process = subprocess.Popen(
            [*GATEWAY, 'serve', '--host', '127.0.0.1', '--port', '0',
             '--database', str(database), '--workers', '1'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.terminate_gateway, process)
        info = wait_listening(process, process.stdout)
        host, port = info['listening'].rsplit(':', 1)
        self.assertEqual(host, '127.0.0.1')
        return f'http://127.0.0.1:{port}'

    def terminate_gateway(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def write_config(self, url):
        directory = self.root / 'advisor'
        directory.mkdir(exist_ok=True)
        path = directory / 'config.yaml'
        path.write_text('app:\n  id: synthetic\n  secret: synthetic-secret\n'
                        'storage:\n  directory: ../data\n'
                        'llm:\n  backend: remote\n  remote_backend: fake\n'
                        f'  remote_url: {url}\n  batch_seconds: 0\n'
                        '  timeout_seconds: 20\n  introduction: false\n')
        path.chmod(0o600)
        return path

    def script(self):
        path = self.root / 'script.jsonl'
        path.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in (
            {'speaker': '张三', 'text': '这个方案的风险是什么？', 'delay_ms': 0},
            {'speaker': '李四', 'text': '主要是集成的回滚路径。', 'delay_ms': 0},
        )) + '\n', encoding='utf-8')
        return path

    def run_advisor(self, config, *args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = advisor_main(['--config', str(config), *args])
        return code, output.getvalue()

    def test_replayed_script_reaches_a_saved_decision_via_the_gateway(self):
        url = self.start_gateway(self.root / 'gateway' / 'tasks.sqlite3')
        config = self.write_config(url)

        with OPENER.open(f'{url}/healthz', timeout=5) as health:
            self.assertEqual(json.loads(health.read())['status'], 'ok')

        code, output = self.run_advisor(config, 'session-new', '--title', 'M1 会议')
        self.assertEqual(code, 0)
        mid = json.loads(output)['session_id']

        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(recorder_main(
                ['fake', str(self.script()), '--session', mid,
                 '--data-root', str(self.root / 'data'), '--speed', '100']), 0)
        self.assertEqual(json.loads(output.getvalue())['stopped_reason'], 'completed')

        code, output = self.run_advisor(config, 'consume', mid)
        self.assertEqual(code, 0)
        summary = json.loads(output)
        self.assertEqual(summary['decisions'], 1)

        data = AdviserStore(self.root / 'data' / 'adviser.sqlite3')
        try:
            rows = data.list_replies(mid, show_text=True)
            self.assertEqual([(row['channel'], row['state']) for row in rows],
                             [('local', 'sent')])
            self.assertTrue(rows[0]['text'])
        finally:
            data.close()

        # Rerunning consume stays idempotent against the same gateway task.
        code, output = self.run_advisor(config, 'consume', mid)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['decisions'], 0)

        code, _ = self.run_advisor(config, 'session-end', mid)
        self.assertEqual(code, 0)
        data = AdviserStore(self.root / 'data' / 'adviser.sqlite3')
        try:
            self.assertEqual(data.require_local_session(mid)['phase'], 'ended')
        finally:
            data.close()


if __name__ == '__main__':
    unittest.main()
