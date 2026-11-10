"""5.12 live demo: watch Feishu mic mute state in real time (v3, rewritten).

Proven components only:
- capture every on-screen Feishu Meetings window by window ID
- BGRA->RGB swap, per-window analysis, pick the frame with the strongest
  red/green signal (twin windows share bounds; z-order flaps)
- state: MUTED (red mic button) / UNMUTED (+speaking when green fill)
- optional hover-nudge (rate-limited, cursor restored) when no window shows
  a control bar (both main toolbar and mini bar hide without the mouse)

Usage: mute_demo.py [--duration 120] [--interval 0.5] [--verbose]
                    [--no-voice] [--no-hover] [--snap PNG]
"""
import argparse
import subprocess
import time

import numpy as np
from AppKit import NSEvent
from Quartz import (
    CGDataProviderCopyData,
    CGDisplayBounds,
    CGEventCreateMouseEvent,
    CGEventPost,
    CGImageGetBitsPerPixel,
    CGImageGetBytesPerRow,
    CGImageGetDataProvider,
    CGImageGetHeight,
    CGImageGetWidth,
    CGMainDisplayID,
    CGPoint,
    kCGEventMouseMoved,
    kCGHIDEventTap,
    CGPreflightScreenCaptureAccess,
    CGWindowListCopyWindowInfo,
    CGWindowListCreateImage,
    CGRectNull,
    kCGNullWindowID,
    kCGWindowListOptionOnScreenOnly,
)

OWNER = 'feishu meetings'


def meeting_windows():
    info = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                      kCGNullWindowID)
    out = []
    for window in info:
        if OWNER not in str(window.get('kCGWindowOwnerName', '')).lower():
            continue
        bounds = window.get('kCGWindowBounds')
        if not bounds or bounds.get('Width', 0) < 60:
            continue
        out.append((window, bounds))
    return out


