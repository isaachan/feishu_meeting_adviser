"""Offscreen UI and controller tests for the single-session monitor."""
import os
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

try:
    import PySide6  # noqa: F401
    import segno  # noqa: F401
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton
    PYSIDE_READY = True
except ImportError:
    PYSIDE_READY = False

from gui import config as config_mod
from gui.api import ApiError, NetworkError

if PYSIDE_READY:
    from gui.app import Controller, MainWindow
    from gui.ui.pages import (DashboardPage, LoginPage, SettingsPage,
                                    StatusOrb, qr_pixmap)


@unittest.skipUnless(PYSIDE_READY, 'PySide6/segno not installed')
class AppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.store = config_mod.MemoryConfigStore()
        self.client = ScriptedClient()
        self.controller = Controller(
            self.store, client_factory=lambda: self.client,
            job_factory=lambda args, out_root: FakeJob(args),
            capture_out_root=self._temp.name)
        self.window = MainWindow(self.controller)
        # Tests drive synchronous core methods; no background HTTP workers.
        self.controller.run_async = lambda work: None
        self.addCleanup(self.window.close)
        self.addCleanup(self.window._login_timer.stop)
        self.addCleanup(self.window._session_timer.stop)
        self.addCleanup(self.window._stats_timer.stop)

    def login_with_stored_token(self):
        config_mod.save_device_token(self.store, 'dt-x')
        self.controller.do_boot()

    def test_first_login_and_settings(self):
        self.controller.do_boot()
        self.assertEqual(self.controller.state, 'login')
        self.assertIsInstance(self.window.stack.currentWidget(), LoginPage)
        self.assertFalse(self.window.login_page.qr_label.pixmap().isNull())
        self.window.login_page.settings_clicked.emit()
        self.assertIsInstance(self.window.stack.currentWidget(), SettingsPage)
        self.assertFalse(self.window.settings_page.auto_checkbox.isChecked())
        self.assertEqual(self.controller.server_url, config_mod.DEFAULT_SERVER_URL)

    def test_boot_survives_login_network_error(self):
        self.client.start_error = NetworkError('<urlopen error [Errno 22]>')
        self.controller.do_boot()  # 服务器不可达时不得在 boot 期抛出
        self.assertEqual(self.controller.state, 'login')
        self.assertIsInstance(self.window.stack.currentWidget(), LoginPage)
        self.assertIn('重试', self.window.login_page.status_label.text())
        self.assertTrue(self.window.login_page.qr_label.pixmap().isNull())
        self.client.start_error = None
        self.controller.do_start_login()  # 「刷新二维码」恢复
        self.assertFalse(self.window.login_page.qr_label.pixmap().isNull())

    def test_qr_fits_compact_window(self):
        pixmap = qr_pixmap('https://accounts.feishu.cn/open-apis/authen/v1/authorize?x=1')
        self.assertLessEqual(max(pixmap.width(), pixmap.height()), 260)
        self.assertGreater(pixmap.width(), 100)

    def test_login_completion_and_token_expiry(self):
        self.controller.do_boot()
        self.client.poll_result = {'status': 'completed', 'device_token': 'dt-2',
                                   'user': {'open_id': 'ou_A'}}
        self.controller.do_login_tick()
        self.assertEqual(self.controller.state, 'listing')
        self.assertIsInstance(self.window.stack.currentWidget(), DashboardPage)
        self.client.sessions_error = ApiError(401, 'bad_token', 'x')
        self.assertIsNone(self.controller.do_poll_sessions())
        self.assertEqual(self.controller.state, 'login')
        self.assertIsNone(config_mod.load_state(self.store)['device_token'])

    def test_settings_saves_auto_connect_and_server_change_relogs(self):
        self.login_with_stored_token()
        self.window.dashboard_page.settings_clicked.emit()
        self.window.settings_page.auto_checkbox.setChecked(True)
        self.window.settings_page._emit_saved()
        self.assertTrue(config_mod.load_state(self.store)['auto_connect'])
        self.assertEqual(self.controller.state, 'listing')
        self.controller.do_edit_server()
        self.window.settings_page.url_edit.setText('https://new.internal/')
        self.window.settings_page._emit_saved()
        self.assertEqual(self.controller.state, 'login')
        self.assertIsNone(config_mod.load_state(self.store)['device_token'])

    def test_invalid_server_keeps_settings(self):
        self.controller.do_boot()
        self.controller.do_edit_server()
        self.window.settings_page.url_edit.setText('ftp://bad')
        self.window.settings_page._emit_saved()
        self.assertEqual(self.controller.state, 'settings')
        self.assertIn('http', self.window.settings_page.error_label.text())

    def test_single_session_discovery_requires_no_selection(self):
        self.login_with_stored_token()
        self.controller.do_poll_sessions()
        page = self.window.dashboard_page
        self.assertEqual(page.session['session_id'], 's-1')
        self.assertEqual(page.status_label.text(), '已断开')
        self.assertTrue(page.connect_button.isVisibleTo(page) if page.isVisible()
                        else not page.auto_connect)
        seen = []
        page.connect_clicked.connect(seen.append)
        page._toggle_connection()
        self.assertEqual(seen[0]['session_id'], 's-1')

    def test_manual_capture_forces_mic_and_green_requires_both_sources(self):
        self.login_with_stored_token()
        self.controller.do_poll_sessions()
        session = self.client.sessions_payload['sessions'][0]
        job = self.controller.do_start_capture(session)
        self.assertTrue(job.args['mic'])
        self.assertFalse(session['recorder_command_args']['mic'])
        page = self.window.dashboard_page
        self.assertEqual(self.window.stack.currentWidget(), page)
        self.assertEqual(page.status_label.text(), '连接中…')
        self.controller.poll_capture_stats()
        self.assertEqual(page.status_label.text(), '已入会')
        self.assertEqual(page.orb.color, '#38a976')
        job.mic_state = 'muted'
        self.controller.poll_capture_stats()
        self.assertEqual(page.orb.mic_state, 'muted')
        self.assertTrue(page.orb.show_mic)
        job.mic_connected = False
        self.controller.poll_capture_stats()
        self.assertEqual(page.orb.color, '#d8645e')
        self.assertEqual(page.status_label.text(), '连接中…')
        job.mic_connected = True
        job.mic_state = 'no_device'
        self.controller.poll_capture_stats()
        self.assertEqual(page.status_label.text(), '设备不可用')
        job.finish()
        self.controller.poll_capture_stats()
        self.assertEqual(page.status_label.text(), '已断开')

    def test_auto_connection_attempts_once(self):
        config_mod.save_auto_connect(self.store, True)
        self.login_with_stored_token()
        self.controller.do_poll_sessions()
        job = self.controller.job
        self.assertIsNotNone(job)
        self.assertFalse(self.window.dashboard_page.connect_button.isVisible())
        job.finish()
        self.controller.poll_capture_stats()
        self.controller.do_poll_sessions()
        self.assertIsNone(self.controller.job)

    def test_pause_stops_without_auto_resuming(self):
        config_mod.save_auto_connect(self.store, True)
        self.login_with_stored_token()
        self.controller.do_poll_sessions()
        job = self.controller.job
        self.client.sessions_payload['sessions'][0]['phase'] = 'paused'
        self.controller.do_poll_sessions()
        self.assertTrue(job.stop_requested)
        self.controller.poll_capture_stats()
        self.client.sessions_payload['sessions'][0]['phase'] = 'running'
        self.controller.do_poll_sessions()
        self.assertIsNone(self.controller.job)

    def test_multiple_sessions_stop_and_block_arbitrary_choice(self):
        self.login_with_stored_token()
        self.controller.do_poll_sessions()
        job = self.controller.do_start_capture(self.client.sessions_payload['sessions'][0])
        self.client.sessions_payload['sessions'] *= 2
        self.controller.do_poll_sessions()
        self.assertTrue(job.stop_requested)
        self.assertIsNone(self.window.dashboard_page.session)
        self.assertIn('多个会话', self.window.dashboard_page.status_label.text())

    def test_auto_mode_can_be_turned_off_while_connected(self):
        config_mod.save_auto_connect(self.store, True)
        self.login_with_stored_token()
        self.controller.do_poll_sessions()
        self.controller.do_edit_server()
        self.assertEqual(self.controller.state, 'settings')
        self.assertFalse(self.window.settings_page.url_edit.isEnabled())
        self.window.settings_page.auto_checkbox.setChecked(False)
        self.window.settings_page._emit_saved()
        self.assertEqual(self.controller.state, 'capturing')
        self.assertFalse(config_mod.load_state(self.store)['auto_connect'])
        self.assertFalse(self.window.dashboard_page.connect_button.isHidden())

    def test_close_hides_while_connected(self):
        self.login_with_stored_token()
        self.controller.do_poll_sessions()
        self.controller.do_start_capture(self.client.sessions_payload['sessions'][0])
        self.window.show()
        self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.controller.capturing)

    def test_revoked_login_stops_connection_and_stays_on_login(self):
        self.login_with_stored_token()
        self.controller.do_poll_sessions()
        job = self.controller.do_start_capture(self.client.sessions_payload['sessions'][0])
        self.client.sessions_error = ApiError(401, 'bad_token', 'x')
        self.controller.do_poll_sessions()
        self.assertTrue(job.stop_requested)
        self.assertEqual(self.controller.state, 'login')
        self.controller.poll_capture_stats()
        self.assertEqual(self.controller.state, 'login')

    def test_visible_copy_avoids_sensitive_terms(self):
        self.login_with_stored_token()
        self.controller.do_poll_sessions()
        self.controller.do_start_capture(self.client.sessions_payload['sessions'][0])
        self.controller.poll_capture_stats()
        labels = [w.text() for w in self.window.dashboard_page.findChildren(QLabel)]
        buttons = [w.text() for w in self.window.dashboard_page.findChildren(QPushButton)]
        self.assertNotIn('采集', ' '.join(labels + buttons))
        self.assertNotIn('声音', ' '.join(labels + buttons))

    def test_mute_slash_is_drawn_only_for_muted_state(self):
        orb = StatusOrb()
        orb.show()
        self.addCleanup(orb.close)

        def red_pixels(state):
            orb.set_state('#38a976', show_mic=True, mic_state=state)
            self.app.processEvents()
            image = orb.grab().toImage()
            return sum(1 for y in range(91, 130) for x in range(91, 130)
                       if (pixel := image.pixelColor(x, y)).red() > 170 and
                       pixel.red() > pixel.green() * 1.4 and
                       pixel.red() > pixel.blue() * 1.4)

        self.assertGreater(red_pixels('muted'), 20)
        self.assertEqual(red_pixels('open'), 0)


