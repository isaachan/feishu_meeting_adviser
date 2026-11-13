import json
from pathlib import Path
import tempfile
import unittest

from feishu_meeting_adviser.adapters.persistence.factory import StorageFactory
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.models import epoch_ns
from feishu_meeting_adviser.spool_ingest import SpoolIngester
from feishu_meeting_adviser.storage import StorageError, Store

NOW = 1791849601.0
SESSION = 'local:ab12'


def caption_event(sequence, utterance, text='合成台词', speaker='张三',
                  start_ms=None, end_ms=None):
    return {
        'schema_version': 1, 'session_id': SESSION,
        'stream_id': SESSION + ':st1', 'sequence': sequence,
        'event_id': f'{SESSION}:st1:{sequence}', 'kind': 'caption',
        'utterance_id': utterance, 'revision': 1, 'text': text,
        'speaker_label': speaker,
        'start_offset_ms': 30000 if start_ms is None else start_ms,
        'end_offset_ms': 30500 if end_ms is None else end_ms,
        'wall_clock_at': '2026-11-08T10:00:00+00:00',
    }


def stream_end_event(sequence, last_sequence):
    return {'schema_version': 1, 'session_id': SESSION, 'stream_id': SESSION + ':st1',
            'sequence': sequence, 'event_id': f'{SESSION}:st1:{sequence}',
            'kind': 'stream_end', 'last_sequence': last_sequence,
            'wall_clock_at': '2026-11-08T10:00:01+00:00'}


class SpoolIngesterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config_path = self.root / 'config.yaml'
        config_path.write_text('app:\n  id: synthetic\n  secret: synthetic\n')
        config_path.chmod(0o600)
        self.config = load_config(config_path)
        StorageFactory(self.config).initialize_source()
        self.source = Store(self.config.data_directory / 'source.sqlite3')
        self.addCleanup(self.source.close)
        self.ingester = SpoolIngester(self.source, self.config.data_directory, SESSION,
                                      started_at=NOW - 30)
        self.stream_dir = (self.config.data_directory / 'recorder' / 'spool'
                           / SESSION / (SESSION + ':st1'))

    def write_segment(self, name, events):
        self.stream_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.stream_dir / name
        path.write_text(''.join(json.dumps(event, ensure_ascii=False) + '\n'
                                for event in events), encoding='utf-8')

    def ack(self):
        path = (self.config.data_directory / 'recorder' / 'ack' / SESSION
                / (SESSION + ':st1.json'))
        return json.loads(path.read_text()) if path.exists() else None

    def test_poll_persists_captions_then_advances_ack(self):
        self.write_segment('000001.jsonl',
                           [caption_event(1, 'u-1'), caption_event(2, 'u-2', text='第二句'),
                            stream_end_event(3, 2)])
        self.assertEqual(self.ingester.poll(), 2)
        self.assertEqual(self.ack(), {'acked_sequence': 3})
        first = self.source.caption(SESSION, 'u-1')
        self.assertEqual((first.text, first.speaker_name, first.received_at),
                         ('合成台词', '张三', '2026-11-08T10:00:00+00:00'))
        # Offsets are anchored to the stream's wall clock, not the session start.
        self.assertEqual(first.end_ns, epoch_ns('2026-11-08T10:00:00+00:00'))
        self.assertEqual(first.start_time,
                         epoch_ns('2026-11-08T10:00:00+00:00') // 1_000_000 - 500)
        self.assertEqual(self.source.backlog(SESSION, 'adviser'), 2)
        self.assertTrue(self.ingester.drained())

    def test_repoll_is_idempotent(self):
        self.write_segment('000001.jsonl',
                           [caption_event(1, 'u-1'), stream_end_event(2, 1)])
        self.assertEqual(self.ingester.poll(), 1)
        self.assertEqual(self.ingester.poll(), 0)
        self.assertEqual(self.source.backlog(SESSION, 'adviser'), 1)

    def test_gap_waits_for_missing_sequence_without_skipping(self):
        self.write_segment('000001.jsonl', [caption_event(1, 'u-1')])
        self.write_segment('000003.jsonl', [caption_event(3, 'u-3'),
                                            stream_end_event(4, 3)])
        self.assertEqual(self.ingester.poll(), 1)
        self.assertEqual(self.ack(), {'acked_sequence': 1})
        self.assertFalse(self.ingester.drained())
        self.write_segment('000002.jsonl', [caption_event(2, 'u-2')])
        self.assertEqual(self.ingester.poll(), 2)
        self.assertEqual(self.ack(), {'acked_sequence': 4})
        self.assertTrue(self.ingester.drained())

    def test_missing_stream_end_keeps_stream_undrained(self):
        self.write_segment('000001.jsonl', [caption_event(1, 'u-1')])
        self.ingester.poll()
        self.assertFalse(self.ingester.drained())

    def test_foreign_session_and_damaged_input_fail_closed(self):
        foreign = caption_event(1, 'u-1') | {'session_id': 'local:other'}
        self.write_segment('000001.jsonl', [foreign])
        with self.assertRaises(StorageError):
            self.ingester.poll()
        self.stream_dir.joinpath('000001.jsonl').write_text('not json\n', encoding='utf-8')
        with self.assertRaises(StorageError):
            self.ingester.poll()

    def test_invalid_caption_fields_fail_closed(self):
        for bad in (caption_event(1, 'u-1', text='  '),
                    caption_event(1, 'u-1', start_ms=500, end_ms=400),
                    caption_event(1, 'u-1') | {'utterance_id': 7},
                    caption_event(1, 'u-1') | {'schema_version': 2},
                    caption_event(1, 'u-1', speaker='  ')):
            with self.subTest(bad=bad):
                self.write_segment('000001.jsonl', [bad])
                ingester = SpoolIngester(self.source, self.config.data_directory, SESSION,
                                         started_at=NOW - 30)
                with self.assertRaises(StorageError):
                    ingester.poll()


if __name__ == '__main__':
    unittest.main()
