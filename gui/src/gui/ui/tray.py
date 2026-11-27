"""System tray / menu-bar presence for the resident client (DESIGN §6.5).

The tray icon carries the connection state (grey/green/red) so the app can
live in the background without a taskbar/Dock window. Left click restores the
window; the context menu exposes open/connect/disconnect/settings/quit.
"""
import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

IDLE_COLOR = '#b7c0ca'
BRAND_COLOR = '#3D7BFF'

ICON_SIZES = (16, 22, 32, 44, 64)
HINT_TEXT = '会参谋已转入后台，点击托盘图标可找回'


def _icon_candidates():
    frozen = getattr(sys, '_MEIPASS', None)
    if frozen:
        yield Path(frozen) / 'assets' / 'icon.png'
    here = Path(__file__).resolve()
    yield here.parents[1] / 'assets' / 'icon.png'          # gui/src/gui/assets
    yield here.parents[4] / 'assets' / 'icon.png'          # repo root assets/


@lru_cache(maxsize=1)
def brand_pixmap():
    """Load the project logo for the window/Dock icon; None when unavailable."""
    for path in _icon_candidates():
        if not path.is_file():
            continue
        pixmap = QPixmap(str(path))
        if not pixmap.isNull():
            return pixmap
    return None


def brand_icon():
    pixmap = brand_pixmap()
    if pixmap is None:
        return tray_icon(BRAND_COLOR)
    return QIcon(pixmap)


def _draw_tile(painter, size):
    """Fallback glyph: rounded blue tile with a white caption bubble."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(BRAND_COLOR))
    radius = size * 0.22
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.setBrush(QColor('white'))
    bubble = QRectF(size * 0.22, size * 0.28, size * 0.56, size * 0.40)
    painter.drawRoundedRect(bubble, size * 0.10, size * 0.10)


def _status_pixmap(color, size):
    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    base = brand_pixmap()
    if base is not None:
        scaled = base.scaled(size, size,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap(0, 0, scaled)
    else:
        _draw_tile(painter, size)

    radius = max(2.5, size * 0.24)
    center = QPointF(size - radius - size * 0.03,
                     size - radius - size * 0.03)
    painter.setPen(QPen(QColor('white'), max(1.0, size * 0.07)))
    painter.setBrush(QColor(color))
    painter.drawEllipse(center, radius, radius)
    painter.end()
    return canvas


@lru_cache(maxsize=8)
def tray_icon(color):
    """Status icon: brand tile plus a grey/green/red badge."""
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(_status_pixmap(color, size))
    return icon


class TrayIcon(QObject):
    """QSystemTrayIcon wrapper with state-aware menu (macOS menu bar extra)."""

    open_requested = Signal()
    connect_requested = Signal()
    disconnect_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self._color = None
        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip('会参谋')
        self.tray.setIcon(tray_icon(IDLE_COLOR))

        self.menu = QMenu()
        self.open_action = self.menu.addAction('打开会参谋')
        self.open_action.triggered.connect(lambda: self.open_requested.emit())
        self.connect_action = self.menu.addAction('连接')
        self.connect_action.triggered.connect(
            lambda: self.connect_requested.emit())
        self.disconnect_action = self.menu.addAction('断开')
        self.disconnect_action.triggered.connect(
            lambda: self.disconnect_requested.emit())
        self.menu.addSeparator()
        self.settings_action = self.menu.addAction('设置')
        self.settings_action.triggered.connect(
            lambda: self.settings_requested.emit())
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction('退出')
        self.quit_action.triggered.connect(lambda: self.quit_requested.emit())
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)

        if self.available:
            self.tray.show()
        self.set_state(color=IDLE_COLOR, label='未登录')

    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_requested.emit()

    def set_state(self, *, color, label, title='', capturing=False,
                  can_connect=False):
        tooltip = f'会参谋 · {title} · {label}' if title else f'会参谋 · {label}'
        self.tray.setToolTip(tooltip)
        if color != self._color:
            self._color = color
            self.tray.setIcon(tray_icon(color))
        self.connect_action.setVisible(not capturing)
        self.connect_action.setEnabled(can_connect and not capturing)
        self.disconnect_action.setVisible(capturing)
        self.disconnect_action.setEnabled(capturing)

    def show_hint(self):
        """One-shot balloon: the window moved to the tray. True when shown."""
        if not self.available or not QSystemTrayIcon.supportsMessages():
            return False
        self.tray.showMessage('会参谋', HINT_TEXT,
                              QSystemTrayIcon.MessageIcon.Information, 4000)
        return True