def rgb_frame(wid):
    img = CGWindowListCreateImage(CGRectNull, 1 << 3, wid, 0)
    if img is None:
        return None
    w, h = CGImageGetWidth(img), CGImageGetHeight(img)
    bpr, bpp = CGImageGetBytesPerRow(img), CGImageGetBitsPerPixel(img)
    data = CGDataProviderCopyData(CGImageGetDataProvider(img))
    arr = np.frombuffer(bytes(data), dtype=np.uint8).reshape(h, bpr // (bpp // 8), 4)
    return arr[:, :w, :3][:, :, ::-1].astype(int)  # BGRA storage -> RGB


def mode_for(windows):
    widest = max(b['Width'] for _, b in windows)
    return 'main' if widest > 400 else 'mini'


def analyze(frame, mode):
    h, w = frame.shape[:2]
    zone = frame[int(h * 0.90):, int(w * 0.38):int(w * 0.56)] if mode == 'main' else frame
    r, g, b = zone[:, :, 0], zone[:, :, 1], zone[:, :, 2]
    red = int((((r > 110) & (r - g > 40) & (r - b > 40))).sum())
    green = int((((g > 110) & (g - r > 35) & (g - b > 35))).sum())
    bright = round(float(zone.mean()), 1)
    if mode == 'main' and bright > 150:
        # 工具栏条应是暗色；亮 = 是视频画面（工具栏隐藏）
        state = 'UNKNOWN(toolbar-hidden)'
    elif red > 150:
        state = 'MUTED'
    elif green > 150:
        state = 'UNMUTED+speaking'
    else:
        state = 'UNMUTED'
    return {'state': state, 'red': red, 'green': green, 'bright': bright}


def sample():
    """-> (mode, result, any_window_bounds) 最好的一帧分析结果。"""
    windows = meeting_windows()
    if not windows:
        return None, {'state': 'no-meeting-window', 'red': -1, 'green': -1,
                      'bright': 0}, None
    mode = mode_for(windows)
    best = None
    for window, bounds in windows:
        frame = rgb_frame(int(window['kCGWindowNumber']))
        if frame is None:
            continue
        result = analyze(frame, mode)
        score = (result['red'] + result['green'], -result['bright'])
        if best is None or score > best[0]:
            best = (score, result, bounds)
    if best is None:
        return mode, {'state': 'capture-failed', 'red': -1, 'green': -1,
                      'bright': 0}, None
    return mode, best[1], best[2]


def move_cursor(point):
    CGEventPost(kCGHIDEventTap,
                CGEventCreateMouseEvent(None, kCGEventMouseMoved, point, 0))


def hover_nudge(bounds):
    saved = NSEvent.mouseLocation()
    screen = CGDisplayBounds(CGMainDisplayID())
    center = CGPoint(bounds['X'] + bounds['Width'] / 2,
                     screen.size.height - bounds['Y'] - bounds['Height'] / 2)
    move_cursor(center)
    time.sleep(0.45)
    move_cursor(CGPoint(saved.x, saved.y))


def speak(text, enabled):
    if enabled:
        subprocess.Popen(['say', '-v', 'Tingting', text])


def save_png(frame, path):
    from Quartz import (CGImageDestinationCreateWithURL, CGImageDestinationAddImage,
                        CGImageDestinationFinalize, CGDataProviderCreateWithCFData,
                        CGImageCreate, CGColorSpaceCreateDeviceRGB,
                        kCGImageAlphaNoneSkipLast, kCGBitmapByteOrder32Big)
    from CoreFoundation import CFDataCreate, CFURLCreateWithFileSystemPath, kCFURLPOSIXPathStyle
    arr = frame.astype(np.uint8)
    hpx, wpx = arr.shape[:2]
    rgba = np.dstack([arr, np.full((hpx, wpx), 255, np.uint8)]).tobytes()
    provider = CGDataProviderCreateWithCFData(CFDataCreate(None, rgba, len(rgba)))
    img = CGImageCreate(wpx, hpx, 8, 32, wpx * 4, CGColorSpaceCreateDeviceRGB(),
                        kCGImageAlphaNoneSkipLast | kCGBitmapByteOrder32Big,
                        provider, None, True, 0)
    url = CFURLCreateWithFileSystemPath(None, path, kCFURLPOSIXPathStyle, False)
    dest = CGImageDestinationCreateWithURL(url, 'public.png', 1, None)
    CGImageDestinationAddImage(dest, img, None)
    CGImageDestinationFinalize(dest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration', type=float, default=120)
    parser.add_argument('--interval', type=float, default=0.5)
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--no-voice', action='store_true')
    parser.add_argument('--no-hover', action='store_true')
    parser.add_argument('--snap', metavar='PNG')
    args = parser.parse_args()

    if not CGPreflightScreenCaptureAccess():
        print('此终端 App 没有屏幕录制权限：请在 系统设置 > 隐私与安全性 > 屏幕录制 '
              '中允许本终端，然后重试', flush=True)
        return 2

    if args.snap:
        windows = meeting_windows()
        for window, bounds in windows:
            frame = rgb_frame(int(window['kCGWindowNumber']))
            if frame is None:
                print(f"wid={window['kCGWindowNumber']} capture failed")
                continue
            print(f"wid={window['kCGWindowNumber']} name={window.get('kCGWindowName')!r} "
                  f"{int(bounds['Width'])}x{int(bounds['Height'])} -> "
                  f"{analyze(frame, mode_for(windows))} shape={frame.shape}")
            save_png(frame, args.snap.replace('.png', f"_{window['kCGWindowNumber']}.png"))
        print('saved:', args.snap.replace('.png', '_<wid>.png'))
        return 0

    print(f'mute_demo v3 running ({args.duration:.0f}s, interval {args.interval}s)',
          flush=True)
    last = None
    last_hover = 0.0
    deadline = time.monotonic() + args.duration
    while time.monotonic() < deadline:
        mode, result, bounds = sample()
        if (result['state'] == 'UNKNOWN(toolbar-hidden)' and not args.no_hover
                and bounds is not None and time.monotonic() - last_hover > 3.0):
            last_hover = time.monotonic()
            hover_nudge(bounds)
            mode, result, bounds = sample()
        line = (f'{time.strftime("%H:%M:%S")} [{mode or "-":4}] '
                f'red={result["red"]:<5} green={result["green"]:<5} '
                f'bright={result["bright"]:<6} -> {result["state"]}')
        if result['state'] != last or args.verbose:
            print(line, flush=True)
        if result['state'] != last:
            speak(result['state'].replace('+speaking', '，正在说话').replace(
                'UNKNOWN(toolbar-hidden)', '看不到工具栏'), not args.no_voice)
            last = result['state']
        time.sleep(args.interval)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
