"""Qt application glue: controller (state machine) + main window.

Synchronous core methods (do_*) are headless-testable; their async wrappers
(run in a worker thread, marshal back via queued signals) plus QTimer wiring
make the UI responsive. HTTP calls block only inside the worker thread.
"""
import logging
import threading
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMainWindow, QStackedWidget

from . import config as config_mod
from .api import ApiError, GuiApiClient, NetworkError
from .login import LoginFlow
from .sessions import normalize_sessions
from .ui.pages import DashboardPage, LoginPage, SettingsPage

LOG = logging.getLogger(__name__)

LOGIN_POLL_INTERVAL_MS = 2500
SESSION_POLL_INTERVAL_MS = 10_000
SESSION_POLL_CAPTURING_MS = 5_000
CAPTURE_STATS_INTERVAL_MS = 1_000


class Controller(QObject):
    """Login, settings, and one session's connection lifecycle."""

    page_changed = Signal(str)          # settings|login|listing|capturing
    login_started = Signal(dict)        # {login_url, device_code, expires_at}
    login_status = Signal(str)          # human text for the login page
    sessions_updated = Signal(dict)     # normalized snapshot
    offline_changed = Signal(bool)
    capture_started = Signal(dict)      # selected session item
    capture_stats = Signal(dict)        # CaptureJob.stats()
    capture_finished = Signal(dict)     # CaptureJob.result

    def __init__(self, store, *, client_factory=None, job_factory=None,
                 capture_out_root=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.client_factory = client_factory or (lambda: GuiApiClient(self.server_url))
        self.job_factory = job_factory
        self.capture_out_root = capture_out_root
        self.state = 'settings'
        self.client = None
        self.flow = None
        self.token = None
        self.offline = False
        self.job = None
        self._auto_attempted_session_id = None
        self._worker_lock = threading.Lock()

    @property
    def capturing(self):
        return self.job is not None and not self.job.finished

    # -- synchronous core (tests call these directly) -------------------------

    @property
    def server_url(self):
        stored = config_mod.load_state(self.store)['server_url']
        return stored or config_mod.DEFAULT_SERVER_URL

    def do_boot(self):
        state = config_mod.load_state(self.store)
        self._connect()
        if state['device_token'] is None:
            self._start_login()
        else:
            self.token = state['device_token']
            self._enter('listing')

    def do_edit_server(self):
        """Open settings; an active connection keeps running in the background."""
        self._enter('settings')

    def do_save_server(self, url):
        """保存服务器地址：换址清除设备凭据并重新扫码；同址直接回列表。"""
        previous = self.server_url
        value = config_mod.normalize_server_url(url)
        if self.capturing:
            if value != previous:
                raise ValueError('请先断开连接，再更改服务器')
            self._enter('capturing')
            return value
        value = config_mod.save_server_url(self.store, url)
        self._connect()
        if value == previous and self.token is not None:
            self._enter('listing')
            return value
        config_mod.clear_device_token(self.store)
        self.token = None
        self._start_login()
        return value

    def do_start_login(self):
        self._connect()
        self._start_login()

    def _start_login(self):
        assert self.client is not None
        self.flow = LoginFlow(self.client)
        start = self.flow.begin()
        self._enter('login')
        self.login_started.emit(start)
        return start

    def do_login_tick(self):
        if self.flow is None or not self.flow.in_progress:
            return None
        event = self.flow.tick()
        if event['state'] == 'awaiting_scan':
            self.login_status.emit('等待扫码…')
        elif event['state'] == 'unavailable':
            self.login_status.emit('暂时连不上服务器，重试中…')
        elif event['state'] == 'expired':
            self._start_login()  # regenerate the QR automatically
            self.login_status.emit('二维码已过期，已自动换新，请重新扫码')
        elif event['state'] == 'completed':
            config_mod.save_device_token(self.store, event['device_token'])
            self.token = event['device_token']
            self.flow = None
            self._enter('listing')
        return event

    def do_poll_sessions(self):
        if self.token is None:
            return None
        if self.client is None:
            self._connect()
        client = self.client
        assert client is not None
        try:
            payload = client.sessions(self.token)
            snapshot = normalize_sessions(payload)
        except ApiError as exc:
            if exc.code == 'bad_token':
                self.do_stop_capture()
                config_mod.clear_device_token(self.store)
                self.token = None
                self._start_login()
                return None
            self._set_offline(True)
            return None
        except NetworkError:
            self._set_offline(True)
            return None
        self._set_offline(False)
        self._auto_stop_ended_session(snapshot)
        self.sessions_updated.emit(snapshot)
        sessions = snapshot['sessions']
        if len(sessions) != 1:
            self._auto_attempted_session_id = None
        elif (self.store.get(config_mod.KEY_AUTO_CONNECT) == 'true' and
              sessions[0]['phase'] == 'running' and self.job is None and
              self.state in ('listing', 'capturing') and
              self._auto_attempted_session_id != sessions[0]['session_id']):
            self._auto_attempted_session_id = sessions[0]['session_id']
            self.do_start_capture(sessions[0])
        return snapshot

    def _auto_stop_ended_session(self, snapshot):
        """会话联动（STATE_MACHINES v2）：采集中的会话不再 running 时干净停止。

        paused（owner 离会）同样停传；恢复 running 后由用户手动重新开始
        （已定 2026-11-16：不自动重录）。
        """
        job = self.job
        if job is None or job.finished:
            return
        if len(snapshot.get('sessions', [])) != 1:
            job.request_stop()
            return
        live = {s['session_id'] for s in snapshot.get('sessions', [])
                if s['phase'] == 'running'}
        if job.session_id not in live:
            job.request_stop()

    def do_logout(self):
        if self.capturing:
            return
        if self.client is None:
            self._connect()
        client = self.client
        assert client is not None
        if self.token is not None:
            try:
                client.logout(self.token)
            except (ApiError, NetworkError):
                pass
        config_mod.clear_device_token(self.store)
        self.token = None
        self._start_login()

    # -- capture (G5) -----------------------------------------------------------

    def _default_job(self, args, out_root):
        from .capture import CaptureJob
        return CaptureJob(args, out_root=out_root)

    def do_start_capture(self, session_item):
        if self.job is not None and not self.job.finished:
            return None
        if session_item.get('phase') != 'running':
            return None
        args = session_item.get('recorder_command_args')
        if not isinstance(args, dict) or not args.get('session'):
            return None
        args = dict(args)
        args['mic'] = True  # fixed client behavior; no user-facing switch
        factory = self.job_factory or self._default_job
        out_root = self.capture_out_root or _default_capture_root()
        job = factory(args, out_root)
        self.job = job
        try:
            job.start()
        except Exception:  # noqa: BLE001  (capture setup can fail on local permissions)
            LOG.exception('capture startup failed')
            self.job = None
            self.capture_finished.emit({'error': '连接失败，请检查系统权限'})
            return None
        self._enter('capturing')
        self.capture_started.emit(session_item)
        return job

    def do_stop_capture(self):
        job = self.job
        if job is not None and not job.finished:
            job.request_stop()

    def poll_capture_stats(self):
        job = self.job
        if job is None:
            return None
        stats = job.stats()
        self.capture_stats.emit(stats)
        if job.finished:
            result = job.result
            self.job = None
            if self.token is not None:
                self._enter('listing')
            self.capture_finished.emit(result)
        return stats

    # -- helpers --------------------------------------------------------------

    def _connect(self):
        self.client = self.client_factory()

    def _enter(self, state):
        self.state = state
        self.page_changed.emit(state)

    def _set_offline(self, value):
        if value != self.offline:
            self.offline = value
            self.offline_changed.emit(value)

    # -- async wrappers (UI threads; queued back via signals) -----------------

    def run_async(self, work):
        def guarded():
            with self._worker_lock:
                try:
                    work()
                except Exception:  # noqa: BLE001  (worker thread; UI stays alive)
                    pass
        threading.Thread(target=guarded, daemon=True).start()


def _default_capture_root():
    """Whole-session audio retention root (same layout as the recorder CLI)."""
    import sys
    if sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support' / 'macos-gui'
    elif sys.platform == 'win32':
        base = Path.home() / 'AppData' / 'Local' / 'macos-gui'
    else:
        base = Path.home() / '.local' / 'state' / 'macos-gui'
    return base / 'recorder' / 'audio'


class MainWindow(QMainWindow):
    def __init__(self, controller: Controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle('会参谋')
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.settings_page = SettingsPage()
        self.login_page = LoginPage()
        self.dashboard_page = DashboardPage()
        for page in (self.settings_page, self.login_page, self.dashboard_page):
            self.stack.addWidget(page)
        self.settings_page.url_edit.setText(controller.server_url)
        self.settings_page.auto_checkbox.setChecked(
            config_mod.load_state(controller.store)['auto_connect'])
        self.dashboard_page.set_auto_connect(
            config_mod.load_state(controller.store)['auto_connect'])

        self._login_timer = QTimer(self)
        self._login_timer.setInterval(LOGIN_POLL_INTERVAL_MS)
        self._login_timer.timeout.connect(self._tick_login)
        self._session_timer = QTimer(self)
        self._session_timer.setInterval(SESSION_POLL_INTERVAL_MS)
        self._session_timer.timeout.connect(self._poll_sessions)
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(CAPTURE_STATS_INTERVAL_MS)
        self._stats_timer.timeout.connect(self.controller.poll_capture_stats)

        controller.page_changed.connect(self._on_page_changed)
        controller.login_started.connect(
            lambda start: self.login_page.show_login(start['login_url'],
                                                     start['device_code']))
        controller.login_status.connect(self.login_page.set_status)
        controller.sessions_updated.connect(self.dashboard_page.update_sessions)
        controller.offline_changed.connect(self.dashboard_page.set_offline)
        controller.capture_started.connect(self.dashboard_page.start_run)
        controller.capture_stats.connect(self.dashboard_page.update_stats)
        controller.capture_finished.connect(self._on_capture_finished)
        self.settings_page.saved.connect(self._save_server)
        self.settings_page.logout_clicked.connect(
            lambda: controller.run_async(controller.do_logout))
        self.login_page.settings_clicked.connect(controller.do_edit_server)
        self.dashboard_page.settings_clicked.connect(controller.do_edit_server)
        self.login_page.regenerate.connect(
            lambda: controller.run_async(controller.do_start_login))
        self.dashboard_page.connect_clicked.connect(self._start_capture)
        self.dashboard_page.disconnect_clicked.connect(self.controller.do_stop_capture)

    def closeEvent(self, event):
        """D7：采集中关窗 = 隐藏窗口继续采集；无采集 = 正常退出。"""
        if self.controller.capturing:
            event.ignore()
            if sys.platform == 'win32':
                self.dashboard_page.set_error('请先断开连接，再关闭窗口')
            else:
                self.hide()
        else:
            event.accept()

    def _save_server(self, url, auto_connect):
        try:
            value = config_mod.normalize_server_url(url)
            if self.controller.capturing and value != self.controller.server_url:
                raise ValueError('请先断开连接，再更改服务器')
            config_mod.save_auto_connect(self.controller.store, auto_connect)
            self.dashboard_page.set_auto_connect(auto_connect)
            if auto_connect:
                self.controller._auto_attempted_session_id = None
            self.controller.do_save_server(value)
        except ValueError as exc:
            self.settings_page.show_error(str(exc))

    def _on_page_changed(self, state):
        pages = {'settings': self.settings_page, 'login': self.login_page,
                 'listing': self.dashboard_page, 'capturing': self.dashboard_page}
        self.stack.setCurrentWidget(pages[state])
        if state == 'settings':
            self.settings_page.url_edit.setText(self.controller.server_url)
            self.settings_page.auto_checkbox.setChecked(
                config_mod.load_state(self.controller.store)['auto_connect'])
            self.settings_page.url_edit.setEnabled(not self.controller.capturing)
            self.settings_page.logout_button.setEnabled(
                self.controller.token is not None and not self.controller.capturing)
            self.settings_page.show_error('')
        if state == 'login':
            self._login_timer.start()
            if self.controller.job is not None:
                self._stats_timer.start()
        else:
            self._login_timer.stop()
        if state == 'listing':
            if not self.controller.capturing:
                self.dashboard_page.stop_run()
            self._session_timer.setInterval(SESSION_POLL_INTERVAL_MS)
            self._session_timer.start()
            self._poll_sessions()
        elif state == 'capturing':
            self._session_timer.setInterval(SESSION_POLL_CAPTURING_MS)
            self._session_timer.start()
            self._stats_timer.start()
        elif state == 'settings' and self.controller.capturing:
            self._session_timer.setInterval(SESSION_POLL_CAPTURING_MS)
            self._session_timer.start()
            self._stats_timer.start()
        else:
            self._session_timer.stop()
            if self.controller.job is None:
                self._stats_timer.stop()

    def _tick_login(self):
        self.controller.run_async(self.controller.do_login_tick)

    def _poll_sessions(self):
        self.controller.run_async(self.controller.do_poll_sessions)

    def _start_capture(self, session):
        args = session.get('recorder_command_args')
        if not args:
            self.dashboard_page.set_error('当前会话无法连接')
            return
        self.controller.run_async(
            lambda: self.controller.do_start_capture(session))

    def _on_capture_finished(self, result):
        if self.controller.job is None and self.controller.state == 'login':
            self._stats_timer.stop()
        self.dashboard_page.stop_run()
        code = result.get('exit_code') if isinstance(result, dict) else None
        notes = {0: '', 2: '会话已失效，请回飞书重新入会',
                 3: '连接失败，请稍后重试', 130: '连接已中断'}
        note = notes.get(code if isinstance(code, int) else -1, '连接已断开')
        if isinstance(result, dict) and result.get('error'):
            note = result['error']
        self.dashboard_page.set_error(note)
        self._poll_sessions()


def run(argv=None):
    from PySide6.QtWidgets import QApplication
    import sys
    app = QApplication(sys.argv if argv is None else argv)
    controller = Controller(config_mod.default_store())
    window = MainWindow(controller)
    window.resize(380, 340)
    window.show()
    controller.do_boot()
    return app.exec()


def main(argv=None):
    return run(argv)
