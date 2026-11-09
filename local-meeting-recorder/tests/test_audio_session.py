import json
import queue
import struct
import tempfile
import time
import unittest
import wave
from pathlib import Path

from local_meeting_recorder.audio import AudioFrame, CaptureSession, FakeSource
from local_meeting_recorder.audio.sck_capture import (
    SourceStartError,
    ensure_helper_binary,
)


def read_wav(path: Path):
    with wave.open(str(path), 'rb') as wf:
        return {
            'rate': wf.getframerate(),
            'channels': wf.getnchannels(),
            'frames': wf.getnframes(),
            'samples': list(wf.readframes(wf.getnframes())),
        }


def event_rows(out_dir: Path):
    return [json.loads(line)
            for line in (out_dir / 'events.jsonl').read_text().splitlines()]


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class AudioFrameTests(unittest.TestCase):
    def test_frames_and_end_offset_math(self):
        data = struct.pack('<f', 0.5) * 300
        frame = AudioFrame('mic', 1, 1200, 16000, 1, data, 0.0)
        self.assertEqual(frame.frames, 300)
        self.assertEqual(frame.end_offset, 1500)

    def test_stereo_frame_count_halves(self):
        data = struct.pack('<2f', 0.1, -0.1) * 50
        frame = AudioFrame('loop', 1, 0, 48000, 2, data, 0.0)
        self.assertEqual(frame.frames, 50)
        self.assertEqual(frame.end_offset, 50)


class CaptureSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / 'audio'
        self.mic = FakeSource('mic')
        self.loop = FakeSource('loopback')
        self.session = CaptureSession([self.mic, self.loop], self.out)

    def tearDown(self):
        self.session.stop(timeout=5)

    def test_dual_sources_retained_and_summarized(self):
        self.session.start()
        self.mic.push_frames(1600, 0.25)
        self.loop.push_frames(3200, 0.5)
        summary = self.session.stop(timeout=5)

        mic_wav = read_wav(self.out / 'mic.wav')
        loop_wav = read_wav(self.out / 'loopback.wav')
        self.assertEqual((mic_wav['rate'], mic_wav['channels'], mic_wav['frames']),
                         (16000, 1, 1600))
        self.assertEqual((loop_wav['rate'], loop_wav['channels'], loop_wav['frames']),
                         (16000, 1, 3200))
        # first float32 sample 0.25 -> int16 ~8191
        self.assertAlmostEqual(
            struct.unpack('<h', bytes(mic_wav['samples'][:2]))[0], 8191, delta=2)
        self.assertAlmostEqual(
            struct.unpack('<h', bytes(loop_wav['samples'][:2]))[0], 16383, delta=2)
        self.assertEqual(summary['sources']['mic']['duration_s'], 0.1)
        self.assertEqual(summary['sources']['loopback']['duration_s'], 0.2)
        types = [row['type'] for row in event_rows(self.out)]
        self.assertIn('session_started', types)
        self.assertEqual(types.count('source_started'), 2)
        self.assertEqual(types.count('source_stopped'), 2)
        self.assertIn('session_stopped', types)

    def test_stop_drains_pending_frames(self):
        self.session.start()
        self.mic.push_frames(1000, 0.25)
        self.mic.push_frames(1000, 0.25)
        # stop() drains the queue before closing the wav
        self.session.stop(timeout=5)
        self.assertEqual(read_wav(self.out / 'mic.wav')['frames'], 2000)

    def test_pause_resume_records_explicit_gap(self):
        self.session.start()
        self.mic.push_frames(800, 0.25)
        self.session.pause('mic')
        self.mic.push_frames  # paused: no frames pushed here
        self.session.resume('mic')
        self.mic.push_frames(400, 0.25)
        summary = self.session.stop(timeout=5)

        rows = event_rows(self.out)
        mic_rows = [row for row in rows if row.get('source') == 'mic']
        self.assertEqual(
            [row['type'] for row in mic_rows],
            ['wav_opened', 'source_started', 'gap_begin', 'gap_end', 'source_stopped'])
        # sample timeline not stretched: 1200 frames total, gap not counted
        self.assertEqual(read_wav(self.out / 'mic.wav')['frames'], 1200)
        self.assertEqual(summary['sources']['mic']['gaps'], 1)

    def test_subscriber_receives_ordered_items(self):
        received: queue.Queue = self.session.subscribe()
        self.session.start()
        self.mic.push_frames(160, 0.25)
        self.mic.push_frames(320, 0.25)
        self.mic.stop()
        self.assertTrue(
            wait_until(lambda: received.qsize() >= 4),
            'subscriber did not receive all items')
        frames = [item for item in received.queue if isinstance(item, AudioFrame)]
        self.assertEqual([frame.seq for frame in frames], [1, 2])
        self.assertEqual(frames[1].sample_offset, 160)
        events = [item.type for item in received.queue
                  if not isinstance(item, AudioFrame)]
        self.assertEqual(events[0], 'source_started')

    def test_stuck_subscriber_does_not_block_capture(self):
        stalled: queue.Queue = self.session.subscribe()  # never drained
        self.session.start()
        for _ in range(50):
            self.mic.push_frames(3200, 0.25)
        summary = self.session.stop(timeout=10)

        self.assertEqual(read_wav(self.out / 'mic.wav')['frames'], 160000)
        self.assertEqual(summary['sources']['mic']['frames'], 160000)
        # 50 mic frames + started/stopped for both sources
        self.assertEqual(stalled.qsize(), 54)

    def test_duplicate_source_ids_rejected(self):
        with self.assertRaises(ValueError):
            CaptureSession([FakeSource('mic'), FakeSource('mic')], self.out)

    def test_start_twice_rejected(self):
        self.session.start()
        with self.assertRaises(RuntimeError):
            self.session.start()


class SCKHelperTests(unittest.TestCase):
    def test_env_override_missing_binary_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            import os
            os.environ['LOCAL_MEETING_RECORDER_SCK_BIN'] = str(Path(tmp) / 'nope')
            try:
                with self.assertRaises(SourceStartError):
                    ensure_helper_binary(Path(tmp))
            finally:
                del os.environ['LOCAL_MEETING_RECORDER_SCK_BIN']


if __name__ == '__main__':
    unittest.main()
