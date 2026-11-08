"""The separately installed modules communicate through durable data, not imports."""
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

from feishu_meeting_adviser.__main__ import main as advisor_main
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.storage import Store as AdvisorStore
from meeting_listener.__main__ import main as listener_main
from meeting_listener.storage import Store as ListenerStore
from feishu_meeting_adviser.adapters.persistence.adviser_store import AdviserStore
from synthetic import BotAPI, MID


ROOT = Path(__file__).resolve().parents[1]


class ModuleSplitTests(unittest.TestCase):
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

    def test_listener_captions_are_consumed_and_acknowledged_by_advisor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listener_config = self.config(root, 'listener')
            advisor_config = self.config(root, 'advisor', 'runtime:\n  poll_seconds: 1\n')
            captions = root / 'captions.jsonl'
            captions.write_text(json.dumps({
                'id': 'split:1', 'speaker': '合成说话人', 'text': '项目截止日期是明天',
            }, ensure_ascii=False) + '\n')
            api = BotAPI()
            sent = []
            api.send_message = lambda *args: sent.append(args)
            with (patch('meeting_listener.__main__.FeishuClient', return_value=api),
                  patch('feishu_meeting_adviser.__main__.FeishuClient', return_value=api),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                source_args = ['--config', str(listener_config)]
                consumer_args = ['--config', str(advisor_config)]
                self.assertEqual(listener_main(source_args + ['join', '012345678']), 0)
                self.assertEqual(listener_main(source_args + ['collect', '--captions', str(captions)]), 0)
                self.assertEqual(advisor_main(consumer_args + ['consume']), 0)
                self.assertEqual(advisor_main(consumer_args + ['consume']), 0)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][:2], (MID, '合成说话人，项目截止日期是今天。'))
            source_path = load_config(advisor_config).data_directory / 'source.sqlite3'
            for store_type in (ListenerStore, AdvisorStore):
                store = store_type(source_path)
                try:
                    self.assertEqual(store.caption(MID, 's:split:1').text, '项目截止日期是明天')
                    self.assertEqual(store.backlog(MID, 'fake-qa'), 0)
                finally:
                    store.close()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(listener_main(source_args + ['delete-meeting-data', '--confirm']), 0)
            store = AdvisorStore(source_path)
            try:
                self.assertTrue(store.is_deleted(MID))
                self.assertIsNone(store.caption(MID, 's:split:1'))
            finally:
                store.close()

    def config(self, root, module, extra=''):
        directory = root / module
        directory.mkdir()
        path = directory / 'config.yaml'
        path.write_text('app:\n  id: synthetic\n  secret: synthetic-secret\n'
                        'storage:\n  directory: ../data\n' + extra)
        path.chmod(0o600)
        return path

    def test_join_introduction_is_visible_to_advisor_with_its_own_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listener_config = self.config(root, 'listener', 'llm:\n  backend: codex\n')
            advisor_config = self.config(root, 'advisor', 'llm:\n  backend: opencode\n')
            api = BotAPI()
            sent = []
            api.send_message = lambda *args: sent.append(args)
            args = ['--config', str(listener_config), 'join', '012345678']
            with (patch('meeting_listener.__main__.FeishuClient', return_value=api),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(listener_main(args), 0)
                self.assertEqual(listener_main(args), 0)
            self.assertEqual(len(sent), 1)
            data = AdviserStore(load_config(advisor_config).data_directory / 'adviser.sqlite3')
            try:
                replies = data.list_replies(MID, show_text=False)
                self.assertTrue(replies)
                self.assertTrue(all(reply['state'] == 'sent' for reply in replies))
            finally:
                data.close()
