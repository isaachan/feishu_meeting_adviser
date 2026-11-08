"""Cross-module guards: module isolation and the advisor local-session flow.

The legacy Feishu handoff between meeting-listener and advisor is disabled; a
fake-recorder integration will be added with plan phase 2.
"""
import ast
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from feishu_meeting_adviser.__main__ import main
from feishu_meeting_adviser.adapters.persistence.adviser_store import AdviserStore
from feishu_meeting_adviser.collector import utc_now
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.llm import Decision
from feishu_meeting_adviser.models import Caption
from feishu_meeting_adviser.storage import Store


ROOT = Path(__file__).resolve().parents[1]


class ModuleIsolationTests(unittest.TestCase):
    def test_each_entrypoint_imports_without_loading_the_other_module(self):
        for package, forbidden in (
            ('meeting_listener', 'feishu_meeting_adviser'),
            ('feishu_meeting_adviser', 'meeting_listener'),
        ):
            with self.subTest(package=package), tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([
                    sys.executable, '-I', '-c',
                    f'import {package}.__main__; import sys; '
                    f'assert not any(n == {forbidden!r} or n.startswith({forbidden + "."!r}) '
                    'for n in sys.modules)',
                ], cwd=cwd, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_files_do_not_import_the_other_module(self):
        for folder, forbidden in (
            ('meeting-listener', ('feishu_meeting_adviser', 'meeting_adviser_harness')),
            ('advisor', ('meeting_listener',)),
        ):
            for path in (ROOT / folder / 'src').rglob('*.py'):
                tree = ast.parse(path.read_text(), filename=str(path))
                imports = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                        imports.append(node.module)
                self.assertFalse(
                    any(name.split('.')[0] in forbidden for name in imports), str(path))


class LocalSessionFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / 'config.yaml'
        self.config_path.write_text('app:\n  id: synthetic\n  secret: synthetic-secret\n'
                                    'storage:\n  directory: ../data\n'
                                    'llm:\n  backend: codex\n  batch_seconds: 0\n')
        self.config_path.chmod(0o600)
        self.config = load_config(self.config_path)

    def run_cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = main(['--config', str(self.config_path), *args])
        return code, output.getvalue()

    def create_session(self, title):
        code, output = self.run_cli('session-new', '--title', title)
        self.assertEqual(code, 0)
        return json.loads(output)['session_id']

    def commit(self, mid, key, text):
        source = Store(self.config.data_directory / 'source.sqlite3')
        try:
            source.ensure_meeting(mid, {})
            stamp = utc_now()
            source.commit_page(mid, [Caption(
                mid, key, text, stamp,
                event_time=stamp, speaker_id='integration-speaker')], {})
        finally:
            source.close()

    def test_two_local_sessions_consume_independently(self):
        first = self.create_session('会议 A')
        second = self.create_session('会议 B')
        self.commit(first, 's:1', '本地集成发言 A')
        self.commit(second, 's:1', '本地集成发言 B')

        replies = []

        class Backend:
            context_overhead_characters = 0

            def decide(self, snapshot, role):
                replies.append(snapshot['captions'][-1]['text'])
                return Decision('none', (), '只回答第一个会话', ('s:1',))

            def diagnostic_metadata(self):
                return {}

            def close(self):
                pass

        for mid in (first, second):
            with patch('feishu_meeting_adviser.adviser.create_backend', return_value=Backend()):
                code, _ = self.run_cli('consume', mid)
                self.assertEqual(code, 0)

        self.assertEqual(replies, ['本地集成发言 A', '本地集成发言 B'])
        data = AdviserStore(self.config.data_directory / 'adviser.sqlite3')
        try:
            for mid, expected in ((first, 1), (second, 1)):
                rows = data.list_replies(mid, show_text=True)
                self.assertEqual([(row['channel'], row['state']) for row in rows],
                                 [('local', 'sent')])
                self.assertEqual(rows[0]['text'], '只回答第一个会话')
                self.assertEqual(data.require_local_session(mid)['phase'], 'recording')
        finally:
            data.close()

        code, output = self.run_cli('session-end', first)
        self.assertEqual((code, json.loads(output)['phase']), (0, 'ended'))
        code, _ = self.run_cli('session-end', second)
        self.assertEqual(code, 0)

    def test_session_list_and_status_reflect_lifecycle(self):
        session = self.create_session('列表检查')
        code, output = self.run_cli('session-list')
        self.assertEqual(code, 0)
        rows = json.loads(output)
        self.assertEqual([row['session_id'] for row in rows], [session])
        code, _ = self.run_cli('session-end', session, '--force')
        self.assertEqual(code, 0)
        code, output = self.run_cli('status', session)
        self.assertEqual(json.loads(output)['session_phase'], 'ended')
