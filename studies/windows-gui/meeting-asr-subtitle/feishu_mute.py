"""检测飞书会议里「自己」的麦克风是否静音（纯 UI Automation，不做图像识别）。

可用信号（按优先级）：

1. 悬浮工具栏 `MiniDialogMicrophoneButton`
   - Name = 打开麦克风 -> 当前静音
   - Name = 关闭麦克风 -> 当前开麦
2. 完整会议窗口参会人列表里「自己」那一行
   - 行内按钮 打开麦克风 / 关闭麦克风
3. 自己的视频小窗 `ParticipantViewMicrophoneButton` / `ParticipantMicrophoneIcon`
   - Name 明确时使用；Name = 麦克风 时视为未知
4. 小尺寸会议窗口（悬浮窗）内任意 Name = 打开/关闭麦克风 的控件

状态处理：

- 防抖：连续 CONFIRM_POLLS 次结果一致才切换
- 保持：短暂读不到时沿用上一次已知状态 HOLD_SECONDS 秒
- 仍然未知：返回 known=False，由调用方决定放行（当前为放行）
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from typing import Optional

try:
    import uiautomation as auto
except Exception:
    auto = None

MUTED_BUTTON = "打开麦克风"       # 当前静音（点击会开麦）
UNMUTED_BUTTON = "关闭麦克风"     # 当前开麦（点击会静音）
MEETING_HINTS = ("飞书会议", "Feishu Meeting", "Lark Meeting")
BROWSER_HINTS = ("google chrome", "microsoft edge", "firefox", "豆包", "doubao")
SELF_NAME_ENV = "FEISHU_SELF_NAME"

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_name(pid: int) -> str:
    """取进程可执行文件名（小写）；失败返回空串。"""
    if not pid:
        return ""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""

CONFIRM_POLLS = 2
HOLD_SECONDS = 8.0


def _display_name() -> str:
    env = os.environ.get(SELF_NAME_ENV, "").strip()
    if env:
        return env
    try:
        buf = ctypes.create_unicode_buffer(257)
        size = ctypes.c_ulong(257)
        if ctypes.windll.secur32.GetUserNameExW(3, buf, ctypes.byref(size)):
            return buf.value.strip()
    except Exception:
        pass
    return ""


def _norm(text: str) -> str:
    return "".join((text or "").split()).lower()


def _name_state(name: str) -> Optional[bool]:
    name = (name or "").strip()
    if name == MUTED_BUTTON:
        return True
    if name == UNMUTED_BUTTON:
        return False
    return None


class FeishuMuteDetector:
    def __init__(self, interval: float = 0.3):
        self.interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._display = _display_name()

        self._muted: Optional[bool] = None
        self._known = False
        self._detail = "未启动"
        self._source = "none"
        self._in_meeting = False
        self._updated_at = 0.0

        self._pending_key = None
        self._pending_count = 0
        self._last_known_muted: Optional[bool] = None
        self._last_known_at = 0.0

    # ---------------- 对外 ----------------

    def start(self) -> None:
        if self._running or auto is None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="feishu-mute", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def get_state(self) -> dict:
        with self._lock:
            return {
                "muted": self._muted,
                "known": self._known,
                "detail": self._detail,
                "source": self._source,
                "in_meeting": self._in_meeting,
                "updated_at": self._updated_at,
            }

    # ---------------- 状态机 ----------------

    def _commit(self, muted: Optional[bool], source: str, detail: str, in_meeting: bool) -> None:
        with self._lock:
            self._muted = muted
            self._known = muted is not None
            self._detail = detail
            self._source = source
            self._in_meeting = in_meeting
            self._updated_at = time.time()

    def _apply(self, raw: Optional[bool], source: str, detail: str, in_meeting: bool) -> None:
        now = time.monotonic()
        if raw is None:
            # 短暂读不到：沿用上一次已知状态
            with self._lock:
                last_muted = self._last_known_muted
                last_at = self._last_known_at
            if last_muted is not None and now - last_at <= HOLD_SECONDS:
                self._commit(last_muted, "hold", "状态读取失败，沿用上次结果", in_meeting)
            else:
                self._commit(None, source, detail, in_meeting)
            self._pending_key = None
            self._pending_count = 0
            return

        with self._lock:
            self._last_known_muted = raw
            self._last_known_at = now

        key = (raw, source)
        if key == self._pending_key:
            self._pending_count += 1
        else:
            self._pending_key = key
            self._pending_count = 1
        if self._pending_count < CONFIRM_POLLS:
            return

        self._commit(raw, source, detail, in_meeting)

    # ---------------- UI 遍历 ----------------

    def _meeting_windows(self):
        result = []
        if auto is None:
            return result
        try:
            children = auto.GetRootControl().GetChildren()
        except Exception:
            return result
        for win in children:
            try:
                name = win.Name or ""
                rect = win.BoundingRectangle
                if not any(h in name for h in MEETING_HINTS):
                    continue
                if rect.width() <= 0 or rect.height() <= 0:
                    continue
                # 排除浏览器标签页（例如「飞书会议语音转文字功能 - 豆包 - Google Chrome」）
                low = name.lower()
                if any(b in low for b in BROWSER_HINTS):
                    continue
                # 只认飞书/Feishu/Lark 进程自己的窗口
                try:
                    proc = _process_name(int(win.ProcessId))
                except Exception:
                    proc = ""
                if proc and "feishu" not in proc and "lark" not in proc:
                    continue
                result.append(win)
            except Exception:
                continue
        return result

    @staticmethod
    def _walk(ctrl, depth=0, max_depth=12):
        if depth > max_depth:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for c in children:
            yield c
            yield from FeishuMuteDetector._walk(c, depth + 1, max_depth)

    def _find_by_class(self, win, class_names):
        found = []
        for c in self._walk(win):
            try:
                if (c.ClassName or "") in class_names:
                    found.append(c)
            except Exception:
                continue
        return found

    def _find_rows(self, win):
        rows = []
        for c in self._walk(win):
            try:
                if c.ControlTypeName == "ListItemControl" and (c.Name or "").strip():
                    rect = c.BoundingRectangle
                    if rect.width() > 0 and rect.height() > 0 and self._mic_action(c) is not None:
                        rows.append(c)
            except Exception:
                continue
        return rows

    def _mic_action(self, row) -> Optional[bool]:
        found = None
        stack = [row]
        while stack:
            c = stack.pop(0)
            try:
                st = _name_state(c.Name)
                if st is True:
                    return True
                if st is False:
                    found = False
                stack.extend(c.GetChildren())
            except Exception:
                continue
        return found

    def _match_self(self, rows):
        if not self._display:
            return None
        target = _norm(self._display)
        for row in rows:
            try:
                name = _norm(row.Name)
            except Exception:
                continue
            if name and (target in name or name in target):
                return row
        return None

    def _detect_raw(self):
        windows = self._meeting_windows()
        if not windows:
            return None, "none", "未找到飞书会议窗口", False

        # 1. 悬浮工具栏
        for win in windows:
            for c in self._find_by_class(win, {"MiniDialogMicrophoneButton"}):
                try:
                    st = _name_state(c.Name)
                except Exception:
                    st = None
                if st is not None:
                    return st, "floating_toolbar", f"悬浮工具栏：{c.Name}", True

        # 2. 参会人列表里自己的行
        for win in windows:
            rows = self._find_rows(win)
            self_row = self._match_self(rows)
            if self_row is not None:
                st = self._mic_action(self_row)
                if st is not None:
                    return st, "participant_row", f"参会人：{self_row.Name}", True

        # 3. 自己的视频小窗图标
        for win in windows:
            for c in self._find_by_class(
                win, {"ParticipantViewMicrophoneButton", "ParticipantMicrophoneIcon"}
            ):
                try:
                    st = _name_state(c.Name)
                except Exception:
                    st = None
                if st is not None:
                    return st, "participant_view", f"视频小窗：{c.Name}", True

        # 4. 小尺寸窗口（悬浮窗）内的通用名称匹配
        for win in windows:
            try:
                rect = win.BoundingRectangle
            except Exception:
                continue
            if rect.width() <= 800 and rect.height() <= 500:
                for c in self._walk(win):
                    try:
                        st = _name_state(c.Name)
                    except Exception:
                        continue
                    if st is not None:
                        return st, "small_window", f"悬浮窗：{c.Name}", True

        return None, "unknown", "未找到可用的麦克风状态控件", True

    def _loop(self) -> None:
        try:
            with auto.UIAutomationInitializerInThread():
                while self._running:
                    try:
                        raw, source, detail, in_meeting = self._detect_raw()
                        self._apply(raw, source, detail, in_meeting)
                    except Exception as exc:
                        self._apply(None, "error", f"检测异常：{type(exc).__name__}: {exc}", False)
                    time.sleep(self.interval)
        except Exception as exc:
            self._commit(None, "error", f"UIA 初始化失败：{type(exc).__name__}: {exc}", False)


detector = FeishuMuteDetector()


def start() -> None:
    detector.start()


def stop() -> None:
    detector.stop()


def get_state() -> dict:
    return detector.get_state()
