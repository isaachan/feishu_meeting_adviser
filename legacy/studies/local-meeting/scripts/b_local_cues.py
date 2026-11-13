#!/usr/bin/env python3
"""Play identifiable local acoustic beeps and log attempted playback times.

For a pilot only: use Mac speakers near the microphone, NOT headphones.
A subprocess launch timestamp is not the exact acoustic onset; calibrate that
uncertainty before using these results as a hard latency claim.
"""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import struct
import subprocess
import time
import wave


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('outdir', type=Path, help='Directory printed by monitor_vemedia.py')
    p.add_argument('--count', type=int, default=24)
    p.add_argument('--interval', type=float, default=10)
    p.add_argument('--lead', type=float, default=3, help='Wait before first beep (seconds)')
    a = p.parse_args()
    if a.count < 1 or a.interval <= 0 or a.lead < 0:
        p.error('count/interval/lead out of range')
    if not (a.outdir / 'events.jsonl').exists():
        p.error('Start monitor_vemedia.py first and pass its output directory')
    wav = a.outdir / 'cue_1000hz.wav'
    rate = 44100
    with wave.open(str(wav), 'wb') as fp:
        fp.setnchannels(1); fp.setsampwidth(2); fp.setframerate(rate)
        samples = []
        for i in range(int(rate * .3)):
            edge = min(1, i / (rate * .01), (rate * .3 - i) / (rate * .01))
            samples.append(int(14000 * max(0, edge) * math.sin(2 * math.pi * 1000 * i / rate)))
        fp.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
    start = time.monotonic() + a.lead
    with (a.outdir / 'cues.jsonl').open('x') as log:
        for i in range(a.count):
            deadline = start + a.interval * i
            while time.monotonic() < deadline:
                time.sleep(min(.1, deadline - time.monotonic()))
            before = time.monotonic_ns()
            try:
                result = subprocess.run(['afplay', str(wav)], capture_output=True, text=True, timeout=5)
                after = time.monotonic_ns()
                row = dict(number=i + 1, launch_monotonic_ns=before,
                           return_monotonic_ns=after, wall_time=datetime.now().astimezone().isoformat(),
                           success=result.returncode == 0, stderr=result.stderr[-300:])
            except (OSError, subprocess.TimeoutExpired) as exc:
                row = dict(number=i + 1, launch_monotonic_ns=before,
                           return_monotonic_ns=time.monotonic_ns(), success=False, error=str(exc))
            log.write(json.dumps(row, ensure_ascii=False) + '\n'); log.flush()
            print(row, flush=True)


if __name__ == '__main__':
    main()
