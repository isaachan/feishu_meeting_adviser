"""Platform policy: retention roots and close-while-capturing behavior."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from gui import platforms


class PlatformPolicyTests(unittest.TestCase):
    def test_windows_blocks_close_and_uses_localappdata(self):
        with patch('gui.platforms.sys.platform', 'win32'), \
                patch.dict(os.environ, {'LOCALAPPDATA': '/tmp/localappdata'}):
            self.assertEqual(platforms.close_while_capturing(), 'block')
            self.assertEqual(platforms.capture_root(),
                             Path('/tmp/localappdata/gui/recorder/audio'))

    def test_windows_falls_back_to_home_without_localappdata(self):
        with patch('gui.platforms.sys.platform', 'win32'), \
                patch.dict(os.environ, {'LOCALAPPDATA': ''}):
            self.assertEqual(platforms.capture_root(),
                             Path.home() / 'AppData/Local/gui/recorder/audio')

    def test_macos_hides_close_and_uses_application_support(self):
        with patch('gui.platforms.sys.platform', 'darwin'):
            self.assertEqual(platforms.close_while_capturing(), 'hide')
            self.assertEqual(platforms.capture_root(),
                             Path.home() / 'Library/Application Support/gui/recorder/audio')

    def test_generic_hides_close_and_uses_home_state(self):
        with patch('gui.platforms.sys.platform', 'linux'):
            self.assertEqual(platforms.close_while_capturing(), 'hide')
            self.assertEqual(platforms.capture_root(),
                             Path.home() / '.local/state/gui/recorder/audio')


if __name__ == '__main__':
    unittest.main()
