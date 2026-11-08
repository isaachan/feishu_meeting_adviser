from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from meeting_listener.__main__ import main
from meeting_listener.collector import PageFormatError
from meeting_listener.config import load_config
from meeting_listener.infrastructure.errors import LifecycleError, LockBusy
from meeting_listener.lifecycle import SessionManager
from meeting_listener.local_source import LocalCaptionSource, parse_local_line
from meeting_listener.models import ChangeKind, SourceState, epoch_ns
from meeting_listener.storage import Store
from test_lifecycle import BotAPI, MID


def line(**fields):
    return json.dumps(fields, ensure_ascii=False)


class KickedStream(io.StringIO):
    def __init__(self, text):
        super().__init__(text)
        self.kicked = False

    def readline(self):
        if not self.kicked:
            self.kicked = True
            raise KeyboardInterrupt
        return ''


class ParseLocalLineTests(unittest.TestCase):
    def test_minimal_line_uses_default_speaker_and_fallback_identity(self):
        caption = parse_local_line('{"text": "本地合成字幕"}', 'm1',
                                   received_at='2026-10-27T00:00:00Z', default_speaker='local')
        self.assertEqual(caption.text, '本地合成字幕')
        self.assertEqual(caption.speaker_id, 'local')
        self.assertEqual(caption.speaker_name, 'local')
        self.assertTrue(caption.sentence_key.startswith('h:'))
        self.assertTrue(caption.identity_degraded)
        self.assertIsNone(caption.end_ns)

    def test_id_provides_stable_identity(self):
        caption = parse_local_line(line(text='一句', id='run:3', speaker='本地说话人',
                                        end_time='2026-10-27T01:02:03.4Z'),
                                   'm1', received_at='r', default_speaker='local')
        self.assertEqual(caption.sentence_key, 's:run:3')
        self.assertFalse(caption.identity_degraded)
        self.assertEqual(caption.speaker_name, '本地说话人')
        self.assertIsNotNone(caption.end_ns)

    def test_integer_millisecond_times_parse(self):
        caption = parse_local_line(line(text='一句', id='x', end_time=1791849600100),
                                   'm1', received_at='r', default_speaker='local')
        self.assertEqual(caption.end_ns, 1791849600100 * 1_000_000)

    def test_collect_local_forwarder_line_shape_is_accepted(self):
        end = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        produced = {'id': 'ab12cd34ef56:2', 'speaker': 'local', 'text': '识别文本', 'end_time': end}
        caption = parse_local_line(json.dumps(produced, ensure_ascii=False), 'm1',
                                   received_at='r', default_speaker='local')
        self.assertEqual(caption.sentence_key, 's:ab12cd34ef56:2')
        self.assertFalse(caption.identity_degraded)
        self.assertIsNotNone(caption.end_ns)

    def test_invalid_input_rejected(self):
        base = {'received_at': 'r', 'default_speaker': 'local'}
        for raw in ('not json', '[]', '{"text": ""}', '{"text": 3}',
                    '{"text": "x", "speaker": 4}', '{"text": "x", "id": 9}',
                    '{"text": "x", "end_time": "not-a-time"}',
                    '{"text": "x", "end_time": true}',
                    '{"text": "x", "speaker": "  "}'):
            with self.assertRaises(PageFormatError):
                parse_local_line(raw, 'm1', **base)


