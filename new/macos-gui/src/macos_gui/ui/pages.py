"""Qt pages: settings / QR login / session list (PySide6).

Pages are dumb views: they emit signals and expose update methods; navigation
and business wiring live in app.Controller.
"""
import io
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QVBoxLayout, QWidget)


def qr_pixmap(url, *, scale=8):
    import segno
    buffer = io.BytesIO()
    segno.make(url, error='m').save(buffer, kind='png', scale=scale, border=2)
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue())
    return pixmap


class SettingsPage(QWidget):
    saved = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('会参谋服务器地址（一次性设置）'))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText('https://adviser.example.internal')
        layout.addWidget(self.url_edit)
        self.error_label = QLabel('')
        self.error_label.setStyleSheet('color: #b00')
        layout.addWidget(self.error_label)
        button = QPushButton('保存')
        button.clicked.connect(self._emit_saved)
        layout.addWidget(button)
        layout.addStretch(1)

    def _emit_saved(self):
        self.saved.emit(self.url_edit.text().strip())

    def show_error(self, message):
        self.error_label.setText(message)


class LoginPage(QWidget):
    regenerate = Signal()

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
        refresh = QPushButton('刷新二维码')
        refresh.clicked.connect(self.regenerate.emit)
        layout.addWidget(refresh)
        layout.addStretch(1)

    def show_login(self, login_url, device_code):
        self.qr_label.setPixmap(qr_pixmap(login_url))
        short = device_code[:11] + '…'
        self.link_label.setText(
            f'<a href="{login_url}">无法扫码？点此在浏览器打开</a>（{short}）')
        self.set_status('等待扫码…')

    def set_status(self, text):
        self.status_label.setText(text)


class ListPage(QWidget):
    refresh_clicked = Signal()
    start_capture = Signal(dict)
    logout_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.user_label = QLabel('')
        header.addWidget(self.user_label)
        header.addStretch(1)
        refresh = QPushButton('刷新')
        refresh.clicked.connect(self.refresh_clicked.emit)
        header.addWidget(refresh)
        logout = QPushButton('退出登录')
        logout.clicked.connect(self.logout_clicked.emit)
        header.addWidget(logout)
        layout.addLayout(header)
        self.session_list = QListWidget()
        self.session_list.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.session_list)
        footer = QHBoxLayout()
        self.status_label = QLabel('')
        footer.addWidget(self.status_label)
        footer.addStretch(1)
        self.start_button = QPushButton('开始采集')
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self._emit_start)
        footer.addWidget(self.start_button)
        layout.addLayout(footer)

    def _selected_session(self):
        items = self.session_list.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def _emit_start(self):
        session = self._selected_session()
        if session is not None:
            self.start_capture.emit(session)

    def _update_buttons(self):
        self.start_button.setEnabled(self._selected_session() is not None)

    def set_status(self, text):
        self.status_label.setText(text)

    def update_sessions(self, snapshot):
        user = snapshot.get('user') or {}
        name = user.get('name') or user.get('open_id') or ''
        email = user.get('email') or ''
        self.user_label.setText(f'{name}（{email}）' if email else name)
        selected = self._selected_session()
        self.session_list.clear()
        for item in snapshot.get('sessions', []):
            phase_text = '可采集' if item['phase'] == 'recording' else '排空中'
            started = _format_started(item.get('started_at'))
            parts = [item['title'], item['meeting_no']]
            if started:
                parts.append(started)
            parts.append(phase_text)
            row = QListWidgetItem('  ·  '.join(parts))
            row.setData(Qt.ItemDataRole.UserRole, item)
            if item['phase'] != 'recording':
                row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled)
            self.session_list.addItem(row)
            if selected and selected['session_id'] == item['session_id']:
                self.session_list.setCurrentItem(row)
        self._update_buttons()


def _format_started(started_at):
    if not isinstance(started_at, (int, float)):
        return ''
    return time.strftime('%H:%M 开始', time.localtime(started_at))
