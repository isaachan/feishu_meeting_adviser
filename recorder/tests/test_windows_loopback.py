import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from recorder.audio.windows_loopback import find_candidates, read_capture


class WindowsLoopbackTests(unittest.TestCase):
    def test_only_feishu_candidates_are_used(self):
        class Capture:
            @staticmethod
            def enumerate_audio_processes():
                return [SimpleNamespace(pid=1, name='chrome.exe', window_title='会议'),
                        SimpleNamespace(pid=2, name='Feishu.exe', window_title='飞书'),
                        SimpleNamespace(pid=3, name='Lark.exe', window_title='会议')]
        self.assertEqual(find_candidates(Capture), [3, 2])

    def test_reads_pcm_wav_as_16k_mono(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audio.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b'\x00\x40' * 160)
            samples = read_capture(path)
            self.assertEqual(len(samples), 160)
            self.assertAlmostEqual(float(samples[0]), 0.5)


if __name__ == '__main__':
    unittest.main()
