import struct
import tempfile
import unittest
import wave
from pathlib import Path

from recorder.audio import CaptureSession
from recorder.audio.frames import AudioFrame, SourceEvent
from recorder.audio.windows_mic import WindowsMicSource


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

    def test_unknown_produces_synthetic_silence_without_opening_device(self):
        self.set_state(False, None)
        self.now[0] += 0.2
        self.source._tick()
        frames = [item for item in self.items if isinstance(item, AudioFrame)]
        self.assertEqual(len(frames), 1)
        self.assertEqual(set(frames[0].data), {0})
        self.assertFalse(self.backend.streams)

    def test_gate_precedes_retention_and_subscriber(self):
        import numpy as np

        self.source.interval = 60
        with tempfile.TemporaryDirectory() as directory:
            session = CaptureSession([self.source], Path(directory))
            received = session.subscribe()
            session.start()
            data = np.ones((160, 1), dtype=np.float32)
            self.set_state(True, False)
            self.source._callback(data, len(data), None, None)
            self.detector.state = {'known': False, 'muted': None}
            self.source._callback(data, len(data), None, None)
            self.source._tick()
            session.stop()
            with wave.open(str(Path(directory) / 'mic.wav'), 'rb') as wav:
                self.assertEqual(wav.getnframes(), 160)
            frames = [item for item in list(received.queue) if isinstance(item, AudioFrame)]
            self.assertEqual(len(frames), 1)


if __name__ == '__main__':
    unittest.main()
