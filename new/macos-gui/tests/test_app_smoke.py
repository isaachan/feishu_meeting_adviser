"""Qt smoke tests (offscreen): page wiring and controller state machine.

Skipped automatically when PySide6/segno are not installed (headless CI legs
install them via the dev extra; core-logic tests never require Qt).
"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    import PySide6  # noqa: F401
    import segno  # noqa: F401
    PYSIDE_READY = True
except ImportError:
    PYSIDE_READY = False

from macos_gui import config as config_mod
from macos_gui.api import ApiError

if PYSIDE_READY:
    from PySide6.QtWidgets import QApplication
    from macos_gui.app import Controller, MainWindow
    from macos_gui.ui.pages import ListPage, LoginPage, SettingsPage, qr_pixmap


@unittest.skipUnless(PYSIDE_READY, 'PySide6/segno not installed')
class AppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.store = config_mod.MemoryConfigStore()
        self.client = ScriptedClient()
        self.controller = Controller(
            self.store, client_factory=lambda: self.client)
        self.window = MainWindow(self.controller)
        self.pages = {'settings': self.window.settings_page,
                      'login': self.window.login_page,
                      'listing': self.window.list_page}

    def current_page(self):
        return self.window.stack.currentWidget()

    def test_boot_without_server_lands_on_settings(self):
        self.controller.do_boot()
        self.assertIsInstance(self.current_page(), SettingsPage)

    def test_save_server_moves_to_login_and_qr_shows(self):
        self.controller.do_boot()
        self.controller.do_save_server('https://advisor.internal/')
        self.assertEqual(self.controller.state, 'login')
        self.assertIsInstance(self.current_page(), LoginPage)
        self.assertFalse(self.window.login_page.qr_label.pixmap().isNull())

    def test_full_state_machine_to_listing(self):
        config_mod.save_server_url(self.store, 'https://advisor.internal/')
        config_mod.save_device_token(self.store, 'dt-old')
        self.controller.do_boot()
        self.assertEqual(self.controller.state, 'listing')

        # poll returns a session; the list page renders it
        snapshot = self.controller.do_poll_sessions()
        self.assertEqual(snapshot['sessions'][0]['session_id'], 's-1')
        self.assertEqual(self.window.list_page.session_list.count(), 1)
        self.assertIsInstance(self.current_page(), ListPage)

        # revoked device token (401) clears state and returns to the QR login
        self.client.sessions_error = ApiError(401, 'bad_token', 'x')
        self.assertIsNone(self.controller.do_poll_sessions())
        self.assertEqual(self.controller.state, 'login')
        self.assertIsNone(config_mod.load_state(self.store)['device_token'])

    def test_login_tick_completion_moves_to_listing(self):
        config_mod.save_server_url(self.store, 'https://advisor.internal/')
        self.controller.do_boot()
        self.client.poll_result = {'status': 'pending'}
        self.controller.do_login_tick()
        self.client.poll_result = {'status': 'completed', 'device_token': 'dt-2',
                                   'user': {'open_id': 'ou_A'}}
        event = self.controller.do_login_tick()
        self.assertEqual(event['state'], 'completed')
        self.assertEqual(self.controller.state, 'listing')
        self.assertEqual(config_mod.load_state(self.store)['device_token'], 'dt-2')

    def test_logout_returns_to_login(self):
        config_mod.save_server_url(self.store, 'https://advisor.internal/')
        config_mod.save_device_token(self.store, 'dt-x')
        self.controller.do_boot()
        self.controller.do_logout()
        self.assertEqual(self.controller.state, 'login')
        self.assertIsNone(config_mod.load_state(self.store)['device_token'])

    def test_qr_pixmap_renders(self):
        pixmap = qr_pixmap('https://accounts.feishu.cn/open-apis/authen/v1/authorize?x=1')
        self.assertFalse(pixmap.isNull())
        self.assertGreater(pixmap.width(), 100)


class ScriptedClient:
    def __init__(self):
        self.poll_result = {'status': 'pending'}
        self.sessions_error = None

    def login_start(self):
        return {'login_url': 'https://accounts.feishu.cn/open-apis/authen/v1/'
                             'authorize?client_id=x&state=st',
                'device_code': 'dc-xyz-123456', 'expires_at': 1.0}

    def login_poll(self, device_code):
        if isinstance(self.poll_result, Exception):
            raise self.poll_result
        return dict(self.poll_result)

    def sessions(self, token):
        if self.sessions_error is not None:
            raise self.sessions_error
        return {'user': {'open_id': 'ou_A', 'name': 'Kai Han',
                         'email': 'kai.han@nio.com'},
                'sessions': [{'session_id': 's-1', 'meeting_no': '111',
                              'title': '周会', 'phase': 'recording',
                              'started_at': 1_763_000_000.0,
                              'recorder_command_args': {
                                  'session': 's-1', 'event': 'c/e',
                                  'url': 'ws://h:8400/ingest',
                                  'token': 'st-x', 'mic': False}}]}

    def logout(self, token):
        return {'ok': True}


if __name__ == '__main__':
    unittest.main()
