"""Tray icon/menu tests (DESIGN §6.5); run offscreen in CI."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    import PySide6  # noqa: F401
    from PySide6.QtWidgets import QApplication
    PYSIDE_READY = True
except ImportError:
    PYSIDE_READY = False

if PYSIDE_READY:
    from gui.ui import tray


@unittest.skipUnless(PYSIDE_READY, 'PySide6 not installed')
class TrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_badge_uses_status_color(self):
        pixmap = tray.tray_icon('#d8645e').pixmap(64, 64)
        self.assertFalse(pixmap.isNull())
        pixel = pixmap.toImage().pixelColor(47, 47)
        self.assertGreater(pixel.red(), 150)
        self.assertGreater(pixel.red(), pixel.green() * 1.3)
        self.assertGreater(pixel.red(), pixel.blue() * 1.3)

    def test_falls_back_to_drawn_tile_without_logo(self):
        with patch.object(tray, 'brand_pixmap', return_value=None):
            pixmap = tray.tray_icon('#123456').pixmap(64, 64)
        pixel = pixmap.toImage().pixelColor(32, 8)
        self.assertGreater(pixel.blue(), 150)

    def test_menu_reflects_connection_state(self):
        icon = tray.TrayIcon()
        self.addCleanup(icon.tray.hide)
        icon.set_state(color='#38a976', label='已入会', title='周会',
                       capturing=True, can_connect=False)
        self.assertTrue(icon.disconnect_action.isVisible())
        self.assertTrue(icon.disconnect_action.isEnabled())
        self.assertFalse(icon.connect_action.isVisible())
        self.assertIn('周会', icon.tray.toolTip())

        icon.set_state(color='#b7c0ca', label='已断开',
                       capturing=False, can_connect=True)
        self.assertTrue(icon.connect_action.isVisible())
        self.assertTrue(icon.connect_action.isEnabled())
        self.assertFalse(icon.disconnect_action.isVisible())


if __name__ == '__main__':
    unittest.main()
