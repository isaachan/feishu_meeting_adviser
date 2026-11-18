import unittest

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


if __name__ == '__main__':
    unittest.main()
