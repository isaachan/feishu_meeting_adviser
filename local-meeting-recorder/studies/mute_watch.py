"""5.12 spike: classify Feishu meeting mic state from toolbar colors.

State model (from live UI inspection 2026-11-10):
  muted   -> mic button is RED with a diagonal slash (leftmost toolbar button)
  unmuted -> gray button; GREEN fill appears while speaking (voice activity)

Approach: poll the meeting window image ~1 fps, crop the bottom toolbar
strip, count red / green pixels in the central-left zone (the right ~25% is
excluded: the leave button is also red). Prints transitions; --json emits
one line per sample for logging.

Usage: mute_watch.py [--duration 60] [--interval 1.0] [--json]
"""
import argparse
import json
import time

import numpy as np
from Quartz import (
    CGDataProviderCopyData,
    CGImageGetBitsPerPixel,
    CGImageGetBytesPerRow,
    CGImageGetDataProvider,
    CGImageGetHeight,
    CGImageGetWidth,
    CGWindowListCopyWindowInfo,
    CGWindowListCreateImage,
    CGRectNull,
    kCGWindowImageBoundsIgnoreFraming,
    kCGWindowImageNominalResolution,
    kCGWindowListOptionOnScreenOnly,
    kCGNullWindowID,
)

MEETING_OWNERS = ('feishu meetings', '飞书会议')
RED = lambda r, g, b: (r > 110) & (r - g > 40) & (r - b > 40)
GREEN = lambda r, g, b: (g > 110) & (g - r > 35) & (g - b > 35)


def meeting_window():
    info = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                      kCGNullWindowID)
    for window in info:
        owner = str(window.get('kCGWindowOwnerName', '')).lower()
        if any(hint in owner for hint in MEETING_OWNERS):
            title = str(window.get('kCGWindowName', '') or '')
            if '会议' in title or 'meeting' in title.lower():
                return window
    return None


def grab(wid):
    img = CGWindowListCreateImage(CGRectNull,
                                  (1 << 3) | kCGWindowImageBoundsIgnoreFraming,
                                  wid, kCGWindowImageNominalResolution)
    if img is None:
        return None
    w, h = CGImageGetWidth(img), CGImageGetHeight(img)
    bpr, bpp = CGImageGetBytesPerRow(img), CGImageGetBitsPerPixel(img)
    data = CGDataProviderCopyData(CGImageGetDataProvider(img))
    arr = np.frombuffer(bytes(data), dtype=np.uint8).reshape(
        h, bpr // (bpp // 8), 4)
    return arr[:, :w, :3].astype(np.int16)


def classify(frame):
    """(state, red_px, green_px) from the bottom toolbar strip."""
    h, w = frame.shape[:2]
    strip = frame[int(h * 0.88):, :]
    x0, x1 = int(w * 0.20), int(w * 0.72)  # central-left zone, excl. leave btn
    zone = strip[:, x0:x1]
    r, g, b = zone[:, :, 0], zone[:, :, 1], zone[:, :, 2]
    red = int(RED(r, g, b).sum())
    green = int(GREEN(r, g, b).sum())
    if red > 40:
        state = 'muted'
    elif green > 40:
        state = 'unmuted+speaking'
    else:
        state = 'unmuted'
    return state, red, green


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration', type=float, default=60)
    parser.add_argument('--interval', type=float, default=1.0)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    last = None
    deadline = time.monotonic() + args.duration
    while time.monotonic() < deadline:
        window = meeting_window()
        if window is None:
            state, red, green = 'no-meeting-window', -1, -1
            wid = 0
        else:
            wid = window['kCGWindowNumber']
            frame = grab(wid)
            if frame is None:
                state, red, green = 'capture-failed', -1, -1
            else:
                state, red, green = classify(frame)
        if args.json:
            print(json.dumps({'t': round(time.monotonic(), 2), 'wid': wid,
                              'state': state, 'red': red, 'green': green}),
                  flush=True)
        elif state != last:
            print(f'{time.strftime("%H:%M:%S")} {state} (red={red} green={green})',
                  flush=True)
        last = state
        time.sleep(args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
