import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
import io

from local_meeting_recorder.__main__ import main
from local_meeting_recorder.replay import FakeReplay
from local_meeting_recorder.script import parse_script

SESSION = 'local:ab12'


def rows(*delays):
    return parse_script('\n'.join(
        json.dumps({'speaker': '张三', 'text': f'第{index}句', 'delay_ms': delay})
        for index, delay in enumerate(delays, 1)))


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def spool_rows(self, stream_dir):
        rows = []
        for segment in sorted(stream_dir.glob('*.jsonl')):
            rows += [json.loads(row) for row in segment.read_text().splitlines()]
        return rows

    def test_completed_replay_writes_offsets_and_stream_end(self):
        replay = FakeReplay(rows(100, 200, 0), session_id=SESSION, data_root=self.root,
                            speed=1000.0, clock=time.time)
        summary = replay.run()
        self.assertEqual((summary['stopped_reason'], summary['events']), ('completed', 4))
        events = self.spool_rows(self.root / 'recorder' / 'spool' / SESSION /
                                 summary['stream_id'])
        self.assertEqual([event['kind'] for event in events],
                         ['caption', 'caption', 'caption', 'stream_end'])
        self.assertEqual([event['sequence'] for event in events], [1, 2, 3, 4])
        self.assertEqual([event['start_offset_ms'] for event in events[:3]],
                         [100, 300, 300])
        self.assertTrue(all(event['end_offset_ms'] > event['start_offset_ms']
                            for event in events[:3]))
        self.assertEqual(events[3]['last_sequence'], 3)
        self.assertTrue(all(event['session_id'] == SESSION for event in events))

    def test_stop_event_interrupts_between_lines(self):
        stop = threading.Event()
        stop.set()  # already stopped: nothing is emitted, no stream_end
        replay = FakeReplay(rows(10, 10), session_id=SESSION, data_root=self.root)
        summary = replay.run(stop_event=stop)
        self.assertEqual((summary['stopped_reason'], summary['events']), ('stopped', 0))
        events = self.spool_rows(self.root / 'recorder' / 'spool' / SESSION /
                                 summary['stream_id'])
        self.assertEqual(events, [])

    def test_partial_stop_keeps_earlier_events_without_stream_end(self):
        stop = threading.Event()

        def explode(delay):
            raise AssertionError('sleep must not be called in this test')

        lines = parse_script('\n'.join(
            json.dumps({'speaker': '张三', 'text': f'第{index}句', 'delay_ms': 0})
            for index in range(1, 4)))
        replay = FakeReplay(lines, session_id=SESSION, data_root=self.root, sleep=explode)
        # The first clock call opens the stream; the second opens the first
        # segment, i.e. the first caption has just been persisted. Stop there.
        ticks = []

        def ticking_clock():
            ticks.append(1)
            if len(ticks) >= 2:
                stop.set()
            return 1000.0 + len(ticks)

        replay.clock = ticking_clock
        summary = replay.run(stop_event=stop)
        self.assertEqual(summary['stopped_reason'], 'stopped')
        events = self.spool_rows(self.root / 'recorder' / 'spool' / SESSION /
                                 summary['stream_id'])
        self.assertEqual([event['kind'] for event in events], ['caption'])
        self.assertEqual(summary['events'], 1)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.script = self.root / 'script.jsonl'
        self.script.write_text(
            '\n'.join(json.dumps({'speaker': '李四', 'text': f'台词{index}',
                                  'delay_ms': 0}) for index in range(1, 4)) + '\n',
            encoding='utf-8')

    def test_fake_replay_cli_writes_spool_and_reports_summary(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['fake', str(self.script), '--session', SESSION,
                                   '--data-root', str(self.root), '--speed', '10']), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary['stopped_reason'], 'completed')
        stream_dir = (self.root / 'recorder' / 'spool' / SESSION / summary['stream_id'])
        rows = [json.loads(row) for segment in sorted(stream_dir.glob('*.jsonl'))
                for row in segment.read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[-1]['kind'], 'stream_end')

    def test_missing_or_broken_script_fails_cleanly(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['fake', str(self.root / 'absent.jsonl'),
                                   '--session', SESSION, '--data-root', str(self.root)]), 1)
        broken = self.root / 'broken.jsonl'
        broken.write_text('not json\n', encoding='utf-8')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['fake', str(broken), '--session', SESSION,
                                   '--data-root', str(self.root)]), 1)
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(['--version'])
        self.assertEqual(caught.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
