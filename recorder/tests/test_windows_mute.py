import unittest

from recorder.audio.windows_mute import FeishuMuteDetector, button_state


class WindowsMuteTests(unittest.TestCase):
    def test_button_actions_are_inverted(self):
        self.assertIs(button_state('打开麦克风'), True)
        self.assertIs(button_state('关闭麦克风'), False)
        self.assertIsNone(button_state('麦克风'))

    def test_unknown_and_transition_fail_closed(self):
        now = [10.0]
        detector = FeishuMuteDetector(clock=lambda: now[0])
        detector.apply(False, 'toolbar', '', True)
        self.assertFalse(detector.get_state()['known'])
        detector.apply(False, 'toolbar', '', True)
        self.assertIs(detector.get_state()['muted'], False)
        detector.apply(True, 'toolbar', '', True)
        self.assertFalse(detector.get_state()['known'])
        detector.apply(True, 'toolbar', '', True)
        self.assertIs(detector.get_state()['muted'], True)
        detector.apply(None, 'unknown', '', True)
        self.assertFalse(detector.get_state()['known'])

    def test_stalled_detector_is_unknown(self):
        now = [10.0]
        detector = FeishuMuteDetector(clock=lambda: now[0], stale_after=2)
        detector.apply(False, 'toolbar', '', True)
        detector.apply(False, 'toolbar', '', True)
        now[0] += 2.1
        self.assertFalse(detector.get_state()['known'])


if __name__ == '__main__':
    unittest.main()
