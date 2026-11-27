"""Compact Qt views for login, settings, and the single-session monitor."""
import io

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

BASE_STYLE = '''
    QWidget { background: #f7f8fa; color: #263242; font-size: 14px; }
    QLabel { background: transparent; }
    QLineEdit { background: #fff; border: 1px solid #d7dee6;
                border-radius: 8px; padding: 8px; }
    QPushButton { border: 1px solid #d7dee6; border-radius: 8px;
                  background: #fff; padding: 6px 12px; }
    QPushButton:disabled { color: #9ca6b2; background: #f3f5f7; }
'''


def qr_pixmap(url, *, scale=12, max_size=260):
    """Render a QR pixmap that fits the compact login window."""
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
    saved = Signal(str, bool)
    logout_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(BASE_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(QLabel('服务器'))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText('http://meeting-advisor.nioint.com')
        layout.addWidget(self.url_edit)
        self.auto_checkbox = QCheckBox('发现会话后自动连接')
        layout.addWidget(self.auto_checkbox)
        self.error_label = QLabel('')
        self.error_label.setStyleSheet('color: #c6504b')
        layout.addWidget(self.error_label)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        self.logout_button = QPushButton('退出登录')
        self.logout_button.clicked.connect(self.logout_clicked.emit)
        buttons.addWidget(self.logout_button)
        buttons.addStretch(1)
        done = QPushButton('完成')
        done.clicked.connect(self._emit_saved)
        buttons.addWidget(done)
        layout.addLayout(buttons)

    def _emit_saved(self):
        self.saved.emit(self.url_edit.text().strip(), self.auto_checkbox.isChecked())

    def show_error(self, message):
        self.error_label.setText(message)


class LoginPage(QWidget):
    regenerate = Signal()
    settings_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(BASE_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        heading = QLabel('用飞书扫码登录')
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        self.qr_label = QLabel('正在生成二维码…')
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.qr_label)
        self.link_label = QLabel('')
        self.link_label.setOpenExternalLinks(True)
        self.link_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.link_label)
        self.status_label = QLabel('等待扫码…')
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)
        buttons = QHBoxLayout()
        refresh = QPushButton('刷新二维码')
        refresh.clicked.connect(self.regenerate.emit)
        buttons.addWidget(refresh)
        settings = QPushButton('设置')
        settings.clicked.connect(self.settings_clicked.emit)
        buttons.addWidget(settings)
        layout.addLayout(buttons)

    def show_login(self, login_url, device_code):
        self.qr_label.setPixmap(qr_pixmap(login_url))
        self.link_label.setText(f'<a href="{login_url}">无法扫码？在浏览器中打开</a>')
        self.set_status('等待扫码…')

    def set_status(self, text):
        self.status_label.setText(text)


