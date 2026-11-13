import os
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from local_meeting_recorder.audio import AudioFrame, FakeSource
from local_meeting_recorder.real import RealRecorder
from local_meeting_recorder.stt import FINAL, PARTIAL, LocalSherpaSession, SttEvent

RATE = 16000


def tone(count: int, amp: float = 0.2) -> bytes:
    return struct.pack('<f', amp) * count


class FakeSherpaStream:
    def __init__(self):
        self.accepted = 0
        self.finished = False

    def accept_waveform(self, rate, samples):
        self.accepted += len(samples)

    def input_finished(self):
        self.finished = True


class FakeSherpaRecognizer:
    """Scripted duck-typed OnlineRecognizer; text/endpoints set by test."""

    def __init__(self):
        self.stream = FakeSherpaStream()
        self.text = ''
        self.endpoint = False
        self.reset_count = 0

    def create_stream(self):
        return self.stream

    def is_ready(self, stream):
        return False

    def decode_stream(self, stream):
        pass

    def get_result(self, stream):
        return self.text

    def is_endpoint(self, stream):
        return self.endpoint

    def reset(self, stream):
        self.reset_count += 1
        self.text = ''
        self.endpoint = False


def frame(source: str, offset: int, samples: int, amp: float = 0.2) -> AudioFrame:
    return AudioFrame(source, 1, offset, RATE, 1, tone(samples, amp), 0.0)


class LocalSherpaSessionTests(unittest.TestCase):
    def make_session(self):
        self.recognizer = FakeSherpaRecognizer()
        session = LocalSherpaSession('mic', model_dir=Path('.'),
                                     recognizer=lambda: self.recognizer,
                                     clock=lambda: 0.0)
        session.start()
        return session

    def test_partial_then_endpoint_final_with_offsets(self):
        session = self.make_session()
        events = session.feed(frame('mic', 0, 1600))
        self.assertEqual(events, [])  # no text yet
        self.recognizer.text = '你好会议'
        events = session.feed(frame('mic', 1600, 1600))
        self.assertEqual([(e.status, e.text, e.start_offset, e.end_offset)
                          for e in events],
                         [(PARTIAL, '你好会议', 0, 3200)])
        self.recognizer.endpoint = True
        events = session.feed(frame('mic', 3200, 1600))
        finals = [e for e in events if e.status == FINAL]
        self.assertEqual([(e.text, e.start_offset, e.end_offset) for e in finals],
                         [('你好会议', 0, 4800)])
        self.assertEqual(self.recognizer.reset_count, 1)
        # next utterance starts at the previous final end
        self.recognizer.text = '第二句'
        events = session.feed(frame('mic', 4800, 1600))
        self.assertEqual(events[0].start_offset, 4800)
        self.assertEqual(events[0].status, PARTIAL)

    def test_feed_gap_flushes_open_utterance(self):
        session = self.make_session()
        session.feed(frame('mic', 0, 1600))
        self.recognizer.text = '被打断的话'
        session.feed(frame('mic', 1600, 1600))
        events = session.feed_gap()
        self.assertEqual([(e.status, e.text, e.end_offset) for e in events],
                         [(FINAL, '被打断的话', 3200)])
        self.assertEqual(self.recognizer.reset_count, 1)

    def test_discontinuity_reanchors_like_gap(self):
        session = self.make_session()
        session.feed(frame('mic', 0, 1600))
        self.recognizer.text = '跨越缺口'
        session.feed(frame('mic', 1600, 1600))
        events = session.feed(frame('mic', 999999, 1600))  # jump
        finals = [e for e in events if e.status == FINAL]
        self.assertEqual(finals[0].text, '跨越缺口')
        self.recognizer.text = '新位置'
        events = session.feed(frame('mic', 1001599, 1600))
        self.assertEqual(events[0].start_offset, 999999)

    def test_close_flushes_trailing_text(self):
        session = self.make_session()
        session.feed(frame('mic', 0, 1600))
        self.recognizer.text = '收尾句'
        session.feed(frame('mic', 1600, 1600))
        events = session.close()
        self.assertEqual([(e.status, e.text) for e in events],
                         [(FINAL, '收尾句')])
        self.assertEqual(session.close(), [])  # idempotent

    def test_missing_model_dir_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = LocalSherpaSession('mic', model_dir=Path(tmp))
            with self.assertRaises(FileNotFoundError):
                session.start()


