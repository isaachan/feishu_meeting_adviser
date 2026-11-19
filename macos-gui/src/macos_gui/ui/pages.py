"""Qt pages: settings / QR login / session list (PySide6).

Pages are dumb views: they emit signals and expose update methods; navigation
and business wiring live in app.Controller.
"""
import io
import sys
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)


def qr_pixmap(url, *, scale=12, max_size=400):
    """Render a QR pixmap capped at ``max_size`` (login URLs yield ~750px
    at native scale, which pushed the buttons below out of the window)."""
    import segno
    buffer = io.BytesIO()
    segno.make(url, error='m').save(buffer, kind='png', scale=scale, border=2)
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue())
    if pixmap.width() > max_size or pixmap.height() > max_size:
        pixmap = pixmap.scaled(max_size, max_size,
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
    return pixmap


class SettingsPage(QWidget):
    saved = Signal(str, bool, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('会参谋服务器地址'))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText('http://meeting-advisor.nioint.com')
        layout.addWidget(self.url_edit)
        self.auto_checkbox = QCheckBox('发现会话后自动连接声音采集')
        self.auto_checkbox.setChecked(False)
        layout.addWidget(self.auto_checkbox)
        self.mic_checkbox = QCheckBox('采集本机麦克风（跟随飞书静音状态）')
        self.mic_checkbox.setVisible(sys.platform in ('win32', 'darwin'))
        layout.addWidget(self.mic_checkbox)
        self.error_label = QLabel('')
        self.error_label.setStyleSheet('color: #b00')
        layout.addWidget(self.error_label)
        button = QPushButton('保存')
        button.clicked.connect(self._emit_saved)
        layout.addWidget(button)
        layout.addStretch(1)

    def _emit_saved(self):
        self.saved.emit(self.url_edit.text().strip(), self.auto_checkbox.isChecked(),
                        self.mic_checkbox.isChecked())

    def show_error(self, message):
        self.error_label.setText(message)


class LoginPage(QWidget):
    regenerate = Signal()
    settings_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('用飞书 App 扫码登录'))
        self.qr_label = QLabel('正在生成二维码…')
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.qr_label)
        self.link_label = QLabel('')
        self.link_label.setOpenExternalLinks(True)
        layout.addWidget(self.link_label)
        self.status_label = QLabel('等待扫码…')
        layout.addWidget(self.status_label)
        buttons = QHBoxLayout()
        refresh = QPushButton('刷新二维码')
        refresh.clicked.connect(self.regenerate.emit)
        buttons.addWidget(refresh)
        settings = QPushButton('服务器设置')
        settings.clicked.connect(self.settings_clicked.emit)
        buttons.addWidget(settings)
        layout.addLayout(buttons)
        layout.addStretch(1)

    def show_login(self, login_url, device_code):
        self.qr_label.setPixmap(qr_pixmap(login_url, max_size=260))
        short = device_code[:11] + '…'
        self.link_label.setText(
            f'<a href="{login_url}">无法扫码？点此在浏览器打开</a>（{short}）')
        self.set_status('等待扫码…')

    def set_status(self, text):
        self.status_label.setText(text)


