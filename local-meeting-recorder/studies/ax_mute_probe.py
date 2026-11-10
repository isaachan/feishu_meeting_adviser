"""5.12 spike: discover Feishu meeting mute-state controls via macOS AX API.

Walks the accessibility tree of Feishu / Feishu Meetings windows and prints
every element whose role/title/description mentions 静音/麦克风/mic/mute —
the goal is to learn the REAL labels on the Mac client (the Windows
reference uses 「打开麦克风」=muted / 「关闭麦克风」=unmuted).

Needs the Accessibility permission for the host terminal app.

Usage: ax_mute_probe.py [--watch N] [--depth 14] [--pid PID]
  --watch N : poll for N seconds and print a one-line state each poll
"""
import argparse
import time

from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    kAXChildrenAttribute,
    kAXDescriptionAttribute,
    kAXRoleAttribute,
    kAXRoleDescriptionAttribute,
    kAXTitleAttribute,
    kAXWindowsAttribute,
    kAXFocusedWindowAttribute,
)
from Foundation import CFGetTypeID

KEYWORDS = ('静音', '麦克风', 'mute', 'microphone', 'mic')


def as_str(value):
    if value is None:
        return None
    try:
        return str(value)
    except Exception:
        return None


def get(element, attribute, default=None):
    code, value = AXUIElementCopyAttributeValue(element, attribute, None)
    if code == 0:
        return value
    return default


def feishu_pids():
    from Quartz import kCGWindowListOptionAll, kCGWindowListOptionOnScreenOnly
    from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID
    from Foundation import NSBundle

    info = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | 0x1,
                                      kCGNullWindowID)
    pids = {}
    for window in info:
        owner = window.get('kCGWindowOwnerName', '')
        pid = window.get('kCGWindowOwnerPID')
        title = window.get('kCGWindowName', '') or ''
        low = str(owner).lower()
        if any(k in low for k in ('feishu', 'lark', '飞书')):
            pids.setdefault(pid, {'owner': str(owner), 'titles': set()})
            if title:
                pids[pid]['titles'].add(str(title))
    return pids


def interesting(element, path):
    role = as_str(get(element, kAXRoleAttribute)) or ''
    title = as_str(get(element, kAXTitleAttribute)) or ''
    desc = as_str(get(element, kAXDescriptionAttribute)) or ''
    role_desc = as_str(get(element, kAXRoleDescriptionAttribute)) or ''
    blob = f'{title} {desc} {role_desc}'.lower()
    hit = any(k in blob for k in KEYWORDS)
    return hit, {'role': role, 'title': title, 'desc': desc,
                 'role_desc': role_desc}


def walk(element, depth, max_depth, out):
    if depth > max_depth:
        return
    hit, info = interesting(element, depth)
    if hit:
        out.append((depth, info))
    children = get(element, kAXChildrenAttribute, [])
    if children is None:
        return
    for child in children:
        try:
            walk(child, depth + 1, max_depth, out)
        except Exception:
            continue


def scan(pid, max_depth):
    app = AXUIElementCreateApplication(pid)
    windows = get(app, kAXWindowsAttribute, []) or []
    results = []
    for index, window in enumerate(windows):
        title = as_str(get(window, kAXTitleAttribute)) or '(no title)'
        out = []
        walk(window, 0, max_depth, out)
        results.append((title, out))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--watch', type=float, default=0)
    parser.add_argument('--depth', type=int, default=14)
    parser.add_argument('--pid', type=int, default=0)
    args = parser.parse_args()

    # permission check
    from ApplicationServices import AXIsProcessTrustedWithOptions
    from Foundation import NSDictionary
    trusted = AXIsProcessTrustedWithOptions(
        NSDictionary.dictionaryWithObject_forKey_(True, 'AXTrustedCheckOptionPrompt'))
    if not trusted:
        print('NOT TRUSTED: grant Accessibility to the host terminal app '
              '(System Settings > Privacy & Security > Accessibility), '
              'then restart the terminal and retry')
        return 2

    pids = feishu_pids()
    if not pids:
        print('no Feishu/Lark window owners found')
        return 1
    for pid, info in pids.items():
        print(f'pid={pid} owner={info["owner"]} titles={sorted(info["titles"])[:3]}')

    deadline = time.monotonic() + (args.watch or 0)
    while True:
        for pid in pids:
            if args.pid and pid != args.pid:
                continue
            for title, out in scan(pid, args.depth):
                print(f'--- pid {pid} window: {title} ({len(out)} hits) ---')
                for depth, info in out:
                    print(f'  d{depth:02d} role={info["role"]} '
                          f'title={info["title"]!r} desc={info["desc"]!r} '
                          f'role_desc={info["role_desc"]!r}')
        if not args.watch:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
        print('=====')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
