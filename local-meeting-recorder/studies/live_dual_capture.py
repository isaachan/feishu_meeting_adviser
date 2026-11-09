"""5.2 live check: real dual-path capture (SCK loopback + mic) for N seconds.

Run with the module venv. Requires Screen Recording + Microphone permission
for the host app. Speak into the mic while Feishu plays audio during the
window; per-second RMS is printed per source afterwards.
"""
import array
import json
import math
import sys
import time
import wave
from pathlib import Path

from local_meeting_recorder.audio import CaptureSession, MicSource
from local_meeting_recorder.audio.sck_capture import SwiftLoopbackSource

OUT = Path(sys.argv[2] if len(sys.argv) > 2 else 'live_capture_out')
DURATION = float(sys.argv[1] if len(sys.argv) > 1 else 12)


def wav_rms(path: Path) -> None:
    with wave.open(str(path), 'rb') as wf:
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = array.array('h', raw)
    if not samples:
        print(f'{path.name}: empty')
        return
    step = rate
    print(f'{path.name}: {rate} Hz, {len(samples)/rate:.1f}s')
    for sec in range(0, len(samples) // step):
        chunk = samples[sec * step:(sec + 1) * step]
        rms = math.sqrt(sum(v * v for v in chunk) / len(chunk)) / 32766
        print(f'  sec {sec:02d}: rms {20 * math.log10(max(rms, 1e-9)):7.1f} dB')
    peak = max(abs(v) for v in samples) / 32766
    print(f'  peak {peak:.3f}')


def main() -> int:
    loopback = SwiftLoopbackSource('loopback')
    mic = MicSource('mic')
    session = CaptureSession([loopback, mic], OUT)
    subscriber = session.subscribe()
    session.start()
    print(f'capturing {DURATION}s — speak now and keep Feishu audio playing')
    time.sleep(DURATION)
    summary = session.stop(timeout=15)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('--- events tail ---')
    for line in (OUT / 'events.jsonl').read_text().splitlines()[-6:]:
        print(' ', line)
    print('--- subscriber sample ---')
    while not subscriber.empty():
        item = subscriber.get_nowait()
        kind = item.type if hasattr(item, 'type') else 'frame'
        if kind != 'frame':
            print(' ', item)
    for name in ('loopback.wav', 'mic.wav'):
        wav_rms(OUT / name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