class ScriptedClient:
    def __init__(self):
        self.poll_result = {'status': 'pending'}
        self.sessions_error = None
        self.start_error = None
        self.sessions_payload = {'user': {'open_id': 'ou_A', 'name': 'Kai Han',
                                          'email': 'kai.han@nio.com'},
                                 'sessions': [{'session_id': 's-1', 'meeting_no': '111',
                                               'title': '周会', 'phase': 'running',
                                               'started_at': 1_763_000_000.0,
                                               'recorder_command_args': {
                                                   'session': 's-1', 'event': 'c/e',
                                                   'url': 'ws://h:8400/ingest',
                                                   'token': 'st-x', 'mic': False}}]}

    def login_start(self):
        if self.start_error is not None:
            raise self.start_error
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
        return self.sessions_payload

    def logout(self, token):
        return {'ok': True}


class FakeJob:
    def __init__(self, args):
        self.args = dict(args)
        self.session_id = args['session']
        self.state = 'idle'
        self.result = None
        self.stop_requested = False
        self.mic_state = 'open'
        self.mic_connected = True
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
                'sources': {'loopback': {'connected': not self.finished},
                            'mic': {'connected': self.mic_connected and not self.finished,
                                    'capture': {'state': self.mic_state}}}}


if __name__ == '__main__':
    unittest.main()
