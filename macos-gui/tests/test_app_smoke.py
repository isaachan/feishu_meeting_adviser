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
        import tempfile
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp_dir = self._temp.name
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

    def test_capture_flow_and_close_semantics(self):
        config_mod.save_server_url(self.store, 'https://advisor.internal/')
        config_mod.save_device_token(self.store, 'dt-x')
        self.controller.job_factory = lambda args, out_root: FakeJob(args)
        self.controller.capture_out_root = self.tmp_dir
        self.controller.do_boot()
        session = self.client.sessions_payload['sessions'][0]
        job = self.controller.do_start_capture(session)
        self.assertIsNotNone(job)
        self.assertEqual(self.controller.state, 'capturing')
        self.assertIs(self.current_page(), self.window.monitor_page)
        self.assertIn('周会', self.window.monitor_page.title_label.text())

        # D7: closing the window while capturing hides it and keeps the job
        self.window.show()
        self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.controller.capturing)

        # stats poll surfaces rows; finish returns to the listing page
        stats = self.controller.poll_capture_stats()
        self.assertEqual(stats['state'], 'running')
        job.finish(exit_code=0)
        stats = self.controller.poll_capture_stats()
        self.assertEqual(stats['state'], 'stopped')
        self.assertFalse(self.controller.capturing)
        self.assertEqual(self.controller.state, 'listing')

    def test_session_disappearance_auto_stops_capture(self):
        config_mod.save_server_url(self.store, 'https://advisor.internal/')
        config_mod.save_device_token(self.store, 'dt-x')
        self.controller.job_factory = lambda args, out_root: FakeJob(args)
        self.controller.capture_out_root = self.tmp_dir
        self.controller.do_boot()
        session = self.client.sessions_payload['sessions'][0]
        job = self.controller.do_start_capture(session)
        self.client.empty_sessions = True
        self.controller.do_poll_sessions()
        self.assertTrue(job.stop_requested)

    def test_qr_pixmap_renders(self):
        pixmap = qr_pixmap('https://accounts.feishu.cn/open-apis/authen/v1/authorize?x=1')
        self.assertFalse(pixmap.isNull())
        self.assertGreater(pixmap.width(), 100)


class ScriptedClient:
    def __init__(self):
        self.poll_result = {'status': 'pending'}
        self.sessions_error = None
        self.empty_sessions = False
        self.sessions_payload = {'user': {'open_id': 'ou_A', 'name': 'Kai Han',
                                          'email': 'kai.han@nio.com'},
                                 'sessions': [{'session_id': 's-1', 'meeting_no': '111',
                                               'title': '周会', 'phase': 'recording',
                                               'started_at': 1_763_000_000.0,
                                               'recorder_command_args': {
                                                   'session': 's-1', 'event': 'c/e',
                                                   'url': 'ws://h:8400/ingest',
                                                   'token': 'st-x', 'mic': False}}]}

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
        if self.empty_sessions:
            return {'user': self.sessions_payload['user'], 'sessions': []}
        return self.sessions_payload

    def logout(self, token):
        return {'ok': True}


class FakeJob:
    """Recorder-free capture job double for controller wiring tests."""

    def __init__(self, args):
        self.args = dict(args)
        self.session_id = args['session']
        self.state = 'idle'
        self.result = None
        self.stop_requested = False
        self._done = __import__('threading').Event()

    def start(self):
        self.state = 'running'

    @property
    def finished(self):
        return self._done.is_set()

    def finish(self, *, exit_code=0):
        self.state = 'stopped'
        self.result = {'session_id': self.session_id, 'exit_code': exit_code,
                       'sources': {}, 'summary': {}}
        self._done.set()

    def request_stop(self, *, interrupt=False):
        self.stop_requested = True
        self.state = 'stopping'
        self.finish(exit_code=130 if interrupt else 0)

    def stats(self):
        return {'state': self.state, 'session_id': self.session_id,
                'sources': {'loopback': {
                    'sent_frames': 3, 'acked_sequence': 3, 'buffered': 0,
                    'reconnects': 0, 'connected': not self.finished,
                    'exit_code': 0, 'exit_reason': ''}}}


if __name__ == '__main__':
    unittest.main()