class LocalSourceTests(unittest.TestCase):
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
        self.manager = SessionManager(self.store, self.config, BotAPI())
        self.manager.join('012345678')

    def source(self, text='', **kwargs):
        kwargs.setdefault('stream', io.StringIO(text, newline='\n'))
        return LocalCaptionSource(self.store, self.manager, **kwargs)

    def added_deliveries(self):
        return self.store.db.execute(
            'SELECT count(*) FROM deliveries WHERE kind=?', (ChangeKind.ADDED.value,)).fetchone()[0]

    def test_commits_lines_reports_stopped_and_updates_checkpoint(self):
        seen = []
        status = self.source(line(id='a:1', text='第一句', end_time='2026-10-27T00:00:10Z') + '\n'
                             + line(id='a:2', text='第二句', end_time='2026-10-27T00:00:20Z') + '\n\n',
                             on_caption=lambda kind, caption: seen.append((kind.value, caption.text))).run()
        self.assertEqual(status.state, SourceState.STOPPED)
        self.assertEqual(status.reason, 'local_input_closed')
        self.assertEqual(seen, [('added', '第一句'), ('added', '第二句')])
        self.assertEqual(self.store.caption(MID, 's:a:2').speaker_name, 'local')
        self.assertEqual(self.added_deliveries(), 2)
        checkpoint = self.store.checkpoint(MID)
        self.assertIsNotNone(checkpoint['last_success'])
        self.assertEqual(checkpoint['last_caption_end_ns'], epoch_ns('2026-10-27T00:00:20Z'))

    def test_duplicate_identity_same_content_not_redelivered(self):
        text = line(id='a:1', text='同一句', end_time='2026-10-27T00:00:10Z')
        self.source(text + '\n' + text + '\n').run()
        self.assertEqual(self.added_deliveries(), 1)

    def test_revised_content_for_same_id_becomes_conflict_and_keeps_accepted(self):
        self.source(line(id='a:1', text='已接受', end_time='2026-10-27T00:00:10Z') + '\n'
                    + line(id='a:1', text='变更候选', end_time='2026-10-27T00:00:10Z') + '\n').run()
        self.assertEqual(self.store.caption(MID, 's:a:1').text, '已接受')
        self.assertEqual(len(self.store.list_conflicts(MID, show_text=True)), 1)

    def test_invalid_line_publishes_action_required_without_skipping(self):
        with self.assertRaises(LifecycleError):
            self.source(line(id='a:1', text='好句') + '\n' + '{"text": 3}\n').run()
        status = self.store.current_state(MID)
        self.assertEqual(status.state, SourceState.ACTION_REQUIRED)
        self.assertEqual(status.reason, 'local_input_invalid')
        self.assertEqual(self.store.caption(MID, 's:a:1').text, '好句')

    def test_keyboard_interrupt_stops_cleanly(self):
        status = self.source('', stream=KickedStream(line(id='a:1', text='首句') + '\n')).run()
        self.assertEqual((status.state, status.reason), (SourceState.STOPPED, 'local_input_stopped'))

    def test_busy_application_lock_refuses_to_start(self):
        lock = self.manager.application_lock()
        lock.acquire()
        self.addCleanup(lock.release)
        with self.assertRaises(LockBusy):
            self.source(line(id='a:1', text='一句') + '\n').run()


class CollectLocalCLITests(unittest.TestCase):
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
        SessionManager(self.store, self.config, BotAPI()).join('012345678')

    def command(self, *args):
        output = io.StringIO()
        with patch('meeting_listener.__main__.FeishuClient'), \
             patch('meeting_listener.lifecycle.FeishuClient'), \
             redirect_stdout(output), redirect_stderr(io.StringIO()):
            result = main(['--config', str(self.config_path), 'collect', *args])
        return result, [json.loads(text) for text in output.getvalue().splitlines()]

    def test_collect_reads_jsonl_file_with_configured_speaker(self):
        path = Path(self.tmp.name) / 'captions.jsonl'
        path.write_text(line(id='k1', text='本地一句', end_time='2026-10-27T00:00:10Z') + '\n', encoding='utf-8')
        result, rows = self.command('--captions', str(path), '--speaker', 'local-stt')
        self.assertEqual(result, 0)
        captions = [row for row in rows if 'text' in row]
        self.assertEqual(captions[0]['speaker'], 'local-stt')
        self.assertEqual(captions[0]['kind'], 'added')
        self.assertEqual(captions[0]['text'], '本地一句')
        self.assertEqual(self.store.caption(MID, 's:k1').speaker_id, 'local-stt')
        self.assertEqual(rows[-1], {'state': 'stopped', 'reason': 'local_input_closed'})

    def test_collect_once_rejected_for_local_input(self):
        result, rows = self.command('--captions', '-', '--once')
        self.assertEqual(result, 1)
        self.assertEqual(rows, [])

    def test_collect_missing_file_reports_local_error(self):
        result, rows = self.command('--captions', str(Path(self.tmp.name) / 'absent.jsonl'))
        self.assertEqual(result, 1)
        self.assertEqual(rows, [])