class ScriptedSession:
    """StreamingSession for RealRecorder tests: emits scripted events."""

    def __init__(self, script):
        self.script = list(script)  # list[(feed_count, status, text)]
        self.feeds = 0
        self.gaps = 0
        self.closed = False

    def start(self):
        pass

    def feed(self, item):
        self.feeds += 1
        out = []
        for entry in list(self.script):
            count, status, text = entry
            if count == self.feeds:
                out.append(SttEvent(status, text, 0, item.sample_offset + 1600))
                self.script.remove(entry)
        return out

    def feed_gap(self):
        self.gaps += 1
        return []

    def close(self):
        self.closed = True
        return []


class RealRecorderStreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mic = FakeSource('mic')
        self.loop = FakeSource('loopback')
        self.sessions = {}

    def factory(self, source_id):
        session = ScriptedSession([
            (1, PARTIAL, '草稿不该入库'),
            (2, FINAL, f'{source_id} 的最终句'),
        ])
        self.sessions[source_id] = session
        return session

    def run_recorder(self, *, duration=2.0):
        recorder = RealRecorder(session_id='local:s1', data_root=self.root,
                                mic_enabled=True, stt_mode='sherpa',
                                streaming_sessions=self.factory,
                                sources=[self.loop, self.mic])
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
        for source in (self.loop, self.mic):
            source.push_frames(1600, 0.2)  # feed 1: partial
            source.push_frames(1600, 0.2)  # feed 2: final
        done.wait(timeout=20)
        thread.join(timeout=5)
        result['diagnostics'] = recorder.diagnostics
        return result

    def spool_rows(self, stream_id):
        stream_dir = self.root / 'recorder' / 'spool' / 'local:s1' / stream_id
        rows = []
        for segment in sorted(stream_dir.glob('*.jsonl')):
            rows += [__import__('json').loads(line) for line in
                     segment.read_text().splitlines()]
        return rows

    def test_partials_never_reach_contract_finals_do(self):
        result = self.run_recorder()
        summary = result['summary']
        self.assertEqual(summary['stopped_reason'], 'completed')
        self.assertEqual(summary['stt_failures'], 0)
        rows = self.spool_rows(summary['stream_id'])
        captions = [row for row in rows if row['kind'] == 'caption']
        texts = [row['text'] for row in captions]
        self.assertNotIn('草稿不该入库', texts)
        self.assertEqual(sorted(texts), ['loopback 的最终句', 'mic 的最终句'])
        speakers = {row['text']: row['speaker_label'] for row in captions}
        self.assertEqual(speakers['mic 的最终句'], '本机')
        self.assertIsNone(speakers['loopback 的最终句'])
        self.assertEqual(rows[-1]['kind'], 'stream_end')
        self.assertEqual(result['diagnostics']['stt_mode'], 'sherpa')
        self.assertEqual(len(result['diagnostics']['latencies']), 2)

    def test_sessions_closed_on_stop(self):
        self.run_recorder()
        for session in self.sessions.values():
            self.assertTrue(session.closed)


@unittest.skipUnless(
    os.environ.get('LMR_SHERPA_MODEL_DIR')
    or (Path(__file__).resolve().parents[2] / 'studies' / 'local-meeting'
        / 'models' / 'sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-'
        'zh-en-punct-int8-2026-06-05').is_dir(),
    'sherpa model not available')
class LocalSherpaRealModelTests(unittest.TestCase):
    MODEL = (Path(__file__).resolve().parents[2] / 'studies' / 'local-meeting'
             / 'models' / 'sherpa-onnx-x-asr-480ms-streaming-zipformer-'
             'transducer-zh-en-punct-int8-2026-06-05')

    def test_official_sample_transcribes(self):
        import wave

        import numpy as np

        wav = self.MODEL / 'test_wavs' / '0.wav'
        with wave.open(str(wav), 'rb') as wf:
            pcm = (np.frombuffer(wf.readframes(wf.getnframes()),
                                 dtype='<i2').astype(np.float32)
                   / 32768.0).tobytes()  # frames carry ±1 float32
        session = LocalSherpaSession('loopback', model_dir=self.MODEL)
        session.start()
        events: list[SttEvent] = []
        chunk_bytes = 3200 * 4
        for i in range(0, len(pcm), chunk_bytes):
            data = pcm[i:i + chunk_bytes]
            from local_meeting_recorder.stt.streaming import RATE
            events += session.feed(AudioFrame(
                'loopback', 0, i // 4, RATE, 1, data, 0.0))
        events += session.close()
        finals = [e.text for e in events if e.status == FINAL and e.text]
        joined = ''.join(finals)
        self.assertIn('昨天', joined)
        self.assertIn('礼拜二', joined)


if __name__ == '__main__':
    unittest.main()
