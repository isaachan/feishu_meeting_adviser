"""5.12 spike: post a hotkey (e.g. Feishu global mute) via CGEventPost.

Usage:
  mute_hotkey_test.py --key a --mods cmd,shift          # Cmd+Shift+A
  mute_hotkey_test.py --key m --mods cmd,shift --tap session

Posts synthetic keyDown+keyUp with modifier flags to the HID event tap;
needs the Accessibility permission (already granted to the host terminal).
"""
import argparse
import time

from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    CGEventSourceCreate,
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskShift,
    kCGEventFlagMaskAlternate,
    kCGEventFlagMaskControl,
    kCGEventSourceStateHIDSystemState,
    kCGEventSourceStateCombinedSessionState,
)

FLAGS = {'cmd': kCGEventFlagMaskCommand, 'shift': kCGEventFlagMaskShift,
         'alt': kCGEventFlagMaskAlternate, 'ctrl': kCGEventFlagMaskControl}

# 一部分常用键的虚拟键码（HID usage）；字母/数字 = 对应 ASCII
KEYCODES = dict((chr(c), c) for c in range(ord('a'), ord('z') + 1))
KEYCODES.update(dict((str(d), d) for d in range(0, 10)))  # kVK_ANSI_0=29? 见下


def keycode_for(key: str) -> int:
    key = key.lower()
    table = {
        'a': 0, 's': 1, 'd': 2, 'f': 3, 'h': 4, 'g': 5, 'z': 6, 'x': 7,
        'c': 8, 'v': 9, 'b': 11, 'q': 12, 'w': 13, 'e': 14, 'r': 15,
        'y': 16, 't': 17, '1': 18, '2': 19, '3': 20, '4': 21, '6': 22,
        '5': 23, '=': 24, '9': 25, '7': 26, '-': 27, '8': 28, '0': 29,
        ']': 30, 'o': 31, 'u': 32, '[': 33, 'i': 34, 'p': 35, 'l': 37,
        'j': 38, "'": 39, 'k': 40, ';': 41, '\\': 42, ',': 43, '/': 44,
        'n': 45, 'm': 46, '.': 47, '`': 50, 'space': 49, 'return': 36,
        'tab': 48, 'esc': 53,
    }
    if key not in table:
        raise SystemExit(f'unknown key: {key}')
    return table[key]


def post_hotkey(key: str, mods: list[str], tap: str = 'hid', to_pid: int = 0) -> None:
    from Quartz import CGEventPostToPid
    source = CGEventSourceCreate(
        kCGEventSourceStateHIDSystemState if tap == 'hid'
        else kCGEventSourceStateCombinedSessionState)
    flags = 0
    for mod in mods:
        flags |= FLAGS[mod.lower()]
    code = keycode_for(key)
    for down in (True, False):
        event = CGEventCreateKeyboardEvent(source, code, down)
        CGEventSetFlags(event, flags)
        if to_pid:
            CGEventPostToPid(to_pid, event)
        else:
            CGEventPost(0 if tap == 'hid' else 1, event)
        time.sleep(0.05)
    mods_str = '+'.join(m.upper() for m in mods)
    target = f'pid={to_pid}' if to_pid else f'tap={tap}'
    print(f'posted {mods_str}+{key.upper()} ({target})')


def focus_meeting_app():
    """激活 Feishu Meetings 进程；返回 (前焦点 App, 会议 App)。"""
    from AppKit import NSWorkspace, NSApplicationActivateIgnoringOtherApps
    from Quartz import (CGWindowListCopyWindowInfo,
                        kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
    info = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                      kCGNullWindowID)
    pid = None
    for window in info:
        if 'feishu meetings' in str(window.get('kCGWindowOwnerName', '')).lower():
            pid = window.get('kCGWindowOwnerPID')
            break
    if pid is None:
        raise SystemExit('Feishu Meetings 进程未找到（会议未开？）')
    meeting = NSWorkspace.sharedWorkspace().runningApplications()
    meeting_app = next((a for a in meeting if a.processIdentifier() == pid), None)
    if meeting_app is None:
        raise SystemExit(f'无法取得会议 App（pid={pid}）')
    previous = NSWorkspace.sharedWorkspace().frontmostApplication()
    meeting_app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    return previous, meeting_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--key', required=True)
    parser.add_argument('--mods', default='cmd,shift')
    parser.add_argument('--tap', choices=['hid', 'session'], default='hid')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--focus', action='store_true',
                        help='activate Feishu Meetings first, restore focus after')
    parser.add_argument('--to-pid', action='store_true',
                        help='post directly to the meeting process (no focus change)')
    args = parser.parse_args()
    from AppKit import NSApplicationActivateIgnoringOtherApps
    target_pid = 0
    if args.to_pid:
        from Quartz import (CGWindowListCopyWindowInfo,
                            kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        info = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                          kCGNullWindowID)
        for window in info:
            if 'feishu meetings' in str(window.get('kCGWindowOwnerName', '')).lower():
                target_pid = int(window['kCGWindowOwnerPID'])
                break
        if not target_pid:
            raise SystemExit('Feishu Meetings 进程未找到')
    for i in range(args.repeat):
        previous = None
        if args.focus:
            previous, _ = focus_meeting_app()
            time.sleep(0.6)
        post_hotkey(args.key, args.mods.split(','), args.tap, to_pid=target_pid)
        if previous is not None:
            time.sleep(0.6)
            previous.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        if i + 1 < args.repeat:
            time.sleep(3)


if __name__ == '__main__':
    raise SystemExit(main())
