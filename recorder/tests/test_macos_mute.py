import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from recorder.audio.macos_mute import MacFeishuMuteDetector


class MacMuteDetectorTests(unittest.TestCase):
    def setUp(self):
        self.now = [10.0]
        self.detector = MacFeishuMuteDetector(clock=lambda: self.now[0], stale_after=2.0)

    def test_requires_two_readings_and_closes_immediately_on_change(self):
        detector = self.detector
        detector.apply(False, 'meeting_button', '关闭麦克风', True)
        self.assertFalse(detector.get_state()['known'])
        detector.apply(False, 'meeting_button', '关闭麦克风', True)
        self.assertEqual((detector.get_state()['known'], detector.get_state()['muted']),
                         (True, False))
        detector.apply(True, 'meeting_button', '打开麦克风', True)
        self.assertFalse(detector.get_state()['known'])
        detector.apply(True, 'meeting_button', '打开麦克风', True)
        self.assertEqual((detector.get_state()['known'], detector.get_state()['muted']),
                         (True, True))

    def test_unknown_resets_confirmation_and_stale_closes_gate(self):
        detector = self.detector
        detector.apply(False, 'meeting_button', '关闭麦克风', True)
        detector.apply(None, 'permission', '需要辅助功能权限', False)
        detector.apply(False, 'meeting_button', '关闭麦克风', True)
        self.assertFalse(detector.get_state()['known'])
        detector.apply(False, 'meeting_button', '关闭麦克风', True)
        self.now[0] += 2.1
        self.assertEqual(detector.get_state()['source'], 'stale')
        self.assertFalse(detector.get_state()['known'])

    def test_helper_stream_is_read_and_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / 'helper'
            helper.write_text(f'#!{sys.executable}\n'
                              'import json, time\n'
                              'while True:\n'
                              ' print(json.dumps({"muted": False, "source": "meeting_button", '
                              '"detail": "关闭麦克风", "in_meeting": True}), flush=True)\n'
                              ' time.sleep(0.05)\n')
            os.chmod(helper, 0o700)
            detector = MacFeishuMuteDetector(binary=helper)
            detector.start()
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not detector.get_state()['known']:
                    time.sleep(0.01)
                self.assertFalse(detector.get_state()['muted'])
                proc = detector._proc
            finally:
                detector.stop()
            self.assertIsNotNone(proc)
            self.assertIsNotNone(proc.poll())


if __name__ == '__main__':
    unittest.main()