class ListPage(QWidget):
    """Single-session capture monitor. The server remains the session authority."""
    connect_clicked = Signal(dict)
    disconnect_clicked = Signal()
    settings_clicked = Signal()
    logout_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self.auto_connect = False
        self.capturing = False
        self.offline = False
        self.error = ''
        self.mic_state = ''
        self.mic_enabled = False
        self.setStyleSheet('''
            QWidget { background: #f7f8fa; color: #253043; font-size: 13px; }
            QPushButton { border: 1px solid #d5dae2; border-radius: 8px;
                          padding: 7px 11px; background: white; }
            QPushButton:disabled { color: #9ca4af; }
        ''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        top = QHBoxLayout()
        self.user_label = QLabel('')
        top.addWidget(self.user_label)
        top.addStretch(1)
        self.settings_button = QPushButton('设置')
        self.settings_button.clicked.connect(self.settings_clicked.emit)
        top.addWidget(self.settings_button)
        self.logout_button = QPushButton('退出登录')
        self.logout_button.clicked.connect(self.logout_clicked.emit)
        top.addWidget(self.logout_button)
        layout.addLayout(top)
        self.meeting_label = QLabel('等待飞书会话')
        self.meeting_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.meeting_label)
        self.orb = QLabel('●')
        self.orb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.orb.setStyleSheet('font-size: 112px; color: #aab2bd; background: transparent;')
        layout.addWidget(self.orb, 1)
        self.mic_label = QLabel('')
        self.mic_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.mic_label)
        self.status_label = QLabel('等待会话')
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.connect_button = QPushButton('连接采集')
        self.connect_button.clicked.connect(self._toggle_capture)
        layout.addWidget(self.connect_button)
        self._render()

    def set_auto_connect(self, enabled):
        self.auto_connect = enabled
        self._render()

    def set_mic_enabled(self, enabled):
        self.mic_enabled = enabled
        self._render()

    def update_sessions(self, snapshot):
        user = snapshot.get('user') or {}
        self.user_label.setText(user.get('name') or user.get('open_id') or '')
        sessions = snapshot.get('sessions') or []
        previous_id = self.session['session_id'] if self.session else None
        self.session = sessions[0] if len(sessions) == 1 else None
        current_id = self.session['session_id'] if self.session else None
        if previous_id != current_id:
            self.error = ''
        if len(sessions) > 1:
            self.error = '发现多个会话，请检查云端单会话约束'
        self._render()

    def set_offline(self, offline):
        self.offline = offline
        self._render()

    def set_status(self, text):
        self.error = text
        self._render()

    def set_capturing(self, value):
        self.capturing = value
        if value:
            self.error = ''
            self.mic_state = ''
        self._render()

    def set_active_session(self, session):
        self.session = session
        self.set_capturing(True)

    def update_stats(self, stats):
        sources = stats.get('sources') or {}
        mic = (sources.get('mic') or {}).get('capture') or {}
        self.mic_state = {'open': '🎙 麦克风已开启',
                          'muted': '🎙 飞书已静音',
                          'unknown': '🎙 飞书静音状态未知',
                          'no_device': '🎙 无默认麦克风',
                          'error': '🎙 麦克风不可用'}.get(str(mic.get('state') or ''), '')
        self._render()

    def _toggle_capture(self):
        if self.capturing:
            self.disconnect_clicked.emit()
        elif self.session is not None:
            self.connect_clicked.emit(self.session)

    def _render(self):
        session = self.session
        if session is None:
            self.meeting_label.setText('等待飞书会话')
        else:
            self.meeting_label.setText(session.get('title') or session.get('meeting_no') or '当前会话')
        if self.offline or self.error:
            color, label = '#d55752', self.error or '服务暂时不可达，正在重试'
        elif self.capturing:
            color, label = '#38a66b', '正在采集声音'
        elif session and session.get('phase') == 'paused':
            color, label = '#aab2bd', '会话已暂停，等待恢复'
        elif session and session.get('phase') == 'running':
            color, label = '#aab2bd', '会话已就绪，尚未连接'
        else:
            color, label = '#aab2bd', '等待飞书会话'
        self.orb.setStyleSheet(f'font-size: 112px; color: {color}; background: transparent;')
        self.status_label.setText(label)
        self.mic_label.setText(
            (self.mic_state or '🎙 麦克风状态检测中') if self.capturing and self.mic_enabled
            else '🎙 麦克风未采集' if self.capturing else '')
        self.connect_button.setVisible(not self.auto_connect)
        self.settings_button.setEnabled(not self.capturing)
        self.logout_button.setEnabled(not self.capturing)
        self.connect_button.setText('断开采集' if self.capturing else '连接采集')
        self.connect_button.setEnabled(self.capturing or bool(session and session.get('phase') == 'running' and session.get('recorder_command_args')))
