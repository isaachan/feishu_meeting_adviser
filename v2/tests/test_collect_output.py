from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from feishu_meeting_adviser.__main__ import main
from feishu_meeting_adviser.collector import Collector, PageFormatError
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.lifecycle import SessionManager
from feishu_meeting_adviser.models import ChangeKind
from feishu_meeting_adviser.storage import Store
from test_collector import FakeAPI, event, page
from test_lifecycle import BotAPI, MID


class FlushedOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushed = ''

    def flush(self):
        self.flushed = self.getvalue()


class CollectOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / 'config.yaml'
        self.config_path.write_text('app:\n  id: synthetic\n  secret: synthetic-secret\n')
        self.config_path.chmod(0o600)
        self.config = load_config(self.config_path)
        Store.initialize(self.config.data_directory / 'source.sqlite3')
        self.store = Store(self.config.data_directory / 'source.sqlite3')
        self.addCleanup(self.store.close)
        self.api = BotAPI()
        SessionManager(self.store, self.config, self.api).join('012345678')
        self.caption_event = event(text='合成测试字幕', speaker={'id': 'synthetic-speaker', 'user_name': '合成说话人'},
                                   start_time_ms='1791849600000', end_time_ms='1791849600100')

    def command(self, *args, output=None):
        output = output if output is not None else FlushedOutput()
        with patch('feishu_meeting_adviser.__main__.FeishuClient', return_value=self.api), \
             patch('feishu_meeting_adviser.lifecycle.FeishuClient', return_value=self.api), \
             redirect_stdout(output), redirect_stderr(io.StringIO()):
            result = main(['--config', str(self.config_path), 'collect', '--once', *args])
        self.assertEqual(result, 0)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_default_output_shows_caption_content(self):
        self.api.events = lambda query: page([self.caption_event])
        rows = self.command()
        captions = [row for row in rows if 'text' in row]
        self.assertEqual(len(captions), 1)
        self.assertEqual(captions[0]['text'], '合成测试字幕')
        self.assertEqual(captions[0]['speaker'], '合成说话人')
        self.assertEqual(self.store.caption(MID, 's:s1').text, '合成测试字幕')

    def test_no_text_hides_caption_content(self):
        self.api.events = lambda query: page([self.caption_event])
        hidden = self.command('--no-text')
        self.assertTrue(hidden)
        self.assertTrue(all('text' not in row and 'speaker' not in row for row in hidden))
        self.assertEqual(self.store.caption(MID, 's:s1').text, '合成测试字幕')

    def test_stream_flushes_before_next_page_and_restart_deduplicates_without_ack(self):
        output = FlushedOutput()
        checks = []
        calls = []

        def events(query):
            calls.append(query)
            if len(calls) == 1:
                return page([self.caption_event], True, 'next')
            checks.append('合成测试字幕' in output.flushed)
            saved = Store(self.store.path)
            try:
                checks.append(saved.caption(MID, 's:s1') is not None)
            finally:
                saved.close()
            return page([self.caption_event, event('s2', text='另一句')])

        self.api.events = events
        captions = [row for row in self.command(output=output) if 'text' in row]
        self.assertEqual(checks, [True, True])
        self.assertEqual(len(captions), 2)
        first = captions[0]
        self.assertEqual(first['speaker'], '合成说话人')
        self.assertEqual(first['kind'], 'added')
        self.assertEqual(first['event_time'], '2026-10-13T00:00:00.100Z')
        self.assertEqual(first['start_time'], '1791849600000')
        self.assertEqual(first['end_time'], '1791849600100')
        self.assertEqual(first['timestamp'], self.store.caption(MID, 's:s1').received_at)
        self.assertEqual(captions[1]['speaker'], '未知说话人')
        for hidden in (MID, 'synthetic-speaker', 'synthetic-secret'):
            self.assertNotIn(hidden, output.getvalue())
        self.api.events = lambda query: page([self.caption_event, event('s2', text='另一句')])
        self.assertFalse(any('text' in row for row in self.command()))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM consumers').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)

    def test_conflict_candidate_is_labeled_and_not_accepted_as_revision(self):
        self.api.events = lambda query: page([self.caption_event, event(text='变更候选')])
        captions = [row for row in self.command() if 'text' in row]
        self.assertEqual([row['kind'] for row in captions], ['added', 'conflict'])
        self.assertEqual(captions[1]['text'], '变更候选')
        self.assertEqual(self.store.caption(MID, 's:s1').text, '合成测试字幕')

    def test_invalid_page_does_not_print_uncommitted_caption(self):
        api = FakeAPI([page([event(), {'event_type': 'transcript_received', 'payload': {'text': 1}}])])
        seen = []
        collector = Collector(self.store, MID, api, on_caption=lambda *args: seen.append(args))
        with self.assertRaises(PageFormatError):
            collector.cycle()
        self.assertEqual(seen, [])
        self.assertIsNone(self.store.caption(MID, 's:s1'))

    def test_output_failure_preserves_committed_caption_and_checkpoint(self):
        def broken_output(kind, caption):
            self.assertEqual(kind, ChangeKind.ADDED)
            raise BrokenPipeError('synthetic closed output')

        collector = Collector(self.store, MID, FakeAPI([page([self.caption_event])]), on_caption=broken_output)
        with self.assertRaises(BrokenPipeError):
            collector.cycle()
        self.assertEqual(self.store.caption(MID, 's:s1').text, '合成测试字幕')
        self.assertIsNotNone(self.store.checkpoint(MID)['last_success'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM consumers').fetchone()[0], 0)
