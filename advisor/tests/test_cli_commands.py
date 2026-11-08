from contextlib import redirect_stderr, redirect_stdout
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from feishu_meeting_adviser.__main__ import main
from feishu_meeting_adviser.adapters.persistence.adviser_store import AdviserStore
from feishu_meeting_adviser.adapters.persistence.factory import StorageFactory
from feishu_meeting_adviser.cli.commands import COMMAND_HANDLERS, handle_session_list
from feishu_meeting_adviser.collector import utc_now
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.llm import Decision
from feishu_meeting_adviser.models import Caption
from feishu_meeting_adviser.storage import Store
from test_adviser import FakeBackend


class CLICommandTests(unittest.TestCase):
    def test_every_parser_command_has_an_independent_handler(self):
        self.assertEqual(set(COMMAND_HANDLERS), {
            'session-new', 'session-list', 'session-end', 'status', 'consume',
            'replies', 'resolve-reply',
        })
        self.assertEqual(len(set(COMMAND_HANDLERS.values())), len(COMMAND_HANDLERS))

    def test_session_list_without_database_prints_empty_list(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(handle_session_list(
                SimpleNamespace(data=None), SimpleNamespace()), 0)
        self.assertEqual(json.loads(output.getvalue()), [])

    def test_local_session_cli_flow_publishes_one_local_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / 'config.yaml'
            config_path.write_text('app:\n  id: synthetic\n  secret: synthetic\n'
                                   'llm:\n  backend: codex\n  batch_seconds: 0\n')
            config_path.chmod(0o600)
            config = load_config(config_path)
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--config', str(config_path), 'session-new',
                                       '--title', '合成会议']), 0)
            session = json.loads(output.getvalue())
            self.assertTrue(session['session_id'].startswith('local:'))
            mid = session['session_id']

            source = Store(config.data_directory / 'source.sqlite3')
            stamp = utc_now()
            source.commit_page(mid, [Caption(
                mid, 's:1', '本地 CLI 发言', stamp,
                event_time=stamp, speaker_id='cli-speaker')], {})
            source.close()

            backend = FakeBackend()
            backend.reply = Decision('none', (), 'CLI 本地回答', ('s:1',))
            output = io.StringIO()
            with patch('feishu_meeting_adviser.adviser.create_backend', return_value=backend), \
                 redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--config', str(config_path), 'consume', mid]), 0)
                self.assertEqual(main(['--config', str(config_path), 'consume', mid]), 0)

            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--config', str(config_path), 'replies', mid,
                                       '--show-text']), 0)
            rows = json.loads(output.getvalue())
            self.assertEqual([(row['channel'], row['state'], row['text']) for row in rows],
                             [('local', 'sent', 'CLI 本地回答')])

            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--config', str(config_path), 'status', mid]), 0)
            status = json.loads(output.getvalue())
            self.assertEqual(status['session_phase'], 'recording')
            self.assertEqual(status['adviser']['pending_model_inputs'], 0)

            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--config', str(config_path), 'session-end', mid]), 0)
            self.assertEqual(json.loads(output.getvalue())['phase'], 'ended')

            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(main(['--config', str(config_path), 'consume', mid]), 1)
            self.assertIn('ended', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
