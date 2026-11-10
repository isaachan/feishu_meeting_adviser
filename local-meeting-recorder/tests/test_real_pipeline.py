import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
import io

from local_meeting_recorder.__main__ import main
from local_meeting_recorder.audio import FakeSource
from local_meeting_recorder.real import RealRecorder
from local_meeting_recorder.stt import TranscriberError, TranscriptionResult

SESSION = 'local:cd34'
RATE = 16000


def tone(count: int, amp: float = 0.2) -> bytes:
    return struct.pack('<f', amp) * count


class FakeTranscriber:
    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)
        self.calls: list[tuple[str, str | None]] = []

    def transcribe(self, pcm, *, rate=16000, prompt=None, request_id=None):
        source = request_id.split('-')[-2] if request_id else '?'
        index = len(self.calls)
        self.calls.append((source, prompt))
        if index in self.fail_for:
            raise TranscriberError('unavailable', 'planned failure')
        return TranscriptionResult(text=f'文本{index}:{source}', request_id=request_id or 'r',
                                   latency_ms=1, model='fake')


class RealRecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mic = FakeSource('mic')
        self.loop = FakeSource('loopback')

    def push_speech(self, source: FakeSource, amp: float = 0.2) -> None:
        """1s speech + 1.5s silence in 100ms frames (closes the segment)."""
        for _ in range(10):
            source.push_frames(RATE // 10, amp)
        for _ in range(15):
            source.push_frames(RATE // 10, 0.0)

    def run_recorder(self, transcriber, *, mic_enabled=True, duration=2.0,
                     stop_after=None, **extra):
        recorder = RealRecorder(session_id=SESSION, data_root=self.root,
                                mic_enabled=mic_enabled,
                                stt_mode='zhipu', transcriber=transcriber,
                                sources=[self.loop, self.mic], **extra)
        result: dict = {}
        done = threading.Event()

        def target(stop_event):
            try:
                result['summary'] = recorder.run(stop_event=stop_event,
                                                 duration_s=duration)
            finally:
                done.set()

        stop = threading.Event()
        thread = threading.Thread(target=target, kwargs={'stop_event': stop})
        thread.start()
        time.sleep(0.2)
        self.push_speech(self.loop)
        if mic_enabled:
            self.push_speech(self.mic)
        if stop_after is not None:
            stop.wait(stop_after)
            stop.set()
        done.wait(timeout=20)
        thread.join(timeout=5)
        result['diagnostics'] = recorder.diagnostics
        return result

    def spool_rows(self, stream_id: str) -> list[dict]:
        stream_dir = self.root / 'recorder' / 'spool' / SESSION / stream_id
        rows: list[dict] = []
        for segment in sorted(stream_dir.glob('*.jsonl')):
            rows += [json.loads(line) for line in
                     segment.read_text().splitlines()]
        return rows

    def test_calendar_event_metadata_written_to_stream(self):
        result = self.run_recorder(FakeTranscriber(),
                                   calendar_event_id='cal_a/evt_b')
        stream_id = result['summary']['stream_id']
        meta = json.loads((self.root / 'recorder' / 'spool' / SESSION
                           / stream_id / 'meta.json').read_text())
        self.assertEqual(meta['calendar_event_id'], 'cal_a/evt_b')
        self.assertEqual(meta['session_id'], SESSION)
        self.assertEqual(meta['stream_id'], stream_id)

    def test_clean_stop_produces_contract_events_and_retention(self):
        result = self.run_recorder(FakeTranscriber())
        summary = result['summary']
        self.assertEqual(summary['stopped_reason'], 'completed')
        rows = self.spool_rows(summary['stream_id'])
        kinds = [row['kind'] for row in rows]
        self.assertEqual(kinds[-1], 'stream_end')
        captions = [row for row in rows if row['kind'] == 'caption']
        self.assertGreaterEqual(len(captions), 2)
        speakers = {row['speaker_label'] for row in captions}
        self.assertIn('本机', speakers)   # mic mapped to local user
        self.assertIn(None, speakers)     # loopback stays remote/unknown
        sequences = [row['sequence'] for row in rows]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(sequences[-1] - 1, sequences[-2])  # stream_end last
        self.assertTrue(all(row['session_id'] == SESSION for row in rows))
        self.assertTrue(all(row['start_offset_ms'] >= 0 for row in captions))
        audio_dir = self.root / 'recorder' / 'audio' / summary['stream_id']
        self.assertTrue((audio_dir / 'mic.wav').stat().st_size > 44)
        self.assertTrue((audio_dir / 'loopback.wav').stat().st_size > 44)

    def test_interrupt_keeps_stream_open(self):
        result = self.run_recorder(FakeTranscriber(), stop_after=0.8)
        summary = result['summary']
        self.assertEqual(summary['stopped_reason'], 'stopped')
        rows = self.spool_rows(summary['stream_id'])
        self.assertNotIn('stream_end', [row['kind'] for row in rows])
        # already-confirmed captions survive for re-delivery
        self.assertGreaterEqual(len([r for r in rows if r['kind'] == 'caption']), 1)

    def test_stt_failure_does_not_break_pipeline(self):
        transcriber = FakeTranscriber(fail_for={0})
        result = self.run_recorder(transcriber)
        summary = result['summary']
        self.assertEqual(summary['stt_failures'], 1)
        self.assertGreaterEqual(summary['captions'], 1)
        self.assertEqual(len(result['diagnostics']['errors']), 1)
        self.assertEqual(result['diagnostics']['errors'][0]['reason'],
                         'unavailable')

    def test_latency_diagnostics_recorded_per_caption(self):
        result = self.run_recorder(FakeTranscriber())
        latencies = result['diagnostics']['latencies']
        self.assertGreaterEqual(len(latencies), 2)
        sources = {entry['source'] for entry in latencies}
        self.assertEqual(sources, {'mic', 'loopback'})
        for entry in latencies:
            self.assertGreaterEqual(entry['after_segment_end_ms'], 0)
            self.assertGreaterEqual(entry['stt_ms'], 0)

    def test_prompt_chaining_per_source(self):
        transcriber = FakeTranscriber()
        result = self.run_recorder(transcriber)
        self.assertGreaterEqual(len(transcriber.calls), 2)
        rows = self.spool_rows(result['summary']['stream_id'])
        utterances = {r['utterance_id'] for r in rows if r['kind'] == 'caption'}
        self.assertTrue(all(u.startswith('u-') for u in utterances))


class RealCLITests(unittest.TestCase):
    def test_real_subcommand_registered(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(['--help'])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn('real', out.getvalue())

    def test_real_requires_session(self):
        with self.assertRaises(SystemExit) as ctx:
            main(['real'])
        self.assertEqual(ctx.exception.code, 2)

    def test_real_accepts_event_option(self):
        from local_meeting_recorder.__main__ import parser

        args = parser().parse_args(
            ['real', '--session', SESSION, '--event', 'cal_a/evt_b'])
        self.assertEqual(args.event, 'cal_a/evt_b')


if __name__ == '__main__':
    unittest.main()
