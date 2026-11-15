"""Read the local user's Feishu meeting mute state via Windows UI Automation.

Unknown and stale readings are deliberately not interpreted as unmuted.
This module only detects state; the capture source must enforce the gate.
"""
import ctypes
from ctypes import wintypes
import os
import threading
import time

MEETING_HINTS = ('飞书会议', 'Feishu Meeting', 'Lark Meeting')
BROWSER_HINTS = ('google chrome', 'microsoft edge', 'firefox', '豆包', 'doubao')


def button_state(name):
    """Return current mute state; button text describes the opposite action."""
    return {'打开麦克风': True, '关闭麦克风': False}.get((name or '').strip())


def _process_name(pid):
    if not pid:
        return ''
    try:
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                                      wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ''
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit('\\', 1)[-1].lower()
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        pass
    return ''


def _self_name():
    name = os.environ.get('FEISHU_SELF_NAME', '').strip()
    if name:
        return name
    try:
        buf = ctypes.create_unicode_buffer(257)
        size = ctypes.c_ulong(len(buf))
        if ctypes.windll.secur32.GetUserNameExW(3, buf, ctypes.byref(size)):
            return buf.value.strip()
    except Exception:
        pass
    return ''


def _walk(control, depth=0):
    if depth > 12:
        return
    try:
        children = control.GetChildren()
    except Exception:
        return
    for child in children:
        yield child
        yield from _walk(child, depth + 1)


def _normal(name):
    return ''.join((name or '').split()).lower()


def detect_raw(auto, self_name):
    """Return (muted or None, source, detail, meeting window found)."""
    windows = []
    for win in auto.GetRootControl().GetChildren():
        try:
            name = win.Name or ''
            rect = win.BoundingRectangle
            if not any(hint in name for hint in MEETING_HINTS):
                continue
            if any(hint in name.lower() for hint in BROWSER_HINTS):
                continue
            if rect.width() <= 0 or rect.height() <= 0:
                continue
            proc = _process_name(int(win.ProcessId))
            if proc and 'feishu' not in proc and 'lark' not in proc:
                continue
            windows.append(win)
        except Exception:
            continue
    if not windows:
        return None, 'none', '未找到飞书会议窗口', False

    # Snapshot descendants once per poll; UIA calls can fail as windows change.
    trees = [(win, list(_walk(win))) for win in windows]
    for win, children in trees:
        for child in children:
            try:
                if child.ClassName == 'MiniDialogMicrophoneButton':
                    state = button_state(child.Name)
                    if state is not None:
                        return state, 'floating_toolbar', child.Name, True
            except Exception:
                continue
    target = _normal(self_name)
    if target:
        for win, children in trees:
            for row in children:
                try:
                    name = _normal(row.Name)
                    if row.ControlTypeName != 'ListItemControl' or not name:
                        continue
                    if target not in name and name not in target:
                        continue
                    rect = row.BoundingRectangle
                    if rect.width() <= 0 or rect.height() <= 0:
                        continue
                    states = [button_state(c.Name) for c in _walk(row)]
                    if True in states:
                        return True, 'participant_row', row.Name, True
                    if False in states:
                        return False, 'participant_row', row.Name, True
                except Exception:
                    continue
    for win, children in trees:
        for child in children:
            try:
                if child.ClassName in ('ParticipantViewMicrophoneButton', 'ParticipantMicrophoneIcon'):
                    state = button_state(child.Name)
                    if state is not None:
                        return state, 'participant_view', child.Name, True
            except Exception:
                continue
    for win, children in trees:
        try:
            rect = win.BoundingRectangle
            if rect.width() > 800 or rect.height() > 500:
                continue
            for child in children:
                state = button_state(child.Name)
                if state is not None:
                    return state, 'small_window', child.Name, True
        except Exception:
            continue
    return None, 'unknown', '未找到可用的麦克风状态控件', True


class FeishuMuteDetector:
    """Poll UIA and publish a fail-closed, debounced snapshot."""

    def __init__(self, *, interval=0.3, stale_after=2.0, clock=time.monotonic):
        self.interval = interval
        self.stale_after = stale_after
        self.clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._state = {'muted': None, 'known': False, 'source': 'none',
                       'detail': '未启动', 'in_meeting': False, 'updated_at': 0.0}
        self._pending = None
        self._count = 0

    def apply(self, muted, source, detail, in_meeting):
        now = self.clock()
        with self._lock:
            key = (muted, source)
            if muted is None:
                self._pending, self._count = None, 0
                self._state = dict(muted=None, known=False, source=source,
                                   detail=detail, in_meeting=in_meeting, updated_at=now)
                return
            if key == self._pending:
                self._count += 1
            else:
                self._pending, self._count = key, 1
            # A new raw signal immediately closes the gate until confirmed.
            if self._count < 2:
                self._state = dict(muted=None, known=False, source=source,
                                   detail='等待状态确认', in_meeting=in_meeting, updated_at=now)
            else:
                self._state = dict(muted=muted, known=True, source=source,
                                   detail=detail, in_meeting=in_meeting, updated_at=now)

    def get_state(self):
        with self._lock:
            state = dict(self._state)
        if self.clock() - state['updated_at'] > self.stale_after:
            state.update(muted=None, known=False, source='stale', detail='检测状态已过期')
        return state

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='feishu-mute', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.apply(None, 'stopped', '检测已停止', False)

    def _loop(self):
        try:
            import uiautomation as auto
            with auto.UIAutomationInitializerInThread():
                while not self._stop.is_set():
                    try:
                        self.apply(*detect_raw(auto, _self_name()))
                    except Exception as exc:
                        self.apply(None, 'error', str(exc), False)
                    self._stop.wait(self.interval)
        except Exception as exc:
            self.apply(None, 'error', str(exc), False)
