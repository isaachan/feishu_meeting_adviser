"""Silent-restart watchdog (2026-11-13 real-meeting fix).

飞书会议 is a separate app (com.electron.lark.iron, "Feishu Meetings") that
only appears in SCShareableContent once a meeting window opens. A capture
started before joining filters it out and delivers bit-perfect silence; the
watchdog must respawn the helper (rebuilding the filter) after
``silent_restart_s`` of exactly-zero audio.
"""
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from recorder.audio.sck_capture import SwiftLoopbackSource  # noqa: E402

FAKE_HELPER = r'''#!/usr/bin/env python3
import json, os, sys, time

counter = os.environ['FAKE_HELPER_COUNTER_FILE']
with open(counter) as fh:
    run = int(fh.read().strip() or 0) + 1
with open(counter, 'w') as fh:
    fh.write(str(run))

def emit(event, **fields):
    print(json.dumps({'event': event, **fields}), file=sys.stderr, flush=True)

emit('target', apps=[['com.electron.lark', 'Feishu', 1]])
emit('started', mode='audio-only')
emit('format', rate=16000, channels=1)

import struct
zero = struct.pack('<f', 0.0) * 1600          # 0.1s of silence
signal = struct.pack('<f', 0.25) + struct.pack('<f', 0.0) * 1599
try:
    if run == 1:
        while True:                            # capture-before-join: silence
            sys.stdout.buffer.write(zero)
            sys.stdout.buffer.flush()
            time.sleep(0.05)
    while True:                                # after restart: real audio flows
        sys.stdout.buffer.write(signal)
        sys.stdout.buffer.flush()
        time.sleep(0.05)
except BrokenPipeError:
    pass
'''


class SilentRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.counter = Path(self.tmp.name) / 'runs'
        self.counter.write_text('0')
        os.environ['FAKE_HELPER_COUNTER_FILE'] = str(self.counter)
        self.addCleanup(os.environ.pop, 'FAKE_HELPER_COUNTER_FILE', None)
        helper = Path(self.tmp.name) / 'fake_helper'
        helper.write_text(FAKE_HELPER)
        helper.chmod(0o755)
        self.helper = helper
        self.events = []
        self.frames = []
        self.lock = threading.RLock()  # predicates re-enter via _events_of

    def _sink(self, item):
        with self.lock:
            from recorder.audio.frames import SourceEvent
            if isinstance(item, SourceEvent):
                self.events.append(item.type)
            else:
                self.frames.append(item)

    def _wait(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if predicate():
                    return True
            time.sleep(0.05)
        return False

    def test_all_zero_stream_triggers_restart_and_then_audio_flows(self):
        source = SwiftLoopbackSource('loopback', binary=self.helper,
                                     silent_restart_s=0.6, max_silent_restarts=3)
        try:
            source.start(self._sink)
            self.assertTrue(self._wait(lambda: 'silent_restart' in self.events),
                            'watchdog never restarted the silent stream')
            self.assertTrue(self._wait(lambda: self._nonzero_frames()),
                            'no non-zero audio after restart')
            self.assertEqual(self.counter.read_text(), '2')
            with self.lock:
                kinds = self.events
            self.assertIn('gap_begin', kinds)
            self.assertIn('gap_end', kinds)
        finally:
            source.stop()

    def test_capped_restarts_do_not_spin_forever(self):
        # helper keeps writing zeros on every run: restarts stop at the cap
        zeros_only = FAKE_HELPER.replace(
            "    if run == 1:\n        while True:                            "
            "# capture-before-join: silence\n            sys.stdout.buffer.write(zero)\n"
            "            sys.stdout.buffer.flush()\n            time.sleep(0.05)\n",
            "")
        zeros_only = zeros_only.replace("signal = struct.pack('<f', 0.25) + struct.pack('<f', 0.0) * 1599",
                                        "signal = struct.pack('<f', 0.0) * 1600")
        helper = Path(self.tmp.name) / 'fake_helper_zero'
        helper.write_text(zeros_only)
        helper.chmod(0o755)
        self.helper = helper
        source = SwiftLoopbackSource('loopback', binary=helper,
                                     silent_restart_s=0.5, max_silent_restarts=2)
        try:
            source.start(self._sink)
            self.assertTrue(self._wait(lambda: self._events_of('silent_restart') == 2))
            time.sleep(1.2)  # well past another restart window
            self.assertEqual(self._events_of('silent_restart'), 2)
        finally:
            source.stop()

    def _events_of(self, kind):
        with self.lock:
            return self.events.count(kind)

    def _nonzero_frames(self):
        for frame in self.frames:
            if frame.data and frame.data.count(0) != len(frame.data):
                return True
        return False


if __name__ == '__main__':
    unittest.main()
