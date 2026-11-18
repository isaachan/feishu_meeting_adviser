import struct
import unittest

from recorder.audio.frames import AudioFrame, SourceEvent
from recorder.audio.macos_mic import MacFeishuMicSource


class Detector:
    def __init__(self):
        self.state = {'known': False, 'muted': None, 'detail': ''}

    def get_state(self):
        return dict(self.state)

    def start(self):
        pass

    def stop(self):
        pass


class Stream:
    def __init__(self):
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class Backend:
    def __init__(self):
        self.streams = []

    def open(self, callback):
        stream = Stream()
        self.streams.append(stream)
        return stream


class MacMicTests(unittest.TestCase):
    def setUp(self):
        self.detector = Detector()
        self.backend = Backend()
        self.now = [10.0]
        self.source = MacFeishuMicSource(detector=self.detector, backend=self.backend,
                                         clock=lambda: self.now[0])
        self.items = []
        self.source._sink = self.items.append

    def set_state(self, known, muted):
        self.detector.state = {'known': known, 'muted': muted, 'detail': ''}
        self.source._tick()

    def test_only_confirmed_unmute_opens_and_muting_closes(self):
        self.set_state(False, None)
        self.set_state(True, True)
        self.assertEqual(self.backend.streams, [])
        self.set_state(True, False)
        self.assertTrue(self.backend.streams[0].started)
        self.set_state(True, True)
        self.assertTrue(self.backend.streams[0].closed)
        self.set_state(True, False)
        self.assertEqual(len(self.backend.streams), 2)
        events = [item.type for item in self.items if isinstance(item, SourceEvent)]
        self.assertIn('gap_begin', events)
        self.assertIn('gap_end', events)

    def test_callback_rechecks_gate_before_retaining_audio(self):
        self.set_state(True, False)
        data = struct.pack('<160f', *([0.5] * 160))
        self.source._callback(data, 160, None, None)
        self.detector.state = {'known': False, 'muted': None}
        self.source._callback(data, 160, None, None)
        frames = [item for item in self.items if isinstance(item, AudioFrame)]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].sample_offset, 0)
        self.assertEqual(struct.unpack('<f', frames[0].data[:4])[0], 0.5)

    def test_manual_pause_and_silence_never_read_microphone(self):
        self.set_state(True, False)
        self.source.pause()
        self.assertTrue(self.backend.streams[0].closed)
        self.now[0] += 0.2
        self.source._tick()
        self.assertEqual(len(self.backend.streams), 1)
        self.now[0] += 0.2
        self.source._tick()
        frames = [item for item in self.items if isinstance(item, AudioFrame)]
        self.assertEqual(len(frames), 1)
        self.assertEqual(set(frames[0].data), {0})
        self.source.resume()
        self.source._tick()
        self.assertEqual(len(self.backend.streams), 2)


if __name__ == '__main__':
    unittest.main()
