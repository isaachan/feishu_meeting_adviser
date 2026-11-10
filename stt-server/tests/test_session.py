import json
import struct
import unittest
from pathlib import Path

from stt_server.session import FINAL, PARTIAL, ServerSession, SessionConfigError


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


if __name__ == '__main__':
    unittest.main()
