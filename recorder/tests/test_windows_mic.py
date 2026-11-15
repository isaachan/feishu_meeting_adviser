import struct
import unittest

from recorder.audio.frames import AudioFrame, SourceEvent
from recorder.audio.windows_mic import WindowsMicSource


class Detector:
    def __init__(self):
        self.state = {'known': False, 'muted': None, 'detail': ''}

    def get_state(self):
        return dict(self.state)


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
        self.device = {'id': 'one', 'name': 'First'}
        self.streams = []

    def default_device(self):
        return self.device

    def open(self, device, callback):
        stream = Stream()
        self.streams.append(stream)
        return stream, 16000


class WindowsMicTests(unittest.TestCase):
    def setUp(self):
        self.detector = Detector()
        self.backend = Backend()
        self.now = [10.0]
        self.source = WindowsMicSource(detector=self.detector, backend=self.backend,
                                       clock=lambda: self.now[0])
        self.items = []
        self.source._sink = self.items.append

    def set_state(self, known, muted):
        self.detector.state = {'known': known, 'muted': muted, 'detail': ''}
        self.source._tick()

    def test_unknown_and_muted_never_open_then_resume(self):
        self.set_state(False, None)
        self.set_state(True, True)
        self.assertEqual(self.backend.streams, [])
        self.set_state(True, False)
        self.assertTrue(self.backend.streams[-1].started)
        self.set_state(False, None)
        self.assertTrue(self.backend.streams[-1].closed)
        self.set_state(True, False)
        self.assertEqual(len(self.backend.streams), 2)
        self.assertIn('gap_begin', [x.type for x in self.items if isinstance(x, SourceEvent)])

    def test_callback_rechecks_gate_before_saving_audio(self):
        import numpy as np
        self.set_state(True, False)
        data = np.ones((160, 1), dtype=np.float32)
        self.source._callback(data, len(data), None, None)
        self.detector.state = {'known': False, 'muted': None}
        self.source._callback(data, len(data), None, None)
        frames = [x for x in self.items if isinstance(x, AudioFrame)]
        self.assertEqual(len(frames), 1)
        self.assertEqual(struct.unpack('<f', frames[0].data[:4])[0], 1.0)

    def test_default_device_switch_is_debounced(self):
        self.set_state(True, False)
        old = self.backend.streams[-1]
        self.backend.device = {'id': 'two', 'name': 'Second'}
        self.source._tick()
        self.now[0] += 2.1
        self.source._tick()
        self.assertTrue(old.closed)
        self.assertEqual(self.source.stats()['device'], 'Second')


if __name__ == '__main__':
    unittest.main()
