"""Qt application glue: controller (state machine) + main window.

Synchronous core methods (do_*) are headless-testable; their async wrappers
(run in a worker thread, marshal back via queued signals) plus QTimer wiring
make the UI responsive. HTTP calls block only inside the worker thread.
"""
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMainWindow, QStackedWidget

from . import config as config_mod
from .api import ApiError, GuiApiClient, NetworkError
from .login import LoginFlow
from .sessions import normalize_sessions
from .ui.pages import ListPage, LoginPage, MonitorPage, SettingsPage

LOGIN_POLL_INTERVAL_MS = 2500
SESSION_POLL_INTERVAL_MS = 10_000
SESSION_POLL_CAPTURING_MS = 5_000
CAPTURE_STATS_INTERVAL_MS = 1_000


class Controller(QObject):
    """Page state machine: settings -> login -> listing -> capturing."""

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
        self._worker_lock = threading.Lock()

    @property
    def capturing(self):
        return self.job is not None and not self.job.finished

    # -- synchronous core (tests call these directly) -------------------------

    @property
    def server_url(self):
        return config_mod.load_state(self.store)['server_url']

    def do_boot(self):
        state = config_mod.load_state(self.store)
        if state['server_url'] is None:
            self._enter('settings')
            return
        self._connect()
        if state['device_token'] is None:
            self._start_login()
        else:
            self.token = state['device_token']
            self._enter('listing')

    def do_save_server(self, url):
        try:
            config_mod.save_server_url(self.store, url)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        self._connect()
        self._start_login()

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
        return snapshot

    def _auto_stop_ended_session(self, snapshot):
        """会话结束联动：采集中的会话不再 recording 时自动干净停止。"""
        job = self.job
        if job is None or job.finished:
            return
        live = {s['session_id'] for s in snapshot.get('sessions', [])
                if s['phase'] == 'recording'}
        if job.session_id not in live:
            job.request_stop()

    def do_logout(self):
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
        args = session_item.get('recorder_command_args')
        if not isinstance(args, dict) or not args.get('session'):
            return None
        factory = self.job_factory or self._default_job
        out_root = self.capture_out_root or _default_capture_root()
        job = factory(args, out_root)
        self.job = job
        job.start()
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
        self.setWindowTitle('会参谋 · 采集')
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.settings_page = SettingsPage()
        self.login_page = LoginPage()
        self.list_page = ListPage()
        self.monitor_page = MonitorPage()
        for page in (self.settings_page, self.login_page, self.list_page,
                     self.monitor_page):
            self.stack.addWidget(page)
        if controller.server_url:
            self.settings_page.url_edit.setText(controller.server_url)

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
        controller.sessions_updated.connect(self.list_page.update_sessions)
        controller.offline_changed.connect(
            lambda offline: self.list_page.set_status(
                '离线，重试中…' if offline else '已连接'))
        controller.capture_started.connect(self.monitor_page.start_run)
        controller.capture_stats.connect(self.monitor_page.update_stats)
        controller.capture_finished.connect(self._on_capture_finished)
        self.settings_page.saved.connect(self._save_server)
        self.login_page.regenerate.connect(
            lambda: controller.run_async(controller.do_start_login))
        self.list_page.refresh_clicked.connect(self._poll_sessions)
        self.list_page.logout_clicked.connect(
            lambda: controller.run_async(controller.do_logout))
        self.list_page.start_capture.connect(self._start_capture)
        self.monitor_page.stop_clicked.connect(self.controller.do_stop_capture)
        self.monitor_page.back_clicked.connect(
            lambda: self._on_page_changed('listing'))

    def closeEvent(self, event):
        """D7：采集中关窗 = 隐藏窗口继续采集；无采集 = 正常退出。"""
        if self.controller.capturing:
            event.ignore()
            self.hide()
        else:
            event.accept()

    def _save_server(self, url):
        try:
            self.controller.do_save_server(url)
        except ValueError as exc:
            self.settings_page.show_error(str(exc))

    def _on_page_changed(self, state):
        pages = {'settings': self.settings_page, 'login': self.login_page,
                 'listing': self.list_page, 'capturing': self.monitor_page}
        self.stack.setCurrentWidget(pages[state])
        if state == 'login':
            self._login_timer.start()
        else:
            self._login_timer.stop()
        if state == 'listing':
            self._session_timer.setInterval(SESSION_POLL_INTERVAL_MS)
            self._session_timer.start()
            self._poll_sessions()
        elif state == 'capturing':
            self._session_timer.setInterval(SESSION_POLL_CAPTURING_MS)
            self._session_timer.start()
            self._stats_timer.start()
        else:
            self._session_timer.stop()
            self._stats_timer.stop()

    def _tick_login(self):
        self.controller.run_async(self.controller.do_login_tick)

    def _poll_sessions(self):
        self.controller.run_async(self.controller.do_poll_sessions)

    def _start_capture(self, session):
        args = session.get('recorder_command_args')
        if not args:
            self.list_page.set_status('该会话已排空，无法采集')
            return
        self.controller.run_async(
            lambda: self.controller.do_start_capture(session))

    def _on_capture_finished(self, result):
        code = result.get('exit_code') if isinstance(result, dict) else None
        notes = {0: '采集已停止（干净收尾）', 2: '会话已失效，请回飞书重新入会',
                 3: '连接不可恢复，采集已停止', 130: '采集已中断'}
        note = notes.get(code) if isinstance(code, int) else None
        if note is None:
            note = '采集已结束'
        self.list_page.set_status(note)
        self._poll_sessions()


def run(argv=None):
    from PySide6.QtWidgets import QApplication
    import sys
    app = QApplication(sys.argv if argv is None else argv)
    controller = Controller(config_mod.default_store())
    window = MainWindow(controller)
    window.resize(560, 640)
    window.show()
    controller.do_boot()
    return app.exec()


def main(argv=None):
    return run(argv)
