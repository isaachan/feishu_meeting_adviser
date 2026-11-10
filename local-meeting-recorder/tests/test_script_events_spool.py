import json
from pathlib import Path
import tempfile
import time
import unittest

from local_meeting_recorder.events import caption_event, stream_end_event
from local_meeting_recorder.script import parse_script
from local_meeting_recorder.spool import SpoolWriter


def line(**fields):
    return json.dumps(fields, ensure_ascii=False)


class ScriptTests(unittest.TestCase):
    def test_parse_accepts_speakers_defaults_and_blank_lines(self):
        parsed = parse_script(
            '\n' + line(text='第一句') + '\n'
            + line(speaker='张三', text='第二句', delay_ms=1500) + '\n'
            + line(speaker=None, text='第三句', delay_ms=0) + '\n\n')
        self.assertEqual([(item.speaker, item.delay_ms) for item in parsed],
                         [(None, 0), ('张三', 1500), (None, 0)])

    def test_parse_rejects_invalid_rows(self):
        for raw in ('', 'not json', '[]', line(), line(text='  '),
                    line(text='x' * 2001), line(text='x', speaker=''),
                    line(text='x', speaker='s' * 81), line(text='x', delay_ms=-1),
                    line(text='x', delay_ms=True), line(text='x', delay_ms='100'),
                    line(text=5)):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_script(raw)


class EventTests(unittest.TestCase):
    def test_caption_event_shape_and_ordering(self):
        event = caption_event('local:s', 'local:s:t', 2, 'u-000002', '一句', '张三',
                              1200, 4100, wall_clock_at='2026-11-08T10:00:01+00:00')
        self.assertEqual(event['event_id'], 'local:s:t:2')
        self.assertEqual(event['schema_version'], 1)
        self.assertEqual((event['kind'], event['revision']), ('caption', 1))
        with self.assertRaises(ValueError):
            caption_event('local:s', 'local:s:t', 3, 'u', 'x', None, 500, 400)

    def test_stream_end_event_carries_last_sequence(self):
        event = stream_end_event('local:s', 'local:s:t', 4, 3,
                                 wall_clock_at='2026-11-08T10:00:05+00:00')
        self.assertEqual((event['kind'], event['last_sequence']), ('stream_end', 3))


class SpoolWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def writer(self):
        return SpoolWriter(self.root, 'local:s', 'local:s:t', clock=lambda: 1.0)

    def events(self, count):
        return [caption_event('local:s', 'local:s:t', number, f'u-{number}', f'句{number}',
                              None, number * 100, number * 100 + 50,
                              wall_clock_at='2026-11-08T10:00:00+00:00')
                for number in range(1, count + 1)]

    def test_segments_are_append_only_and_renamed_atomically(self):
        writer = self.writer()
        for event in self.events(3):
            writer.append(event)
        self.assertTrue(list(writer.stream_dir.glob('current.tmp')))
        self.assertEqual(list(writer.stream_dir.glob('*.jsonl')), [])
        writer.finish()
        self.assertEqual(list(writer.stream_dir.glob('current.tmp')), [])
        [segment] = writer.stream_dir.glob('*.jsonl')
        self.assertEqual(segment.name, '000001.jsonl')
        rows = [json.loads(row) for row in segment.read_text().splitlines()]
        self.assertEqual([row['sequence'] for row in rows], [1, 2, 3])

    def test_rolling_names_segments_by_first_sequence(self):
        writer = SpoolWriter(self.root, 'local:s', 'local:s:t', roll_events=2,
                             clock=lambda: 1.0)
        for event in self.events(5):
            writer.append(event)
        writer.finish()
        self.assertEqual([path.name for path in sorted(writer.stream_dir.glob('*.jsonl'))],
                         ['000001.jsonl', '000003.jsonl', '000005.jsonl'])

    def test_sparse_event_becomes_visible_without_next_append(self):
        writer = SpoolWriter(self.root, 'local:s', 'local:s:sparse',
                             roll_seconds=0.3)
        try:
            writer.append(self.events(1)[0])
            deadline = time.monotonic() + 3.0
            visible = []
            while time.monotonic() < deadline:
                visible = list(writer.stream_dir.glob('*.jsonl'))
                if visible:
                    break
                time.sleep(0.05)
            self.assertEqual(len(visible), 1, 'age roller must publish sparse events')
            rows = [json.loads(row) for row in visible[0].read_text().splitlines()]
            self.assertEqual([row['sequence'] for row in rows], [1])
        finally:
            writer.finish()

    def test_cleanup_removes_only_fully_acked_segments(self):
        writer = SpoolWriter(self.root, 'local:s', 'local:s:t', roll_events=2,
                             clock=lambda: 1.0)
        for event in self.events(4):
            writer.append(event)
        writer.finish()
        self.assertEqual([path.name for path in sorted(writer.stream_dir.glob('*.jsonl'))],
                         ['000001.jsonl', '000003.jsonl'])
        writer.ack_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        writer.ack_path.write_text('{"acked_sequence": 2}', encoding='utf-8')
        self.assertEqual(writer.acked_sequence(), 2)
        self.assertEqual(writer.cleanup(), 1)
        remaining = [path.name for path in sorted(writer.stream_dir.glob('*.jsonl'))]
        self.assertEqual(remaining, ['000003.jsonl'])  # covers 3..4, only 2 acked
        writer.ack_path.write_text('{"acked_sequence": 4}', encoding='utf-8')
        self.assertEqual(writer.cleanup(), 1)
        self.assertEqual(list(writer.stream_dir.glob('*.jsonl')), [])


if __name__ == '__main__':
    unittest.main()
