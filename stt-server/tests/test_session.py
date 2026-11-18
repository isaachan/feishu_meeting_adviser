import json
import struct
import unittest
from pathlib import Path

from stt_server.session import (FINAL, PARTIAL, FireRedSession,
                                ServerSession, SessionConfigError,
                                fire_red_model)


class FakeSherpaStream:
    def __init__(self):
        self.accepted = 0
        self.finished = False

    def accept_waveform(self, rate, samples):
        self.accepted += len(samples)

    def input_finished(self):
        self.finished = True


class FakeSherpaRecognizer:
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


class FakeOfflineStream:
    def __init__(self):
        self.rate = None
        self.samples = None
        self.text = ''

    def accept_waveform(self, rate, samples):
        self.rate = rate
        self.samples = samples

    @property
    def result(self):
        class _Result:
            pass

        result = _Result()
        result.text = self.text
        return result


class FakeOfflineRecognizer:
    def __init__(self, texts=None):
        self.texts = list(texts or [])
        self.decoded_lengths = []

    def create_stream(self):
        return FakeOfflineStream()

    def decode_stream(self, stream):
        stream.text = self.texts.pop(0) if self.texts else ''
        self.decoded_lengths.append(len(stream.samples))


class FakeSpeechSegment:
    def __init__(self, start, length):
        self.start = start
        self.samples = [0.01] * length


class FakeVad:
    def __init__(self):
        self.closed = []
        self.opened = None
        self.accepted = 0

    def accept_waveform(self, samples):
        self.accepted += len(samples)

    def empty(self):
        return not self.closed

    @property
    def front(self):
        return self.closed[0]

    def pop(self):
        self.closed.pop(0)

    def flush(self):
        if self.opened is not None:
            self.closed.append(self.opened)
            self.opened = None


def pcm16(samples: int, value: int = 8000) -> bytes:
    return struct.pack('<h', value) * samples


class ServerSessionTests(unittest.TestCase):
    def make(self):
        self.recognizer = FakeSherpaRecognizer()
        session = ServerSession(model_dir=Path('.'),
                                recognizer_factory=lambda: self.recognizer,
                                clock=lambda: 0.0)
        session.start()
        return session

    def test_partial_then_endpoint_final_with_offsets(self):
        session = self.make()
        self.assertEqual(session.feed_samples(pcm16(1600)), [])
        self.recognizer.text = '你好会议'
        events = session.feed_samples(pcm16(1600))
        self.assertEqual([(e.status, e.text, e.start_offset, e.end_offset)
                          for e in events],
                         [(PARTIAL, '你好会议', 0, 3200)])
        self.recognizer.endpoint = True
        events = session.feed_samples(pcm16(1600))
        self.assertEqual([(e.status, e.text, e.start_offset, e.end_offset)
                          for e in events],
                         [(FINAL, '你好会议', 0, 4800)])
        self.assertEqual(self.recognizer.reset_count, 1)
        # next utterance starts where the previous final ended
        self.recognizer.text = '第二句'
        events = session.feed_samples(pcm16(1600))
        self.assertEqual(events[0].start_offset, 4800)

    def test_gap_flushes_open_utterance(self):
        session = self.make()
        session.feed_samples(pcm16(1600))
        self.recognizer.text = '被打断的话'
        session.feed_samples(pcm16(1600))
        events = session.feed_gap()
        self.assertEqual([(e.status, e.text, e.end_offset) for e in events],
                         [(FINAL, '被打断的话', 3200)])

    def test_close_flushes_trailing_text(self):
        session = self.make()
        session.feed_samples(pcm16(1600))
        self.recognizer.text = '收尾句'
        session.feed_samples(pcm16(1600))
        events = session.close()
        self.assertEqual([(e.status, e.text) for e in events],
                         [(FINAL, '收尾句')])
        self.assertEqual(session.close(), [])

    def test_bad_pcm_rejected(self):
        session = self.make()
        with self.assertRaises(ValueError):
            session.feed_samples(b'\x01')  # odd length
        with self.assertRaises(ValueError):
            session.feed_samples(b'')

    def test_missing_model_dir_raises(self):
        session = ServerSession(model_dir=Path('/nonexistent-lmr'))
        with self.assertRaises(SessionConfigError):
            session.start()