class StatusOrb(QWidget):
    """Grey/no session, red/disconnected, green/connected; mic slash on mute."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(140, 140)
        self.color = '#b7c0ca'
        self.show_mic = False
        self.mic_state = 'unknown'

    def set_state(self, color, *, show_mic=False, mic_state='unknown'):
        self.color = color
        self.show_mic = show_mic
        self.mic_state = mic_state
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.color))
        painter.drawEllipse(14, 8, 106, 106)
        if self.show_mic:
            painter.setBrush(QColor('white'))
            painter.setPen(QPen(QColor('#dbe3e9'), 1))
            painter.drawEllipse(91, 91, 38, 38)
            ink = QColor('#263242' if self.mic_state in ('open', 'muted') else '#9ca6b2')
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ink)
            painter.drawRoundedRect(106, 99, 8, 16, 4, 4)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(ink, 2))
            painter.drawArc(102, 103, 16, 19, 180 * 16, 180 * 16)
            painter.drawLine(110, 119, 110, 124)
            painter.drawLine(105, 124, 115, 124)
            if self.mic_state == 'muted':
                painter.setPen(QPen(QColor('#d8645e'), 3))
                painter.drawLine(99, 125, 122, 96)
        painter.end()


class DashboardPage(QWidget):
    connect_clicked = Signal(dict)
    disconnect_clicked = Signal()
    settings_clicked = Signal()
    visual_state_changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self.auto_connect = False
        self.capturing = False
        self.connected = False
        self.offline = False
        self.error = ''
        self.mic_state = 'unknown'
        self.setStyleSheet(BASE_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 14, 22, 18)
        top = QHBoxLayout()
        top.addStretch(1)
        self.settings_button = QPushButton('⚙')
        self.settings_button.setAccessibleName('设置')
        self.settings_button.clicked.connect(self.settings_clicked.emit)
        top.addWidget(self.settings_button)
        layout.addLayout(top)
        layout.addStretch(1)
        self.meeting_label = QLabel('')
        self.meeting_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.meeting_label.setStyleSheet('color: #8b96a5;')
        layout.addWidget(self.meeting_label)
        orb_row = QHBoxLayout()
        orb_row.addStretch(1)
        self.orb = StatusOrb()
        orb_row.addWidget(self.orb)
        orb_row.addStretch(1)
        layout.addLayout(orb_row)
        self.status_label = QLabel('等待飞书会话')
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet('font-size: 20px;')
        layout.addWidget(self.status_label)
        layout.addStretch(1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.connect_button = QPushButton('连接')
        self.connect_button.clicked.connect(self._toggle_connection)
        bottom.addWidget(self.connect_button)
        layout.addLayout(bottom)
        self._render()

    def set_auto_connect(self, enabled):
        self.auto_connect = enabled
        self._render()

    def update_sessions(self, snapshot):
        sessions = snapshot.get('sessions') or []
        previous_id = self.session['session_id'] if self.session else None
        self.session = sessions[0] if len(sessions) == 1 else None
        current_id = self.session['session_id'] if self.session else None
        if current_id != previous_id:
            self.error = ''
            self.connected = False
        if self.session is not None and self.session.get('phase') != 'running':
            self.connected = False
        if len(sessions) > 1:
            self.error = '发现多个会话，请检查服务状态'
        self._render()

    def set_offline(self, offline):
        self.offline = offline
        self._render()

    def set_error(self, message):
        self.error = message
        self._render()

    def start_run(self, session):
        self.session = session
        self.capturing = True
        self.connected = False
        self.mic_state = 'unknown'
        self.error = ''
        self._render()

    def stop_run(self):
        self.capturing = False
        self.connected = False
        self.mic_state = 'unknown'
        self._render()

    def update_stats(self, stats):
        if self.session is None or stats.get('session_id') != self.session['session_id']:
            return
        sources = stats.get('sources') or {}
        self.mic_state = str(((sources.get('mic') or {}).get('capture') or {}).get('state') or 'unknown')
        self.connected = (stats.get('state') == 'running' and
                          self.mic_state in ('open', 'muted') and
                          all((sources.get(source) or {}).get('connected')
                              for source in ('loopback', 'mic')))
        self._render()

    def _toggle_connection(self):
        if self.capturing:
            self.disconnect_clicked.emit()
        elif self.session is not None:
            self.connect_clicked.emit(self.session)

    def _render(self):
        session = self.session
        if session is None:
            self.meeting_label.setText('')
            color, label = '#b7c0ca', '等待飞书会话'
        else:
            parts = [session.get('title') or '', session.get('meeting_no') or '']
            self.meeting_label.setText(' · '.join(filter(None, parts)))
            color, label = '#d8645e', '已断开'
            if session.get('phase') == 'paused':
                label = '会话已暂停'
            elif session.get('phase') == 'draining':
                label = '会话即将结束'
            elif self.capturing and not self.connected:
                label = ('设备不可用' if self.mic_state in ('no_device', 'error')
                         else '连接中…')
            if self.connected:
                color, label = '#38a976', '已入会'
        if self.offline:
            color, label = '#d8645e', '服务暂时不可达'
        elif self.error:
            color, label = '#d8645e', self.error
        self.orb.set_state(color, show_mic=bool(session and self.capturing),
                           mic_state=self.mic_state)
        self.status_label.setText(label)
        can_connect = bool(session and session.get('phase') == 'running' and
                           session.get('recorder_command_args'))
        self.connect_button.setVisible(not self.auto_connect)
        self.connect_button.setText('断开' if self.capturing else '连接')
        self.connect_button.setEnabled(self.capturing or can_connect)
        self.settings_button.setEnabled(True)
        self.visual_state_changed.emit({
            'color': color,
            'label': label,
            'title': self.meeting_label.text(),
            'capturing': self.capturing,
            'can_connect': can_connect,
        })
