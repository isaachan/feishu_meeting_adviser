"""Qt application glue: controller (state machine) + main window.

Synchronous core methods (do_*) are headless-testable; their async wrappers
(run in a worker thread, marshal back via queued signals) plus QTimer wiring
make the UI responsive. HTTP calls block only inside the worker thread.
"""
import threading

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMainWindow, QStackedWidget

from . import config as config_mod
from .api import ApiError, GuiApiClient, NetworkError
from .login import LoginFlow
from .sessions import normalize_sessions
from .ui.pages import ListPage, LoginPage, SettingsPage

LOGIN_POLL_INTERVAL_MS = 2500
SESSION_POLL_INTERVAL_MS = 10_000


class Controller(QObject):
    """Page state machine: settings -> login -> listing (-> capturing in G5)."""

    page_changed = Signal(str)          # settings|login|listing
    login_started = Signal(dict)        # {login_url, device_code, expires_at}
    login_status = Signal(str)          # human text for the login page
    sessions_updated = Signal(dict)     # normalized snapshot
    offline_changed = Signal(bool)

    def __init__(self, store, *, client_factory=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.client_factory = client_factory or (lambda: GuiApiClient(self.server_url))
        self.state = 'settings'
        self.client = None
        self.flow = None
        self.token = None
        self.offline = False
        self._worker_lock = threading.Lock()

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
        self.sessions_updated.emit(snapshot)
        return snapshot

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
        for page in (self.settings_page, self.login_page, self.list_page):
            self.stack.addWidget(page)
        if controller.server_url:
            self.settings_page.url_edit.setText(controller.server_url)

        self._login_timer = QTimer(self)
        self._login_timer.setInterval(LOGIN_POLL_INTERVAL_MS)
        self._login_timer.timeout.connect(self._tick_login)
        self._session_timer = QTimer(self)
        self._session_timer.setInterval(SESSION_POLL_INTERVAL_MS)
        self._session_timer.timeout.connect(self._poll_sessions)

        controller.page_changed.connect(self._on_page_changed)
        controller.login_started.connect(
            lambda start: self.login_page.show_login(start['login_url'],
                                                     start['device_code']))
        controller.login_status.connect(self.login_page.set_status)
        controller.sessions_updated.connect(self.list_page.update_sessions)
        controller.offline_changed.connect(
            lambda offline: self.list_page.set_status(
                '离线，重试中…' if offline else '已连接'))
        self.settings_page.saved.connect(self._save_server)
        self.login_page.regenerate.connect(
            lambda: controller.run_async(controller.do_start_login))
        self.list_page.refresh_clicked.connect(self._poll_sessions)
        self.list_page.logout_clicked.connect(
            lambda: controller.run_async(controller.do_logout))
        self.list_page.start_capture.connect(self._start_capture)

    def _save_server(self, url):
        try:
            self.controller.do_save_server(url)
        except ValueError as exc:
            self.settings_page.show_error(str(exc))

    def _on_page_changed(self, state):
        self.stack.setCurrentWidget({'settings': self.settings_page,
                                     'login': self.login_page,
                                     'listing': self.list_page}[state])
        if state == 'login':
            self._login_timer.start()
        else:
            self._login_timer.stop()
        if state == 'listing':
            self._session_timer.start()
            self._poll_sessions()
        else:
            self._session_timer.stop()

    def _tick_login(self):
        self.controller.run_async(self.controller.do_login_tick)

    def _poll_sessions(self):
        self.controller.run_async(self.controller.do_poll_sessions)

    def _start_capture(self, session):
        # G5 wires capture; the button is only meaningful with a real session.
        self.list_page.set_status(
            f"采集功能尚未接入（已选 {session['session_id']}）")


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