class FireRedSessionTests(unittest.TestCase):
    def make(self, texts=('第一句识别文本', '第二句识别文本', '')):
        self.recognizer = FakeOfflineRecognizer(texts)
        self.vad = FakeVad()
        session = FireRedSession(model_dir=Path('.'),
                                 recognizer_factory=lambda: self.recognizer,
                                 vad_factory=lambda: self.vad,
                                 clock=lambda: 0.0)
        session.start()
        return session

    def test_closed_segment_emits_final_with_offsets(self):
        session = self.make(texts=('第一句识别文本',))
        self.assertEqual(session.feed_samples(pcm16(1600)), [])
        self.vad.closed.append(FakeSpeechSegment(1600, 3200))
        events = session.feed_samples(pcm16(1600))
        self.assertEqual([(e.status, e.text, e.start_offset, e.end_offset)
                          for e in events],
                         [(FINAL, '第一句识别文本', 1600, 4800)])
        self.assertEqual(self.recognizer.decoded_lengths, [3200])

    def test_empty_text_segment_emits_nothing(self):
        session = self.make(texts=('',))
        self.vad.closed.append(FakeSpeechSegment(0, 1600))
        self.assertEqual(session.feed_samples(pcm16(1600)), [])
        self.assertTrue(self.vad.empty())

    def test_no_partial_events(self):
        session = self.make(texts=('第一句识别文本',))
        session.feed_samples(pcm16(3200))
        self.vad.opened = FakeSpeechSegment(0, 3200)
        self.assertEqual(session.feed_samples(pcm16(1600)), [])

    def test_gap_flushes_open_segment(self):
        session = self.make(texts=('被打断的话',))
        session.feed_samples(pcm16(1600))
        self.vad.opened = FakeSpeechSegment(0, 3200)
        events = session.feed_gap()
        self.assertEqual([(e.status, e.text, e.start_offset, e.end_offset)
                          for e in events],
                         [(FINAL, '被打断的话', 0, 3200)])
    def test_close_flushes_open_segment_once(self):
        session = self.make(texts=('收尾句',))
        session.feed_samples(pcm16(1600))
        self.vad.opened = FakeSpeechSegment(0, 1600)
        self.assertEqual([(e.status, e.text) for e in session.close()],
                         [(FINAL, '收尾句')])
        self.assertEqual(session.close(), [])

    def test_bad_pcm_rejected(self):
        session = self.make()
        with self.assertRaises(ValueError):
            session.feed_samples(b'\x01')
        with self.assertRaises(ValueError):
            session.feed_samples(b'')

    def test_fed_counts_samples(self):
        session = self.make()
        session.feed_samples(pcm16(1600))
        session.feed_samples(pcm16(800))
        self.assertEqual(session.fed, 2400)

    def test_decode_ms_accumulates(self):
        ticks = iter([0.0, 0.25, 0.0, 0.75])
        recognizer = FakeOfflineRecognizer(texts=('a', 'b'))
        vad = FakeVad()
        session = FireRedSession(model_dir=Path('.'),
                                 recognizer_factory=lambda: recognizer,
                                 vad_factory=lambda: vad,
                                 clock=lambda: next(ticks))
        session.start()
        vad.closed.append(FakeSpeechSegment(0, 1600))
        session.feed_samples(pcm16(1600))
        vad.closed.append(FakeSpeechSegment(1600, 1600))
        session.feed_samples(pcm16(1600))
        self.assertEqual(session.decode_ms, 1000.0)

    def test_missing_model_dir_raises(self):
        session = FireRedSession(model_dir=Path('/nonexistent-lmr'))
        with self.assertRaises(SessionConfigError):
            session.start()


class FireRedModelDetectionTests(unittest.TestCase):
    def test_streaming_dir_not_fire_red(self):
        with __import__('tempfile').TemporaryDirectory() as tmp:
            model_dir = Path(tmp)
            for name in ('tokens.txt', 'encoder.int8.onnx', 'decoder.onnx',
                         'joiner.int8.onnx'):
                (model_dir / name).write_bytes(b'x')
            self.assertFalse(fire_red_model(model_dir))

    def test_fire_red_dir_detected(self):
        with __import__('tempfile').TemporaryDirectory() as tmp:
            model_dir = Path(tmp)
            for name in ('tokens.txt', 'encoder.int8.onnx',
                         'decoder.int8.onnx'):
                (model_dir / name).write_bytes(b'x')
            self.assertTrue(fire_red_model(model_dir))

    def test_empty_dir_not_fire_red(self):
        with __import__('tempfile').TemporaryDirectory() as tmp:
            self.assertFalse(fire_red_model(Path(tmp)))


if __name__ == '__main__':
    unittest.main()
