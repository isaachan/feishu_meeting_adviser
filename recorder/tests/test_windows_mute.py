import unittest
from types import SimpleNamespace
from unittest.mock import patch

from recorder.audio.windows_mute import FeishuMuteDetector, button_state, detect_raw


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

    def test_detector_uses_feishu_toolbar_action(self):
        rect = SimpleNamespace(width=lambda: 600, height=lambda: 400)
        button = SimpleNamespace(ClassName='MiniDialogMicrophoneButton',
                                 Name='打开麦克风', GetChildren=lambda: [])
        window = SimpleNamespace(Name='飞书会议', BoundingRectangle=rect, ProcessId=123,
                                 GetChildren=lambda: [button])
        auto = SimpleNamespace(GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: [window]))
        with patch('recorder.audio.windows_mute._process_name', return_value='feishu.exe'):
            result = detect_raw(auto, 'User')
        self.assertEqual(result, (True, 'floating_toolbar', '打开麦克风', True))

    def test_browser_meeting_window_is_not_treated_as_feishu(self):
        rect = SimpleNamespace(width=lambda: 600, height=lambda: 400)
        window = SimpleNamespace(Name='飞书会议 - Google Chrome', BoundingRectangle=rect,
                                 ProcessId=123, GetChildren=lambda: [])
        auto = SimpleNamespace(GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: [window]))
        self.assertEqual(detect_raw(auto, 'User')[0], None)


if __name__ == '__main__':
    unittest.main()
